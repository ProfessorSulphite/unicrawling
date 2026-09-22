"""
The failures of run c_1 (2026-09-05), each pinned by a test.

That run reached 9 of 21 universities in 11 hours. Every test in this file is a
regression test for something the run log, the state DB or the notebook audit
proved had actually gone wrong -- not for a hypothesis about what might.

    no per-university ceiling     -> test_the_watchdog_...
    a ledger that only ever grew  -> test_an_unspent_reservation_...
    "processed" for a failure     -> test_a_university_that_...

C33 retired the NotebookLM engine, and the regressions that were specific to it
-- the hung chat.ask, the leaked notebook, the refund reconciliation -- went with
the code they guarded.
"""
import asyncio
import json

import pytest

from src.config import config
from src.utilities.state_management import StateManager

pytestmark = pytest.mark.asyncio


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
