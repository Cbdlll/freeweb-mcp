#!/usr/bin/env python3
"""搜索引擎结果详尽测试。

覆盖:多语言、多类型查询、九引擎覆盖矩阵、auto 链成功率、去重正确性、
时间过滤、区域参数。这是针对"SearXNG 以 Google CSE 为主搜不到东西"痛点
的核心验证:多引擎独立索引聚合应显著提高召回。

用法:
  python3 tests/test_search_quality.py                 # 全部 4 段(约 40 请求)
  python3 tests/test_search_quality.py --section 1     # 只跑某段(1-4)
    段1 多类型查询 × auto(8 查询级)   段2 九引擎覆盖矩阵(9 请求级)
    段3 参数行为(timelimit/region/合并/翻页/news)   段4 auto 链稳定性(5 查询级)
分段用途:搜索引擎对数据中心/共享代理出口 IP 有应用层风控,重流量会在
几分钟内触发验证码;分窗口各跑一段可让每段都在恢复窗口内完成。
建议搭配节流:FREEWEB_MIN_INTERVAL=3 python3 tests/test_search_quality.py
"""
import sys
import time
from collections import Counter

sys.path.insert(0, __file__.rsplit("/", 2)[0])
import server  # noqa: E402

ARGS = sys.argv[1:]
SECTION = None
if "--section" in ARGS:
    SECTION = str(ARGS[ARGS.index("--section") + 1])

PASS_FAIL = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    PASS_FAIL.append(cond)
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return cond


def norm(url):
    return server._norm_url(url)


def section_1():
    """多类型查询 × auto 模式(默认链)。"""
    print("=" * 72)
    print("1) 多类型查询 × auto 模式(默认链)")
    print("=" * 72, flush=True)

    QUERIES_AUTO = [
        ("英文技术文档", "fastmcp python tutorial"),
        ("中文技术内容", "MCP 协议 教程 中文"),
        ("冷门长尾项目", "ddgs python pypi library"),
        ("学术经典论文", "attention is all you need paper arxiv"),
        ("新近技术话题", "GLM-4.6 model benchmark"),
        ("生活化中文查询", "武汉 热干面 做法"),
        ("错误排查类", "python asyncio RuntimeError event loop is closed fix"),
        ("比较类查询", "rust vs go performance 2025"),
    ]
    auto_ok = 0
    auto_latencies = []
    for label, q in QUERIES_AUTO:
        t0 = time.time()
        r = server.do_search(q, 8, "auto", None)
        dt = time.time() - t0
        n = len(r.get("results", []))
        dup = n - len({norm(x["url"]) for x in r.get("results", [])})
        ok = n >= 5 and dup == 0 and not r.get("error")
        auto_ok += ok
        auto_latencies.append(dt)
        time.sleep(1.0)  # 礼貌节流,降低触发搜索引擎反爬的概率
        check(f"auto[{label}] '{q[:30]}'", ok,
              f"n={n} engines={r.get('engines_used')} fail={r.get('engines_failed')} "
              f"dup={dup} {dt:.1f}s")

    check("auto 成功率 ≥ 7/8", auto_ok >= 7, f"{auto_ok}/8")
    if auto_latencies:
        check("auto 平均延迟 < 15s", sum(auto_latencies) / len(auto_latencies) < 15,
              f"avg={sum(auto_latencies)/len(auto_latencies):.1f}s")


def section_2():
    """九引擎覆盖矩阵 + 多引擎召回对照。"""
    print("=" * 72)
    print("2) 九引擎覆盖矩阵(同一组查询逐引擎跑)")
    print("=" * 72, flush=True)

    ENGINES = ["google", "brave", "duckduckgo", "bing", "yandex",
               "mojeek", "startpage", "wikipedia", "yahoo"]
    # 单查询×9引擎:足够验证各引擎功能,负载更低(重流量会触发出口IP风控)
    MATRIX_QUERIES = ["attention is all you need"]
    coverage = Counter()
    for q in MATRIX_QUERIES:
        row = {}
        for e in ENGINES:
            try:
                r = server.do_search(q, 5, e, None)
                n = len(r.get("results", []))
            except Exception:
                n = 0
            row[e] = n
            if n >= 1:
                coverage[e] += 1
            time.sleep(1.5)  # 礼貌节流
        print(f"  {q[:28]:30s} " + " ".join(f"{e[:4]}={row[e]}" for e in ENGINES),
              flush=True)

    print("  覆盖统计(查询中有结果的次数):")
    for e in ENGINES:
        print(f"    {e:12s} {coverage[e]}/{len(MATRIX_QUERIES)}")
    # 严格门槛:引擎在矩阵查询上有结果即算达标(矩阵为单查询,降低触发风控的负载)
    strong = [e for e in ENGINES if coverage[e] >= 1]
    check("至少 4/9 引擎在查询上有结果", len(strong) >= 4, f"达标引擎: {strong}")

    # SearXNG 痛点对照:冷门/长尾查询下,独立索引引擎(brave/yandex/mojeek 等)
    # 应贡献 google 之外的新结果(引擎层直接采集,不受 max_results 封顶影响)。
    NICHE = ["ddgs python pypi library", "GLM-4.6 model benchmark", "MCP 协议 教程 中文"]
    g_unique, m_unique = set(), set()
    contrib = {}
    for q in NICHE:
        for e in ["google", "brave", "duckduckgo", "bing", "yahoo"]:
            try:
                items = server._search_one_engine(e, q, 10, None, "us-en", attempts=1)
            except Exception:
                items = []
            if e == "google":
                g_unique |= {norm(x["url"]) for x in items}
            contrib.setdefault(e, set()).update(norm(x["url"]) for x in items)
            time.sleep(0.8)  # 礼貌节流
        others = [v for k, v in contrib.items() if k != "google"] or [set()]
        m_unique |= set().union(*others) | g_unique
    print("  各引擎独立 URL 贡献: " + " ".join(f"{k}={len(v)}" for k, v in contrib.items()),
          flush=True)
    check("冷门查询下多引擎比 google 单独多 ≥20% 独立结果",
          len(m_unique) >= len(g_unique) * 1.2,
          f"google_only={len(g_unique)} union={len(m_unique)} (+{len(m_unique)-len(g_unique)})")


