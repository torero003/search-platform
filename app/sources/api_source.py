"""Direct API-based sources (no browser automation needed).

Covers: Hacker News, GitHub Trending, RSSHub, Yahoo Finance,
CoinGecko, Binance, Fear&Greed, World Bank, SEC EDGAR, cninfo, v2ex.
"""

import asyncio
import json
import logging
import os
import time
from urllib.parse import quote

import httpx

from app.sources.base import BaseSource, SearchResult

logger = logging.getLogger(__name__)

# API sources that are permanently non-functional (all instances down, geo-blocked)
BROKEN_API_SOURCES = {
    "rsshub",     # All public instances blocked by Cloudflare
    "sec_edgar",  # SEC blocks non-US access (403)
}

# Disable system proxy for direct API calls (proxy breaks TLS for many APIs)
for _proxy_var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_var, None)


def _make_client(timeout=15.0, **kwargs):
    """Create httpx AsyncClient with proxy explicitly disabled."""
    # Remove proxy from kwargs if present, and set trust_env=False to ignore env proxies
    kwargs.pop("proxy", None)
    kwargs["trust_env"] = False
    return httpx.AsyncClient(timeout=timeout, **kwargs)

# ---------------------------------------------------------------------------
# Hacker News
# ---------------------------------------------------------------------------


