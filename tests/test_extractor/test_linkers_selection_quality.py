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
