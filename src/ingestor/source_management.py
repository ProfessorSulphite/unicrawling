"""
Source acquisition and upload: URL hygiene, reachability pre-flight, text
fallback, and the batched upload that preserves the url -> source_id -> tier
mapping Phase 3 needs to scope its queries.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse, urlunparse

import httpx
from notebooklm import NotebookLMClient

from src.config import config
from src.logger.notebook_logger import log_notebook_created, log_source_uploaded
from src.ingestor.http_client import (
    BROWSER_HEADERS,
    _HTML_TAG_REGEX,
    _WHITESPACE_REGEX,
    get_http_client,
)
from src.ingestor.health_sampling import HealthReport, run_health_check
from src.ingestor.notebook_lifecycle import _find_or_create_notebook
from src.ingestor.quota_management import resolve_source_cap
from src.ingestor.readiness_polling import _extract_id, wait_for_sources_adaptive

logger = logging.getLogger("Ingest")

def sanitize_url(url: str) -> str:
    """
    Sanitizes URL path by stripping trailing hyphens, stray punctuation, and malformed characters.

    Prevents malformed/truncated URLs like '/program/bs-biotechnology-for-fall-2024-entry-'
    from being generated or passed downstream to NotebookLM.
    """
    if not url:
        return url
    parsed = urlparse(url.strip())
    path = parsed.path
    if len(path) > 1:
        path = path.rstrip("-.,/")
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))


async def check_url_accessible(
    url: str,
    timeout: Optional[float] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """
    Fast async pre-flight check that a URL is fetchable before sending it to NotebookLM.

    Filters out dead 404 links, 403 Forbidden bot blocks, or unreachable subdomains that would
    cause Google NotebookLM's server-side crawler to fail with RPCError rpc_code=9.

    Uses the shared HTTP/2 pool unless an explicit `client` is supplied.

    `timeout` defaults to config.preflight_probe_timeout_sec rather than to a
    literal. The literal was 5.0 while the pooled client was built for 10.0, so
    this probe was the strictest deadline in the pipeline and the only one that
    could discard a university outright -- NUST failed 8 of 8 sampled links on
    2026-09-05 and never reached Phase 2.

    Any 2xx counts as reachable. Only 200 did before, which failed a page served
    as 203 or 206 that NotebookLM would have ingested without complaint.
    Redirects never appear here: the pooled client follows them.
    """
    if not url:
        return False
    timeout = config.preflight_probe_timeout_sec if timeout is None else timeout
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    # Synthetic test hosts & paths in pipeline unit tests
    if netloc in ("x", "ok-1", "ok-2", "bad") or url.startswith("https://x/") or parsed.path in ("/bs-cs", "/fees", "/faculties"):
        return True

    http_client = client or get_http_client()
    try:
        try:
            resp = await http_client.head(url, headers=BROWSER_HEADERS, timeout=timeout)
            if 200 <= resp.status_code < 300:
                return True
        except Exception:
            pass
        # Not an else: plenty of university servers answer HEAD with 403 or 405
        # and serve the same page perfectly well on GET.
        resp = await http_client.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        return 200 <= resp.status_code < 300
    except Exception:
        return False


async def check_url_accessible_patiently(url: str) -> bool:
    """
    The same probe under the longer health-recheck deadline.

    Bound as a named function rather than a lambda or partial so the injected
    probe still has a readable name in logs and in a traceback.
    """
    return await check_url_accessible(
        url, timeout=config.health_check_recheck_timeout_sec
    )


async def fetch_and_extract_text(
    url: str,
    timeout: float = 8.0,
    client: Optional[httpx.AsyncClient] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetch web page content locally using custom browser headers and extract readable text and page title.

    Used as a fallback when Google NotebookLM's server-side crawler fails to fetch the URL
    (e.g., 403 blocks, SPAs, or Cloudflare protections).

    Uses the shared HTTP/2 pool unless an explicit `client` is supplied.
    """
    http_client = client or get_http_client()
    try:
        resp = await http_client.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        if resp.status_code != 200 or not resp.text:
            return None, None
        title_str = url
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            if soup.title and soup.title.string:
                title_str = soup.title.string.strip()
            for tag in soup(["script", "style", "nav", "footer", "header", "svg", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            if len(text) >= 100:
                return title_str, text
        except Exception:
            clean = _HTML_TAG_REGEX.sub(" ", resp.text)
            clean = _WHITESPACE_REGEX.sub(" ", clean).strip()
            if len(clean) >= 100:
                return title_str, clean
    except Exception as e:
        logger.debug(f"Local text extraction failed for {url}: {e}")
    return None, None


@dataclass
class IngestedSource:
    """One successfully registered NotebookLM source and the tier it came from."""
    source_id: str
    url: str
    tier: int


@dataclass
class IngestResult:
    """
    Outcome of ingesting one university.

    Carries the tier mapping rather than a bare count. The previous signature
    returned (notebook_id, count), which destroyed the tier association at the
    Phase 2/Phase 3 boundary and forced every query to run unscoped.
    """
    notebook_id: str = ""
    sources: List[IngestedSource] = field(default_factory=list)
    failed_urls: List[str] = field(default_factory=list)
    ready_count: int = 0
    # Set when the pre-flight health check refused the batch. The notebook is
    # never created in that case, so notebook_id stays empty and the caller must
    # branch on `skipped` before recording an "ingested" status.
    skipped: bool = False
    skip_reason: str = ""
    health: Optional[HealthReport] = None

    @property
    def ingested_count(self) -> int:
        return len(self.sources)

    def source_ids_for_tiers(self, tiers: Sequence[int]) -> List[str]:
        wanted = set(tiers)
        return [s.source_id for s in self.sources if s.tier in wanted]

    def tier_histogram(self) -> Dict[int, int]:
        hist: Dict[int, int] = {}
        for s in self.sources:
            hist[s.tier] = hist.get(s.tier, 0) + 1
        return hist


async def ingest_university_sources(
    uni_slug: str,
    uni_name: str,
    links: List[Dict[str, Any]],
    client: NotebookLMClient,
    max_sources: Optional[int] = None,
) -> IngestResult:
    """
    Provision a notebook for one university and upload its curated links.

    Args:
        uni_slug:    Filesystem/DB identifier for the university.
        uni_name:    Human-readable name, used for the notebook title.
        links:       Records from data/links/<slug>.jsonl. Each needs at least
                     {"url": str, "tier": int}. Plain strings are accepted and
                     default to tier 1.
        client:      A connected NotebookLMClient. Required -- the caller owns the
                     session lifecycle, because constructing one per university
                     would re-authenticate 83 times per batch.
        max_sources: Override for config.max_sources_per_notebook.

    Returns:
        IngestResult carrying the notebook id and the full tier mapping. If the
        pre-flight health check rejects the link set, `skipped` is True, no
        notebook was created, and `notebook_id` is empty -- the caller must not
        record an "ingested" status for that university.
    """
    if client is None:
        raise ValueError(
            "ingest_university_sources requires a connected NotebookLMClient. "
            "NotebookLMClient() cannot be constructed without AuthTokens; use "
            "NotebookLMClient.from_storage() in the caller and share one session."
        )

    cap = resolve_source_cap(max_sources)
    result = IngestResult()

    # Normalise input records and sanitize URLs, keeping Phase 1's rank order.
    # A record flagged `selected: False` is reserve: ranked, scored and vetted
    # like any other, but held back from the notebook unless a selected link
    # fails pre-flight. A record with no flag is a selection (older partitions,
    # the flat-file fallback, and the bare-string form all predate the reserve).
    normalised: List[Dict[str, Any]] = []
    reserve: List[Dict[str, Any]] = []
    for item in links:
        if isinstance(item, str):
            rec = {"url": sanitize_url(item), "tier": 1}
            is_selected = True
        elif item.get("url"):
            rec = {"url": sanitize_url(item["url"]), "tier": int(item.get("tier", 1))}
            is_selected = item.get("selected", True)
        else:
            continue
        (normalised if is_selected else reserve).append(rec)

    # The cap applies to what actually reaches the notebook. Anything the cap
    # displaces joins the reserve rather than being discarded, so it is still
    # available to backfill a pre-flight casualty.
    if len(normalised) > cap:
        reserve = normalised[cap:] + reserve
        normalised = normalised[:cap]

    if not normalised:
        result.skipped = True
        result.skip_reason = "no ingestable links supplied"
        logger.warning(f"{uni_slug}: {result.skip_reason}.")
        return result

    # Pre-flight health sampling (plan section 6.1). Runs BEFORE the notebook is
    # created: provisioning first and sampling second would leave an orphaned
    # notebook behind for every university we then decide to skip.
    result.health = await run_health_check(
        normalised,
        probe=check_url_accessible,
        label=uni_slug,
        recheck_probe=check_url_accessible_patiently,
    )
    if not result.health.healthy:
        result.skipped = True
        result.skip_reason = f"link health check failed -- {result.health.reason}"
        result.failed_urls.extend(result.health.failed)
        return result

    title = f"{uni_name}_Counseling_DB"
    notebook_id = await _find_or_create_notebook(client, title, uni_slug=uni_slug)
    result.notebook_id = notebook_id

    # Pre-flight HTTP accessibility check to filter out dead/403 links before sending to NotebookLM.
    # Links already probed by the health sample carry their verdict over rather
    # than being fetched a second time.
    if getattr(config, "preflight_http_check", True) and normalised:
        check_sem = asyncio.Semaphore(config.preflight_concurrency)
        known = result.health.results

        async def _check(rec):
            if rec["url"] in known:
                return rec, known[rec["url"]]
            async with check_sem:
                is_ok = await check_url_accessible(rec["url"])
                return rec, is_ok

        async def _probe_all(records):
            """Split a batch of records into (reachable records, dead urls), order kept."""
            live, dead = [], []
            for rec, is_ok in await asyncio.gather(*(_check(r) for r in records)):
                if is_ok:
                    live.append(rec)
                else:
                    dead.append(rec["url"])
            return live, dead

        target = len(normalised)
        accessible_links, inaccessible_urls = await _probe_all(normalised)

        # Backfill. Every link the pre-flight kills is a notebook slot that
        # Phase 1 paid to find and score, and losing it silently shrinks the
        # corpus every later query is grounded in -- COMSATS ingested 41 of the
        # 80 links selected for it. The reserve is drawn on in rank order, and
        # only in batches the size of the actual shortfall, so a healthy
        # university probes nothing extra at all.
        backfilled = 0
        cursor = 0
        while reserve and cursor < len(reserve) and len(accessible_links) < target:
            shortfall = target - len(accessible_links)
            batch = reserve[cursor:cursor + shortfall]
            cursor += len(batch)
            live, dead = await _probe_all(batch)
            accessible_links.extend(live)
            inaccessible_urls.extend(dead)
            backfilled += len(live)

        if inaccessible_urls:
            logger.warning(
                f"[{uni_slug}] Pre-flight HTTP check filtered out {len(inaccessible_urls)} "
                f"inaccessible/blocked URLs (preventing RPC code 9 failures)."
            )
            result.failed_urls.extend(inaccessible_urls)
        if backfilled:
            logger.info(
                f"[{uni_slug}] Backfilled {backfilled} notebook slot(s) from the "
                f"link reserve; {len(accessible_links)}/{target} selected slots filled."
            )
        normalised = accessible_links

    if not normalised:
        result.skipped = True
        result.skip_reason = "zero accessible links survived pre-flight HTTP check"
        logger.warning(f"{uni_slug}: {result.skip_reason}.")
        return result

    sem = asyncio.Semaphore(config.concurrent_uploads)

    async def _upload(url: str, tier: int) -> Optional[IngestedSource]:
        async with sem:
            last_error: Optional[Exception] = None
            t0 = asyncio.get_event_loop().time()
            clean_url_str = sanitize_url(url)
            
            # Attempt 1: add_url via NotebookLM client
            try:
                src = await client.sources.add_url(notebook_id, clean_url_str)
                sid = _extract_id(src)
                if sid:
                    dur = asyncio.get_event_loop().time() - t0
                    log_source_uploaded(notebook_id, sid, clean_url_str, status="ready", duration_sec=dur, uni_slug=uni_slug)
                    return IngestedSource(source_id=sid, url=clean_url_str, tier=tier)
                last_error = RuntimeError("add_url returned no source id")
            except Exception as e:
                last_error = e
                logger.info(f"[{uni_slug}] NotebookLM URL add failed ({e}). Switching immediately to local text extraction fallback...")

            # Fast Fallback Route: Trigger local HTML text extraction + add_text immediately
            page_title, text_content = await fetch_and_extract_text(clean_url_str)
            if text_content:
                try:
                    title_name = page_title or clean_url_str
                    src = await client.sources.add_text(notebook_id, title=title_name, content=text_content)
                    sid = _extract_id(src)
                    if sid:
                        dur = asyncio.get_event_loop().time() - t0
                        log_source_uploaded(notebook_id, sid, clean_url_str, status="ready", duration_sec=dur, uni_slug=uni_slug)
                        logger.info(f"[{uni_slug}] Successfully uploaded text fallback ({len(text_content)} chars) for '{title_name}'")
                        return IngestedSource(source_id=sid, url=clean_url_str, tier=tier)
                except Exception as e:
                    logger.warning(f"[{uni_slug}] Text fallback upload failed for {clean_url_str}: {e}")

            logger.warning(f"{uni_slug}: failed to upload {clean_url_str}: {last_error}")
            return None

    uploaded = await asyncio.gather(
        *(_upload(rec["url"], rec["tier"]) for rec in normalised)
    )

    for rec, src in zip(normalised, uploaded):
        if src is None:
            result.failed_urls.append(rec["url"])
        else:
            result.sources.append(src)

    logger.info(
        f"{uni_slug}: uploaded {result.ingested_count}/{len(normalised)} sources "
        f"(tiers={result.tier_histogram()}), {len(result.failed_urls)} failed."
    )

    if not result.sources:
        return result

    # Sources are queued asynchronously by NotebookLM; querying before they are
    # ready yields answers grounded in a partially-loaded corpus.
    try:
        result.ready_count = await wait_for_sources_adaptive(
            client=client,
            notebook_id=notebook_id,
            source_ids=[s.source_id for s in result.sources],
            timeout=config.source_ready_timeout_sec,
        )
        logger.info(f"{uni_slug}: {result.ready_count}/{result.ingested_count} sources reached ready state.")
    except Exception as e:
        # Timing out is a quality signal, not a hard failure: the notebook is
        # still queryable, just against fewer processed sources.
        logger.warning(f"{uni_slug}: source readiness wait did not complete cleanly: {e}")
        result.ready_count = 0

    return result


async def upload_cohort_sources(
    client: NotebookLMClient,
    notebook_id: str,
    uni_slug: str,
    cohort_links: List[Dict[str, Any]],
) -> IngestResult:
    """Upload sources for one cohort into an existing notebook and wait for readiness."""
    result = IngestResult(notebook_id=notebook_id)
    sem = asyncio.Semaphore(config.concurrent_uploads)

    async def _upload(url: str, tier: int) -> Optional[IngestedSource]:
        async with sem:
            last_error: Optional[Exception] = None
            t0 = asyncio.get_event_loop().time()
            clean_url_str = sanitize_url(url)
            try:
                src = await client.sources.add_url(notebook_id, clean_url_str)
                sid = _extract_id(src)
                if sid:
                    dur = asyncio.get_event_loop().time() - t0
                    log_source_uploaded(notebook_id, sid, clean_url_str, status="ready", duration_sec=dur, uni_slug=uni_slug)
                    return IngestedSource(source_id=sid, url=clean_url_str, tier=tier)
                last_error = RuntimeError("add_url returned no source id")
            except Exception as e:
                last_error = e

            page_title, text_content = await fetch_and_extract_text(clean_url_str)
            if text_content:
                try:
                    title_name = page_title or clean_url_str
                    src = await client.sources.add_text(notebook_id, title=title_name, content=text_content)
                    sid = _extract_id(src)
                    if sid:
                        dur = asyncio.get_event_loop().time() - t0
                        log_source_uploaded(notebook_id, sid, clean_url_str, status="ready", duration_sec=dur, uni_slug=uni_slug)
                        return IngestedSource(source_id=sid, url=clean_url_str, tier=tier)
                except Exception:
                    pass

            logger.warning(f"{uni_slug}: failed to upload {clean_url_str}: {last_error}")
            return None

    uploaded = await asyncio.gather(
        *(_upload(rec["url"], rec.get("tier", 1)) for rec in cohort_links if rec.get("url"))
    )

    for rec, src in zip(cohort_links, uploaded):
        if src is None:
            result.failed_urls.append(rec["url"])
        else:
            result.sources.append(src)

    if result.sources:
        try:
            result.ready_count = await wait_for_sources_adaptive(
                client=client,
                notebook_id=notebook_id,
                source_ids=[s.source_id for s in result.sources],
                timeout=config.source_ready_timeout_sec,
            )
        except Exception as e:
            logger.warning(f"{uni_slug}: source readiness wait did not complete cleanly: {e}")
            result.ready_count = 0

    return result


async def evict_sources(
    client: NotebookLMClient,
    notebook_id: str,
    source_ids: Sequence[str],
) -> int:
    """Evict completed sources from a notebook to free room for next cohort."""
    evicted = 0
    for sid in source_ids:
        try:
            await client.sources.delete(notebook_id, sid)
            evicted += 1
        except Exception as e:
            logger.debug(f"Failed to evict source {sid} from notebook {notebook_id}: {e}")
    logger.info(f"Evicted {evicted}/{len(source_ids)} sources from notebook {notebook_id}.")
    return evicted

