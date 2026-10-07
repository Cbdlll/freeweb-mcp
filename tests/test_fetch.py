#!/usr/bin/env python3
"""web_fetch 详尽测试:各类站点、编码、反爬、格式、边界情况。

不依赖搜索引擎(直接抓目标站),不会与搜索测试互相触发限流。

用法: python3 tests/test_fetch.py
"""
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 2)[0])
import server  # noqa: E402

PASS_FAIL = []


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    PASS_FAIL.append(cond)
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return cond


CASES_OK = [
    # (标签, URL, 输出格式, 断言函数(content, meta))
    ("静态最小页 example.com", "https://example.com", "markdown",
     lambda c, m: m.get("http_status") == 200 and "domain" in c.lower()),
    ("GitHub 仓库页(markdown 提取)", "https://github.com/jlowin/fastmcp", "markdown",
     lambda c, m: m.get("format") == "markdown" and len(c) > 1000),
    ("中文门户(编码/噪音处理)", "https://finance.sina.com.cn", "markdown",
     lambda c, m: len(c) > 500 and any("\u4e00" <= ch <= "\u9fff" for ch in c[:2000])),
    ("维基百科条目", "https://en.wikipedia.org/wiki/Model_Context_Protocol", "markdown",
     lambda c, m: "Model Context Protocol" in c and len(c) > 1000),
    ("Hacker News(简单 HTML)", "https://news.ycombinator.com", "markdown",
     lambda c, m: len(c) > 200),
    ("纯 text 格式输出", "https://example.com", "text",
     lambda c, m: m.get("format") == "text" and "<html" not in c.lower()),
    ("html 原始输出", "https://example.com", "html",
     lambda c, m: m.get("format") == "html" and "<html" in c.lower()),
]

CASES_GRACEFUL = [
    # (标签, URL, 断言函数(result))
    ("强反爬站点优雅报错(知乎)", "https://www.zhihu.com/question/19554961",
     lambda r: ("error" in r) or ("http_status" in r and (r.get("http_status") or 0) >= 400)
     or len(r.get("content", "")) > 0),
    ("不存在域名优雅报错", "https://this-domain-definitely-does-not-exist-xyz123.com",
     lambda r: "error" in r),
    ("非法协议优雅报错", "ftp://example.com/file",
     lambda r: "error" in r and "http" in r.get("error", "")),
    ("PDF 文件优雅报错", "https://arxiv.org/pdf/1706.03762",
     lambda r: "error" in r or len(r.get("content", "")) > 0),
]

print("=" * 72)
print("1) 正常抓取(内容质量、格式、编码)")
print("=" * 72)
for label, url, fmt, ok_fn in CASES_OK:
    t0 = time.time()
    r = server.do_fetch(url, 20000, fmt)
    dt = time.time() - t0
    c = r.get("content", "")
    try:
        ok = ok_fn(c, r)
    except Exception as e:  # noqa: BLE001
        ok = False
        r["_exc"] = str(e)
    check(f"{label} ({dt:.1f}s)", ok,
          f"format={r.get('format')} len={r.get('length')} status={r.get('http_status')}"
          + (f" exc={r['_exc'][:50]}" if "_exc" in r else ""))

print()
print("=" * 72)
print("2) 截断与上限")
print("=" * 72)
r = server.do_fetch("https://github.com/jlowin/fastmcp", 3000, "markdown")
check("max_chars=3000 截断生效", r.get("truncated") and r.get("length") == 3000,
      f"len={r.get('length')} truncated={r.get('truncated')}")
r = server.do_fetch("https://example.com", 0, "markdown")
check("max_chars=0 走默认值", not r.get("truncated") and r.get("length", 0) > 0,
      f"len={r.get('length')}")

print()
print("=" * 72)
print("3) 边界与优雅降级(不崩溃、给可读错误)")
print("=" * 72)
for label, url, ok_fn in CASES_GRACEFUL:
    t0 = time.time()
    r = server.do_fetch(url, 5000, "markdown")
    dt = time.time() - t0
    try:
        ok = ok_fn(r)
    except Exception:  # noqa: BLE001
        ok = False
    check(f"{label} ({dt:.1f}s)", ok, f"error={str(r.get('error'))[:60]}")

total, okc = len(PASS_FAIL), sum(PASS_FAIL)
print()
print("=" * 72)
print(f"总计: {okc}/{total} 通过")
sys.exit(0 if okc == total else 1)
