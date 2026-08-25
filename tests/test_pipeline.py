"""
Production test suite for the Education Counselor RAG pipeline.

Every test here is a regression test for a defect that was actually present in the
shipped code, not a smoke test. Each one is annotated with the failure it locks out.

Phase 1 link-extraction tests moved to tests/test_extractor/test_linkers_*.py in
C14. What remains -- JSON repair, ingestion, extraction, the rankings registry --
leaves for tests/test_extractor/ and tests/test_ingestor/ in C15, and this file is
rebuilt as an orchestrator test in C27.
"""
import json
from typing import List

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.schema import (
    UniversityPayload, MainInfo, KeyLinks, ContactInfo, ProgramItem,
    DegreeLevel, ApplicationStatus, UniversityType,
)
from src.extract_data import (
    repair_and_validate_json, strip_citation_markers,
    ExtractionError, QUERY_SUITE,
    extract_university_payload, apply_registry_facts, lookup_registry,
)
from src.ingest import ingest_university_sources









# =============================================================================
# B5.5 -- LLM JSON repair
# =============================================================================

def test_json_repair_strips_fences_and_citations():
    raw = """Here is the requested program list [1]:
    ```json
    [
      {
        "name": "BS Artificial Intelligence [2, 3]",
        "program_info_link": "https://nust.edu.pk/bsai",
        "degree_level": "undergraduate",
        "duration": "4 Years",
        "summary_3_lines": "Line 1\\nLine 2\\nLine 3",
        "eligibility_requirements": {"minimum_marks_percentage": "60%",
                                     "entry_tests_accepted": ["NET"]},
        "application_status": "open"
      }
    ]
    ```
    End of response."""
    programs = repair_and_validate_json(raw, List[ProgramItem])
    assert len(programs) == 1
    # No trailing whitespace debris: the old regex left "BS Artificial Intelligence ".
    assert programs[0].name == "BS Artificial Intelligence"
    assert programs[0].degree_level == DegreeLevel.UNDERGRADUATE
    assert programs[0].application_status == ApplicationStatus.OPEN


def test_json_repair_preserves_numeric_arrays():
    """
    The naive citation regex \\[\\s*\\d+(,\\d+)*\\s*\\] applied to the whole document
    deletes any all-numeric JSON array. "phone_numbers": [1234567] became
    "phone_numbers":  -- an unparseable document. Markers must be stripped only
    from inside string literals.
    """
    raw = '{"official_email": "a@b.pk [2]", "phone_numbers": [1234567, 89], "physical_address": "H-12 [3, 4]"}'
    contact = repair_and_validate_json(raw, ContactInfo)
    assert contact.phone_numbers == ["1234567", "89"]
    assert contact.official_email == "a@b.pk"
    assert contact.physical_address == "H-12"


def test_json_repair_ignores_prose_citation_before_json():
    """A leading '[1]' must not be mistaken for the start of the JSON array."""
    raw = 'Sure, here you go [1]:\n{"official_email": "x@y.pk", "phone_numbers": []}'
    contact = repair_and_validate_json(raw, ContactInfo)
    assert contact.official_email == "x@y.pk"


def test_json_repair_handles_trailing_commas():
    raw = '{"official_email": "x@y.pk", "phone_numbers": ["1",],}'
    assert repair_and_validate_json(raw, ContactInfo).phone_numbers == ["1"]


def test_json_repair_recovers_truncated_answer():
    """A response cut off mid-array must yield the complete objects it did contain."""
    raw = ('[{"name": "BS CS", "degree_level": "undergraduate", '
           '"summary_3_lines": "x", "eligibility_requirements": {}')
    programs = repair_and_validate_json(raw, List[ProgramItem])
    assert len(programs) == 1 and programs[0].name == "BS CS"


def test_json_repair_coerces_scalar_phone_number():
    raw = '{"phone_numbers": "+92-51-90851000"}'
    assert repair_and_validate_json(raw, ContactInfo).phone_numbers == ["+92-51-90851000"]


def test_json_repair_raises_on_unrecoverable_input():
    """Failures must surface as ExtractionError so the caller can re-ask."""
    with pytest.raises(ExtractionError):
        repair_and_validate_json("I could not find any programmes.", List[ProgramItem])


