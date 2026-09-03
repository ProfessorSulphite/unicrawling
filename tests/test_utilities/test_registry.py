"""
The registry is where identity facts come from instead of the model.

C25 merged rankings_pk.json into rankings_global.json. The split had a real cost:
every pk entry carried `rankings: []`, and the extractor assigned that over the
payload, so NUST's QS rank of 353 and LUMS's 540 -- both sitting in the *other*
registry the whole time -- were written out empty on every run.
"""
import json

import pytest

from src.utilities.registry import canonical_domain, load_registry, lookup, reset_cache


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_cache()
    yield
    reset_cache()


# ------------------------------------------------------ resolving a domain --

@pytest.mark.parametrize("given,expected", [
    ("nust.edu.pk", "nust.edu.pk"),
    ("www.nust.edu.pk", "nust.edu.pk"),
    ("NUST.EDU.PK", "nust.edu.pk"),
    ("https://nust.edu.pk", "nust.edu.pk"),
    ("https://www.nust.edu.pk/admissions", "nust.edu.pk"),
    ("http://nust.edu.pk/", "nust.edu.pk"),
    ("  nust.edu.pk  ", "nust.edu.pk"),
])
def test_canonical_domain(given, expected):
    assert canonical_domain(given) == expected


def test_lookup_by_canonical_domain():
    assert lookup("nust.edu.pk")["abbreviation"] == "NUST"


def test_lookup_by_alias():
    """seecs.nust.edu.pk is NUST; a run started there must not come back empty."""
    assert lookup("seecs.nust.edu.pk")["abbreviation"] == "NUST"
    assert lookup("pnec.nust.edu.pk")["abbreviation"] == "NUST"


def test_lookup_by_subdomain_is_anchored_on_a_dot():
    """'notnust.edu.pk' ends with 'nust.edu.pk' as a substring but is not NUST."""
    assert lookup("notnust.edu.pk") is None


def test_lookup_misses_return_none():
    assert lookup("unknown-university.example") is None
    assert lookup("") is None


# --------------------------------------------------- what the merge carried --

def test_every_entry_has_a_country():
    """
    Post-C19 nothing defaults country. The registry is where a known
    university's country comes from, so an entry without one supplies nothing.
    """
    registry = load_registry()
    missing = [d for d, e in registry.items() if not e.get("country")]
    assert missing == []


def test_the_merge_kept_both_registries_entries():
    registry = load_registry()
    # From rankings_pk.json
    for d in ("qau.edu.pk", "giki.edu.pk", "uaf.edu.pk", "bahria.edu.pk"):
        assert d in registry, f"{d} was lost in the C25 merge"
    # From rankings_global.json
    for d in ("lmu.de", "tum.de"):
        assert d in registry, f"{d} was lost in the C25 merge"


def test_the_merge_kept_both_schemas_fields():
    """
    pk contributed `type` and `aliases`; global contributed `country`,
    `established_year` and `accreditation_body`. A merge that dropped either
    side's columns would look complete and be lossy.
    """
    nust = lookup("nust.edu.pk")
    assert nust["type"] == "public"                 # pk-only field
    assert nust["aliases"]                          # pk-only field
    assert nust["country"] == "Pakistan"            # global-only field
    assert nust["established_year"] == 1991         # global-only field
    assert nust["accreditation_body"]               # global-only field


def test_the_sourced_rankings_survived_the_merge():
    """The bug C25 fixes: these existed and were being written out as empty."""
    assert [r["rank"] for r in lookup("nust.edu.pk")["rankings"]] == [353]
    assert [r["rank"] for r in lookup("lums.edu.pk")["rankings"]] == [540]


def test_an_unranked_university_has_an_empty_list_not_a_missing_key():
    """apply_registry_facts reads entry["rankings"] to erase invented ranks."""
    assert lookup("itu.edu.pk")["rankings"] == []


def test_no_entry_carries_a_ranking_without_a_source():
    for domain, entry in load_registry().items():
        for r in entry.get("rankings", []):
            assert r.get("source"), f"{domain} has an unsourced ranking"
            assert r.get("year"), f"{domain} has a ranking with no year"


# ------------------------------------------------------------- degradation --

def test_a_missing_registry_file_degrades_rather_than_raising(monkeypatch, tmp_path):
    """A run must survive a missing registry; it just gets no sourced facts."""
    monkeypatch.setattr("src.config.config.rankings_json_path", tmp_path / "absent.json")
    reset_cache()
    assert load_registry() == {}
    assert lookup("nust.edu.pk") is None


def test_a_corrupt_registry_file_degrades_rather_than_raising(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("src.config.config.rankings_json_path", bad)
    reset_cache()
    assert load_registry() == {}


def test_the_loader_reads_the_path_at_call_time(monkeypatch, tmp_path):
    """
    A module-level snapshot of config.rankings_json_path would freeze it, so a
    test pointing Config at a fixture would silently keep reading the real file.
    That is the same by-value binding hazard the shims had.
    """
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(
        {"universities": {"example.edu": {"name": "Example", "abbreviation": "EX"}}}
    ), encoding="utf-8")
    monkeypatch.setattr("src.config.config.rankings_json_path", fixture)
    reset_cache()
    assert lookup("example.edu")["abbreviation"] == "EX"
    assert lookup("nust.edu.pk") is None
