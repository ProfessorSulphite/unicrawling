"""
Tokenised path exclusion, URL sanitisation and dynamic year decay.

Moved out of tests/test_pipeline.py in C14. Every test here is a regression test
for a defect that was actually present in the shipped code, not a smoke test.
"""
import pytest

from src.extractor.linkers.filteration import (
    compute_year_decay_factor,
    dedupe_key,
    is_excluded_path,
    normalize_url,
    preprocess_and_filter_links,
)


# =============================================================================
# B5.1 -- Tokenized path exclusion
# =============================================================================

@pytest.mark.parametrize("url", [
    "https://nust.edu.pk/programs/bs-business-administration",
    "https://nust.edu.pk/programs/mba-executive",
    "https://lums.edu.pk/programs/bba",
    "https://nust.edu.pk/programs/bs-accounting-and-auditing",
    "https://nust.edu.pk/admissions/apply-online-portal",
    "https://portal.nust.edu.pk/apply",
    "https://nust.edu.pk/admissions/undergraduate-admission-portal",
    "https://nust.edu.pk/newsletter-for-prospective-students",
])
def test_tokenized_exclusion_keeps_academic_urls(url):
    """
    Substring matching deleted the pipeline's own deliverables:
    'admin' killed every business-administration/MBA URL, 'audit' killed auditing
    programmes, and 'portal' killed application_portal_url -- the field the data
    model marks PRIMARY FOCUS. A NUST crawl returned 0 hits for
    'administration|auditing' and 0 for 'portal' as a direct result.
    """
    assert is_excluded_path(url) is False, f"{url} must survive filtering"


@pytest.mark.parametrize("url", [
    "https://nust.edu.pk/wp-admin/edit.php",
    "https://nust.edu.pk/wp-login.php",
    "https://nust.edu.pk/news/2026/spring-convocation",
    "https://nust.edu.pk/events/",
    "https://nust.edu.pk/admin/",
    "https://lms.nust.edu.pk/course/view",
    "https://nust.edu.pk/student-portal/login",
    "https://nust.edu.pk/careers/vacancies",
    "https://nust.edu.pk/tenders/procurement-notice",
    "https://nust.edu.pk/privacy-policy",
])
def test_tokenized_exclusion_still_removes_noise(url):
    """Tokenisation must not weaken the Zero Garbage Policy it replaces."""
    assert is_excluded_path(url) is True, f"{url} must be excluded"


def test_exclude_keywords_are_token_matched_not_substring():
    """
    --exclude-keywords "news" must remove /news/ but not words that merely contain
    'news' as a substring. ('newsletter-signup' is deliberately NOT used as the
    control here -- it tokenises to {newsletter, signup} and is correctly dropped
    by the signup rule, which would make this assertion prove nothing.)
    """
    links = [
        {"href": "https://x.edu.pk/news/item-1", "text": "News"},
        {"href": "https://x.edu.pk/newsletter-for-prospective-students", "text": "Newsletter"},
        {"href": "https://x.edu.pk/events/gala", "text": "Events"},
        {"href": "https://x.edu.pk/programs/bs-cs", "text": "BS CS"},
    ]
    out = preprocess_and_filter_links(links, base_url="https://x.edu.pk",
                                         exclude_keywords="news|events")
    urls = [l["href"] for l in out]
    assert not any("/news/" in u for u in urls)
    assert not any("/events/" in u for u in urls)
    assert any("newsletter" in u for u in urls)
    assert any("bs-cs" in u for u in urls)


# =============================================================================
# B5.2 -- URL sanitisation
# =============================================================================

def test_url_sanitization_unescapes_html_entities():
    """'&amp;' appeared verbatim in committed output, producing unfetchable URLs."""
    got = normalize_url("https://nust.edu.pk/x?p=959&amp;post_type=scholarship")
    assert "&amp;" not in got
    assert "post_type=scholarship" in got


