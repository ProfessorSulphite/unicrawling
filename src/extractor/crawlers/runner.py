"""
Phase 3 orchestration: run the staged query plan, fill gaps from the deterministic
rankings registry and the Exa fallback, and return a validated UniversityPayload.

Notebook deletion lives here but is the caller's decision -- it happens only after
validation succeeds and the payload is written.

Top of this package's dependency order; nothing here is imported back.
"""

import asyncio
import json
import logging

from notebooklm import NotebookLMClient
from pydantic import BaseModel
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.config import config
from src.utilities.registry import load_registry, lookup as registry_lookup
from src.logger.notebook_logger import log_notebook_deleted
from src.ingestor.notebook_lifecycle import purge_notebook_sources
from src.utilities.schema import (
    ContactInfo,
    DegreeLevel,
    KeyLinks,
    MainInfo,
    ProgramCategoryBlock,
    ProgramItem,
    RankingItem,
    UniversityPayload,
    UniversityType,
)

from src.extractor.normalizers.degree_names import classify_degree_level
from src.extractor.crawlers.exa_enriching import exa_find_application_portal
from src.extractor.crawlers.notebook_querying import (
    ExtractionReport,
    QUERY_SUITE,
    QuerySpec,
    build_detail_specs,
    build_faculties_spec,
    build_gapfill_spec,
    build_identity_spec,
    build_roster_specs,
    match_roster_name,
    plan_query_budget,
    run_query,
)

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
# Roster / detail merging
# ---------------------------------------------------------------------------

PROGRAM_QUERY_KEYS = ("bachelors", "masters", "phd", "diploma")

# Fields a detail record may contribute to its roster row. `name` and
# `degree_level` are absent on purpose: the ROSTER owns identity. A detail ask
# that renames a programme, or files it under a different level, must not be
# able to rewrite the row it was asked about -- that is how one answer silently
# becomes another programme.
_MERGEABLE_FIELDS = tuple(
    f for f in ProgramItem.model_fields if f not in ("name", "degree_level")
)


def _is_empty_value(value: Any) -> bool:
    """
    Whether a field currently carries no information.

    A nested model counts as empty when every field inside it is. This is not a
    nicety: `eligibility_requirements` has a default_factory, so a roster row
    arrives holding a fully-constructed EligibilityRequirements with three empty
    fields. Treating that object as "already answered" makes the merge skip it
    forever, and the detail ask's marks, entry tests and aggregate formula are
    silently discarded -- the field then reads 0% in the audit no matter how well
    the model answered.
    """
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, str)):
        return len(value) == 0
    if isinstance(value, BaseModel):
        return all(_is_empty_value(v) for v in value.model_dump().values())
    return False


def merge_detail_into_roster(
    roster: Sequence[ProgramItem],
    details: Sequence[ProgramItem],
    report: ExtractionReport,
    label: str = "detail",
) -> List[ProgramItem]:
    """
    Fold detail records onto the roster rows they describe, keyed by name.

    The roster owns identity; a detail record only ever *fills* fields. An
    already-answered field is never overwritten, so a later gap-fill ask cannot
    replace a real per-programme fee with a university-wide one.

    A record matching no roster row is KEPT and recorded as an anomaly rather
    than dropped or silently appended. Both alternatives are worse: dropping it
    loses a real programme the roster missed, and appending it quietly lets a
    renamed duplicate through as a second programme.
    """
    if not details:
        return list(roster)

    by_name: Dict[str, ProgramItem] = {p.name: p for p in roster}
    names = list(by_name)
    merged = list(roster)
    orphans = 0
    filled = 0

    for record in details:
        target_name = match_roster_name(record.name, names)
        if target_name is None:
            orphans += 1
            if orphans <= 5:
                report.note(
                    f"[{label}] '{record.name}' matches no roster programme; "
                    f"kept as a separate entry."
                )
            merged.append(record)
            names.append(record.name)
            by_name[record.name] = record
            continue

        target = by_name[target_name]
        for field_name in _MERGEABLE_FIELDS:
            incoming = getattr(record, field_name, None)
            if _is_empty_value(incoming):
                continue
            if _is_empty_value(getattr(target, field_name, None)):
                setattr(target, field_name, incoming)
                filled += 1

    if orphans > 5:
        report.note(f"[{label}] {orphans} records matched no roster programme in total.")
    logger.info(
        f"[{label}] merged {len(details)} records onto the roster: "
        f"{filled} fields filled, {orphans} orphan(s)."
    )
    return merged


