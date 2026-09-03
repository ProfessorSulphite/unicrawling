"""
Characterization tests for the universal normalizer.

Written BEFORE the C16 split, against the unsplit module, so the split had a net
to fall into; repointed at the new modules by that split. universal_normalizer.py had no tests at all despite sitting on a
production read path -- inspect_cli normalizes every payload it yields, so every
export and every dashboard row goes through this code.

These tests record what the normalizer *does today*, including the parts that are
wrong. Several of the values asserted here are fabricated defaults the normalizer
invents when the model returned nothing -- "Refer to Official Tuition Portal",
"Standard University Application Fee", a made-up aggregate formula, an
accreditation body derived from the country name. Those are decision D2's subject
and are stripped in C19; until then, pinning them is what makes the split
verifiable. Every such assertion is marked FABRICATED.
"""
import copy
import json
import pathlib

import pytest

from src.extractor.normalizers.currency_tuition import resolve_universal_currency
from src.extractor.normalizers.runner import (
    normalize_universal_payload,
    normalize_universal_program,
)

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "normalizer"


# --------------------------------------------------------- currency resolution --

@pytest.mark.parametrize("tuition,country,expected", [
    ("EUR 500 per semester", "Germany", "EUR"),
    ("€500", "Germany", "EUR"),
    ("500 Euro", "Germany", "EUR"),
    ("USD 30,000", "United States", "USD"),
    ("$30,000", "United States", "USD"),
    ("30000 dollars", "United States", "USD"),
    ("GBP 9,250", "United Kingdom", "GBP"),
    ("£9,250", "United Kingdom", "GBP"),
    ("9250 pounds", "United Kingdom", "GBP"),
    ("CHF 1,500", "Switzerland", "CHF"),
    ("PKR 150,000", "Pakistan", "PKR"),
    ("Rs. 150,000", "Pakistan", "PKR"),
    ("150000 rupees", "Pakistan", "PKR"),
    ("CAD 20,000", "Canada", "CAD"),
    ("AUD 40,000", "Australia", "AUD"),
])
def test_currency_is_read_out_of_the_tuition_string_when_present(tuition, country, expected):
    assert resolve_universal_currency(tuition, country) == expected


@pytest.mark.parametrize("country,expected", [
    ("Pakistan", "PKR"),
    ("Germany", "EUR"),
    ("United States", "USD"),
])
def test_currency_falls_back_to_the_country_map_when_tuition_is_silent(country, expected):
    assert resolve_universal_currency(None, country) == expected
    assert resolve_universal_currency("", country) == expected


def test_currency_defaults_to_pkr_for_an_unknown_country():
    # The historical default: this started as a Pakistan-only pipeline.
    assert resolve_universal_currency(None, "Atlantis") == "PKR"
    assert resolve_universal_currency(None, "") == "PKR"


def test_currency_uses_eur_for_an_unmapped_european_country():
    assert resolve_universal_currency(None, "Eastern Europe") == "EUR"


def test_tuition_string_beats_the_country_map():
    # A German university quoting USD is quoting USD, not EUR.
    assert resolve_universal_currency("USD 5,000", "Germany") == "USD"


# ------------------------------------------------------------ tuition defaults --

def _prog(**kw):
    base = {"name": "BS CS", "tuition_fee": None, "currency": None}
    base.update(kw)
    return base


@pytest.mark.parametrize("country", ["Germany", "Finland", "Norway", "Austria"])
def test_tuition_free_countries_get_the_semester_contribution_note(country):
    out = normalize_universal_program(_prog(), country)
    assert out["tuition_fee"] == "Tuition Free (Semester Contribution applies)"
    # FABRICATED (D2/C19): invents a fee schedule the sources never stated.
    assert out["application_fee"] == "Uni-Assist €75 / Free Direct Application"


def test_missing_tuition_elsewhere_gets_a_pointer_not_a_number():
    out = normalize_universal_program(_prog(), "Pakistan")
    # FABRICATED (D2/C19), but at least it does not invent an amount.
    assert out["tuition_fee"] == "Refer to Official Tuition Portal"


@pytest.mark.parametrize("empty", [None, "", "  ", "null", "None", "n/a", "0"])
def test_these_all_count_as_missing_tuition(empty):
    out = normalize_universal_program(_prog(tuition_fee=empty), "Pakistan")
    assert out["tuition_fee"] == "Refer to Official Tuition Portal"


def test_a_real_tuition_figure_is_left_alone():
    out = normalize_universal_program(_prog(tuition_fee="PKR 150,000 per semester"), "Pakistan")
    assert out["tuition_fee"] == "PKR 150,000 per semester"


def test_missing_application_fee_is_filled_in():
    out = normalize_universal_program(_prog(tuition_fee="PKR 1"), "Pakistan")
    # FABRICATED (D2/C19).
    assert out["application_fee"] == "Standard University Application Fee"


def test_a_stated_application_fee_is_left_alone():
    out = normalize_universal_program(_prog(application_fee="PKR 2,500"), "Pakistan")
    assert out["application_fee"] == "PKR 2,500"


# -------------------------------------------------------- eligibility defaults --

EU = ["Germany", "France", "Italy", "Netherlands", "Switzerland", "Finland"]
ANGLO = ["United States", "USA", "United Kingdom", "UK", "Canada", "Australia"]