async def search_hacker_news(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search Hacker News via Firebase API + best-of HN search."""
    results = []
    try:
        # Step 1: Get top story IDs
        async with _make_client(timeout=10.0) as client:
            resp = await client.get("https://hacker-news.firebaseio.com/v0/topstories.json")
            story_ids = resp.json()[:100]  # top 100

        # Step 2: Fetch story details
        fetch_tasks = []
        for sid in story_ids[:50]:
            fetch_tasks.append(_fetch_hn_story(sid))
        stories = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        for story in stories:
            if isinstance(story, Exception):
                continue
            if not story:
                continue
            # Filter by query keywords
            text = f"{story.get('title', '')} {story.get('text', '')}".lower()
            if any(kw in text for kw in query.lower().split()) or not query:
                results.append(SearchResult(
                    title=story["title"],
                    url=story.get("url", story["hn_url"]),
                    content=(story.get("text", "") or "")[:400],
                    source="hacker_news",
                    score=story.get("score", 0),
                    published_date=story.get("date", ""),
                    engagement={
                        "upvotes": story.get("score", 0),
                        "comments": story.get("descendants", 0),
                    },
                ))
            if len(results) >= max_results:
                break
    except Exception as e:
        logger.error(f"hacker_news search error: {e}")

    return results[:max_results]


async def _fetch_hn_story(story_id: int) -> dict | None:
    try:
        async with _make_client(timeout=5.0) as client:
            resp = await client.get(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json")
            item = resp.json()
        if not item or not item.get("title"):
            return None
        from datetime import datetime, timezone
        ts = item.get("time", 0)
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else ""
        return {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "text": item.get("text", "").strip() if item.get("type") == "story" else "",
            "hn_url": f"https://news.ycombinator.com/item?id={story_id}",
            "score": item.get("score", 0),
            "descendants": item.get("descendants", 0),
            "date": date,
        }
    except Exception:
        return None


class HackerNewsSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_hacker_news(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "Hacker News API ready"}


# ---------------------------------------------------------------------------
# GitHub Trending
# ---------------------------------------------------------------------------


async def search_github_trending(query: str, max_results: int = 10) -> list[SearchResult]:
    """Fetch GitHub Trending repositories, filter by query keywords."""
    results = []
    try:
        async with _make_client(timeout=15.0) as client:
            resp = await client.get("https://github.com/trending", headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            html = resp.text

        import re
        # GitHub changed HTML structure: h2 contains repo links, descriptions in <p>
        h2_repos = re.findall(r'<h2.*?href="(/[^"]+)".*?>.*?</a>', html, re.S)
        descriptions = re.findall(r'<p class="col-9.*?">(.*?)</p>', html, re.S)
        languages = re.findall(r'itemprop="programmingLanguage">([^<]+)', html)

        for i, repo_path in enumerate(h2_repos):
            # Skip login/navigation links
            if not repo_path or "/login" in repo_path or "/explore" in repo_path:
                continue
            # Extract owner/repo from path
            parts = repo_path.strip("/").split("/")
            if len(parts) < 2:
                continue
            title = parts[0] + "/" + parts[1]
            desc = re.sub(r'<[^>]+>', '', descriptions[i]).strip() if i < len(descriptions) else ""
            lang = languages[i] if i < len(languages) else ""

            combined = f"{title} {desc} {lang}".lower()
            if query and not any(kw in combined for kw in query.lower().split()):
                continue
            results.append(SearchResult(
                    title=title,
                    url=f"https://github.com{repo_path}",
                    content=f"{desc} | Lang: {lang}"[:400],
                    source="github_trending",
                ))
            if len(results) >= max_results:
                break
    except Exception as e:
        logger.error(f"github_trending search error: {e}")

    return results[:max_results]


class GitHubTrendingSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_github_trending(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "GitHub Trending ready"}


# ---------------------------------------------------------------------------
# RSSHub — aggregates Weibo, Bilibili, Douyin, Zhihu hot lists
# ---------------------------------------------------------------------------

RSSHub_ROUTES = {
    "weibo_hot": "/weibo/hot/search",
    "zhihu_hot": "/zhihu/hot-list",
    "bilibili_hot": "/bilibili/popular",
    "douyin_hot": "/douyin/hot",
    "zhihu_new": "/zhihu/daily/night",
}

# RSSHub public instances to try (main instance often blocked by Cloudflare)
RSSHub_INSTANCES = [
    "https://rsshub.rssforever.com",
    "https://rsshub.rs",
    "https://rsshub.app",
]


async def search_rsshub(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search RSSHub hot lists and filter by query. Tries multiple instances."""
    results = []

    for base_url in RSSHub_INSTANCES:
        if results:
            break
        try:
            async with _make_client(timeout=15.0, follow_redirects=True) as client:
                # Test connection first
                test_resp = await client.get(f"{base_url}/", headers={"User-Agent": "Mozilla/5.0"})
                if test_resp.status_code == 403:
                    logger.info(f"rsshub {base_url} blocked (403), trying next")
                    continue

                tasks = {}
                for name, route in RSSHub_ROUTES.items():
                    tasks[name] = client.get(f"{base_url}{route}", headers={
                        "User-Agent": "Mozilla/5.0"
                    })

                responses = {name: await t for name, t in tasks.items()}

                for name, resp in responses.items():
                    if resp.status_code != 200:
                        continue
                    try:
                        import xml.etree.ElementTree as ET
                        root = ET.fromstring(resp.content)
                        for item in root.findall(".//item"):
                            title_el = item.find("title")
                            link_el = item.find("link")
                            desc_el = item.find("description")
                            pub_el = item.find("pubDate")

                            title = title_el.text.strip() if title_el is not None and title_el.text else ""
                            link = link_el.text.strip() if link_el is not None and link_el.text else ""
                            desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""
                            pub = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

                            if not title or not link:
                                continue

                            # Filter by query keywords
                            combined = f"{title} {desc}".lower()
                            if query and not any(kw in combined for kw in query.lower().split()):
                                continue

                            results.append(SearchResult(
                                title=title[:120],
                                url=link[:200],
                                content=desc[:400],
                                source="rsshub",
                                published_date=pub,
                            ))
                            if len(results) >= max_results:
                                return results
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"rsshub {base_url} error: {e}")

    return results[:max_results]


class RSSHubSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_rsshub(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "RSSHub ready"}


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------