def arbitrate_cross_level_duplicates(
    programs: Sequence[ProgramItem],
    report: ExtractionReport,
) -> List[ProgramItem]:
    """
    Keep one row per programme name when the same name appears at two levels.

    The roster is a single ask, so a model that lists "MS Data Science" under
    both masters and diploma has contradicted itself within one answer. The
    arbiter is the programme NAME, via the normalizer's classify_degree_level --
    which already resolves the hard cases (MBBS, DPT and PharmD are entry-level
    bachelors despite reading as doctorates; MPhil is masters).

    Where the name cannot be read, the first occurrence wins and the conflict is
    recorded. Guessing a level would fabricate a fact a student acts on, which
    is the exact failure mode C19 exists to remove.
    """
    by_name: Dict[str, ProgramItem] = {}
    order: List[str] = []

    for item in programs:
        key = str(item.name).strip().casefold()
        if key not in by_name:
            by_name[key] = item
            order.append(key)
            continue

        incumbent = by_name[key]
        if incumbent.degree_level == item.degree_level:
            continue   # a plain duplicate; the first row already holds the data

        verdict = classify_degree_level(item.name)
        if verdict is None:
            report.note(
                f"'{item.name}' appears under both {incumbent.degree_level.value} and "
                f"{item.degree_level.value}; the name is unreadable, keeping the first."
            )
            continue

        winner = item if item.degree_level == verdict else incumbent
        loser = incumbent if winner is item else item
        report.note(
            f"'{item.name}' appeared under both {incumbent.degree_level.value} and "
            f"{item.degree_level.value}; the name reads as {verdict.value}, "
            f"dropping the {loser.degree_level.value} entry."
        )
        by_name[key] = winner

    return [by_name[k] for k in order]


def bucket_by_level(programs: Sequence[ProgramItem]) -> ProgramCategoryBlock:
    """Split a flat programme list into the four DegreeLevel buckets."""
    buckets: Dict[str, List[ProgramItem]] = {lvl.value: [] for lvl in DegreeLevel}
    for item in programs:
        level = getattr(item.degree_level, "value", str(item.degree_level))
        buckets.setdefault(level, []).append(item)
    return ProgramCategoryBlock(**buckets)


def programs_missing_fee_or_deadline(programs: Sequence[ProgramItem]) -> List[str]:
    """Names of programmes whose application fee or deadlines are still blank."""
    return [
        p.name for p in programs
        if _is_empty_value(p.application_fee) or _is_empty_value(p.application_deadlines)
    ]


