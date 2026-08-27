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
from src.utilities.schema import KeyLinks, MainInfo, UniversityPayload, UniversityType


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
    client = _extract_client([_Q1, _Q2, "[]", "[]", "[]"])
    payload, report = await extract_university_payload(client, "nb-1", "NUST", "nust.edu.pk")
    assert isinstance(payload, UniversityPayload)
    # The registry's official name deliberately overrides the model's "NUST";
    # identity facts come from resources/rankings_pk.json, not from the answer.
    assert payload.main_info.name == "National University of Sciences and Technology"
    assert payload.main_info.abbreviation == "NUST"
    assert payload.main_info.domain_verified is True
    assert len(payload.programs.undergraduate) == 1
    assert payload.contact.official_email == "info@nust.edu.pk"
    assert report.ok


async def test_extraction_does_not_delete_notebook():
    """
    Deletion previously lived in a `finally:`, so any transient chat error
    destroyed all 60 ingested sources. Deletion is now the orchestrator's call,
    made only after the payload is validated and persisted.
    """
    client = _extract_client([_Q1, _Q2, "[]", "[]", "[]"])
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
    ])
    payload, report = await extract_university_payload(client, "nb-1", "NUST", "nust.edu.pk")
    assert payload.programs.undergraduate == []
    assert not report.ok
    assert "undergraduate" in report.failed


async def test_extraction_scopes_queries_by_tier():
    """Each query must see only the sources that can answer it."""
    client = _extract_client([_Q1, _Q2, "[]", "[]", "[]"])
    by_tier = {1: ["s1"], 2: ["s2"], 3: ["s3"], 4: ["s4"]}
    await extract_university_payload(client, "nb-1", "NUST", "nust.edu.pk",
                                     source_ids_by_tier=by_tier)
    calls = client.chat.ask.call_args_list
    faculties_call = calls[4].kwargs
    assert faculties_call["source_ids"] == ["s3", "s1"]
    undergrad_call = calls[1].kwargs
    assert undergrad_call["source_ids"] == ["s1", "s2"]


async def test_extraction_flags_probable_truncation():
    """40 Tier-1 programme pages yielding 1 programme is a truncated answer."""
    client = _extract_client([_Q1, _Q2, "[]", "[]", "[]"])
    payload, _ = await extract_university_payload(
        client, "nb-1", "NUST", "nust.edu.pk", tier1_source_count=40)
    assert payload.programs_possibly_truncated is True


async def test_extraction_does_not_flag_healthy_yield():
    many = json.dumps([
        {"name": f"BS Program {i}", "degree_level": "undergraduate",
         "summary_3_lines": "x", "eligibility_requirements": {}}
        for i in range(30)
    ])
    client = _extract_client([_Q1, many, "[]", "[]", "[]"])
    payload, _ = await extract_university_payload(
        client, "nb-1", "NUST", "nust.edu.pk", tier1_source_count=27)
    assert payload.programs_possibly_truncated is False


async def test_query_suite_has_five_queries():
    """5 x 83 = 415 of 500, leaving 85 queries of retry headroom."""
    assert len(QUERY_SUITE) == 5
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


def test_registry_never_invents_rankings():
    """
    A numeric world rank is the most confidently hallucinated field in the payload.
    Until rankings_pk.json is populated from Webometrics/QS, rankings stay empty
    rather than being taken from the model's answer.
    """
    main = MainInfo(name="X", website="https://nust.edu.pk", description="d",
                    key_links=KeyLinks())
    assert apply_registry_facts(main, "nust.edu.pk").rankings == []


def test_registry_lookup_misses_are_non_fatal():
    main = MainInfo(name="Unknown Uni", website="https://unknown.edu.pk",
                    description="d", key_links=KeyLinks())
    out = apply_registry_facts(main, "unknown.edu.pk")
    assert out.name == "Unknown Uni"
    assert out.domain_verified is False
