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


# =============================================================================
# The ledger is append-only: the LAST row for a university is the current one
# =============================================================================

def _payload(name, programme, extra=None):
    """A minimal current-shape payload with one bachelors programme."""
    doc = {
        "main_info": {
            "name": name,
            "abbreviation": "X",
            "type": "public",
            "city": "Lahore",
            "country": "Pakistan",
            "website": "https://x.edu.pk",
            "key_links": {},
        },
        "programs": {"bachelors": [{"name": programme, "degree_level": "bachelors"}],
                     "masters": [], "phd": [], "diploma": []},
        "faculties": [],
        "contact": {},
    }
    doc.update(extra or {})
    return doc


@pytest.fixture
def ledger_corpus(monkeypatch, tmp_path):
    """A ledger holding three runs of one university, oldest first."""
    uni_outputs = tmp_path / "uni_outputs"
    uni_outputs.mkdir(parents=True, exist_ok=True)
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n".join(json.dumps(r) for r in [
        _payload("Information Technology University", "BS Oldest"),
        _payload("Information Technology University", "BS Middle"),
        _payload("Information Technology University", "BS Newest"),
    ]) + "\n", encoding="utf-8")

    monkeypatch.setattr("src.inspector.records.config.outputs_uni_outputs_dir", uni_outputs)
    monkeypatch.setattr("src.inspector.records.config.output_jsonl_path", ledger)
    return tmp_path, ledger, uni_outputs


def test_a_re_run_university_reads_back_as_its_newest_row(ledger_corpus):
    """
    Re-running a university APPENDS to the ledger. This reader kept the first row
    per name while stream_compile_master_json keeps the last -- two opposite
    rules over one file, so the master JSON held the newest payload and every
    inspector command held the oldest.

    Observed 2026-09-05: ITU's newest run extracted 20 programmes into the master
    JSON while `cli.py audit` reported 13 and flagged a bucket-contamination bug
    that had been fixed two days earlier. It was auditing the older run.
    """
    records = load_all_records()
    assert len(records) == 1, "three runs of one university are one university"
    assert [p["name"] for p in records[0]["programs"]["bachelors"]] == ["BS Newest"]


def test_the_audit_door_and_the_master_compiler_pick_the_same_row(ledger_corpus):
    """
    The regression that matters is the disagreement, not either rule alone: the
    Supabase readiness gate reads through iter_all_records while the file it
    gates the push of is built by stream_compile_master_json.
    """
    from src.utilities.json_io import stream_compile_master_json

    _, ledger, _ = ledger_corpus
    master = ledger.with_name("master.json")
    stream_compile_master_json(ledger, master)

    compiled = json.loads(master.read_text(encoding="utf-8"))
    streamed = load_all_records()
    assert len(compiled) == len(streamed) == 1
    assert ([p["name"] for p in compiled[0]["programs"]["bachelors"]]
            == [p["name"] for p in streamed[0]["programs"]["bachelors"]])


def test_identity_is_case_insensitive_like_the_compilers(ledger_corpus):
    """Both readers key on the casefolded name; a shouted name is not a new university."""
    _, ledger, _ = ledger_corpus
    ledger.write_text("\n".join(json.dumps(r) for r in [
        _payload("Information Technology University", "BS Oldest"),
        _payload("INFORMATION TECHNOLOGY UNIVERSITY", "BS Newest"),
    ]) + "\n", encoding="utf-8")

    records = load_all_records()
    assert len(records) == 1
    assert [p["name"] for p in records[0]["programs"]["bachelors"]] == ["BS Newest"]


def test_a_university_only_on_disk_is_still_read(ledger_corpus):
    """uni_outputs covers universities the ledger has no row for."""
    _, _, uni_outputs = ledger_corpus
    (uni_outputs / "lums.json").write_text(
        json.dumps(_payload("Lahore University of Management Sciences", "BS Accounting")),
        encoding="utf-8",
    )
    names = sorted(r["main_info"]["name"] for r in load_all_records())
    assert names == [
        "Information Technology University",
        "Lahore University of Management Sciences",
    ]


def test_a_per_slug_file_does_not_re_add_a_university_the_ledger_already_gave(ledger_corpus):
    """One university, one record, however many places hold a copy of it."""
    _, _, uni_outputs = ledger_corpus
    (uni_outputs / "itu.json").write_text(
        json.dumps(_payload("Information Technology University", "BS From Disk")),
        encoding="utf-8",
    )
    records = load_all_records()
    assert len(records) == 1
    assert [p["name"] for p in records[0]["programs"]["bachelors"]] == ["BS Newest"]


def test_an_unidentifiable_record_is_never_dropped(ledger_corpus):
    """
    The compiler keeps rows it cannot key rather than silently losing them, and
    this reader now does the same. A payload with no name is broken data, but
    invisible broken data is worse.
    """
    _, ledger, _ = ledger_corpus
    nameless = _payload("Information Technology University", "BS Nameless")
    nameless["main_info"]["name"] = ""
    ledger.write_text("\n".join(json.dumps(r) for r in [
        _payload("Information Technology University", "BS Newest"),
        nameless,
    ]) + "\n", encoding="utf-8")

    assert len(load_all_records()) == 2


def test_a_torn_final_line_costs_only_itself(ledger_corpus):
    """A crash mid-append must not make the whole ledger unreadable."""
    _, ledger, _ = ledger_corpus
    with open(ledger, "a", encoding="utf-8") as f:
        f.write('{"main_info": {"name": "Truncated Uni"')

    records = load_all_records()
    assert [p["name"] for p in records[0]["programs"]["bachelors"]] == ["BS Newest"]
