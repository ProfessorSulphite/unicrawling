"""
Phase 3 orchestration and the deterministic rankings registry.

Covers what extract_university_payload guarantees: a validated payload, per-query
failure reporting rather than a silently clean result, tier-scoped queries, a
truncation flag, and a notebook that is never deleted by the extractor itself.

Moved out of tests/test_pipeline.py in C15.
"""
import json

from unittest.mock import AsyncMock, MagicMock

from src.extractor.crawlers.notebook_querying import QUERY_SUITE
from src.extractor.crawlers.runner import (
    apply_registry_facts,
    extract_university_payload,
    lookup_registry,
)
from src.config import config
from src.utilities.schema import (
    RankingItem,
    DegreeLevel,
    KeyLinks,
    MainInfo,
    UniversityPayload,
    UniversityType,
)


_Q1 = json.dumps({
    "main_info": {
        "name": "NUST", "website": "https://nust.edu.pk", "type": "public",
        "description": "National University of Sciences and Technology",
        "key_links": {"academics_url": "https://nust.edu.pk/academics",
                      "admissions_url": "https://nust.edu.pk/admissions",
                      "application_portal_url": None},
        "rankings": [],
    },
    "contact": {"official_email": "info@nust.edu.pk", "phone_numbers": ["+92-51-90851000"],
                "physical_address": "H-12, Islamabad", "sub_campuses_contact": []},
})
_Q2 = json.dumps([{"name": "BS Computer Science", "degree_level": "undergraduate",
                   "summary_3_lines": "BSCS overview", "eligibility_requirements": {}}])


def _extract_client(answers):
    client = MagicMock()
    client.chat.ask = AsyncMock(side_effect=[MagicMock(answer=a) for a in answers])
    client.notebooks.delete = AsyncMock()
    return client


async def test_extraction_builds_validated_payload():
    client = _extract_client([_Q1, _Q2, "[]", "[]", "[]", "[]"])
    payload, report = await extract_university_payload(client, "nb-1", "NUST", "nust.edu.pk")
    assert isinstance(payload, UniversityPayload)
    # The registry's official name deliberately overrides the model's "NUST";
    # identity facts come from the registry, not from the answer.
    assert payload.main_info.name == "National University of Sciences and Technology"
    assert payload.main_info.abbreviation == "NUST"
    assert payload.main_info.domain_verified is True
    assert len(payload.programs.bachelors) == 1
    assert payload.contact.official_email == "info@nust.edu.pk"
    assert report.ok


async def test_extraction_does_not_delete_notebook():
    """
    Deletion previously lived in a `finally:`, so any transient chat error
    destroyed all 60 ingested sources. Deletion is now the orchestrator's call,
    made only after the payload is validated and persisted.
    """
    client = _extract_client([_Q1, _Q2, "[]", "[]", "[]", "[]"])
    await extract_university_payload(client, "nb-1", "NUST", "nust.edu.pk")
    client.notebooks.delete.assert_not_called()


async def test_extraction_survives_a_failing_query_and_reports_it():
    client = MagicMock()
    client.chat.ask = AsyncMock(side_effect=[
        MagicMock(answer=_Q1),
        MagicMock(answer="I don't know."),   # Q2 attempt 1
        MagicMock(answer="Still no."),        # Q2 retry 1
        MagicMock(answer="Nope."),            # Q2 retry 2
        MagicMock(answer="[]"),
        MagicMock(answer="[]"),
        MagicMock(answer="[]"),
        MagicMock(answer="[]"),
    ])
    payload, report = await extract_university_payload(client, "nb-1", "NUST", "nust.edu.pk")
    assert payload.programs.bachelors == []
    assert not report.ok
    assert "bachelors" in report.failed


async def test_extraction_scopes_queries_by_tier():
    """Each query must see only the sources that can answer it."""
    client = _extract_client([_Q1, _Q2, "[]", "[]", "[]", "[]"])
    by_tier = {1: ["s1"], 2: ["s2"], 3: ["s3"], 4: ["s4"]}
    await extract_university_payload(client, "nb-1", "NUST", "nust.edu.pk",
                                     source_ids_by_tier=by_tier)
    # Indexed by suite position rather than a literal, so inserting a query
    # (C17 added diploma ahead of faculties) does not silently re-point these
    # at the wrong call.
    calls = client.chat.ask.call_args_list
    order = [spec.key for spec in QUERY_SUITE]
    assert calls[order.index("faculties")].kwargs["source_ids"] == ["s3", "s1"]
    assert calls[order.index("bachelors")].kwargs["source_ids"] == ["s1", "s2"]


