"""
Tests for the SQLite pipeline state machine.

Extracted from tests/test_pipeline.py in C6, which frees the root test_pipeline.py
name for the genuine end-to-end orchestration suite written in C27.
"""
import pytest

from src.utilities.state_management import (
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


