"""
Per-run analytics: the join across the run manifest, the audit documents, the
state DB and the payloads.

The distinction this module exists for is the one under test: `analytics` reads
the cumulative master JSONL, so a run that extracted nothing leaves the dataset
looking exactly as healthy as before and the bad run is invisible. These assert
that a run is reported as what it actually did.
"""

import json

import pytest

from src.config import config
from src.inspector.run_analytics import (
    collect_run_facts,
    render_run_analytics,
    render_run_list,
)
from src.logger.pipeline_logger import PipelineLogger, RunKind, RunNotFoundError


def _write_notebook_log(notebook_id, slug, asks, at="2026-09-16T14:20:00+00:00"):
    """A notebook audit document shaped like the one the logger writes."""
    events = []
    for i, (key, status, nbytes, secs) in enumerate(asks, start=1):
        events.append({
            "timestamp": at,
            "event_type": "QUERY_EXECUTED",
            "notebook_id": notebook_id,
            "uni_slug": slug,
            "details": {
                "query_index": i, "query_key": key, "prompt_len": 100,
                "response_bytes": nbytes, "duration_sec": secs, "status": status,
            },
        })
    doc = {
        "notebook_id": notebook_id, "uni_slug": slug,
        "first_seen": at, "last_seen": at,
        "event_counts": {"QUERY_EXECUTED": len(events)},
        "events": events,
    }
    config.notebook_logs_dir.mkdir(parents=True, exist_ok=True)
    path = config.notebook_logs_dir / f"{notebook_id}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _write_payload(slug, programs, faculties=0, failed_blocks=()):
    config.outputs_uni_outputs_dir.mkdir(parents=True, exist_ok=True)
    (config.outputs_uni_outputs_dir / f"{slug}.json").write_text(json.dumps({
        "main_info": {"name": slug, "website": f"https://{slug}.edu",
                      "description": "d", "key_links": {}},
        "programs": programs,
        "faculties": [{"faculty_name": f"F{i}"} for i in range(faculties)],
        "contact": {},
        "failed_query_blocks": list(failed_blocks),
    }), encoding="utf-8")


def _run_window(token):
    """The manifest's own start timestamp, which is what asks are joined on."""
    from src.logger.pipeline_logger import load_run
    return load_run(token)["started_at"]


@pytest.fixture
def one_run():
    """A completed single run of ITU, with an audit document and a payload."""
    with PipelineLogger(
        kind=RunKind.SINGLE,
        settings={"max_links": 60},
        universities=[{"url": "https://itu.edu.pk", "name": "ITU", "slug": "itu"}],
    ) as log:
        log.record("itu", "processed")
    token = log.path.stem

    # Inside the run's own window: that boundary is the whole join, so a fixture
    # that ignores it would be testing nothing.
    _write_notebook_log("nb-1", "itu", [
        ("identity", "ok", 903, 62.7),
        ("faculties", "ok", 1321, 53.1),
        ("roster", "oversized", 0, 67.9),
        ("roster", "oversized", 0, 68.7),
    ], at=_run_window(token))
    _write_payload("itu", {
        "bachelors": [{"name": "BS CS", "degree_level": "bachelors",
                       "duration": "4 years", "application_deadlines": ["2026-08-05"],
                       "eligibility_requirements": {"minimum_marks_percentage": "60%"}}],
        "masters": [], "phd": [], "diploma": [],
    }, faculties=4, failed_blocks=["roster"])
    return token


