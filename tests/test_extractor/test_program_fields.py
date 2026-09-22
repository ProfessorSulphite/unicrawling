"""
C18 -- the per-programme field set required by plan section 5.

Two things are pinned here. First, that ProgramItem actually carries every field
the plan calls required, driven off a list rather than prose so a future field
cannot be quietly dropped. Second, that a payload written before C18 still
yields those fields on read: inspect_cli re-normalizes every record it loads, so
a carry-forward that silently failed would show up as the new fields being
universally empty across the whole existing corpus.

The fixture is real -- three programmes lifted from the ITU payload at tag
`pre-refactor`, in their original pre-C18 shape (`summary_3_lines`, a singular
`application_deadline`, the retired bucket names).
"""
import copy
import json
import pathlib

import pytest

from src.extractor.crawlers.query_schemas import QUERY_SUITE
from src.extractor.normalizers.program_fields import apply_program_field_carryover
from src.extractor.normalizers.runner import normalize_universal_payload
from src.utilities.schema import (
    REQUIRED_PROGRAM_FIELDS,
    SUMMARY_FALLBACK,
    ProgramItem,
    UniversityPayload,
)

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "normalizer"

# Plan section 5, "Per-program required fields", one entry per bullet.
# C21 moved the canonical list into the schema module, so the model that declares
# these fields, the auditor that reports them missing, and this test all read one
# definition. A copy here would let the auditor quietly stop checking a field.


def _item(**over):
    base = dict(name="BS CS", degree_level="bachelors", summary_3_lines="x")
    base.update(over)
    return ProgramItem(**base)


@pytest.mark.parametrize("field", REQUIRED_PROGRAM_FIELDS)
def test_program_item_carries_every_required_field(field):
    assert field in ProgramItem.model_fields


def test_tuition_is_labelled_not_converted():
    """Finding 8: currency is a label for what the university published."""
    item = _item(tuition_fee="EUR 1,500 per semester", currency="EUR")
    assert item.tuition_fee == "EUR 1,500 per semester"
    assert item.currency == "EUR"


# ------------------------------------------------------------------ defaults --

def test_description_and_admission_requirements_default_to_none():
    """Not to prose. A programme page that says nothing normalizes to nothing."""
    item = _item()
    assert item.description is None
    assert item.admission_requirements is None


def test_deadlines_default_to_an_empty_list():
    assert _item().application_deadlines == []


# ------------------------------------------------------------- carry-forward --

def test_description_is_carried_over_from_a_real_summary():
    prog = {"name": "BS CS", "summary_3_lines": "Covers ML and systems."}
    assert apply_program_field_carryover(prog)["description"] == "Covers ML and systems."


def test_the_summary_placeholder_is_never_carried_into_description():
    """C19 is about removing that stand-in, not propagating it into a new field."""
    prog = {"name": "BS CS", "summary_3_lines": SUMMARY_FALLBACK}
    assert apply_program_field_carryover(prog)["description"] is None


def test_an_existing_description_is_not_overwritten():
    prog = {"name": "BS CS", "description": "Real paragraph.", "summary_3_lines": "Old."}
    assert apply_program_field_carryover(prog)["description"] == "Real paragraph."


def test_a_singular_deadline_becomes_a_one_entry_list():
    prog = {"name": "BS CS", "application_deadline": "2026-08-05"}
    out = apply_program_field_carryover(prog)
    assert out["application_deadlines"] == ["2026-08-05"]
    assert "application_deadline" not in out


def test_the_retired_deadline_key_also_survives_pydantic():
    """The schema accepts it as a validation alias, for callers that skip the
    normalizer entirely -- without it a pre-C18 programme's only deadline is
    silently dropped at validation."""
    assert _item(application_deadline="2026-08-05").application_deadlines == ["2026-08-05"]


# ------------------------------------------------------- prompts and the gate --

@pytest.mark.parametrize("key", ["bachelors", "masters", "phd", "diploma"])
def test_every_programme_prompt_requests_the_new_fields(key):
    spec = next(s for s in QUERY_SUITE if s.key == key)
    assert '"description"' in spec.prompt
    assert '"admission_requirements"' in spec.prompt
    assert '"application_deadlines"' in spec.prompt
    assert "full paragraph" in spec.prompt
    # The three-line summary is superseded; asking for both wastes budget.
    assert "summary_3_lines" not in spec.prompt


def test_a_pre_c18_payload_normalizes_and_validates_against_the_new_model():
    """The C18 gate, on real recorded output.

    Round-trips the whole payload: retired bucket names, retired degree levels,
    summary_3_lines and a singular application_deadline all have to land on the
    current schema without losing a value.
    """
    src = json.loads((FIXTURES / "pre_c18_programs.input.json").read_text())
    record = normalize_universal_payload(copy.deepcopy(src))
    payload = UniversityPayload.model_validate(record)

    programs = (payload.programs.bachelors + payload.programs.masters
                + payload.programs.phd + payload.programs.diploma)
    assert len(programs) == 3
    for program in programs:
        assert program.description, f"{program.name} lost its description"
        assert program.application_deadlines, f"{program.name} lost its deadline"

    # And the round trip is stable: re-validating the dump changes nothing.
    assert UniversityPayload.model_validate(payload.model_dump()) == payload
