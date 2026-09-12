"""
Pre-flight link health sampling (plan section 6, C13).

The point of the feature is that a university whose links are mostly dead is
abandoned *before* a notebook is provisioned and before its query budget is
reserved. These tests pin that ordering, the sample arithmetic, and the
guarantee that one bad link in a healthy batch does not discard the good ones.
"""
import random

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.config import config
from src.ingestor.health_sampling import (
    HealthReport,
    resolve_sample_size,
    run_health_check,
    select_health_sample,
)
from src.ingestor.source_management import ingest_university_sources
from src.utilities.state_management import QuotaExceededError, StateManager


def _links(n, prefix="good"):
    return [{"url": f"https://uni.test/{prefix}-{i}", "tier": 1} for i in range(n)]


def _mock_client(notebook_id="nb-health-1"):
    client = MagicMock()
    nb = MagicMock()
    nb.id = notebook_id
    nb.title = "Test_Counseling_DB"
    client.notebooks.list = AsyncMock(return_value=[])
    client.notebooks.create = AsyncMock(return_value=nb)
    client.sources.add_url = AsyncMock(side_effect=lambda nid, url: f"src-{url[-3:]}")
    client.sources.add_text = AsyncMock(return_value="src-text")
    client.sources.wait_for_sources = AsyncMock(return_value=[])
    return client


# --------------------------------------------------------------- arithmetic --

def test_sample_size_is_the_ratio_floored_at_the_minimum():
    # 10% of 200 is 20, comfortably above the floor of 5.
    assert resolve_sample_size(200, ratio=0.10, minimum=5) == 20
    # 10% of 30 is 3, so the floor takes over.
    assert resolve_sample_size(30, ratio=0.10, minimum=5) == 5


def test_sample_size_never_exceeds_the_population():
    # A university with fewer links than the floor gets all of them probed,
    # rather than an impossible request for 5 out of 3.
    assert resolve_sample_size(3, ratio=0.10, minimum=5) == 3
    assert resolve_sample_size(1, ratio=0.10, minimum=5) == 1
    assert resolve_sample_size(0, ratio=0.10, minimum=5) == 0


def test_sample_is_drawn_from_the_whole_list_not_the_head():
    # Link lists arrive tier-ordered, so a head-of-list sample would only ever
    # probe the highest-scoring pages and clear a university with a dead tail.
    records = _links(100)
    sample = select_health_sample(records, ratio=0.10, minimum=5, rng=random.Random(7))
    assert len(sample) == 10
    positions = [records.index(rec) for rec in sample]
    assert max(positions) >= 10, "sample never reached past the head of the list"


# ------------------------------------------------------------------ verdicts --

@pytest.mark.asyncio
async def test_majority_failure_marks_the_batch_unhealthy():
    async def probe(url):
        return "good" in url

    records = _links(2, prefix="good") + _links(18, prefix="dead")
    report = await run_health_check(
        records, probe=probe, ratio=1.0, minimum=20, rng=random.Random(1)
    )

    assert report.healthy is False
    assert report.sample_size == 20
    assert report.pass_ratio == pytest.approx(0.1)
    assert len(report.failed) == 18


@pytest.mark.asyncio
async def test_majority_pass_marks_the_batch_healthy():
    async def probe(url):
        return "dead" not in url

    records = _links(18, prefix="good") + _links(2, prefix="dead")
    report = await run_health_check(
        records, probe=probe, ratio=1.0, minimum=20, rng=random.Random(1)
    )

    assert report.healthy is True
    assert report.pass_ratio == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_a_probe_that_raises_counts_as_one_dead_link_not_a_failed_check():
    async def probe(url):
        if "boom" in url:
            raise RuntimeError("connection reset")
        return True

    records = _links(9, prefix="good") + _links(1, prefix="boom")
    report = await run_health_check(
        records, probe=probe, ratio=1.0, minimum=10, rng=random.Random(1)
    )

    assert report.healthy is True
    assert report.failed == ["https://uni.test/boom-0"]


@pytest.mark.asyncio
async def test_exactly_meeting_the_threshold_passes():
    # min_pass_ratio is a floor, not a strict inequality: a 50/50 split at a
    # 0.5 threshold proceeds rather than being skipped.
    async def probe(url):
        return "good" in url

    report = await run_health_check(
        _links(5, "good") + _links(5, "dead"),
        probe=probe,
        ratio=1.0,
        minimum=10,
        min_pass_ratio=0.5,
        rng=random.Random(1),
    )
    assert report.pass_ratio == pytest.approx(0.5)
    assert report.healthy is True


@pytest.mark.asyncio
async def test_disabling_the_check_short_circuits_it(monkeypatch):
    calls = []

    async def probe(url):
        calls.append(url)
        return False

    monkeypatch.setattr(config, "health_check_enabled", False)
    report = await run_health_check(_links(50, "dead"), probe=probe)

    assert report.skipped_check is True
    assert report.healthy is True
    assert calls == [], "disabled check still spent network calls"


# ------------------------------------------------------- end-to-end ingestion --

@pytest.mark.asyncio
async def test_unhealthy_university_is_skipped_before_a_notebook_is_created(monkeypatch):
    async def all_dead(url, timeout=5.0):
        return False

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", all_dead)
    client = _mock_client()

    res = await ingest_university_sources(
        uni_slug="deaduni", uni_name="Dead Uni", links=_links(40), client=client
    )

    assert res.skipped is True
    assert "health check failed" in res.skip_reason
    assert res.notebook_id == ""
    assert res.ingested_count == 0
    # The whole point of the feature: nothing was provisioned and nothing was uploaded.
    client.notebooks.create.assert_not_called()
    client.sources.add_url.assert_not_called()


