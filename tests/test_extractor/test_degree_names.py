"""
C17 -- the four-level degree taxonomy.

The table below is not invented. Every string in REAL_DEGREE_NAMES was lifted
from the eight payloads at tag `pre-refactor`, which is where the awkward cases
come from: a bachelors degree whose name starts with "Post", a bachelors degree
whose name starts with "Doctor", the same physical-therapy degree filed under
two different levels in the same batch, and PhDs the extractor had put in the
masters bucket.

The suite's job is to prove that every one of those maps to exactly one of the
four levels and that nothing falls through to a default.
"""
import json
import pathlib

import pytest

from src.extractor.crawlers.notebook_querying import QUERY_SUITE
from src.extractor.normalizers.degree_names import (
    apply_degree_level,
    classify_degree_level,
    coerce_declared_level,
    resolve_degree_level,
    tokenize_degree_name,
)
from src.utilities.schema import DegreeLevel, ProgramCategoryBlock, ProgramItem

B, M, P, D = (DegreeLevel.BACHELORS, DegreeLevel.MASTERS,
              DegreeLevel.PHD, DegreeLevel.DIPLOMA)

REAL_DEGREE_NAMES = [
    # --- bachelors, plain ---
    ("BS Computer Science", B),
    ("BSc Electrical Engineering", B),
    ("BBA", B),
    ("BFA Fine Arts", B),
    ("BDes Communication Design", B),
    ("B.Sc. in Economics", B),
    ("Bachelor of Studies in English", B),
    ("Bachelor in Business and Information Technology", B),
    ("Bachelor of Electrical Engineering (BEE)", B),
    # --- bachelors that do not look like it ---
    ("Bachelor of Medicine, Bachelor of Surgery (MBBS)", B),
    ("MBBS", B),
    ("Bachelor of Laws (LLB)", B),
    ("Pharm.D", B),
    # "Post" is not a level marker. This is the exact string that would break a
    # naive prefix rule, and it is a real programme.
    ("Post-RN Bachelor of Science in Nursing", B),
    ("Post-RM Bachelor of Science in Midwifery", B),
    ("Bachelor of Midwifery Science (Post-RM), Uganda", B),
    # Entry-level professional doctorates: five-year post-intermediate degrees
    # in these markets, whatever the word "Doctor" suggests.
    ("Doctor of Physical Therapy (DPT)", B),
    ("Doctorate of Physical Therapy", B),
    # --- masters ---
    ("MS Computer Science", M),
    ("M.Sc. in Civil Engineering", M),
    ("M.A. Political Science", M),
    ("MBA", M),
    ("Executive MBA (EMBA)", M),
    ("LLM", M),
    ("M.Ed.", M),
    ("Master of Philosophy (MPhil) in Education", M),
    ("M.Phil. in Applied Psychology", M),
    ("M Phil Management Sciences", M),          # space-separated, not dotted
    ("Masters in Government and Public Policy", M),
    ("Master of Science in Nursing (MScN)", M),
    ("Executive Masters in Media Leadership and Innovation", M),
    # --- phd ---
    ("PhD in Computer Science", P),
    ("PHD Electrical Engineering", P),          # extractor had this under masters
    ("Doctor of Philosophy (PhD) in Health Sciences", P),
    ("Doctorate in Management Sciences", P),
    # A research doctorate that merely happens to be about pharmacy: the
    # entry-level rule must not claim it.
    ("PhD in Pharmacy Practice", P),
    # --- diploma ---
    ("Postgraduate Diploma in Clinical Psychology", D),
    ("PGD in Human Resource Management", D),
    ("Advanced Diploma in Islamic Banking", D),
    ("Certificate in Montessori Education", D),
]


@pytest.mark.parametrize("name,expected", REAL_DEGREE_NAMES)
def test_every_observed_degree_name_maps_to_exactly_one_level(name, expected):
    assert classify_degree_level(name) == expected


def test_no_observed_degree_name_falls_through():
    """Nothing lands in a fallback bucket -- the C17 gate."""
    unresolved = [n for n, _ in REAL_DEGREE_NAMES if classify_degree_level(n) is None]
    assert unresolved == []


def test_an_unreadable_name_returns_none_rather_than_guessing():
    """No default level. Guessing here fabricates a fact a student could act on."""
    assert classify_degree_level("") is None
    assert classify_degree_level("Faculty of Arts and Humanities") is None
    assert classify_degree_level("2026") is None


def test_post_doctoral_is_not_a_level():
    """Plan section 1, note 5: a post-doc is an appointment, not a programme."""
    assert "postdoc" not in {level.value for level in DegreeLevel}
    assert "post_doctoral" not in {level.value for level in DegreeLevel}


# ------------------------------------------------------------- tokenisation --

def test_dotted_and_spaced_abbreviations_fold_to_the_same_token():
    for variant in ("M.Phil. Economics", "MPhil Economics", "M Phil Economics"):
        assert "mphil" in tokenize_degree_name(variant)


def test_matching_is_on_tokens_not_substrings():
    """The lesson the link filter learned: "phd" must not match inside a word."""
    assert "phd" not in tokenize_degree_name("BS Alphdynamics")
    assert classify_degree_level("BS Alphdynamics") == B


# --------------------------------------------------------- declared values --

@pytest.mark.parametrize("declared,expected", [
    ("undergraduate", B),
    ("graduate", M),
    ("postgraduate_phd", P),
    ("postgraduate_and_phd", P),
    ("bachelors", B),
    ("Masters", M),
    ("PhD", P),
    ("diploma", D),
])
def test_retired_and_current_declared_values_coerce(declared, expected):
    assert coerce_declared_level(declared) == expected


def test_unreadable_declared_value_is_none():
    assert coerce_declared_level("postdoctoral") is None
    assert coerce_declared_level(None) is None
    assert coerce_declared_level(7) is None


def test_the_name_outranks_the_declared_level():
    """The name is the published degree title; the level is the model's opinion."""
    assert resolve_degree_level("PHD Computer Science", "graduate") == P


def test_the_declared_level_is_the_fallback_when_the_name_is_unreadable():
    assert resolve_degree_level("Faculty of Arts", "graduate") == M


def test_apply_degree_level_leaves_an_unresolvable_program_alone():
    prog = {"name": "Faculty of Arts", "degree_level": "something odd"}
    assert apply_degree_level(prog)["degree_level"] == "something odd"


# ------------------------------------------------- taxonomy wiring, end to end --

def test_the_four_levels_are_exactly_the_four_buckets():
    assert {level.value for level in DegreeLevel} == set(ProgramCategoryBlock().model_dump())


def test_the_query_suite_asks_for_every_level():
    assert {level.value for level in DegreeLevel} <= {spec.key for spec in QUERY_SUITE}


def test_a_program_declaring_a_retired_level_still_validates():
    """Payloads written before C17 must keep loading; see schema._coerce_degree_level."""
    item = ProgramItem(name="BS CS", degree_level="undergraduate", summary_3_lines="x")
    assert item.degree_level is DegreeLevel.BACHELORS


def test_the_exported_schema_advertises_the_four_levels():
    schema = json.loads(pathlib.Path("university_payload_schema.json").read_text())
    assert schema["$defs"]["DegreeLevel"]["enum"] == ["bachelors", "masters", "phd", "diploma"]
    assert set(schema["$defs"]["ProgramCategoryBlock"]["properties"]) == {
        "bachelors", "masters", "phd", "diploma"}
