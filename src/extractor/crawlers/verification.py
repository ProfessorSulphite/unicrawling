"""
Grounding and Citation Verification Gate for extracted degree programs.

Uses TypeSafe Jev Noul to verify factual claims (tuition fee, deadlines,
eligibility) against source text, nullifying ungrounded or hallucinated claims.
"""
import asyncio
import logging
from typing import List, Optional, Union

from src.utilities.schema import ProgramItem
from src.utilities.typesafe_client import evaluate_noul, is_typesafe_available

logger = logging.getLogger("ExtractData")

GROUNDING_THRESHOLD = 0.75


async def verify_program_claims(
    program: ProgramItem,
    source_snippets: Optional[Union[str, List[str]]] = None,
) -> ProgramItem:
    """
    Verify high-risk factual claims in a ProgramItem against source snippets.

    Fields verified:
    - tuition_fee: Must be explicitly mentioned or grounded in source text.
    - application_deadlines: Must be grounded in source text.
    - eligibility_requirements.minimum_marks_percentage: Must be grounded in source text.

    If Jev Noul confidence < 0.75, the ungrounded field is set to None (or removed for deadlines),
    enforcing the golden rule: "Nothing is invented to fill a gap."
    """
    if not is_typesafe_available():
        return program

    # Build context from source snippets or program description
    if isinstance(source_snippets, list):
        context = "\n".join(s for s in source_snippets if s).strip()
    elif isinstance(source_snippets, str):
        context = source_snippets.strip()
    else:
        context = ""

    if not context and program.description:
        context = program.description

    if not context:
        # No context available to verify against; leave as-is
        return program

    # 1. Verify tuition_fee
    if program.tuition_fee:
        claim = f"The tuition fee for {program.name} is {program.tuition_fee}."
        instructions = (
            f"Is the claim '{claim}' explicitly supported or directly mentioned in the provided source text?"
        )
        try:
            prob = await evaluate_noul(state=context, instructions=instructions)
            if prob is not None and prob < GROUNDING_THRESHOLD:
                logger.warning(
                    f"Intercepted ungrounded claim for {program.name}: "
                    f"tuition_fee='{program.tuition_fee}' (grounded_prob={prob:.2f} < {GROUNDING_THRESHOLD}). Nullifying."
                )
                program.tuition_fee = None
                program.currency = None
        except Exception as e:
            logger.warning(f"Failed to verify tuition fee for {program.name}: {e}")

    # 2. Verify application_deadlines
    if program.application_deadlines:
        verified_deadlines = []
        for deadline in program.application_deadlines:
            instructions = (
                f"Is the application deadline '{deadline}' for {program.name} explicitly mentioned in the source text?"
            )
            try:
                prob = await evaluate_noul(state=context, instructions=instructions)
                if prob is not None and prob < GROUNDING_THRESHOLD:
                    logger.warning(
                        f"Intercepted ungrounded deadline for {program.name}: "
                        f"'{deadline}' (grounded_prob={prob:.2f} < {GROUNDING_THRESHOLD}). Removing."
                    )
                else:
                    verified_deadlines.append(deadline)
            except Exception as e:
                logger.warning(f"Failed to verify deadline '{deadline}' for {program.name}: {e}")
                verified_deadlines.append(deadline)
        program.application_deadlines = verified_deadlines

    # 3. Verify minimum_marks_percentage in eligibility_requirements
    if program.eligibility_requirements and program.eligibility_requirements.minimum_marks_percentage:
        marks = program.eligibility_requirements.minimum_marks_percentage
        instructions = (
            f"Is the minimum eligibility requirement of '{marks}' for {program.name} explicitly supported by the source text?"
        )
        try:
            prob = await evaluate_noul(state=context, instructions=instructions)
            if prob is not None and prob < GROUNDING_THRESHOLD:
                logger.warning(
                    f"Intercepted ungrounded eligibility requirement for {program.name}: "
                    f"minimum_marks='{marks}' (grounded_prob={prob:.2f} < {GROUNDING_THRESHOLD}). Nullifying."
                )
                program.eligibility_requirements.minimum_marks_percentage = None
        except Exception as e:
            logger.warning(f"Failed to verify eligibility for {program.name}: {e}")

    return program


async def verify_program_batch(
    programs: List[ProgramItem],
    source_snippets: Optional[Union[str, List[str]]] = None,
) -> List[ProgramItem]:
    """
    Verify all programs concurrently in a category block.
    """
    if not programs or not is_typesafe_available():
        return programs

    tasks = [verify_program_claims(p, source_snippets=source_snippets) for p in programs]
    return list(await asyncio.gather(*tasks))
