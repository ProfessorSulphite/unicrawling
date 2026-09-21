"""
Unit tests for autonomous academic department, school, and faculty hub discovery.
"""
import pytest
from unittest.mock import AsyncMock, patch

from src.config import config
from src.extractor.linkers.department_discovery import (
    classify_academic_hubs_jev,
    detect_academic_department_hubs,
    is_academic_hub_candidate,
    normalize_hub_url,
)
from src.extractor.linkers.runner import run_pipeline


# =============================================================================
# Candidate Hub Heuristics
# =============================================================================

def test_is_academic_hub_candidate_subdomains():
    base_url = "https://mit.edu"

    # Genuine academic subdomains
    assert is_academic_hub_candidate("https://mitsloan.mit.edu", "MIT Sloan School of Management", base_url)
    assert is_academic_hub_candidate("https://eecs.mit.edu/programs", "EECS Department", base_url)
    assert is_academic_hub_candidate("https://architecture.mit.edu", "School of Architecture", base_url)
    assert is_academic_hub_candidate("https://mitadmissions.org", "Admissions Office", base_url)

    # Operational / administrative noise subdomains
    assert not is_academic_hub_candidate("https://mail.mit.edu", "Webmail", base_url)
    assert not is_academic_hub_candidate("https://vpn.mit.edu", "VPN Login", base_url)
    assert not is_academic_hub_candidate("https://canvas.mit.edu", "Canvas LMS", base_url)
    assert not is_academic_hub_candidate("https://facilities.mit.edu", "Facilities & Maintenance", base_url)
    assert not is_academic_hub_candidate("https://parking.mit.edu", "Parking & Transportation", base_url)
    assert not is_academic_hub_candidate("https://dining.mit.edu", "Campus Dining", base_url)

    # Completely external domains
    assert not is_academic_hub_candidate("https://google.com", "Search", base_url)
    assert not is_academic_hub_candidate("https://harvard.edu", "Harvard", base_url)


def test_is_academic_hub_candidate_path_patterns():
    base_url = "https://mit.edu"

    # Path hubs on primary domain
    assert is_academic_hub_candidate("https://mit.edu/schools/engineering", "Engineering", base_url)
    assert is_academic_hub_candidate("https://mit.edu/departments/physics", "Physics", base_url)
    assert is_academic_hub_candidate("https://mit.edu/academics/schools/science", "Science", base_url)

    # Prominent anchor text phrases
    assert is_academic_hub_candidate("https://mit.edu/hms-programs", "School of Humanities", base_url)
    assert is_academic_hub_candidate("https://mit.edu/ug-apply", "Undergraduate Admissions", base_url)

    # Non-academic pages
    assert not is_academic_hub_candidate("https://mit.edu/about/leadership", "Leadership", base_url)
    assert not is_academic_hub_candidate("https://mit.edu/document.pdf", "School of Engineering Report", base_url)


# =============================================================================
# Hub URL Normalization
# =============================================================================

def test_normalize_hub_url():
    # Subdomains should resolve to root origin
    assert normalize_hub_url("https://mitsloan.mit.edu/admissions/programs/mba") == "https://mitsloan.mit.edu/"
    assert normalize_hub_url("http://eecs.mit.edu/academics?query=1#top") == "http://eecs.mit.edu/"

    # Path hubs should retain path up to department/school slug
    assert normalize_hub_url("https://mit.edu/schools/engineering/degrees/bs") == "https://mit.edu/schools/engineering"
    assert normalize_hub_url("https://mit.edu/academics/departments/physics/faculty") == "https://mit.edu/academics/departments/physics"


# =============================================================================
# Jev Classification & Fallback
# =============================================================================

@pytest.mark.asyncio
async def test_classify_academic_hubs_offline_fallback(monkeypatch):
    """When TypeSafe API is unavailable, verify heuristic keyword fallback."""
    monkeypatch.setattr("src.extractor.linkers.department_discovery.is_typesafe_available", lambda: False)

    candidates = [
        {"href": "https://mitsloan.mit.edu/", "text": "MIT Sloan School of Management"},
        {"href": "https://eecs.mit.edu/", "text": "Electrical Engineering & Computer Science"},
        {"href": "https://club.mit.edu/", "text": "Sailing Club"},
    ]

    verified = await classify_academic_hubs_jev(candidates)
    urls = [h["href"] for h in verified]
    assert "https://mitsloan.mit.edu/" in urls
    assert "https://eecs.mit.edu/" in urls
    assert "https://club.mit.edu/" not in urls


