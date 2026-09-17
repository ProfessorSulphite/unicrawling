"""
Unit tests for Gemini Notebook 5-Hour Rolling Usage & Reusable Worker Workspace (tests/test_notebooklm_quota.py)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.config import config
from src.utilities.state_management import StateManager, QuotaExceededError
from src.extractor.crawlers.notebook_querying import is_rate_limit_exception
from src.extractor.crawlers.runner import delete_notebook_after_success
from src.ingestor.notebook_lifecycle import (
    purge_notebook_sources,
    _find_or_create_notebook as create_notebook,
)


@pytest.fixture
def temp_state(tmp_path):
    db_path = tmp_path / "state_test.sqlite"
    sm = StateManager(db_path=db_path)
    yield sm
    sm.close()


def test_sliding_5_hour_window_tracking(temp_state, monkeypatch):
    """Verifies that queries_used_in_window accurately aggregates within rolling window."""
    monkeypatch.setattr(config, "budget_5_hours", 20)
    monkeypatch.setattr(config, "daily_query_budget", 100)

    # Reserve 10 queries
    temp_state.reserve_queries("nust", 10)
    assert temp_state.queries_used_in_window(5.0) == 10
    assert temp_state.remaining_5h_budget() == 10

    # Reserve another 8 queries
    temp_state.reserve_queries("itu", 8)
    assert temp_state.queries_used_in_window(5.0) == 18
    assert temp_state.remaining_5h_budget() == 2

    # Attempting to reserve 5 more queries should trip 5-hour quota
    with pytest.raises(QuotaExceededError) as exc_info:
        temp_state.reserve_queries("lums", 5)
    assert "5-Hour Rolling NotebookLM budget exhausted" in str(exc_info.value)


def test_rate_limit_exception_detection():
    """Verifies regex and exception class detection for Gemini 5-hour usage messages."""
    class MockRateLimit(Exception):
        retry_after = 120

    is_limit, wait, msg = is_rate_limit_exception(MockRateLimit("Usage limit reached. Resets at 3:00 PM"))
    assert is_limit is True
    assert "3:00 PM" in msg

    # Text message with retry-after
    err = RuntimeError("HTTP 429: Too Many Requests. retry-after: 45")
    is_limit, wait, _ = is_rate_limit_exception(err)
    assert is_limit is True
    assert wait == 45

    # Regular error
    not_limit, _, _ = is_rate_limit_exception(ValueError("JSON decode failed"))
    assert not_limit is False


async def test_purge_notebook_sources():
    """Verifies that purge_notebook_sources iterates through sources and deletes each."""
    client = MagicMock()
    s1 = MagicMock(id="src-1")
    s2 = MagicMock(id="src-2")
    client.sources.list = AsyncMock(return_value=[s1, s2])
    client.sources.delete = AsyncMock()

    deleted = await purge_notebook_sources(client, "nb-worker")
    assert deleted == 2
    assert client.sources.delete.call_count == 2
    client.sources.delete.assert_any_call("nb-worker", "src-1")
    client.sources.delete.assert_any_call("nb-worker", "src-2")


async def test_reusable_mode_preserves_notebook(monkeypatch):
    """In reusable mode, delete_notebook_after_success purges sources and preserves notebook."""
    monkeypatch.setattr(config, "notebook_mode", "reusable")
    client = MagicMock()
    client.notebooks.delete = AsyncMock()
    client.sources.list = AsyncMock(return_value=[])

    res = await delete_notebook_after_success(client, "nb-worker", "nust")
    assert res is True
    # client.notebooks.delete should NOT be called in reusable mode!
    client.notebooks.delete.assert_not_called()


async def test_ephemeral_mode_deletes_notebook(monkeypatch):
    """In ephemeral mode, delete_notebook_after_success deletes the notebook."""
    monkeypatch.setattr(config, "notebook_mode", "ephemeral")
    client = MagicMock()
    client.notebooks.delete = AsyncMock()

    res = await delete_notebook_after_success(client, "nb-ephemeral", "nust")
    assert res is True
    client.notebooks.delete.assert_called_once_with("nb-ephemeral")


async def test_ingest_purges_stale_sources_on_reusable_notebook(monkeypatch):
    """When resuming an interrupted run in reusable mode, leftover sources are purged before new uploads."""
    from src.ingestor import ingest_university_sources
    monkeypatch.setattr(config, "notebook_mode", "reusable")
    monkeypatch.setattr(config, "preflight_http_check", False)
    monkeypatch.setattr(config, "health_check_enabled", False)

    client = MagicMock()
    # Mock existing worker notebook
    nb_mock = MagicMock(id="nb-worker", title="Education_Counselor_Worker_DB")
    client.notebooks.list = AsyncMock(return_value=[nb_mock])

    # Leftover sources from an interrupted run
    s1 = MagicMock(id="old-src-1")
    s2 = MagicMock(id="old-src-2")
    # First call returns old sources (before purge), subsequent calls return empty
    client.sources.list = AsyncMock(side_effect=[[s1, s2], []])
    client.sources.delete = AsyncMock()

    # Upload mock
    new_src = MagicMock(id="new-src-1")
    client.sources.add_url = AsyncMock(return_value=new_src)
    client.sources.wait_until_ready = AsyncMock()

    links = [{"url": "https://test.edu.pk/programs", "tier": 1}]
    result = await ingest_university_sources("test_uni", "Test Uni", links, client)

    # Verify old sources were deleted!
    assert client.sources.delete.call_count == 2
    client.sources.delete.assert_any_call("nb-worker", "old-src-1")
    client.sources.delete.assert_any_call("nb-worker", "old-src-2")
    assert result.notebook_id == "nb-worker"


async def test_ingest_enforces_300_source_ceiling(monkeypatch):
    """Candidate links are strictly clamped to at most 300 sources."""
    from src.ingestor import ingest_university_sources
    monkeypatch.setattr(config, "notebook_mode", "reusable")
    monkeypatch.setattr(config, "preflight_http_check", False)
    monkeypatch.setattr(config, "health_check_enabled", False)
    monkeypatch.setattr(config, "notebooklm_max_sources_limit", 300)

    client = MagicMock()
    nb_mock = MagicMock(id="nb-worker", title="Education_Counselor_Worker_DB")
    client.notebooks.list = AsyncMock(return_value=[nb_mock])
    client.sources.list = AsyncMock(return_value=[])
    client.sources.delete = AsyncMock()

    uploaded_ids = []
    async def _mock_add_url(nb_id, url, **kwargs):
        sid = f"src-{len(uploaded_ids)}"
        uploaded_ids.append(sid)
        return MagicMock(id=sid)

    client.sources.add_url = AsyncMock(side_effect=_mock_add_url)
    client.sources.wait_until_ready = AsyncMock()

    # Supply 350 links
    many_links = [{"url": f"https://test.edu.pk/page_{i}", "tier": 1} for i in range(350)]
    result = await ingest_university_sources("test_uni", "Test Uni", many_links, client, max_sources=350)

    # Total uploaded sources must NEVER exceed 300!
    assert len(result.sources) == 300
    assert len(uploaded_ids) == 300


def test_seconds_until_5h_budget_available(temp_state, monkeypatch):
    """seconds_until_5h_budget_available returns 0 when budget is available and positive when full."""
    monkeypatch.setattr(config, "budget_5_hours", 10)
    assert temp_state.seconds_until_5h_budget_available(5) == 0.0

    # Exhaust budget
    temp_state.reserve_queries("uni-a", 10)
    wait_time = temp_state.seconds_until_5h_budget_available(5)
    # Since queries were just logged, wait time should be positive (close to 5 hours)
    assert wait_time > 0.0



# ------------------------------------- the 5-hour window refund (C32) --------
#
# `release_queries` credited the mutable `query_ledger` but never the
# append-only `query_events`, which backs the rolling window -- and the rolling
# window (75) is the TIGHTER of the two ceilings, so the drift accumulated
# exactly where it limits throughput.

def test_an_unspent_reservation_is_refunded_to_the_5_hour_window(temp_state, monkeypatch):
    """
    A university that reserves 6 and spends 2 must owe 2, not 6.

    Before C32 it kept all 6 charged against the rolling window for five hours.
    At 75 per window that is a whole university's worth of quota lost every time
    an extraction ended early -- which is every failure, every skip and every
    roster smaller than the reservation assumed.
    """
    monkeypatch.setattr(config, "budget_5_hours", 75)

    temp_state.reserve_queries("itu", 6)
    assert temp_state.queries_used_in_window() == 6
    assert temp_state.queries_used_today() == 6

    temp_state.release_queries("itu", 4)

    assert temp_state.queries_used_in_window() == 2, "the 5-hour window kept the refund"
    assert temp_state.queries_used_today() == 2
    assert temp_state.remaining_5h_budget() == 73


def test_the_refund_is_a_compensating_row_not_a_deletion(temp_state):
    """
    Append-only is the point: the reservation and its refund both stay visible,
    and the negative row expires on the same schedule as the charge it reverses.
    Deleting or mutating the original would get that timing wrong.
    """
    temp_state.reserve_queries("itu", 6)
    temp_state.release_queries("itu", 4)

    with temp_state._get_connection() as conn:
        rows = conn.execute(
            "SELECT queries FROM query_events WHERE university_slug = 'itu' ORDER BY id"
        ).fetchall()

    assert [int(r["queries"]) for r in rows] == [6, -4]


def test_the_window_can_never_read_as_negative(temp_state):
    """
    The charge and the refund expire independently, so a refund can outlive its
    charge inside the window. A negative "used" count would report MORE budget
    than the quota holds and turn an accounting artifact into a real over-spend.
    """
    temp_state.release_queries("itu", 5)     # refund with no charge in range
    assert temp_state.queries_used_in_window() == 0
    assert temp_state.remaining_5h_budget() == config.budget_5_hours


def test_a_refund_does_not_predict_capacity_that_never_arrives(temp_state, monkeypatch):
    """
    Only a real charge frees capacity when it ages out of the window. Counting a
    negative row as "about to expire" would have the caller wait for capacity
    that expiry actually removes.
    """
    monkeypatch.setattr(config, "budget_5_hours", 10)
    temp_state.reserve_queries("itu", 10)
    temp_state.release_queries("itu", 2)

    assert temp_state.remaining_5h_budget() == 2
    # Two are already free, so nothing has to be waited for.
    assert temp_state.seconds_until_5h_budget_available(required=2) == 0.0
    # More than that means waiting for the real charge to age out.
    assert temp_state.seconds_until_5h_budget_available(required=9) > 0


# ------------------------------------------- the staged plan's budget (C32) --

def test_the_base_reservation_is_smaller_than_the_ceiling():
    """
    The fixed stages are reserved up front; the rest is reserved once the roster
    says how many detail asks are needed. Reserving the worst case up front
    would hold quota sized for a 200-programme university on every 17-programme
    one.
    """
    assert config.base_queries_per_university < config.max_queries_per_university
    assert config.base_queries_per_university == 3      # identity + faculties + roster


def test_the_legacy_suite_still_matches_its_own_reservation():
    """`queries_per_university` now governs only the JSON path, and must still fit it."""
    from src.extractor.crawlers.notebook_querying import QUERY_SUITE
    assert len(QUERY_SUITE) == config.queries_per_university
