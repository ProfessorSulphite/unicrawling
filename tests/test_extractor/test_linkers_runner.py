"""
The Phase 1 -> Phase 2 file contract: partitioned link export and slug stability.

Moved out of tests/test_pipeline.py in C14. Every test here is a regression test
for a defect that was actually present in the shipped code, not a smoke test.
"""
import json

import pytest

from src.config import config
from src.extractor.linkers.crawling import CrawlFailure
from src.extractor.linkers.runner import (
    export_partitioned_links,
    load_partitioned_links,
    run_pipeline,
    slugify_university,
)


# =============================================================================
# Phase 1 <-> Phase 2 file contract
# =============================================================================

def test_partitioned_export_round_trip(tmp_path, monkeypatch):
    """
    HEC batch mode wrote one undifferentiated extracted_links.txt (287 links:
    ~284 NUST, ~10 LUMS, 0 ITU) with no record of which university a URL belonged
    to, making Phase 2's per-university contract unsatisfiable.
    """
    monkeypatch.setattr(config, "data_links_dir", tmp_path)
    items = [
        {"href": "https://nust.edu.pk/bs-cs", "text": "BS CS", "priority_tier_num": 1,
         "category": "Tier 1", "weighted_score": 0.9, "raw_similarity_score": 0.8,
         "matched_keyword": "k", "year_tag": "t"},
        {"href": "https://nust.edu.pk/contact", "text": "Contact", "priority_tier_num": 4,
         "category": "Tier 4", "weighted_score": 0.5, "raw_similarity_score": 0.5,
         "matched_keyword": "k", "year_tag": "t"},
    ]
    export_partitioned_links(items, "nust", "NUST", "https://nust.edu.pk")
    loaded = load_partitioned_links("nust")
    assert len(loaded) == 2
    assert {r["tier"] for r in loaded} == {1, 4}
    assert all(r["university_slug"] == "nust" for r in loaded)
    assert loaded[0]["url"] == "https://nust.edu.pk/bs-cs"


def test_slugify_is_stable_across_url_forms():
    assert (slugify_university("NUST", "https://www.nust.edu.pk/")
            == slugify_university("NUST", "http://nust.edu.pk"))


# =============================================================================
# A total crawl failure must not terminate the process
# =============================================================================

async def test_a_total_crawl_failure_raises_rather_than_exiting(tmp_path, monkeypatch):
    """
    run_pipeline used to call sys.exit(1) when every target produced zero links.
    SystemExit inherits from BaseException, so the batch driver's `except
    Exception` did not catch it: one dead university terminated the whole run and
    every university queued behind it never executed. It must raise an ordinary
    exception the caller can handle.
    """
    monkeypatch.setattr(config, "data_links_dir", tmp_path)

    async def _boom(start_url, max_pages=15):
        raise CrawlFailure(f"nothing at {start_url}")

    monkeypatch.setattr("src.extractor.linkers.runner.crawl_site_links", _boom)

    with pytest.raises(CrawlFailure) as excinfo:
        await run_pipeline(
            url="https://dead.edu.pk",
            output_links=str(tmp_path / "links.txt"),
            output_detailed=str(tmp_path / "detailed.txt"),
        )
    assert not isinstance(excinfo.value, SystemExit)
    assert "dead.edu.pk" in str(excinfo.value)


async def test_a_partial_batch_failure_still_returns(tmp_path, monkeypatch):
    """One dead site among several is a recorded failure, not an exception."""
    monkeypatch.setattr(config, "data_links_dir", tmp_path)

    async def _one_good_one_dead(start_url, max_pages=15):
        if "dead" in start_url:
            raise CrawlFailure(f"nothing at {start_url}")
        return [{"href": "https://live.edu.pk/bs-cs", "text": "BS CS", "title": "", "crawl_score": 1.0}]

    monkeypatch.setattr("src.extractor.linkers.runner.crawl_site_links", _one_good_one_dead)
    monkeypatch.setattr(
        "src.extractor.linkers.runner.extract_hec_universities",
        lambda limit=5: [
            {"name": "Live", "url": "https://live.edu.pk"},
            {"name": "Dead", "url": "https://dead.edu.pk"},
        ],
    )

    result = await run_pipeline(
        hec_mode=True,
        hec_limit=2,
        output_links=str(tmp_path / "links.txt"),
        output_detailed=str(tmp_path / "detailed.txt"),
    )
    assert [name for name, _, _ in result["succeeded"]] == ["Live"]
    assert [name for name, _ in result["failed"]] == ["Dead"]
