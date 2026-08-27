"""
Phase 3 orchestration: run the query suite, fill gaps from the deterministic
rankings registry and the Exa fallback, and return a validated UniversityPayload.

Notebook deletion lives here but is the caller's decision -- it happens only after
validation succeeds and the payload is written.

Top of this package's dependency order; nothing here is imported back.
"""

import asyncio
import json
import logging

from notebooklm import NotebookLMClient
from typing import Any, Dict, List, Optional, Tuple

from src.config import config
from src.logger.notebook_logger import log_notebook_deleted
from src.utilities.schema import (
    ContactInfo,
    KeyLinks,
    MainInfo,
    ProgramCategoryBlock,
    RankingItem,
    UniversityPayload,
    UniversityType,
)

from src.extractor.crawlers.exa_enriching import exa_find_application_portal
from src.extractor.crawlers.notebook_querying import (
    ExtractionReport,
    QUERY_SUITE,
    QuerySpec,
    run_query,
)

# Same registry entry as every other module in this package: logging.getLogger
# returns one object per name, so this is the logger extract_data.py created.
logger = logging.getLogger("ExtractData")


# ---------------------------------------------------------------------------
# Deterministic reference data
# ---------------------------------------------------------------------------

_RANKINGS_CACHE: Optional[Dict[str, Any]] = None


def load_rankings_registry() -> Dict[str, Any]:
    """Load resources/rankings_pk.json once per process."""
    global _RANKINGS_CACHE
    if _RANKINGS_CACHE is None:
        try:
            with open(config.rankings_json_path, "r", encoding="utf-8") as f:
                _RANKINGS_CACHE = json.load(f).get("universities", {})
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Rankings registry unavailable ({e}); proceeding without it.")
            _RANKINGS_CACHE = {}
    return _RANKINGS_CACHE


def lookup_registry(domain: str) -> Optional[Dict[str, Any]]:
    """Find a university registry entry by canonical domain or alias."""
    registry = load_rankings_registry()
    key = domain.lower().replace("www.", "").strip("/")
    if key in registry:
        return registry[key]
    for canonical, entry in registry.items():
        if key == canonical or key in entry.get("aliases", []):
            return entry
        # Subdomain of a known institution (seecs.nust.edu.pk -> nust.edu.pk).
        if key.endswith("." + canonical):
            return entry
    return None


def apply_registry_facts(main_info: MainInfo, domain: str) -> MainInfo:
    """
    Overwrite LLM-guessed identity fields with registry ground truth.

    Rankings in particular are never taken from the model: a numeric world rank is
    the most confidently hallucinated field in the whole payload. If the registry
    has no rank, the payload correctly reports none.
    """
    entry = lookup_registry(domain)
    if not entry:
        return main_info

    main_info.name = entry.get("name") or main_info.name
    main_info.abbreviation = entry.get("abbreviation") or main_info.abbreviation
    main_info.city = entry.get("city") or main_info.city
    if entry.get("type"):
        try:
            main_info.type = UniversityType(entry["type"])
        except ValueError:
            pass

    main_info.rankings = [RankingItem(**r) for r in entry.get("rankings", [])]
    main_info.domain_verified = True
    main_info.verification_note = "Identity fields sourced from resources/rankings_pk.json registry."
    return main_info

# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def extract_university_payload(
    client: NotebookLMClient,
    notebook_id: str,
    uni_name: str,
    uni_domain: str,
    source_ids_by_tier: Optional[Dict[int, List[str]]] = None,
    tier1_source_count: int = 0,
) -> Tuple[UniversityPayload, ExtractionReport]:
    """
    Execute the 5-query suite and assemble a validated UniversityPayload.

    This function NEVER deletes the notebook. Deletion is a separate, explicit
    call made by the orchestrator only after the payload validates and has been
    persisted -- the previous implementation deleted inside a `finally:`, so any
    transient chat timeout destroyed all 60 ingested sources with no way to retry
    short of re-crawling and re-ingesting the whole university.

    Returns:
        (payload, report). Inspect report.ok / report.failed before trusting the
        payload: a query that failed yields an empty block, not an error.
    """
    report = ExtractionReport()

    def ids_for(spec: QuerySpec) -> Optional[List[str]]:
        if not source_ids_by_tier:
            return None
        ids: List[str] = []
        for tier in spec.tiers:
            ids.extend(source_ids_by_tier.get(tier, []))
        return ids or None

    # The five queries share no data, so they are issued concurrently under a
    # semaphore rather than in a serial loop.
    #
    # IMPORTANT -- measured, not assumed: against a single notebook this does NOT
    # currently reduce wall time. The notebooklm SDK takes a per-notebook_id lock
    # for the full duration of any chat.ask() made without a conversation_id
    # (_chat/api.py: `async with self._get_new_conversation_lock(notebook_id)`),
    # because the server treats concurrent unkeyed asks as racing turn N+1. The
    # SDK exposes no way to create independent conversations, so the suite
    # serialises inside the client no matter what we do here.
    #
    # This structure is kept because it is correct, costs nothing when
    # serialised, and is the piece that would have to exist anyway: the real
    # win available today is running multiple *notebooks* concurrently, where
    # the per-notebook lock no longer binds.
    sem = asyncio.Semaphore(max(1, config.query_concurrency))

    async def _run_one(spec: QuerySpec) -> Tuple[str, Any, ExtractionReport]:
        # Each query accumulates into its own sub-report, which is merged back in
        # QUERY_SUITE order below. Sharing one report across concurrent tasks
        # would make report.succeeded ordering depend on which answer landed
        # first, so identical inputs could produce different reports.
        sub = ExtractionReport()
        async with sem:
            value = await run_query(client, notebook_id, spec, ids_for(spec), sub)
        return spec.key, value, sub

    completed = await asyncio.gather(*(_run_one(spec) for spec in QUERY_SUITE))

    results: Dict[str, Any] = {}
    for key, value, sub in completed:   # gather preserves QUERY_SUITE order
        results[key] = value
        report.merge(sub)

    # --- Block 1 & 4: main_info + contact ---
    q1 = results.get("main_info_contact")
    if q1 is not None:
        main_info, contact_info = q1.main_info, q1.contact
    else:
        logger.error(f"{uni_name}: main_info query failed; emitting a minimal identity block.")
        main_info = MainInfo(
            name=uni_name,
            website=f"https://{uni_domain}",
            description=f"Identity block for {uni_name}; source extraction failed.",
            key_links=KeyLinks(),
        )
        contact_info = ContactInfo()

    main_info = apply_registry_facts(main_info, uni_domain)

    if not main_info.key_links.application_portal_url:
        portal = await exa_find_application_portal(uni_domain, main_info.name)
        if portal:
            main_info.key_links.application_portal_url = portal
            main_info.exa_enriched = True

    # --- Block 2: programs ---
    programs = ProgramCategoryBlock(
        undergraduate=results.get("undergraduate") or [],
        graduate=results.get("graduate") or [],
        postgraduate_and_phd=results.get("postgraduate_phd") or [],
    )

    # --- Truncation signal ---
    # A notebook built from N Tier-1 programme pages that yields far fewer
    # programmes than pages almost certainly had its answer cut short. This is a
    # free quality flag; the field was previously hardcoded to False.
    total_programs = (
        len(programs.undergraduate) + len(programs.graduate) + len(programs.postgraduate_and_phd)
    )
    truncated = bool(tier1_source_count) and total_programs < max(1, tier1_source_count // 2)
    if truncated:
        logger.warning(
            f"{uni_name}: {total_programs} programmes extracted from {tier1_source_count} "
            f"Tier-1 sources -- flagging programs_possibly_truncated."
        )

    payload = UniversityPayload(
        main_info=main_info,
        programs=programs,
        faculties=results.get("faculties") or [],
        contact=contact_info,
        programs_possibly_truncated=truncated,
    )
    return payload, report


async def delete_notebook_after_success(client: NotebookLMClient, notebook_id: str, uni_slug: Optional[str] = None) -> bool:
    """
    Free the workspace slot. Call ONLY once the payload is validated and written.

    Returns True on success; a failed delete leaks a slot but must never mask a
    successful extraction.
    """
    try:
        await client.notebooks.delete(notebook_id)
        log_notebook_deleted(notebook_id=notebook_id, trigger="success_cleanup", uni_slug=uni_slug)
        logger.info(f"Deleted notebook {notebook_id}.")
        return True
    except Exception as e:
        logger.warning(f"Failed to delete notebook {notebook_id}: {e}")
        return False
