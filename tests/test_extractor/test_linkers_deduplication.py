"""
Structured discipline-token deduplication.

Moved out of tests/test_pipeline.py in C14. Every test here is a regression test
for a defect that was actually present in the shipped code, not a smoke test.
"""
from src.extractor.linkers.deduplication import (
    deduplicate_canonical_degree_links,
    get_discipline_tokens,
)


# =============================================================================
# B5.4 -- Structured discipline-token deduplication
# =============================================================================

def test_electrical_and_electronic_engineering_stay_separate():
    """
    SequenceMatcher at ratio > 0.88 merged these two distinct degrees
    (they score ~0.90 on slug similarity), silently deleting one programme.
    """
    a = get_discipline_tokens("https://nust.edu.pk/bs-electrical-engineering")
    b = get_discipline_tokens("https://nust.edu.pk/bs-electronic-engineering")
    assert a != b


def test_multi_campus_mirrors_merge():
    """Same programme on different campus subdomains is one source, not three."""
    keys = {
        get_discipline_tokens("https://seecs.nust.edu.pk/programs/bs-computer-science"),
        get_discipline_tokens("https://mcs.nust.edu.pk/programs/bs-computer-science"),
        get_discipline_tokens("https://ceme.nust.edu.pk/programs/bs-computer-science"),
    }
    assert len(keys) == 1


def test_intake_year_variants_merge():
    a = get_discipline_tokens("https://nust.edu.pk/programs/bs-cs-for-fall-2024")
    b = get_discipline_tokens("https://nust.edu.pk/programs/bs-cs-fall-2025-onward")
    assert a == b


def test_same_discipline_different_level_stays_separate():
    ug = get_discipline_tokens("https://nust.edu.pk/bs-computer-science")
    gr = get_discipline_tokens("https://nust.edu.pk/ms-computer-science")
    assert ug != gr


def test_path_prefix_does_not_split_identical_programmes():
    """Keying on the last path segment alone split /programs/bs-cs from /admissions/bs-cs."""
    a = get_discipline_tokens("https://nust.edu.pk/programs/bs-computer-science")
    b = get_discipline_tokens("https://nust.edu.pk/admissions/bs-computer-science")
    assert a == b


def test_deduplicate_keeps_highest_scoring_variant():
    items = [
        {"href": "https://a.nust.edu.pk/bs-cs-fall-2024", "text": "BS CS",
         "priority_tier_num": 1, "weighted_score": 0.60, "category": "Tier 1"},
        {"href": "https://b.nust.edu.pk/bs-cs-fall-2026", "text": "BS CS",
         "priority_tier_num": 1, "weighted_score": 0.95, "category": "Tier 1"},
    ]
    out = deduplicate_canonical_degree_links(items)
    assert len(out) == 1
    assert "2026" in out[0]["href"]


# =============================================================================
# TypeSafe Jev Entity Deduplication Tests
# =============================================================================

import pytest
from unittest.mock import patch, AsyncMock
from src.extractor.linkers.deduplication import (
    are_duplicate_degree_variants_jev,
    deduplicate_canonical_degree_links_async,
)
from src.utilities.typesafe_client import is_typesafe_available


@pytest.mark.asyncio
async def test_are_duplicate_degree_variants_jev_mocked():
    item1 = {"href": "https://nust.edu.pk/programs/bs-cs", "text": "BS CS"}
    item2 = {"href": "https://nust.edu.pk/programs/bs-computer-science", "text": "BS Computer Science"}

    with patch("src.extractor.linkers.deduplication.is_typesafe_available", return_value=True), \
         patch("src.extractor.linkers.deduplication.evaluate_noul", new_callable=AsyncMock) as mock_noul:
        # High confidence duplicate
        mock_noul.return_value = 0.92
        assert await are_duplicate_degree_variants_jev(item1, item2) is True

        # Low confidence distinct
        mock_noul.return_value = 0.15
        assert await are_duplicate_degree_variants_jev(item1, item2) is False


@pytest.mark.asyncio
async def test_deduplicate_canonical_degree_links_async_with_jev():
    items = [
        {"href": "https://nust.edu.pk/programs/bs-cs", "text": "BS CS",
         "priority_tier_num": 1, "weighted_score": 0.70, "category": "Tier 1"},
        {"href": "https://nust.edu.pk/programs/bs-computer-science", "text": "BS Computer Science",
         "priority_tier_num": 1, "weighted_score": 0.90, "category": "Tier 1"},
    ]

    with patch("src.extractor.linkers.deduplication.is_typesafe_available", return_value=True), \
         patch("src.extractor.linkers.deduplication.are_duplicate_degree_variants_jev", new_callable=AsyncMock) as mock_dup:
        mock_dup.return_value = True
        out = await deduplicate_canonical_degree_links_async(items)
        assert len(out) == 1
        assert out[0]["weighted_score"] == 0.90
        assert "bs-computer-science" in out[0]["href"]


@pytest.mark.live
@pytest.mark.skipif(
    not is_typesafe_available(),
    reason="Requires typesafe-sdk and TYPESAFE_API_KEY",
)
@pytest.mark.asyncio
async def test_live_jev_duplicate_entity_alignment():
    """Live API test for Jev entity deduplication."""
    item_cs = {"href": "https://nust.edu.pk/programs/bs-cs", "text": "Bachelor in CS"}
    item_comp_sci = {"href": "https://nust.edu.pk/programs/bs-computer-science", "text": "BS Computer Science"}
    item_ee = {"href": "https://nust.edu.pk/programs/bs-electrical-engineering", "text": "BS Electrical Engineering"}
    item_electr = {"href": "https://nust.edu.pk/programs/bs-electronic-engineering", "text": "BS Electronic Engineering"}

    # CS vs Computer Science -> Duplicate should be True
    assert await are_duplicate_degree_variants_jev(item_cs, item_comp_sci) is True

    # Electrical vs Electronic -> Duplicate should be False
    assert await are_duplicate_degree_variants_jev(item_ee, item_electr) is False


def test_are_potential_duplicates_heuristic():
    from src.extractor.linkers.deduplication import are_potential_duplicates
    # Direct overlap
    assert are_potential_duplicates({"computer", "engineering"}, {"software", "engineering"}) is True
    # Acronym match
    assert are_potential_duplicates({"cs"}, {"computer", "science"}) is True
    assert are_potential_duplicates({"ai"}, {"artificial", "intelligence"}) is True
    # Abbreviation / prefix match
    assert are_potential_duplicates({"bio"}, {"biological", "engineering"}) is True
    # Completely distinct (no false positives)
    assert are_potential_duplicates({"law"}, {"electrical", "engineering"}) is False
    assert are_potential_duplicates({"biology"}, {"chemistry"}) is False

