"""
Link selection: what the deeper crawl is allowed to bring back, and how the
candidates are ranked once it has.

Widening discovery only pays if selection gets sharper at the same time -- a
bigger candidate pool with the same filter is just more noise in the notebook.
"""
import pytest

from src.config import config
from src.extractor.linkers.filteration import is_excluded_path, preprocess_and_filter_links
from src.extractor.linkers.semantic_scoring import _crawl_score_normaliser


# ------------------------------------------------ on-site search result pages --

@pytest.mark.parametrize("url", [
    "https://www.comsats.edu.pk/search.aspx?q=research",
    "https://www.comsats.edu.pk/search.aspx?q=academic+programs",
    "https://uni.edu.pk/index.php?s=admission",
    "https://uni.edu.pk/results?query=bs+computer+science",
    "https://uni.edu.pk/find?keyword=fee",
])
def test_a_search_result_page_is_not_a_source(url):
    """
    A search URL renders a query, not a page. The 2026-09-05 COMSATS notebook
    spent two of its 41 slots on /search.aspx?q=research and
    /search.aspx?q=academic+programs -- two lists of titles, ingested as if they
    were content, grounding answers that the underlying pages should have.
    """
    assert is_excluded_path(url) is True


@pytest.mark.parametrize("url", [
    "https://uni.edu.pk/research/centres",
    "https://uni.edu.pk/researchers",
    "https://uni.edu.pk/programs?level=undergraduate",
    "https://uni.edu.pk/admissions?campus=lahore",
])
def test_the_search_rule_does_not_touch_real_pages(url):
    """
    "research" contains "search" and a legitimate page may well carry a query
    string. Only the query *key* decides, which is the same token-not-substring
    discipline the rest of this filter is built on.
    """
    assert is_excluded_path(url) is False


# ---------------------------------------------- the crawler's own opinion --

def test_the_crawler_score_survives_the_filter():
    """
    Crawl4AI scores every href it discovers, crawling.py harvested that score,
    and this filter then built a fresh dict without it -- so the work was paid
    for on every page of every crawl and thrown away here.
    """
    raw = [{"href": "https://itu.edu.pk/admissions", "text": "Admissions",
            "title": "", "crawl_score": 0.87}]
    clean = preprocess_and_filter_links(raw, base_url="https://itu.edu.pk")
    assert clean and clean[0]["crawl_score"] == 0.87


def test_the_crawler_score_ranks_but_does_not_decide():
    """
    Bounded to [1.0, 1.0 + crawl_score_weight]. It is a tie-breaker between links
    the embedding scores alike, not a second threshold.
    """
    links = [
        {"crawl_score": 0.0}, {"crawl_score": 0.5}, {"crawl_score": 1.0},
    ]
    boost = _crawl_score_normaliser(links)
    assert boost(links[0]) == pytest.approx(1.0)
    assert boost(links[2]) == pytest.approx(1.0 + config.crawl_score_weight)
    assert boost(links[0]) < boost(links[1]) < boost(links[2])


def test_a_batch_with_no_crawler_scores_is_left_alone():
    """
    Older partitions, the flat-file fallback and every unit-test fixture carry no
    score. "No opinion" must mean no adjustment, not a silent penalty.
    """
    links = [{"href": "https://x/a"}, {"href": "https://x/b", "crawl_score": None}]
    boost = _crawl_score_normaliser(links)
    assert boost(links[0]) == 1.0 and boost(links[1]) == 1.0


def test_a_batch_where_every_score_is_equal_is_left_alone():
    """A constant carries no ranking information; min-maxing it would divide by zero."""
    links = [{"crawl_score": 0.4}, {"crawl_score": 0.4}]
    boost = _crawl_score_normaliser(links)
    assert boost(links[0]) == 1.0


def test_the_weight_can_be_switched_off_entirely(monkeypatch):
    monkeypatch.setattr(config, "crawl_score_weight", 0.0)
    links = [{"crawl_score": 0.0}, {"crawl_score": 1.0}]
    boost = _crawl_score_normaliser(links)
    assert boost(links[0]) == boost(links[1]) == 1.0


# ---------------------------------------------------- discovery is wider now --

