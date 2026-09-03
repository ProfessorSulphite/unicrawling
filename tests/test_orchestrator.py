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
    settings_path = tmp_path / "run_settings.json"
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


# ------------------------------------------------- C23: the settings file --

def test_the_run_settings_default_comes_from_config_not_a_literal():
    """
    C23 renamed config.json to run_settings.json. The default lives on Config so
    the inspector's `batch` subcommand can share it without importing the
    orchestrator at module scope, which Finding 6 forbids.
    """
    from src.config import config
    from src.orchestrator import DEFAULT_RUN_SETTINGS_PATH

    assert DEFAULT_RUN_SETTINGS_PATH == config.run_settings_path
    assert config.run_settings_path.name == "run_settings.json"


def test_neither_entrypoint_still_defaults_to_the_old_filename():
    from src.inspector.cli import build_parser as inspector_parser
    from src.orchestrator import build_parser as orchestrator_parser

    for parser in (orchestrator_parser(), inspector_parser()):
        for action in parser._actions:
            assert str(getattr(action, "default", "")) != "config.json"


# ------------------------------------------ the Phase 1 -> Phase 2 handoff --
#
# The two pieces C22 actually changed, plus the contract they sit on. Phase 1
# writes data/links/<slug>.jsonl -- atomically, with a tier per link -- and
# ingest_university_sources documents exactly what it needs back:
#
#     links: Records from data/links/<slug>.jsonl. Each needs at least
#            {"url": str, "tier": int}. Plain strings are accepted and
#            default to tier 1.
#
# Nothing had ever tested that the orchestrator honours it.

import json as _json

from src.orchestrator import _read_harvested_links

PARTITION_RECORD = {
    "university_slug": "itu",
    "university_name": "Information Technology University",
    "university_url": "https://itu.edu.pk",
    "rank": 1,
    "url": "https://itu.edu.pk/admissions",
    "anchor_text": "Admissions",
    "tier": 1,
    "tier_name": "admissions",
    "weighted_score": 0.91,
    "raw_similarity_score": 0.88,
    "matched_keyword": "admission",
    "year_tag": "2026",
}


@pytest.fixture
def links_dir(monkeypatch, tmp_path):
    d = tmp_path / "links"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.config.config.data_links_dir", d)
    monkeypatch.setattr("src.config.config.base_dir", tmp_path)
    return d


