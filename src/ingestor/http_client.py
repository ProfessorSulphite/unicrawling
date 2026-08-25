"""
Shared HTTP/2 connection pool for the ingestor.

One client is reused for every pre-flight probe and text-fallback fetch. Building
an AsyncClient per URL paid a fresh TCP + TLS handshake on every one of ~150 links
per university.
"""
import re
import asyncio
import logging
import weakref
from contextlib import asynccontextmanager
from typing import Any, Dict

import httpx

from src.config import config

logger = logging.getLogger("Ingest.http")

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


