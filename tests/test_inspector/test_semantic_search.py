"""
Unit and Live Integration Tests for Semantic Counselor Search & Intent Routing.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.inspector.semantic_search import (
    rerank_program_candidates,
    route_search_intent,
)
from src.utilities.typesafe_client import is_typesafe_available


# =============================================================================
# Offline Unit Tests (Mocks)
# =============================================================================

@pytest.mark.asyncio
async def test_route_search_intent_mock():
    """Verify that search intent parses degree level correctly."""
    with patch("src.inspector.semantic_search.is_typesafe_available", return_value=True), \
         patch("src.inspector.semantic_search.evaluate_choice", new_callable=AsyncMock) as mock_choice:
        # Mock choice for masters
        mock_choice.return_value = ("masters", 0.95, {})
        intent = await route_search_intent("masters in data science")
        assert intent["level"] == "masters"

        # Mock choice for bachelors
        mock_choice.return_value = ("bachelors", 0.90, {})
        intent = await route_search_intent("undergraduate software engineering")
        assert intent["level"] == "bachelors"

        # Mock choice for any / unconstrained
        mock_choice.return_value = ("any", 0.85, {})
        intent = await route_search_intent("top universities in lahore")
        assert intent["level"] is None


@pytest.mark.asyncio
async def test_rerank_program_candidates_mock():
    """Verify candidate reranking based on Jev Score fit."""
    candidates = [
        {"program_name": "BS History", "category": "BS / BSc", "tuition_fee": "80,000 PKR"},
        {"program_name": "MS Computer Science", "category": "MS / MSc", "tuition_fee": "150,000 PKR"},
    ]

    with patch("src.inspector.semantic_search.is_typesafe_available", return_value=True), \
         patch("src.inspector.semantic_search.evaluate_score", new_callable=AsyncMock) as mock_score:
        # Score returns (score, confidence, map)
        # First candidate (BS History) gets 1.0, second candidate (MS CS) gets 4.8
        mock_score.side_effect = [(1.0, 0.9, {}), (4.8, 0.95, {})]

        reranked = await rerank_program_candidates("masters in computer science", candidates)
        assert len(reranked) == 2
        assert reranked[0]["program_name"] == "MS Computer Science"
        assert reranked[0]["fit_score"] == 4.8
        assert reranked[1]["program_name"] == "BS History"


@pytest.mark.asyncio
async def test_typesafe_disabled_intent_and_reranking():
    """Verify fallback behavior when TypeSafe is unavailable."""
    with patch("src.inspector.semantic_search.is_typesafe_available", return_value=False):
        intent = await route_search_intent("phd artificial intelligence")
        assert intent["level"] is None

        candidates = [{"program_name": "BS CS"}, {"program_name": "MS CS"}]
        reranked = await rerank_program_candidates("cs", candidates)
        assert len(reranked) == 2
        assert reranked[0]["program_name"] == "BS CS"


# =============================================================================
# Real API Live Integration Tests
# =============================================================================

@pytest.mark.live
@pytest.mark.skipif(
    not is_typesafe_available(),
    reason="Requires typesafe-sdk and TYPESAFE_API_KEY",
)
@pytest.mark.asyncio
async def test_live_route_search_intent():
    """Live API test: Route student query to masters degree level."""
    query = "Looking for affordable masters programs in computer science"
    intent = await route_search_intent(query)
    assert intent["level"] == "masters"


@pytest.mark.live
@pytest.mark.skipif(
    not is_typesafe_available(),
    reason="Requires typesafe-sdk and TYPESAFE_API_KEY",
)
@pytest.mark.asyncio
async def test_live_rerank_program_candidates():
    """Live API test: Rerank matching programs by student inquiry relevance."""
    query = "Looking for an advanced graduate degree in artificial intelligence and machine learning"
    candidates = [
        {
            "program_name": "Bachelor of Arts in English",
            "category": "BS / BSc",
            "university": "Sample University",
            "department": "Humanities",
            "tuition_fee": "90,000 PKR",
            "description": "Undergraduate degree in English literature and composition.",
        },
        {
            "program_name": "Master of Science in Artificial Intelligence",
            "category": "MS / MSc",
            "university": "Sample University",
            "department": "Computer Science",
            "tuition_fee": "160,000 PKR",
            "description": "Advanced research degree covering deep learning, neural networks and computer vision.",
        },
    ]

    reranked = await rerank_program_candidates(query, candidates)
    assert len(reranked) == 2
    # The AI masters program must be ranked #1 with a strong score
    assert reranked[0]["program_name"] == "Master of Science in Artificial Intelligence"
    assert reranked[0]["fit_score"] >= 3.5
    assert reranked[1]["program_name"] == "Bachelor of Arts in English"
    assert reranked[1]["fit_score"] < 2.5
