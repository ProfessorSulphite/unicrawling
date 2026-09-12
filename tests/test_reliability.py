"""
The failures of run c_1 (2026-09-05), each pinned by a test.

That run reached 9 of 21 universities in 11 hours. Every test in this file is a
regression test for something the run log, the state DB or the notebook audit
proved had actually gone wrong -- not for a hypothesis about what might.

    a hung chat.ask               -> test_a_hung_ask_...
    no per-university ceiling     -> test_the_watchdog_...
    a leaked notebook             -> test_the_reaper_...
    a ledger that only ever grew  -> test_an_unspent_reservation_...
    "processed" for a failure     -> test_a_university_that_...
"""
import asyncio
import json

import pytest

from src.config import config
from src.utilities.state_management import StateManager

pytestmark = pytest.mark.asyncio


# ------------------------------------------------------------ the hung ask --

async def test_a_hung_ask_fails_on_the_deadline_instead_of_hanging(monkeypatch):
    """
    config.chat_timeout_sec was declared, documented, and read by nothing.

    On 2026-09-05 a COMSATS chat.ask stopped responding at 19:20:40 and the
    await sat there until the operator killed the process 7h11m later. The
    twelve universities queued behind it never ran.
    """
    from src.extractor.crawlers import notebook_querying as nq

    monkeypatch.setattr(config, "chat_timeout_sec", 1)

    class HangingChat:
        async def ask(self, **kwargs):
            await asyncio.sleep(3600)

    class HangingClient:
        chat = HangingChat()

    with pytest.raises(nq.QueryTimeoutError):
        await asyncio.wait_for(
            nq._ask(HangingClient(), "nb-1", "anything"), timeout=10
        )


