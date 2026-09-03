"""
Orchestration: the queue, the run log, and what --resume actually resumes.

The old pipeline.py had no tests at all -- it was 557 lines on the critical path
with zero coverage, which is why C22's gate is a dry run rather than a promise.
`--dry-run` walks the full queue and writes a real run log without executing a
phase, so the orchestration can be exercised end-to-end without a NotebookLM
account or a single query of quota.

D3 option A is the thing most worth pinning here: the run log is a manifest plus
audit trail, and `state.sqlite` is the sole authority on completion. A resume
must therefore consult the database, not the log's own event list.
"""
import json

import pytest

from src.orchestrator import build_queue, run_batch_pipeline
from src.utilities.naming import derive_uni_info
from src.utilities.state_management import StateManager

TWO_UNIVERSITIES = {
    "universities": {
        "Pakistan": [{"url": "https://itu.edu.pk", "name": "Information Technology University"}],
        "Germany": [{"url": "https://www.lmu.de", "name": "LMU Munich"}],
    },
    "pipeline_settings": {"max_links": 12, "exclude_keywords": "news", "clean_logging": False},
}


@pytest.fixture
def workspace(monkeypatch, tmp_path):
    """A config file, an isolated state DB, and isolated log directories."""
    settings_path = tmp_path / "config.json"
    settings_path.write_text(json.dumps(TWO_UNIVERSITIES), encoding="utf-8")

    single = tmp_path / "single_logs"
    complete = tmp_path / "complete_logs"
    for d in (single, complete):
        d.mkdir(parents=True, exist_ok=True)

    # config is one shared object; patching its attributes reaches every module.
    monkeypatch.setattr("src.config.config.state_db_path", tmp_path / "state.sqlite")
    monkeypatch.setattr("src.config.config.loggings_single_logs_dir", single)
    monkeypatch.setattr("src.config.config.loggings_complete_logs_dir", complete)
    monkeypatch.setattr("src.config.config.data_outputs_dir", tmp_path / "outputs")
    return settings_path


# ------------------------------------------------------------- the queue --

def test_build_queue_expands_every_country():
    queue, skipped = build_queue(TWO_UNIVERSITIES["universities"], processed_slugs=set(), rerun=False)
    assert [e["slug"] for e in queue] == ["itu", "lmu"]
    assert [e["country"] for e in queue] == ["Pakistan", "Germany"]
    assert skipped == []


def test_a_completed_university_is_skipped_not_queued():
    queue, skipped = build_queue(
        TWO_UNIVERSITIES["universities"], processed_slugs={"itu"}, rerun=False
    )
    assert [e["slug"] for e in queue] == ["lmu"]
    assert [e["slug"] for e in skipped] == ["itu"]


def test_rerun_all_ignores_completion():
    queue, skipped = build_queue(
        TWO_UNIVERSITIES["universities"], processed_slugs={"itu", "lmu"}, rerun=True
    )
    assert [e["slug"] for e in queue] == ["itu", "lmu"]
    assert skipped == []


def test_a_bare_url_string_is_accepted_alongside_a_dict():
    """The settings file has always allowed both shapes."""
    queue, _ = build_queue({"Kenya": ["https://uonbi.ac.ke"]}, set(), rerun=False)
    assert queue[0]["slug"] == "uonbi"
    assert queue[0]["url"] == "https://uonbi.ac.ke"


# ------------------------------------------------------- the dry-run gate --

