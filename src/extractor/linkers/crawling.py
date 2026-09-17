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

# Document extensions worth ingesting as sources rather than discarding. Fee
# schedules, admission calendars and prospectuses are published as PDFs at most
# universities, and NotebookLM ingests them natively.
DOCUMENT_EXTENSIONS = {".pdf"}

# Everything crawl4ai must never NAVIGATE to. The harvest loop already refused
# to keep these, but the crawl strategy had no filter chain at all, so the
# browser still fetched each one -- ITU's 8 PDFs consumed 8 of the page budget
# and logged 8 "Page failed" warnings before being thrown away one function
# later. Documents are harvested from the link graph, never visited.
_NON_NAVIGABLE_EXTENSIONS = sorted(EXCLUDED_EXTENSIONS | DOCUMENT_EXTENSIONS)


def _build_filter_chain():
    """
    Refuse to navigate to non-HTML URLs, or return None if unsupported.

    Imported defensively and at call time: the filter classes moved packages
    between crawl4ai releases, and a missing symbol here must cost the page
    budget optimisation, never the entire crawl.
    """
    try:
        from crawl4ai.deep_crawling.filters import FilterChain, URLPatternFilter
    except Exception:
        try:
            from crawl4ai import FilterChain, URLPatternFilter  # type: ignore
        except Exception as e:
            logger.debug(f"crawl4ai URL filters unavailable ({e}); crawling without a filter chain.")
            return None

    try:
        patterns = [f"*{ext}" for ext in _NON_NAVIGABLE_EXTENSIONS]
        patterns += [f"*{ext}?*" for ext in _NON_NAVIGABLE_EXTENSIONS]
        return FilterChain([URLPatternFilter(patterns=patterns, reverse=True)])
    except Exception as e:
        logger.debug(f"Could not build crawl4ai filter chain ({e}); crawling without one.")
        return None


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

    In-flight navigations are drained first (C32). Closing the browser out from
    under them raises TargetClosedError on each, and crawl4ai reports that as an
    ordinary page failure -- so the pages are simply lost. The ITU run lost
    /admissions/eligibility-criteria/, /admissions/faqs/ and
    /admissions/bs-management-and-technology/ that way, three of the highest
    value pages on the site, to a race at shutdown rather than to anything
    wrong with the pages.

    The drain is bounded: a navigation that will not finish must not hold the
    process open, so the wait is capped and teardown proceeds regardless.
    """
    global _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP

    crawler, _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP = _SHARED_CRAWLER, None, None
    if crawler is None:
        return

    await _drain_in_flight_navigations()

    try:
        await crawler.close()
        logger.info("Closed shared headless browser.")
    except Exception as e:
        logger.warning(f"Shared browser did not close cleanly: {e}")


async def _drain_in_flight_navigations(timeout: float = 15.0) -> None:
    """
    Let outstanding page fetches finish before the browser goes away.

    Identified by task name rather than by holding a registry: crawl4ai owns the
    tasks and does not expose them. Anything still pending that is neither this
    coroutine nor the caller's is given a bounded chance to complete.
    """
    try:
        current = asyncio.current_task()
        pending = [
            t for t in asyncio.all_tasks()
            if t is not current and not t.done()
            and "crawl" in (t.get_name() or "").lower()
        ]
    except RuntimeError:
        return

    if not pending:
        return

    logger.info(f"Draining {len(pending)} in-flight navigation(s) before browser teardown...")
    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    if still_pending:
        logger.warning(
            f"{len(still_pending)} navigation(s) did not finish within {timeout}s; "
            f"closing the browser anyway."
        )
        for task in still_pending:
            task.cancel()
        # Collect the cancellations so they are not reported as never-retrieved.
        await asyncio.gather(*still_pending, return_exceptions=True)


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
        strategy_kwargs = dict(max_depth=max_depth, max_pages=max_pages, url_scorer=scorer)
        filter_chain = _build_filter_chain()
        if filter_chain is not None:
            strategy_kwargs["filter_chain"] = filter_chain
        strategy = BestFirstCrawlingStrategy(**strategy_kwargs)

        aggregated_links = []
        document_links = []
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

                        parsed_href = urlparse(href)
                        ext = os.path.splitext(parsed_href.path)[1].lower()

                        # Documents are collected, not discarded (C32). A fee
                        # schedule published as a PDF is the highest-value
                        # source a university offers for the two fields the
                        # payload was least able to answer.
                        is_document = ext in DOCUMENT_EXTENSIONS
                        if ext in EXCLUDED_EXTENSIONS and not is_document:
                            continue
                        if is_document and not getattr(config, "ingest_document_links", True):
                            continue

                        href_norm = href.rstrip("/").split("#")[0]
                        if href_norm not in seen_raw_hrefs:
                            seen_raw_hrefs.add(href_norm)
                            record = {
                                "href": href,
                                "text": link.get("text", "").strip(),
                                "title": link.get("title", "").strip(),
                                "crawl_score": link.get("total_score") or link.get("intrinsic_score"),
                                "is_document": is_document,
                            }
                            (document_links if is_document else aggregated_links).append(record)
            except Exception as e:
                crawl_error = f"{type(e).__name__}: {e}"
                logger.error(f"Error during Crawl4AI execution for {target_candidate}: {crawl_error}")

        if aggregated_links:
            logger.info(
                f"Completed site crawl for {target_candidate}: visited {visited_count} pages "
                f"({successful_pages} succeeded), harvested {len(aggregated_links)} raw unique links"
                + (f" and {len(document_links)} documents." if document_links else ".")
            )
            return aggregated_links + document_links
        else:
            last_error = crawl_error or f"0 links from {target_candidate}"
            logger.warning(f"Candidate URL {target_candidate} yielded 0 links ({last_error}). Trying next candidate URL...")

    raise CrawlFailure(
        f"Crawl of {start_url} (and candidates {candidates}) produced zero links: {last_error}"
    )