async def test_a_hung_ask_is_a_retryable_attempt_not_a_dead_pipeline(monkeypatch):
    """
    The deadline is only useful if the loop above it treats a timeout as an
    ordinary failed attempt. A second attempt that answers must still succeed.
    """
    from src.extractor.crawlers import notebook_querying as nq

    monkeypatch.setattr(config, "chat_timeout_sec", 1)
    monkeypatch.setattr(config, "max_query_retries", 2)

    calls = {"n": 0}

    class FlakyChat:
        async def ask(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                await asyncio.sleep(3600)      # the hang
            return type("R", (), {"answer": '{"faculty_name": "Sciences"}'})()

    spec = nq.QuerySpec(
        key="faculties", prompt="p", model=nq.FacultyItem, tiers=(3,),
    )
    report = nq.ExtractionReport()
    value, error = await nq._attempt_query(
        type("C", (), {"chat": FlakyChat()})(), "nb-1", spec, None, report
    )

    assert error is None, f"the retry after the hang did not succeed: {error}"
    assert value.faculty_name == "Sciences"
    assert calls["n"] == 2, "the hung attempt was not retried"


# ------------------------------------------------------------- the ledger --

async def test_an_unspent_reservation_is_handed_back():
    """
    The suite is reserved whole and up front. COMSATS was billed all 6 having
    issued 2, because only the *overage* was ever settled -- never the shortfall.
    """
    state = StateManager()
    try:
        state.reserve_queries("comsats", 6)
        assert state.queries_used_today() == 6

        state.release_queries("comsats", 4)     # issued 2 of the 6
        assert state.queries_used_today() == 2
    finally:
        state.close()


async def test_a_release_can_never_drive_the_ledger_negative():
    """A refund larger than the reservation is a bug upstream, not free quota."""
    state = StateManager()
    try:
        state.reserve_queries("itu", 3)
        state.release_queries("itu", 99)
        assert state.queries_used_today() == 0
    finally:
        state.close()


# --------------------------------------------------------- leaked notebooks --

async def test_a_notebook_on_a_non_terminal_row_is_an_orphan():
    """
    Notebook 714feb18 was created for COMSATS at 19:18:45 and never deleted: the
    row is still 'ingested'. A row in a terminal status is not an orphan --
    its notebook id is provenance, kept to tie it to the audit document.
    """
    state = StateManager()
    try:
        state.set_status("comsats", "ingested", notebook_id="nb-orphan")
        state.set_status("itu", "completed", notebook_id="nb-done")
        state.set_status("bnu", "partial", notebook_id="nb-partial")
        state.set_status("nust", "failed", error_log="health check")

        orphans = {o["university_slug"]: o["notebook_id"]
                   for o in state.get_orphaned_notebooks()}
        assert orphans == {"comsats": "nb-orphan"}
    finally:
        state.close()


async def test_the_reaper_frees_orphans_and_stops_chasing_them(monkeypatch):
    """
    The reaper reads from sqlite rather than from a cleanup handler on purpose:
    run c_1 was killed outright, and no in-process handler survives that.
    """
    from src import orchestrator

    state = StateManager()
    try:
        state.set_status("comsats", "ingested", notebook_id="nb-orphan")
    finally:
        state.close()

    deleted = []

    class FakeNotebooks:
        async def delete(self, notebook_id):
            deleted.append(notebook_id)

    class FakeClient:
        notebooks = FakeNotebooks()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(orchestrator.NotebookLMClient, "from_storage",
                        staticmethod(lambda *a, **k: FakeClient()))

    assert await orchestrator.reap_orphaned_notebooks() == 1
    assert deleted == ["nb-orphan"]

    # And the reference is gone, so a second run does not chase a dead notebook.
    assert await orchestrator.reap_orphaned_notebooks() == 0
    assert deleted == ["nb-orphan"]


# ------------------------------------------------- honest outcome reporting --

async def test_a_university_that_failed_phase_2_is_not_logged_as_processed(monkeypatch):
    """
    Phases 1 and 2 report failure by returning, not raising -- a dead site must
    not abort the batch. _drain_queue read "did not raise" as success, so LUMS
    is recorded 'processed' in run c_1 with no notebook, no payload and no error.
    """
    from src import orchestrator
    from src.logger.pipeline_logger import PipelineLogger, RunKind

    async def fake_pipeline(**kwargs):
        return orchestrator.PipelineOutcome("skipped", "Phase 2 skipped: link health check failed.")

    monkeypatch.setattr(orchestrator, "run_master_pipeline", fake_pipeline)

    queue = [{"country": "Pakistan", "url": "https://lums.edu.pk", "name": "LUMS", "slug": "lums"}]
    with PipelineLogger(kind=RunKind.COMPLETE, settings={}, universities=queue) as run_log:
        await orchestrator._drain_queue(queue, {}, False, run_log)
        path = run_log.path

    events = json.loads(path.read_text(encoding="utf-8"))["events"]
    assert [e["outcome"] for e in events] == ["skipped"]
    assert "health check" in events[0]["error"]


async def test_a_successful_university_is_still_logged_as_processed(monkeypatch):
    from src import orchestrator
    from src.logger.pipeline_logger import PipelineLogger, RunKind

    async def fake_pipeline(**kwargs):
        return orchestrator.PipelineOutcome("processed")

    monkeypatch.setattr(orchestrator, "run_master_pipeline", fake_pipeline)

    queue = [{"country": "Pakistan", "url": "https://itu.edu.pk", "name": "ITU", "slug": "itu"}]
    with PipelineLogger(kind=RunKind.COMPLETE, settings={}, universities=queue) as run_log:
        await orchestrator._drain_queue(queue, {}, False, run_log)
        path = run_log.path

    events = json.loads(path.read_text(encoding="utf-8"))["events"]
    assert [e["outcome"] for e in events] == ["processed"]


# ------------------------------------------------------------ the watchdog --

async def test_the_watchdog_abandons_one_university_and_keeps_the_batch(monkeypatch):
    """
    Every await inside the pipeline is bounded now, but "every part is bounded"
    is a weaker claim than "the whole is bounded", and the batch window is made
    of wholes. The universities queued behind a wedged one must still run.
    """
    from src import orchestrator
    from src.logger.pipeline_logger import PipelineLogger, RunKind

    monkeypatch.setattr(config, "university_timeout_sec", 1)
    ran = []

    async def fake_pipeline(url, **kwargs):
        ran.append(url)
        if "comsats" in url:
            await asyncio.sleep(3600)
        return orchestrator.PipelineOutcome("processed")

    monkeypatch.setattr(orchestrator, "run_master_pipeline", fake_pipeline)

    queue = [
        {"country": "Pakistan", "url": "https://comsats.edu.pk", "name": "CUI", "slug": "comsats"},
        {"country": "Pakistan", "url": "https://gcu.edu.pk", "name": "GCU", "slug": "gcu"},
    ]
    with PipelineLogger(kind=RunKind.COMPLETE, settings={}, universities=queue) as run_log:
        await asyncio.wait_for(
            orchestrator._drain_queue(queue, {}, False, run_log), timeout=30
        )
        path = run_log.path

    events = json.loads(path.read_text(encoding="utf-8"))["events"]
    assert [e["outcome"] for e in events] == ["failed", "processed"]
    assert "ceiling" in events[0]["error"]
    assert ran == ["https://comsats.edu.pk", "https://gcu.edu.pk"], \
        "the university behind the wedged one never ran"

    state = StateManager()
    try:
        assert state.get_state("comsats")["status"] == "failed"
    finally:
        state.close()


async def test_a_failed_extraction_refunds_only_what_it_did_not_spend(monkeypatch):
    """
    The refund has to know what the failed run actually issued. A report the
    extractor keeps privately dies with the call, and refunding the whole suite
    would then credit back queries that were really spent -- pushing the ledger
    below the real quota, which is the more dangerous direction of the two.
    """
    from src import orchestrator
    from src.extractor.crawlers.notebook_querying import ExtractionReport

    monkeypatch.setattr(config, "queries_per_university", 6)

    async def failing_extract(*, report=None, **kwargs):
        # Two asks land, then the notebook stops answering -- COMSATS exactly.
        report.queries_used = 2
        raise RuntimeError("notebook stopped answering")

    monkeypatch.setattr(orchestrator, "extract_university_payload", failing_extract)

    state = StateManager()
    try:
        state.reserve_queries("comsats", 6)
        # The reconciliation the orchestrator performs on the failure path.
        report = ExtractionReport()
        try:
            await orchestrator.extract_university_payload(report=report)
        except RuntimeError:
            state.release_queries("comsats", max(0, 6 - report.queries_used))

        assert state.queries_used_today() == 2, "the refund credited back spent queries"
    finally:
        state.close()


async def test_the_extractor_still_makes_its_own_report_when_none_is_given():
    """The caller-owned report is an option, not a new requirement."""
    import inspect

    from src.extractor.crawlers.runner import extract_university_payload

    sig = inspect.signature(extract_university_payload)
    assert sig.parameters["report"].default is None


async def test_a_cancellation_mid_query_still_counts_what_that_query_spent(monkeypatch):
    """
    Each spec accumulates into its own sub-report and merges on the way out, so
    a cancellation landing inside one used to lose that spec's asks entirely --
    and the refund would then credit back queries that had really been issued.
    """
    from src.extractor.crawlers import runner as crawlers_runner

    async def cancel_after_one_ask(client, notebook_id, spec, source_ids, sub):
        sub.queries_used += 1
        raise asyncio.CancelledError()

    monkeypatch.setattr(crawlers_runner, "run_query", cancel_after_one_ask)

    report = crawlers_runner.ExtractionReport()
    with pytest.raises(asyncio.CancelledError):
        await crawlers_runner.extract_university_payload(
            client=None, notebook_id="nb-1", uni_name="ITU",
            uni_domain="itu.edu.pk", report=report,
        )

    assert report.queries_used == 1, "the cancelled query's spend was lost"

