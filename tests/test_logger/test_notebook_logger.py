"""
Unit tests for NotebookLM Lifecycle Audit Logger (src/logger/notebook_logger.py)
Verifies multi-destination event persistence: log, jsonl, and sqlite table.
"""
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.config import Config
from src.utilities.state_management import StateManager
from src.logger.notebook_logger import NotebookLifecycleLogger


@pytest.fixture
def temp_logger_env(monkeypatch, tmp_path):
    log_file = tmp_path / "notebook_lifecycle.log"
    jsonl_file = tmp_path / "notebook_audit.jsonl"
    sqlite_db = tmp_path / "state.sqlite"

    monkeypatch.setattr("src.logger.notebook_logger.config.notebook_lifecycle_log_path", log_file)
    monkeypatch.setattr("src.logger.notebook_logger.config.notebook_audit_jsonl_path", jsonl_file)
    # One line, not two: config is a singleton, so reaching it through the logger
    # module and through src.config patches the same object.
    monkeypatch.setattr("src.config.config.state_db_path", sqlite_db)

    logger = NotebookLifecycleLogger()
    return logger, log_file, jsonl_file, sqlite_db


def test_log_notebook_created(temp_logger_env):
    logger, log_file, jsonl_file, sqlite_db = temp_logger_env

    nb_id = "test_nb_123"
    title = "ITU_Counseling_DB"
    uni_slug = "itu"

    res = logger.log_notebook_created(notebook_id=nb_id, title=title, uni_slug=uni_slug)

    assert res["event_type"] == "NOTEBOOK_CREATED"
    assert res["notebook_id"] == nb_id
    assert res["uni_slug"] == uni_slug

    # 1. Verify text log
    assert log_file.exists()
    log_text = log_file.read_text(encoding="utf-8")
    assert "NOTEBOOK_CREATED" in log_text
    assert nb_id in log_text

    # 2. Verify jsonl log
    assert jsonl_file.exists()
    jsonl_lines = [json.loads(line) for line in jsonl_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(jsonl_lines) == 1
    assert jsonl_lines[0]["event_type"] == "NOTEBOOK_CREATED"
    assert jsonl_lines[0]["details"]["title"] == title

    # 3. Verify SQLite DB
    sm = StateManager(db_path=sqlite_db)
    audits = sm.list_notebook_audits(notebook_id=nb_id)
    assert len(audits) == 1
    assert audits[0]["event_type"] == "NOTEBOOK_CREATED"
    assert audits[0]["details"]["title"] == title


def test_all_lifecycle_events(temp_logger_env):
    logger, log_file, jsonl_file, sqlite_db = temp_logger_env
    nb_id = "test_nb_full_cycle"
    slug = "ncbae"

    logger.log_notebook_created(nb_id, "NCBAE_DB", slug)
    logger.log_source_uploaded(nb_id, "src_001", "https://ncbae.edu.pk/programs", "ready", 1.25, slug)
    logger.log_query_executed(nb_id, 1, "undergraduate", 250, 1024, 2.50, slug)
    logger.log_json_repaired(nb_id, "undergraduate", "stripped_trailing_commas", slug)
    logger.log_notebook_deleted(nb_id, "success_cleanup", slug)

    sm = StateManager(db_path=sqlite_db)
    audits = sm.list_notebook_audits(notebook_id=nb_id)
    assert len(audits) == 5

    event_types = [a["event_type"] for a in reversed(audits)]
    assert event_types == [
        "NOTEBOOK_CREATED",
        "SOURCE_UPLOADED",
        "QUERY_EXECUTED",
        "JSON_REPAIRED",
        "NOTEBOOK_DELETED",
    ]
