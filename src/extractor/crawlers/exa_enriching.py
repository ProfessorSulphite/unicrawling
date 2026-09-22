"""
Domain-scoped Exa fallback for the one field NotebookLM most often misses.

Deliberately restricted to the university's own domain: an unconstrained search
returns third-party admissions aggregators, which would be written into the
payload as if they were official.
"""

import logging
from typing import Optional

from src.config import config

# Same registry entry as every other module in this package: logging.getLogger
# returns one object per name, so this is the logger extract_data.py created.
logger = logging.getLogger("ExtractData")


# ---------------------------------------------------------------------------
# Exa fallback
# ---------------------------------------------------------------------------

async def exa_find_application_portal(uni_domain: str, uni_name: str) -> Optional[str]:
    """
    Domain-scoped Exa search for an application portal URL.

    Restricted to the university's own domain and filtered by URL shape, because an
    unconstrained search returns third-party admissions aggregators that would be
    written into the payload as if they were official.
    """
    from src.extractor.crawlers.free_search_enrichment import free_search_find_portal

    if not config.exa_api_key:
        return await free_search_find_portal(uni_domain, uni_name)

    try:
        from exa_py import AsyncExa
    except ImportError:
        logger.info("exa_py not installed; falling back to free search portal enrichment.")
        return await free_search_find_portal(uni_domain, uni_name)

    query = f"{uni_name} online admission application portal apply now"
    try:
        exa = AsyncExa(api_key=config.exa_api_key)
        res = await exa.search(query=query, include_domains=[uni_domain], num_results=5)
    except Exception as e:
        logger.warning(f"Exa search failed for {uni_domain}: {e}")
        return None

    results = getattr(res, "results", None) or []
    portal_markers = ("apply", "portal", "admission", "online-application", "register")
    for r in results:
        url = getattr(r, "url", "") or ""
        if any(m in url.lower() for m in portal_markers):
            logger.info(f"Exa portal candidate for {uni_domain}: {url}")
            return url
    return None