def section_3():
    """参数行为:timelimit / region / max_results / engines 合并去重 / news。"""
    print("=" * 72)
    print("3) 参数行为:timelimit / region / max_results / engines 合并去重")
    print("=" * 72, flush=True)

    r = server.do_search("AI news", 5, "auto", "w")
    check("timelimit=w 有结果", len(r.get("results", [])) >= 3,
          f"n={len(r.get('results', []))}")

    r = server.do_search("python tutorial", 6, "auto", None, region="cn-zh")
    check("region=cn-zh 有结果", len(r.get("results", [])) >= 3,
          f"n={len(r.get('results', []))}")

    # 动态挑选当前健康(能出结果)的引擎做合并与翻页测试,避免测试结果
    # 被特定引擎的临时风控状态绑架(数据中心/代理出口 IP 常被应用层限流)。
    def pick_healthy_engines(k=3):
        ok_engines = []
        for e in ["bing", "yahoo", "yandex", "google", "brave", "duckduckgo",
                  "mojeek", "startpage", "wikipedia"]:
            try:
                items = server._search_one_engine(e, "python tutorial", 3, None, "us-en", attempts=1)
            except Exception:
                items = []
            if items:
                ok_engines.append(e)
            if len(ok_engines) >= k:
                break
            time.sleep(0.8)
        return ok_engines

    healthy = pick_healthy_engines(3)
    if len(healthy) >= 3:
        r = server.do_search("python", 3, ",".join(healthy[:3]), None)
        urls = [norm(x["url"]) for x in r.get("results", [])]
        check(f"指定 3 引擎({','.join(healthy[:3])})合并无重复",
              len(urls) == len(set(urls)) and len(urls) == 3, f"n={len(urls)}")
        r = server.do_search("python tutorial", 20, ",".join(healthy), None)
        check(f"max_results 上限 20 生效(引擎 {healthy})", len(r.get("results", [])) == 20,
              f"n={len(r.get('results', []))}")
    else:
        print(f"[SKIP] 合并/翻页测试——当前仅 {healthy} 个引擎可用(环境风控)", flush=True)

    r = server.do_news_search("artificial intelligence", 5, "auto")
    dates = [x.get("date", "") for x in r.get("results", [])]
    check("news_search 返回结果", len(r.get("results", [])) >= 3,
          f"n={len(r.get('results', []))} 带日期 {sum(1 for d in dates if d)}/{len(dates)}(部分新闻引擎不提供,不强制)")


def section_4():
    """auto 链稳定性。"""
    print("=" * 72)
    print("4) auto 链稳定性(连续 5 次,含限流容错)")
    print("=" * 72, flush=True)
    succ = 0
    lat_ok = []
    for i in range(5):
        t0 = time.time()
        r = server.do_search(f"python web framework comparison {i % 3}", 5, "auto", None)
        dt = time.time() - t0
        if r.get("results"):
            succ += 1
            lat_ok.append(dt)
        time.sleep(1.5)
    lat_rep = f"avg={sum(lat_ok)/len(lat_ok):.1f}s(成功查询)" if lat_ok else "无成功查询"
    check("auto 连续 5 次成功率 100%", succ == 5, f"{succ}/5, {lat_rep}")


SECTIONS = {"1": section_1, "2": section_2, "3": section_3, "4": section_4}

if __name__ == "__main__":
    todo = [SECTIONS[SECTION]] if SECTION in SECTIONS else list(SECTIONS.values())
    if SECTION and SECTION not in SECTIONS:
        print(f"未知段号 {SECTION},可用: {list(SECTIONS)}")
        sys.exit(2)
    for fn in todo:
        print(flush=True)
        fn()
    print(flush=True)
    total, okc = len(PASS_FAIL), sum(PASS_FAIL)
    print("=" * 72)
    scope = f"段 {SECTION}" if SECTION else "全部"
    print(f"{scope}: {okc}/{total} 通过")
    sys.exit(0 if okc == total else 1)