async def test_extraction_flags_probable_truncation():
    """40 Tier-1 programme pages yielding 1 programme is a truncated answer."""
    client = _extract_client([_Q1, _Q2, "[]", "[]", "[]", "[]"])
    payload, _ = await extract_university_payload(
        client, "nb-1", "NUST", "nust.edu.pk", tier1_source_count=40)
    assert payload.programs_possibly_truncated is True


async def test_extraction_does_not_flag_healthy_yield():
    many = json.dumps([
        {"name": f"BS Program {i}", "degree_level": "undergraduate",
         "summary_3_lines": "x", "eligibility_requirements": {}}
        for i in range(30)
    ])
    client = _extract_client([_Q1, many, "[]", "[]", "[]", "[]"])
    payload, _ = await extract_university_payload(
        client, "nb-1", "NUST", "nust.edu.pk", tier1_source_count=27)
    assert payload.programs_possibly_truncated is False


async def test_query_suite_matches_the_reserved_budget():
    """reserve_queries() claims queries_per_university up front, before a single
    query is issued. If the suite grows past that number the ledger under-counts
    and the daily NotebookLM cap is silently overrun, so the two are pinned
    together here rather than left to drift.

    C17 took the suite from 5 to 6 by adding the diploma query.
    """
    assert len(QUERY_SUITE) == 6
    assert config.queries_per_university == len(QUERY_SUITE)


async def test_query_suite_covers_every_degree_level():
    """One programme query per DegreeLevel, keyed by the level's own value."""
    keys = {spec.key for spec in QUERY_SUITE}
    assert {level.value for level in DegreeLevel} <= keys
def test_registry_lookup_resolves_aliases_and_subdomains():
    assert lookup_registry("nust.edu.pk") is not None
    assert lookup_registry("www.nust.edu.pk") is not None
    assert lookup_registry("seecs.nust.edu.pk")["abbreviation"] == "NUST"


def test_registry_overrides_llm_identity_fields():
    main = MainInfo(name="Nust Univ", website="https://nust.edu.pk",
                    description="d", key_links=KeyLinks(), type=UniversityType.PRIVATE)
    out = apply_registry_facts(main, "nust.edu.pk")
    assert out.name == "National University of Sciences and Technology"
    assert out.type == UniversityType.PUBLIC
    assert out.domain_verified is True


def test_rankings_come_from_the_registry_not_the_answer():
    """
    A numeric world rank is the most confidently hallucinated field in the
    payload, so it is only ever the registry's to supply. C25 populated the
    registry, so this now asserts the sourced rank arrives -- the previous
    version asserted `== []`, which was true only because rankings_pk.json held
    no rankings at all, and that emptiness was itself the bug.
    """
    main = MainInfo(name="X", website="https://nust.edu.pk", description="d",
                    key_links=KeyLinks())
    out = apply_registry_facts(main, "nust.edu.pk")
    assert [r.rank for r in out.rankings] == [353]
    assert out.rankings[0].source == "QS World University Rankings"


def test_a_model_supplied_rank_is_erased_for_an_unranked_university():
    """
    The other half, and the one that matters: ITU has no recorded ranking, so a
    rank in the model's answer is invented and must not survive. The assignment
    is unconditional for exactly this reason.
    """
    main = MainInfo(
        name="ITU", website="https://itu.edu.pk", description="d", key_links=KeyLinks(),
        rankings=[RankingItem(source="QS", scope="Global", year=2026, rank=12)],
    )
    assert apply_registry_facts(main, "itu.edu.pk").rankings == []


def test_registry_lookup_misses_are_non_fatal():
    main = MainInfo(name="Unknown Uni", website="https://unknown.edu.pk",
                    description="d", key_links=KeyLinks())
    out = apply_registry_facts(main, "unknown.edu.pk")
    assert out.name == "Unknown Uni"
    assert out.domain_verified is False


