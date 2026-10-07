# FreeWebFetch MCP

[简体中文](README.md) | **English**

A free, API-key-less, locally deployed [MCP](https://modelcontextprotocol.io) server that gives LLMs web search and web fetching — a drop-in replacement for the metered built-in web tools of LLM APIs (zero API cost, zero quota consumption).

- `web_search` — aggregated keyless search across 9 engines (google / brave / duckduckgo / bing / yandex / mojeek / startpage / wikipedia / yahoo)
- `web_fetch` — article extraction to markdown (ads/nav noise stripped automatically)
- `news_search` — aggregated news search (bing_news / duckduckgo_news / yahoo_news)

How it differs from SearXNG-style setups: public SearXNG instances and default configs lean heavily on Google CSE, which limits index coverage. This server scrapes the **web frontends of 9 engines directly** — Brave, Mojeek and Yandex are independent indexes unrelated to Google — with automatic failover, so a rate-limited engine is simply replaced by the next one.

---

## Architecture

```
MCP client (ZCode / Claude Desktop / any MCP host)
        │  stdio (JSON-RPC)
        ▼
┌────────────────────────────────────────────────────┐
│                server.py (MCP tool layer)           │
│        web_search │ web_fetch │ news_search         │
└────────┬──────────────────────────────┬────────────┘
         ▼                              ▼
┌─────────────────┐            ┌──────────────────────┐
│  Search pipeline │            │   Fetch pipeline     │
│ ddgs multi-engine│            │ curl_cffi (Chrome    │
│ failover chain,  │            │  TLS fingerprint,    │
│ dedupe / merge,  │            │  beats basic anti-bot)│
│ circuit breaker, │            │  → httpx fallback    │
│ adaptive ranking,│            │  → trafilatura extract│
│ throttle, cache  │            │  → bs4 fallback      │
└─────────────────┘            │  → Jina Reader (opt.) │
                               └──────────────────────┘
```

### Search pipeline

```
query
 → engine selection: engines="auto" filters out engines in circuit-breaker
   cooldown and sorts by recent health (EMA); explicit lists such as
   engines="google,brave" run every named engine, ignoring the breaker
 → per-engine scraping of the web SERP (ddgs backend=, one page per engine
   in auto mode to keep latency low)
 → URL normalization & dedupe (fragment stripped, utm/spm tracking params removed)
 → merge: primary engine's results first, stop once max_results reached
 → JSON: {query, engines_used, engines_failed, results[{title,url,snippet,engine}]}
```

Design trade-off: scraping engine **web frontends** instead of official APIs, because every official search API (Brave free tier 2,000/mo, Google CSE 100/day, Tavily requires signup) needs a key with quotas, while frontend scraping is keyless and unmetered; the cost is anti-bot volatility, absorbed by the resilience mechanisms below.

### Fetch pipeline

```
URL
 → curl_cffi (impersonate="chrome"): TLS/HTTP2 fingerprint of a real browser,
   passes most basic anti-bot gates
 → httpx fallback (plain client)
 → content triage: PDF/binary → graceful error (no pointless text extraction)
 → trafilatura: main-content extraction to markdown (strips ads/nav/scripts,
   keeps tables)
 → bs4 fallback: title + plain text if trafilatura fails
 → still failing (usually JS-rendered SPAs) → Jina Reader (optional
   JINA_API_KEY) → raw HTML
```

### Resilience design (the reality of keyless scraping)

| Mechanism | Detail |
|-----------|--------|
| Multi-engine failover | auto mode switches engines on any failure until enough results |
| Adaptive ranking | auto chain re-sorted by per-engine recent success rate (EMA, decay 0.8) — whatever works on *your* network goes first |
| Engine circuit breaker | after consecutive failures an engine is skipped by the auto chain for 45s→90s→180s→300s (capped); success resets it |
| Global throttle | `FREEWEB_MIN_INTERVAL` enforces a minimum gap between engine requests — keyless scraping survives by "not looking like a bot", and throttling measurably reduces anti-bot trips |
| Result cache | identical queries served from an in-process TTL cache (10 min, response marked `cached: true`) |
| Four-layer fetch fallback | curl_cffi → httpx → multi-level extraction → optional Jina; every failure has a next step |

### Implementation notes

- **Single-file deployment**: all logic lives in `server.py` (~500 lines), stdio transport, 8 plain pip dependencies.
- **ddgs selects engines via `backend=`**, not `engine=` (the latter is silently ignored — learned the hard way during development).
- **MCP clients do not inherit proxy env vars**: spawned server processes get only `HOME`/`PATH`-style basics, so proxy-dependent networks must pass proxy vars explicitly in the MCP config `env` (below), or every search times out.
- Synchronous implementation: the MCP SDK runs sync tool functions in worker threads, so network calls never block the event loop.

---

## Install

```bash
pip install --user --break-system-packages -r requirements.txt
# or in a venv:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Hooking up an MCP client

stdio transport. Full example in [`mcp-config.example.json`](mcp-config.example.json).

**ZCode / Claude Desktop / generic MCP config** (ZCode user scope: `~/.zcode/cli/config.json` → `mcp.servers`; Claude Desktop and most clients use a top-level `mcpServers` key):

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

> **Proxy matters**: if your network reaches international engines through a proxy (typical in mainland China), you must pass the proxy in `env` as above (or use `FREEWEB_PROXY`); on a globally-routable network just remove `env`.

Restart the client (or open a new session); tools appear as `mcp__freewebfetch__web_search` etc.

## Tool parameters

| Tool | Parameters |
|------|------------|
| `web_search(query, max_results=8, engines="auto", timelimit=None, region="")` | `engines`: `"auto"` (failover chain) or comma-separated list; `timelimit`: `d/w/m/y`; `region`: ddgs format such as `us-en`, `cn-zh` |
| `web_fetch(url, max_chars=0, output_format="markdown")` | `output_format`: `markdown` / `text` / `html`; `max_chars` truncation limit |
| `news_search(query, max_results=8, engines="auto", region="")` | news engines: `bing_news` / `duckduckgo_news` / `yahoo_news` |

## Environment variables (all optional)

| Variable | Default | Purpose |
|----------|---------|---------|
| `FREEWEB_ENGINE_CHAIN` | `google,brave,duckduckgo,bing,yandex,mojeek,startpage,yahoo` | auto-mode failover chain |
| `FREEWEB_REGION` | `us-en` | default region preference |
| `FREEWEB_SAFESEARCH` | `moderate` | safe-search level |
| `FREEWEB_SEARCH_TIMEOUT` | `10` | per-request timeout (s) |
| `FREEWEB_SEARCH_DEADLINE` | `40` | overall budget per search call (s) |
| `FREEWEB_FETCH_TIMEOUT` | `25` | fetch timeout (s) |
| `FREEWEB_MAX_CHARS` | `60000` | default content truncation |
| `FREEWEB_CACHE_TTL` | `600` | result cache TTL (s), 0 disables |
| `FREEWEB_ENGINE_COOLDOWN` | `45` | initial breaker duration (exponential backoff, capped 300s) |
| `FREEWEB_MIN_INTERVAL` | `0` | global min gap between engine requests (s); 3 recommended on shared exit IPs |
| `FREEWEB_PROXY` | empty | explicit proxy, overrides standard env vars |
| `JINA_API_KEY` | empty | optional, enables Jina Reader fetch fallback |
| `FREEWEB_LOG_LEVEL` | `INFO` | log level (stderr, keeps stdio protocol clean) |

## Tests

```bash
python3 tests/test_mcp_e2e.py          # MCP end-to-end (handshake / list tools / calls / error paths)
python3 tests/test_fetch.py            # fetch: 13 checks (site types / encodings / formats / truncation / PDF / anti-bot)
python3 tests/test_search_quality.py   # search: multi-query / 9-engine matrix / parameters / stability
```

The search suite supports `--section 1..4` (recommended with `FREEWEB_MIN_INTERVAL=3`):

```bash
FREEWEB_MIN_INTERVAL=3 python3 tests/test_search_quality.py --section 1   # multi-type queries
FREEWEB_MIN_INTERVAL=3 python3 tests/test_search_quality.py --section 2   # 9-engine matrix
FREEWEB_MIN_INTERVAL=3 python3 tests/test_search_quality.py --section 3   # parameter behavior
FREEWEB_MIN_INTERVAL=3 python3 tests/test_search_quality.py --section 4   # stability
```

Measured results (shared datacenter proxy exit, active application-level rate limiting): `test_mcp_e2e` **8/8**, `test_fetch` **13/13**, `test_search_quality` all four sections green (18/18 total: multi-type queries 10/10, matrix with 5/9 engines at full page, parameters 5/5 including 20-result pagination, stability 5/5 at 4.1s avg).

## Known limitations

- **Search-engine application-level rate limiting**: datacenter / shared proxy exit IPs get flagged by Google et al. (CAPTCHAs, empty result pages); recovery windows run ~25-30 min. This is inherent to all keyless approaches. The breaker + failover keeps the server graceful and bounded in that state; on residential / dedicated IPs it is essentially invisible.
- Hard anti-bot sites (e.g. Zhihu) return 403 — graceful error with a hint.
- JS-rendered SPA pages without server-side content fall back to raw HTML or the optional Jina reader.
- The `date` field of news results is not provided by all engines.

## Layout

```
freewebfetch-mcp/
├── server.py                    # MCP server (single file, stdio)
├── requirements.txt
├── mcp-config.example.json      # example MCP client config
├── README.md                    # 中文文档
├── README.en.md                 # this doc (English)
└── tests/
    ├── test_mcp_e2e.py          # end-to-end protocol tests
    ├── test_fetch.py            # fetch quality tests
    └── test_search_quality.py   # search quality tests (--section supported)
```
