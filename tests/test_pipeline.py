"""
Production test suite for the Education Counselor RAG pipeline.

Every test here is a regression test for a defect that was actually present in the
shipped code, not a smoke test. Each one is annotated with the failure it locks out.
"""
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import List

import pytest
from unittest.mock import AsyncMock, MagicMock

from src import extract_links as P1
from src.config import Config
from src.schema import (
    UniversityPayload, MainInfo, KeyLinks, ContactInfo, ProgramItem,
    FacultyItem, DegreeLevel, ApplicationStatus, UniversityType,
)
from src.extract_data import (
    repair_and_validate_json, extract_json_str, strip_citation_markers,
    ExtractionError, QUERY_SUITE, run_query, ExtractionReport,
    extract_university_payload, apply_registry_facts, lookup_registry,
)
from src.ingest import ingest_university_sources, IngestResult
from src.state import StateManager, InvalidStatusError, QuotaExceededError


# =============================================================================
# B5.1 -- Tokenized path exclusion
# =============================================================================

@pytest.mark.parametrize("url", [
    "https://nust.edu.pk/programs/bs-business-administration",
    "https://nust.edu.pk/programs/mba-executive",
    "https://lums.edu.pk/programs/bba",
    "https://nust.edu.pk/programs/bs-accounting-and-auditing",
    "https://nust.edu.pk/admissions/apply-online-portal",
    "https://portal.nust.edu.pk/apply",
    "https://nust.edu.pk/admissions/undergraduate-admission-portal",
    "https://nust.edu.pk/newsletter-for-prospective-students",
])
def test_tokenized_exclusion_keeps_academic_urls(url):
    """
    Substring matching deleted the pipeline's own deliverables:
    'admin' killed every business-administration/MBA URL, 'audit' killed auditing
    programmes, and 'portal' killed application_portal_url -- the field the data
    model marks PRIMARY FOCUS. A NUST crawl returned 0 hits for
    'administration|auditing' and 0 for 'portal' as a direct result.
    """
    assert P1.is_excluded_path(url) is False, f"{url} must survive filtering"


@pytest.mark.parametrize("url", [
    "https://nust.edu.pk/wp-admin/edit.php",
    "https://nust.edu.pk/wp-login.php",
    "https://nust.edu.pk/news/2026/spring-convocation",
    "https://nust.edu.pk/events/",
    "https://nust.edu.pk/admin/",
    "https://lms.nust.edu.pk/course/view",
    "https://nust.edu.pk/student-portal/login",
    "https://nust.edu.pk/careers/vacancies",
    "https://nust.edu.pk/tenders/procurement-notice",
    "https://nust.edu.pk/privacy-policy",
])
def test_tokenized_exclusion_still_removes_noise(url):
    """Tokenisation must not weaken the Zero Garbage Policy it replaces."""
    assert P1.is_excluded_path(url) is True, f"{url} must be excluded"


def test_exclude_keywords_are_token_matched_not_substring():
    """
    --exclude-keywords "news" must remove /news/ but not words that merely contain
    'news' as a substring. ('newsletter-signup' is deliberately NOT used as the
    control here -- it tokenises to {newsletter, signup} and is correctly dropped
    by the signup rule, which would make this assertion prove nothing.)
    """
    links = [
        {"href": "https://x.edu.pk/news/item-1", "text": "News"},
        {"href": "https://x.edu.pk/newsletter-for-prospective-students", "text": "Newsletter"},
        {"href": "https://x.edu.pk/events/gala", "text": "Events"},
        {"href": "https://x.edu.pk/programs/bs-cs", "text": "BS CS"},
    ]
    out = P1.preprocess_and_filter_links(links, base_url="https://x.edu.pk",
                                         exclude_keywords="news|events")
    urls = [l["href"] for l in out]
    assert not any("/news/" in u for u in urls)
    assert not any("/events/" in u for u in urls)
    assert any("newsletter" in u for u in urls)
    assert any("bs-cs" in u for u in urls)


# =============================================================================
# B5.2 -- URL sanitisation
# =============================================================================

def test_url_sanitization_unescapes_html_entities():
    """'&amp;' appeared verbatim in committed output, producing unfetchable URLs."""
    got = P1.normalize_url("https://nust.edu.pk/x?p=959&amp;post_type=scholarship")
    assert "&amp;" not in got
    assert "post_type=scholarship" in got


