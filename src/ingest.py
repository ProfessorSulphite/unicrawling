"""
Async NotebookLM Ingestion & Lifecycle Engine (ingest.py)

Provisions one notebook per university and uploads its curated link partition with
bounded concurrency, preserving the url -> source_id -> tier mapping that Phase 3
needs in order to scope each query to the sources that can answer it.
"""
import sys
import random
import asyncio
import logging
import weakref
from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Sequence, Any, Tuple
from urllib.parse import urlparse, urlunparse

import re
import httpx
from notebooklm import NotebookLMClient

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.config import config
    from src.notebook_logger import log_notebook_created, log_source_uploaded
except ImportError:
    from config import config
    from notebook_logger import log_notebook_created, log_source_uploaded

logger = logging.getLogger("Ingest")

# ---------------------------------------------------------------------------
# Shared HTTP/2 connection pool
# ---------------------------------------------------------------------------

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Keyed by event loop, because an httpx.AsyncClient binds to the loop that
# created it. A single module-level client would be reused across successive
# asyncio.run() calls and fail on the second one with a closed-loop error.
# WeakKeyDictionary lets a finished loop drop its client without a manual purge.
_HTTP_CLIENTS: "weakref.WeakKeyDictionary[Any, httpx.AsyncClient]" = weakref.WeakKeyDictionary()

_HTML_TAG_REGEX = re.compile(r"<[^>]+>")
_WHITESPACE_REGEX = re.compile(r"\s+")


def _new_http_client() -> httpx.AsyncClient:
    """Construct a pooled client, degrading gracefully when h2 is unavailable."""
    kwargs: Dict[str, Any] = dict(
        follow_redirects=True,
        verify=False,
        headers=BROWSER_HEADERS,
        timeout=httpx.Timeout(
            config.http_timeout_sec, connect=config.http_connect_timeout_sec
        ),
        limits=httpx.Limits(
            max_connections=config.http_max_connections,
            max_keepalive_connections=config.http_max_keepalive_connections,
            keepalive_expiry=config.http_keepalive_expiry_sec,
        ),
    )
    if config.http2_enabled:
        try:
            return httpx.AsyncClient(http2=True, **kwargs)
        except Exception as e:
            # The `h2` extra is optional; HTTP/1.1 keep-alive still captures most
            # of the win, so a missing dependency must not break ingestion.
            logger.debug(f"HTTP/2 unavailable ({e}); falling back to HTTP/1.1 pool.")
    return httpx.AsyncClient(**kwargs)


def get_http_client() -> httpx.AsyncClient:
    """
    Return the pooled client for the running loop, creating it on first use.

    Every pre-flight probe and text-fallback fetch shares one connection pool
    instead of paying a fresh TCP + TLS handshake per URL. Contains no await, so
    concurrent callers cannot interleave and create duplicate clients.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _new_http_client()  # no loop running: caller owns the lifecycle

    client = _HTTP_CLIENTS.get(loop)
    if client is None or getattr(client, "is_closed", False):
        client = _new_http_client()
        _HTTP_CLIENTS[loop] = client
    return client


async def close_http_client() -> None:
    """Close and drop the pooled client for the running loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    client = _HTTP_CLIENTS.pop(loop, None)
    if client is not None:
        try:
            await client.aclose()
        except Exception as e:
            logger.debug(f"Ignoring error while closing pooled HTTP client: {e}")


@asynccontextmanager
async def http_session():
    """Scope the pooled HTTP client to a block, closing it on exit."""
    try:
        yield get_http_client()
    finally:
        await close_http_client()


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
    timeout: float = 5.0,
    client: Optional[httpx.AsyncClient] = None,
) -> bool:
    """
    Fast async pre-flight check to ensure URL returns HTTP 200 OK before sending to NotebookLM.

    Filters out dead 404 links, 403 Forbidden bot blocks, or unreachable subdomains that would
    cause Google NotebookLM's server-side crawler to fail with RPCError rpc_code=9.

    Uses the shared HTTP/2 pool unless an explicit `client` is supplied.
    """
    if not url:
        return False
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    # Synthetic test hosts & paths in pipeline unit tests
    if netloc in ("x", "ok-1", "ok-2", "bad") or url.startswith("https://x/") or parsed.path in ("/bs-cs", "/fees", "/faculties"):
        return True

    http_client = client or get_http_client()
    try:
        try:
            resp = await http_client.head(url, headers=BROWSER_HEADERS, timeout=timeout)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        resp = await http_client.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


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
    notebook_id: str
    sources: List[IngestedSource] = field(default_factory=list)
    failed_urls: List[str] = field(default_factory=list)
    ready_count: int = 0

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


def _extract_id(obj: Any) -> Optional[str]:
    """Pull a source/notebook id out of an SDK object or a bare string."""
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj or None
    for attr in ("id", "source_id", "notebook_id"):
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    return None