def _flag_cross_contaminated_buckets(
    results: Dict[str, Any], uni_name: str, report: "ExtractionReport"
) -> None:
    """
    Refuse to file two programme queries that came back with the same answer.

    Guards the legacy JSON suite, where each degree level is its own ask. The
    suite runs serially precisely so this cannot happen (see the note in
    extract_university_payload), but a duplicated answer is invisible in the
    output -- a full, plausible programme list under the wrong degree level --
    and it went undetected on a live run until the payload was read by hand. A
    guard that costs one set comparison is worth having permanently.

    Both offending blocks are dropped rather than one kept. There is no way to
    tell from here which query the shared answer actually belonged to, and
    filing it under a guess is how the bug did its damage in the first place.

    The staged text plan is structurally immune -- there is one programme ask,
    the roster, so there is no second block for it to be confused with -- but
    this still runs over the final buckets, because it costs nothing and the
    JSON path is still reachable.
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


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# The plan runs SERIALLY, one ask at a time against this notebook. This is a
# correctness requirement, not a throughput choice.
#
# Concurrent asks against one notebook return each other's answers. Observed on
# a live ITU run (notebook 029c9450, 2026-09-03): the `bachelors` ask was in
# flight from 10:38:21 to 10:43:35 and the `phd` ask from 10:41:15 to 10:46:00;
# both returned byte-identical 4617-byte payloads, and the content was the PhD
# programmes. The bachelors bucket in that payload is equal to the phd bucket
# element for element. Two different prompts, one answer.
#
# The mechanism is last-write-wins on the conversation: an unkeyed chat.ask()
# polls the notebook for its newest turn, so an ask still waiting when a later
# ask's turn lands reads that turn instead of its own. The SDK's per-notebook
# lock guards conversation *creation*, not answer routing, so it does not
# prevent this.
#
# This is the worst failure shape available: no exception, no empty block, a
# full and plausible answer filed under the wrong degree level. It has been live
# since 4d531f0 and cost nothing in wall time to have -- concurrency was already
# not reducing wall time against a single notebook. It was pure downside.
#
# The real parallelism available is across *notebooks*, where no conversation is
# shared. That stays open; config.query_concurrency governs it and is
# deliberately not read here.


async def _run_stage(
    client: NotebookLMClient,
    notebook_id: str,
    spec: QuerySpec,
    source_ids: Optional[List[str]],
    report: ExtractionReport,
    uni_slug: Optional[str],
) -> Any:
    """Run one ask, merge its sub-report, and pace before the next one."""
    sub = ExtractionReport(index_offset=report.index_offset + report.queries_used)
    try:
        value = await run_query(client, notebook_id, spec, source_ids, sub, uni_slug)
    except BaseException:
        # A cancellation lands mid-ask, and without this the asks already issued
        # are absent from `report.queries_used` -- so the orchestrator's refund
        # would credit back queries that were really spent. `_attempt_query`
        # swallows ordinary Exceptions, so in practice this is the
        # CancelledError path: the watchdog, or a Ctrl-C.
        report.merge(sub)
        raise
    report.merge(sub)

    pacing = getattr(config, "query_pacing_delay_sec", 0.0)
    if pacing > 0:
        await asyncio.sleep(pacing)
    return value


async def extract_university_payload(
    client: NotebookLMClient,
    notebook_id: str,
    uni_name: str,
    uni_domain: str,
    source_ids_by_tier: Optional[Dict[int, List[str]]] = None,
    tier1_source_count: int = 0,
    report: Optional[ExtractionReport] = None,
    uni_slug: Optional[str] = None,
    reserve_more: Optional[Callable[[int], int]] = None,
) -> Tuple[UniversityPayload, ExtractionReport]:
    """
    Execute the staged query plan and assemble a validated UniversityPayload.

    This function NEVER deletes the notebook. Deletion is a separate, explicit
    call made by the orchestrator only after the payload validates and has been
    persisted -- the previous implementation deleted inside a `finally:`, so any
    transient chat timeout destroyed all ingested sources with no way to retry
    short of re-crawling and re-ingesting the whole university.

    `report` may be supplied by the caller, which is the only way to learn what a
    *failed* extraction spent. `queries_used` is the basis for reconciling the
    up-front query reservation, and when this function raises, a report it owns
    privately dies with the call -- leaving the caller to refund the whole plan
    including the queries that really were issued.

    `reserve_more(n) -> granted` is how a variable-length plan stays inside a
    fixed quota. The roster reveals how many detail asks are needed, which is
    only knowable after three asks have already run, so the caller reserves the
    base stages up front and this callback asks for the rest. Returning less
    than requested is normal and handled: the chunk size rises to fit, rather
    than programmes being dropped. Quota lives with the orchestrator, so this
    module never imports state management.

    Returns:
        (payload, report). Inspect report.ok / report.failed / report.anomalies
        before trusting the payload: a query that failed yields an empty block,
        not an error.
    """
    report = ExtractionReport() if report is None else report

    def ids_for(spec: QuerySpec) -> Optional[List[str]]:
        if not source_ids_by_tier:
            return None
        ids: List[str] = []
        for tier in spec.tiers:
            ids.extend(source_ids_by_tier.get(tier, []))
        return ids or None

    use_text = getattr(config, "response_format", "text") == "text"

    if use_text:
        main_info, contact_info, faculties, programs = await _run_staged_plan(
            client, notebook_id, uni_name, ids_for, report, uni_slug, reserve_more
        )
    else:
        main_info, contact_info, faculties, programs = await _run_legacy_suite(
            client, notebook_id, uni_name, ids_for, report, uni_slug
        )

    if main_info is None:
        logger.error(f"{uni_name}: identity query failed; emitting a minimal identity block.")
        main_info = MainInfo(
            name=uni_name,
            website=f"https://{uni_domain}",
            description=f"Identity block for {uni_name}; source extraction failed.",
            key_links=KeyLinks(),
        )
        contact_info = contact_info or ContactInfo()

    main_info = apply_registry_facts(main_info, uni_domain)

    if not main_info.key_links.application_portal_url:
        portal = await exa_find_application_portal(uni_domain, main_info.name)
        if portal:
            main_info.key_links.application_portal_url = portal
            main_info.exa_enriched = True

    # --- Truncation signal ---
    # A notebook built from N Tier-1 programme pages that yields far fewer
    # programmes than pages almost certainly had its answer cut short. This is a
    # free quality flag; the field was previously hardcoded to False.
    total_programs = len(programs)
    truncated = bool(tier1_source_count) and total_programs < max(1, tier1_source_count // 2)
    if truncated:
        logger.warning(
            f"{uni_name}: {total_programs} programmes extracted from {tier1_source_count} "
            f"Tier-1 sources -- flagging programs_possibly_truncated."
        )

    payload = UniversityPayload(
        main_info=main_info,
        programs=bucket_by_level(programs),
        faculties=faculties or [],
        contact=contact_info or ContactInfo(),
        programs_possibly_truncated=truncated,
        # Carried ON the payload, not only in the report: the report dies with
        # the process, while the payload is what every downstream consumer -- the
        # master JSON, the auditor, the Supabase push -- actually reads. An empty
        # bucket and a failed query are indistinguishable without it.
        failed_query_blocks=sorted(report.failed),
    )
    return payload, report


async def _run_staged_plan(
    client: NotebookLMClient,
    notebook_id: str,
    uni_name: str,
    ids_for: Callable[[QuerySpec], Optional[List[str]]],
    report: ExtractionReport,
    uni_slug: Optional[str],
    reserve_more: Optional[Callable[[int], int]],
):
    """Stages 1-5: identity, faculties, roster, bounded detail, gap-fill."""

    # --- Stages 1 & 2: identity and structure -----------------------------
    identity_spec = build_identity_spec()
    identity = await _run_stage(client, notebook_id, identity_spec, ids_for(identity_spec), report, uni_slug)
    main_info = identity.main_info if identity else None
    contact_info = identity.contact if identity else None

    faculties_spec = build_faculties_spec()
    faculties = await _run_stage(client, notebook_id, faculties_spec, ids_for(faculties_spec), report, uni_slug)

    # --- Stage 3: the roster ----------------------------------------------
    #
    # One ask by default; four (one per degree level) when
    # config.roster_split_by_level is on. Either way the answers are merged into
    # one roster before anything downstream sees them, so the rest of the plan
    # cannot tell which shape produced it.
    roster: List[ProgramItem] = []
    roster_specs = build_roster_specs()
    for spec in roster_specs:
        answer = await _run_stage(client, notebook_id, spec, ids_for(spec), report, uni_slug)
        if answer:
            roster.extend(answer)

    if not roster:
        report.note(
            f"{uni_name}: the roster ask returned no programmes; no detail asks were "
            f"issued. The payload will carry no programmes at all."
        )
        return main_info, contact_info, faculties, []

    if len(roster_specs) > 1:
        # A split roster can surface the same programme under two levels -- an
        # MPhil answered by both the masters and the diploma ask. The arbiter
        # below settles it on the programme NAME, which is the same rule a
        # single roster contradicting itself gets.
        logger.info(
            f"{uni_name}: merged {len(roster_specs)} roster asks into "
            f"{len(roster)} rows before arbitration."
        )

    roster = arbitrate_cross_level_duplicates(roster, report)
    logger.info(f"{uni_name}: roster enumerated {len(roster)} distinct programmes.")

    # --- Budget the detail stage against what the quota will actually grant --
    #
    # The roster is what makes this knowable, and it is only knowable now: the
    # ask count depends on an answer that cost three asks to obtain. Asking for
    # the remainder here, rather than reserving a worst case up front, is what
    # keeps a 17-programme university from holding quota sized for a 200-
    # programme one.
    max_total = int(getattr(config, "max_queries_per_university", 14))
    wants_gapfill = bool(getattr(config, "program_roster_gapfill", True))
    remaining_slots = max(1, max_total - report.queries_used - (1 if wants_gapfill else 0))

    chunk_size, want_asks = plan_query_budget(roster, budget=remaining_slots)

    granted = want_asks
    if reserve_more is not None:
        granted = max(0, int(reserve_more(want_asks + (1 if wants_gapfill else 0))))
        # The gap-fill slot is spent last and is optional; detail asks are not.
        granted = max(0, granted - (1 if wants_gapfill and granted > 1 else 0))
        if granted < want_asks:
            report.note(
                f"{uni_name}: quota granted {granted} of {want_asks} detail asks; "
                f"raising the chunk size to keep every programme described."
            )
            chunk_size, want_asks = plan_query_budget(roster, budget=max(1, granted))

    detail_specs = build_detail_specs(roster, chunk_size=chunk_size)

    if reserve_more is not None and granted <= 0:
        # The quota refused the top-up outright. Issuing the asks anyway would
        # spend budget the ledger has already said is not there -- which is the
        # exact failure the up-front reservation exists to prevent, just moved
        # later in the run. The roster is real data and is kept: every programme
        # is named and levelled, simply undescribed.
        report.note(
            f"{uni_name}: quota granted no detail asks; keeping the roster's "
            f"{len(roster)} programmes undescribed rather than overspending. "
            f"Re-run this university once quota frees."
        )
        return main_info, contact_info, faculties, roster

    if 0 < granted < len(detail_specs):
        dropped = len(detail_specs) - granted
        report.note(
            f"{uni_name}: the quota allows {granted} detail asks but {len(detail_specs)} "
            f"are needed even at the maximum chunk size ({chunk_size}). "
            f"{dropped} chunk(s) will not be described; those programmes keep their "
            f"roster fields only."
        )
        detail_specs = detail_specs[:granted]

    logger.info(
        f"{uni_name}: {len(detail_specs)} detail ask(s) of up to {chunk_size} programmes each."
    )

    # --- Stage 4: bounded detail ------------------------------------------
    details: List[ProgramItem] = []
    for spec in detail_specs:
        answer = await _run_stage(client, notebook_id, spec, ids_for(spec), report, uni_slug)
        if answer:
            details.extend(answer)

    programs = merge_detail_into_roster(roster, details, report, label="detail")

    # --- Stage 5: gap-fill -------------------------------------------------
    if wants_gapfill:
        missing = programs_missing_fee_or_deadline(programs)
        if not missing:
            logger.info(f"{uni_name}: no fee/deadline gaps; skipping the gap-fill ask.")
        elif report.queries_used >= max_total:
            report.note(
                f"{uni_name}: {len(missing)} programme(s) still missing a fee or deadline, "
                f"but the per-university ask ceiling ({max_total}) is reached."
            )
        else:
            gapfill_spec = build_gapfill_spec(missing)
            answer = await _run_stage(
                client, notebook_id, gapfill_spec, ids_for(gapfill_spec), report, uni_slug
            )
            if answer:
                programs = merge_detail_into_roster(programs, answer, report, label="gapfill")

    programs = arbitrate_cross_level_duplicates(programs, report)
    return main_info, contact_info, faculties, programs


async def _run_legacy_suite(
    client: NotebookLMClient,
    notebook_id: str,
    uni_name: str,
    ids_for: Callable[[QuerySpec], Optional[List[str]]],
    report: ExtractionReport,
    uni_slug: Optional[str],
):
    """
    The pre-C32 six-query JSON suite, reachable via config.response_format='json'.

    Kept so a regression in the staged path can be A/B'd against the same
    university rather than argued about.
    """
    results: Dict[str, Any] = {}
    for spec in QUERY_SUITE:
        results[spec.key] = await _run_stage(
            client, notebook_id, spec, ids_for(spec), report, uni_slug
        )

    q1 = results.get("main_info_contact")
    main_info = q1.main_info if q1 is not None else None
    contact_info = q1.contact if q1 is not None else None

    _flag_cross_contaminated_buckets(results, uni_name, report)
    programs: List[ProgramItem] = []
    for key in PROGRAM_QUERY_KEYS:
        programs.extend(results.get(key) or [])

    return main_info, contact_info, results.get("faculties") or [], programs


async def delete_notebook_after_success(client: NotebookLMClient, notebook_id: str, uni_slug: Optional[str] = None) -> bool:
    """
    Free the workspace slot. If notebook_mode is 'reusable', purges sources while preserving the notebook.
    Call ONLY once the payload is validated and written.
    """
    if getattr(config, "notebook_mode", "ephemeral") == "reusable":
        await purge_notebook_sources(client, notebook_id)
        logger.info(f"Preserved reusable worker notebook {notebook_id}; purged university sources.")
        return True

    try:
        await client.notebooks.delete(notebook_id)
        log_notebook_deleted(notebook_id=notebook_id, trigger="success_cleanup", uni_slug=uni_slug)
        logger.info(f"Deleted notebook {notebook_id}.")
        return True
    except Exception as e:
        logger.warning(f"Failed to delete notebook {notebook_id}: {e}")
        return False
