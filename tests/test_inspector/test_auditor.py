"""
The auditor has to name exactly the fields that are missing -- no more, no less.

An over-reporting audit gets ignored; an under-reporting one waves a gappy corpus
through to Supabase. Both failures look the same from the outside (a green
verdict), so the fixture below is built with its gaps known in advance and the
tests assert the exact set.

C19 is what makes any of this possible: before it the normalizer filled every
empty field, so this audit would have reported perfect health over 240 invented
admission formulas.
"""
import pytest

from src.inspector.auditor import (
    CoverageReport,
    audit_program,
    audit_records,
    is_missing,
    readiness_verdict,
)
from src.utilities.schema import CRITICAL_PROGRAM_FIELDS, REQUIRED_PROGRAM_FIELDS


def _program(name="BS Computer Science", level="bachelors", **over):
    """A programme answering every required field, before `over` removes some."""
    base = {
        "name": name,
        "degree_level": level,
        "duration": "4 years",
        "tuition_fee": "PKR 150,000 per semester",
        "currency": "PKR",
        "eligibility_requirements": {
            "minimum_marks_percentage": "60%",
            "entry_tests_accepted": ["NTS"],
            "aggregate_formula": None,
        },
        "admission_requirements": "Transcript, two references, an interview.",
        "application_fee": "PKR 2,000",
        "application_deadlines": ["2026-08-05"],
        "description": "A four-year programme covering systems, theory and practice.",
    }
    base.update(over)
    return base


def _record(name, programs):
    return {"main_info": {"name": name}, "programs": programs}


# ------------------------------------------------------- the emptiness rule --

@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "N/A",
        "n/a",
        "not specified",
        "TBD",
        "null",
        [],
        {},
        [None, ""],
        float("nan"),
        # The shape that matters most: eligibility_requirements always
        # serialises, so a programme with no eligibility data still ships a dict.
        {"minimum_marks_percentage": None, "entry_tests_accepted": [], "aggregate_formula": None},
    ],
)
def test_these_all_count_as_missing(value):
    assert is_missing(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "4 years",
        "0",
        0,
        False,
        ["NTS"],
        {"minimum_marks_percentage": "60%"},
        # Partially filled is answered: the programme told us something.
        {"minimum_marks_percentage": None, "entry_tests_accepted": ["SAT"]},
    ],
)
def test_these_all_count_as_answered(value):
    assert is_missing(value) is False


def test_zero_and_false_are_answers_not_gaps():
    """A fee of 0 is a fact -- free tuition. Truthiness is the wrong test."""
    assert is_missing(0) is False
    assert is_missing(False) is False


# ---------------------------------------------- exactly the missing fields --

def test_audit_program_names_exactly_the_unanswered_fields():
    prog = _program(
        tuition_fee=None,
        application_fee="N/A",
        description="   ",
        eligibility_requirements={},
    )
    assert audit_program(prog) == (
        "tuition_fee",
        "eligibility_requirements",
        "application_fee",
        "description",
    )


def test_a_complete_programme_reports_no_gaps():
    assert audit_program(_program()) == ()


def test_missing_fields_are_reported_in_schema_order():
    """Stable order, so a diff of two audit runs is readable."""
    prog = _program(description=None, duration=None, currency=None)
    missing = audit_program(prog)
    assert missing == tuple(f for f in REQUIRED_PROGRAM_FIELDS if f in set(missing))


# --------------------------------------------------------- corpus coverage --

@pytest.fixture
def gappy_corpus():
    """
    Four programmes across two universities, with gaps placed on purpose:

      alpha/BS CS      complete
      alpha/MS DS      no tuition_fee, no currency
      beta/PhD Physics no application_fee, no description, empty eligibility
      beta/PGD Data    no duration

    So: duration 3/4, tuition_fee 3/4, currency 3/4, eligibility 3/4,
    application_fee 3/4, description 3/4, and name/degree_level 4/4.
    """
    return [
        _record(
            "Alpha University",
            {
                "bachelors": [_program()],
                "masters": [_program("MS Data Science", "masters", tuition_fee=None, currency=None)],
            },
        ),
        _record(
            "Beta Institute",
            {
                "phd": [
                    _program(
                        "PhD Physics",
                        "phd",
                        application_fee=None,
                        description=None,
                        eligibility_requirements={},
                    )
                ],
                "diploma": [_program("PGD Data Analytics", "diploma", duration=None)],
            },
        ),
    ]


def test_coverage_counts_are_exact(gappy_corpus):
    report = audit_records(gappy_corpus)

    assert report.universities == 2
    assert report.programs == 4
    assert report.complete_programs == 1

    expected = {
        "name": 4,
        "degree_level": 4,
        "duration": 3,
        "tuition_fee": 3,
        "currency": 3,
        "eligibility_requirements": 3,
        "admission_requirements": 4,
        "application_fee": 3,
        "application_deadlines": 4,
        "description": 3,
    }
    assert {n: c.present for n, c in report.fields.items()} == expected
    assert all(c.total == 4 for c in report.fields.values())