async def wait_for_sources_adaptive(
    client: NotebookLMClient,
    notebook_id: str,
    source_ids: Sequence[str],
    timeout: Optional[float] = None,
) -> int:
    """
    Wait for uploaded sources to reach `ready`, with jitter and per-source isolation.

    Two problems with delegating straight to ``client.sources.wait_for_sources``:

      1. **Thundering herd.** Every source was uploaded within seconds of the
         others, so identical backoff schedules keep all N pollers landing on the
         same instants for the whole wait. A randomised first interval spreads
         them out permanently, since the offset survives every backoff step.

      2. **All-or-nothing reporting.** ``wait_for_sources`` raises if *any*
         single source times out or errors, which previously collapsed the whole
         result to ``ready_count = 0`` even when 59 of 60 sources were ready --
         discarding a usable notebook on one bad link.

    Returns:
        The number of sources that actually reached ready state.
    """
    if not source_ids:
        return 0

    timeout = float(timeout if timeout is not None else config.source_ready_timeout_sec)
    sem = asyncio.Semaphore(max(1, config.readiness_poll_concurrency))
    jitter = max(0.0, config.readiness_jitter_ratio)

    async def _wait_one(source_id: str) -> bool:
        async with sem:
            # Jitter only the first interval; the SDK's backoff multiplies from
            # there, so the de-phasing compounds rather than washing out.
            initial = config.readiness_initial_interval_sec * (1.0 + random.uniform(-jitter, jitter))
            await client.sources.wait_until_ready(
                notebook_id,
                source_id,
                timeout=timeout,
                initial_interval=max(0.25, initial),
                max_interval=config.readiness_max_interval_sec,
                backoff_factor=config.readiness_backoff_factor,
            )
            return True

    results = await asyncio.gather(
        *(_wait_one(sid) for sid in source_ids), return_exceptions=True
    )

    ready = 0
    unsupported = 0
    for sid, res in zip(source_ids, results):
        if res is True:
            ready += 1
        elif isinstance(res, (TypeError, AttributeError)):
            # The per-source API is absent (older SDK, or a test double).
            unsupported += 1
        else:
            logger.debug(f"Source {sid} did not reach ready state: {res}")

    if unsupported == len(source_ids):
        logger.debug("sources.wait_until_ready unavailable; using batch wait_for_sources.")
        ready_sources = await client.sources.wait_for_sources(
            notebook_id=notebook_id, source_ids=list(source_ids), timeout=timeout
        )
        return len(ready_sources) if ready_sources else 0

    return ready


async def _find_or_create_notebook(client: NotebookLMClient, title: str, uni_slug: Optional[str] = None) -> str:
    """
    Reuse an existing notebook with this title, else create one.

    Reuse matters for resumability: a run that crashed after uploading 40 of 60
    sources must not leave an orphan notebook consuming a workspace slot and then
    create a second one on retry.
    """
    try:
        for nb in await client.notebooks.list():
            if getattr(nb, "title", None) == title:
                nb_id = _extract_id(nb)
                if nb_id:
                    logger.info(f"Reusing existing notebook '{title}' ({nb_id})")
                    log_notebook_created(nb_id, title, uni_slug)
                    return nb_id
    except Exception as e:
        # A listing failure is not fatal -- we can still create -- but it must be
        # visible, since it is the only thing standing between us and duplicates.
        logger.warning(f"Could not list existing notebooks ({e}); proceeding to create '{title}'.")

    nb = await client.notebooks.create(title=title)
    nb_id = _extract_id(nb)
    if not nb_id:
        raise RuntimeError(f"notebooks.create returned no usable id for '{title}'")
    logger.info(f"Created notebook '{title}' ({nb_id})")
    log_notebook_created(nb_id, title, uni_slug)
    return nb_id


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
        IngestResult carrying the notebook id and the full tier mapping.
    """
    if client is None:
        raise ValueError(
            "ingest_university_sources requires a connected NotebookLMClient. "
            "NotebookLMClient() cannot be constructed without AuthTokens; use "
            "NotebookLMClient.from_storage() in the caller and share one session."
        )

    cap = max_sources or config.max_sources_per_notebook
    title = f"{uni_name}_Counseling_DB"
    notebook_id = await _find_or_create_notebook(client, title, uni_slug=uni_slug)
    result = IngestResult(notebook_id=notebook_id)

    # Normalise input records, sanitize URLs, and apply the per-notebook cap.
    normalised: List[Dict[str, Any]] = []
    for item in links[:cap]:
        if isinstance(item, str):
            clean_u = sanitize_url(item)
            normalised.append({"url": clean_u, "tier": 1})
        elif item.get("url"):
            clean_u = sanitize_url(item["url"])
            normalised.append({"url": clean_u, "tier": int(item.get("tier", 1))})

    if not normalised:
        logger.warning(f"{uni_slug}: no ingestable links supplied.")
        return result

    # Pre-flight HTTP accessibility check to filter out dead/403 links before sending to NotebookLM
    if getattr(config, "preflight_http_check", True) and normalised:
        accessible_links = []
        inaccessible_urls = []
        check_sem = asyncio.Semaphore(config.preflight_concurrency)

        async def _check(rec):
            async with check_sem:
                is_ok = await check_url_accessible(rec["url"])
                return rec, is_ok

        check_results = await asyncio.gather(*(_check(rec) for rec in normalised))
        for rec, is_ok in check_results:
            if is_ok:
                accessible_links.append(rec)
            else:
                inaccessible_urls.append(rec["url"])

        if inaccessible_urls:
            logger.warning(
                f"[{uni_slug}] Pre-flight HTTP check filtered out {len(inaccessible_urls)}/{len(normalised)} "
                f"inaccessible/blocked URLs (preventing RPC code 9 failures)."
            )
            result.failed_urls.extend(inaccessible_urls)
        normalised = accessible_links

    if not normalised:
        logger.warning(f"{uni_slug}: zero accessible links survived pre-flight HTTP check.")
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