async def search_yahoo_finance(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search Yahoo Finance for market data (quotes + news)."""
    results = []
    try:
        query_encoded = quote(query)
        async with _make_client(timeout=15.0) as client:
            resp = await client.get(
                f"https://query2.finance.yahoo.com/v1/finance/search?q={query_encoded}&newsCount=20&quotesCount=10",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            data = resp.json()

        # Quote items (stocks/ETFs) - reliable source of results
        for item in data.get("quotes", []):
            symbol = item.get("symbol", "")
            name = item.get("shortname", "") or item.get("longname", "")
            type_disp = item.get("typeDisp", "")
            exch = item.get("exchDisp", "")
            sector = item.get("sector", "") or item.get("sectorDisp", "")
            industry = item.get("industry", "") or item.get("industryDisp", "")
            results.append(SearchResult(
                title=f"{symbol} - {name}",
                url=f"https://finance.yahoo.com/quote/{symbol}/",
                content=f"{type_disp} | {exch}" + (f" | {sector}" if sector else "") + (f" | {industry}" if industry else ""),
                source="yahoo_finance",
            ))

        # News items (may not always be present)
        for item in data.get("news", []):
            title = item.get("title", "")
            uuid = item.get("uuid", "")
            results.append(SearchResult(
                title=title[:120] if title else "",
                url=f"https://finance.yahoo.com/news/{uuid}" if uuid else item.get("url", ""),
                content=item.get("content", "")[:400],
                source="yahoo_finance",
                published_date=item.get("formatedPublisher", "") or item.get("publisher", ""),
            ))

    except Exception as e:
        logger.error(f"yahoo_finance search error: {type(e).__name__}: {e}", exc_info=True)

    return results[:max_results]


class YahooFinanceSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_yahoo_finance(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "Yahoo Finance ready"}


# ---------------------------------------------------------------------------
# CoinGecko — Cryptocurrency
# ---------------------------------------------------------------------------


async def search_coingecko(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search CoinGecko for crypto market data."""
    results = []
    try:
        async with _make_client(timeout=15.0) as client:
            # Search coins by query
            resp = await client.get(
                f"https://api.coingecko.com/api/v3/search?query={quote(query)}",
                headers={"Accept": "application/json"}
            )
            data = resp.json()

            for coin in data.get("coins", [])[:max_results]:
                symbol = coin.get("symbol", "")
                name = coin.get("name", "")
                coin_id = coin.get("id", "")
                results.append(SearchResult(
                    title=f"{name} ({symbol})",
                    url=f"https://www.coingecko.com/en/coins/{coin_id}",
                    content=f"Symbol: {symbol}",
                    source="coingecko",
                ))

            if not results:
                # Fallback: get top coins
                resp2 = await client.get(
                    "https://api.coingecko.com/api/v3/coins/markets",
                    params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 20, "sparkline": "false"},
                    headers={"Accept": "application/json"}
                )
                for coin in resp2.json():
                    results.append(SearchResult(
                        title=f"{coin.get('name', '')} ({coin.get('symbol', '')})",
                        url=coin.get("url", ""),
                        content=f"Price: ${coin.get('current_price', '')} | "
                                f"24h: {coin.get('price_change_percentage_24h', '')}% | "
                                f"Market Cap: ${coin.get('market_cap', '')}",
                        source="coingecko",
                    ))
                    if len(results) >= max_results:
                        break
    except Exception as e:
        logger.error(f"coingecko search error: {e}")

    return results[:max_results]


class CoinGeckoSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_coingecko(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "CoinGecko API ready"}


# ---------------------------------------------------------------------------
# Binance — Cryptocurrency real-time
# ---------------------------------------------------------------------------


async def search_binance(query: str, max_results: int = 10) -> list[SearchResult]:
    """Fetch Binance ticker data, filter by query."""
    results = []
    try:
        async with _make_client(timeout=15.0) as client:
            resp = await client.get("https://api.binance.com/api/v3/ticker/24hr")
            tickers = resp.json()

        query_lower = query.lower()
        for ticker in tickers:
            symbol = ticker.get("symbol", "")
            # Filter by query
            if query_lower and not any(kw in symbol.lower() for kw in query_lower.split()):
                continue

            symbol_clean = symbol.replace("USDT", "").replace("BTC", "").replace("ETH", "")
            results.append(SearchResult(
                title=f"{symbol} 24h Ticker",
                url=f"https://www.binance.com/en/price/{symbol_clean.lower() if symbol_clean else symbol}",
                content=(
                    f"Last Price: {ticker.get('lastPrice', '')} | "
                    f"24h Change: {ticker.get('priceChangePercent', '')}% | "
                    f"Volume: {ticker.get('volume', '')} | "
                    f"Quote Volume: {ticker.get('quoteVolume', '')}"
                ),
                source="binance",
            ))
            if len(results) >= max_results:
                break
    except Exception as e:
        logger.error(f"binance search error: {e}")

    return results[:max_results]


class BinanceSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_binance(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "Binance API ready"}


# ---------------------------------------------------------------------------
# Fear & Greed Index — Crypto sentiment
# ---------------------------------------------------------------------------


async def search_fear_greed(query: str, max_results: int = 10) -> list[SearchResult]:
    """Fetch Crypto Fear & Greed Index."""
    results = []
    try:
        async with _make_client(timeout=10.0) as client:
            resp = await client.get("https://api.alternative.me/fng/?limit=10")
            data = resp.json()

        for item in data.get("data", []):
            value = item.get("value", "")
            classification = item.get("value_classification", "")
            timestamp = item.get("timestamp", "")
            try:
                from datetime import datetime, timezone
                ts = int(timestamp) if timestamp else 0
                date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts > 946684800 else ""
            except (ValueError, OSError, OverflowError):
                date = ""

            results.append(SearchResult(
                title=f"Crypto Fear & Greed Index: {classification} ({value})",
                url="https://alternative.me/crypto/fear-and-greed-index/",
                content=f"Date: {date} | Value: {value} | Classification: {classification}",
                source="fear_greed",
                published_date=date,
            ))
            if len(results) >= max_results:
                break
    except Exception as e:
        logger.error(f"fear_greed search error: {e}")

    return results[:max_results]


class FearGreedSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_fear_greed(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "Fear & Greed Index ready"}


# ---------------------------------------------------------------------------
# World Bank API — Global economic data
# ---------------------------------------------------------------------------


