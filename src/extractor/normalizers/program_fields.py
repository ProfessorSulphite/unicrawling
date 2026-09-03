"""
Per-programme field carry-forward for the C18 schema (plan section 5).

C18 gave ProgramItem a full-paragraph `description`, an `admission_requirements`
field, and a plural `application_deadlines` list. Payloads written before that
carry `summary_3_lines` and a singular `application_deadline` instead, and
inspect_cli re-normalizes every record it reads -- so without this step the
inspector's audits would report the new fields as universally empty and the old
deadlines as lost.

Nothing here invents a value. Every field it writes is text the payload already
contained, moved to where the current schema looks for it.
"""
from __future__ import annotations

from typing import Any, Dict

from src.utilities.schema import SUMMARY_FALLBACK


def apply_program_field_carryover(prog: Dict[str, Any]) -> Dict[str, Any]:
    """Move pre-C18 values onto their C18 fields. Mutates and returns prog."""
    # description <- summary_3_lines, but never the stand-in the schema writes
    # when a programme arrived with no summary at all. Carrying that across
    # would turn one placeholder into two, and C19 is about removing it, not
    # spreading it.
    if not str(prog.get("description") or "").strip():
        summary = str(prog.get("summary_3_lines") or "").strip()
        if summary and summary != SUMMARY_FALLBACK:
            prog["description"] = summary

    # application_deadlines <- application_deadline. The schema accepts the
    # retired key as a validation alias, but the normalizer works on raw dicts
    # that never see Pydantic, so the same move has to happen here.
    deadlines = prog.get("application_deadlines")
    if not isinstance(deadlines, list) or not deadlines:
        single = prog.get("application_deadline")
        if isinstance(single, str) and single.strip():
            prog["application_deadlines"] = [single.strip()]
        elif isinstance(single, list):
            prog["application_deadlines"] = [
                str(d).strip() for d in single if d is not None and str(d).strip()
            ]
        else:
            prog.setdefault("application_deadlines", [])
    prog.pop("application_deadline", None)

    # Both new fields are always present afterwards, even when empty. The
    # inspector audits empty fields by reading them; an absent key and a null
    # one would otherwise be counted differently for no reason.
    prog.setdefault("description", None)
    prog.setdefault("admission_requirements", None)
    return prog
