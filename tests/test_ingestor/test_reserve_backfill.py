"""
The link reserve: refilling notebook slots that pre-flight empties.

Phase 2 probes every selected link and drops the ones that no longer resolve.
Before the reserve the notebook simply ended up smaller -- COMSATS was ingested
with 41 of the 80 links Phase 1 had crawled, filtered, scored and tiered for it,
and every Phase 3 answer was grounded in that reduced corpus.
"""
from unittest.mock import MagicMock

import pytest

from src.config import config
from src.ingestor.source_management import ingest_university_sources

pytestmark = pytest.mark.asyncio


def _client():
    """A NotebookLM client that accepts every notebook and every source."""
    client = MagicMock()
    notebook = MagicMock()
    notebook.id = "nb-reserve"
    notebook.title = "Test_Counseling_DB"

    async def create(*a, **k):
        return notebook

    async def list_notebooks(*a, **k):
        return []

    async def add_url(notebook_id, url):
        src = MagicMock()
        src.id = f"src::{url}"
        return src

    async def wait_for_sources(*a, **k):
        return []

    client.notebooks.create = create
    client.notebooks.list = list_notebooks
    client.sources.add_url = add_url
    client.sources.wait_for_sources = wait_for_sources
    return client


def _links(selected, reserve):
    return (
        [{"url": u, "tier": 1, "selected": True} for u in selected]
        + [{"url": u, "tier": 1, "selected": False} for u in reserve]
    )


async def test_a_dead_selected_link_is_replaced_from_the_reserve(monkeypatch):
    dead = "https://uni.edu.pk/gone"

    async def probe(url, *a, **kw):
        return url != dead

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", probe)

    result = await ingest_university_sources(
        uni_slug="uni", uni_name="Uni", client=_client(),
        links=_links(["https://uni.edu.pk/a", dead], ["https://uni.edu.pk/spare"]),
    )

    ingested = [s.url for s in result.sources]
    assert ingested == ["https://uni.edu.pk/a", "https://uni.edu.pk/spare"]
    assert result.failed_urls == [dead]


async def test_a_healthy_university_never_touches_its_reserve(monkeypatch):
    """
    The reserve costs an HTTP probe per link drawn. A university whose selection
    is entirely alive must pay nothing for the mechanism -- not one extra probe.
    """
    probed = []

    async def probe(url, *a, **kw):
        probed.append(url)
        return True

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", probe)

    result = await ingest_university_sources(
        uni_slug="uni", uni_name="Uni", client=_client(),
        links=_links(["https://uni.edu.pk/a", "https://uni.edu.pk/b"],
                     ["https://uni.edu.pk/spare"]),
    )

    assert [s.url for s in result.sources] == ["https://uni.edu.pk/a", "https://uni.edu.pk/b"]
    assert "https://uni.edu.pk/spare" not in probed, "the reserve was probed for nothing"


async def test_the_reserve_is_drawn_in_rank_order(monkeypatch):
    """Phase 1 ranked the reserve; a backfill must respect that, not shuffle it."""
    # The health gate fires first and would refuse a selection this dead --
    # correctly, and that is its own test. This one is about the order the
    # reserve is drawn in once backfill is actually reached.
    monkeypatch.setattr(config, "health_check_enabled", False)

    async def probe(url, *a, **kw):
        return "dead" not in url

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", probe)

    result = await ingest_university_sources(
        uni_slug="uni", uni_name="Uni", client=_client(),
        links=_links(
            ["https://uni.edu.pk/dead-1", "https://uni.edu.pk/dead-2"],
            ["https://uni.edu.pk/r1", "https://uni.edu.pk/r2", "https://uni.edu.pk/r3"],
        ),
    )

    assert [s.url for s in result.sources] == ["https://uni.edu.pk/r1", "https://uni.edu.pk/r2"]


async def test_an_exhausted_reserve_is_not_an_error(monkeypatch):
    """Backfill is best-effort: a short notebook still beats no notebook."""
    monkeypatch.setattr(config, "health_check_enabled", False)

    async def probe(url, *a, **kw):
        return "good" in url

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", probe)

    result = await ingest_university_sources(
        uni_slug="uni", uni_name="Uni", client=_client(),
        links=_links(["https://uni.edu.pk/good", "https://uni.edu.pk/dead-1",
                      "https://uni.edu.pk/dead-2"],
                     ["https://uni.edu.pk/dead-3"]),
    )

    assert [s.url for s in result.sources] == ["https://uni.edu.pk/good"]
    assert not result.skipped


async def test_links_the_source_cap_displaces_become_reserve(monkeypatch):
    """
    The cap used to truncate with `links[:cap]`, discarding the remainder
    outright. Those links are as vetted as any other and are exactly what a
    backfill wants.
    """
    monkeypatch.setattr(config, "max_sources_per_notebook", 2)

    async def probe(url, *a, **kw):
        return url != "https://uni.edu.pk/b"

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", probe)

    result = await ingest_university_sources(
        uni_slug="uni", uni_name="Uni", client=_client(),
        links=_links(["https://uni.edu.pk/a", "https://uni.edu.pk/b",
                      "https://uni.edu.pk/c"], []),
    )

    # 'c' was pushed out by the cap, then recalled to replace the dead 'b'.
    assert [s.url for s in result.sources] == ["https://uni.edu.pk/a", "https://uni.edu.pk/c"]


async def test_a_partition_without_the_flag_behaves_exactly_as_before(monkeypatch):
    """
    Older partitions, the flat-file fallback and bare strings record no
    `selected` key. All of them must still be treated as the selection.
    """
    async def probe(url, *a, **kw):
        return True

    monkeypatch.setattr("src.ingestor.source_management.check_url_accessible", probe)

    result = await ingest_university_sources(
        uni_slug="uni", uni_name="Uni", client=_client(),
        links=[{"url": "https://uni.edu.pk/a", "tier": 1},
               "https://uni.edu.pk/b"],
    )

    assert [s.url for s in result.sources] == ["https://uni.edu.pk/a", "https://uni.edu.pk/b"]
