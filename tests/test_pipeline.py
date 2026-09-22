"""
End-to-end orchestration: all four phases, stubbed only at the real boundaries.

Finding 9 freed this filename. The old tests/test_pipeline.py was a 33 KB unit
grab-bag over extract_links, extract_data, ingest, state and schema; its contents
were split into the mirrored packages in C13-C19, and this is what the name was
being held for.

**What is stubbed and what is not.** Two seams only: the crawler (Phase 1 would
fetch real pages) and the extraction API (Phase 3 would spend real quota).
Everything between them is production code -- corpus assembly, the consolidated
prompts, JSON parsing, schema validation, the registry, the normalizer, bucket
assignment, persistence, aggregation and the audit. That is the point: the bugs
this pipeline has actually shipped all lived in the seams between modules that
were each individually tested.

Two of them would still have been caught here:

  - the Phase 1 -> Phase 2 handoff read "href" from a file written with "url",
    so the per-university link partition was never read (158d47e);
  - the extractor assigned rankings: [] over sourced QS ranks (cd4ecc2).

C33 replaced NotebookLM with Gemini, so the second seam moved from a fake
notebook client to a fake generate-content call. The assertions about notebook
provisioning, source scoping and the notebook audit trail went with it: there is
no notebook any more, and a test for one would be testing nothing.
"""
import json
from unittest.mock import AsyncMock

import pytest

from src.orchestrator import run_batch_pipeline, run_master_pipeline
from src.utilities.state_management import StateManager

# ---------------------------------------------------------------- fixtures --

IDENTITY_ANSWER = {
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
    "faculties": [
        {"faculty_name": "Faculty of Computing", "departments": ["Computer Science"]},
    ],
}

BACHELORS_ANSWER = [{
    "name": "BS Computer Science", "degree_level": "bachelors",
    "department": "Department of Computer Science", "duration": "4 years",
    "tuition_fee": "PKR 1,416,000", "currency": "PKR",
    "admission_requirements": "Intermediate with 60% and the ITU entry test.",
    "application_fee": "PKR 2,000", "application_deadlines": ["2026-08-05"],
    "description": "A four-year programme covering systems, theory and practice.",
    "eligibility_requirements": {"minimum_marks_percentage": "60%",
                                 "entry_tests_accepted": ["ITU Entry Test"]},
}]

MASTERS_ANSWER = [{
    "name": "MS Data Science", "degree_level": "masters",
    "department": "Department of Computer Science", "duration": "2 years",
    "tuition_fee": "PKR 327,000", "currency": "PKR",
    "application_deadlines": ["August 5, 2026"],
    "description": "A two-year taught masters in data science.",
    "eligibility_requirements": {"minimum_marks_percentage": "60%"},
}]

PHD_ANSWER = [{
    "name": "PhD Computer Science", "degree_level": "phd",
    "department": "Department of Computer Science",
    "description": "A doctoral programme in computer science.",
    "eligibility_requirements": {},
}]

# Mutated by the tests that need a different corpus, exactly as the old answer
# table was. Keyed by degree level so a test can change one bucket.
PROGRAM_ANSWERS = {
    "bachelors": BACHELORS_ANSWER,
    "masters": MASTERS_ANSWER,
    "phd": PHD_ANSWER,
    "diploma": [],
}

LINKS = [
    {"university_slug": "itu", "rank": 1, "url": "https://itu.edu.pk/admissions", "tier": 1},
    {"university_slug": "itu", "rank": 2, "url": "https://itu.edu.pk/programs", "tier": 1},
    {"university_slug": "itu", "rank": 3, "url": "https://itu.edu.pk/fees", "tier": 2},
    {"university_slug": "itu", "rank": 4, "url": "https://itu.edu.pk/contact", "tier": 4},
]


class FakeGeminiAPI:
    """Records what it was asked, so the test can assert on the interaction."""

    def __init__(self):
        self.asked = []
        self.links_seen = []

    async def generate(self, *, user_prompt, system_instruction, response_schema, **kwargs):
        self.asked.append({"prompt": user_prompt, "schema": response_schema})
        if "degree programmes" in user_prompt:
            return dict(PROGRAM_ANSWERS)
        return json.loads(json.dumps(IDENTITY_ANSWER))


@pytest.fixture
def pipeline(monkeypatch):
    """
    The two seams, and nothing else.

    conftest's autouse fixture has already pointed every writable path at
    tmp_path, so this writes into an isolated workspace.
    """
    from src.config import config

    api = FakeGeminiAPI()

    async def fake_crawl(**kwargs):
        # Phase 1's real contract: a per-university JSONL partition on disk.
        config.data_links_dir.mkdir(parents=True, exist_ok=True)
        path = config.data_links_dir / "itu.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in LINKS) + "\n", encoding="utf-8")
        return None

    async def fake_fetch_corpus(links, max_pages=25, concurrency=8):
        api.links_seen = list(links)
        return {l["url"]: f"Page text for {l['url']}" for l in links}

    monkeypatch.setattr(config, "extraction_engine", "gemini")
    monkeypatch.setattr(config, "gemini_api_keys", "test-key")
    monkeypatch.setattr("src.orchestrator.run_link_extractor", fake_crawl)
    monkeypatch.setattr(
        "src.extractor.crawlers.gemini_extractor.gemini_generate_json", api.generate
    )
    monkeypatch.setattr(
        "src.extractor.crawlers.gemini_extractor.fetch_corpus_text_for_links", fake_fetch_corpus
    )
    monkeypatch.setattr(
        "src.extractor.crawlers.gemini_extractor.free_search_find_portal",
        AsyncMock(return_value=None),
    )
    return api