def test_url_sanitization_strips_zero_width_characters():
    """A U+200B inside an MBBS slug made that source unfetchable by NotebookLM."""
    got = P1.normalize_url("https://nust.edu.pk/mbbs-​bachelor-of-medicine")
    assert "​" not in got
    assert got.endswith("/mbbs-bachelor-of-medicine")


def test_url_sanitization_collapses_double_slashes():
    got = P1.normalize_url("https://sines.nust.edu.pk//program//bs-cs/")
    assert got == "https://sines.nust.edu.pk/program/bs-cs"


def test_url_sanitization_removes_tracking_params_and_fragment():
    got = P1.normalize_url("https://nust.edu.pk/apply?utm_source=fb&fbclid=abc&id=7#section")
    assert "utm_source" not in got and "fbclid" not in got and "#" not in got
    assert "id=7" in got


def test_url_sanitization_rejects_unfetchable_schemes():
    for bad in ["mailto:x@y.pk", "tel:+92515", "javascript:void(0)"]:
        assert P1.normalize_url(bad) is None


def test_dedupe_key_unifies_www_and_scheme_variants():
    """Otherwise one page consumes two of the 60 per-notebook source slots."""
    a = P1.dedupe_key(P1.normalize_url("https://www.nust.edu.pk/apply"))
    b = P1.dedupe_key(P1.normalize_url("http://nust.edu.pk/apply"))
    assert a == b


# =============================================================================
# B5.3 -- Dynamic year decay
# =============================================================================

def test_year_decay_boosts_current_and_future():
    assert P1.compute_year_decay_factor("admissions-2026", now_year=2026)[0] > 1.0
    assert P1.compute_year_decay_factor("admissions-2027", now_year=2026)[0] > 1.0


def test_year_decay_penalises_past_years_monotonically():
    """
    The old regexes classified 2024 as 'current' (202[4-7]) while the 'outdated'
    window stopped at 2023, so a fall-2024 link scored 1.15x and ranked #1 of 287
    during a 2026 run. Decay must now be strictly decreasing into the past.
    """
    f2025 = P1.compute_year_decay_factor("intake-2025", now_year=2026)[0]
    f2024 = P1.compute_year_decay_factor("intake-2024", now_year=2026)[0]
    f2023 = P1.compute_year_decay_factor("intake-2023", now_year=2026)[0]
    assert 1.0 > f2025 > f2024 > f2023
    assert f2023 >= 0.25


def test_year_decay_ranks_2024_below_2026():
    """The exact inversion observed in the committed detailed report."""
    old = P1.compute_year_decay_factor("bs-software-engineering-for-fall-2024", now_year=2026)[0]
    new = P1.compute_year_decay_factor("bs-software-engineering-for-fall-2026", now_year=2026)[0]
    assert new > old


def test_year_decay_uses_latest_year_in_range():
    assert P1.compute_year_decay_factor("session-2025-2026", now_year=2026)[0] > 1.0


def test_year_decay_treats_onward_ranges_as_current():
    """'fall-2025-onward' names the policy in force, not a historical intake."""
    assert P1.compute_year_decay_factor("fall-2025-onward", now_year=2026)[0] > 1.0


def test_year_decay_bounds_the_onward_rescue():
    """
    A live NUST crawl surfaced 'for-fall-2023-onwards' and a 2022 variant. Granting
    those the full current-year boost would rank a four-year-old scheme above this
    year's, so the rescue is bounded: neutral beyond the grace window, never boosted.
    """
    stale = P1.compute_year_decay_factor("bpa-for-fall-2022-onwards", now_year=2026)[0]
    fresh = P1.compute_year_decay_factor("bba-for-2025-onwards", now_year=2026)[0]
    assert fresh > 1.0
    assert stale == 1.0
    # Still better than a bare historical year, which is what it actually is.
    assert stale > P1.compute_year_decay_factor("bpa-for-fall-2022", now_year=2026)[0]