async def search_world_bank(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search World Bank for economic data.
    Gets latest data for common indicators filtered by query keywords.
    """
    results = []
    # Common indicators to check
    indicators = {
        "GDP": "NY.GDP.MKTP.CD",
        "GDP per capita": "NY.GDP.PCAP.CD",
        "GDP growth": "NY.GDP.MKTP.KD.ZG",
        "inflation": "FP.CPI.TOTL.ZG",
        "trade": "NE.TRD.GNFS.ZS",
        "population": "SP.POP.TOTL",
        "unemployment": "SL.UEM.TOTL.ZS",
        "poverty": "SI.POV.DDAY",
        "life expectancy": "SP.DYN.LE00.IN",
        "CO2 emissions": "EN.ATM.CO2E.KT",
        "electricity": "EG.ELC.ACCS.ZS",
        "internet": "IT.NET.USER.ZS",
        "fertility": "SP.DYN.TFRT.IN",
        "government revenue": "GC.REX.TOTL.ZS",
        "foreign debt": "UI.TRS.DECT.CD",
    }

    try:
        async with _make_client(timeout=15.0) as client:
            # Build list of indicators to fetch
            ind_ids = []
            for ind_name, ind_id in indicators.items():
                if not query or any(kw in ind_name.lower() for kw in query.lower().split()):
                    ind_ids.append((ind_name, ind_id))

            # Also fetch all indicators if query is broad
            if not ind_ids:
                ind_ids = list(indicators.items())

            for ind_name, ind_id in ind_ids:
                try:
                    resp = await client.get(
                        f"https://api.worldbank.org/v2/country/all/indicator/{ind_id}",
                        params={"format": "json", "per_page": 10, "date": "2023:2025"},
                        headers={"User-Agent": "Mozilla/5.0"}
                    )
                    data = resp.json()
                    if not isinstance(data, list) or len(data) < 2:
                        continue
                    items = data[1]
                    if not items:
                        continue
                    for item in items:
                        value = item.get("value")
                        if value is None:
                            continue
                        country_info = item.get("country", {})
                        country = country_info.get("value", "") if isinstance(country_info, dict) else ""
                        date = item.get("date", "")
                        results.append(SearchResult(
                            title=f"{ind_name} - {country}",
                            url=f"https://data.worldbank.org/indicator/{ind_id}",
                            content=f"Value: {value} | Date: {date} | Country: {country}",
                            source="world_bank",
                            published_date=date,
                        ))
                        if len(results) >= max_results:
                            break
                except Exception:
                    continue
                if len(results) >= max_results:
                    break
    except Exception as e:
        logger.error(f"world_bank search error: {e}")

    return results[:max_results]


class WorldBankSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_world_bank(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "World Bank API ready"}


# ---------------------------------------------------------------------------
# SEC EDGAR — US company filings
# ---------------------------------------------------------------------------


async def search_sec_edgar(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search SEC EDGAR for company filings.
    Uses company_tickers.json index + data.sec.gov submissions API.
    """
    results = []
    sec_headers = {
        "User-Agent": "smart-search-platform/1.0 (contact: user@localhost)",
        "Accept": "application/json",
    }

    # Well-known CIKs as fallback
    KNOWN_CIKS = {
        "tesla": "0001811217",
        "apple": "0000320193",
        "microsoft": "0000789019",
        "google": "0001652044",
        "alphabet": "0001652044",
        "amazon": "0001018724",
        "meta": "0001326801",
        "nvidia": "0001045810",
        "openai": None,  # Private
    }

    try:
        async with _make_client(timeout=15.0) as client:
            # Step 1: Try to find CIK from company index
            cik = None
            cik_raw = None
            company_name = None

            # Check known CIKs first
            for name, c in KNOWN_CIKS.items():
                if c and name in query.lower():
                    cik = c
                    company_name = name
                    break

            # Step 2: If not found, load company index
            if not cik:
                try:
                    resp = await client.get(
                        "https://www.sec.gov/files/company_tickers.json",
                        headers=sec_headers,
                    )
                    tickers = resp.json()
                    query_lower = query.lower()
                    for t in tickers:
                        if query_lower in t.get("ticker", "").lower() or query_lower in t.get("title", "").lower():
                            cik_raw = t.get("cik_str")
                            cik = f"{cik_raw:012d}" if cik_raw else None
                            company_name = t.get("title", query)
                            break
                except Exception:
                    pass

            if not cik:
                logger.warning(f"sec_edgar: could not find CIK for '{query}'")
                return results

            # Step 3: Fetch recent filings via data.sec.gov
            cik_padded = f"{int(cik):010d}" if cik else ""
            resp2 = await client.get(
                f"https://data.sec.gov/submissions/CIK{cik_padded}.json",
                headers=sec_headers,
            )
            filings_data = resp2.json()

            for filing in filings_data.get("filings", {}).get("recent", [])[:max_results]:
                filing_type = filing.get("type", "")
                filing_date = filing.get("dateFiled", "")
                accession = filing.get("accessionNumber", cik)
                primary_doc = filing.get("primaryDocument", "")
                filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_padded}/{accession.replace('|', '-')}-1.htm"
                if primary_doc:
                    filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik_padded}/{accession.replace('|', '-')}{primary_doc}"

                results.append(SearchResult(
                    title=f"{company_name} - {filing_type} ({filing_date})",
                    url=filing_url[:200],
                    content=f"CIK: {cik} | Type: {filing_type} | Date: {filing_date}",
                    source="sec_edgar",
                    published_date=filing_date,
                ))
                if len(results) >= max_results:
                    return results
    except Exception as e:
        logger.error(f"sec_edgar search error: {e}")

    return results[:max_results]


class SECEdgarsSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_sec_edgar(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "SEC EDGAR ready"}


# ---------------------------------------------------------------------------
# 巨潮资讯 (cninfo) — A股公告
# ---------------------------------------------------------------------------