@pytest.mark.asyncio
async def test_classify_academic_hubs_jev_mock(monkeypatch):
    """When TypeSafe API is available, verify probability thresholding."""
    monkeypatch.setattr("src.extractor.linkers.department_discovery.is_typesafe_available", lambda: True)

    async def mock_noul(state, instructions):
        if "sloan" in state.lower():
            return 0.95
        if "athletics" in state.lower():
            return 0.15
        return 0.40

    monkeypatch.setattr("src.extractor.linkers.department_discovery.evaluate_noul", mock_noul)

    candidates = [
        {"href": "https://mitsloan.mit.edu/", "text": "MIT Sloan School"},
        {"href": "https://athletics.mit.edu/", "text": "MIT Athletics"},
    ]

    verified = await classify_academic_hubs_jev(candidates)
    assert len(verified) == 1
    assert verified[0]["href"] == "https://mitsloan.mit.edu/"


# =============================================================================
# End-to-End detect_academic_department_hubs
# =============================================================================

@pytest.mark.asyncio
async def test_detect_academic_department_hubs_end_to_end(monkeypatch):
    monkeypatch.setattr("src.extractor.linkers.department_discovery.is_typesafe_available", lambda: False)

    raw_links = [
        {"href": "https://mitsloan.mit.edu/admissions", "text": "Sloan School"},
        {"href": "https://eecs.mit.edu/undergraduate", "text": "Department of EECS"},
        {"href": "https://mail.mit.edu/inbox", "text": "Webmail"},
        {"href": "https://mit.edu/schools/science", "text": "School of Science"},
    ]

    hubs = await detect_academic_department_hubs(
        raw_links=raw_links,
        base_url="https://mit.edu",
        max_hubs=5,
    )

    assert any("mitsloan.mit.edu" in h for h in hubs)
    assert any("eecs.mit.edu" in h for h in hubs)
    assert any("schools/science" in h for h in hubs)
    assert not any("mail.mit.edu" in h for h in hubs)
    # metadecision/registry aliases for mit.edu should also be picked up
    assert any("mitadmissions.org" in h for h in hubs)


# =============================================================================
# Runner Pipeline Integration
# =============================================================================

@pytest.mark.asyncio
async def test_run_pipeline_department_fan_out(tmp_path, monkeypatch):
    """Test that run_pipeline triggers department discovery and merges results."""
    monkeypatch.setattr(config, "data_links_dir", tmp_path)

    # Mock crawl_site_links for root domain
    async def mock_root_crawl(start_url, max_pages=15):
        return [
            {"href": "https://mit.edu/academics", "text": "Academics", "title": "Academics", "crawl_score": 1.0},
            {"href": "https://mitsloan.mit.edu/mba", "text": "Sloan MBA", "title": "MBA", "crawl_score": 1.0},
        ]

    # Mock crawl_department_hubs
    async def mock_dept_crawl(dept_hubs, max_pages_per_hub=10, max_depth=2):
        return [
            {"href": "https://mitsloan.mit.edu/programs/masters", "text": "Master of Finance", "title": "MFin", "crawl_score": 1.0},
        ]

    monkeypatch.setattr("src.extractor.linkers.runner.crawl_site_links", mock_root_crawl)
    monkeypatch.setattr("src.extractor.linkers.runner.crawl_department_hubs", mock_dept_crawl)
    monkeypatch.setattr("src.extractor.linkers.department_discovery.is_typesafe_available", lambda: False)

    result = await run_pipeline(
        url="https://mit.edu",
        enable_dept_discovery=True,
        max_dept_hubs=3,
        output_links=str(tmp_path / "links.txt"),
        output_detailed=str(tmp_path / "detailed.txt"),
    )

    assert len(result["succeeded"]) == 1
    # Check that department links were merged into the pipeline results
    urls = [r["href"] for r in result["links"]]
    assert any("mitsloan.mit.edu" in u for u in urls)
