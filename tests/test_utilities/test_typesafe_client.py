"""
Tests for TypeSafe client wrapper (src/utilities/typesafe_client.py).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.config import config
from src.utilities.typesafe_client import (
    evaluate_choice,
    evaluate_noul,
    evaluate_score,
    evaluate_system_one,
    is_typesafe_available,
)


def test_is_typesafe_available_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "typesafe_enabled", False)
    assert not is_typesafe_available()


def test_is_typesafe_available_when_key_empty(monkeypatch):
    monkeypatch.setattr(config, "typesafe_enabled", True)
    monkeypatch.setattr(config, "typesafe_api_key", "")
    assert not is_typesafe_available()


def test_is_typesafe_available_when_configured(monkeypatch):
    monkeypatch.setattr(config, "typesafe_enabled", True)
    monkeypatch.setattr(config, "typesafe_api_key", "test_key_123")
    assert is_typesafe_available()


@pytest.mark.asyncio
async def test_evaluate_noul_with_mock(monkeypatch):
    monkeypatch.setattr(config, "typesafe_enabled", True)
    monkeypatch.setattr(config, "typesafe_api_key", "test_key")

    mock_response = MagicMock()
    mock_noul_ans = MagicMock()
    mock_noul_ans.noul = 0.92
    mock_response.nouls = {"q": mock_noul_ans}

    with patch("src.utilities.typesafe_client.AsyncTypeSafeClient") as MockClient:
        mock_instance = AsyncMock()
        mock_instance.system_one.return_value = mock_response
        MockClient.return_value.__aenter__.return_value = mock_instance

        prob = await evaluate_noul("Sample document", "Is this academic?")
        assert prob == 0.92


@pytest.mark.asyncio
async def test_evaluate_choice_with_mock(monkeypatch):
    monkeypatch.setattr(config, "typesafe_enabled", True)
    monkeypatch.setattr(config, "typesafe_api_key", "test_key")

    mock_response = MagicMock()
    mock_choice_ans = MagicMock()
    mock_choice_ans.choice = "bachelors"
    mock_choice_ans.confidence = 0.88
    mock_choice_ans.probabilities = {"bachelors": 0.88, "masters": 0.12}
    mock_response.choices = {"q": mock_choice_ans}

    with patch("src.utilities.typesafe_client.AsyncTypeSafeClient") as MockClient:
        mock_instance = AsyncMock()
        mock_instance.system_one.return_value = mock_response
        MockClient.return_value.__aenter__.return_value = mock_instance

        chosen, conf, probs = await evaluate_choice(
            "BS in Computer Science",
            "What level is this?",
            {"bachelors": "Undergraduate", "masters": "Graduate"},
        )
        assert chosen == "bachelors"
        assert conf == 0.88
        assert probs["bachelors"] == 0.88


@pytest.mark.asyncio
async def test_evaluate_score_with_mock(monkeypatch):
    monkeypatch.setattr(config, "typesafe_enabled", True)
    monkeypatch.setattr(config, "typesafe_api_key", "test_key")

    mock_response = MagicMock()
    mock_score_ans = MagicMock()
    mock_score_ans.score = 4.5
    mock_score_ans.confidence = 0.95
    mock_score_ans.probabilities = {"4": 0.5, "5": 0.5}
    mock_response.scores = {"q": mock_score_ans}

    with patch("src.utilities.typesafe_client.AsyncTypeSafeClient") as MockClient:
        mock_instance = AsyncMock()
        mock_instance.system_one.return_value = mock_response
        MockClient.return_value.__aenter__.return_value = mock_instance

        score, conf, probs = await evaluate_score(
            "PhD Candidate",
            "Rate research intensity",
            ["low", "medium", "high", "exceptional", "world-class"],
        )
        assert score == 4.5
        assert conf == 0.95


@pytest.mark.asyncio
async def test_evaluate_system_one_handles_exception(monkeypatch):
    monkeypatch.setattr(config, "typesafe_enabled", True)
    monkeypatch.setattr(config, "typesafe_api_key", "test_key")

    with patch("src.utilities.typesafe_client.AsyncTypeSafeClient") as MockClient:
        mock_instance = AsyncMock()
        mock_instance.system_one.side_effect = RuntimeError("API connection timeout")
        MockClient.return_value.__aenter__.return_value = mock_instance

        result = await evaluate_system_one("State", {})
        assert result is None
