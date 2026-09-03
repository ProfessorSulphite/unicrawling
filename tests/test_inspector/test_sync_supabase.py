"""
The push gate: plan section 5's last rule, which is an ordering rule.

  Local DB -> inspect -> validate -> only then push

A corpus that fails the audit must not reach a database something else reads as
authoritative. So the gate is tested before the transport is: the row mapping is
a pure function with no client, and the refusal is asserted without credentials.
"""
import json

import pytest

from src.inspector.sync import (
    SUPABASE_TABLES,
    AuditGateFailed,
    SupabaseNotConfigured,
    build_sync_plan,
    flatten_for_supabase,
    sync_to_supabase,
)

COMPLETE_PROGRAM = {
    "name": "BS Computer Science", "degree_level": "bachelors",
    "department": "Department of Computer Science", "duration": "4 years",
    "tuition_fee": "PKR 150,000 per semester", "currency": "PKR",
    "application_fee": "PKR 2,000", "admission_requirements": "Transcript and interview.",
    "application_deadlines": ["2026-08-05"], "description": "A four-year programme.",
    "eligibility_requirements": {"minimum_marks_percentage": "60%",
                                 "entry_tests_accepted": ["NTS"], "aggregate_formula": None},
}

RECORD = {
    "main_info": {
        "name": "Information Technology University", "abbreviation": "ITU",
        "country": "Pakistan", "city": "Lahore", "website": "https://www.itu.edu.pk/",
        "type": "public", "established_year": 2012, "description": "A public university.",
        "domain_verified": True, "exa_enriched": False,
        "key_links": {"application_portal_url": "https://apply.itu.edu.pk"},
        "rankings": [{"source": "QS", "scope": "Global", "subject": "Overall",
                      "year": 2026, "rank": 353, "source_url": "https://example.org"}],
    },
    "programs": {"bachelors": [COMPLETE_PROGRAM], "masters": [], "phd": [], "diploma": []},
    "faculties": [{"faculty_name": "Faculty of Computing", "departments": ["CS"]}],
    "contact": {"official_email": "admissions@itu.edu.pk", "phone_numbers": ["042-111"]},
    "programs_possibly_truncated": False,
}


@pytest.fixture
def corpus():
    """One complete record on disk, so the audit has something that passes."""
    from src.config import config

    config.outputs_uni_outputs_dir.mkdir(parents=True, exist_ok=True)
    (config.outputs_uni_outputs_dir / "itu.json").write_text(json.dumps(RECORD), encoding="utf-8")
    return config


# -------------------------------------------------------------- the mapping --

def test_the_website_becomes_the_canonical_key():
    """'https://www.itu.edu.pk/' and the registry must agree on one key."""
    rows = flatten_for_supabase(RECORD)
    assert rows["universities"][0]["domain"] == "itu.edu.pk"
    assert rows["programs"][0]["university_domain"] == "itu.edu.pk"


def test_every_table_gets_its_rows():
    rows = flatten_for_supabase(RECORD)
    assert set(rows) == set(SUPABASE_TABLES)
    assert len(rows["universities"]) == 1
    assert len(rows["programs"]) == 1
    assert len(rows["faculties"]) == 1
    assert len(rows["contacts"]) == 1
    assert len(rows["rankings"]) == 1


def test_the_programme_level_comes_from_its_bucket():
    rows = flatten_for_supabase(RECORD)
    assert rows["programs"][0]["degree_level"] == "bachelors"


def test_the_eligibility_block_is_flattened_not_dropped():
    p = flatten_for_supabase(RECORD)["programs"][0]
    assert p["eligibility_min_marks"] == "60%"
    assert p["eligibility_entry_tests"] == ["NTS"]
    assert p["eligibility_formula"] is None


def test_the_fee_keeps_the_currency_the_university_published():
    """Finding 8: labelled, never converted, and never parsed into a number."""
    p = flatten_for_supabase(RECORD)["programs"][0]
    assert p["tuition_fee"] == "PKR 150,000 per semester"
    assert p["currency"] == "PKR"


def test_nulls_survive_as_nulls():
    """C19 made these legitimately empty; the push must not re-invent them."""
    sparse = json.loads(json.dumps(RECORD))
    sparse["main_info"]["country"] = None
    sparse["programs"]["bachelors"][0]["tuition_fee"] = None

    rows = flatten_for_supabase(sparse)
    assert rows["universities"][0]["country"] is None
    assert rows["programs"][0]["tuition_fee"] is None


def test_an_unsourced_ranking_is_not_pushed():
    """Rankings come from the registry; a partial one is not a ranking."""
    bad = json.loads(json.dumps(RECORD))
    bad["main_info"]["rankings"] = [{"source": "QS", "year": None, "rank": 12}]
    assert flatten_for_supabase(bad)["rankings"] == []


