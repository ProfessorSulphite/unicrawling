"""
What ingest_university_sources guarantees to Phase 3: a tier mapping rather than a
bare count, notebook reuse instead of a duplicate, failed uploads recorded without
aborting the batch, and the per-notebook source cap enforced.

Moved out of tests/test_pipeline.py in C15. Pre-flight filtering and the text
fallback live in test_ingest_resilience.py; health sampling in test_health_sampling.py.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.ingestor.source_management import ingest_university_sources


def _mock_client():
    client = MagicMock()
    nb = MagicMock(); nb.id = "nb-123"; nb.title = "NUST_Counseling_DB"
    client.notebooks.list = AsyncMock(return_value=[])
    client.notebooks.create = AsyncMock(return_value=nb)
    client.notebooks.delete = AsyncMock()
    counter = {"n": 0}

    async def _add_url(notebook_id, url, **kw):
        counter["n"] += 1
        src = MagicMock(); src.id = f"src-{counter['n']}"
        return src

    client.sources.add_url = AsyncMock(side_effect=_add_url)
    client.sources.wait_for_sources = AsyncMock(side_effect=lambda **kw: [MagicMock()] * len(kw["source_ids"]))
    return client


async def test_ingest_returns_tier_mapping():
    client = _mock_client()
    links = [
        {"url": "https://nust.edu.pk/bs-cs", "tier": 1},
        {"url": "https://nust.edu.pk/fees", "tier": 2},
        {"url": "https://nust.edu.pk/faculties", "tier": 3},
    ]
    res = await ingest_university_sources("nust", "NUST", links, client=client)
    assert res.notebook_id == "nb-123"
    assert res.ingested_count == 3
    assert res.tier_histogram() == {1: 1, 2: 1, 3: 1}
    assert len(res.source_ids_for_tiers([1, 2])) == 2


async def test_ingest_reuses_existing_notebook():
    """Retrying a crashed run must not leak a second notebook per university."""
    client = _mock_client()
    existing = MagicMock(); existing.id = "nb-existing"; existing.title = "NUST_Counseling_DB"
    client.notebooks.list = AsyncMock(return_value=[existing])
    res = await ingest_university_sources("nust", "NUST", [{"url": "https://x", "tier": 1}], client=client)
    assert res.notebook_id == "nb-existing"
    client.notebooks.create.assert_not_called()


async def test_ingest_records_failed_uploads_without_aborting():
    client = _mock_client()
    calls = {"n": 0}

    async def flaky(notebook_id, url, **kw):
        calls["n"] += 1
        if "bad" in url:
            raise RuntimeError("upload rejected")
        src = MagicMock(); src.id = f"src-{calls['n']}"
        return src

    client.sources.add_url = AsyncMock(side_effect=flaky)
    links = [{"url": "https://ok-1", "tier": 1},
             {"url": "https://bad", "tier": 1},
             {"url": "https://ok-2", "tier": 2}]
    res = await ingest_university_sources("nust", "NUST", links, client=client)
    assert res.ingested_count == 2
    assert res.failed_urls == ["https://bad"]


async def test_ingest_respects_source_cap():
    client = _mock_client()
    links = [{"url": f"https://x/{i}", "tier": 1} for i in range(200)]
    res = await ingest_university_sources("nust", "NUST", links, client=client, max_sources=60)
    assert res.ingested_count == 60


async def test_ingest_rejects_missing_client():
    """NotebookLMClient() cannot be built without AuthTokens; fail loudly, not at runtime."""
    with pytest.raises(ValueError):
        await ingest_university_sources("nust", "NUST", [{"url": "https://x", "tier": 1}], client=None)
