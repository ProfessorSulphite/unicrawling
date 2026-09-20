"""
Real-API Live Integration Tests for TypeSafe System One (Jev).

These tests execute against the live https://api.typesafe.ai/v1/systemone endpoint.
Gated by @pytest.mark.live and skipped if TYPESAFE_API_KEY is unset.
"""
import pytest
from src.config import config
from src.utilities.typesafe_client import (
    evaluate_choice,
    evaluate_noul,
    evaluate_system_one,
    is_typesafe_available,
)

try:
    from typesafe_sdk import Choice, Noul
except ImportError:
    Noul = None
    Choice = None


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not is_typesafe_available(),
        reason="Live test requires typesafe-sdk and TYPESAFE_API_KEY in environment",
    ),
]


@pytest.mark.asyncio
async def test_live_typesafe_noul_grounding():
    """Verify live Noul evaluation for admission policy claim verification."""
    state = "The annual tuition fee for the BS Computer Science program is 240,000 PKR."
    
    # Grounded claim
    prob_true = await evaluate_noul(
        state=state,
        instructions="Does the text explicitly state that the tuition fee is 240,000 PKR?",
    )
    assert prob_true is not None
    assert prob_true >= 0.80, f"Expected high probability for grounded claim, got {prob_true}"

    # Hallucinated claim
    prob_false = await evaluate_noul(
        state=state,
        instructions="Does the text state that the application deadline is December 31st?",
    )
    assert prob_false is not None
    assert prob_false <= 0.20, f"Expected low probability for absent claim, got {prob_false}"


@pytest.mark.asyncio
async def test_live_typesafe_choice_degree_classification():
    """Verify live Choice classification for academic degree level."""
    state = {
        "degree_name": "Doctor of Physical Therapy (DPT)",
        "duration": "5 Years",
        "entry_requirements": "Higher Secondary School Certificate (FSc Pre-Medical)",
    }
    
    criteria = {
        "bachelors": "Entry-level undergraduate degree, including clinical 5-year entry doctorates like MBBS, DPT, PharmD",
        "masters": "Postgraduate graduate degree e.g. MS, MSc, MBA, MPhil",
        "phd": "Doctor of Philosophy, research doctorate only",
        "diploma": "Postgraduate diploma, professional certificate",
    }

    result = await evaluate_choice(
        state=state,
        instructions="Classify this degree program into exactly one of the target degree levels.",
        criteria=criteria,
    )
    assert result is not None
    chosen, confidence, probs = result
    assert chosen == "bachelors", f"Expected 'bachelors' for entry-level DPT, got '{chosen}'"
    assert confidence >= 0.70


@pytest.mark.asyncio
async def test_live_typesafe_speculative_fanout_batch():
    """Verify live speculative fan-out evaluating multiple questions in a single request."""
    state = {
        "url": "https://itu.edu.pk/admissions/bs-computer-science-fall-2026/",
        "anchor_text": "BS Computer Science Admissions 2026",
        "path_words": "admissions bs computer science fall 2026",
    }

    questions = {
        "is_prospective": Noul(instructions="Is this page relevant for prospective students applying to university?"),
        "tier": Choice(
            instructions="Which priority tier does this link belong to?",
            criteria={
                "tier_1_programs": "Specific degree program curriculum, syllabus, or detail page",
                "tier_2_admissions": "General admission policies, fee schedules, or application portals",
                "tier_3_faculties": "Academic faculties, departments, or schools directory",
                "tier_4_contacts": "Contact us, phone numbers, email addresses, help desks",
                "noise": "News, tenders, jobs, event galleries, login portals",
            },
        ),
    }

    response = await evaluate_system_one(state=state, questions=questions)
    assert response is not None
    assert hasattr(response, "nouls") and "is_prospective" in response.nouls
    assert hasattr(response, "choices") and "tier" in response.choices

    is_prospective_prob = float(response.nouls["is_prospective"].noul)
    tier_choice = str(response.choices["tier"].choice)

    assert is_prospective_prob >= 0.80
    assert tier_choice in ("tier_1_programs", "tier_2_admissions")
