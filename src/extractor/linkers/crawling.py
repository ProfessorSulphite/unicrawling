"""
Crawl4AI deep link discovery and the shared headless browser pool.

One Chromium process is started on first use and reused for the whole batch;
launching per crawl cost 83 process starts and orphaned the browser whenever a
crawl raised before teardown.
"""

import asyncio
import os

from contextlib import asynccontextmanager
from crawl4ai import (
    AsyncWebCrawler,
    BestFirstCrawlingStrategy,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    KeywordRelevanceScorer,
)
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse, urlunparse

from src.config import config

from src.extractor.linkers.constants import (
    CRAWL4AI_SCORER_KEYWORDS,
    EXCLUDED_EXTENSIONS,
    logger,
)


class CrawlFailure(RuntimeError):
    """Raised when a site could not be crawled at all, so callers can mark state."""


# --- Shared headless browser pool -------------------------------------------
#
# A browser was previously launched and torn down inside every crawl_site_links
# call. Across an 83-university batch that is 83 Chromium starts, and any crawl
# that raised before __aexit__ left the process orphaned -- the leak the plan
# targets. One browser is now started on first use and reused, with page
# recycling capping resident memory no matter how long the batch runs.

_SHARED_CRAWLER: Optional[AsyncWebCrawler] = None
_SHARED_CRAWLER_LOOP: Optional[Any] = None


def build_browser_config() -> BrowserConfig:
    """Memory-bounded browser settings for link discovery."""
    return BrowserConfig(
        headless=config.crawler_headless,
        text_mode=config.crawler_text_mode,
        light_mode=config.crawler_light_mode,
        memory_saving_mode=config.crawler_memory_saving_mode,
        max_pages_before_recycle=config.crawler_max_pages_before_recycle,
        viewport_width=config.crawler_viewport_width,
        viewport_height=config.crawler_viewport_height,
        verbose=False,
    )


async def get_shared_crawler() -> AsyncWebCrawler:
    """
    Return the process-wide crawler, starting it on first use.

    Rebinds if the running event loop changed: a browser started under a previous
    asyncio.run() holds transports attached to a now-closed loop and every call
    against it would fail.
    """
    global _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP

    loop = asyncio.get_running_loop()
    if _SHARED_CRAWLER is not None and _SHARED_CRAWLER_LOOP is loop:
        return _SHARED_CRAWLER

    if _SHARED_CRAWLER is not None:
        await close_shared_crawler()

    crawler = AsyncWebCrawler(config=build_browser_config())
    await crawler.start()
    _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP = crawler, loop
    logger.info("Started shared headless browser (reused across universities).")
    return crawler


async def close_shared_crawler() -> None:
    """
    Shut the shared browser down. Safe to call repeatedly and when none is open.

    The globals are cleared before awaiting close() so that a hang or error in
    teardown cannot leave a half-dead crawler installed as the shared instance.
    """
    global _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP

    crawler, _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP = _SHARED_CRAWLER, None, None
    if crawler is None:
        return
    try:
        await crawler.close()
        logger.info("Closed shared headless browser.")
    except Exception as e:
        logger.warning(f"Shared browser did not close cleanly: {e}")


@asynccontextmanager
async def browser_pool():
    """Scope the shared browser to a block, guaranteeing teardown on exit."""
    try:
        yield await get_shared_crawler()
    finally:
        await close_shared_crawler()


@asynccontextmanager
async def _crawler_scope():
    """
    Yield the shared crawler, or a private one when reuse is disabled.

    A private crawler is always closed here; the shared one deliberately outlives
    the block and is closed by the batch driver.
    """
    if config.crawler_reuse_browser:
        yield await get_shared_crawler()
        return

    crawler = AsyncWebCrawler(config=build_browser_config())
    await crawler.start()
    try:
        yield crawler
    finally:
        try:
            await crawler.close()
        except Exception as e:
            logger.warning(f"Per-run browser did not close cleanly: {e}")


