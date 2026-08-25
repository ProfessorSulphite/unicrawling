"""
Tests for structured JSON pipeline run logs (C9).

Covers the two things a resumable log must get right: ids that cannot collide,
and a reader that refuses to turn a hostile token into a filesystem path.
"""
import json
import threading

import pytest

from src.logger import pipeline_logger as pl
from src.logger.pipeline_logger import (
    InvalidRunTokenError,
    PipelineLogger,
    RunKind,
    RunNotFoundError,
    list_runs,
    load_run,
    parse_run_token,
    run_log_path,
)


@pytest.fixture(autouse=True)
def isolated_log_dirs(tmp_path, monkeypatch):
    """Every test gets its own loggings tree; ids must start from 1."""
    single = tmp_path / "single_logs"
    complete = tmp_path / "complete_logs"
    single.mkdir()
    complete.mkdir()
    monkeypatch.setattr(pl.config, "loggings_single_logs_dir", single)
    monkeypatch.setattr(pl.config, "loggings_complete_logs_dir", complete)
    return tmp_path


# ---------------------------------------------------------------- token parsing

@pytest.mark.parametrize("token,expected", [
    ("s_1", ("single", 1)),
    ("c_7", ("complete", 7)),
    ("s_42", ("single", 42)),
    ("  c_10  ", ("complete", 10)),   # shell-quoted arg
    ("c_1\n", ("complete", 1)),        # trailing newline from a pipe
    ("s_0", ("single", 0)),
])
def test_parse_valid_tokens(token, expected):
    assert parse_run_token(token) == expected


@pytest.mark.parametrize("token", [
    "", "s", "s_", "_1", "x_1", "S_1", "C_1",
    "s_-1", "s_1.0", "s_ 1", "s_1_2", "s_one",
    "../../etc/passwd", "s_1/../../x", "s_1; rm -rf /",
    "s_١٢٣",  # Arabic-Indic digits must not alias onto s_123
])
def test_parse_rejects_malformed_tokens(token):
    """A token reaches the filesystem as a path -- garbage must never get through."""
    with pytest.raises(InvalidRunTokenError):
        parse_run_token(token)


@pytest.mark.parametrize("token", [None, 42, 3.5, ["s_1"], {"s": 1}])
def test_parse_rejects_non_strings(token):
    with pytest.raises(InvalidRunTokenError):
        parse_run_token(token)


def test_run_log_path_stays_inside_the_log_directory(isolated_log_dirs):
    p = run_log_path("s_9")
    assert p.parent == isolated_log_dirs / "single_logs"
    assert p.name == "s_9.json"


# ------------------------------------------------------------- id allocation

def test_ids_start_at_one_and_increment():
    assert PipelineLogger(RunKind.COMPLETE).run_id == "c_1"
    assert PipelineLogger(RunKind.COMPLETE).run_id == "c_2"
    assert PipelineLogger(RunKind.COMPLETE).run_id == "c_3"


def test_single_and_complete_ids_are_independent():
    assert PipelineLogger(RunKind.COMPLETE).run_id == "c_1"
    assert PipelineLogger(RunKind.SINGLE).run_id == "s_1"
    assert PipelineLogger(RunKind.COMPLETE).run_id == "c_2"


def test_id_resumes_above_existing_files(isolated_log_dirs):
    (isolated_log_dirs / "complete_logs" / "c_5.json").write_text("{}")
    assert PipelineLogger(RunKind.COMPLETE).run_id == "c_6"


def test_unrelated_files_do_not_disturb_numbering(isolated_log_dirs):
    d = isolated_log_dirs / "complete_logs"
    (d / "notes.txt").write_text("x")
    (d / "c_notanumber.json").write_text("{}")
    (d / "backup_c_99.json").write_text("{}")
    assert PipelineLogger(RunKind.COMPLETE).run_id == "c_1"


def test_concurrent_allocation_never_collides():
    """
    Scan-then-write is a check-then-act race. Twenty threads must produce twenty
    distinct ids and twenty distinct files.
    """
    ids, errors = [], []
    lock = threading.Lock()

    def claim():
        try:
            rid = PipelineLogger(RunKind.COMPLETE).run_id
            with lock:
                ids.append(rid)
        except Exception as exc:          # pragma: no cover - failure detail
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=claim) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(ids) == 20
    assert len(set(ids)) == 20, f"duplicate run ids allocated: {sorted(ids)}"


