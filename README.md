# FreeWebFetch MCP

**简体中文** | [English](README.en.md)

免费、无 API key、本地部署的 [MCP](https://modelcontextprotocol.io) server,为 LLM 提供联网搜索与网页抓取能力,替代 LLM API 官方按次计费的内置联网工具(零 API 费用、零额度消耗)。

- `web_search` —— 9 个搜索引擎免 key 聚合搜索(google / brave / duckduckgo / bing / yandex / mojeek / startpage / wikipedia / yahoo)
- `web_fetch` —— 网页正文抓取,输出 markdown(自动去广告/导航噪音)
- `news_search` —— 新闻聚合搜索(bing_news / duckduckgo_news / yahoo_news)

与 SearXNG 类方案的区别:SearXNG 公共实例/默认配置常以 Google CSE 为主,索引覆盖受限;本服务直接抓取 9 个搜索引擎的网页版结果(其中 Brave、Mojeek、Yandex 是与 Google 无关的独立索引),配合自动降级,单引擎被限流或漏搜时由其它引擎补位,覆盖面显著更广。

---

## 架构

```
MCP 客户端(ZCode / Claude Desktop / 任意 MCP 宿主)
        │  stdio(JSON-RPC)
        ▼
┌────────────────────────────────────────────────────┐
│                  server.py(MCP 工具层)              │
│        web_search │ web_fetch │ news_search         │
└────────┬──────────────────────────────┬────────────┘
         ▼                              ▼
┌─────────────────┐            ┌──────────────────────┐
│     搜索管线     │            │        抓取管线       │
│ ddgs 多引擎抓取   │            │ curl_cffi(Chrome TLS │
│ 降级链/去重/合并   │            │  指纹,过基础反爬)      │
│ 熔断/自适应/节流   │            │  → httpx 兜底        │
│ TTL 结果缓存     │            │  → trafilatura 提取   │
└─────────────────┘            │  → bs4 兜底          │
                               │  → Jina Reader(可选)  │
                               └──────────────────────┘
```

### 搜索管线

```
query
 → 引擎选择:engines="auto" 时,过滤掉熔断中的引擎,按近期健康度(EMA)排序;
           engines="google,brave" 等显式列表则全部执行、不做熔断跳过
 → 逐引擎抓取网页版 SERP(ddgs backend=,auto 模式每引擎只取 1 页以压延迟)
 → URL 规范化去重(去 fragment、去 utm/spm 等跟踪参数)
 → 合并:主引擎结果排前,凑够 max_results 即停
 → 返回 JSON:{query, engines_used, engines_failed, results[{title,url,snippet,engine}]}
```

设计取舍:抓取引擎**网页版**而非官方 API,是因为所有官方 API(Brave 免费 2000 次/月、Google CSE 100 次/天、Tavily 需注册)都需要 key 且有配额,而网页版抓取无 key 无配额;代价是反爬波动,由下述韧性机制消化。

### 抓取管线

```
URL
 → curl_cffi(impersonate="chrome"):TLS/HTTP2 指纹模拟真浏览器,可过多数基础反爬
 → httpx 兜底(常规客户端)
 → 内容分类:PDF/二进制 → 优雅报错(不做无意义的正文提取)
 → trafilatura:正文提取并转 markdown(去广告/导航/脚本噪音,保留表格)
 → bs4 兜底:trafilatura 失败时退回 标题+纯文本
 → 仍失败(多为 JS 渲染页)→ Jina Reader 兜底(需可选 JINA_API_KEY)→ 原始 HTML
```

### 韧性设计(针对免费抓取的反爬现实)

| 机制 | 说明 |
|------|------|
| 多引擎降级 | auto 模式单引擎失败立即切下一个,凑够结果即停 |
| 自适应排序 | auto 链按各引擎近期成功率(EMA,衰减系数 0.8)动态重排,当前网络下谁好用谁优先 |
| 引擎熔断 | 连续失败后 45s→90s→180s→300s(封顶)内 auto 链跳过该引擎,成功即清零 |
| 全局节流 | `FREEWEB_MIN_INTERVAL` 限制引擎请求最小间隔——免费方案靠"不像爬虫"生存,节流显著降低触发风控概率 |
| 结果缓存 | 相同查询 10 分钟内命中缓存(响应带 `cached: true`),进程内 TTL+容量上限 |
| 四层抓取降级 | curl_cffi → httpx → 正文提取多级兜底 → 可选 Jina,任何一步失败都有下一步 |

### 关键实现说明

- **单文件部署**:全部逻辑在 `server.py`(约 500 行),stdio 传输,依赖 8 个纯 pip 包。
- **ddgs 的引擎参数是 `backend=`** 而非 `engine=`(后者会被静默忽略——开发期实测踩坑)。
- **MCP 客户端不继承代理变量**:子进程默认只有 `HOME`/`PATH` 等基础 env,依赖代理的网络必须在 MCP 配置的 `env` 里显式传入(见下文),否则搜索全部超时。
- 同步实现:MCP SDK 自动把同步工具函数放入工作线程执行,搜索/抓取不阻塞事件循环。

---

## 安装

```bash
pip install --user --break-system-packages -r requirements.txt
# 或在 venv 中:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 接入 MCP 客户端

stdio 模式。完整示例见 [`mcp-config.example.json`](mcp-config.example.json)。

**ZCode / Claude Desktop / 通用 MCP 配置**(ZCode 用户级: `~/.zcode/cli/config.json` → `mcp.servers`;Claude Desktop 等顶层键为 `mcpServers`):

```json
{
  "mcp": {
    "servers": {
      "freewebfetch": {
        "command": "python3",
        "args": ["/path/to/freewebfetch-mcp/server.py"],
        "env": {
          "HTTPS_PROXY": "http://127.0.0.1:7897",
          "HTTP_PROXY": "http://127.0.0.1:7897"
        }
      }
    }
  }
}
```

> **代理很重要**:若你的网络需要代理访问国际搜索引擎(常见于国内环境),必须如上在 `env` 显式传入(或用专用变量 `FREEWEB_PROXY`);可直连的国际网络则删除 `env` 即可。

重启客户端(或开新会话)后,工具以 `mcp__freewebfetch__web_search` 等形式出现。

## 工具参数

| 工具 | 参数 |
|------|------|
| `web_search(query, max_results=8, engines="auto", timelimit=None, region="")` | `engines`:`"auto"`(降级链)或逗号分隔列表;`timelimit`:`d/w/m/y` 按天/周/月/年;`region`:ddgs 区域格式如 `us-en`、`cn-zh` |
| `web_fetch(url, max_chars=0, output_format="markdown")` | `output_format`:`markdown` / `text` / `html`;`max_chars` 截断上限 |
| `news_search(query, max_results=8, engines="auto", region="")` | 新闻引擎:`bing_news` / `duckduckgo_news` / `yahoo_news` |

## 环境变量(全部可选)

| 变量 | 默认 | 说明 |
|------|------|------|
| `FREEWEB_ENGINE_CHAIN` | `google,brave,duckduckgo,bing,yandex,mojeek,startpage,yahoo` | auto 模式降级链 |
| `FREEWEB_REGION` | `us-en` | 默认区域偏好 |
| `FREEWEB_SAFESEARCH` | `moderate` | 安全搜索级别 |
| `FREEWEB_SEARCH_TIMEOUT` | `10` | 单请求超时(秒) |
| `FREEWEB_SEARCH_DEADLINE` | `40` | 单次搜索调用总时限(秒) |
| `FREEWEB_FETCH_TIMEOUT` | `25` | 抓取超时(秒) |
| `FREEWEB_MAX_CHARS` | `60000` | 默认正文截断长度 |
| `FREEWEB_CACHE_TTL` | `600` | 结果缓存秒数,0 关闭 |
| `FREEWEB_ENGINE_COOLDOWN` | `45` | 熔断初始时长(指数退避,封顶 300s) |
| `FREEWEB_MIN_INTERVAL` | `0` | 引擎请求全局最小间隔(秒);共享出口 IP 建议设 3 |
| `FREEWEB_PROXY` | 空 | 显式代理,优先于标准环境变量 |
| `JINA_API_KEY` | 空 | 可选,启用 Jina Reader 抓取兜底 |
| `FREEWEB_LOG_LEVEL` | `INFO` | 日志级别(stderr,不污染 stdio 协议) |

## 测试

```bash
python3 tests/test_mcp_e2e.py          # MCP 协议端到端(握手/列工具/调工具/错误路径)
python3 tests/test_fetch.py            # 抓取:13 项(站点类型/编码/格式/截断/PDF/反爬)
python3 tests/test_search_quality.py   # 搜索:多类型查询/九引擎矩阵/参数行为/稳定性
```

搜索套件支持 `--section 1..4` 分段运行(推荐配合 `FREEWEB_MIN_INTERVAL=3`):

```bash
FREEWEB_MIN_INTERVAL=3 python3 tests/test_search_quality.py --section 1   # 多类型查询
FREEWEB_MIN_INTERVAL=3 python3 tests/test_search_quality.py --section 2   # 九引擎矩阵
FREEWEB_MIN_INTERVAL=3 python3 tests/test_search_quality.py --section 3   # 参数行为
FREEWEB_MIN_INTERVAL=3 python3 tests/test_search_quality.py --section 4   # 稳定性
```

实测成绩(共享数据中心代理出口、应用层风控活跃环境):`test_mcp_e2e` **8/8**、`test_fetch` **13/13**、`test_search_quality` 四段全绿(合计 **18/18**:多类型查询 10/10、引擎矩阵 5/9 引擎满结果、参数行为 5/5 含翻页取满 20 条、稳定性 5 次 100% 均值 4.1s)。

## 已知限制

- **搜索引擎应用层风控**:数据中心/共享代理出口 IP 会被 Google 等标记(验证码/空结果),恢复窗口约 25-30 分钟;这是所有免 key 方案的共同约束。熔断+降级保证此状态下优雅报错、延迟有界;家宽/独立 IP 环境基本无感。
- 强反爬站点(如知乎)返回 403,优雅报错并提示。
- JS 渲染的 SPA 页面提取不到正文时,退回原始 HTML 或 Jina 兜底。
- 新闻结果的 `date` 字段部分引擎不提供。

## 目录结构

```
freewebfetch-mcp/
├── server.py                    # MCP server(单文件,stdio)
├── requirements.txt
├── mcp-config.example.json      # MCP 客户端配置示例
├── README.md                    # 本文档(中文)
├── README.en.md                 # English documentation
└── tests/
    ├── test_mcp_e2e.py          # 端到端协议测试
    ├── test_fetch.py            # 抓取质量测试
    └── test_search_quality.py   # 搜索质量测试(支持 --section 分段)
```