async def crawl_site_links(
    start_url: str,
    max_pages: Optional[int] = None,
    max_depth: Optional[int] = None,
) -> List[Dict[str, str]]:
    """
    Uses Crawl4AI BestFirstCrawlingStrategy with KeywordRelevanceScorer
    to discover internal and external links across high-relevance pages.
    Automatically retries with alternative URL candidates (e.g. https:// vs http://)
    if the initial URL times out or fails.

    `max_pages` and `max_depth` fall back to config. `max_depth` was a hardcoded
    2, which on most university sites reaches the landing page and the pages its
    top-level menu links to, and stops -- individual programme pages usually sit
    one hop further in, behind a faculty or department index.
    """
    max_pages = config.max_crawl_pages if max_pages is None else max_pages
    max_depth = config.crawl_max_depth if max_depth is None else max_depth

    # Build candidate URLs to try in priority order (http vs https, www vs non-www)
    candidates = [start_url]
    parsed_start = urlparse(start_url)

    if parsed_start.scheme == "http":
        https_url = urlunparse(("https", parsed_start.netloc, parsed_start.path, parsed_start.params, parsed_start.query, parsed_start.fragment))
        candidates.append(https_url)
    elif parsed_start.scheme == "https":
        http_url = urlunparse(("http", parsed_start.netloc, parsed_start.path, parsed_start.params, parsed_start.query, parsed_start.fragment))
        candidates.append(http_url)

    for candidate in list(candidates):
        c_parsed = urlparse(candidate)
        if c_parsed.netloc.startswith("www."):
            non_www = urlunparse((c_parsed.scheme, c_parsed.netloc[4:], c_parsed.path, c_parsed.params, c_parsed.query, c_parsed.fragment))
            if non_www not in candidates:
                candidates.append(non_www)
        else:
            with_www = urlunparse((c_parsed.scheme, f"www.{c_parsed.netloc}", c_parsed.path, c_parsed.params, c_parsed.query, c_parsed.fragment))
            if with_www not in candidates:
                candidates.append(with_www)

    last_error: Optional[str] = None

    for target_candidate in candidates:
        run_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, score_links=True)
        scorer = KeywordRelevanceScorer(keywords=CRAWL4AI_SCORER_KEYWORDS, weight=1.0)
        strategy = BestFirstCrawlingStrategy(
            max_depth=max_depth,
            max_pages=max_pages,
            url_scorer=scorer
        )

        aggregated_links = []
        seen_raw_hrefs = set()
        visited_count = 0
        successful_pages = 0
        crawl_error: Optional[str] = None

        async with _crawler_scope() as crawler:
            logger.info(
                f"Starting Crawl4AI BestFirstCrawlingStrategy for {target_candidate} "
                f"(max_pages={max_pages}, max_depth={max_depth})..."
            )
            try:
                results = await strategy.arun(start_url=target_candidate, crawler=crawler, config=run_config)
                for res in results:
                    visited_count += 1
                    if not res.success:
                        logger.warning(f"Page failed: {getattr(res, 'url', '?')} -> {getattr(res, 'error_message', 'unknown error')}")
                        continue
                    successful_pages += 1
                    if not res.links:
                        continue

                    internal_links = res.links.get("internal", [])
                    external_links = res.links.get("external", [])
                    page_links = internal_links + external_links

                    logger.debug(f"Page {visited_count}/{max_pages}: {res.url} -> Found {len(page_links)} raw links")

                    for link in page_links:
                        href = link.get("href", "").strip()
                        if not href:
                            continue

                        if not href.lower().startswith(("http://", "https://")):
                            href = urljoin(res.url, href)

                        # Filter out document extensions before adding
                        parsed_href = urlparse(href)
                        ext = os.path.splitext(parsed_href.path)[1].lower()
                        if ext in EXCLUDED_EXTENSIONS:
                            continue

                        href_norm = href.rstrip("/").split("#")[0]
                        if href_norm not in seen_raw_hrefs:
                            seen_raw_hrefs.add(href_norm)
                            aggregated_links.append({
                                "href": href,
                                "text": link.get("text", "").strip(),
                                "title": link.get("title", "").strip(),
                                "crawl_score": link.get("total_score") or link.get("intrinsic_score"),
                            })
            except Exception as e:
                crawl_error = f"{type(e).__name__}: {e}"
                logger.error(f"Error during Crawl4AI execution for {target_candidate}: {crawl_error}")

        if aggregated_links:
            logger.info(
                f"Completed site crawl for {target_candidate}: visited {visited_count} pages "
                f"({successful_pages} succeeded), harvested {len(aggregated_links)} raw unique links."
            )
            return aggregated_links
        else:
            last_error = crawl_error or f"0 links from {target_candidate}"
            logger.warning(f"Candidate URL {target_candidate} yielded 0 links ({last_error}). Trying next candidate URL...")

    raise CrawlFailure(
        f"Crawl of {start_url} (and candidates {candidates}) produced zero links: {last_error}"
    )
