"""
C19 standing guard: the pipeline does not invent values.

Decision D2 was answered "strip", on the grounds that this pipeline targets
universities worldwide and every fabricated default encoded an assumption about
Pakistan or western Europe. The characterization tests in test_normalizers.py
cover the behaviour; this file is the guard that stops the strings themselves
from coming back -- in a prompt, a schema default, or a new normalizer step.
"""
import ast
import pathlib

import pytest

from src.extractor.normalizers.runner import normalize_universal_payload
from src.utilities.schema import MainInfo, ProgramItem, UniversityPayload

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

# Every value the pipeline used to invent. Removed in C19; none of these may
# reappear anywhere under src/.
FABRICATED_STRINGS = [
    "Refer to Official Tuition Portal",
    "Standard University Application Fee",
    "Uni-Assist €75 / Free Direct Application",
    "Tuition Free (Semester Contribution applies)",
    "Intermediate / HSSC (60% Minimum)",
    "Matric (10%) + HSSC (40%) + Entry Test (50%)",
    "Abitur NC Grade / ECTS Credit Prerequisites",
    "ECTS & Academic Degree Evaluation",
    "High School Diploma / GPA Equivalent",
    "GPA & Standardized Test Evaluation",
    "Ministry of Higher Education (",
    "Bavarian State Ministry of Science and the Arts",
]

# The one exception, and why. This string is never written any more, but it sits
# in every payload extracted before C19, and the normalizer has to recognise it
# in order to refuse to carry it into `description`.
RECOGNISED_LEGACY_STRING = "Academic degree program offered by the university."
ALLOWED_FILES = {"schema.py", "program_fields.py"}


def _source_files():
    return [f for f in SRC.rglob("*.py") if "__pycache__" not in f.parts]


def _code_strings(path):
    """Every string literal in the file except its docstrings.

    Comments and docstrings are excluded deliberately: several modules explain
    at length which values C19 removed and why, and quoting a deleted string in
    order to document its removal is the opposite of reintroducing it. What
    matters is that no live code can still write one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


@pytest.mark.parametrize("needle", FABRICATED_STRINGS)
def test_no_fabricated_string_survives_in_src_code(needle):
    offenders = [
        str(f.relative_to(SRC)) for f in _source_files()
        if any(needle in s for s in _code_strings(f))
    ]
    assert offenders == [], f"{needle!r} is back in {offenders}"


def test_the_legacy_summary_placeholder_is_only_recognised_never_written():
    users = {
        f.name for f in _source_files()
        if any(RECOGNISED_LEGACY_STRING in s for s in _code_strings(f))
    }
    assert users <= ALLOWED_FILES, f"unexpected users: {users - ALLOWED_FILES}"


# ------------------------------------------------------- schema-level defaults --

@pytest.mark.parametrize("model,field", [
    (MainInfo, "country"),
    (MainInfo, "primary_instruction_language"),
    (MainInfo, "type"),
    (ProgramItem, "currency"),
    (ProgramItem, "delivery_mode"),
    (ProgramItem, "application_status"),
    (ProgramItem, "summary_3_lines"),
    (ProgramItem, "description"),
    (ProgramItem, "admission_requirements"),
])
def test_no_field_defaults_to_a_substantive_value(model, field):
    """Each of these used to default to a claim: Pakistan, English, public, PKR,
    On-Campus, rolling admissions, a stand-in summary. All None now."""
    assert model.model_fields[field].get_default(call_default_factory=True) is None


@pytest.mark.parametrize("model,field", [
    (MainInfo, "admission_cycles_offered"),
    (ProgramItem, "intake_terms"),
    (ProgramItem, "application_deadlines"),
])
def test_no_list_field_defaults_to_invented_entries(model, field):
    """["Fall"] and ["Fall", "Spring"] are northern-hemisphere naming, asserted
    for universities that run Semester 1 and 2 from February."""
    assert model.model_fields[field].get_default(call_default_factory=True) == []


# --------------------------------------------------------------- end to end --

def test_an_empty_payload_normalizes_to_nulls_not_to_prose():
    record = normalize_universal_payload({
        "main_info": {"name": "Somewhere University", "website": "https://x.example"},
        "programs": {"bachelors": [{"name": "BS Something"}]},
        "faculties": [],
        "contact": {},
    })
    main = record["main_info"]
    assert main.get("accreditation_body") is None
    assert main.get("primary_instruction_language") is None
    assert main.get("established_year") is None

    program = record["programs"]["bachelors"][0]
    assert program["tuition_fee"] is None
    assert program["currency"] is None
    assert program.get("application_fee") is None
    assert program["description"] is None
    assert program["application_deadlines"] == []
    elig = program["eligibility_requirements"]
    assert elig["minimum_marks_percentage"] is None
    assert elig["aggregate_formula"] is None
    assert elig["entry_tests_accepted"] == []


def test_that_payload_still_validates():
    """Nulls everywhere must not mean the model rejects the record: an audit can
    only report an empty field on a record it was able to load."""
    record = normalize_universal_payload({
        "main_info": {"name": "Somewhere University", "website": "https://x.example",
                      "description": "A university.", "key_links": {}},
        "programs": {"bachelors": [{"name": "BS Something", "degree_level": "bachelors"}]},
        "faculties": [],
        "contact": {},
    })
    payload = UniversityPayload.model_validate(record)
    assert payload.programs.bachelors[0].tuition_fee is None
    assert payload.main_info.country is None
