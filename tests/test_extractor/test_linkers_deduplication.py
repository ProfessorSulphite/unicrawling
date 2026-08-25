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