async def test_dry_run_over_two_universities_completes_and_writes_a_complete_log(workspace):
    log_path = await run_batch_pipeline(config_file_path=workspace, dry_run=True)

    assert log_path is not None and log_path.exists()
    assert log_path.name.startswith("c_"), "a batch run must write a complete_logs token"

    doc = json.loads(log_path.read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert doc["settings"]["dry_run"] is True
    assert [u["slug"] for u in doc["universities"]] == ["itu", "lmu"]
    assert [(e["slug"], e["outcome"]) for e in doc["events"]] == [
        ("itu", "dry-run"),
        ("lmu", "dry-run"),
    ]


async def test_a_dry_run_spends_no_quota_and_completes_nothing(workspace):
    await run_batch_pipeline(config_file_path=workspace, dry_run=True)

    state = StateManager()
    try:
        assert state.get_completed_slugs() == []
        assert state.queries_used_today() == 0
    finally:
        state.close()


async def test_a_missing_settings_file_returns_rather_than_raising(tmp_path):
    assert await run_batch_pipeline(config_file_path=tmp_path / "absent.json") is None


# ------------------------------------------------------------ the resume --

async def test_resume_replays_the_manifest_and_skips_what_state_says_is_done(workspace):
    """
    D3 option A. The first run's log lists both universities. Marking one
    complete in state.sqlite -- and touching nothing in the log -- must make the
    resumed run skip exactly that one.
    """
    first = await run_batch_pipeline(config_file_path=workspace, dry_run=True)
    token = first.stem

    state = StateManager()
    try:
        state.set_status("itu", "completed")
    finally:
        state.close()

    second = await run_batch_pipeline(resume=token, dry_run=True)
    doc = json.loads(second.read_text(encoding="utf-8"))

    outcomes = {e["slug"]: e["outcome"] for e in doc["events"]}
    assert outcomes == {"itu": "skipped", "lmu": "dry-run"}
    assert doc["settings"]["resumed_from"] == token


async def test_resume_reuses_the_original_settings_not_todays_file(workspace):
    """
    A resumed run must reproduce the original. Rewriting the settings file
    between the two runs must not change what the resume does.
    """
    first = await run_batch_pipeline(config_file_path=workspace, dry_run=True)
    token = first.stem

    workspace.write_text(
        json.dumps({"universities": {"Kenya": ["https://uonbi.ac.ke"]}, "pipeline_settings": {}}),
        encoding="utf-8",
    )

    second = await run_batch_pipeline(resume=token, dry_run=True)
    doc = json.loads(second.read_text(encoding="utf-8"))

    assert [u["slug"] for u in doc["universities"]] == ["itu", "lmu"]
    assert doc["settings"]["max_links"] == 12
    assert "uonbi" not in {e["slug"] for e in doc["events"]}


async def test_the_manifest_carries_skipped_universities_too(workspace):
    """
    A resume of a resume must still see everything. If the manifest recorded
    only the queue, each resume would narrow the list and a later state reset
    would have nothing left to re-run.
    """
    state = StateManager()
    try:
        state.set_status("itu", "completed")
    finally:
        state.close()

    first = await run_batch_pipeline(config_file_path=workspace, dry_run=True)
    doc = json.loads(first.read_text(encoding="utf-8"))
    assert {u["slug"] for u in doc["universities"]} == {"itu", "lmu"}

    second = await run_batch_pipeline(resume=first.stem, dry_run=True)
    doc2 = json.loads(second.read_text(encoding="utf-8"))
    assert {u["slug"] for u in doc2["universities"]} == {"itu", "lmu"}


async def test_an_unknown_run_token_raises_rather_than_starting_a_fresh_run(workspace):
    from src.logger.pipeline_logger import RunNotFoundError

    with pytest.raises(RunNotFoundError):
        await run_batch_pipeline(resume="c_99999", dry_run=True)


@pytest.mark.parametrize("token", ["s_1; DROP", "../../etc", "S_1", "c_-1", "nonsense"])
async def test_a_malformed_resume_token_is_rejected(workspace, token):
    from src.logger.pipeline_logger import InvalidRunTokenError

    with pytest.raises(InvalidRunTokenError):
        await run_batch_pipeline(resume=token, dry_run=True)


# ------------------------------------------------------- the entry point --

def test_url_and_resume_are_mutually_exclusive():
    from src.orchestrator import main

    assert main(["--url", "https://itu.edu.pk", "--resume", "c_1"]) == 2


def test_derive_uni_info_is_the_one_slug_rule():
    """Everything is filed under this slug: state row, payload, links file."""
    assert derive_uni_info("https://www.itu.edu.pk/programs") == ("ITU", "itu", "itu.edu.pk")
    assert derive_uni_info("https://lmu.de", "LMU Munich") == ("LMU Munich", "lmu", "lmu.de")