@pytest.mark.asyncio
async def test_healthy_university_proceeds_to_the_full_batch(monkeypatch):
    async def all_alive(url, timeout=5.0):
        return True

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", all_alive)
    client = _mock_client()

    res = await ingest_university_sources(
        uni_slug="liveuni", uni_name="Live Uni", links=_links(20), client=client
    )

    assert res.skipped is False
    assert res.notebook_id == "nb-health-1"
    assert res.ingested_count == 20
    assert res.health is not None and res.health.healthy is True


@pytest.mark.asyncio
async def test_sampled_links_are_not_probed_twice(monkeypatch):
    # The sample is drawn from the same list the full pre-flight pass walks, so
    # carrying the verdicts over is what keeps sampling free rather than a tax.
    probed = []

    async def counting_probe(url, timeout=5.0):
        probed.append(url)
        return True

    monkeypatch.setattr(
        "src.ingestor.source_management.check_url_accessible", counting_probe
    )
    client = _mock_client()

    await ingest_university_sources(
        uni_slug="liveuni", uni_name="Live Uni", links=_links(30), client=client
    )

    assert len(probed) == len(set(probed)) == 30


@pytest.mark.asyncio
async def test_one_mid_batch_upload_failure_does_not_discard_the_successes(monkeypatch):
    async def all_alive(url, timeout=5.0):
        return True

    async def no_text(url, timeout=8.0):
        return None, None

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", all_alive)
    monkeypatch.setattr("src.ingestor.source_management.fetch_and_extract_text", no_text)

    client = _mock_client()

    async def flaky_add_url(notebook_id, url):
        if url.endswith("good-3"):
            raise RuntimeError("RPCError rpc_code=9")
        return f"src-{url.rsplit('-', 1)[-1]}"

    client.sources.add_url = AsyncMock(side_effect=flaky_add_url)

    res = await ingest_university_sources(
        uni_slug="liveuni", uni_name="Live Uni", links=_links(10), client=client
    )

    assert res.skipped is False
    assert res.ingested_count == 9
    assert res.failed_urls == ["https://uni.test/good-3"]


@pytest.mark.asyncio
async def test_empty_link_set_is_a_skip_and_provisions_nothing():
    client = _mock_client()
    res = await ingest_university_sources(
        uni_slug="emptyuni", uni_name="Empty Uni", links=[], client=client
    )

    assert res.skipped is True
    assert res.notebook_id == ""
    client.notebooks.create.assert_not_called()


# --------------------------------------------------------- quota accounting --

def test_skipping_a_university_leaves_the_query_budget_untouched(tmp_path, monkeypatch):
    # A skip must not consume budget: the reservation happens in Phase 3, which
    # a skipped university never reaches. This pins the ledger side of that.
    monkeypatch.setattr(config, "state_db_path", tmp_path / "state.sqlite")
    mgr = StateManager()
    before = mgr.remaining_query_budget()

    mgr.reserve_queries("healthy-uni", config.queries_per_university)

    assert mgr.queries_used_today() == config.queries_per_university
    assert mgr.remaining_query_budget() == before - config.queries_per_university


def test_reservation_refuses_to_overrun_the_daily_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "state_db_path", tmp_path / "state.sqlite")
    monkeypatch.setattr(config, "daily_query_budget", 7)
    mgr = StateManager()

    mgr.reserve_queries("uni-a", 5)
    with pytest.raises(QuotaExceededError):
        mgr.reserve_queries("uni-b", 5)
    # The refused reservation is not partially applied.
    assert mgr.queries_used_today() == 5


# ------------------------------------------------------ the second chance --

@pytest.mark.asyncio
async def test_a_transient_failure_does_not_cost_the_whole_university():
    """
    The verdict discards an entire university, so one bad minute on the network
    must not decide it. NUST scored 0/8 on 2026-09-05 against a probe whose
    deadline was half the pooled client's, and never reached Phase 2.
    """
    records = [{"url": f"https://nust.edu.pk/{i}"} for i in range(8)]

    async def always_fails(url):
        return False

    async def patient(url):
        return True

    report = await run_health_check(
        records, probe=always_fails, recheck_probe=patient, label="nust"
    )

    assert report.healthy is True
    assert not report.failed
    assert report.pass_ratio == 1.0


@pytest.mark.asyncio
async def test_the_recheck_is_only_paid_for_when_the_verdict_is_already_lost():
    """A university that passes first time must not pay for a second pass."""
    records = [{"url": f"https://itu.edu.pk/{i}"} for i in range(8)]
    rechecked = []

    async def always_passes(url):
        return True

    async def patient(url):
        rechecked.append(url)
        return True

    report = await run_health_check(
        records, probe=always_passes, recheck_probe=patient
    )
    assert report.healthy is True
    assert rechecked == [], "a healthy sample was re-probed for nothing"


@pytest.mark.asyncio
async def test_a_genuinely_dead_site_is_still_refused():
    """The second chance promotes failures to passes; it never invents one."""
    records = [{"url": f"https://gone.edu.pk/{i}"} for i in range(8)]

    async def dead(url):
        return False

    report = await run_health_check(records, probe=dead, recheck_probe=dead)
    assert report.healthy is False
    assert len(report.failed) == report.sample_size
    assert not report.passed


@pytest.mark.asyncio
async def test_the_recheck_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "health_check_recheck_failures", False)
    records = [{"url": f"https://x/{i}"} for i in range(8)]
    rechecked = []

    async def dead(url):
        return False

    async def patient(url):
        rechecked.append(url)
        return True

    report = await run_health_check(records, probe=dead, recheck_probe=patient)
    assert report.healthy is False
    assert rechecked == []