def test_a_record_with_no_website_is_refused_rather_than_keyed_on_nothing():
    orphan = json.loads(json.dumps(RECORD))
    orphan["main_info"]["website"] = ""
    with pytest.raises(ValueError, match="no website"):
        flatten_for_supabase(orphan)


def test_build_sync_plan_accumulates_across_records():
    plan = build_sync_plan([RECORD, RECORD])
    assert len(plan["universities"]) == 2
    assert len(plan["programs"]) == 2


# ------------------------------------------------------------- the gate --

def test_a_failing_audit_refuses_the_push(corpus, monkeypatch):
    """
    The ordering rule. A gappy corpus must be refused before anything is
    written, and before credentials are even looked at.
    """
    gappy = json.loads(json.dumps(RECORD))
    gappy["programs"]["bachelors"][0]["application_fee"] = None
    gappy["programs"]["bachelors"][0]["admission_requirements"] = None
    gappy["programs"]["bachelors"][0]["duration"] = None
    (corpus.outputs_uni_outputs_dir / "itu.json").write_text(json.dumps(gappy), encoding="utf-8")

    with pytest.raises(AuditGateFailed):
        sync_to_supabase(dry_run=False)


def test_the_gate_fires_before_credentials_are_needed(corpus, monkeypatch):
    """
    A failing corpus with no credentials must report the audit failure, not a
    missing-config error -- otherwise configuring Supabase would appear to fix
    a data problem.
    """
    broken = json.loads(json.dumps(RECORD))
    broken["programs"]["phd"] = list(broken["programs"]["bachelors"])
    (corpus.outputs_uni_outputs_dir / "itu.json").write_text(json.dumps(broken), encoding="utf-8")

    monkeypatch.setattr("src.config.config.supabase_url", "")
    with pytest.raises(AuditGateFailed):
        sync_to_supabase(dry_run=False)


def test_a_passing_corpus_reaches_the_transport_and_stops_at_credentials(corpus, monkeypatch):
    monkeypatch.setattr("src.config.config.supabase_url", "")
    monkeypatch.setattr("src.config.config.supabase_service_key", "")
    with pytest.raises(SupabaseNotConfigured):
        sync_to_supabase(dry_run=False)


def test_dry_run_is_the_default_and_writes_nothing(corpus):
    summary = sync_to_supabase()
    assert summary["dry_run"] is True
    assert summary["audit_passed"] is True
    assert summary["rows"]["programs"] == 1
    assert summary["rows"]["universities"] == 1


def test_force_overrides_the_gate_and_says_so(corpus, monkeypatch):
    """
    The override exists so the gate does not simply get deleted the first time
    it is inconvenient -- but it is never the default and it is reported.
    """
    gappy = json.loads(json.dumps(RECORD))
    gappy["programs"]["bachelors"][0]["application_fee"] = None
    gappy["programs"]["bachelors"][0]["duration"] = None
    (corpus.outputs_uni_outputs_dir / "itu.json").write_text(json.dumps(gappy), encoding="utf-8")

    summary = sync_to_supabase(dry_run=True, force=True)
    assert summary["forced"] is True
    assert summary["audit_passed"] is False


def test_the_cli_returns_non_zero_when_the_push_is_refused(corpus):
    """So a shell script cannot ignore the refusal."""
    from src.inspector.cli import main

    gappy = json.loads(json.dumps(RECORD))
    gappy["programs"]["bachelors"][0]["application_fee"] = None
    gappy["programs"]["bachelors"][0]["duration"] = None
    (corpus.outputs_uni_outputs_dir / "itu.json").write_text(json.dumps(gappy), encoding="utf-8")

    assert main(["sync", "--no-dry-run"]) == 1


# --------------------------------------------------------------- the DDL --

def test_the_schema_file_and_the_auditor_agree_on_what_cannot_be_null():
    """
    The four NOT NULLs must be exactly the auditor's critical fields plus
    university identity. If they drift, either good rows get rejected or a row
    with no name reaches the database.
    """
    from pathlib import Path

    from src.utilities.schema import CRITICAL_PROGRAM_FIELDS

    ddl = (Path(__file__).resolve().parents[2] / "resources" / "supabase_schema.sql").read_text()
    programs_block = ddl.split("create table if not exists programs")[1].split(");")[0]

    for field in CRITICAL_PROGRAM_FIELDS:
        assert f"{field} " in programs_block, f"{field} missing from the programs table"
        line = next(l for l in programs_block.splitlines() if l.strip().startswith(field + " "))
        assert "not null" in line.lower(), f"{field} is critical but nullable in the DDL"


def test_the_ddl_enforces_the_four_degree_levels():
    from pathlib import Path

    from src.utilities.schema import DegreeLevel

    ddl = (Path(__file__).resolve().parents[2] / "resources" / "supabase_schema.sql").read_text()
    for level in DegreeLevel:
        assert f"'{level.value}'" in ddl, f"{level.value} missing from the degree_level check"