# ------------------------------------------- concurrent asks cross-contaminate --
#
# Found on a live ITU run, notebook 029c9450, 2026-09-03. The `bachelors` ask was
# in flight 10:38:21-10:43:35 and the `phd` ask 10:41:15-10:46:00; both returned
# byte-identical 4617-byte payloads containing the PhD programmes, and the
# bachelors bucket of the saved payload equalled the phd bucket element for
# element. Two different prompts, one answer, no error raised.
#
# An unkeyed chat.ask() polls the notebook for its newest turn, so an ask still
# waiting when a later ask's turn lands reads that turn instead of its own.

_PHD_ANSWER = json.dumps([
    {"name": "PhD Computer Science", "degree_level": "phd",
     "department": "Department of Computer Science", "eligibility_requirements": {}},
    {"name": "PhD Electrical Engineering", "degree_level": "phd",
     "department": "Electrical Engineering Department", "eligibility_requirements": {}},
])


def test_the_query_suite_issues_one_ask_at_a_time():
    """
    The structural guarantee. A semaphore with a raised limit, or a stray
    asyncio.gather, reintroduces the race -- so this reads the source rather
    than trusting a comment.
    """
    import ast
    import inspect as _inspect

    from src.extractor.crawlers import runner as crawler_runner

    tree = ast.parse(_inspect.getsource(crawler_runner.extract_university_payload))
    called = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "gather" not in called, "the suite must not fan the queries out concurrently"
    assert "Semaphore" not in called, "a semaphore here means more than one ask is in flight"


async def test_two_queries_returning_one_answer_are_both_dropped():
    """
    The safety net for the failure above. Neither block may be filed: there is
    no way to tell which query the shared answer belonged to, and guessing is
    exactly how the bug did its damage.
    """
    client = _extract_client([_Q1, _PHD_ANSWER, "[]", _PHD_ANSWER, "[]", "[]"])
    payload, report = await extract_university_payload(client, "nb-1", "ITU", "itu.edu.pk")

    assert payload.programs.bachelors == []
    assert payload.programs.phd == []
    assert not report.ok, "a duplicated answer must not report a clean extraction"
    assert set(report.failed) == {"bachelors", "phd"}
    assert "received the other's response" in report.failed["bachelors"]


async def test_the_saved_payload_can_never_repeat_the_itu_shape():
    """The exact assertion that would have caught it: no two buckets are equal."""
    client = _extract_client([_Q1, _PHD_ANSWER, "[]", _PHD_ANSWER, "[]", "[]"])
    payload, _ = await extract_university_payload(client, "nb-1", "ITU", "itu.edu.pk")

    buckets = {
        "bachelors": payload.programs.bachelors,
        "masters": payload.programs.masters,
        "phd": payload.programs.phd,
        "diploma": payload.programs.diploma,
    }
    for a in buckets:
        for b in buckets:
            if a < b and buckets[a] and buckets[b]:
                assert buckets[a] != buckets[b], f"{a} and {b} hold the same programmes"


async def test_two_empty_queries_are_not_treated_as_contamination():
    """Four empty arrays are the normal case for a small university, not a bug."""
    client = _extract_client([_Q1, "[]", "[]", "[]", "[]", "[]"])
    payload, report = await extract_university_payload(client, "nb-1", "ITU", "itu.edu.pk")
    assert report.ok
    assert payload.programs.bachelors == [] and payload.programs.phd == []


async def test_genuinely_different_answers_are_left_alone():
    bs = json.dumps([{"name": "BS Computer Science", "degree_level": "bachelors",
                      "eligibility_requirements": {}}])
    client = _extract_client([_Q1, bs, "[]", _PHD_ANSWER, "[]", "[]"])
    payload, report = await extract_university_payload(client, "nb-1", "ITU", "itu.edu.pk")

    assert [p.name for p in payload.programs.bachelors] == ["BS Computer Science"]
    assert [p.name for p in payload.programs.phd] == [
        "PhD Computer Science", "PhD Electrical Engineering",
    ]
    assert report.ok
