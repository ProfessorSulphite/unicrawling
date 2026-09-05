"""
Tests for the SQLite pipeline state machine.

Extracted from tests/test_pipeline.py in C6, which frees the root test_pipeline.py
name for the genuine end-to-end orchestration suite written in C27.
"""
import pytest

from src.utilities.state_management import (
    PARTIAL_EXTRACTION,
    InvalidStatusError,
    QuotaExceededError,
    StateManager,
)


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
    import src.utilities.state_management as state_mod
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




# ------------------------------------------------ C22: tier-scoped sources --
#
# source_ids_by_tier moved off pipeline.py onto StateManager in C22. Phase 3
# scopes each query to the sources that can answer it, so getting this wrong
# either widens every query to every source or narrows it to none -- and both
# failures look like "the extraction was a bit worse today".

def test_source_ids_by_tier_groups_by_tier(state):
    state.record_sources("itu", [
        ("s1", "https://itu.edu.pk/admissions", 1),
        ("s2", "https://itu.edu.pk/apply", 1),
        ("s3", "https://itu.edu.pk/programs", 2),
        ("s4", "https://itu.edu.pk/fees", 3),
    ])
    assert state.source_ids_by_tier("itu") == {1: ["s1", "s2"], 2: ["s3"], 3: ["s4"]}


def test_an_empty_tier_is_absent_rather_than_empty(state):
    """A tier mapped to [] would scope that query to no sources at all."""
    state.record_sources("itu", [("s1", "https://itu.edu.pk/a", 2)])
    mapping = state.source_ids_by_tier("itu")
    assert mapping == {2: ["s1"]}
    assert 1 not in mapping and 3 not in mapping


def test_no_recorded_sources_returns_none_not_an_empty_dict(state):
    """
    The query suite reads None as "search every source" and {} as "search
    nothing". A university whose source map was never written must fall back to
    searching everything, not come back empty.
    """
    assert state.source_ids_by_tier("never-ingested") is None


def test_it_does_not_leak_another_universitys_sources(state):
    state.record_sources("itu", [("s1", "https://itu.edu.pk/a", 1)])
    state.record_sources("lums", [("s2", "https://lums.edu.pk/a", 1)])
    assert state.source_ids_by_tier("itu") == {1: ["s1"]}
    assert state.source_ids_by_tier("lums") == {1: ["s2"]}


def test_tier_4_is_included(state):
    """The loop is hardcoded to tiers 1-4; a tier 4 source must not be dropped."""
    state.record_sources("itu", [("s1", "https://itu.edu.pk/news", 4)])
    assert state.source_ids_by_tier("itu") == {4: ["s1"]}


# =============================================================================
# A partial extraction is work outstanding, not a completion
# =============================================================================

def test_partial_is_a_valid_status_but_not_a_completion(state):
    """
    Writing "completed" for a university whose bachelors query failed meant the
    next batch skipped it forever: one transient API failure permanently cost a
    degree level. 'partial' keeps the payload and keeps the work queued.
    """
    state.set_status("itu", PARTIAL_EXTRACTION, error_log="bachelors failed after retries")

    assert state.get_status("itu") == "partial"
    assert "itu" not in state.get_completed_slugs()
    assert state.get_partial_slugs() == ["itu"]


def test_a_partial_status_keeps_its_error_log_across_an_untouched_update(state):
    """The note survives the way a failure's does; it is the only record of why."""
    state.set_status("itu", PARTIAL_EXTRACTION, error_log="bachelors failed after retries")
    state.set_status("itu", PARTIAL_EXTRACTION, queries_executed=9)

    row = state.get_state("itu")
    assert row["error_log"] == "bachelors failed after retries"
    assert row["queries_executed"] == 9


def test_advancing_a_partial_to_completed_clears_the_note(state):
    """A retry that answered every block must not leave a stale warning behind."""
    state.set_status("itu", PARTIAL_EXTRACTION, error_log="bachelors failed after retries")
    state.set_status("itu", "completed")

    assert state.get_state("itu")["error_log"] is None
    assert state.get_completed_slugs() == ["itu"]
