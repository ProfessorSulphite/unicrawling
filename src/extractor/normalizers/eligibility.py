"""
Eligibility block cleanup. Since C19 it invents nothing.

What this module used to do: when the extractor returned no minimum marks or no
aggregate formula, it wrote one in, chosen by country. Three branches -- one for
a handful of western European countries, one for the anglophone ones, and an
`else` that handed *the entire rest of the world* Pakistan's
"Intermediate / HSSC (60% Minimum)" and the weighted formula
"Matric (10%) + HSSC (40%) + Entry Test (50%)". The AKU payload alone covers
Kenya, Tanzania and Uganda, none of which have an HSSC.

The aggregate formula was the worst of them: a specific admission calculation,
stated with no source, that a student could plan an application around.

What remains is normalisation, not invention: the strings the extractor uses to
mean "nothing" ("null", "none", "N/A", "") become real nulls, so the inspector's
empty-field audit can see them.
"""
from typing import Any, Dict, Optional

# What the extractor writes when it means "no value". Normalised to None so an
# absent fact reads as absent rather than as the word "null".
_EMPTY_MARKERS = {"", "null", "none", "n/a", "na", "not available", "not specified", "-"}


def _blank_to_none(value: Any) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, str):
        return None if value.strip().lower() in _EMPTY_MARKERS else value.strip()
    return value


def normalize_eligibility(prog: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure the eligibility block exists and says nothing it cannot support."""
    elig = prog.get("eligibility_requirements") or {}

    elig["minimum_marks_percentage"] = _blank_to_none(elig.get("minimum_marks_percentage"))
    elig["aggregate_formula"] = _blank_to_none(elig.get("aggregate_formula"))

    tests = elig.get("entry_tests_accepted")
    if isinstance(tests, str):
        tests = [tests]
    elig["entry_tests_accepted"] = [
        t for t in (_blank_to_none(x) for x in (tests or [])) if t
    ]

    prog["eligibility_requirements"] = elig
    return prog
