"""
Unit tests for Ingestion Resilience, URL Sanitization, and Pre-flight HTTP Verification (src/ingest.py)
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.ingestor.source_management import (
    sanitize_url,
    check_url_accessible,
    ingest_university_sources,
    IngestedSource,
)


def test_sanitize_url():
    # Strips trailing hyphens, commas, dots, slashes
    assert sanitize_url("https://asab.nust.edu.pk/program/bs-biotechnology-for-fall-2024-entry-") == "https://asab.nust.edu.pk/program/bs-biotechnology-for-fall-2024-entry"
    assert sanitize_url("https://nust.edu.pk/academics/---") == "https://nust.edu.pk/academics"
    assert sanitize_url("https://itu.edu.pk/program/bs-cs.") == "https://itu.edu.pk/program/bs-cs"
    assert sanitize_url("https://itu.edu.pk/program/bs-cs,") == "https://itu.edu.pk/program/bs-cs"
    assert sanitize_url("https://nust.edu.pk/") == "https://nust.edu.pk/"
    assert sanitize_url("") == ""


@pytest.mark.asyncio
async def test_check_url_accessible(monkeypatch):
    class MockResponse:
        def __init__(self, status_code):
            self.status_code = status_code

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def head(self, url, *args, **kwargs):
            if "accessible" in url:
                return MockResponse(200)
            return MockResponse(403)
        async def get(self, url, *args, **kwargs):
            if "accessible" in url:
                return MockResponse(200)
            return MockResponse(404)

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    assert await check_url_accessible("https://live-test.org/accessible") is True
    assert await check_url_accessible("https://live-test.org/blocked") is False


@pytest.mark.asyncio
async def test_ingest_preflight_filtering(monkeypatch):
    # Mock NotebookLM client
    mock_client = MagicMock()
    mock_nb = MagicMock()
    mock_nb.id = "nb-test-123"
    mock_nb.title = "NUST_Counseling_DB"

    mock_client.notebooks.list = AsyncMock(return_value=[mock_nb])
    mock_client.notebooks.create = AsyncMock(return_value=mock_nb)
    mock_client.sources.add_url = AsyncMock(return_value="src-999")
    mock_client.sources.wait_for_sources = AsyncMock(return_value=["src-999"])

    # Mock check_url_accessible: only allow 'good' link
    async def mock_accessible(url, timeout=5.0):
        return "good" in url

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", mock_accessible)

    links = [
        {"url": "https://uni-test.edu.pk/good-link-1", "tier": 1},
        {"url": "https://uni-test.edu.pk/bad-link-403-", "tier": 2},
    ]

    res = await ingest_university_sources(
        uni_slug="nust",
        uni_name="NUST",
        links=links,
        client=mock_client,
    )

    assert res.notebook_id == "nb-test-123"
    assert res.ingested_count == 1
    assert res.sources[0].url == "https://uni-test.edu.pk/good-link-1"
    assert len(res.failed_urls) == 1
    assert "https://uni-test.edu.pk/bad-link-403" in res.failed_urls


@pytest.mark.asyncio
async def test_ingest_text_fallback(monkeypatch):
    mock_client = MagicMock()
    mock_nb = MagicMock()
    mock_nb.id = "nb-fallback-123"
    mock_nb.title = "NUST_Counseling_DB"

    mock_client.notebooks.list = AsyncMock(return_value=[mock_nb])
    mock_client.notebooks.create = AsyncMock(return_value=mock_nb)
    # add_url fails with RPCError rpc_code=9
    mock_client.sources.add_url = AsyncMock(side_effect=RuntimeError("RPCError rpc_code=9"))
    # add_text succeeds
    mock_client.sources.add_text = AsyncMock(return_value="src-text-888")
    mock_client.sources.wait_for_sources = AsyncMock(return_value=["src-text-888"])

    # Mock check_url_accessible to return True
    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", AsyncMock(return_value=True))

    # Mock fetch_and_extract_text to return valid (title, text) tuple
    async def mock_extract_text(url, timeout=8.0):
        return "NUST Rankings", "National University of Sciences and Technology (NUST) degree requirements and curriculum program details."

    monkeypatch.setattr("src.ingestor.source_management.fetch_and_extract_text", mock_extract_text)

    links = [{"url": "https://nust.edu.pk/about-us/nust-rankings", "tier": 1}]

    res = await ingest_university_sources(
        uni_slug="nust",
        uni_name="NUST",
        links=links,
        client=mock_client,
    )

    assert res.ingested_count == 1
    assert res.sources[0].source_id == "src-text-888"
    mock_client.sources.add_text.assert_called_once()