async def search_cninfo(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search cninfo (巨潮资讯) for A-share announcements.
    Uses the full-text search API. Response is GBK-encoded.
    """
    results = []
    try:
        async with _make_client(timeout=15.0, follow_redirects=True) as client:
            resp = await client.post(
                "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                data={
                    "searchkey": query,
                    "column": "szse",
                    "tabName": "fulltext",
                    "pageSize": 30,
                    "pageNum": 1,
                    "category": "",
                    "seDate": "",
                    "sortName": "",
                    "sortType": "",
                    "isHLtitle": "false",
                },
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Origin": "https://www.cninfo.com.cn",
                    "Referer": "https://www.cninfo.com.cn/new/disclosure",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Accept": "application/json",
                }
            )
            # API returns GBK-encoded response, not UTF-8
            try:
                data = json.loads(resp.content.decode("gbk"))
            except (UnicodeDecodeError, ValueError):
                try:
                    data = json.loads(resp.content.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    data = resp.json()

            announcements = data.get("announcements")
            if not announcements:
                # Try with SSE exchange
                resp2 = await client.post(
                    "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                    data={
                        "searchkey": query,
                        "column": "sse",
                        "tabName": "fulltext",
                        "pageSize": 30,
                        "pageNum": 1,
                        "category": "",
                        "seDate": "",
                        "sortName": "",
                        "sortType": "",
                        "isHLtitle": "false",
                    },
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Origin": "https://www.cninfo.com.cn",
                        "Referer": "https://www.cninfo.com.cn/new/disclosure",
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    }
                )
                try:
                    data2 = json.loads(resp2.content.decode("gbk"))
                except (UnicodeDecodeError, ValueError):
                    data2 = resp2.json()
                announcements = data2.get("announcements")

            if not announcements:
                logger.warning(f"cninfo: no announcements found for '{query}'")
                return results

            for ann in announcements:
                title = ann.get("announcementTitle", "").strip()
                ann_url = ann.get("adjunctUrl", "").strip()
                org_name = ann.get("secName", "")
                ann_time_raw = ann.get("announcementTime", "")

                # Convert millisecond timestamp to date string
                try:
                    from datetime import datetime, timezone
                    ann_time = datetime.fromtimestamp(int(ann_time_raw) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                except (ValueError, TypeError, OSError):
                    ann_time = str(ann_time_raw)

                if title and ann_url:
                    results.append(SearchResult(
                        title=title[:120],
                        url=f"https://static.cninfo.com.cn/{ann_url}" if ann_url.startswith("/") else ann_url,
                        content=f"{org_name} | {ann_time}",
                        source="cninfo",
                        published_date=ann_time,
                    ))
                if len(results) >= max_results:
                    break
    except Exception as e:
        logger.error(f"cninfo search error: {e}")

    return results[:max_results]


class CninfoSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_cninfo(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "巨潮资讯 ready"}


# ---------------------------------------------------------------------------
# Sina Finance — A-share real-time quotes
# ---------------------------------------------------------------------------


# Common A-share stock codes for name-based lookup
_A_SHARE_STOCKS = {
    "平安银行": "sz000001", "平安": "sz000001",
    "浦发银行": "sh600000", "浦发": "sh600000",
    "工商银行": "sh601398", "工行": "sh601398",
    "建设银行": "sh601939", "建行": "sh601939",
    "农业银行": "sh601288", "农行": "sh601288",
    "中国银行": "sh601988", "中行": "sh601988",
    "招商银行": "sz000002", "招行": "sz000002",
    "万科": "sz000002", "万科A": "sz000002",
    "比亚迪": "sz002594", "宁德时代": "sz300750", "宁德": "sz300750",
    "腾讯": "sz00700", "美团": "sz3690",
    "中芯国际": "sh688981", "中芯": "sh688981",
    "华虹": "sh688347", "华虹公司": "sh688347",
    "隆基绿能": "sh601012", "隆基": "sh601012",
    "阳光电源": "sz300274", "阳光": "sz300274",
    "特变电工": "sh600089", "特变": "sh600089",
    "国电南瑞": "sh600406", "南瑞": "sh600406",
    "许继电气": "sz000400", "许继": "sz000400",
    "平高电气": "sh600312", "平高": "sh600312",
    "正泰电器": "sh601877", "正泰": "sh601877",
    "三一重工": "sz600031", "三一": "sz600031",
    "徐工机械": "sz000425", "徐工": "sz000425",
    "恒立液压": "sh601100", "恒立": "sh601100",
    "国轩高科": "sz002074", "国轩": "sz002074",
    "亿纬锂能": "sz300014", "亿纬": "sz300014",
    "赣锋锂业": "sz002460", "赣锋": "sz002460",
    "天齐锂业": "sz002466", "天齐": "sz002466",
    "紫金矿业": "sh601899", "紫金": "sh601899",
    "中国石油": "sh601857", "中石油": "sh601857",
    "中国石化": "sh600028", "中石化": "sh600028",
    "中国海油": "sh600938", "中海油": "sh600938",
    "长江电力": "sh600900", "长电": "sh600900",
    "华能国际": "sh600011", "华能": "sh600011",
    "华电国际": "sh600027", "华电": "sh600027",
    "国电电力": "sh600795", "国电": "sh600795",
    "电网": "sh600406", "电网设备": "sh600406",
}


async def search_sina_finance(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search Sina Finance for A-share real-time quotes.
    Response is GBK-encoded. Supports stock name and code search.
    """
    logger.info(f"sina_finance: searching for '{query}'")
    results = []
    # Build list of stock codes to query from the query
    codes = []
    query_lower = query.lower()

    # Check if query contains a stock code directly (e.g., "sh600000" or "000001")
    import re
    direct_codes = re.findall(r'(?:sh|sz|600|000|300|688)\d{5,6}', query)
    for dc in direct_codes:
        if not dc.startswith(('sh', 'sz')):
            dc = 'sh' + dc if dc.startswith('6') else 'sz' + dc
        codes.append(dc)

    # Match by name
    for name, code in _A_SHARE_STOCKS.items():
        if name in query and code not in codes:
            codes.append(code)

    # Default: popular stocks if no match
    if not codes:
        # Try to find any matching stock
        for name, code in _A_SHARE_STOCKS.items():
            if any(kw in name for kw in query.split()) and code not in codes:
                codes.append(code)
        if not codes:
            # Return top stocks as fallback
            codes = ["sh600000", "sz000001", "sz000002", "sz002594", "sz300750",
                     "sh601398", "sh601939", "sh601288", "sh601988", "sh600406"]

    codes = codes[:20]  # limit

    try:
        async with _make_client(timeout=10.0) as client:
            resp = await client.get(
                f"http://hq.sinajs.cn/list={','.join(codes)}",
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "http://finance.sina.com.cn/",
                }
            )
            logger.info(f"sina_finance: status={resp.status_code}, len={len(resp.content)}, codes={codes[:3]}")
            if resp.status_code != 200:
                logger.warning(f"sina_finance: non-200 status {resp.status_code}")
                return results
            # GBK-encoded response
            try:
                text = resp.content.decode("gbk")
            except UnicodeDecodeError:
                text = resp.content.decode("utf-8", errors="ignore")

            for line in text.strip().split("\n"):
                if not line.startswith("var hq_str_"):
                    continue
                # Parse: var hq_str_sz000001="name,price,..."
                match = re.match(r'var hq_str_(\w+)="(.*)"', line)
                if not match:
                    continue
                stock_code = match.group(1)
                data_str = match.group(2)
                if not data_str:
                    continue

                parts = data_str.split(",")
                if len(parts) < 32:
                    continue

                name = parts[0]
                open_price = parts[1]
                prev_close = parts[2]
                current = parts[3]
                high = parts[4]
                low = parts[5]
                volume = parts[8]  # in shares
                amount = parts[9]  # in yuan
                date = parts[30] if len(parts) > 30 else ""

                # Calculate change
                try:
                    change = float(current) - float(prev_close)
                    change_pct = (change / float(prev_close)) * 100
                except (ValueError, ZeroDivisionError):
                    change = 0
                    change_pct = 0

                results.append(SearchResult(
                    title=f"{name} ({stock_code})",
                    url=f"http://finance.sina.com.cn/realstock/company/{stock_code}/nc.shtml",
                    content=(
                        f"价格: {current} | 涨跌: {change:+.2f} ({change_pct:+.2f}%) | "
                        f"最高: {high} | 最低: {low} | 成交量: {float(volume)/100:.0f}手"
                    ),
                    source="sina_finance",
                    published_date=date,
                    score=abs(change_pct),
                ))
                if len(results) >= max_results:
                    break
    except Exception as e:
        logger.error(f"sina_finance search error: {type(e).__name__}: {e}")

    return results[:max_results]


class SinaFinanceSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_sina_finance(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "新浪财经 ready"}


# ---------------------------------------------------------------------------
# 搜狗微信搜索 — direct HTTP, bypasses CSP issue with CDP
# ---------------------------------------------------------------------------

async def search_sogou_wechat(query: str, max_results: int = 10) -> list[SearchResult]:
    """Search WeChat articles via sogou.com direct HTTP.

    CSP blocks CDP browser automation, but direct HTTP works fine.
    """
    results = []
    try:
        query_encoded = quote(query.encode("gbk", "ignore"))
        url = f"https://weixin.sogou.com/weixin?type=2&query={query_encoded}"

        async with _make_client(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://weixin.sogou.com/",
            })

        if resp.status_code != 200:
            logger.warning(f"sogou_wechat: HTTP {resp.status_code}")
            return results

        html = resp.text

        # Parse using regex on the HTML structure
        # Each result: <div class="txt-box"> with h3>a (title+url) and p.txt-info (summary)
        import re

        # Find all txt-box blocks
        txt_blocks = re.findall(r'<div class="txt-box">(.*?)</div>', html, re.DOTALL)

        for block in txt_blocks[:max_results]:
            # Title from h3 > a
            title_m = re.search(r'<h3>.*?<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', block, re.DOTALL)
            if not title_m:
                continue

            sogou_url = title_m.group(1)
            title_html = title_m.group(2)
            # Clean HTML tags and red markers from title
            title = re.sub(r'<[^>]+>', '', title_html).strip()
            title = re.sub(r'&[^;]+;', '', title).strip()

            # Summary from p.txt-info
            summary_m = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.DOTALL)
            summary = ""
            if summary_m:
                summary = re.sub(r'<[^>]+>', '', summary_m.group(1)).strip()
                summary = re.sub(r'&[^;]+;', '', summary).strip()

            # Source name from span.all-time-y2
            source_m = re.search(r'<span class="all-time-y2">([^<]+)</span>', block)
            source_name = source_m.group(1).strip() if source_m else ""

            # Resolve sogou redirect URL to actual weixin URL
            real_url = sogou_url
            if sogou_url.startswith("/link"):
                try:
                    full_redirect = f"https://weixin.sogou.com{sogou_url}"
                    redirect_resp = await client.head(full_redirect, headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    }, follow_redirects=True)
                    real_url = redirect_resp.url
                except Exception:
                    pass

            if not title:
                continue

            results.append(SearchResult(
                title=title[:120],
                url=str(real_url)[:200],
                content=summary[:400],
                source="sogou_wechat",
                published_date=source_name,
            ))

    except Exception as e:
        logger.error(f"sogou_wechat search error: {type(e).__name__}: {e}")

    return results[:max_results]