class TestCollection:
    def test_the_latest_run_is_chosen_when_no_token_is_given(self, one_run):
        assert collect_run_facts().run_id == one_run

    def test_no_runs_at_all_is_reported_rather_than_shown_as_empty(self):
        """
        An empty table looks exactly like a healthy run that did no work. Saying
        so is the only honest output when there is nothing recorded.
        """
        with pytest.raises(RunNotFoundError):
            collect_run_facts()

    def test_every_ask_is_attributed_to_the_run(self, one_run):
        facts = collect_run_facts(one_run)
        assert facts.total_asks == 4
        assert [a["query_key"] for a in facts.universities[0].asks] == [
            "identity", "faculties", "roster", "roster",
        ]

    def test_failed_asks_and_wasted_time_are_counted(self, one_run):
        """The number an operator acts on: quota spent for nothing."""
        uni = collect_run_facts(one_run).universities[0]
        assert uni.asks_ok == 2
        assert uni.asks_failed == 2
        assert uni.seconds_wasted == pytest.approx(136.6)
        assert uni.seconds_spent == pytest.approx(252.4)

    def test_payload_facts_are_joined_in(self, one_run):
        uni = collect_run_facts(one_run).universities[0]
        assert uni.programmes == 1
        assert uni.by_level == {"bachelors": 1, "masters": 0, "phd": 0, "diploma": 0}
        assert uni.faculties == 4
        assert uni.failed_blocks == ["roster"]

    def test_coverage_counts_a_nested_block_only_when_something_answered_it(self, one_run):
        """
        `eligibility_requirements` always serialises, empty or not, so a
        presence check would score it 100% on every payload ever written.
        """
        cov = collect_run_facts(one_run).universities[0].coverage
        assert cov["duration"] == 1.0
        assert cov["eligibility_requirements"] == 1.0
        assert cov["application_fee"] == 0.0
        assert cov["tuition_fee"] == 0.0

    def test_asks_are_joined_on_the_window_not_the_notebook_id(self, one_run):
        """
        The id in `pipeline_state` is whatever the university's MOST RECENT run
        left there, so joining on it reports zero asks for every earlier run --
        ITU's `s_2` came back empty while its audit document sat on disk.
        """
        facts = collect_run_facts(one_run)
        assert facts.universities[0].notebook_id in (None, "", "nb-1")
        assert facts.total_asks == 4, "the asks were dropped by a notebook-id filter"

    def test_an_ask_outside_the_window_is_not_attributed_to_this_run(self, one_run):
        _write_notebook_log("nb-old", "itu", [("roster", "ok", 100, 1.0)],
                            at="2020-01-01T00:00:00+00:00")
        assert collect_run_facts(one_run).total_asks == 4


class TestSupersession:
    def test_a_later_run_marks_the_earlier_one_stale(self, one_run):
        """
        The payload and state row are keyed by slug, not by run. Reporting them
        as this run's output would be a lie the tool cannot detect afterwards.
        """
        with PipelineLogger(
            kind=RunKind.SINGLE, settings={},
            universities=[{"url": "https://itu.edu.pk", "name": "ITU", "slug": "itu"}],
        ) as log:
            log.record("itu", "processed")

        facts = collect_run_facts(one_run)
        assert facts.universities[0].superseded_by == log.path.stem

    def test_the_newest_run_is_not_stale(self, one_run):
        assert collect_run_facts(one_run).universities[0].superseded_by is None


class TestRendering:
    def test_a_clean_run_exits_zero(self, one_run):
        with PipelineLogger(kind=RunKind.SINGLE, settings={},
                            universities=[{"slug": "itu", "url": "u", "name": "ITU"}]) as log:
            log.record("itu", "processed")
        token = log.path.stem
        _write_notebook_log("nb-2", "itu", [("identity", "ok", 900, 10.0)],
                            at=_run_window(token))
        assert render_run_analytics(token) == 0

    def test_a_run_with_failed_asks_exits_non_zero(self, one_run):
        """Usable as a gate in a shell script, not only as something to read."""
        assert render_run_analytics(one_run) == 1

    def test_no_runs_exits_non_zero_without_raising(self):
        assert render_run_analytics() == 1
        assert render_run_list() == 1

    def test_the_run_list_renders(self, one_run):
        assert render_run_list() == 0

    def test_suppressing_the_ask_table_still_reports(self, one_run):
        assert render_run_analytics(one_run, show_asks=False) == 1
