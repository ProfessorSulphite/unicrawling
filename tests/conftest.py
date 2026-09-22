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

    Found in C26: the suite was appending ~203 records to a production log on
    every run, 90% of which was synthetic test exhaust, and every `pytest` made
    it worse.

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
    # and extracts from it. Found writing the C27 e2e suite.
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
    ]:
        target = workspace / relative
        if not target.suffix:
            target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, name, target)

    # Tests default to the Gemini engine and to no credentials: both engines
    # are then inert unless a test supplies its own mock, so nothing reaches
    # an external API by accident.
    monkeypatch.setattr(config, "extraction_engine", "gemini")
    monkeypatch.setattr(config, "gemini_api_keys", "")
    monkeypatch.setattr(config, "deepseek_api_key", "")

    # The registry is read-only, so it keeps pointing at the real resource file:
    # tests assert against its actual contents.
    yield
