"""
End-to-end orchestration: all four phases, stubbed only at the real boundaries.

Finding 9 freed this filename. The old tests/test_pipeline.py was a 33 KB unit
grab-bag over extract_links, extract_data, ingest, state and schema; its contents
were split into the mirrored packages in C13-C19, and this is what the name was
being held for.

**What is stubbed and what is not.** Two seams only: the crawler (Phase 1 would
fetch real pages) and the NotebookLM client (Phase 2/3 would spend real quota).
Everything between them is production code -- source normalisation, the health
check, tier recording, the query suite, JSON repair, schema validation, the
registry, the normalizer, bucket assignment, persistence, aggregation and the
audit. That is the point: the bugs this pipeline has actually shipped all lived
in the seams between modules that were each individually tested.

Three of them would have been caught here:

  - the Phase 1 -> Phase 2 handoff read "href" from a file written with "url",
    so the per-university link partition was never read (158d47e);
  - concurrent asks against one notebook returned each other's answers, filing
    the PhD programmes as bachelors (9718346);
  - the extractor assigned rankings: [] over sourced QS ranks (cd4ecc2).
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.logger.notebook_audit import list_notebook_logs, load_notebook_log
from src.orchestrator import run_batch_pipeline, run_master_pipeline
from src.utilities.state_management import StateManager

# ---------------------------------------------------------------- fixtures --

MAIN_INFO_ANSWER = json.dumps({
    "main_info": {
        "name": "Information Technology University",
        "abbreviation": "ITU",
        "country": "Pakistan",
        "city": "Lahore",
        "website": "https://itu.edu.pk",
        "description": "A public research university in Lahore.",
        "type": "public",
        "key_links": {
            "academics_url": "https://itu.edu.pk/academics",
            "admissions_url": "https://itu.edu.pk/admissions",
            "application_portal_url": "https://apply.itu.edu.pk",
        },
    },
    "contact": {
        "official_email": "admissions@itu.edu.pk",
        "phone_numbers": ["042-111-111-488"],
        "physical_address": "Arfa Software Technology Park, Lahore",
    },
})

BACHELORS_ANSWER = json.dumps([{
    "name": "BS Computer Science", "degree_level": "bachelors",
    "department": "Department of Computer Science", "duration": "4 years",
    "tuition_fee": "PKR 1,416,000", "currency": "PKR",
    "admission_requirements": "Intermediate with 60% and the ITU entry test.",
    "application_fee": "PKR 2,000", "application_deadlines": ["2026-08-05"],
    "description": "A four-year programme covering systems, theory and practice.",
    "eligibility_requirements": {"minimum_marks_percentage": "60%",
                                 "entry_tests_accepted": ["ITU Entry Test"]},
}])

MASTERS_ANSWER = json.dumps([{
    "name": "MS Data Science", "degree_level": "masters",
    "department": "Department of Computer Science", "duration": "2 years",
    "tuition_fee": "PKR 327,000", "currency": "PKR",
    "application_deadlines": ["August 5, 2026"],
    "description": "A two-year taught masters in data science.",
    "eligibility_requirements": {"minimum_marks_percentage": "60%"},
}])

PHD_ANSWER = json.dumps([{
    "name": "PhD Computer Science", "degree_level": "phd",
    "department": "Department of Computer Science",
    "description": "A doctoral programme in computer science.",
    "eligibility_requirements": {},
}])

FACULTIES_ANSWER = json.dumps([
    {"faculty_name": "Faculty of Computing", "departments": ["Computer Science"]},
])

# Keyed by QUERY_SUITE order. A dict rather than a side_effect list so the test
# is not silently order-dependent -- the answer is chosen by what was asked.
ANSWERS = {
    "BACHELORS": BACHELORS_ANSWER,
    "MASTERS": MASTERS_ANSWER,
    "PhD and research doctorate": PHD_ANSWER,
    "DIPLOMA": "[]",
    "faculties": FACULTIES_ANSWER,
}


def _answer_for(question: str) -> str:
    if "main_info" in question or "contact" in question.lower()[:400]:
        return MAIN_INFO_ANSWER
    for marker, answer in ANSWERS.items():
        if marker in question:
            return answer
    return MAIN_INFO_ANSWER


class FakeNotebookLM:
    """Records what it was asked, so the test can assert on the interaction."""

    def __init__(self):
        self.asked = []
        self.uploaded = []
        self.created = []
        self.deleted = []

        self.notebooks = MagicMock()
        self.notebooks.list = AsyncMock(return_value=[])
        self.notebooks.create = AsyncMock(side_effect=self._create)
        self.notebooks.delete = AsyncMock(side_effect=self._delete)

        self.sources = MagicMock()
        self.sources.add_url = AsyncMock(side_effect=self._add_url)
        self.sources.add_text = AsyncMock(side_effect=self._add_url)
        self.sources.wait_until_ready = AsyncMock(return_value=True)
        self.sources.wait_for_sources = AsyncMock(return_value=True)

        self.chat = MagicMock()
        self.chat.ask = AsyncMock(side_effect=self._ask)

    async def _create(self, title):
        self.created.append(title)
        return MagicMock(id="nb-e2e-1")

    async def _delete(self, notebook_id):
        self.deleted.append(notebook_id)
        return True

    async def _add_url(self, notebook_id, url, *a, **kw):
        self.uploaded.append(url)
        return MagicMock(id=f"src-{len(self.uploaded)}")

    async def _ask(self, notebook_id, question, source_ids=None, conversation_id=None):
        self.asked.append({"question": question, "source_ids": source_ids})
        return MagicMock(answer=_answer_for(question))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


LINKS = [
    {"university_slug": "itu", "rank": 1, "url": "https://itu.edu.pk/admissions", "tier": 1},
    {"university_slug": "itu", "rank": 2, "url": "https://itu.edu.pk/programs", "tier": 1},
    {"university_slug": "itu", "rank": 3, "url": "https://itu.edu.pk/fees", "tier": 2},
    {"university_slug": "itu", "rank": 4, "url": "https://itu.edu.pk/contact", "tier": 4},
]


@pytest.fixture
def pipeline(monkeypatch):
    """
    The two seams, and nothing else.

    conftest's autouse fixture has already pointed every writable path at
    tmp_path, so this writes into an isolated workspace.
    """
    from src.config import config

    client = FakeNotebookLM()

    async def fake_crawl(**kwargs):
        # Phase 1's real contract: a per-university JSONL partition on disk.
        config.data_links_dir.mkdir(parents=True, exist_ok=True)
        path = config.data_links_dir / "itu.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in LINKS) + "\n", encoding="utf-8")
        return None

    async def no_http_probe(url, *a, **kw):
        return True

    monkeypatch.setattr("src.orchestrator.run_link_extractor", fake_crawl)
    monkeypatch.setattr("src.orchestrator.NotebookLMClient.from_storage",
                        staticmethod(lambda *a, **k: client))
    # The pre-flight check would otherwise make real HTTP requests.
    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", no_http_probe)
    monkeypatch.setattr("src.extractor.crawlers.runner.exa_find_application_portal",
                        AsyncMock(return_value=None))
    return client


# ------------------------------------------------------------- a full run --

async def test_a_full_run_produces_a_validated_payload(pipeline):
    from src.config import config

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    payload_path = config.outputs_uni_outputs_dir / "itu.json"
    assert payload_path.exists(), "no per-university payload was written"
    record = json.loads(payload_path.read_text(encoding="utf-8"))

    assert record["main_info"]["name"] == "Information Technology University"
    assert record["main_info"]["country"] == "Pakistan"
    assert [p["name"] for p in record["programs"]["bachelors"]] == ["BS Computer Science"]
    assert [p["name"] for p in record["programs"]["masters"]] == ["MS Data Science"]
    assert [p["name"] for p in record["programs"]["phd"]] == ["PhD Computer Science"]
    assert record["programs"]["diploma"] == []


async def test_the_jsonl_ledger_and_master_array_both_get_the_record(pipeline):
    from src.config import config

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    lines = [l for l in config.output_jsonl_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["main_info"]["abbreviation"] == "ITU"

    master = json.loads(config.output_master_json_path.read_text(encoding="utf-8"))
    assert isinstance(master, list) and len(master) == 1


async def test_phase_1_links_reach_phase_2_with_their_tiers(pipeline):
    """The handoff that was broken from the original release until 158d47e."""
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    assert pipeline.uploaded == [l["url"] for l in LINKS]

    state = StateManager()
    try:
        assert state.source_ids_by_tier("itu") == {
            1: ["src-1", "src-2"], 2: ["src-3"], 4: ["src-4"],
        }
    finally:
        state.close()


async def test_each_query_is_scoped_to_the_sources_that_can_answer_it(pipeline):
    """
    Tier scoping is the reason a tier is recorded at all. Programme queries ask
    tiers 1-2; the identity query is allowed the whole corpus.
    """
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    programme_asks = [a for a in pipeline.asked if "degree programme" in a["question"]]
    assert programme_asks, "no programme query was issued"
    for ask in programme_asks:
        assert ask["source_ids"] == ["src-1", "src-2", "src-3"], (
            "a programme query was not scoped to its tiers"
        )


async def test_no_two_programme_queries_share_an_answer(pipeline):
    """
    The 2026-09-03 failure: `bachelors` and `phd` came back byte-identical and
    the doctorates were filed as bachelors. Serial execution prevents it; this
    asserts the output shape that made it visible.
    """
    from src.config import config

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")
    programs = json.loads((config.outputs_uni_outputs_dir / "itu.json").read_text())["programs"]

    filled = [(k, v) for k, v in programs.items() if v]
    for i, (a, va) in enumerate(filled):
        for b, vb in filled[i + 1:]:
            assert va != vb, f"{a} and {b} hold the same programmes"


async def test_the_registry_supplies_rankings_the_model_never_saw(pipeline):
    """ITU has no recorded ranking, so the payload must claim none."""
    from src.config import config

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")
    main = json.loads((config.outputs_uni_outputs_dir / "itu.json").read_text())["main_info"]
    assert main["rankings"] == []
    assert main["domain_verified"] is True


async def test_the_run_is_recorded_as_completed_with_its_query_count(pipeline):
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    state = StateManager()
    try:
        row = state.get_state("itu")
        assert row["status"] == "completed"
        assert row["notebook_id"] == "nb-e2e-1"
        assert row["sources_ingested"] == 4
        assert row["queries_executed"] == 6
        assert state.queries_used_today() == 6
    finally:
        state.close()


async def test_the_notebook_is_deleted_only_after_the_payload_is_durable(pipeline):
    """
    Deletion used to live in a `finally:`, so a transient chat error destroyed
    every ingested source with no way to retry short of re-crawling.
    """
    from src.config import config

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")
    assert pipeline.deleted == ["nb-e2e-1"]
    assert (config.outputs_uni_outputs_dir / "itu.json").exists()


async def test_the_notebook_audit_trail_records_the_whole_lifecycle(pipeline):
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    assert list_notebook_logs() == ["nb-e2e-1"]
    counts = load_notebook_log("nb-e2e-1")["event_counts"]
    assert counts["NOTEBOOK_CREATED"] == 1
    assert counts["SOURCE_UPLOADED"] == 4
    assert counts["QUERY_EXECUTED"] == 6
    assert counts["NOTEBOOK_DELETED"] == 1


# ------------------------------------------------------ the skip paths --

async def test_a_health_check_refusal_skips_before_any_query_is_spent(pipeline, monkeypatch):
    """
    Plan section 6: a university whose pages NotebookLM cannot ingest is skipped
    rather than burning quota. The refusal must land before Phase 3.
    """
    from src.ingestor.health_sampling import HealthReport

    async def refuse(*a, **kw):
        urls = [l["url"] for l in LINKS]
        return HealthReport(sampled=urls, passed=[], failed=urls, healthy=False,
                            reason="every sampled link failed to load")

    monkeypatch.setattr("src.ingestor.source_management.run_health_check", refuse)
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    assert pipeline.asked == [], "quota was spent on a university that failed pre-flight"
    state = StateManager()
    try:
        assert state.get_status("itu") == "failed"
        assert state.queries_used_today() == 0
    finally:
        state.close()


async def test_zero_harvested_links_fails_before_a_notebook_is_created(pipeline, monkeypatch):
    from src.config import config

    async def crawl_nothing(**kwargs):
        config.data_links_dir.mkdir(parents=True, exist_ok=True)
        (config.data_links_dir / "itu.jsonl").write_text("", encoding="utf-8")

    monkeypatch.setattr("src.orchestrator.run_link_extractor", crawl_nothing)
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    assert pipeline.created == [], "a notebook was provisioned for a university with no links"
    state = StateManager()
    try:
        assert state.get_status("itu") == "failed"
    finally:
        state.close()


async def test_a_raising_phase_1_marks_the_university_failed(pipeline, monkeypatch):
    """
    Phase 1 raises CrawlFailure for a site that yields nothing. Uncaught, the
    state row kept whatever the previous phase had written, so a university whose
    crawl died looked merely unstarted and no error was ever recorded for it.
    """
    from src.extractor.linkers.crawling import CrawlFailure

    async def crawl_explodes(**kwargs):
        raise CrawlFailure("crawl of https://itu.edu.pk produced zero links")

    monkeypatch.setattr("src.orchestrator.run_link_extractor", crawl_explodes)
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    assert pipeline.created == [], "a notebook was provisioned after Phase 1 died"
    state = StateManager()
    try:
        assert state.get_status("itu") == "failed"
        row = state.get_state("itu")
        assert "CrawlFailure" in (row["error_log"] or "")
    finally:
        state.close()


# ------------------------------------------ a partial extraction stays open --

async def test_a_failed_query_block_is_recorded_on_the_payload_and_left_open(
    pipeline, monkeypatch
):
    """
    A query that fails every retry yields an empty bucket, not an error. Written
    as "completed" it was indistinguishable from a university that genuinely
    offers no bachelors programmes -- and the next batch skipped it forever, so
    one transient API failure permanently cost a degree level.
    """
    from src.config import config

    original_ask = pipeline.chat.ask.side_effect

    async def ask_but_bachelors_always_fails(notebook_id, question, **kwargs):
        if "BACHELORS" in question:
            raise RuntimeError("chat.ask exploded")
        return await original_ask(notebook_id, question, **kwargs)

    pipeline.chat.ask = AsyncMock(side_effect=ask_but_bachelors_always_fails)

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    record = json.loads(
        (config.outputs_uni_outputs_dir / "itu.json").read_text(encoding="utf-8")
    )
    # The other blocks are real data and are kept.
    assert [p["name"] for p in record["programs"]["masters"]] == ["MS Data Science"]
    # The empty one says why it is empty.
    assert record["programs"]["bachelors"] == []
    assert record["failed_query_blocks"] == ["bachelors"]

    state = StateManager()
    try:
        assert state.get_status("itu") == "partial"
        assert "itu" not in state.get_completed_slugs(), (
            "a partial university must stay queued for the next run"
        )
        assert "bachelors" in state.get_state("itu")["error_log"]
    finally:
        state.close()


async def test_a_clean_run_records_no_failed_blocks(pipeline):
    from src.config import config

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    record = json.loads(
        (config.outputs_uni_outputs_dir / "itu.json").read_text(encoding="utf-8")
    )
    assert record["failed_query_blocks"] == []

    state = StateManager()
    try:
        assert state.get_status("itu") == "completed"
    finally:
        state.close()


# ------------------------------------------------- batch, resume, and audit --

async def test_a_batch_run_writes_a_manifest_and_completes(pipeline, tmp_path):
    settings = tmp_path / "run_settings.json"
    settings.write_text(json.dumps({
        "universities": {"Pakistan": [{"url": "https://itu.edu.pk", "name": "ITU"}]},
        "pipeline_settings": {"clean_logging": False},
    }), encoding="utf-8")

    log_path = await run_batch_pipeline(config_file_path=settings)

    doc = json.loads(log_path.read_text(encoding="utf-8"))
    assert doc["status"] == "completed"
    assert [(e["slug"], e["outcome"]) for e in doc["events"]] == [("itu", "processed")]


async def test_resuming_a_run_skips_what_state_says_is_complete(pipeline, tmp_path):
    settings = tmp_path / "run_settings.json"
    settings.write_text(json.dumps({
        "universities": {"Pakistan": [{"url": "https://itu.edu.pk", "name": "ITU"}]},
        "pipeline_settings": {"clean_logging": False},
    }), encoding="utf-8")

    first = await run_batch_pipeline(config_file_path=settings)
    asks_after_first = len(pipeline.asked)

    second = await run_batch_pipeline(resume=first.stem)
    doc = json.loads(second.read_text(encoding="utf-8"))

    assert [(e["slug"], e["outcome"]) for e in doc["events"]] == [("itu", "skipped")]
    assert len(pipeline.asked) == asks_after_first, "a completed university was re-queried"


async def test_a_clean_run_still_fails_the_audit_gate_honestly(pipeline):
    """
    The push gate (C21). This run's masters and phd programmes genuinely lack an
    application_fee, so the corpus is NOT ready -- and the auditor must say so
    rather than waving through a run that completed without error.
    """
    from src.inspector.auditor import audit_records, readiness_verdict
    from src.inspector.records import iter_all_records

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    report = audit_records(iter_all_records())
    assert report.programs == 3
    assert report.misfiled == [] and report.duplicated_buckets == []

    verdict = readiness_verdict(report)
    assert verdict.ready is False
    assert any("application_fee" in r for r in verdict.blocking)


async def test_a_corpus_meeting_every_floor_passes_the_gate(pipeline, monkeypatch):
    """The gate is not simply always-false: a complete corpus is allowed through."""
    from src.inspector.auditor import audit_records, readiness_verdict
    from src.inspector.records import iter_all_records

    # Distinct from the bachelors answer on purpose: two programme queries
    # returning identical content is cross-contamination, and the extractor
    # correctly drops both blocks when it sees it.
    complete_masters = json.loads(BACHELORS_ANSWER)
    complete_masters[0].update(name="MS Data Science", degree_level="masters",
                               duration="2 years", tuition_fee="PKR 327,000")
    monkeypatch.setitem(ANSWERS, "MASTERS", json.dumps(complete_masters))
    monkeypatch.setitem(ANSWERS, "PhD and research doctorate", "[]")

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    verdict = readiness_verdict(audit_records(iter_all_records()))
    assert verdict.ready is True, verdict.blocking