def test_degree_level_distribution_covers_all_four_buckets(gappy_corpus):
    report = audit_records(gappy_corpus)
    assert report.by_level == {"bachelors": 1, "masters": 1, "phd": 1, "diploma": 1}


def test_each_gap_is_attributed_to_its_programme(gappy_corpus):
    report = audit_records(gappy_corpus)
    gaps = {g.program: g.missing_fields for g in report.gaps}

    assert set(gaps) == {"MS Data Science", "PhD Physics", "PGD Data Analytics"}
    assert gaps["MS Data Science"] == ("tuition_fee", "currency")
    assert gaps["PhD Physics"] == ("eligibility_requirements", "application_fee", "description")
    assert gaps["PGD Data Analytics"] == ("duration",)
    assert report.gaps_by_university() == {"Alpha University": 1, "Beta Institute": 2}


def test_a_university_with_no_programmes_is_named(gappy_corpus):
    corpus = gappy_corpus + [_record("Empty College", {})]
    report = audit_records(corpus)
    assert report.universities_without_programs == ["Empty College"]
    # It contributes a university but no programme rows, so the grid is unmoved.
    assert report.programs == 4


def test_overall_ratio_is_answered_cells_over_the_whole_grid(gappy_corpus):
    report = audit_records(gappy_corpus)
    # 4 programmes x 10 required fields = 40 cells; 6 are missing.
    assert report.overall_ratio == pytest.approx(34 / 40)


# ---------------------------------------------------------- the verdict --

def test_a_complete_corpus_is_ready():
    report = audit_records([_record("Alpha University", {"bachelors": [_program()]})])
    verdict = readiness_verdict(report)
    assert verdict.ready is True
    assert verdict.blocking == ()


def test_an_empty_corpus_is_never_ready():
    verdict = readiness_verdict(audit_records([]))
    assert verdict.ready is False
    assert "No programmes" in verdict.blocking[0]


@pytest.mark.parametrize("critical", CRITICAL_PROGRAM_FIELDS)
def test_one_missing_critical_field_blocks_regardless_of_threshold(critical):
    """
    A row with no name or no degree_level is not a programme. No coverage floor,
    however low, makes it shippable -- so this blocks even at a 0% threshold.
    """
    complete = [_program(f"Prog {i}") for i in range(19)]
    broken = _program("Broken")
    broken[critical] = None
    report = audit_records([_record("Alpha University", {"bachelors": complete + [broken]})])

    verdict = readiness_verdict(report, min_field_coverage=0.0, min_overall_coverage=0.0)
    assert verdict.ready is False
    assert any(f"'{critical}'" in reason for reason in verdict.blocking)


def test_a_field_below_its_floor_blocks_and_says_which(gappy_corpus):
    verdict = readiness_verdict(audit_records(gappy_corpus), min_field_coverage=0.80)
    assert verdict.ready is False
    named = " ".join(verdict.blocking)
    # 3/4 = 75%, below the 80% floor.
    for f in ("duration", "tuition_fee", "currency", "application_fee", "description"):
        assert f"'{f}'" in named
    # 4/4 fields clear it and must not be named.
    assert "'admission_requirements'" not in named
    assert "'application_deadlines'" not in named


def test_overall_coverage_below_its_floor_blocks_on_its_own(gappy_corpus):
    """Every field can clear its own floor while the grid as a whole does not."""
    verdict = readiness_verdict(
        audit_records(gappy_corpus), min_field_coverage=0.0, min_overall_coverage=0.99
    )
    assert verdict.ready is False
    assert any("Overall required-field coverage" in r for r in verdict.blocking)


def test_thresholds_default_to_config(monkeypatch, gappy_corpus):
    report = audit_records(gappy_corpus)
    monkeypatch.setattr("src.inspector.auditor.config.audit_min_field_coverage", 0.99)
    monkeypatch.setattr("src.inspector.auditor.config.audit_min_overall_coverage", 0.0)
    assert readiness_verdict(report).ready is False

    monkeypatch.setattr("src.inspector.auditor.config.audit_min_field_coverage", 0.5)
    assert readiness_verdict(report).ready is True


def test_universities_without_programmes_warn_but_do_not_block(gappy_corpus):
    corpus = gappy_corpus + [_record("Empty College", {})]
    verdict = readiness_verdict(
        audit_records(corpus), min_field_coverage=0.0, min_overall_coverage=0.0
    )
    assert verdict.ready is True
    assert any("Empty College" in w for w in verdict.warnings)


def test_an_entirely_empty_degree_level_warns():
    """A whole bucket at zero usually means a query in the suite is failing."""
    report = audit_records([_record("Alpha University", {"bachelors": [_program()]})])
    verdict = readiness_verdict(report)
    warning = " ".join(verdict.warnings)
    for level in ("masters", "phd", "diploma"):
        assert level in warning
