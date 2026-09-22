from urllib.parse import urlparse
import pytest

from src.utilities.naming import derive_uni_info
from src.extractor.linkers.filteration import is_satellite_campus_leak
from src.extractor.linkers.crawling import EXPAND_DOM_JS


def test_derive_uni_info_satellite_campuses():
    """Verify satellite and branch campus subdomains produce compound slugs and accurate names."""
    # Texas A&M Qatar
    name, slug, domain = derive_uni_info("https://qatar.tamu.edu")
    assert slug == "tamu-qatar"
    assert name == "Texas A&M University at Qatar"
    assert domain == "qatar.tamu.edu"

    # Weill Cornell
    name, slug, domain = derive_uni_info("https://weill.cornell.edu/admissions")
    assert slug == "cornell-weill"
    assert name == "Weill Cornell Medicine"
    assert domain == "weill.cornell.edu"

    # NYU Abu Dhabi
    name, slug, domain = derive_uni_info("https://abudhabi.nyu.edu")
    assert slug == "nyu-abudhabi"
    assert name == "NYU Abu Dhabi"
    assert domain == "abudhabi.nyu.edu"

    # Standard main campus
    name, slug, domain = derive_uni_info("https://www.cornell.edu")
    assert slug == "cornell"
    assert name == "CORNELL"
    assert domain == "cornell.edu"


def test_is_satellite_campus_leak_when_crawling_main_campus():
    """Main campus crawls should filter out links to independent satellite subdomains."""
    main_base = "https://www.tamu.edu"

    # Satellite subdomains should be detected as leaks
    assert is_satellite_campus_leak("https://qatar.tamu.edu/programs/bs-petroleum", main_base) is True
    assert is_satellite_campus_leak("https://galveston.tamu.edu/marine-biology", main_base) is True

    # Standard departmental and admissions subdomains are NOT leaks
    assert is_satellite_campus_leak("https://engineering.tamu.edu/academics", main_base) is False
    assert is_satellite_campus_leak("https://admissions.tamu.edu/apply", main_base) is False
    assert is_satellite_campus_leak("https://www.tamu.edu/academics/degrees", main_base) is False


def test_is_satellite_campus_leak_when_crawling_satellite_campus():
    """Satellite campus crawls should confine crawling strictly to the satellite domain."""
    satellite_base = "https://qatar.tamu.edu"

    # On-site satellite links should not be leaks
    assert is_satellite_campus_leak("https://qatar.tamu.edu/programs", satellite_base) is False
    assert is_satellite_campus_leak("https://qatar.tamu.edu/admissions/fees", satellite_base) is False

    # Main campus or another satellite should be filtered out
    assert is_satellite_campus_leak("https://www.tamu.edu/about", satellite_base) is True
    assert is_satellite_campus_leak("https://galveston.tamu.edu/programs", satellite_base) is True


def test_expand_dom_js_snippet():
    """Ensure SPA accordion/dropdown expander script targets details and aria-expanded buttons."""
    assert "details:not([open])" in EXPAND_DOM_JS
    assert "aria-expanded" in EXPAND_DOM_JS
