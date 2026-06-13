# Search Platform

Local multi-source search platform aggregating 25 information sources with RRF cross-source fusion and LLM relevance scoring.

## Sources (25)

| Category | Sources |
|----------|---------|
| Search Engines | Google, Bing, Yandex |
| Chinese Communities | Zhihu, Xueqiu, V2EX, Sogou WeChat |
| Video | YouTube, Bilibili |
| Social Media | Twitter (X) |
| Finance | Eastmoney, TrendForce, Yahoo Finance, CoinGecko, Binance, Fear & Greed Index, CNInfo, Sina Finance, SEC EDGAR |
| Tech | GitHub, Hacker News, GitHub Trending |
| Macro | World Bank, Stats.gov |
| Aggregator | RSSHub |

## Architecture

```
User Query -> /search API -> 25 sources parallel search (CDP browser + direct API)
                                              |
                                     RRF Fusion + dedup
                                              |
                              LLM Judge relevance scoring
                                              |
                              Structured results output
```

## Key Features

- **Smart source selection** — tech queries route to GitHub/V2EX/Zhihu/YouTube, investment to Xueqiu/Eastmoney/TrendForce
- **RRF cross-source fusion** — Reciprocal Rank Fusion merging results from multiple sources with dedup
- **LLM relevance scoring** — semantic relevance ranking of search results
- **Video transcript extraction** — YouTube via yt-dlp, Bilibili via CDP browser
- **Cross-validation** — multi-source fact verification (Verified/Unverified)
- **Login auto-recovery** — detects and recovers expired sessions for Zhihu/Xueqiu/Twitter

## Quick Start

```bash
# Prerequisites: Edge browser running with CDP on port 9222
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8085

# Test
curl http://localhost:8085/health

# Search
curl -X POST http://localhost:8085/search \
  -H "Content-Type: application/json" \
  -d '{"query": "your query", "all_sources": true, "max_results": 50}'
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/search` | POST | Smart search with source filtering |
| `/query` | POST | Full source search with LLM answer |
| `/history` | GET | Search history |
| `/health` | GET | Platform health check |
| `/status/sources` | GET | Source health status |
| `/status/login` | GET | Login status for all sources |

## Configuration

- `config/sources.yml` — source definitions, categories, search templates
- `config/extraction_rules.yml` — data extraction rules per source

## Tech Stack

- FastAPI + Uvicorn (async web framework)
- CDP (Chrome DevTools Protocol) via WebSocket for browser automation
- yt-dlp for YouTube subtitle extraction
- SQLite for search history and caching
