"""
The university identity registry: sourced facts, keyed by canonical domain.

One file (resources/rankings_global.json), one loader, one lookup. Until C25
there were two of each -- rankings_pk.json read by the extractor and
rankings_global.json read by the normalizer -- with different schemas, separate
caches, and three domains present in both. That split had a cost: every entry in
rankings_pk.json carried `rankings: []`, and the extractor assigned that over
whatever the payload held, so NUST's QS rank of 353 and LUMS's 540 -- both
recorded in rankings_global.json -- were written out as empty on every run.

What lives here is deliberately dumb: a JSON read and a domain match. It exists
so that identity facts a student might act on come from a file someone wrote
down, rather than from a model asked to remember.

Leaf layer: nothing in utilities imports upward.
"""
import json
import logging
import threading
from typing import Any, Dict, Optional

from src.config import config

logger = logging.getLogger(__name__)

# Loaded once per process. Guarded because the query suite and the normalizer can
# both reach it, and a torn read would surface as missing identity facts rather
# than as an error.
_REGISTRY_CACHE: Optional[Dict[str, Any]] = None
_CACHE_LOCK = threading.Lock()


def load_registry() -> Dict[str, Any]:
    """The {domain: facts} map, read once and cached."""
    global _REGISTRY_CACHE
    if _REGISTRY_CACHE is not None:
        return _REGISTRY_CACHE

    with _CACHE_LOCK:
        if _REGISTRY_CACHE is not None:
            return _REGISTRY_CACHE
        try:
            with open(config.rankings_json_path, "r", encoding="utf-8") as f:
                _REGISTRY_CACHE = json.load(f).get("universities", {})
        except (OSError, json.JSONDecodeError) as e:
            # A missing registry degrades the payload; it does not stop a run.
            logger.warning(
                f"Registry unavailable at {config.rankings_json_path} ({e}); "
                f"proceeding without sourced identity facts."
            )
            _REGISTRY_CACHE = {}
    return _REGISTRY_CACHE


def reset_cache() -> None:
    """Drop the cached registry. For tests that point config at a fixture."""
    global _REGISTRY_CACHE
    with _CACHE_LOCK:
        _REGISTRY_CACHE = None


def canonical_domain(domain_or_url: str) -> str:
    """Reduce a URL or host to the bare lowercase host the registry is keyed by."""
    d = (domain_or_url or "").strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    d = d.split("/")[0].strip("/")
    if d.startswith("www."):
        d = d[4:]
    return d


def lookup(domain_or_url: str) -> Optional[Dict[str, Any]]:
    """
    Find a university's entry by canonical domain, alias, or subdomain.

    Three ways in, because one institution is reachable at many hosts:
    seecs.nust.edu.pk and pnec.nust.edu.pk are NUST, and a run started from
    either must get NUST's facts rather than none.

    The subdomain rule is anchored on a dot -- `key.endswith("." + canonical)` --
    so "notnust.edu.pk" does not match "nust.edu.pk".
    """
    registry = load_registry()
    key = canonical_domain(domain_or_url)
    if not key:
        return None
    if key in registry:
        return registry[key]
    for canonical, entry in registry.items():
        if key in entry.get("aliases", []):
            return entry
        if key.endswith("." + canonical):
            return entry
    return None
