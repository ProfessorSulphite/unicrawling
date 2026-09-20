"""
Unit and Live Integration Tests for Jev Grounding & Citation Verification Gate.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.extractor.crawlers.verification import (
    GROUNDING_THRESHOLD,
    verify_program_batch,
    verify_program_claims,
)
from src.utilities.schema import DegreeLevel, EligibilityRequirements, ProgramItem
from src.utilities.typesafe_client import is_typesafe_available


@pytest.fixture
def sample_program():
    return ProgramItem(
        name="BS Computer Science",
        degree_level=DegreeLevel.BACHELORS,
        tuition_fee="180,000 PKR",
        currency="PKR",
        application_deadlines=["August 10, 2026"],
        eligibility_requirements=EligibilityRequirements(
            minimum_marks_percentage="60%",
            entry_tests_accepted=["NAT-IE"],
        ),
        description="Comprehensive 4-year undergraduate degree in computer science.",
    )


# =============================================================================
# Offline Unit Tests (Mocks)
# =============================================================================

@pytest.mark.asyncio
async def test_verify_program_claims_grounded_mock(sample_program):
    """When Jev confirms claims (p >= 0.75), fields are retained."""
    with patch("src.extractor.crawlers.verification.is_typesafe_available", return_value=True), \
         patch("src.extractor.crawlers.verification.evaluate_noul", new_callable=AsyncMock) as mock_noul:
        mock_noul.return_value = 0.95

        verified = await verify_program_claims(sample_program, source_snippets="Grounded official text snippet.")
        assert verified.tuition_fee == "180,000 PKR"
        assert verified.currency == "PKR"
        assert verified.application_deadlines == ["August 10, 2026"]
        assert verified.eligibility_requirements.minimum_marks_percentage == "60%"


@pytest.mark.asyncio
async def test_verify_program_claims_fabricated_mock(sample_program):
    """When Jev detects ungrounded claims (p < 0.75), fields are nullified."""
    with patch("src.extractor.crawlers.verification.is_typesafe_available", return_value=True), \
         patch("src.extractor.crawlers.verification.evaluate_noul", new_callable=AsyncMock) as mock_noul:
        mock_noul.return_value = 0.12

        verified = await verify_program_claims(sample_program, source_snippets="Unrelated departmental page.")
        assert verified.tuition_fee is None
        assert verified.currency is None
        assert verified.application_deadlines == []
        assert verified.eligibility_requirements.minimum_marks_percentage is None
        # Other fields stay intact
        assert verified.name == "BS Computer Science"
        assert verified.eligibility_requirements.entry_tests_accepted == ["NAT-IE"]


@pytest.mark.asyncio
async def test_verify_program_batch_mock(sample_program):
    """Verify batch processing across multiple programs."""
    prog2 = ProgramItem(
        name="MS Software Engineering",
        degree_level=DegreeLevel.MASTERS,
        tuition_fee="220,000 PKR",
    )
    with patch("src.extractor.crawlers.verification.is_typesafe_available", return_value=True), \
         patch("src.extractor.crawlers.verification.evaluate_noul", new_callable=AsyncMock) as mock_noul:
        # First call grounded, second call ungrounded
        mock_noul.side_effect = [0.90, 0.90, 0.90, 0.15]

        batch = await verify_program_batch([sample_program, prog2], source_snippets="Context text")
        assert len(batch) == 2
        assert batch[0].tuition_fee == "180,000 PKR"
        assert batch[1].tuition_fee is None


@pytest.mark.asyncio
async def test_typesafe_unavailable_leaves_program_untouched(sample_program):
    """When TypeSafe is not configured or unavailable, no fields are modified."""
    with patch("src.extractor.crawlers.verification.is_typesafe_available", return_value=False):
        verified = await verify_program_claims(sample_program)
        assert verified.tuition_fee == "180,000 PKR"
        assert verified.application_deadlines == ["August 10, 2026"]


# =============================================================================
# Real API Live Integration Tests
# =============================================================================

@pytest.mark.live
@pytest.mark.skipif(
    not is_typesafe_available(),
    reason="Requires typesafe-sdk and TYPESAFE_API_KEY",
)
@pytest.mark.asyncio
async def test_live_verify_program_claims():
    """Live API test verifying grounded vs hallucinated claims against real context."""
    official_source_snippet = (
        "Admissions 2026: The Department of Electrical Engineering invites applications for "
        "BS Electrical Engineering. Eligibility: minimum 60% marks in intermediate. "
        "The semester tuition fee is 145,000 PKR. Applications must be submitted by August 20, 2026."
    )

    # 1. Program with Grounded Facts
    grounded_prog = ProgramItem(
        name="BS Electrical Engineering",
        degree_level=DegreeLevel.BACHELORS,
        tuition_fee="145,000 PKR",
        currency="PKR",
        application_deadlines=["August 20, 2026"],
        eligibility_requirements=EligibilityRequirements(
            minimum_marks_percentage="60%",
        ),
    )

    verified_grounded = await verify_program_claims(
        grounded_prog,
        source_snippets=official_source_snippet,
    )
    assert verified_grounded.tuition_fee == "145,000 PKR"
    assert verified_grounded.application_deadlines == ["August 20, 2026"]
    assert verified_grounded.eligibility_requirements.minimum_marks_percentage == "60%"

    # 2. Program with Fabricated Facts
    hallucinated_prog = ProgramItem(
        name="BS Electrical Engineering",
        degree_level=DegreeLevel.BACHELORS,
        tuition_fee="850,000 PKR",  # Fabricated
        currency="PKR",
        application_deadlines=["December 31, 2026"],  # Fabricated
        eligibility_requirements=EligibilityRequirements(
            minimum_marks_percentage="95%",  # Fabricated
        ),
    )

    verified_hallucinated = await verify_program_claims(
        hallucinated_prog,
        source_snippets=official_source_snippet,
    )
    # Fabricated tuition fee must be nullified
    assert verified_hallucinated.tuition_fee is None
    # Fabricated deadline must be removed
    assert verified_hallucinated.application_deadlines == []
    # Fabricated eligibility marks must be nullified
    assert verified_hallucinated.eligibility_requirements.minimum_marks_percentage is None
