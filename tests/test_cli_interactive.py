"""
Unit tests for Interactive CLI Expansion & Vector DB Exporter (src/inspect_cli.py)
"""
import json
import csv
import inspect
from pathlib import Path

import pytest

from src.inspect_cli import (
    extract_numeric_fee,
    find_university_record,
    compare_universities,
    search_programs,
    export_dataset,
)


@pytest.fixture
def mock_master_data(monkeypatch, tmp_path):
    output_jsonl = tmp_path / "university_counseling_data.jsonl"
    uni_outputs = tmp_path / "uni_outputs"
    uni_outputs.mkdir(parents=True, exist_ok=True)

    records = [
        {
            "main_info": {
                "name": "Information Technology University",
                "abbreviation": "ITU",
                "type": "public",
                "city": "Lahore",
                "country": "Pakistan",
                "website": "https://itu.edu.pk",
                "description": "Tech university in Lahore",
                "key_links": {
                    "academics_url": "https://itu.edu.pk/academics",
                    "admissions_url": "https://itu.edu.pk/admissions",
                    "application_portal_url": "https://itu.edu.pk/apply",
                },
            },
            "programs": {
                "undergraduate": [
                    {
                        "name": "BS Computer Science",
                        "department": "Computer Science",
                        "degree_level": "undergraduate",
                        "duration": "4 Years",
                        "tuition_fee": "PKR 120,000 / semester",
                        "summary_3_lines": "Premier computer science program focusing on software engineering and AI.",
                        "application_deadline": "2026-08-15",
                    }
                ],
                "graduate": [
                    {
                        "name": "MS Data Science",
                        "department": "Computer Science",
                        "degree_level": "graduate",
                        "duration": "2 Years",
                        "tuition_fee": "PKR 150,000 / semester",
                        "summary_3_lines": "Advanced data science and machine learning research.",
                        "application_deadline": "2026-08-20",
                    }
                ],
                "postgraduate_and_phd": [],
            },
            "faculties": [
                {
                    "faculty_name": "Faculty of Engineering & Technology",
                    "departments": ["Computer Science", "Electrical Engineering"],
                }
            ],
            "contact": {
                "official_email": "admissions@itu.edu.pk",
                "phone_numbers": ["+92-42-99232531"],
                "physical_address": "Ferozepur Road, Lahore",
            },
        },
        {
            "main_info": {
                "name": "National College of Business Administration and Economics",
                "abbreviation": "NCBAE",
                "type": "private",
                "city": "Lahore",
                "country": "Pakistan",
                "website": "https://ncbae.edu.pk",
                "description": "Business & economics institution",
                "key_links": {
                    "application_portal_url": "https://ncbae.edu.pk/apply-now",
                },
            },
            "programs": {
                "undergraduate": [
                    {
                        "name": "BBA Honors",
                        "department": "Business Administration",
                        "degree_level": "undergraduate",
                        "duration": "4 Years",
                        "tuition_fee": "PKR 95,000 / semester",
                        "summary_3_lines": "Comprehensive business management curriculum.",
                        "application_deadline": "2026-08-10",
                    }
                ],
                "graduate": [],
                "postgraduate_and_phd": [],
            },
            "faculties": [],
            "contact": {
                "official_email": "info@ncbae.edu.pk",
                "phone_numbers": ["+92-42-35752716"],
            },
        },
    ]

    with open(output_jsonl, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    for r in records:
        slug = r["main_info"]["abbreviation"].lower()
        with open(uni_outputs / f"{slug}.json", "w", encoding="utf-8") as f:
            json.dump(r, f)

    monkeypatch.setattr("src.inspect_cli.config.output_jsonl_path", output_jsonl)
    monkeypatch.setattr("src.inspect_cli.config.outputs_uni_outputs_dir", uni_outputs)
    monkeypatch.setattr("src.inspect_cli.config.data_outputs_dir", tmp_path)

    return tmp_path, records


def test_extract_numeric_fee():
    assert extract_numeric_fee("PKR 150,000 / semester") == 150000.0
    assert extract_numeric_fee("95000 PKR") == 95000.0
    assert extract_numeric_fee("Fee unavailable") is None


def test_find_university_record(mock_master_data):
    r1 = find_university_record("itu")
    assert r1 is not None
    assert r1["main_info"]["name"] == "Information Technology University"

    r2 = find_university_record("ncbae")
    assert r2 is not None
    assert r2["main_info"]["type"] == "private"


def test_compare_universities(mock_master_data, capsys):
    compare_universities("itu", "ncbae")
    captured = capsys.readouterr()
    assert "Information Technology University" in captured.out or "ITU" in captured.out
    assert "NCBAE" in captured.out


def test_search_programs(mock_master_data, capsys):
    matches = search_programs("data science")
    captured = capsys.readouterr()
    assert len(matches) == 1
    assert matches[0]["program_name"] == "MS Data Science"
    assert matches[0]["university"] == "Information Technology University"
    assert "Matches Found" in captured.out


def test_export_csv(mock_master_data):
    tmp_path, _ = mock_master_data
    csv_out = tmp_path / "counseling_programs.csv"

    export_dataset(format_type="csv", output_path=csv_out)

    assert csv_out.exists()
    with open(csv_out, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 3
        prog_names = [row["program_name"] for row in reader]
        assert "BS Computer Science" in prog_names
        assert "MS Data Science" in prog_names
        assert "BBA Honors" in prog_names


def test_export_pinecone_payload(mock_master_data):
    tmp_path, _ = mock_master_data
    pinecone_out = tmp_path / "pinecone_export.json"

    export_dataset(format_type="pinecone", output_path=pinecone_out)

    assert pinecone_out.exists()
    with open(pinecone_out, "r", encoding="utf-8") as f:
        payload = json.load(f)
        assert "vectors" in payload
        vectors = payload["vectors"]
        assert len(vectors) == 3
        v0 = vectors[0]
        assert "id" in v0
        assert "values" in v0
        assert "metadata" in v0
        assert v0["metadata"]["uni_name"] == "Information Technology University"


def test_export_json_is_country_grouped(mock_master_data):
    """
    The production path: run_batch_pipeline calls export_dataset(format_type="json").
    It had no test before C11, which made the Qdrant excision riskier than it looked.
    """
    tmp_path, records = mock_master_data
    json_out = tmp_path / "grouped.json"

    export_dataset(format_type="json", output_path=json_out)

    assert json_out.exists()
    with open(json_out, "r", encoding="utf-8") as f:
        grouped = json.load(f)

    assert set(grouped) == {"Pakistan"}
    assert len(grouped["Pakistan"]) == len(records)

    per_country = tmp_path / "country_outputs" / "pak_output" / "pakistan_universities.json"
    assert per_country.exists(), "per-country directory output was not written"


def test_export_rejects_the_removed_qdrant_format():
    """C11 removed Qdrant; the format must be gone from the CLI's accepted choices."""
    import argparse

    import src.inspect_cli as cli

    parser_src = inspect.getsource(cli.main)
    assert "qdrant" not in parser_src.lower()
    assert '"csv", "pinecone", "json"' in parser_src