@pytest.mark.parametrize("country", EU)
def test_eligibility_defaults_for_europe(country):
    out = normalize_universal_program(_prog(), country)
    elig = out["eligibility_requirements"]
    # FABRICATED (D2/C19): no source said this programme uses Abitur NC.
    assert elig["minimum_marks_percentage"] == "Abitur NC Grade / ECTS Credit Prerequisites"
    assert elig["aggregate_formula"] == "ECTS & Academic Degree Evaluation"


@pytest.mark.parametrize("country", ANGLO)
def test_eligibility_defaults_for_anglophone_countries(country):
    out = normalize_universal_program(_prog(), country)
    elig = out["eligibility_requirements"]
    # FABRICATED (D2/C19).
    assert elig["minimum_marks_percentage"] == "High School Diploma / GPA Equivalent"
    assert elig["aggregate_formula"] == "GPA & Standardized Test Evaluation"


def test_eligibility_defaults_for_everywhere_else():
    out = normalize_universal_program(_prog(), "Pakistan")
    elig = out["eligibility_requirements"]
    # FABRICATED (D2/C19): a specific weighted formula, invented wholesale. This is
    # the single most misleading value the normalizer produces -- a student could
    # act on it.
    assert elig["minimum_marks_percentage"] == "Intermediate / HSSC (60% Minimum)"
    assert elig["aggregate_formula"] == "Matric (10%) + HSSC (40%) + Entry Test (50%)"


def test_stated_eligibility_is_never_overwritten():
    stated = {"minimum_marks_percentage": "70%", "aggregate_formula": "FSc 50% + NET 50%"}
    out = normalize_universal_program(_prog(eligibility_requirements=dict(stated)), "Pakistan")
    assert out["eligibility_requirements"] == stated


# ------------------------------------------------------------- payload level --

def _payload(**main):
    base = {"name": "Test University", "website": "https://test.edu.pk", "country": "Pakistan"}
    base.update(main)
    return {"main_info": base, "programs": {"undergraduate": [], "graduate": [],
                                            "postgraduate_and_phd": []}}


def test_instruction_language_is_country_aware():
    de = normalize_universal_payload(_payload(country="Germany"))
    pk = normalize_universal_payload(_payload(country="Pakistan"))
    assert de["main_info"]["primary_instruction_language"] == "German / English"
    assert pk["main_info"]["primary_instruction_language"] == "English"


def test_accreditation_body_falls_back_to_the_country_name():
    out = normalize_universal_payload(_payload(country="Kenya"))
    # FABRICATED (D2/C19): "Ministry of Higher Education (Kenya)" may not exist.
    assert out["main_info"]["accreditation_body"] == "Ministry of Higher Education (Kenya)"


@pytest.mark.parametrize("name,year,body", [
    ("LMU Munich", 1472, "Bavarian State Ministry of Science and the Arts"),
    ("ITU Lahore", 2012, "Higher Education Commission (HEC)"),
    ("NUST Islamabad", 1991, "HEC / PEC"),
])
def test_three_universities_are_hardcoded_by_name(name, year, body):
    # Hardcoded identity facts keyed off a substring of the name. Fragile -- any
    # university whose name contains "itu" (Institute of Technology Umea, say)
    # collects ITU Lahore's founding year. Recorded so C19 can decide its fate.
    out = normalize_universal_payload(_payload(name=name))
    assert out["main_info"]["established_year"] == year
    assert out["main_info"]["accreditation_body"] == body


def test_a_stated_established_year_is_never_overwritten():
    out = normalize_universal_payload(_payload(name="NUST", established_year=1885))
    assert out["main_info"]["established_year"] == 1885


def test_programs_are_normalized_in_place_across_all_three_categories():
    payload = _payload()
    payload["programs"] = {
        "undergraduate": [_prog(name="BS CS")],
        "graduate": [_prog(name="MS CS")],
        "postgraduate_and_phd": [_prog(name="PhD CS")],
    }
    out = normalize_universal_payload(payload)
    for key in ("undergraduate", "graduate", "postgraduate_and_phd"):
        assert out["programs"][key][0]["tuition_fee"] == "Refer to Official Tuition Portal"


# ------------------------------------------------- golden files, real payloads --

@pytest.mark.parametrize("slug", ["aiou", "lums", "nust"])
def test_real_payloads_normalize_to_their_golden_output(slug):
    """
    Byte-for-byte against output captured from the pre-split module.

    These three came out of tag `pre-refactor` -- real Phase 3 output, not
    hand-written. They are the small ones; the full 8 were diffed once as the C16
    gate, which is what the commit message reports.
    """
    src = json.loads((FIXTURES / f"{slug}.input.json").read_text())
    golden = (FIXTURES / f"{slug}.golden.json").read_text()
    got = normalize_universal_payload(copy.deepcopy(src))
    assert json.dumps(got, indent=2, ensure_ascii=False, sort_keys=True) + "\n" == golden


def test_normalizer_mutates_its_argument():
    # Documented, not endorsed: inspect_cli relies on the return value, but any
    # caller reusing the input dict afterwards gets the normalized version.
    payload = _payload()
    returned = normalize_universal_payload(payload)
    assert returned is payload
