import json
from unittest.mock import AsyncMock, patch
import pytest

from src.utilities.deepseek_client import (
    _offline_normalize_fee,
    normalize_tuition_batch,
    extract_field_from_text,
    is_deepseek_available,
)
from src.utilities.schema import ProgramItem, DegreeLevel, NormalizedTuition
from src.inspector.formatting import get_program_normalized_usd


def test_offline_normalize_fee_usd():
    """Verify offline parsing of standard USD fee."""
    norm = _offline_normalize_fee("$55,000 per year")
    assert norm.amount == 55000.0
    assert norm.currency == "USD"
    assert norm.interval == "annual"
    assert norm.normalized_usd == 55000.0


def test_offline_normalize_fee_pkr():
    """Verify offline parsing of PKR semester fee with USD conversion."""
    norm = _offline_normalize_fee("Rs. 250,000 per semester")
    assert norm.amount == 250000.0
    assert norm.currency == "PKR"
    assert norm.interval == "semester"
    assert norm.normalized_usd == 900.0  # 250000 * 0.0036


def test_offline_normalize_fee_eur():
    """Verify offline parsing of EUR semester fee with USD conversion."""
    norm = _offline_normalize_fee("€1,500 / semester")
    assert norm.amount == 1500.0
    assert norm.currency == "EUR"
    assert norm.interval == "semester"
    assert norm.normalized_usd == 1620.0  # 1500 * 1.08


@pytest.mark.asyncio
async def test_normalize_tuition_batch_offline_fallback():
    """When API key is absent, normalize_tuition_batch applies offline rules safely."""
    with patch("src.utilities.deepseek_client.is_deepseek_available", return_value=False):
        programs = [
            ProgramItem(name="BS CS", degree_level=DegreeLevel.BACHELORS, tuition_fee="$12,000 / year"),
            ProgramItem(name="MS EE", degree_level=DegreeLevel.MASTERS, tuition_fee="Rs. 300,000 per semester"),
            ProgramItem(name="PhD AI", degree_level=DegreeLevel.PHD, tuition_fee=None),
        ]

        normalized = await normalize_tuition_batch(programs)
        assert len(normalized) == 3

        # BS CS
        assert normalized[0].tuition_fee_normalized is not None
        assert normalized[0].tuition_fee_normalized.amount == 12000.0
        assert normalized[0].tuition_fee_normalized.normalized_usd == 12000.0

        # MS EE
        assert normalized[1].tuition_fee_normalized is not None
        assert normalized[1].tuition_fee_normalized.amount == 300000.0
        assert normalized[1].tuition_fee_normalized.normalized_usd == 1080.0

        # PhD AI
        assert normalized[2].tuition_fee_normalized is None


@pytest.mark.asyncio
async def test_normalize_tuition_batch_mock_deepseek_api():
    """Verify parsing DeepSeek API JSON response into NormalizedTuition models."""
    mock_response_data = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "normalized": [
                            {
                                "index": 0,
                                "amount": 62150.0,
                                "currency": "USD",
                                "interval": "annual",
                                "normalized_usd": 62150.0,
                            }
                        ]
                    })
                }
            }
        ]
    }

    programs = [
        ProgramItem(name="BS CS", degree_level=DegreeLevel.BACHELORS, tuition_fee="$62,150 per year"),
    ]

    with patch("src.utilities.deepseek_client.is_deepseek_available", return_value=True), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json = lambda: mock_response_data

        res = await normalize_tuition_batch(programs)
        assert res[0].tuition_fee_normalized is not None
        assert res[0].tuition_fee_normalized.amount == 62150.0
        assert res[0].tuition_fee_normalized.currency == "USD"
        assert res[0].tuition_fee_normalized.normalized_usd == 62150.0


@pytest.mark.asyncio
async def test_extract_field_from_text_mock_api():
    """Verify DeepSeek direct field extraction from scraped text."""
    mock_response_data = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "extracted_value": "https://admissions.cornell.edu/apply",
                        "quote": "Apply online at https://admissions.cornell.edu/apply"
                    })
                }
            }
        ]
    }

    with patch("src.utilities.deepseek_client.is_deepseek_available", return_value=True), \
         patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json = lambda: mock_response_data

        portal = await extract_field_from_text(
            page_text="Welcome. Apply online at https://admissions.cornell.edu/apply for 2026.",
            field_name="application_portal_url",
            uni_name="Cornell",
        )
        assert portal == "https://admissions.cornell.edu/apply"


def test_get_program_normalized_usd():
    """Verify get_program_normalized_usd prioritizes normalized_usd over raw number."""
    # Program with NormalizedTuition
    prog1 = {
        "name": "BS Software Engineering",
        "tuition_fee": "Rs. 250,000 / semester",
        "tuition_fee_normalized": {
            "amount": 250000.0,
            "currency": "PKR",
            "interval": "semester",
            "normalized_usd": 900.0,
        },
    }
    assert get_program_normalized_usd(prog1) == 900.0

    # Program without NormalizedTuition falls back to raw number
    prog2 = {
        "name": "BS Software Engineering",
        "tuition_fee": "$14,000 per year",
    }
    assert get_program_normalized_usd(prog2) == 14000.0