class SogouWechatSource(BaseSource):
    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        return await search_sogou_wechat(query, max_results)

    def health_check(self) -> dict:
        return {"available": True, "message": "搜狗微信搜索 ready"}


# ---------------------------------------------------------------------------
# Source registry — maps source name to class
# ---------------------------------------------------------------------------
# Note: YouTube and Bilibili are CDP-based (edge_mcp type), not API sources.
# Their extractors are in cdp_client.py as _js_youtube_extract() and _js_bilibili_extract().

# ---------------------------------------------------------------------------
API_SOURCE_MAP = {
    "hacker_news": HackerNewsSource,
    "github_trending": GitHubTrendingSource,
    "rsshub": RSSHubSource,
    "yahoo_finance": YahooFinanceSource,
    "coingecko": CoinGeckoSource,
    "binance": BinanceSource,
    "fear_greed": FearGreedSource,
    "world_bank": WorldBankSource,
    "sec_edgar": SECEdgarsSource,
    "cninfo": CninfoSource,
    "sina_finance": SinaFinanceSource,
    "sogou_wechat": SogouWechatSource,
}


def get_api_source(name: str) -> BaseSource | None:
    """Get an API source instance by name. Returns None for broken sources."""
    if name in BROKEN_API_SOURCES:
        logger.warning(f"{name}: source currently non-functional, skipping")
        return None
    cls = API_SOURCE_MAP.get(name)
    if cls:
        return cls()
    return None
