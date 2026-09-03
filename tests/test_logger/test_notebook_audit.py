"""
The audit trail, scoped per notebook, and the migration that got it there.

The legacy loggings/notebook_audit.jsonl was one append-only file holding every
event from every run since 2026-07-24 -- 19,395 records by the time it was
migrated, never rotated, never scoped. Answering "what happened to this notebook"
meant scanning all of it.

The migration's one hard requirement is that no record is lost, and its one
hazard is running twice: a migration that doubles an audit trail is worse than
one that never ran, because the damage is silent.
"""
import json

import pytest

from src.config import config
from src.logger.migrate_audit import group_by_notebook, migrate, read_legacy
from src.logger.notebook_audit import (
    append_event,
    iter_events,
    list_notebook_logs,
    load_notebook_log,
    notebook_log_path,
)


def _event(nb, et="QUERY_EXECUTED", slug="itu", ts="2026-09-03T10:00:00+00:00", **details):
    return {"timestamp": ts, "event_type": et, "notebook_id": nb,
            "uni_slug": slug, "details": details}


# -------------------------------------------------------- writing an event --

def test_an_event_lands_in_its_own_notebooks_file():
    append_event(_event("nb-a"))
    append_event(_event("nb-b"))
    assert sorted(list_notebook_logs()) == ["nb-a", "nb-b"]
    assert len(load_notebook_log("nb-a")["events"]) == 1


def test_the_document_summarises_what_it_holds():
    append_event(_event("nb-a", "NOTEBOOK_CREATED", ts="2026-09-03T10:00:00+00:00"))
    append_event(_event("nb-a", "QUERY_EXECUTED", ts="2026-09-03T10:05:00+00:00"))
    append_event(_event("nb-a", "QUERY_EXECUTED", ts="2026-09-03T10:09:00+00:00"))

    doc = load_notebook_log("nb-a")
    assert doc["event_counts"] == {"NOTEBOOK_CREATED": 1, "QUERY_EXECUTED": 2}
    assert doc["first_seen"] == "2026-09-03T10:00:00+00:00"
    assert doc["last_seen"] == "2026-09-03T10:09:00+00:00"
    assert doc["uni_slug"] == "itu"


def test_an_unknown_notebook_reads_as_empty_not_an_error():
    doc = load_notebook_log("never-seen")
    assert doc["events"] == [] and doc["event_counts"] == {}


def test_a_torn_document_does_not_make_the_notebook_unauditable():
    path = notebook_log_path("nb-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated", encoding="utf-8")

    assert load_notebook_log("nb-a")["events"] == []
    append_event(_event("nb-a"))
    assert len(load_notebook_log("nb-a")["events"]) == 1


@pytest.mark.parametrize("nasty", ["../../etc/passwd", "a/b/c", "nb id", "", "..", "x" * 400])
def test_a_notebook_id_cannot_escape_the_log_directory(nasty):
    """The id comes from an API response and becomes a filename."""
    path = notebook_log_path(nasty)
    assert path.parent == config.notebook_logs_dir
    assert len(path.name) <= 130


def test_iter_events_streams_across_notebooks():
    append_event(_event("nb-a"))
    append_event(_event("nb-b"))
    append_event(_event("nb-b"))
    assert len(list(iter_events())) == 3
    assert len(list(iter_events("nb-b"))) == 2


# ------------------------------------------------------------ the migration --

LEGACY = [
    _event("nb-1", "NOTEBOOK_CREATED", ts="2026-07-24T14:05:33+00:00"),
    _event("nb-1", "SOURCE_UPLOADED", ts="2026-07-24T14:06:00+00:00"),
    _event("nb-2", "NOTEBOOK_CREATED", slug="lums", ts="2026-08-01T09:00:00+00:00"),
    _event("nb-1", "QUERY_EXECUTED", ts="2026-07-24T14:10:00+00:00"),
]


@pytest.fixture
def legacy_file(tmp_path):
    p = tmp_path / "notebook_audit.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in LEGACY) + "\n", encoding="utf-8")
    return p


def test_migration_loses_no_record(legacy_file):
    report = migrate(source=legacy_file)
    assert report["records_in"] == report["records_out"] == 4
    assert report["notebooks"] == 2
    assert report["ok"] is True

    on_disk = sum(len(load_notebook_log(nb)["events"]) for nb in list_notebook_logs())
    assert on_disk == 4


def test_migration_is_idempotent(legacy_file):
    """The hazard: a second run must not double the trail."""
    migrate(source=legacy_file)
    first = {p.name: p.read_bytes() for p in config.notebook_logs_dir.glob("*.json")}

    migrate(source=legacy_file)
    second = {p.name: p.read_bytes() for p in config.notebook_logs_dir.glob("*.json")}

    assert first == second
    assert sum(len(load_notebook_log(nb)["events"]) for nb in list_notebook_logs()) == 4


def test_migration_groups_events_under_their_own_notebook(legacy_file):
    migrate(source=legacy_file)
    assert len(load_notebook_log("nb-1")["events"]) == 3
    assert len(load_notebook_log("nb-2")["events"]) == 1
    assert load_notebook_log("nb-2")["uni_slug"] == "lums"


def test_migration_derives_the_true_time_span_regardless_of_file_order(legacy_file):
    """nb-1's last event is written before its middle one in the source file."""
    migrate(source=legacy_file)
    doc = load_notebook_log("nb-1")
    assert doc["first_seen"] == "2026-07-24T14:05:33+00:00"
    assert doc["last_seen"] == "2026-07-24T14:10:00+00:00"


def test_check_mode_writes_nothing(legacy_file):
    report = migrate(check_only=True, source=legacy_file)
    assert report["records_in"] == 4 and report["files_written"] == 0
    assert list_notebook_logs() == []


def test_a_malformed_line_is_counted_and_skipped_not_fatal(tmp_path):
    p = tmp_path / "audit.jsonl"
    p.write_text(json.dumps(LEGACY[0]) + "\n{ truncated\n" + json.dumps(LEGACY[2]) + "\n",
                 encoding="utf-8")
    records, malformed = read_legacy(p)
    assert len(records) == 2 and malformed == 1

    report = migrate(source=p)
    # ok reflects the parseable records; the skipped line is reported separately
    # and the source file is never deleted, so nothing is unrecoverable.
    assert report["records_in"] == 2 and report["malformed_lines"] == 1 and report["ok"]


def test_a_missing_legacy_file_is_a_no_op(tmp_path):
    report = migrate(source=tmp_path / "absent.jsonl")
    assert report["records_in"] == 0 and report["ok"] is True


def test_grouping_handles_an_event_with_no_notebook_id():
    grouped = group_by_notebook([{"event_type": "X", "timestamp": "2026-01-01T00:00:00+00:00"}])
    assert "unknown" in grouped
