"""
The embedding model name and batch size are a real contract with Config.

Before C14 the scorer hardcoded both and Config's fields were dead -- and the two
had silently diverged onto different models with different vector dimensions
(bge-small, 384-dim, in the code; bge-base, 768-dim, in Config). C11b pinned them
together with a source-text check; these tests replace that with the real thing:
they observe what the scorer actually asks for.

No real model is ever loaded. A fake stands in, which is also what keeps the suite
at ~7s -- loading bge-small takes several seconds on its own.
"""
import numpy as np
import pytest

from src.config import config
from src.extractor.linkers import semantic_scoring


@pytest.fixture(autouse=True)
def _clear_model_singleton():
    """The loader caches process-wide; isolate each test from the others."""
    semantic_scoring._EMBEDDING_MODEL = None
    yield
    semantic_scoring._EMBEDDING_MODEL = None


class FakeModel:
    """Records how it was called; returns shapes the scorer can consume."""

    def __init__(self, name="fake"):
        self.name = name
        self.encode_calls = []

    def encode(self, texts, **kwargs):
        self.encode_calls.append((list(texts), kwargs))
        return np.zeros((len(texts), 4), dtype="float32")

    def similarity(self, a, b):
        return np.full((len(a), len(b)), 0.9, dtype="float32")


def _links(n=3):
    return [
        {
            "href": f"https://uni.test/programs/bs-cs-{i}",
            "text": f"BS Computer Science {i}",
            "raw_text": f"BS Computer Science {i}",
            "path_words": "programs bs cs",
        }
        for i in range(n)
    ]


# ------------------------------------------------------------- model name --

def test_loader_asks_for_the_configured_model(monkeypatch):
    seen = []

    def fake_ctor(name, *a, **kw):
        seen.append(name)
        return FakeModel(name)

    monkeypatch.setattr(semantic_scoring, "SentenceTransformer", fake_ctor)
    semantic_scoring._get_embedding_model()

    assert seen == [config.embedding_model_name]


def test_changing_the_configured_model_changes_what_is_loaded(monkeypatch):
    # Proves the loader reads config rather than happening to match its default.
    seen = []
    monkeypatch.setattr(
        semantic_scoring, "SentenceTransformer", lambda name, *a, **kw: seen.append(name) or FakeModel(name)
    )
    monkeypatch.setattr(config, "embedding_model_name", "BAAI/bge-large-en-v1.5")
    semantic_scoring._get_embedding_model()

    assert seen == ["BAAI/bge-large-en-v1.5"]


def test_model_is_loaded_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(
        semantic_scoring, "SentenceTransformer", lambda name, *a, **kw: calls.append(name) or FakeModel(name)
    )
    first = semantic_scoring._get_embedding_model()
    second = semantic_scoring._get_embedding_model()

    assert first is second
    assert len(calls) == 1, "the scorer reloaded the model instead of reusing it"


# ------------------------------------------------------------- batch size --

def _encode_kwargs_for_links(monkeypatch, fake):
    monkeypatch.setattr(semantic_scoring, "_get_embedding_model", lambda: fake)
    semantic_scoring.classify_and_score_links(_links(), threshold=0.45, uptodate=False)
    # Two encode calls: the counselor keywords, then the links. Only the link side
    # is batched -- that is the side whose size scales with a university's crawl.
    assert len(fake.encode_calls) == 2
    return fake.encode_calls[1][1]


def test_links_are_encoded_at_the_configured_batch_size(monkeypatch):
    kwargs = _encode_kwargs_for_links(monkeypatch, FakeModel())
    assert kwargs["batch_size"] == config.embedding_batch_size


def test_changing_the_configured_batch_size_changes_the_encode_call(monkeypatch):
    monkeypatch.setattr(config, "embedding_batch_size", 7)
    kwargs = _encode_kwargs_for_links(monkeypatch, FakeModel())
    assert kwargs["batch_size"] == 7


def test_keyword_side_carries_the_query_prefix_and_the_link_side_does_not(monkeypatch):
    # bge-* models are asymmetric: the retrieval instruction belongs on the query
    # side only. Encoding both sides bare collapses the spread --threshold is tuned
    # against, so this is a scoring-correctness guard, not a style one.
    fake = FakeModel()
    monkeypatch.setattr(semantic_scoring, "_get_embedding_model", lambda: fake)
    semantic_scoring.classify_and_score_links(_links(), threshold=0.45, uptodate=False)

    keyword_texts, _ = fake.encode_calls[0]
    link_texts, _ = fake.encode_calls[1]
    assert all(t.startswith(config.bge_query_prefix) for t in keyword_texts)
    assert not any(t.startswith(config.bge_query_prefix) for t in link_texts)


# ------------------------------------------------------------- Jev Fan-out tests --

@pytest.mark.asyncio
async def test_classify_and_score_links_jev_with_mock(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.extractor.linkers.semantic_scoring import classify_and_score_links_jev

    mock_resp = MagicMock()
    mock_choice = MagicMock()
    mock_choice.choice = "tier_1_programs"
    mock_noul = MagicMock()
    mock_noul.noul = 0.90
    mock_resp.choices = {"tier_0": mock_choice}
    mock_resp.nouls = {"rel_0": mock_noul}

    links = [{
        "href": "https://uni.edu.pk/programs/bs-cs",
        "text": "BS Computer Science",
        "raw_text": "BS Computer Science",
        "path_words": "programs bs cs",
    }]

    with patch("src.extractor.linkers.semantic_scoring.evaluate_system_one", new_callable=AsyncMock) as mock_eval:
        mock_eval.return_value = mock_resp
        scored = await classify_and_score_links_jev(links, threshold=0.60, uptodate=False)
        assert len(scored) == 1
        assert scored[0]["category"] == "Tier 1: Bachelor & Master Programs"
        assert scored[0]["priority_tier_num"] == 1
        assert scored[0]["passed_threshold"] is True


@pytest.mark.live
@pytest.mark.asyncio
async def test_classify_and_score_links_jev_live():
    from src.extractor.linkers.semantic_scoring import classify_and_score_links_jev
    from src.utilities.typesafe_client import is_typesafe_available

    if not is_typesafe_available():
        pytest.skip("TYPESAFE_API_KEY required for live test")

    links = [
        {
            "href": "https://itu.edu.pk/academics/bs-artificial-intelligence",
            "text": "BS in Artificial Intelligence Curriculum",
            "raw_text": "BS in Artificial Intelligence Curriculum",
            "path_words": "academics bs artificial intelligence",
        },
        {
            "href": "https://itu.edu.pk/admissions/fee-structure",
            "text": "Undergraduate Fee Structure 2026",
            "raw_text": "Undergraduate Fee Structure 2026",
            "path_words": "admissions fee structure",
        },
        {
            "href": "https://itu.edu.pk/news/annual-sports-day-2025",
            "text": "Annual Sports Gala Highlights",
            "raw_text": "Annual Sports Gala Highlights",
            "path_words": "news annual sports day",
        },
    ]

    scored = await classify_and_score_links_jev(links, threshold=0.50, uptodate=False)
    assert len(scored) >= 2
    # Verify BS program is Tier 1
    bs_item = next((s for s in scored if "bs-artificial" in s["href"]), None)
    assert bs_item is not None
    assert bs_item["priority_tier_num"] == 1
    # Verify fee structure is Tier 2
    fee_item = next((s for s in scored if "fee-structure" in s["href"]), None)
    assert fee_item is not None
    assert fee_item["priority_tier_num"] == 2
