"""
Semantic Counselor Search & Intent Routing using TypeSafe Jev.

Parses natural language student counseling queries into typed intent constraints
and semantically reranks matching degree programs using Jev Score (0-5 rubric).
"""
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.inspector.formatting import extract_numeric_fee, show
from src.inspector.records import load_all_records
from src.utilities.typesafe_client import (
    evaluate_choice,
    evaluate_score,
    is_typesafe_available,
)

logger = logging.getLogger("Inspector")

try:
    from typesafe_sdk import Choice, Score
except ImportError:
    Choice = None
    Score = None


STUDENT_LEVEL_CRITERIA: Dict[str, str] = {
    "bachelors": "Undergraduate, BS, BSc, BA, Bachelor's programs",
    "masters": "Postgraduate graduate, Master's, MS, MSc, MBA, MPhil programs",
    "phd": "Doctorate, PhD, research doctoral programs",
    "diploma": "Postgraduate diploma, professional certificates",
    "any": "No specific degree level mentioned, or general query",
}

FIT_RUBRIC: List[str] = [
    "Completely irrelevant or mismatched degree discipline",
    "Poor match with major discipline or level mismatch",
    "Weak match with partial subject overlap",
    "Moderate match on topic and level but missing key preferences",
    "Strong match closely satisfying student inquiry",
    "Perfect match aligning exactly with student counseling criteria",
]


async def route_search_intent(query: str) -> Dict[str, Any]:
    """
    Parse a student counseling query into structured filters using Jev Choice.
    """
    intent = {
        "level": None,
        "raw_query": query,
    }
    if not is_typesafe_available():
        return intent

    try:
        level_choice = await evaluate_choice(
            state={"student_query": query},
            instructions="What academic degree level is the student inquiring about?",
            criteria=STUDENT_LEVEL_CRITERIA,
        )
        if level_choice and level_choice[0] != "any" and level_choice[1] >= 0.70:
            intent["level"] = level_choice[0]
    except Exception as e:
        logger.warning(f"Jev intent routing failed: {e}")

    return intent


async def rerank_program_candidates(
    query: str,
    candidates: List[Dict[str, Any]],
    top_k: int = 25,
) -> List[Dict[str, Any]]:
    """
    Rerank candidate programs using Jev Score along a 0-5 rubric.
    """
    if not candidates or not is_typesafe_available():
        return candidates[:top_k]

    slice_to_score = candidates[:min(len(candidates), 12)]

    async def score_one(candidate: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
        state = {
            "student_query": query,
            "program_name": candidate.get("program_name"),
            "university": candidate.get("university"),
            "category": candidate.get("category"),
            "department": candidate.get("department"),
            "tuition_fee": candidate.get("tuition_fee"),
            "description": candidate.get("description", ""),
        }
        try:
            res = await evaluate_score(
                state=state,
                instructions="Score how well this university program satisfies the student's query.",
                criteria=FIT_RUBRIC,
            )
            fit_score = res[0] if res else 3.0
        except Exception:
            fit_score = 3.0

        candidate_copy = dict(candidate)
        candidate_copy["fit_score"] = round(fit_score, 2)
        return candidate_copy, fit_score

    try:
        scored = await asyncio.gather(*(score_one(c) for c in slice_to_score))
        scored.sort(key=lambda x: -x[1])
        reranked = [c for c, _ in scored]

        if len(candidates) > len(slice_to_score):
            reranked.extend(candidates[len(slice_to_score):])

        return reranked[:top_k]
    except Exception as e:
        logger.warning(f"Jev reranking failed: {e}")
        return candidates[:top_k]
