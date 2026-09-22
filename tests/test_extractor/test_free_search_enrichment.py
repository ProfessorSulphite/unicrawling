from unittest.mock import AsyncMock, patch
import pytest

from src.extractor.crawlers.free_search_enrichment import (
    _clean_ddg_url,
    search_duckduckgo_html,
    free_search_find_portal,
    free_search_find_tuition_source,
    exa_find_application_portal,
)
from src.extractor.crawlers.exa_enriching import exa_find_application_portal as exa_module_func


MOCK_DDG_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="result results_links">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fadmissions.cornell.edu%2Fapply&amp;rut=1">Cornell Admissions Apply Now</a>
    <a class="result__snippet">Apply to Cornell University through the undergraduate portal.</a>
</div>
<div class="result results_links">
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.usnews.com%2Fbest-colleges%2Fcornell-university-2711&amp;rut=1">Cornell University US News</a>
    <a class="result__snippet">Tuition and admissions ranking for Cornell.</a>
</div>
</body>
</html>
"""


def test_clean_ddg_url():
    """Verify DuckDuckGo redirect wrapper stripping and URL unquoting."""
    raw = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fadmissions.cornell.edu%2Fapply&rut=1"
    cleaned = _clean_ddg_url(raw)
    assert cleaned == "https://admissions.cornell.edu/apply"

    # Already clean URL
    plain = "https://mit.edu/admissions"
    assert _clean_ddg_url(plain) == plain


@pytest.mark.asyncio
async def test_search_duckduckgo_html_parsing():
    """Verify parsing of DuckDuckGo HTML results."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.text = MOCK_DDG_HTML

        results = await search_duckduckgo_html("Cornell apply online", max_results=5)
        assert len(results) == 2
        assert results[0]["title"] == "Cornell Admissions Apply Now"
        assert results[0]["url"] == "https://admissions.cornell.edu/apply"
        assert "undergraduate portal" in results[0]["snippet"]

        assert results[1]["url"] == "https://www.usnews.com/best-colleges/cornell-university-2711"


@pytest.mark.asyncio
async def test_free_search_find_portal_prioritizes_official_domain():
    """Verify official domain candidate is selected over aggregators."""
    mock_results = [
        {"title": "Aggregator", "url": "https://www.shiksha.com/cornell-apply", "snippet": "Portal info"},
        {"title": "Official Portal", "url": "https://admissions.cornell.edu/applicant-portal", "snippet": "Apply official"},
    ]
    with patch("src.extractor.crawlers.free_search_enrichment.search_duckduckgo_html", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = mock_results

        portal = await free_search_find_portal("cornell.edu", "Cornell University")
        assert portal == "https://admissions.cornell.edu/applicant-portal"


@pytest.mark.asyncio
async def test_free_search_find_tuition_source_tiering():
    """Verify tier 1 official domain vs tier 2 reputable aggregator distinction."""
    # 1. Official domain present -> Tier 1
    with patch("src.extractor.crawlers.free_search_enrichment.search_duckduckgo_html", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = [
            {"title": "MIT Tuition", "url": "https://registrar.mit.edu/tuition-fees", "snippet": "Cost info"},
        ]
        src = await free_search_find_tuition_source("mit.edu", "MIT")
        assert src is not None
        assert src["tier"] == 1
        assert "registrar.mit.edu" in src["url"]

    # 2. Only aggregator present -> Tier 2
    with patch("src.extractor.crawlers.free_search_enrichment.search_duckduckgo_html", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = [
            {"title": "US News MIT Fees", "url": "https://www.usnews.com/best-colleges/mit/paying", "snippet": "Tuition cost"},
        ]
        src = await free_search_find_tuition_source("mit.edu", "MIT")
        assert src is not None
        assert src["tier"] == 2
        assert "usnews.com" in src["url"]


@pytest.mark.asyncio
async def test_exa_find_application_portal_delegates_to_free_search():
    """When Exa key is absent, calling exa_find_application_portal delegates to free search."""
    with patch("src.config.config.exa_api_key", ""), \
         patch("src.extractor.crawlers.free_search_enrichment.free_search_find_portal", new_callable=AsyncMock) as mock_free:
        mock_free.return_value = "https://admissions.itu.edu.pk/apply"

        portal = await exa_module_func("itu.edu.pk", "ITU")
        assert portal == "https://admissions.itu.edu.pk/apply"
        mock_free.assert_called_once_with("itu.edu.pk", "ITU")
