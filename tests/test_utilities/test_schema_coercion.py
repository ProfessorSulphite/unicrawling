"""
Schema-level coercion of messy LLM output.

sub_campuses_contact arrives as free-text strings as often as structured objects;
the model has to accept both without losing the campus/detail split.

Moved out of tests/test_pipeline.py in C15.
"""
from src.utilities.schema import ContactInfo


def test_sub_campuses_contact_coercion():
    c = ContactInfo(sub_campuses_contact=[
        "Kenya Campus: 3rd Parklands (Tel: +254 20 366 2424)",
        "Tanzania Campus: Plot 34",
        {"campus_name": "Uganda Campus", "contact_details": "Plot 9/11"}
    ])
    assert len(c.sub_campuses_contact) == 3
    assert c.sub_campuses_contact[0].campus_name == "Kenya Campus"
    assert c.sub_campuses_contact[0].contact_details == "3rd Parklands (Tel: +254 20 366 2424)"
    assert c.sub_campuses_contact[1].campus_name == "Tanzania Campus"
    assert c.sub_campuses_contact[2].campus_name == "Uganda Campus"