def test_semantic_threshold_is_calibrated_for_prefixed_bge():
    """
    The inherited 0.45 was a dead knob: with the BGE query prefix and L2-normalised
    embeddings, a live 473-link crawl scored min 0.639, so every link cleared it and
    the threshold filtered nothing.
    """
    from src.config import Config
    assert Config().semantic_threshold > 0.60


def test_year_decay_neutral_without_years():
    assert P1.compute_year_decay_factor("programs/bs-computer-science")[0] == 1.0


def test_year_decay_is_not_hardcoded_to_2026():
    """Regression against re-introducing a fixed year window."""
    assert P1.compute_year_decay_factor("intake-2030", now_year=2030)[0] > 1.0
    assert P1.compute_year_decay_factor("intake-2026", now_year=2030)[0] < 1.0


# =============================================================================
# B5.4 -- Structured discipline-token deduplication
# =============================================================================

def test_electrical_and_electronic_engineering_stay_separate():
    """
    SequenceMatcher at ratio > 0.88 merged these two distinct degrees
    (they score ~0.90 on slug similarity), silently deleting one programme.
    """
    a = P1.get_discipline_tokens("https://nust.edu.pk/bs-electrical-engineering")
    b = P1.get_discipline_tokens("https://nust.edu.pk/bs-electronic-engineering")
    assert a != b


def test_multi_campus_mirrors_merge():
    """Same programme on different campus subdomains is one source, not three."""
    keys = {
        P1.get_discipline_tokens("https://seecs.nust.edu.pk/programs/bs-computer-science"),
        P1.get_discipline_tokens("https://mcs.nust.edu.pk/programs/bs-computer-science"),
        P1.get_discipline_tokens("https://ceme.nust.edu.pk/programs/bs-computer-science"),
    }
    assert len(keys) == 1


def test_intake_year_variants_merge():
    a = P1.get_discipline_tokens("https://nust.edu.pk/programs/bs-cs-for-fall-2024")
    b = P1.get_discipline_tokens("https://nust.edu.pk/programs/bs-cs-fall-2025-onward")
    assert a == b


def test_same_discipline_different_level_stays_separate():
    ug = P1.get_discipline_tokens("https://nust.edu.pk/bs-computer-science")
    gr = P1.get_discipline_tokens("https://nust.edu.pk/ms-computer-science")
    assert ug != gr


def test_path_prefix_does_not_split_identical_programmes():
    """Keying on the last path segment alone split /programs/bs-cs from /admissions/bs-cs."""
    a = P1.get_discipline_tokens("https://nust.edu.pk/programs/bs-computer-science")
    b = P1.get_discipline_tokens("https://nust.edu.pk/admissions/bs-computer-science")
    assert a == b


def test_deduplicate_keeps_highest_scoring_variant():
    items = [
        {"href": "https://a.nust.edu.pk/bs-cs-fall-2024", "text": "BS CS",
         "priority_tier_num": 1, "weighted_score": 0.60, "category": "Tier 1"},
        {"href": "https://b.nust.edu.pk/bs-cs-fall-2026", "text": "BS CS",
         "priority_tier_num": 1, "weighted_score": 0.95, "category": "Tier 1"},
    ]
    out = P1.deduplicate_canonical_degree_links(items)
    assert len(out) == 1
    assert "2026" in out[0]["href"]


# =============================================================================
# Proportional tier quotas
# =============================================================================

def _mk(tier, score):
    return {"priority_tier_num": tier, "weighted_score": score,
            "href": f"https://x/{tier}-{score}", "category": f"Tier {tier}"}


def test_tier_quotas_guarantee_faculty_and_contact_sources():
    """
    scored[:max_links] after a tier-major sort let Tier 1 consume the whole budget,
    so the faculties (Q5) and contact (Q1) queries ran against a notebook that
    contained no faculty or contact pages at all.
    """
    links = ([_mk(1, 0.9 - i * 0.001) for i in range(200)]
             + [_mk(2, 0.8 - i * 0.001) for i in range(100)]
             + [_mk(3, 0.7 - i * 0.001) for i in range(50)]
             + [_mk(4, 0.6 - i * 0.001) for i in range(50)])
    selected = P1.allocate_proportional_tier_quotas(links, total_cap=60)
    tiers = [i["priority_tier_num"] for i in selected]
    assert len(selected) == 60
    for t in (1, 2, 3, 4):
        assert tiers.count(t) > 0, f"tier {t} was starved"
    assert tiers.count(1) == 27 and tiers.count(2) == 18
    assert tiers.count(3) == 9 and tiers.count(4) == 6