def test_strip_citation_markers_leaves_structure_intact():
    src = '{"a": "x [1]", "b": [1, 2, 3]}'
    assert json.loads(strip_citation_markers(src)) == {"a": "x", "b": [1, 2, 3]}


# =============================================================================
# Phase 2 ingestion
# =============================================================================

def _mock_client():
    client = MagicMock()
    nb = MagicMock(); nb.id = "nb-123"; nb.title = "NUST_Counseling_DB"
    client.notebooks.list = AsyncMock(return_value=[])
    client.notebooks.create = AsyncMock(return_value=nb)
    client.notebooks.delete = AsyncMock()
    counter = {"n": 0}

    async def _add_url(notebook_id, url, **kw):
        counter["n"] += 1
        src = MagicMock(); src.id = f"src-{counter['n']}"
        return src

    client.sources.add_url = AsyncMock(side_effect=_add_url)
    client.sources.wait_for_sources = AsyncMock(side_effect=lambda **kw: [MagicMock()] * len(kw["source_ids"]))
    return client


async def test_ingest_returns_tier_mapping():
    client = _mock_client()
    links = [
        {"url": "https://nust.edu.pk/bs-cs", "tier": 1},
        {"url": "https://nust.edu.pk/fees", "tier": 2},
        {"url": "https://nust.edu.pk/faculties", "tier": 3},
    ]
    res = await ingest_university_sources("nust", "NUST", links, client=client)
    assert res.notebook_id == "nb-123"
    assert res.ingested_count == 3
    assert res.tier_histogram() == {1: 1, 2: 1, 3: 1}
    assert len(res.source_ids_for_tiers([1, 2])) == 2


async def test_ingest_reuses_existing_notebook():
    """Retrying a crashed run must not leak a second notebook per university."""
    client = _mock_client()
    existing = MagicMock(); existing.id = "nb-existing"; existing.title = "NUST_Counseling_DB"
    client.notebooks.list = AsyncMock(return_value=[existing])
    res = await ingest_university_sources("nust", "NUST", [{"url": "https://x", "tier": 1}], client=client)
    assert res.notebook_id == "nb-existing"
    client.notebooks.create.assert_not_called()


async def test_ingest_records_failed_uploads_without_aborting():
    client = _mock_client()
    calls = {"n": 0}

    async def flaky(notebook_id, url, **kw):
        calls["n"] += 1
        if "bad" in url:
            raise RuntimeError("upload rejected")
        src = MagicMock(); src.id = f"src-{calls['n']}"
        return src

    client.sources.add_url = AsyncMock(side_effect=flaky)
    links = [{"url": "https://ok-1", "tier": 1},
             {"url": "https://bad", "tier": 1},
             {"url": "https://ok-2", "tier": 2}]
    res = await ingest_university_sources("nust", "NUST", links, client=client)
    assert res.ingested_count == 2
    assert res.failed_urls == ["https://bad"]


async def test_ingest_respects_source_cap():
    client = _mock_client()
    links = [{"url": f"https://x/{i}", "tier": 1} for i in range(200)]
    res = await ingest_university_sources("nust", "NUST", links, client=client, max_sources=60)
    assert res.ingested_count == 60


async def test_ingest_rejects_missing_client():
    """NotebookLMClient() cannot be built without AuthTokens; fail loudly, not at runtime."""
    with pytest.raises(ValueError):
        await ingest_university_sources("nust", "NUST", [{"url": "https://x", "tier": 1}], client=None)


# =============================================================================
# Phase 3 extraction
# =============================================================================

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


# =============================================================================
# Rankings registry
# =============================================================================

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




def test_sub_campuses_contact_coercion():
    from src.schema import ContactInfo
    c = ContactInfo(sub_campuses_contact=[
        "Kenya Campus: 3rd Parklands (Tel: +254 20 366 2424)",
        "Tanzania Campus: Plot 34",
        {"campus_name": "Uganda Campus", "contact_details": "Plot 9/11"}
    ])
    assert len(c.sub_campuses_contact) == 3
    assert c.sub_campuses_contact[0].campus_name == "Kenya Campus"
    assert c.sub_campuses_contact[0].contact_details == "3rd Parklands (Tel: +254 20 366 2424)"
    assert c.sub_campuses_contact[1].campus_name == "Tanzania Campus"
    assert c.sub_campuses_contact[2].campus_name == "Uganda Campus"

