"""
Tests for the university payload schema contract.

Extracted from tests/test_pipeline.py in C7. These assert the CURRENT 3-tier
taxonomy; C17 rewrites them for Bachelors/Masters/PhD/Diploma.
"""
import json

from src.utilities.schema import (
    ContactInfo,
    FacultyItem,
    KeyLinks,
    MainInfo,
    UniversityPayload,
)

def test_payload_serialises_all_four_blocks():
    payload = UniversityPayload(
        main_info=MainInfo(name="NUST", website="https://nust.edu.pk",
                           description="d", key_links=KeyLinks()),
        programs={"undergraduate": [], "graduate": [], "postgraduate_and_phd": []},
        faculties=[FacultyItem(faculty_name="SEECS")],
        contact=ContactInfo(),
    )
    dumped = payload.model_dump(mode="json")
    assert set(dumped) >= {"main_info", "programs", "faculties", "contact",
                           "programs_possibly_truncated"}
    assert json.loads(json.dumps(dumped))  # JSONL-serialisable


def test_application_portal_url_is_a_first_class_field():
    payload = UniversityPayload(
        main_info=MainInfo(name="NUST", website="https://nust.edu.pk", description="d",
                           key_links=KeyLinks(application_portal_url="https://portal.nust.edu.pk")),
        programs={}, contact=ContactInfo(),
    )
    assert payload.model_dump()["main_info"]["key_links"]["application_portal_url"]


