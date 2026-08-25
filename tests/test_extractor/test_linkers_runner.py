"""
The Phase 1 -> Phase 2 file contract: partitioned link export and slug stability.

Moved out of tests/test_pipeline.py in C14. Every test here is a regression test
for a defect that was actually present in the shipped code, not a smoke test.
"""
import json

import pytest

from src.config import config
from src.extractor.linkers.runner import (
    export_partitioned_links,
    load_partitioned_links,
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
