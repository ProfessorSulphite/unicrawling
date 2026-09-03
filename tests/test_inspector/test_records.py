"""
The read layer has to hand every caller the same record.

`iter_all_records` normalizes on the way out; `find_university_record` used to
return the file verbatim whenever the filename matched the query, and fell back
to the normalized path otherwise. So one stored file read two different ways
depending on how it was found -- and a payload in a pre-C17 shape reported zero
programmes to `inspect` and `diff` while `search` and `export` saw them all.
"""
import json

import pytest

from src.inspector.records import find_university_record, load_all_records

# A payload in the shape the extractor wrote before C17 renamed the buckets and
# before C18 made the deadline a list. Real corpora still hold files like this.
PRE_C17_PAYLOAD = {
    "main_info": {
        "name": "Legacy Institute of Technology",
        "abbreviation": "LIT",
        "type": "public",
        "city": "Lahore",
        "country": "Pakistan",
        "website": "https://lit.example.edu",
        "key_links": {"application_portal_url": "https://lit.example.edu/apply"},
    },
    "programs": {
        "undergraduate": [{"name": "BS Computer Science", "application_deadline": "2026-08-05"}],
        "graduate": [{"name": "MS Data Science"}],
        "postgraduate_and_phd": [{"name": "PhD Computer Science"}],
    },
    "faculties": [],
    "contact": {},
}


@pytest.fixture
def legacy_corpus(monkeypatch, tmp_path):
    """One pre-C17 record, reachable both by filename and by the fallback scan."""
    uni_outputs = tmp_path / "uni_outputs"
    uni_outputs.mkdir(parents=True, exist_ok=True)
    (uni_outputs / "lit.json").write_text(json.dumps(PRE_C17_PAYLOAD), encoding="utf-8")

    # config is the shared singleton: patching its attributes reaches every module.
    monkeypatch.setattr("src.inspector.records.config.outputs_uni_outputs_dir", uni_outputs)
    monkeypatch.setattr("src.inspector.records.config.output_jsonl_path", tmp_path / "absent.jsonl")
    return tmp_path


def test_filename_hit_is_normalized_like_the_streamed_path(legacy_corpus):
    """
    'lit' matches the filename, so this takes the fast path. It must still come
    back in the current shape.
    """
    record = find_university_record("lit")
    assert record is not None

    programs = record["programs"]
    assert [p["name"] for p in programs["bachelors"]] == ["BS Computer Science"]
    assert [p["name"] for p in programs["masters"]] == ["MS Data Science"]
    assert [p["name"] for p in programs["phd"]] == ["PhD Computer Science"]

    # Retired bucket names are gone, not merely shadowed by the new ones.
    assert "undergraduate" not in programs
    assert "graduate" not in programs
    assert "postgraduate_and_phd" not in programs

    # C18's list-valued deadline is migrated too.
    assert programs["bachelors"][0]["application_deadlines"] == ["2026-08-05"]


def test_both_lookup_paths_agree(legacy_corpus):
    """
    'legacy' matches no filename, so this falls through to the scan over
    load_all_records(). Both routes must produce the same record.
    """
    by_filename = find_university_record("lit")
    by_scan = find_university_record("legacy")
    assert by_scan is not None
    assert by_scan["programs"] == by_filename["programs"]


def test_a_current_shape_record_is_unchanged(legacy_corpus):
    """Normalizing on read must be idempotent -- reading twice cannot drift."""
    once = find_university_record("lit")
    twice = load_all_records()
    assert len(twice) == 1
    assert twice[0]["programs"] == once["programs"]
