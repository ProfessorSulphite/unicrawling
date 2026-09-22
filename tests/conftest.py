import sys
from pathlib import Path

import pytest

# Tests import the package as `src.*`; make the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import config  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_writable_paths(monkeypatch, tmp_path):
    """
    No test may write into the real workspace.

    Found in C26: the suite was appending ~203 records to the production
    notebook audit trail on every run. 17,423 of that file's 19,395 lines were
    synthetic -- notebook ids like "nb-123" and "nb-health-1" from fixtures --
    so 90% of the audit trail for a pipeline that talks to a paid API was test
    exhaust, and every `pytest` made it worse.

    Autouse and unconditional, because the failure is silent: a test that forgets
    to redirect a path still passes, and the damage lands in a file nobody reads
    until they need it.

    config is a singleton, so patching its attributes here reaches every module
    that imported it. Tests that redirect these paths themselves still work --
    they just override an already-safe value.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    # base_dir too, and not only for tidiness: the orchestrator's flat-file link
    # fallback reads `config.base_dir / "extracted_links.txt"`, so without this a
    # test that harvests zero links silently picks up the repo root's real file
    # and provisions a notebook for it. Found writing the C27 e2e suite.
    monkeypatch.setattr(config, "base_dir", workspace)
    for name, relative in [
        ("data_dir", "data"),
        ("data_links_dir", "data/links"),
        ("data_outputs_dir", "data/outputs"),
        ("outputs_uni_outputs_dir", "data/outputs/uni_outputs"),
        ("outputs_all_uni_outputs_dir", "data/outputs/all_uni_outputs"),
        ("state_db_path", "data/state.sqlite"),
        ("output_jsonl_path", "data/outputs/all_uni_outputs/universities_crawling_data.jsonl"),
        ("output_master_json_path", "data/outputs/all_uni_outputs/universities_crawling_data.json"),
        ("loggings_dir", "loggings"),
        ("loggings_single_logs_dir", "loggings/single_logs"),
        ("loggings_complete_logs_dir", "loggings/complete_logs"),
        ("notebook_logs_dir", "loggings/notebook_logs"),
        ("notebook_lifecycle_log_path", "loggings/notebook_lifecycle.log"),
        ("notebook_audit_jsonl_path", "loggings/notebook_audit.jsonl"),
    ]:
        target = workspace / relative
        if not target.suffix:
            target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, name, target)

    # In tests, default extraction engine to notebooklm so existing mocks for
    # NotebookLM ingestion/querying execute without hitting external APIs,
    # unless a test explicitly requests deepseek.
    monkeypatch.setattr(config, "extraction_engine", "notebooklm")

    # The registry is read-only, so it keeps pointing at the real resource file:
    # tests assert against its actual contents.
    yield