def test_tier_quotas_redistribute_unfilled_budget():
    """A small site must still fill its budget rather than under-ingesting."""
    links = [_mk(1, 0.9 - i * 0.001) for i in range(100)] + [_mk(3, 0.5)]
    selected = P1.allocate_proportional_tier_quotas(links, total_cap=60)
    assert len(selected) == 60


def test_tier_reserve_prevents_threshold_starving_a_tier():
    """
    A global threshold tuned for the dense programme tier starved the sparse
    faculties tier on a live NUST run (4 sources against a quota of 9), degrading
    the faculties query. A tier fills its quota from its own sub-threshold reserve
    before any cross-tier redistribution.
    """
    links = [dict(_mk(1, 0.9 - i * 0.001), passed_threshold=True) for i in range(200)]
    links += [dict(_mk(2, 0.85 - i * 0.001), passed_threshold=True) for i in range(60)]
    links += [dict(_mk(3, 0.80), passed_threshold=True) for _ in range(4)]
    links += [dict(_mk(3, 0.62 - i * 0.001), passed_threshold=False) for i in range(20)]
    links += [dict(_mk(4, 0.75), passed_threshold=True) for _ in range(10)]

    selected = P1.allocate_proportional_tier_quotas(links, total_cap=60)
    tiers = [i["priority_tier_num"] for i in selected]
    assert tiers.count(3) == 9, "tier 3 must backfill from its own reserve"
    # The 4 above-threshold tier-3 links must all be chosen ahead of the reserve.
    t3 = [i for i in selected if i["priority_tier_num"] == 3]
    assert sum(1 for i in t3 if i["passed_threshold"]) == 4


def test_tier_quotas_prefer_passing_links_over_reserve():
    links = [dict(_mk(1, 0.70), passed_threshold=False) for _ in range(50)]
    links += [dict(_mk(1, 0.69), passed_threshold=True) for _ in range(50)]
    selected = P1.allocate_proportional_tier_quotas(links, total_cap=20)
    assert all(i["passed_threshold"] for i in selected), \
        "an above-threshold link must outrank a higher-scoring reserve link"


def test_tier_quotas_handle_undersized_input():
    selected = P1.allocate_proportional_tier_quotas([_mk(1, 0.9), _mk(2, 0.8)], total_cap=60)
    assert len(selected) == 2


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
# B5.6 -- SQLite state machine
# =============================================================================

@pytest.fixture
def state(tmp_path):
    return StateManager(db_path=tmp_path / "state.sqlite")


def test_state_machine_transitions(state):
    for status in ("pending", "crawled", "ingested", "extracted", "completed"):
        state.set_status("nust", status)
        assert state.get_status("nust") == status
    assert state.is_complete("nust")


def test_state_machine_rejects_invalid_status(state):
    """VALID_STATUSES was declared and never enforced, so typos became unresumable rows."""
    with pytest.raises(InvalidStatusError):
        state.set_status("nust", "ingesting")


def test_state_machine_preserves_unspecified_fields(state):
    state.set_status("nust", "ingested", notebook_id="nb-1", sources_ingested=60)
    state.set_status("nust", "extracted", queries_executed=5)
    row = state.get_state("nust")
    assert row["notebook_id"] == "nb-1"
    assert row["sources_ingested"] == 60
    assert row["queries_executed"] == 5


def test_state_machine_clears_error_on_recovery(state):
    state.set_status("nust", "failed", error_log="boom")
    state.set_status("nust", "crawled")
    assert state.get_state("nust")["error_log"] is None


def test_source_map_round_trip_preserves_tiers(state):
    """
    Phase 2 previously returned only a count, destroying the url->source_id->tier
    mapping, so Phase 3 could not scope any query with source_ids.
    """
    state.record_sources("nust", [
        ("s1", "https://nust.edu.pk/bs-cs", 1),
        ("s2", "https://nust.edu.pk/fees", 2),
        ("s3", "https://nust.edu.pk/faculties", 3),
        ("s4", "https://nust.edu.pk/contact", 4),
    ])
    assert set(state.get_source_ids("nust", tiers=[1, 2])) == {"s1", "s2"}
    assert state.get_source_ids("nust", tiers=[3]) == ["s3"]
    assert len(state.get_source_ids("nust")) == 4
    assert state.count_sources_by_tier("nust") == {1: 1, 2: 1, 3: 1, 4: 1}