def test_log_file_exists_immediately_so_a_crashed_run_is_resumable():
    run = PipelineLogger(RunKind.COMPLETE, universities=[{"slug": "nust"}])
    assert run.path.exists()
    assert load_run(run.run_id)["status"] == "running"


def test_rejects_unknown_kind():
    with pytest.raises(ValueError):
        PipelineLogger("partial")


# ------------------------------------------------------------------ manifest

def test_manifest_records_settings_and_targets():
    unis = [{"name": "NUST", "url": "https://nust.edu.pk", "slug": "nust"}]
    run = PipelineLogger(RunKind.COMPLETE, settings={"max_links": 80}, universities=unis)
    run.finish()

    doc = load_run(run.run_id)
    assert doc["settings"] == {"max_links": 80}
    assert doc["universities"] == unis
    assert doc["status"] == "completed"
    assert doc["finished_at"] is not None


def test_events_are_appended_in_order():
    run = PipelineLogger(RunKind.SINGLE)
    run.record("nust", "crawled", links=473)
    run.record("nust", "ingested", sources=150)
    run.finish()

    events = load_run(run.run_id)["events"]
    assert [e["outcome"] for e in events] == ["crawled", "ingested"]
    assert events[0]["links"] == 473
    assert all(e["slug"] == "nust" for e in events)


def test_document_property_is_a_defensive_copy():
    run = PipelineLogger(RunKind.SINGLE)
    run.document["events"].append({"forged": True})
    assert run.document["events"] == []


def test_context_manager_marks_failure_with_the_exception():
    run = None
    with pytest.raises(RuntimeError):
        with PipelineLogger(RunKind.COMPLETE) as r:
            run = r
            raise RuntimeError("crawl exploded")

    doc = load_run(run.run_id)
    assert doc["status"] == "failed"
    assert "crawl exploded" in doc["error"]
    assert doc["finished_at"] is not None


def test_context_manager_completes_a_clean_run():
    with PipelineLogger(RunKind.COMPLETE) as run:
        run.record("nust", "completed")
    assert load_run(run.run_id)["status"] == "completed"


def test_writes_are_atomic_leaving_no_temp_files(isolated_log_dirs):
    run = PipelineLogger(RunKind.COMPLETE)
    for i in range(5):
        run.record(f"uni{i}", "crawled")
    run.finish()
    assert list(isolated_log_dirs.glob("**/*.tmp")) == []


# -------------------------------------------------------------------- reading

def test_load_run_rejects_a_missing_log():
    with pytest.raises(RunNotFoundError):
        load_run("c_999")


def test_load_run_rejects_a_bad_token_before_touching_disk():
    with pytest.raises(InvalidRunTokenError):
        load_run("../../etc/passwd")


def test_list_runs_is_newest_first_and_counts_contents():
    a = PipelineLogger(RunKind.COMPLETE, universities=[{"slug": "x"}])
    a.record("x", "crawled")
    a.finish()
    PipelineLogger(RunKind.COMPLETE).finish()

    rows = list_runs(RunKind.COMPLETE)
    assert [r["run_id"] for r in rows] == ["c_2", "c_1"]
    assert rows[1]["universities"] == 1
    assert rows[1]["events"] == 1


def test_list_runs_skips_a_corrupt_log_instead_of_failing(isolated_log_dirs):
    """One half-written file must not make every other run unlistable."""
    PipelineLogger(RunKind.COMPLETE).finish()
    (isolated_log_dirs / "complete_logs" / "c_2.json").write_text("{not json")
    (isolated_log_dirs / "complete_logs" / "c_3.json").write_text('"a string, not a doc"')

    rows = list_runs(RunKind.COMPLETE)
    assert [r["run_id"] for r in rows] == ["c_1"]


def test_list_runs_covers_both_kinds_by_default():
    PipelineLogger(RunKind.COMPLETE).finish()
    PipelineLogger(RunKind.SINGLE).finish()
    assert {r["run_id"] for r in list_runs()} == {"c_1", "s_1"}


def test_manifest_is_the_resume_source_not_the_progress_record():
    """
    D3 option A: the log says what a run targeted; state.sqlite says what is done.
    A university recorded as completed here is still offered back for resume --
    filtering is StateManager's job, deliberately not the log's.
    """
    unis = [{"slug": "nust"}, {"slug": "lums"}]
    run = PipelineLogger(RunKind.COMPLETE, universities=unis)
    run.record("nust", "completed")
    run.finish("interrupted")

    doc = load_run(run.run_id)
    assert doc["universities"] == unis
    assert doc["status"] == "interrupted"
