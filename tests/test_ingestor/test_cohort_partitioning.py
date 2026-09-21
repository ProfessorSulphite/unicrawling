"""
Unit tests for degree-cohort partitioning for massive link corpora.
"""
import pytest

from src.ingestor.cohort_partitioning import (
    DegreeCohort,
    classify_link_degree_level,
    partition_links_into_cohorts,
)


def test_classify_link_degree_level():
    assert classify_link_degree_level({"url": "https://mit.edu/academics/bachelor-of-science"}) == "bachelors"
    assert classify_link_degree_level({"url": "https://mit.edu/degree-charts/course-6-eecs"}) == "bachelors"
    assert classify_link_degree_level({"url": "https://mit.edu/admissions/majors"}) == "bachelors"
    assert classify_link_degree_level({"url": "https://mit.edu/masters/mba-program"}) == "masters"
    assert classify_link_degree_level({"url": "https://mit.edu/graduate/ms-computer-science"}) == "masters"
    assert classify_link_degree_level({"url": "https://mit.edu/doctoral/phd-physics"}) == "phd"
    assert classify_link_degree_level({"url": "https://mit.edu/programs/postgraduate-diploma"}) == "diploma"
    assert classify_link_degree_level({"url": "https://mit.edu/about-us"}) is None


def test_partition_links_under_platform_limit():
    """Links <= 300 should return a single unified cohort."""
    links = [
        {"url": f"https://mit.edu/program-{i}", "tier": 1, "text": "Program"}
        for i in range(150)
    ]
    cohorts = partition_links_into_cohorts(links, cohort_cap=250)
    assert len(cohorts) == 1
    assert cohorts[0].name == "unified"
    assert cohorts[0].source_count == 150
    assert not cohorts[0].evict_degree_sources


def test_partition_links_exceeding_platform_limit():
    """Links > 300 should split into typed staged cohorts."""
    links = []
    # Baseline
    links.extend([{"url": f"https://mit.edu/admissions-{i}", "tier": 2, "text": "Admissions"} for i in range(20)])
    links.extend([{"url": f"https://mit.edu/contact-{i}", "tier": 4, "text": "Contact"} for i in range(10)])
    # Tier 3
    links.extend([{"url": f"https://mit.edu/faculty-{i}", "tier": 3, "text": "School of Eng"} for i in range(30)])
    # Tier 1 Bachelors
    links.extend([{"url": f"https://mit.edu/bachelor-{i}", "tier": 1, "text": "Bachelor of Science"} for i in range(120)])
    # Tier 1 Masters
    links.extend([{"url": f"https://mit.edu/master-{i}", "tier": 1, "text": "Master of Science"} for i in range(150)])
    # Tier 1 PhD
    links.extend([{"url": f"https://mit.edu/phd-{i}", "tier": 1, "text": "PhD Program"} for i in range(50)])

    assert len(links) == 380

    cohorts = partition_links_into_cohorts(links, cohort_cap=250)
    assert len(cohorts) >= 3

    cohort_names = [c.name for c in cohorts]
    assert "undergraduate" in cohort_names
    assert any("masters" in n for n in cohort_names)
    assert "doctoral_specialized" in cohort_names

    for c in cohorts:
        # No single cohort may exceed 250 sources
        assert c.source_count <= 250
        # Each cohort must retain baseline Tier 2/4 links
        assert any(l["tier"] == 2 for l in c.links)
        assert any(l["tier"] == 4 for l in c.links)
