"""
Free Search & Deep Content Enrichment (free_search_enrichment.py).

Replaces paid search APIs (such as Exa) with zero-cost DuckDuckGo HTML scraping
and targeted page fetching via httpx / Crawl4AI.

Features:
- Discovers missing official application portals.
- Finds tuition fee tables and admissions deadlines.
- Supports Tier 1 (official institutional .edu/.ac domains) and Tier 2 (reputable educational portals).
"""

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import config
from src.utilities.naming import derive_uni_info

logger = logging.getLogger("ExtractData")

# Reputable educational reporting and directory platforms for Tier 2 fallback
REPUTABLE_AGGREGATORS = {
    "usnews.com",
    "mastersportal.com",
    "bachelorsportal.com",
    "topuniversities.com",
    "collegedunia.com",
    "shiksha.com",
    "niche.com",
    "timeshighereducation.com",
}

PORTAL_KEYWORDS = ("apply", "portal", "admission", "online-application", "register", "applicant")


def _clean_ddg_url(raw_href: str) -> str:
    """Extract destination URL from DuckDuckGo redirect wrapper if present."""
    if not raw_href:
        return ""
    if "uddg=" in raw_href:
        parsed = urlparse(raw_href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    return raw_href


async def search_duckduckgo_html(
    query: str,
    max_results: int = 5,
    timeout_sec: float = 10.0,
) -> List[Dict[str, str]]:
    """
    Query DuckDuckGo HTML search without paid API keys.
    Returns list of dicts: [{"title": ..., "url": ..., "snippet": ...}].
    """
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    data = {"q": query}

    try:
        async with httpx.AsyncClient(timeout=timeout_sec, follow_redirects=True) as client:
            resp = await client.post(url, data=data, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"DuckDuckGo search returned HTTP {resp.status_code} for query: {query}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            results: List[Dict[str, str]] = []

            for r in soup.find_all("div", class_="result"):
                a_title = r.find("a", class_="result__a")
                snippet_el = r.find("a", class_="result__snippet") or r.find("div", class_="result__snippet")
                if not a_title:
                    continue

                title = a_title.get_text(strip=True)
                href = _clean_ddg_url(a_title.get("href", "").strip())
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""

                if href.startswith(("http://", "https://")):
                    results.append({
                        "title": title,
                        "url": href,
                        "snippet": snippet,
                    })
                if len(results) >= max_results:
                    break

            return results
    except Exception as e:
        logger.warning(f"Free search query failed for '{query}': {type(e).__name__}: {e}")
        return []


async def free_search_find_portal(uni_domain: str, uni_name: str) -> Optional[str]:
    """
    Free search discovery for missing university application portal URL.
    Prioritizes official university domains, falling back to verified portals.
    """
    clean_domain = uni_domain.lower().removeprefix("www.")
    query = f"{uni_name} online admission application portal apply now"
    results = await search_duckduckgo_html(query=query, max_results=6)
    if not results:
        return None

    # Priority 1: Institutional domain with portal keywords
    for r in results:
        url = r.get("url", "").lower()
        parsed = urlparse(url)
        host = parsed.netloc.removeprefix("www.")
        if clean_domain in host or host.endswith(f".{clean_domain}"):
            if any(k in url for k in PORTAL_KEYWORDS):
                logger.info(f"Free search discovered official portal for {uni_name}: {r['url']}")
                return r["url"]

    # Priority 2: Any educational .edu / .ac.* domain matching portal keywords
    for r in results:
        url = r.get("url", "").lower()
        parsed = urlparse(url)
        host = parsed.netloc.removeprefix("www.")
        if host.endswith((".edu", ".ac.uk", ".edu.pk", ".edu.cn", ".ac.in")):
            if any(k in url for k in PORTAL_KEYWORDS):
                logger.info(f"Free search discovered institutional portal candidate for {uni_name}: {r['url']}")
                return r["url"]

    return None


async def free_search_find_tuition_source(
    uni_domain: str,
    uni_name: str,
    degree_level: str = "undergraduate graduate",
) -> Optional[Dict[str, Any]]:
    """
    Search for official or independent tuition fee source pages when missing.
    Returns: {"url": str, "title": str, "snippet": str, "tier": int}
    """
    clean_domain = uni_domain.lower().removeprefix("www.")
    query = f"{uni_name} {degree_level} tuition fees 2026 cost of attendance"
    results = await search_duckduckgo_html(query=query, max_results=6)
    if not results:
        return None

    # Priority 1: Official domain
    for r in results:
        url = r.get("url", "")
        parsed = urlparse(url.lower())
        host = parsed.netloc.removeprefix("www.")
        if clean_domain in host or host.endswith(f".{clean_domain}"):
            return {
                "url": url,
                "title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "tier": 1,
            }

    # Priority 2: Reputable educational aggregator platforms
    for r in results:
        url = r.get("url", "")
        parsed = urlparse(url.lower())
        host = parsed.netloc.removeprefix("www.")
        if any(agg in host for agg in REPUTABLE_AGGREGATORS):
            return {
                "url": url,
                "title": r.get("title", ""),
                "snippet": r.get("snippet", ""),
                "tier": 2,
            }

    return None


# Backward-compatible alias for existing imports
exa_find_application_portal = free_search_find_portal
