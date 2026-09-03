"""
backup_existing_outputs is the one function here that can lose a completed run.

It copies data/outputs aside, deletes the original, and resets the state
database. Until C27 it had no test at all, on either side of the C22 move that
extracted it from pipeline.py.

The rule it encodes is easy to get wrong in the obvious direction: archive the
outputs but leave state.sqlite, and every university still reads "completed"
against payloads that have just moved, so the next run skips everything and
produces nothing. The two have to move together.
"""
import json

import pytest

from src.config import config
from src.utilities.state_management import StateManager
from src.utilities.workspace import backup_existing_outputs


@pytest.fixture
def populated_workspace():
    """An outputs tree and a state DB that says a university is done."""
    config.outputs_uni_outputs_dir.mkdir(parents=True, exist_ok=True)
    payload = config.outputs_uni_outputs_dir / "itu.json"
    payload.write_text(json.dumps({"main_info": {"name": "ITU"}}), encoding="utf-8")

    state = StateManager()
    try:
        state.set_status("itu", "completed")
    finally:
        state.close()
    return payload


def test_the_outputs_are_archived_not_destroyed(populated_workspace):
    backup = backup_existing_outputs()

    assert backup is not None and backup.is_dir()
    archived = backup / "uni_outputs" / "itu.json"
    assert archived.exists(), "the previous run's payload was deleted, not archived"
    assert json.loads(archived.read_text())["main_info"]["name"] == "ITU"


def test_the_live_outputs_are_cleared(populated_workspace):
    backup_existing_outputs()
    assert not populated_workspace.exists()
    assert config.outputs_uni_outputs_dir.is_dir(), "the directory tree was not recreated"


def test_the_state_database_is_reset_alongside_the_outputs(populated_workspace):
    """
    The rule. Leaving state behind would make the next run skip every university
    whose payload had just been archived.
    """
    state = StateManager()
    try:
        assert state.get_completed_slugs() == ["itu"]
    finally:
        state.close()

    backup_existing_outputs()

    state = StateManager()
    try:
        assert state.get_completed_slugs() == [], (
            "state still claims a university is complete after its outputs moved"
        )
    finally:
        state.close()


def test_the_write_ahead_log_is_removed_too(populated_workspace):
    """
    Deleting only state.sqlite leaves SQLite able to recover the old contents
    from -wal, so the reset would not stick.
    """
    for ext in ("-wal", "-shm"):
        p = config.state_db_path.parent / (config.state_db_path.name + ext)
        p.write_text("stale", encoding="utf-8")

    backup_existing_outputs()

    for ext in ("", "-wal", "-shm"):
        p = config.state_db_path.parent / (config.state_db_path.name + ext)
        assert not p.exists(), f"state{ext} survived the reset"


def test_the_expected_directory_tree_is_recreated(populated_workspace):
    backup_existing_outputs()
    assert config.data_outputs_dir.is_dir()
    assert config.outputs_uni_outputs_dir.is_dir()
    assert (config.data_outputs_dir / "country_outputs").is_dir()


def test_backing_up_an_empty_workspace_is_a_no_op():
    """A first run has nothing to archive and must not fail on that."""
    assert backup_existing_outputs() is None
    assert config.data_outputs_dir.is_dir()


def test_two_backups_do_not_collide(populated_workspace):
    """Timestamped, so a second archive cannot overwrite the first."""
    first = backup_existing_outputs()

    config.outputs_uni_outputs_dir.mkdir(parents=True, exist_ok=True)
    (config.outputs_uni_outputs_dir / "lums.json").write_text("{}", encoding="utf-8")
    second = backup_existing_outputs()

    assert second is not None and second != first
    assert first.exists() and second.exists()