def _write_partition(links_dir, slug, records):
    path = links_dir / f"{slug}.jsonl"
    path.write_text("\n".join(_json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_the_per_slug_partition_is_read_at_all(links_dir):
    """
    The partition is written with key "url". A reader looking for "href" finds
    nothing in it, ever, and silently falls through to the shared flat file --
    which is the undifferentiated file the partition exists to replace.
    """
    _write_partition(links_dir, "itu", [PARTITION_RECORD])
    links = _read_harvested_links("itu")
    assert links, "the per-university partition produced no links"


def test_every_link_carries_the_tier_phase_1_assigned(links_dir):
    """
    Tiers are the whole point of the partition: Phase 3 scopes each query to the
    sources that can answer it. Handing Phase 2 bare strings makes every source
    tier 1, and the scoping silently becomes a no-op.
    """
    records = [
        {**PARTITION_RECORD, "rank": 1, "url": "https://itu.edu.pk/admissions", "tier": 1},
        {**PARTITION_RECORD, "rank": 2, "url": "https://itu.edu.pk/programs", "tier": 2},
        {**PARTITION_RECORD, "rank": 3, "url": "https://itu.edu.pk/fees", "tier": 3},
    ]
    _write_partition(links_dir, "itu", records)

    links = _read_harvested_links("itu")
    assert [l["tier"] for l in links] == [1, 2, 3]
    assert [l["url"] for l in links] == [r["url"] for r in records]


def test_rank_order_from_phase_1_is_preserved(links_dir):
    """Phase 2 caps at max_sources_per_notebook, so order decides what survives."""
    records = [
        {**PARTITION_RECORD, "rank": i, "url": f"https://itu.edu.pk/p{i}", "tier": 1}
        for i in range(1, 6)
    ]
    _write_partition(links_dir, "itu", records)
    assert [l["url"] for l in _read_harvested_links("itu")] == [r["url"] for r in records]


def test_the_flat_file_is_only_a_fallback(links_dir, tmp_path):
    """
    extracted_links.txt is shared and non-atomic. It may be used when no
    partition exists, but never in preference to one.
    """
    (tmp_path / "extracted_links.txt").write_text(
        "https://itu.edu.pk/from-the-flat-file\n", encoding="utf-8"
    )
    _write_partition(links_dir, "itu", [PARTITION_RECORD])

    links = _read_harvested_links("itu")
    assert [l["url"] for l in links] == ["https://itu.edu.pk/admissions"]


def test_the_flat_file_still_works_when_no_partition_exists(links_dir, tmp_path):
    (tmp_path / "extracted_links.txt").write_text(
        "https://itu.edu.pk/a\nhttps://itu.edu.pk/b\n", encoding="utf-8"
    )
    links = _read_harvested_links("itu")
    assert [l["url"] for l in links] == ["https://itu.edu.pk/a", "https://itu.edu.pk/b"]
    # Nothing recorded a tier for these, and 1 is what Phase 2 assumes.
    assert {l["tier"] for l in links} == {1}


def test_no_links_anywhere_yields_an_empty_list(links_dir):
    assert _read_harvested_links("itu") == []


def test_what_phase_1_writes_is_what_phase_2_accepts(links_dir):
    """
    The contract, end to end: feed the reader a real partition record and check
    the result satisfies ingest_university_sources' documented normalisation.
    """
    _write_partition(links_dir, "itu", [PARTITION_RECORD])
    for item in _read_harvested_links("itu"):
        assert isinstance(item, dict)
        assert item.get("url")
        assert isinstance(item.get("tier"), int)


async def test_phase_1_hands_phase_2_tiered_records_not_bare_urls(
    monkeypatch, links_dir, tmp_path
):
    """
    The handoff, exercised inside _run_master_pipeline rather than asserted about
    it. Phase 2 is stubbed to refuse the batch immediately, so nothing touches
    the network -- but by then it has already received `links`, which is the one
    thing under test.

    IngestResult's own docstring records that returning a bare count "destroyed
    the tier association at the Phase 2/Phase 3 boundary and forced every query
    to run unscoped". That was fixed in the ingestor; the caller kept passing
    tier-less strings, so the fix was inert until now.
    """
    from src.ingestor.source_management import IngestResult
    from src.orchestrator import _run_master_pipeline

    _write_partition(links_dir, "itu", [
        {**PARTITION_RECORD, "rank": 1, "url": "https://itu.edu.pk/admissions", "tier": 1},
        {**PARTITION_RECORD, "rank": 2, "url": "https://itu.edu.pk/programs", "tier": 2},
        {**PARTITION_RECORD, "rank": 3, "url": "https://itu.edu.pk/news", "tier": 4},
    ])

    async def fake_link_extractor(**kwargs):
        return None

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    seen = {}

    async def fake_ingest(*, uni_slug, uni_name, links, client, **kw):
        seen["links"] = links
        # Refuse the batch: the run stops here, having already taken the links.
        return IngestResult(skipped=True, skip_reason="stubbed in test")

    monkeypatch.setattr("src.orchestrator.run_link_extractor", fake_link_extractor)
    monkeypatch.setattr("src.orchestrator.ingest_university_sources", fake_ingest)
    monkeypatch.setattr(
        "src.orchestrator.NotebookLMClient.from_storage", staticmethod(lambda *a, **k: _FakeClient())
    )

    state = StateManager(db_path=tmp_path / "state.sqlite")
    try:
        await _run_master_pipeline(state, "https://itu.edu.pk")
    finally:
        state.close()

    links = seen["links"]
    assert links, "Phase 2 was handed nothing"
    assert all(isinstance(l, dict) for l in links), (
        "bare strings reach Phase 2 as tier 1, silently disabling Phase 3 scoping"
    )
    assert [l["tier"] for l in links] == [1, 2, 4]
    assert [l["url"] for l in links] == [
        "https://itu.edu.pk/admissions",
        "https://itu.edu.pk/programs",
        "https://itu.edu.pk/news",
    ]