def test_discovery_is_configured_to_reach_past_the_top_level_menu():
    """
    Depth 2 over 15 pages reaches the landing page and what its menu links to.
    Programme pages usually sit one hop further in, behind a faculty index --
    which is why the 2026-09-05 batch found 41 usable links for COMSATS.
    """
    assert config.crawl_max_depth >= 3
    assert config.max_crawl_pages >= 30


# ------------------------------------------- structural tier overrides (C32) --
#
# Tier was decided purely by which of 23 keywords won cosine argmax, with no
# structural input at all. These pin the vote the URL path now gets, using the
# exact URLs from the ITU run `s_1` where the absence of that vote cost the
# payload its fee and deadline coverage.

from src.extractor.linkers.deduplication import get_discipline_tokens  # noqa: E402
from src.extractor.linkers.semantic_scoring import (  # noqa: E402
    DEMOTION_TIER,
    apply_structural_tier_rules,
    ensure_guaranteed_coverage,
)


@pytest.mark.parametrize("url", [
    "https://itu.edu.pk/merit-lists-2026/bs-software-engineering-1st-merit-list-2026",
    "https://itu.edu.pk/merit-lists-2026-2/bs-computer-science-2nd-merit-list-2026",
    "https://uni.edu.pk/results/spring-2026",
    "https://uni.edu.pk/notice-board/",
    "https://uni.edu.pk/faculty/profile/dr-someone",
    "https://uni.edu.pk/people/staff-directory",
])
def test_outcome_listings_and_directories_cannot_hold_a_programme_tier(url):
    """
    A merit list reads like an admissions page to an embedding, because it IS
    about admissions. 13 of ITU's 46 selected sources were merit lists holding
    Tier 1 or 2 -- slots the programme and fee queries read and found nothing
    in. Demoted rather than excluded: it is still real institutional content.
    """
    tier, reason = apply_structural_tier_rules(url, 1)
    assert tier == DEMOTION_TIER
    assert reason and reason.startswith("demoted")


@pytest.mark.parametrize("url", [
    "https://itu.edu.pk/financial-assistance",
    "https://uni.edu.pk/admissions/fee-structure",
    "https://uni.edu.pk/important-dates",
    "https://uni.edu.pk/how-to-apply",
    "https://uni.edu.pk/downloads/prospectus",
])
def test_fee_and_deadline_pages_are_pulled_into_a_tier_a_query_reads(url):
    """
    ITU's /financial-assistance landed in Tier 4, which NO programme query
    reads, because "scholarships" matched a Tier-4 phrase first. The page that
    would have answered the blocking field was ingested and never consulted.
    """
    tier, reason = apply_structural_tier_rules(url, 4)
    assert tier == 2
    assert reason and reason.startswith("promoted")


def test_demotion_beats_promotion():
    """
    /merit-lists-2026/bs-financial-technology-1st-merit-list matches BOTH rules:
    "financial" promotes, "merit-list" demotes. It is a merit list. Three of
    ITU's selected links have exactly this shape, and reading them as fee pages
    would re-create the original failure while appearing to fix it.
    """
    url = "https://itu.edu.pk/merit-lists-2026/bs-financial-technology-1st-merit-list-2026"
    tier, reason = apply_structural_tier_rules(url, 2)
    assert tier == DEMOTION_TIER
    assert "demoted" in reason


def test_an_ordinary_programme_page_is_left_alone():
    """The override is a floor and a ceiling, not a re-ranking of everything."""
    tier, reason = apply_structural_tier_rules("https://itu.edu.pk/admissions/bs-computer-science", 1)
    assert (tier, reason) == (1, None)


def test_the_same_programme_reached_three_ways_dedupes_to_one():
    """
    ITU published one BS Software Engineering merit list at three URLs that
    differ only in a year suffix, a faculty path segment and a round number.
    All three survived deduplication as distinct "programmes" -- dedup reduced
    96 links to 93 on that run.
    """
    urls = [
        "https://itu.edu.pk/merit-lists-2026/bs-software-engineering",
        "https://itu.edu.pk/merit-lists-2026/faculty-of-engineering/bs-software-engineering",
        "https://itu.edu.pk/merit-lists-2026-2/bs-software-engineering-2nd-round",
    ]
    assert len({get_discipline_tokens(u, "") for u in urls}) == 1


