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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.config import config
from src.utilities.registry import load_registry, lookup as registry_lookup
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
from src.extractor.crawlers.verification import verify_program_batch
from src.utilities.typesafe_client import is_typesafe_available

# Same registry entry as every other module in this package: logging.getLogger
# returns one object per name, so this is the logger extract_data.py created.
logger = logging.getLogger("ExtractData")


# ---------------------------------------------------------------------------
# Deterministic reference data
# ---------------------------------------------------------------------------

# C25 moved the registry itself to utilities/registry.py -- one file, one loader,
# one lookup, shared with the normalizer. These two names are kept as the
# extractor's vocabulary for it.
load_rankings_registry = load_registry
lookup_registry = registry_lookup


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

    # Assigned UNCONDITIONALLY, including when the registry has no rankings for
    # this university. That is the anti-hallucination rule and it is deliberate:
    # a rank the model supplied for an institution nobody recorded a rank for is
    # invented, and an empty list is the correct answer. What was broken before
    # C25 was the data, not this line -- every rankings_pk.json entry held [],
    # so this correctly-written assignment erased the sourced QS ranks that
    # rankings_global.json carried (NUST 353, LUMS 540) on every run.
    main_info.rankings = [RankingItem(**r) for r in entry.get("rankings", [])]
    main_info.domain_verified = True
    main_info.verification_note = (
        f"Identity fields sourced from {config.rankings_json_path.name} registry."
    )
    return main_info

# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

PROGRAM_QUERY_KEYS = ("bachelors", "masters", "phd", "diploma")


def _flag_cross_contaminated_buckets(
    results: Dict[str, Any], uni_name: str, report: "ExtractionReport"
) -> None:
    """
    Refuse to file two programme queries that came back with the same answer.

    The suite runs serially precisely so this cannot happen (see the note in
    extract_university_payload), but a duplicated answer is invisible in the
    output -- a full, plausible programme list under the wrong degree level --
    and it went undetected on a live run until the payload was read by hand. A
    guard that costs one set comparison is worth having permanently.

    Both offending blocks are dropped rather than one kept. There is no way to
    tell from here which query the shared answer actually belonged to, and
    filing it under a guess is how the bug did its damage in the first place.
    The queries are recorded as failed, so report.ok is False and the caller
    reports a partial extraction instead of a clean one.
    """
    seen: Dict[str, str] = {}
    for key in PROGRAM_QUERY_KEYS:
        value = results.get(key)
        if not value:
            continue
        # Compare the answers themselves, not the objects: two parses of one
        # response are distinct objects with identical content.
        fingerprint = json.dumps(
            [p.model_dump(mode="json") if hasattr(p, "model_dump") else p for p in value],
            sort_keys=True,
            default=str,
        )
        if fingerprint in seen:
            other = seen[fingerprint]
            msg = (
                f"identical answer returned for '{key}' and '{other}' "
                f"({len(value)} programmes) -- one query received the other's "
                f"response; both blocks dropped"
            )
            logger.error(f"{uni_name}: {msg}")
            report.failed[key] = msg
            report.failed[other] = msg
            results[key] = []
            results[other] = []
            continue
        seen[fingerprint] = key


async def extract_university_payload(
    client: NotebookLMClient,
    notebook_id: str,
    uni_name: str,
    uni_domain: str,
    source_ids_by_tier: Optional[Dict[int, List[str]]] = None,
    tier1_source_count: int = 0,
    report: Optional[ExtractionReport] = None,
    query_keys: Optional[Sequence[str]] = None,
    accumulated_results: Optional[Dict[str, Any]] = None,
) -> Tuple[UniversityPayload, ExtractionReport]:
    """
    Execute the query suite (or a targeted subset) and assemble a validated UniversityPayload.

    This function NEVER deletes the notebook. Deletion is a separate, explicit
    call made by the orchestrator only after the payload validates and has been
    persisted -- the previous implementation deleted inside a `finally:`, so any
    transient chat timeout destroyed all 60 ingested sources with no way to retry
    short of re-crawling and re-ingesting the whole university.

    `report` may be supplied by the caller, which is the only way to learn what a
    *failed* extraction spent. `queries_used` is the basis for reconciling the
    up-front query reservation, and when this function raises, a report it owns
    privately dies with the call -- leaving the caller to refund the whole suite
    including the queries that really were issued.

    Returns:
        (payload, report). Inspect report.ok / report.failed before trusting the
        payload: a query that failed yields an empty block, not an error.
    """
    report = ExtractionReport() if report is None else report

    def ids_for(spec: QuerySpec) -> Optional[List[str]]:
        if not source_ids_by_tier:
            return None
        ids: List[str] = []
        for tier in spec.tiers:
            ids.extend(source_ids_by_tier.get(tier, []))
        return ids or None

    results: Dict[str, Any] = dict(accumulated_results or {})
    target_suite = (
        [s for s in QUERY_SUITE if s.key in set(query_keys)]
        if query_keys is not None
        else QUERY_SUITE
    )

    for spec in target_suite:
        sub = ExtractionReport()
        try:
            query_res = await run_query(client, notebook_id, spec, ids_for(spec), sub)
            if spec.key in PROGRAM_QUERY_KEYS and spec.key in results:
                existing = results[spec.key] or []
                new_items = query_res or []
                existing_names = {p.name.lower().strip() for p in existing if hasattr(p, "name")}
                for p in new_items:
                    if hasattr(p, "name") and p.name.lower().strip() not in existing_names:
                        existing.append(p)
                        existing_names.add(p.name.lower().strip())
                results[spec.key] = existing
            else:
                results[spec.key] = query_res
        except BaseException:
            report.merge(sub)
            raise
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
    # Bucket names, QuerySpec keys and DegreeLevel values are all the same four
    # strings since C17, so this is a straight fan-out with nothing to translate.
    _flag_cross_contaminated_buckets(results, uni_name, report)
    bachelors = results.get("bachelors") or []
    masters = results.get("masters") or []
    phd = results.get("phd") or []
    diploma = results.get("diploma") or []

    # Apply Jev Grounding & Citation Verification Gate
    if is_typesafe_available():
        bachelors = await verify_program_batch(bachelors)
        masters = await verify_program_batch(masters)
        phd = await verify_program_batch(phd)
        diploma = await verify_program_batch(diploma)

    programs = ProgramCategoryBlock(
        bachelors=bachelors,
        masters=masters,
        phd=phd,
        diploma=diploma,
    )

    # --- Truncation signal ---
    # A notebook built from N Tier-1 programme pages that yields far fewer
    # programmes than pages almost certainly had its answer cut short. This is a
    # free quality flag; the field was previously hardcoded to False.
    total_programs = (
        len(programs.bachelors) + len(programs.masters)
        + len(programs.phd) + len(programs.diploma)
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
        # Carried ON the payload, not only in the report: the report dies with
        # the process, while the payload is what every downstream consumer -- the
        # master JSON, the auditor, the Supabase push -- actually reads. An empty
        # bucket and a failed query are indistinguishable without it.
        failed_query_blocks=sorted(report.failed),
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
