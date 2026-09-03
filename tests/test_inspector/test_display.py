"""
Every view has to survive a record C19 legitimately produces.

Before C19 the normalizer filled the gaps -- country got "Pakistan", type got
"public", currency got "PKR" -- so the display layer never met a null. C19 stopped
inventing, and `main.get('type', 'public').upper()` raises AttributeError the
moment the key is present and null, which is exactly what the extractor now writes
when it cannot read the type. `inspect` and `diff` crashed outright on such a
record; these tests pin that they render it instead.
"""
import json

import pytest

from src.inspector.dashboard import (
    _declared_currency,
    _fee_range,
    compare_universities,
    inspect_university,
    search_programs,
)
from src.inspector.formatting import show

# Nulls everywhere the schema now allows them.
NULL_RECORD = {
    "main_info": {
        "name": "Nulltype University",
        "abbreviation": "NTU",
        "type": None,
        "city": None,
        "country": None,
        "website": None,
        "description": None,
        "key_links": {},
    },
    "programs": {
        "bachelors": [
            {
                "name": "BS Something",
                "department": None,
                "duration": None,
                "tuition_fee": None,
                "currency": None,
                "eligibility_requirements": {"minimum_marks_percentage": None},
            }
        ],
        "masters": [],
        "phd": [],
        "diploma": [],
    },
    "faculties": [{"faculty_name": None, "departments": []}],
    "contact": {"official_email": None, "phone_numbers": None, "physical_address": None},
}


@pytest.fixture
def null_corpus(monkeypatch, tmp_path):
    uni_outputs = tmp_path / "uni_outputs"
    uni_outputs.mkdir(parents=True, exist_ok=True)
    (uni_outputs / "ntu.json").write_text(json.dumps(NULL_RECORD), encoding="utf-8")
    monkeypatch.setattr("src.inspector.records.config.outputs_uni_outputs_dir", uni_outputs)
    monkeypatch.setattr("src.inspector.records.config.output_jsonl_path", tmp_path / "absent.jsonl")
    return tmp_path


def test_inspect_renders_an_all_null_record(null_corpus, capsys):
    inspect_university("ntu")
    out = capsys.readouterr().out
    assert "Nulltype University" in out
    # The country is unknown, not Pakistan.
    assert "Pakistan" not in out
    assert "Unknown" in out


def test_diff_renders_an_all_null_record(null_corpus, capsys):
    compare_universities("ntu", "ntu")
    out = capsys.readouterr().out
    assert "NTU" in out
    assert "Pakistan" not in out


def test_search_renders_an_all_null_record(null_corpus, capsys):
    matches = search_programs("something")
    assert len(matches) == 1
    assert matches[0]["department"] == "N/A"


def test_no_view_labels_a_fee_column_with_an_assumed_currency(null_corpus, capsys):
    """A PKR column header is a claim about money, and it was wrong by default."""
    inspect_university("ntu")
    search_programs("something")
    assert "(PKR)" not in capsys.readouterr().out


def test_fee_range_is_labelled_only_when_the_currency_is_unambiguous():
    one = {"bachelors": [{"currency": "EUR"}, {"currency": "eur"}]}
    two = {"bachelors": [{"currency": "EUR"}], "masters": [{"currency": "PKR"}]}
    none = {"bachelors": [{"currency": None}]}

    assert _declared_currency(one) == "EUR"
    assert _declared_currency(two) is None
    assert _declared_currency(none) is None

    assert _fee_range([1000.0, 5000.0], one) == "EUR 1,000 - 5,000"
    # Two currencies in one span is not a span; the numbers stand unlabelled.
    assert _fee_range([1000.0, 5000.0], two) == "1,000 - 5,000"
    assert _fee_range([], one) == "N/A"


def test_show_renders_missing_as_missing():
    assert show(None) == "N/A"
    assert show("") == "N/A"
    assert show("   ") == "N/A"
    assert show(None, "Unknown") == "Unknown"
    assert show(" Lahore ") == "Lahore"
    assert show(0) == "0"