def test_url_sanitization_strips_zero_width_characters():
    """A U+200B inside an MBBS slug made that source unfetchable by NotebookLM."""
    got = normalize_url("https://nust.edu.pk/mbbs-​bachelor-of-medicine")
    assert "​" not in got
    assert got.endswith("/mbbs-bachelor-of-medicine")


def test_url_sanitization_collapses_double_slashes():
    got = normalize_url("https://sines.nust.edu.pk//program//bs-cs/")
    assert got == "https://sines.nust.edu.pk/program/bs-cs"


def test_url_sanitization_removes_tracking_params_and_fragment():
    got = normalize_url("https://nust.edu.pk/apply?utm_source=fb&fbclid=abc&id=7#section")
    assert "utm_source" not in got and "fbclid" not in got and "#" not in got
    assert "id=7" in got


def test_url_sanitization_rejects_unfetchable_schemes():
    for bad in ["mailto:x@y.pk", "tel:+92515", "javascript:void(0)"]:
        assert normalize_url(bad) is None


def test_dedupe_key_unifies_www_and_scheme_variants():
    """Otherwise one page consumes two of the 60 per-notebook source slots."""
    a = dedupe_key(normalize_url("https://www.nust.edu.pk/apply"))
    b = dedupe_key(normalize_url("http://nust.edu.pk/apply"))
    assert a == b


# =============================================================================
# B5.3 -- Dynamic year decay
# =============================================================================

def test_year_decay_boosts_current_and_future():
    assert compute_year_decay_factor("admissions-2026", now_year=2026)[0] > 1.0
    assert compute_year_decay_factor("admissions-2027", now_year=2026)[0] > 1.0


def test_year_decay_penalises_past_years_monotonically():
    """
    The old regexes classified 2024 as 'current' (202[4-7]) while the 'outdated'
    window stopped at 2023, so a fall-2024 link scored 1.15x and ranked #1 of 287
    during a 2026 run. Decay must now be strictly decreasing into the past.
    """
    f2025 = compute_year_decay_factor("intake-2025", now_year=2026)[0]
    f2024 = compute_year_decay_factor("intake-2024", now_year=2026)[0]
    f2023 = compute_year_decay_factor("intake-2023", now_year=2026)[0]
    assert 1.0 > f2025 > f2024 > f2023
    assert f2023 >= 0.25


def test_year_decay_ranks_2024_below_2026():
    """The exact inversion observed in the committed detailed report."""
    old = compute_year_decay_factor("bs-software-engineering-for-fall-2024", now_year=2026)[0]
    new = compute_year_decay_factor("bs-software-engineering-for-fall-2026", now_year=2026)[0]
    assert new > old


def test_year_decay_uses_latest_year_in_range():
    assert compute_year_decay_factor("session-2025-2026", now_year=2026)[0] > 1.0


def test_year_decay_treats_onward_ranges_as_current():
    """'fall-2025-onward' names the policy in force, not a historical intake."""
    assert compute_year_decay_factor("fall-2025-onward", now_year=2026)[0] > 1.0


def test_year_decay_bounds_the_onward_rescue():
    """
    A live NUST crawl surfaced 'for-fall-2023-onwards' and a 2022 variant. Granting
    those the full current-year boost would rank a four-year-old scheme above this
    year's, so the rescue is bounded: neutral beyond the grace window, never boosted.
    """
    stale = compute_year_decay_factor("bpa-for-fall-2022-onwards", now_year=2026)[0]
    fresh = compute_year_decay_factor("bba-for-2025-onwards", now_year=2026)[0]
    assert fresh > 1.0
    assert stale == 1.0
    # Still better than a bare historical year, which is what it actually is.
    assert stale > compute_year_decay_factor("bpa-for-fall-2022", now_year=2026)[0]
def test_year_decay_neutral_without_years():
    assert compute_year_decay_factor("programs/bs-computer-science")[0] == 1.0


def test_year_decay_is_not_hardcoded_to_2026():
    """Regression against re-introducing a fixed year window."""
    assert compute_year_decay_factor("intake-2030", now_year=2030)[0] > 1.0
    assert compute_year_decay_factor("intake-2026", now_year=2030)[0] < 1.0
