"""
Source readiness polling with jittered exponential backoff.

Sources are all created within a few seconds of each other, so pollers started in
lockstep re-converge on the same instants for the whole run. A randomised first
interval spreads the fan-out permanently.
"""
import asyncio
import logging
import random
from typing import Any, List, Optional, Sequence

from notebooklm import NotebookLMClient

from src.config import config

logger = logging.getLogger("Ingest.readiness")


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