def test_distinct_programmes_still_do_not_collapse():
    """
    The stopword list grew; the exact-token-set rule must still keep
    electrical and electronic engineering apart, which is the defect the
    SequenceMatcher merge it replaced used to cause.
    """
    a = get_discipline_tokens("https://uni.edu.pk/programs/bs-electrical-engineering", "")
    b = get_discipline_tokens("https://uni.edu.pk/programs/bs-electronic-engineering", "")
    assert a != b


# ------------------------------------------------- guaranteed coverage (C32) --

def _link(url, tier=2, score=0.5):
    return {
        "href": url, "text": url, "raw_text": url, "matched_keyword": "k",
        "raw_similarity_score": score, "weighted_score": score,
        "category": f"Tier {tier}", "priority_tier_num": tier,
        "year_tag": "Current / Timeless", "passed_threshold": True,
    }


def test_a_fee_page_below_the_cut_is_promoted_into_the_selection():
    """
    A tier quota is a good default and a bad guarantee: it fills Tier 2 with
    whatever scored highest there. On ITU that was merit lists, and the page
    publishing the fee schedule ranked below the cut.
    """
    selected = [_link(f"https://uni.edu.pk/merit-lists-2026/p{i}", score=0.9) for i in range(5)]
    scored = selected + [_link("https://uni.edu.pk/fee-structure", score=0.2)]

    out = ensure_guaranteed_coverage(selected, scored, total_cap=5)
    assert "https://uni.edu.pk/fee-structure" in [i["href"] for i in out]
    assert len(out) == 5, "the selection size must not grow"


def test_a_promotion_never_evicts_another_promotion():
    selected = [
        _link("https://uni.edu.pk/fee-structure", score=0.9),
        _link("https://uni.edu.pk/random-page", score=0.8),
    ]
    scored = selected + [_link("https://uni.edu.pk/important-dates", score=0.1)]

    hrefs = [i["href"] for i in ensure_guaranteed_coverage(selected, scored, total_cap=2)]
    assert "https://uni.edu.pk/fee-structure" in hrefs
    assert "https://uni.edu.pk/important-dates" in hrefs


def test_nothing_to_promote_leaves_the_selection_untouched():
    selected = [_link("https://uni.edu.pk/programs/bs-cs")]
    assert ensure_guaranteed_coverage(selected, selected, total_cap=1) == selected


# ------------------------------------- documents in CMS upload dirs (C32) ----

def test_a_pdf_under_wp_content_is_not_treated_as_a_theme_asset():
    """
    `/wp-content/uploads/` is WordPress's DEFAULT upload path, so it is where a
    WordPress university keeps its fee schedules and admission calendars. The
    `wp-content` phrase is on the denylist to block themes, scripts and
    stylesheets -- every one of which the extension denylist already removes --
    so applying it to documents discarded exactly the files this pass exists to
    ingest, on every WordPress site, ITU included.
    """
    url = "https://itu.edu.pk/wp-content/uploads/2026/fee-structure-2026.pdf"
    assert is_excluded_path(url, is_document=True) is False
    # Unchanged for ordinary pages: a /wp-content/ HTML path is still noise.
    assert is_excluded_path(url) is True


def test_the_exemption_does_not_reopen_the_admin_surface():
    """Only asset DIRECTORIES are exempted, never the authenticated endpoints."""
    for url in ("https://uni.edu.pk/wp-login.php",
                "https://uni.edu.pk/wp-admin/options.php"):
        assert is_excluded_path(url, is_document=True) is True


def test_a_wordpress_fee_pdf_survives_the_whole_filter():
    """The end-to-end path, since the defect only showed up when composed."""
    clean = preprocess_and_filter_links(
        [{"href": "https://itu.edu.pk/wp-content/uploads/fee-structure-2026.pdf",
          "text": "Fee Structure 2026", "crawl_score": 0.8, "is_document": True}],
        base_url="https://itu.edu.pk",
    )
    assert [c["href"] for c in clean] == [
        "https://itu.edu.pk/wp-content/uploads/fee-structure-2026.pdf"
    ]
    assert clean[0]["is_document"] is True
