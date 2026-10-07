#!/usr/bin/env python3
"""端到端 MCP 协议测试:spawn stdio server → initialize → list tools → 调用工具。

用法: python3 tests/test_mcp_e2e.py
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "server.py")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _check(name: str, cond: bool, detail: str = ""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    return cond


async def main() -> int:
    # 完整环境 spawn:模拟 MCP 配置里 env 继承了代理变量的正确部署。
    # (mcp 客户端默认只继承 HOME/PATH 等,会丢掉 HTTP(S)_PROXY —— 见 README)
    params = StdioServerParameters(command=sys.executable, args=[SERVER],
                                   env=dict(os.environ))
    ok = True
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            name = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
            ok &= _check("initialize 握手", bool(name and name.name),
                         f"server={name.name if name else '?'}")
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            ok &= _check("list_tools 三个工具",
                         {"web_search", "web_fetch", "news_search"} <= names,
                         f"tools={sorted(names)}")

            # web_search 调用
            res = await session.call_tool("web_search", {"query": "what is MCP protocol", "max_results": 4})
            data = json.loads(res.content[0].text)
            # 契约级断言:要么有结果,要么给出结构化的"全部引擎无结果"错误
            # (搜索引擎对数据中心/代理出口 IP 的应用层限流会随时间波动)
            ok &= _check("web_search 契约(结果或清晰错误)",
                         len(data.get("results", [])) >= 1 or "error" in data,
                         f"engines={data.get('engines_used')} n={len(data.get('results', []))} err={data.get('error', '')[:30]}")

            # 中文搜索
            res = await session.call_tool("web_search", {"query": "MCP 协议 教程", "max_results": 4})
            data = json.loads(res.content[0].text)
            ok &= _check("web_search 中文契约",
                         len(data.get("results", [])) >= 1 or "error" in data,
                         f"first={data['results'][0]['title'][:30] if data.get('results') else data.get('error', '')[:30]}")

            # web_fetch 调用
            res = await session.call_tool("web_fetch", {"url": "https://example.com", "max_chars": 1000})
            data = json.loads(res.content[0].text)
            ok &= _check("web_fetch example.com",
                         data.get("http_status") == 200 and "domain" in data.get("content", "").lower()
                         and len(data.get("content", "")) > 50,
                         f"format={data.get('format')} len={data.get('length')}")

            # news_search 调用
            res = await session.call_tool("news_search", {"query": "AI", "max_results": 3})
            data = json.loads(res.content[0].text)
            ok &= _check("news_search 契约",
                         len(data.get("results", [])) >= 1 or "error" in data,
                         f"engines={data.get('engines_used')} n={len(data.get('results', []))}")

            # 错误路径:非法 URL
            res = await session.call_tool("web_fetch", {"url": "ftp://bad"})
            data = json.loads(res.content[0].text)
            ok &= _check("web_fetch 非法 URL 报错", "error" in data, detail=data.get("error", "")[:40])

            # 错误路径:非法引擎
            res = await session.call_tool("web_search", {"query": "x", "engines": "nope"})
            data = json.loads(res.content[0].text)
            ok &= _check("web_search 非法引擎报错", "error" in data, detail=data.get("error", "")[:40])

    print("\n" + ("🎉 全部通过" if ok else "❌ 存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