@pytest.fixture(autouse=True)
def _restore_program_answers():
    """PROGRAM_ANSWERS is module state; a test that edits it must not leak."""
    original = dict(PROGRAM_ANSWERS)
    yield
    PROGRAM_ANSWERS.clear()
    PROGRAM_ANSWERS.update(original)


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
    assert record["contact"]["official_email"] == "admissions@itu.edu.pk"
    assert [f["faculty_name"] for f in record["faculties"]] == ["Faculty of Computing"]


async def test_the_jsonl_ledger_and_master_array_both_get_the_record(pipeline):
    from src.config import config

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    lines = [l for l in config.output_jsonl_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["main_info"]["abbreviation"] == "ITU"

    master = json.loads(config.output_master_json_path.read_text(encoding="utf-8"))
    assert isinstance(master, list) and len(master) == 1


async def test_phase_1_links_reach_the_engine_with_their_tiers(pipeline):
    """The handoff that was broken from the original release until 158d47e."""
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    assert [l["url"] for l in pipeline.links_seen] == [l["url"] for l in LINKS]
    assert [l["tier"] for l in pipeline.links_seen] == [1, 1, 2, 4], (
        "bare strings reach the engine as tier 1, silently disabling corpus scoping"
    )


async def test_a_full_extraction_is_two_requests_not_six(pipeline):
    """
    The consolidated passes are the whole point of the direct engines: one
    request for programmes, one for identity, for the same six blocks.
    """
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    assert len(pipeline.asked) == 2
    prompts = [a["prompt"] for a in pipeline.asked]
    assert any("degree programmes" in p for p in prompts)
    assert any("identity details" in p for p in prompts)


async def test_no_two_programme_buckets_share_an_answer(pipeline):
    """
    The 2026-09-03 failure: `bachelors` and `phd` came back byte-identical and
    the doctorates were filed as bachelors. One consolidated call now returns
    every level at once; this asserts the output shape that made it visible.
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


async def test_the_run_is_recorded_as_completed_with_its_block_count(pipeline):
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    state = StateManager()
    try:
        row = state.get_state("itu")
        assert row["status"] == "completed"
        assert row["sources_ingested"] == 4
        # Six schema blocks are accounted for, though only two requests were sent.
        assert row["queries_executed"] == 6
    finally:
        state.close()


# ------------------------------------------------------ the skip paths --

async def test_zero_harvested_links_fails_before_any_request_is_sent(pipeline, monkeypatch):
    from src.config import config

    async def crawl_nothing(**kwargs):
        config.data_links_dir.mkdir(parents=True, exist_ok=True)
        (config.data_links_dir / "itu.jsonl").write_text("", encoding="utf-8")

    monkeypatch.setattr("src.orchestrator.run_link_extractor", crawl_nothing)
    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    assert pipeline.asked == [], "quota was spent on a university with no links"
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

    assert pipeline.asked == [], "a request was sent after Phase 1 died"
    state = StateManager()
    try:
        assert state.get_status("itu") == "failed"
        row = state.get_state("itu")
        assert "CrawlFailure" in (row["error_log"] or "")
    finally:
        state.close()


async def test_a_spent_quota_stops_the_run_rather_than_writing_an_empty_payload(
    pipeline, monkeypatch
):
    """
    A quota error is not a university with no programmes. It must surface, so the
    batch stops and smart resume picks the university up again next run.
    """
    from src.config import config
    from src.utilities.gemini_client import GeminiQuotaError, reset_gemini_exhausted

    async def exhausted(**kwargs):
        raise GeminiQuotaError("daily quota spent")

    monkeypatch.setattr(
        "src.extractor.crawlers.gemini_extractor.gemini_generate_json", exhausted
    )
    try:
        with pytest.raises(GeminiQuotaError):
            await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")
    finally:
        reset_gemini_exhausted()

    assert not (config.outputs_uni_outputs_dir / "itu.json").exists()
    state = StateManager()
    try:
        assert state.get_status("itu") == "partial"
        assert "itu" not in state.get_completed_slugs()
    finally:
        state.close()


# ------------------------------------------ a partial extraction stays open --

async def test_a_failed_query_block_is_recorded_on_the_payload_and_left_open(
    pipeline, monkeypatch
):
    """
    A pass that fails yields empty buckets, not an error. Written as "completed"
    they were indistinguishable from a university that genuinely offers no
    programmes -- and the next batch skipped it forever, so one transient API
    failure permanently cost every degree level.
    """
    from src.config import config

    original = pipeline.generate

    async def identity_only(*, user_prompt, **kwargs):
        if "degree programmes" in user_prompt:
            raise RuntimeError("generate_content exploded")
        return await original(user_prompt=user_prompt, **kwargs)

    monkeypatch.setattr(
        "src.extractor.crawlers.gemini_extractor.gemini_generate_json", identity_only
    )

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    record = json.loads(
        (config.outputs_uni_outputs_dir / "itu.json").read_text(encoding="utf-8")
    )
    # The block that did answer is real data and is kept.
    assert record["contact"]["official_email"] == "admissions@itu.edu.pk"
    # The empty ones say why they are empty.
    assert record["programs"]["bachelors"] == []
    assert record["failed_query_blocks"] == ["bachelors", "diploma", "masters", "phd"]

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

    complete_masters = json.loads(json.dumps(BACHELORS_ANSWER))
    complete_masters[0].update(name="MS Data Science", degree_level="masters",
                               duration="2 years", tuition_fee="PKR 327,000")
    monkeypatch.setitem(PROGRAM_ANSWERS, "masters", complete_masters)
    monkeypatch.setitem(PROGRAM_ANSWERS, "phd", [])

    await run_master_pipeline(url="https://itu.edu.pk", uni_name_override="ITU")

    verdict = readiness_verdict(audit_records(iter_all_records()))
    assert verdict.ready is True, verdict.blocking
