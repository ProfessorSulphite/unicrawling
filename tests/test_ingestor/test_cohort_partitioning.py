"""
Unit tests for degree-cohort partitioning for massive link corpora.
"""
import pytest

from src.ingestor.cohort_partitioning import (
    DegreeCohort,
    classify_admissions_degree_level,
    classify_link_degree_level,
    partition_links_into_cohorts,
)
from src.ingestor.notebook_lifecycle import patch_notebooklm_rpc_size_limit


def test_classify_link_degree_level():
    assert classify_link_degree_level({"url": "https://mit.edu/academics/bachelor-of-science"}) == "bachelors"
    assert classify_link_degree_level({"url": "https://mit.edu/degree-charts/course-6-eecs"}) == "bachelors"
    assert classify_link_degree_level({"url": "https://mit.edu/admissions/majors"}) == "bachelors"
    assert classify_link_degree_level({"url": "https://mit.edu/masters/mba-program"}) == "masters"
    assert classify_link_degree_level({"url": "https://mit.edu/graduate/ms-computer-science"}) == "masters"
    assert classify_link_degree_level({"url": "https://mit.edu/doctoral/phd-physics"}) == "phd"
    assert classify_link_degree_level({"url": "https://mit.edu/programs/postgraduate-diploma"}) == "diploma"
    assert classify_link_degree_level({"url": "https://mit.edu/about-us"}) is None


def test_classify_admissions_degree_level():
    assert classify_admissions_degree_level({"url": "https://mitadmissions.org/apply/firstyear/eligibility"}) == "bachelors"
    assert classify_admissions_degree_level({"url": "https://oge.mit.edu/graduate-admissions/applications"}) == "graduate"
    assert classify_admissions_degree_level({"url": "https://web.mit.edu/admissions-aid"}) is None


def test_partition_links_under_threshold():
    """Links <= 75 should return a single unified cohort."""
    links = [
        {"url": f"https://mit.edu/program-{i}", "tier": 1, "text": "Program"}
        for i in range(50)
    ]
    cohorts = partition_links_into_cohorts(links, cohort_cap=200)
    assert len(cohorts) == 1
    assert cohorts[0].name == "unified"
    assert cohorts[0].source_count == 50
    assert not cohorts[0].evict_degree_sources


def test_partition_links_exceeding_threshold():
    """Links > 75 with multiple degree levels should split into typed staged cohorts."""
    links = []
    # Baseline
    links.extend([{"url": f"https://mitadmissions.org/apply/firstyear-{i}", "tier": 2, "text": "Undergrad Admissions"} for i in range(15)])
    links.extend([{"url": f"https://oge.mit.edu/graduate-admissions-{i}", "tier": 2, "text": "Grad Admissions"} for i in range(15)])
    links.extend([{"url": f"https://mit.edu/contact-{i}", "tier": 4, "text": "Contact"} for i in range(10)])
    # Tier 3
    links.extend([{"url": f"https://mit.edu/faculty-{i}", "tier": 3, "text": "School of Eng"} for i in range(20)])
    # Tier 1 Bachelors
    links.extend([{"url": f"https://mit.edu/bachelor-{i}", "tier": 1, "text": "Bachelor of Science"} for i in range(40)])
    # Tier 1 Masters
    links.extend([{"url": f"https://mit.edu/master-{i}", "tier": 1, "text": "Master of Science"} for i in range(40)])
    # Tier 1 PhD
    links.extend([{"url": f"https://mit.edu/phd-{i}", "tier": 1, "text": "PhD Program"} for i in range(20)])

    assert len(links) == 160

    cohorts = partition_links_into_cohorts(links, cohort_cap=200)
    assert len(cohorts) == 3

    cohort_names = [c.name for c in cohorts]
    assert "undergraduate" in cohort_names
    assert "masters" in cohort_names
    assert "doctoral_specialized" in cohort_names

    for c in cohorts:
        # No single cohort may exceed 200 sources
        assert c.source_count <= 200
        # Each cohort must retain baseline Tier 4 contact links
        assert any(l["tier"] == 4 for l in c.links)


def test_patch_notebooklm_rpc_size_limit():
    """Test that patch_notebooklm_rpc_size_limit successfully patches notebooklm-py."""
    import notebooklm._kernel
    import notebooklm._streaming_post

    patch_notebooklm_rpc_size_limit(200 * 1024 * 1024)
    assert getattr(notebooklm._kernel.stream_post_with_size_cap, "_is_patched", False)
    assert getattr(notebooklm._streaming_post.stream_post_with_size_cap, "_is_patched", False)


@pytest.mark.asyncio
async def test_patch_notebooklm_kernel_post_execution():
    """Verify that kernel.post forwards keyword-only args without TypeError."""
    from unittest.mock import AsyncMock, MagicMock
    import notebooklm._kernel

    patch_notebooklm_rpc_size_limit(200 * 1024 * 1024)

    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {}
    mock_response.aiter_bytes = MagicMock(return_value=[b")]}'\n", b"123\n", b'["wrb.fr", null, "{}"]\n'].__iter__())

    mock_client = MagicMock()
    mock_client.stream = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(return_value=mock_response), __aexit__=AsyncMock()))

    kernel = notebooklm._kernel.Kernel()
    kernel._http_client = mock_client

    # Should execute cleanly without TypeError: takes 2 positional arguments
    await kernel.post(
        "https://notebooklm.google.com/test",
        headers={"content-type": "application/json"},
        body="test_body",
        read_timeout=30.0,
    )