def test_source_map_is_idempotent(state):
    for _ in range(3):
        state.record_sources("nust", [("s1", "https://x", 1)])
    assert len(state.get_source_ids("nust")) == 1


def test_query_ledger_enforces_daily_budget(state, monkeypatch):
    """
    6 queries x 83 universities = 498 of 500, leaving no retry margin. The ledger
    must refuse the overrun instead of the API failing mid-run.
    """
    import src.state as state_mod
    monkeypatch.setattr(state_mod.config, "daily_query_budget", 10)
    state.reserve_queries("a", 5)
    state.reserve_queries("b", 5)
    assert state.queries_used_today() == 10
    assert state.remaining_query_budget() == 0
    with pytest.raises(QuotaExceededError):
        state.reserve_queries("c", 1)


def test_reset_state_clears_source_map(state):
    state.set_status("nust", "ingested")
    state.record_sources("nust", [("s1", "https://x", 1)])
    state.reset_state("nust")
    assert state.get_status("nust") is None
    assert state.get_source_ids("nust") == []


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


# =============================================================================
# Schema contract
# =============================================================================

def test_payload_serialises_all_four_blocks():
    payload = UniversityPayload(
        main_info=MainInfo(name="NUST", website="https://nust.edu.pk",
                           description="d", key_links=KeyLinks()),
        programs={"undergraduate": [], "graduate": [], "postgraduate_and_phd": []},
        faculties=[FacultyItem(faculty_name="SEECS")],
        contact=ContactInfo(),
    )
    dumped = payload.model_dump(mode="json")
    assert set(dumped) >= {"main_info", "programs", "faculties", "contact",
                           "programs_possibly_truncated"}
    assert json.loads(json.dumps(dumped))  # JSONL-serialisable


def test_application_portal_url_is_a_first_class_field():
    payload = UniversityPayload(
        main_info=MainInfo(name="NUST", website="https://nust.edu.pk", description="d",
                           key_links=KeyLinks(application_portal_url="https://portal.nust.edu.pk")),
        programs={}, contact=ContactInfo(),
    )
    assert payload.model_dump()["main_info"]["key_links"]["application_portal_url"]


# =============================================================================
# Phase 1 <-> Phase 2 file contract
# =============================================================================

def test_partitioned_export_round_trip(tmp_path, monkeypatch):
    """
    HEC batch mode wrote one undifferentiated extracted_links.txt (287 links:
    ~284 NUST, ~10 LUMS, 0 ITU) with no record of which university a URL belonged
    to, making Phase 2's per-university contract unsatisfiable.
    """
    monkeypatch.setattr(P1.config, "data_links_dir", tmp_path)
    items = [
        {"href": "https://nust.edu.pk/bs-cs", "text": "BS CS", "priority_tier_num": 1,
         "category": "Tier 1", "weighted_score": 0.9, "raw_similarity_score": 0.8,
         "matched_keyword": "k", "year_tag": "t"},
        {"href": "https://nust.edu.pk/contact", "text": "Contact", "priority_tier_num": 4,
         "category": "Tier 4", "weighted_score": 0.5, "raw_similarity_score": 0.5,
         "matched_keyword": "k", "year_tag": "t"},
    ]
    P1.export_partitioned_links(items, "nust", "NUST", "https://nust.edu.pk")
    loaded = P1.load_partitioned_links("nust")
    assert len(loaded) == 2
    assert {r["tier"] for r in loaded} == {1, 4}
    assert all(r["university_slug"] == "nust" for r in loaded)
    assert loaded[0]["url"] == "https://nust.edu.pk/bs-cs"


def test_slugify_is_stable_across_url_forms():
    assert (P1.slugify_university("NUST", "https://www.nust.edu.pk/")
            == P1.slugify_university("NUST", "http://nust.edu.pk"))


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

