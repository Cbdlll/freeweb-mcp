#!/usr/bin/env python3
"""FreeWebFetch MCP server — 免费无 key 的本地 web search / web fetch。

目标:替代 LLM API 官方内置的联网工具(按次消耗额度),提供完全免费、
本地部署、无需任何 API key 的联网搜索与网页抓取能力。

搜索:基于 ddgs 库直接抓取 google / brave / duckduckgo / bing / yandex /
mojeek / startpage / wikipedia / yahoo 的网页版结果,多引擎聚合 + 自动降级,
比单一 SearXNG(Google CSE 为主)覆盖面更广。

抓取:curl_cffi(Chrome TLS 指纹)→ httpx 兜底,trafilatura 正文提取
(markdown),可选 Jina Reader 兜底(需要 key,处理 JS 重度/强反爬页面)。

运行:python3 server.py   (stdio MCP)
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit

logging.basicConfig(
    level=os.environ.get("FREEWEB_LOG_LEVEL", "INFO"),
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("freewebfetch")

# ---------------------------------------------------------------------------
# 配置(全部可用环境变量覆盖)
# ---------------------------------------------------------------------------

# auto 模式下的引擎降级链:按顺序尝试,累计到 max_results 即止。
# 前两名是独立索引(google 抓取版、brave 独立索引),后面依次补位。
DEFAULT_ENGINE_CHAIN = os.environ.get(
    "FREEWEB_ENGINE_CHAIN", "google,brave,duckduckgo,bing,yandex,mojeek,startpage,yahoo"
)
TEXT_ENGINES = {
    "google", "brave", "duckduckgo", "bing", "yandex",
    "mojeek", "startpage", "wikipedia", "yahoo",
}
NEWS_ENGINES = {"bing_news", "duckduckgo_news", "yahoo_news"}

FETCH_TIMEOUT = float(os.environ.get("FREEWEB_FETCH_TIMEOUT", "25"))
SEARCH_TIMEOUT = float(os.environ.get("FREEWEB_SEARCH_TIMEOUT", "10"))
MAX_CHARS_DEFAULT = int(os.environ.get("FREEWEB_MAX_CHARS", "60000"))
REGION = os.environ.get("FREEWEB_REGION", "us-en")  # ddgs 区域格式,如 us-en / cn-zh
SAFESEARCH = os.environ.get("FREEWEB_SAFESEARCH", "moderate")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
# 显式代理(可选)。多数 MCP 客户端 spawn 子进程时不继承 HTTP_PROXY 等变量;
# 若依赖代理访问国际引擎,请在 MCP 配置的 env 中设置:
#   FREEWEB_PROXY=http://127.0.0.1:7897  (或标准 HTTP_PROXY/HTTPS_PROXY)
# 留空则使用标准代理环境变量(若进程有继承到)。
FREEWEB_PROXY = os.environ.get("FREEWEB_PROXY", "")
# auto 链整体搜索时限(秒),防止最坏情况下整条链拖满超时
SEARCH_DEADLINE = float(os.environ.get("FREEWEB_SEARCH_DEADLINE", "40"))
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ddgs/primp 的逐请求 INFO 日志太吵(stderr 会被 MCP 客户端当服务日志),压到 ERROR
for _noisy in ("ddgs", "ddgs.ddgs", "primp", "httpx", "httpcore", "curl_cffi"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

# ---------------------------------------------------------------------------
# 搜索实现
# ---------------------------------------------------------------------------

_TRACK_PARAMS = re.compile(r"^(utm_|ref_|gclid|fbclid|spm|from|vd_source)", re.I)


def _norm_url(url: str) -> str:
    """用于去重的规范化 URL:去 fragment、去常见跟踪参数。"""
    try:
        parts = urlsplit(url.strip())
        query = "&".join(
            kv for kv in parts.query.split("&")
            if kv and not _TRACK_PARAMS.match(kv.split("=", 1)[0])
        )
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                           parts.path, query, ""))
    except Exception:
        return url.strip()


def _ddgs():
    """线程级复用 DDGS 客户端(避免每次调用重建 TLS 会话;客户端本身轻量但握手不便宜)。"""
    import threading
    from ddgs import DDGS
    tl = getattr(_ddgs, "_tl", None)
    if tl is None:
        tl = _ddgs._tl = threading.local()
    client = getattr(tl, "client", None)
    if client is None:
        kw = {"timeout": SEARCH_TIMEOUT}
        proxy = FREEWEB_PROXY or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            kw["proxy"] = proxy
        client = tl.client = DDGS(**kw)
    return client


# 全局请求节流:两次搜索引擎请求之间的最小间隔(秒),0 关闭。
# 免费 schema 靠"不像爬虫"生存;真实 LLM 会话本就是单发请求,
# 节流可显著降低触发引擎应用层风控(验证码/空结果)的概率。
_MIN_INTERVAL = float(os.environ.get("FREEWEB_MIN_INTERVAL", "0"))
_LAST_REQ = [0.0]
_THROTTLE_LOCK = threading.Lock()


def _throttle() -> None:
    if _MIN_INTERVAL <= 0:
        return
    with _THROTTLE_LOCK:
        wait = _LAST_REQ[0] + _MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _LAST_REQ[0] = time.monotonic()


def _search_one_engine(engine: str, query: str, max_results: int,
                       timelimit: str | None, region: str,
                       attempts: int = 1, max_pages: int = 1) -> list[dict]:
    """单引擎搜索:带翻页(ddgs 单页约 5-10 条)与小重试。"""
    base_kwargs = dict(
        region=region or REGION,
        safesearch=SAFESEARCH,
    )
    if timelimit:
        base_kwargs["timelimit"] = timelimit

    collected: list[dict] = []
    seen: set[str] = set()
    for attempt in range(attempts):
        for page in range(1, max_pages + 1):
            _throttle()
            try:
                # 注意:ddgs 的引擎选择参数是 backend(传 engine 会被静默忽略)
                raw = _ddgs().text(query, max_results=max_results - len(collected),
                                   page=page, backend=engine, **base_kwargs)
            except Exception as e:  # noqa: BLE001 — 单引擎失败不应影响整体
                log.warning("engine %s page %s failed: %s", engine, page, e)
                break
            new = 0
            for r in raw or []:
                title = (r.get("title") or "").strip()
                href = (r.get("href") or r.get("url") or r.get("link") or "").strip()
                body = (r.get("body") or r.get("description") or r.get("snippet") or "").strip()
                key = _norm_url(href)
                if href and key not in seen:
                    seen.add(key)
                    collected.append({"title": title, "url": href, "snippet": body})
                    new += 1
            if len(collected) >= max_results or new == 0:
                break
        if collected or attempt + 1 >= attempts:
            break
        time.sleep(0.5 + random.random())  # 空手而归且可重试 → 抖动退避,缓解瞬时限流
    return collected


def _merge(results_by_engine: dict[str, list[dict]], max_results: int) -> list[dict]:
    """按引擎顺序合并去重;主引擎结果排前,并记录来源引擎。"""
    seen: set[str] = set()
    merged: list[dict] = []
    for engine, items in results_by_engine.items():
        for it in items:
            key = _norm_url(it["url"])
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append({**it, "engine": engine})
            if len(merged) >= max_results:
                return merged
    return merged


def _cache_key(parts: tuple) -> str:
    return json.dumps(parts, ensure_ascii=False, sort_keys=True)


_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_LOCK = __import__("threading").Lock()
_CACHE_TTL = float(os.environ.get("FREEWEB_CACHE_TTL", "600"))  # 秒,0 关闭


def _cache_get(key: str) -> dict | None:
    if _CACHE_TTL <= 0:
        return None
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL:
            return json.loads(json.dumps(hit[1], ensure_ascii=False))  # 深拷贝防调用方改缓存
        return None


def _cache_put(key: str, value: dict) -> None:
    if _CACHE_TTL <= 0:
        return
    with _CACHE_LOCK:
        if len(_CACHE) > 256:  # 粗略容量上限
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)
        _CACHE[key] = (time.monotonic(), json.loads(json.dumps(value, ensure_ascii=False)))


# 引擎熔断:某引擎刚失败(常见为瞬时限流/验证码),短时间内 auto 链跳过它,
# 避免每条查询都在坏引擎上白等数秒。连败冷却指数退避(45s→90s→…封顶 300s),
# 成功即清零。显式指定引擎时不受熔断限制。
_COOLDOWN_BASE = float(os.environ.get("FREEWEB_ENGINE_COOLDOWN", "45"))
_ENGINE_COOLDOWN: dict[str, float] = {}
_ENGINE_FAILS: dict[str, int] = {}
_CB_LOCK = __import__("threading").Lock()


def _cooldown_set(engine: str) -> None:
    with _CB_LOCK:
        n = _ENGINE_FAILS.get(engine, 0) + 1
        _ENGINE_FAILS[engine] = n
        _ENGINE_COOLDOWN[engine] = time.monotonic() + min(
            _COOLDOWN_BASE * (2 ** (n - 1)), 300)


def _cooldown_clear(engine: str) -> None:
    with _CB_LOCK:
        _ENGINE_FAILS.pop(engine, None)
        _ENGINE_COOLDOWN.pop(engine, None)


def _in_cooldown(engine: str) -> bool:
    with _CB_LOCK:
        until = _ENGINE_COOLDOWN.get(engine)
    return bool(until and time.monotonic() < until)


# 引擎健康度 EMA(近期成功率),用于 auto 链自适应排序:
# 当前网络环境下表现好的引擎优先,被限流的排后(配合熔断跳过)。
_ENGINE_STATS: dict[str, dict] = {}


def _record(engine: str, ok: bool) -> None:
    with _CB_LOCK:
        s = _ENGINE_STATS.setdefault(engine, {"ok": 0.0, "n": 0.0})
        s["n"] = s["n"] * 0.8 + 1
        s["ok"] = s["ok"] * 0.8 + (1.0 if ok else 0.0)


def _health(engine: str) -> float:
    with _CB_LOCK:
        s = _ENGINE_STATS.get(engine)
    if not s or s["n"] < 1:
        return 0.5  # 无数据排中间
    return s["ok"] / s["n"]


def do_search(query: str, max_results: int, engines: str,
              timelimit: str | None, region: str = "") -> dict:
    """多引擎聚合搜索。engines: 'auto' | 逗号分隔引擎名。"""
    if engines.strip().lower() == "auto":
        chain = [e.strip() for e in DEFAULT_ENGINE_CHAIN.split(",") if e.strip()]
    else:
        chain = []
        for name in engines.split(","):
            name = name.strip().lower()
            if not name:
                continue
            if name not in TEXT_ENGINES:
                return {"error": f"未知引擎 '{name}'。可用: {', '.join(sorted(TEXT_ENGINES))} 或 'auto'"}
            chain.append(name)
    if not chain:
        return {"error": "引擎列表为空"}

    # auto 模式:顺序降级,每轮后累计去重,凑够即停;
    # 指定引擎模式:全部执行再合并(用户明确要多引擎对比)。
    auto = engines.strip().lower() == "auto"
    ckey = _cache_key(("search", query, max_results, tuple(sorted(_norm_url(e) for e in chain)), timelimit, region or REGION))
    cached = _cache_get(ckey)
    if cached is not None:
        cached["cached"] = True
        return cached
    deadline = time.monotonic() + SEARCH_DEADLINE
    collected: dict[str, list[dict]] = {}
    merged: list[dict] = []
    ok_engines = 0
    # auto 模式下:跳过熔断中的引擎,并按近期健康度(EMA 成功率)重排,
    # 让当前网络下表现好的引擎优先;全部熔断时仍试前两个(防饿死)。
    if auto:
        live = sorted((e for e in chain if not _in_cooldown(e)),
                      key=lambda e: -_health(e))
        if live:
            chain = live
        elif len(chain) > 2:
            chain = chain[:2]
    for engine in chain:
        # auto 模式:1 次尝试 + 单页(靠降级链补位,压延迟);
        # 指定引擎:2 次尝试 + 多页翻满(用户点名要它,值得花时间)。
        items = _search_one_engine(engine, query, max_results, timelimit, region,
                                   attempts=1 if auto else 2,
                                   max_pages=1 if auto else 4)
        collected[engine] = items
        merged = _merge(collected, max_results)
        _record(engine, bool(items))
        if items:
            ok_engines += 1
            _cooldown_clear(engine)
        else:
            _cooldown_set(engine)
        if auto and (len(merged) >= max_results
                     or (ok_engines >= 2 and len(merged) >= 5)
                     or time.monotonic() > deadline):
            break
    failures = [e for e, v in collected.items() if not v]
    result = {
        "query": query,
        "engines_used": [e for e in chain if collected.get(e)],
        "engines_failed": failures,
        "results": merged,
    }
    if merged:
        _cache_put(ckey, result)
    else:
        result["error"] = (
            "所有引擎均无结果。可能原因:网络反爬/限流(稍后重试)、"
            "查询过冷门(尝试英文关键词或换引擎)、或触发验证码。"
            f"失败引擎: {failures or chain}"
        )
    return result


def do_news_search(query: str, max_results: int, engines: str, region: str = "") -> dict:
    """新闻搜索(近 1 月为主,ddgs news 系引擎)。"""
    if engines.strip().lower() == "auto":
        chain = ["bing_news", "duckduckgo_news", "yahoo_news"]
    else:
        chain = [e.strip().lower() for e in engines.split(",") if e.strip()]
        bad = [e for e in chain if e not in NEWS_ENGINES]
        if bad:
            return {"error": f"未知新闻引擎 {bad}。可用: {', '.join(sorted(NEWS_ENGINES))} 或 'auto'"}

    collected: dict[str, list[dict]] = {}
    ckey = _cache_key(("news", query, max_results, engines.strip().lower(), region or REGION))
    cached = _cache_get(ckey)
    if cached is not None:
        cached["cached"] = True
        return cached
    deadline = time.monotonic() + SEARCH_DEADLINE
    for engine in chain:
        if time.monotonic() > deadline and collected:
            break
        try:
            _throttle()
            raw = _ddgs().text(query, backend=engine, max_results=max_results,
                               region=region or REGION, safesearch=SAFESEARCH)
        except Exception as e:  # noqa: BLE001
            log.warning("news engine %s failed: %s", engine, e)
            raw = []
        collected[engine] = [
            {"title": (r.get("title") or "").strip(),
             "url": (r.get("href") or r.get("url") or "").strip(),
             "snippet": (r.get("body") or "").strip(),
             "date": (r.get("date") or r.get("published") or r.get("timestamp") or "").strip()}
            for r in raw or [] if (r.get("href") or r.get("url"))
        ]
    merged = _merge(collected, max_results)
    result = {
        "query": query,
        "engines_used": [e for e in chain if collected.get(e)],
        "engines_failed": [e for e, v in collected.items() if not v],
        "results": merged,
    }
    if merged:
        _cache_put(ckey, result)
    else:
        result["error"] = "新闻引擎均无结果,可稍后重试或换关键词。"
    return result

# ---------------------------------------------------------------------------
# 抓取实现
# ---------------------------------------------------------------------------


def _proxy_url() -> str:
    """显式配置 > 标准环境变量。"""
    return FREEWEB_PROXY or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""


def _fetch_html(url: str) -> tuple[str, str | None, int]:
    """抓取原始 HTML。返回 (html, error, status)。curl_cffi 优先,httpx 兜底。"""
    errors: list[str] = []
    purl = _proxy_url()
    try:
        from curl_cffi import requests as creq
        kw = {"proxies": {"http": purl, "https": purl}} if purl else {}
        r = creq.get(url, timeout=FETCH_TIMEOUT, impersonate="chrome",
                     allow_redirects=True, headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                     **kw)
        if r.status_code < 400 and r.text:
            return r.text, None, r.status_code
        errors.append(f"curl_cffi http {r.status_code}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"curl_cffi: {type(e).__name__}")

    try:
        import httpx
        r = httpx.get(url, timeout=FETCH_TIMEOUT, follow_redirects=True,
                      headers={"User-Agent": BROWSER_UA,
                               "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
                      **({"proxy": purl} if purl else {}))
        if r.status_code < 400 and r.text:
            return r.text, None, r.status_code
        errors.append(f"httpx http {r.status_code}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"httpx: {type(e).__name__}")
    return "", "; ".join(errors) or "unknown", 0


def _jina_reader(url: str) -> str | None:
    """Jina Reader 兜底(JS 重度/强反爬页面)。需要 JINA_API_KEY。"""
    if not JINA_API_KEY:
        return None
    try:
        import httpx
        kw = {"proxy": _proxy_url()} if _proxy_url() else {}
        r = httpx.get(f"https://r.jina.ai/{url}", timeout=FETCH_TIMEOUT + 20,
                      headers={"Authorization": f"Bearer {JINA_API_KEY}",
                               "X-Return-Format": "markdown"}, **kw)
        if r.status_code == 200 and r.text.strip():
            return r.text
    except Exception as e:  # noqa: BLE001
        log.warning("jina reader failed: %s", e)
    return None


def do_fetch(url: str, max_chars: int, output_format: str) -> dict:
    """抓取网页并提取正文。output_format: markdown | text | html | raw。"""
    url = url.strip()
    if max_chars <= 0:
        max_chars = MAX_CHARS_DEFAULT
    if not re.match(r"^https?://", url, re.I):
        return {"error": f"仅支持 http(s) URL,收到: {url[:100]}"}

    html, err, status = _fetch_html(url)

    def truncated(text: str, label: str) -> dict:
        note = ""
        if len(text) > max_chars:
            text = text[:max_chars]
            note = f"(内容已截断至 {max_chars} 字符,原文更长)"
        return {"url": url, "http_status": status, "format": label,
                "length": len(text), "truncated": bool(note), "note": note,
                "content": text}

    if err and not html:
        # 本地直连失败 → 可选 Jina 兜底(反爬/网络原因)
        jina = _jina_reader(url)
        if jina:
            out = truncated(jina, "markdown(jina)")
            out["http_status"] = None
            out["via"] = "jina_reader"
            return out
        return {"error": f"抓取失败: {err}",
                "hint": "该站点可能需要浏览器渲染、有强反爬、或当前网络不可达。"
                        "可尝试配置 JINA_API_KEY 环境变量启用 Jina Reader 兜底。"}

    # PDF/二进制守卫:正文提取器只吃 HTML,二进制内容会变成乱码
    if html[:5].lstrip() == "%PDF-":
        return {"url": url, "http_status": status,
                "error": "目标是 PDF 文件,本工具只提取 HTML 正文。",
                "hint": "可先用系统工具下载后再解析,或改用 output_format='html' 自行处理。"}

    if output_format == "raw" or output_format == "html":
        return truncated(html, "html")

    # 正文提取:trafilatura → BeautifulSoup 兜底
    try:
        import trafilatura
        extracted = trafilatura.extract(
            html, url=url, output_format="markdown" if output_format == "markdown" else "txt",
            include_comments=False, include_tables=True, favor_recall=True,
        )
        if extracted and extracted.strip():
            return truncated(extracted, output_format)
    except Exception as e:  # noqa: BLE001
        log.warning("trafilatura failed on %s: %s", url, e)

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "aside"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        title = soup.title.get_text(strip=True) if soup.title else ""
        content = (f"# {title}\n\n{text}" if title else text)
        if content.strip():
            return truncated(content, "text(bs4)")
    except Exception as e:  # noqa: BLE001
        log.warning("bs4 fallback failed on %s: %s", url, e)

    # 提取失败(多半是 JS 渲染页)→ Jina 兜底 → 退回原始 HTML
    jina = _jina_reader(url)
    if jina:
        return truncated(jina, "markdown(jina)")
    return truncated(html, "html(正文提取失败,返回原始 HTML,可能为 JS 渲染页)")

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------


try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x 兼容
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

mcp = _Server(
    "freewebfetch",
    instructions=(
        "免费无 key 的本地联网工具集:web_search 多引擎聚合搜索(自动降级),"
        "web_fetch 网页正文抓取(markdown),news_search 新闻搜索。"
    ),
)


@mcp.tool()
def web_search(
    query: str,
    max_results: int = 8,
    engines: str = "auto",
    timelimit: str | None = None,
    region: str = "",
) -> str:
    """多引擎聚合网络搜索,完全免费、无需 API key。

    Args:
        query: 搜索关键词,支持中英文;结果差时可改写为英文重试。
        max_results: 期望返回的结果数(1-20),多引擎会去重合并。
        engines: "auto"(默认:先跳过熔断中的引擎,再按近期健康度动态排序,
                 凑够即止)或逗号分隔的指定引擎列表
                 (如 "google,brave,wikipedia",全部执行并合并对比)。
                 可用引擎: google, brave, duckduckgo, bing, yandex, mojeek,
                 startpage, wikipedia, yahoo。
        timelimit: 时间过滤:"d"=一天内, "w"=一周内, "m"=一月内, "y"=一年内。
        region: 区域偏好,格式如 us-en(美英)、cn-zh(中中)、hk-tzh、jp-ja;
                留空用环境变量 FREEWEB_REGION(默认 us-en)。
    """
    max_results = max(1, min(int(max_results), 20))
    return json.dumps(
        do_search(query, max_results, engines, timelimit, region.strip().lower()),
        ensure_ascii=False)


@mcp.tool()
def news_search(query: str, max_results: int = 8, engines: str = "auto",
                region: str = "") -> str:
    """新闻搜索(bing_news / duckduckgo_news / yahoo_news 聚合),免费无 key。

    Args:
        query: 新闻关键词。
        max_results: 期望返回条数(1-20)。
        engines: "auto" 或逗号分隔: bing_news, duckduckgo_news, yahoo_news。
    """
    max_results = max(1, min(int(max_results), 20))
    return json.dumps(
        do_news_search(query, max_results, engines, region.strip().lower()),
        ensure_ascii=False)


@mcp.tool()
def web_fetch(url: str, max_chars: int = 0, output_format: str = "markdown") -> str:
    """抓取网页并提取正文为 markdown/text(自动去广告/导航等噪音),免费。

    Args:
        url: 要抓取的 http(s) 地址。
        max_chars: 返回正文最大字符数,0 表示用默认值(60000)。
        output_format: "markdown"(默认,正文转 markdown) | "text"(纯文本) |
                        "html"(原始 HTML,适合自行解析)。
    """
    if max_chars <= 0:
        max_chars = MAX_CHARS_DEFAULT
    return json.dumps(do_fetch(url, min(max_chars, 300000), output_format.strip().lower()),
                      ensure_ascii=False)


if __name__ == "__main__":
    log.info("FreeWebFetch MCP server starting (stdio)…")
    mcp.run()
