"""
Normalizer behaviour tests.

Written BEFORE the C16 split, against the unsplit module, so the split had a net
to fall into, and repointed at the new modules by that split.
universal_normalizer.py had no tests at all despite sitting on a production read
path -- inspect_cli normalizes every payload it yields, so every export and every
dashboard row goes through this code.

Up to C18 the assertions marked FABRICATED pinned values the normalizer invented
when the extractor returned nothing: a made-up aggregate admission formula, fee
strings, eligibility criteria chosen by country, an accreditation body derived
from the country name, and founding years hardcoded for three universities
matched by name substring. Those were decision D2's subject.

**D2 was answered: strip them.** The pipeline targets universities worldwide, and
every one of those defaults encoded an assumption about Pakistan or western
Europe -- the eligibility branch handed the entire rest of the world Pakistan's
HSSC formula, which is wrong for the Kenyan, Tanzanian and Ugandan programmes
already in the corpus. Each former FABRICATED assertion is now its inverse: the
field stays null and the inspector's empty-field audit gets to report it.
"""
import copy
import json
import pathlib

import pytest

from src.extractor.normalizers.currency_tuition import resolve_universal_currency
from src.extractor.normalizers.runner import (
    PROGRAM_BUCKETS,
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


def test_currency_is_none_for_an_unmapped_country():
    """Was PKR, from when this was a Pakistan-only pipeline. That default quietly
    asserted every unmapped country's fees were in rupees -- and the map covers a
    small fraction of the world's countries, so the tail was the common case."""
    assert resolve_universal_currency(None, "Atlantis") is None
    assert resolve_universal_currency(None, "") is None
    assert resolve_universal_currency(None, None) is None


def test_currency_markers_are_matched_on_word_boundaries():
    """`"RS" in text.upper()` matched inside COURSE, so any fee mentioning a
    course was labelled PKR. Same substring-versus-token defect the link filter
    and the degree mapper each had to fix."""
    assert resolve_universal_currency("Fee for all courses: 5000", "Kenya") == "KES"
    assert resolve_universal_currency("Rs. 150,000", "Pakistan") == "PKR"


def test_the_currency_map_reaches_beyond_europe_and_south_asia():
    for country, code in [("Kenya", "KES"), ("Tanzania", "TZS"), ("Uganda", "UGX"),
                          ("Nigeria", "NGN"), ("Brazil", "BRL"), ("Japan", "JPY")]:
        assert resolve_universal_currency(None, country) == code


def test_tuition_string_beats_the_country_map():
    # A German university quoting USD is quoting USD, not EUR.
    assert resolve_universal_currency("USD 5,000", "Germany") == "USD"


# ------------------------------------------------------------ tuition defaults --

def _prog(**kw):
    base = {"name": "BS CS", "tuition_fee": None, "currency": None}
    base.update(kw)
    return base


@pytest.mark.parametrize("country", ["Germany", "Finland", "Norway", "Austria",
                                    "Pakistan", "Kenya", "Atlantis"])
def test_a_missing_tuition_fee_stays_missing(country):
    """No country gets a fee written for it.

    The removed branch claimed "Tuition Free (Semester Contribution applies)" for
    four European countries -- an outright claim about money, wrong for every
    non-EU-student and executive programme those universities run -- and
    "Refer to Official Tuition Portal" for everywhere else.
    """
    out = normalize_universal_program(_prog(), country)
    assert out["tuition_fee"] is None


def test_a_missing_application_fee_stays_missing():
    out = normalize_universal_program(_prog(tuition_fee="PKR 1"), "Pakistan")
    assert out.get("application_fee") is None


def test_a_real_tuition_figure_is_left_alone():
    out = normalize_universal_program(_prog(tuition_fee="PKR 150,000 per semester"), "Pakistan")
    assert out["tuition_fee"] == "PKR 150,000 per semester"


def test_a_stated_application_fee_is_left_alone():
    out = normalize_universal_program(_prog(application_fee="PKR 2,500"), "Pakistan")
    assert out["application_fee"] == "PKR 2,500"


# -------------------------------------------------------- eligibility defaults --

ALL_OVER = ["Germany", "France", "Switzerland", "United States", "United Kingdom",
            "Canada", "Australia", "Pakistan", "Kenya", "Tanzania", "Uganda",
            "Brazil", "Japan", "Atlantis", ""]


@pytest.mark.parametrize("country", ALL_OVER)
def test_no_country_gets_eligibility_criteria_invented_for_it(country):
    """The removed code had three branches: western Europe, the anglophone
    countries, and an `else` that handed EVERYWHERE ELSE Pakistan's
    "Intermediate / HSSC (60% Minimum)" and the weighted formula
    "Matric (10%) + HSSC (40%) + Entry Test (50%)".

    That formula was the single most misleading value the normalizer produced --
    a specific admission calculation, stated with no source, that a student could
    plan an application around. The AKU payload alone covers Kenya, Tanzania and
    Uganda, none of which have an HSSC.
    """
    elig = normalize_universal_program(_prog(), country)["eligibility_requirements"]
    assert elig["minimum_marks_percentage"] is None
    assert elig["aggregate_formula"] is None
    assert elig["entry_tests_accepted"] == []


@pytest.mark.parametrize("empty", ["null", "None", "N/A", "  ", "", "not specified"])
def test_the_extractors_words_for_nothing_become_real_nulls(empty):
    """So the inspector's empty-field audit can see them. Normalisation, not
    invention: no fact is added, one is made legible."""
    prog = _prog(eligibility_requirements={"minimum_marks_percentage": empty,
                                           "aggregate_formula": empty,
                                           "entry_tests_accepted": [empty]})
    elig = normalize_universal_program(prog, "Pakistan")["eligibility_requirements"]
    assert elig["minimum_marks_percentage"] is None
    assert elig["aggregate_formula"] is None
    assert elig["entry_tests_accepted"] == []


def test_stated_eligibility_is_never_overwritten():
    stated = {"minimum_marks_percentage": "70%", "aggregate_formula": "FSc 50% + NET 50%"}
    out = normalize_universal_program(_prog(eligibility_requirements=dict(stated)), "Pakistan")
    # entry_tests_accepted is always present afterwards, empty when unstated:
    # the inspector counts empty fields by reading them, so an absent key and a
    # null one must not be two different things.
    assert out["eligibility_requirements"] == {**stated, "entry_tests_accepted": []}


# ------------------------------------------------------------- payload level --

def _payload(**main):
    base = {"name": "Test University", "website": "https://test.edu.pk", "country": "Pakistan"}
    base.update(main)
    return {"main_info": base, "programs": {"bachelors": [], "masters": [],
                                            "phd": [], "diploma": []}}


@pytest.mark.parametrize("country", ["Germany", "Pakistan", "Kenya", "Japan", None])
def test_instruction_language_is_never_asserted(country):
    """Was "English", or "German / English" for Germany -- claimed for a
    university in any country on earth, including ones where it is simply false."""
    out = normalize_universal_payload(_payload(country=country))
    assert out["main_info"].get("primary_instruction_language") is None


@pytest.mark.parametrize("country", ["Kenya", "Pakistan", "Brazil", None])
def test_accreditation_body_is_not_derived_from_the_country_name(country):
    """Was "Ministry of Higher Education (<country>)" -- a body that under that
    exact name does not exist in most countries."""
    out = normalize_universal_payload(_payload(country=country))
    assert out["main_info"].get("accreditation_body") is None


@pytest.mark.parametrize("name", ["LMU Munich", "ITU Lahore", "NUST Islamabad",
                                  "Institute of Technology Umea"])
def test_no_university_gets_identity_facts_hardcoded_by_name(name):
    """Three universities used to have their founding year and accrediting body
    written in, matched by NAME SUBSTRING -- so "Institute of Technology Umea"
    contains "itu" and collected ITU Lahore's 2012 and Pakistan's HEC.

    The registry lookup supplies exactly these facts, sourced, for every
    university in resources/rankings_global.json. Anything it does not cover
    stays null.
    """
    out = normalize_universal_payload(_payload(name=name))
    assert out["main_info"].get("established_year") is None
    assert out["main_info"].get("accreditation_body") is None


def test_a_stated_established_year_is_never_overwritten():
    out = normalize_universal_payload(_payload(name="NUST", established_year=1885))
    assert out["main_info"]["established_year"] == 1885


def test_registry_facts_are_still_applied():
    """Stripping the fabrications must not take the sourced facts with them."""
    out = normalize_universal_payload(
        _payload(name="NUST", website="https://nust.edu.pk"))
    assert out["main_info"]["established_year"]
    assert out["main_info"]["accreditation_body"]


def test_programs_are_normalized_in_place_across_all_four_categories():
    payload = _payload()
    payload["programs"] = {
        "bachelors": [_prog(name="BS CS")],
        "masters": [_prog(name="MS CS")],
        "phd": [_prog(name="PhD CS")],
        "diploma": [_prog(name="PGD Data Science")],
    }
    out = normalize_universal_payload(payload)
    for key in PROGRAM_BUCKETS:
        program = out["programs"][key][0]
        # Every bucket is walked: each programme comes back with the normalizer's
        # own keys present, and with nothing invented in them.
        assert program["tuition_fee"] is None
        assert program["description"] is None
        assert program["application_deadlines"] == []
        assert program["eligibility_requirements"]["aggregate_formula"] is None


def test_retired_buckets_are_folded_into_the_canonical_four():
    """A payload written before C17 must not lose its programmes on read.

    inspect_cli re-normalizes every record it loads, so if the retired bucket
    names survived here the inspector's audits, search and CSV export would all
    report zero programmes for every university extracted before this commit.
    """
    payload = _payload()
    payload["programs"] = {
        "undergraduate": [_prog(name="BS CS")],
        "graduate": [_prog(name="MS CS")],
        "postgraduate_and_phd": [_prog(name="PhD CS")],
    }
    out = normalize_universal_payload(payload)
    assert set(out["programs"]) == set(PROGRAM_BUCKETS)
    assert [p["name"] for p in out["programs"]["bachelors"]] == ["BS CS"]
    assert [p["name"] for p in out["programs"]["masters"]] == ["MS CS"]
    assert [p["name"] for p in out["programs"]["phd"]] == ["PhD CS"]
    assert out["programs"]["diploma"] == []


def test_retired_and_canonical_buckets_merge_rather_than_overwrite():
    """A half-migrated payload carrying both names keeps every programme."""
    payload = _payload()
    payload["programs"] = {
        "undergraduate": [_prog(name="BS CS")],
        "bachelors": [_prog(name="BBA")],
    }
    out = normalize_universal_payload(payload)
    assert sorted(p["name"] for p in out["programs"]["bachelors"]) == ["BBA", "BS CS"]


def test_degree_level_is_settled_from_the_programme_name():
    """The name outranks the declared level, which the extractor gets wrong.

    "Doctorate of Physical Therapy" arrived filed as postgraduate_and_phd in one
    real payload and as undergraduate in another. DPT is a five-year
    post-intermediate degree here, so bachelors is the answer in both.
    """
    payload = _payload()
    payload["programs"] = {
        "phd": [_prog(name="Doctorate of Physical Therapy", degree_level="postgraduate_phd")],
    }
    out = normalize_universal_payload(payload)
    assert out["programs"]["phd"][0]["degree_level"] == "bachelors"


# ------------------------------------------------- golden files, real payloads --

@pytest.mark.parametrize("slug", ["aiou", "lums", "nust"])
def test_real_payloads_normalize_to_their_golden_output(slug):
    """
    Byte-for-byte against output captured from the pre-split module.

    These three came out of tag `pre-refactor` -- real Phase 3 output, not
    hand-written. They are the small ones; the full 8 were diffed once as the C16
    gate, which is what the commit message reports.

    Re-captured once, at C17, for the bucket rename. That regeneration was gated
    the same way: all 8 payloads pre-C17 vs post-C17 showed no change to
    main_info, contact, faculties or the truncation flag, no programme count or
    order drift, and no field change other than degree_level -- 260 of which were
    the straight three-to-four rename and 3 of which were real reclassifications
    the mapper corrected (see test_degree_names.py).
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
