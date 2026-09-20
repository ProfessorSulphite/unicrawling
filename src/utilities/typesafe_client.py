"""
TypeSafe AI (Jev System One) client wrapper and evaluation primitives.

Provides asynchronous evaluation of application state against typed questions
(Choice, Noul, Score) with graceful fallback, timeout management, and confidence
reporting.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.config import config

logger = logging.getLogger("TypeSafeClient")

try:
    from typesafe_sdk import (
        AsyncTypeSafeClient,
        Choice,
        Noul,
        Score,
        TypeSafeError,
    )
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    AsyncTypeSafeClient = None
    Choice = None
    Noul = None
    Score = None
    TypeSafeError = Exception


def is_typesafe_available() -> bool:
    """True when typesafe-sdk is installed, enabled, and has an API key configured."""
    return _SDK_AVAILABLE and config.typesafe_enabled and bool(config.typesafe_api_key)


async def evaluate_system_one(
    state: Any,
    questions: Dict[str, Any],
    model: Optional[str] = None,
) -> Optional[Any]:
    """
    Execute a System One evaluation request with one or more typed questions.

    Returns the response object (containing .choices, .nouls, .scores), or None
    if TypeSafe is unavailable or the evaluation encountered an unrecoverable error.
    """
    if not is_typesafe_available():
        logger.debug("TypeSafe evaluation skipped (SDK unavailable or API key unset).")
        return None

    model_name = model or config.typesafe_model
    try:
        async with AsyncTypeSafeClient(api_key=config.typesafe_api_key) as client:
            response = await client.system_one(
                state=state,
                questions=questions,
                model=model_name,
            )
            return response
    except Exception as e:
        logger.warning(f"TypeSafe evaluation error against model '{model_name}': {e}")
        return None


async def evaluate_noul(
    state: Any,
    instructions: Any,
    criteria: Optional[Dict[str, str]] = None,
    model: Optional[str] = None,
) -> Optional[float]:
    """
    Evaluate a yes/no condition. Returns the probability of yes (0.0 to 1.0), or None.
    """
    if not is_typesafe_available():
        return None

    q = Noul(instructions=instructions, criteria=criteria) if criteria else Noul(instructions=instructions)
    response = await evaluate_system_one(state=state, questions={"q": q}, model=model)
    if response and hasattr(response, "nouls") and "q" in response.nouls:
        return float(response.nouls["q"].noul)
    return None


async def evaluate_choice(
    state: Any,
    instructions: Any,
    criteria: Dict[str, Any],
    model: Optional[str] = None,
) -> Optional[Tuple[str, float, Dict[str, float]]]:
    """
    Pick one option from a defined set.

    Returns:
        (chosen_option, confidence, probabilities_map), or None if unavailable.
    """
    if not is_typesafe_available():
        return None

    q = Choice(instructions=instructions, criteria=criteria)
    response = await evaluate_system_one(state=state, questions={"q": q}, model=model)
    if response and hasattr(response, "choices") and "q" in response.choices:
        choice_ans = response.choices["q"]
        return (
            str(choice_ans.choice),
            float(choice_ans.confidence or 0.0),
            dict(choice_ans.probabilities or {}),
        )
    return None


async def evaluate_score(
    state: Any,
    instructions: Any,
    criteria: List[Any],
    model: Optional[str] = None,
) -> Optional[Tuple[float, float, Dict[str, float]]]:
    """
    Evaluate degree along an ordered descriptive rubric.

    Returns:
        (score, confidence, probabilities_map), or None if unavailable.
    """
    if not is_typesafe_available():
        return None

    q = Score(instructions=instructions, criteria=criteria)
    response = await evaluate_system_one(state=state, questions={"q": q}, model=model)
    if response and hasattr(response, "scores") and "q" in response.scores:
        score_ans = response.scores["q"]
        return (
            float(score_ans.score),
            float(score_ans.confidence or 0.0),
            dict(score_ans.probabilities or {}),
        )
    return None
