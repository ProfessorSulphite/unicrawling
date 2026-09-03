"""
Eligibility and admission-requirement consolidation.

Every value written here is a placeholder invented when the model returned
nothing; none of it is sourced, and the aggregate formula in particular states a
specific weighting no source provided. See decision D2 -- C19 decides whether
these become nulls.
"""
from typing import Any, Dict


def apply_eligibility_defaults(prog: Dict[str, Any], country: str) -> Dict[str, Any]:
    """
    Step 3 of the former normalize_universal_program, extracted verbatim in C16.

    country_lower was computed once in the combined function; it is recomputed
    here so this step stands alone. Same value, same call.
    """
    country_lower = country.lower()

    # 3. Eligibility Requirements Normalization
    elig = prog.get("eligibility_requirements") or {}
    min_marks = elig.get("minimum_marks_percentage")
    agg_form = elig.get("aggregate_formula")

    if not min_marks or str(min_marks).strip().lower() in ("null", "none", ""):
        if country_lower in ("germany", "france", "italy", "netherlands", "switzerland", "finland"):
            elig["minimum_marks_percentage"] = "Abitur NC Grade / ECTS Credit Prerequisites"
        elif country_lower in ("united states", "usa", "united kingdom", "uk", "canada", "australia"):
            elig["minimum_marks_percentage"] = "High School Diploma / GPA Equivalent"
        else:
            elig["minimum_marks_percentage"] = "Intermediate / HSSC (60% Minimum)"

    if not agg_form or str(agg_form).strip().lower() in ("null", "none", ""):
        if country_lower in ("germany", "france", "italy", "netherlands", "switzerland", "finland"):
            elig["aggregate_formula"] = "ECTS & Academic Degree Evaluation"
        elif country_lower in ("united states", "usa", "united kingdom", "uk", "canada", "australia"):
            elig["aggregate_formula"] = "GPA & Standardized Test Evaluation"
        else:
            elig["aggregate_formula"] = "Matric (10%) + HSSC (40%) + Entry Test (50%)"

    prog["eligibility_requirements"] = elig
    return prog
