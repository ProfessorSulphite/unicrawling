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
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, name, target)

    # The registry is read-only, so it keeps pointing at the real resource file:
    # tests assert against its actual contents.
    yield


@pytest.fixture
def legacy_json_suite(monkeypatch):
    """
    Pin a test to the pre-C32 six-query JSON suite.

    The default is now the staged text plan, and the two paths ask different
    questions in a different order and parse a different wire format -- so a
    test that feeds JSON answers is testing the legacy path whether it says so
    or not. Requesting this fixture is that test saying so.

    The legacy path is still shipped (config.response_format = 'json') so a
    regression in the text path can be A/B'd against the same university, which
    is only worth having if it stays covered.
    """
    monkeypatch.setattr(config, "response_format", "json")
    yield


@pytest.fixture
def oversize_split_enabled(monkeypatch):
    """
    Turn the legacy source-splitting oversize remedy back on.

    Off by default since C32: the evidence says an oversized response is
    transient rather than a property of the corpus, so one identical re-ask is
    tried instead. The split path is retained for one release and stays covered
    here. `oversize_single_reask` is disabled alongside it so a test counting
    asks sees the split's own asks and not an extra retry in front of them.
    """
    monkeypatch.setattr(config, "enable_oversize_split", True)
    monkeypatch.setattr(config, "oversize_single_reask", False)
    yield
