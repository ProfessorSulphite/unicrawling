"""
Ingestion budget accounting.

Today this owns only the per-notebook source cap. The *daily query* budget is
deliberately elsewhere: StateManager.reserve_queries() persists it in
state.sqlite because it spans runs, and a per-run module cannot enforce a
per-day ceiling (see decision D3).

C13 adds the up-front per-university query reservation here, per plan section 6.5.
"""
import logging
from typing import Optional

from src.config import config

logger = logging.getLogger("Ingest.quota")


def resolve_source_cap(max_sources: Optional[int] = None) -> int:
    """
    Effective ceiling on sources uploaded to one notebook.

    An explicit override wins; otherwise the configured cap applies. Kept as a
    function rather than an inline `or` so C13's sampling logic has one place to
    consult when it decides how many links a health check should draw from.
    """
    cap = max_sources or config.max_sources_per_notebook
    if cap < 1:
        raise ValueError(f"source cap must be >= 1, got {cap}")
    return cap
