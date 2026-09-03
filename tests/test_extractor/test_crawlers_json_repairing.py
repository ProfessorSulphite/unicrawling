"""
LLM JSON repair: the highest-value tests in the suite.

NotebookLM returns prose-wrapped, fence-wrapped, citation-annotated and sometimes
truncated JSON. Every test here is a regression test for a real answer that broke
the parser, not a smoke test.

Moved out of tests/test_pipeline.py in C15.
"""
import json
from typing import List

import pytest

from src.extractor.crawlers.json_repairing import (
    ExtractionError,
    repair_and_validate_json,
    strip_citation_markers,
)
from src.utilities.schema import ApplicationStatus, ContactInfo, DegreeLevel, ProgramItem


def test_json_repair_strips_fences_and_citations():
    raw = """Here is the requested program list [1]:
    ```json
    [
      {
        "name": "BS Artificial Intelligence [2, 3]",
        "program_info_link": "https://nust.edu.pk/bsai",
        "degree_level": "undergraduate",
        "duration": "4 Years",
        "summary_3_lines": "Line 1\\nLine 2\\nLine 3",
        "eligibility_requirements": {"minimum_marks_percentage": "60%",
                                     "entry_tests_accepted": ["NET"]},
        "application_status": "open"
      }
    ]
    ```
    End of response."""
    programs = repair_and_validate_json(raw, List[ProgramItem])
    assert len(programs) == 1
    # No trailing whitespace debris: the old regex left "BS Artificial Intelligence ".
    assert programs[0].name == "BS Artificial Intelligence"
    # The fixture still says "undergraduate": that is what a real pre-C17 answer
    # looks like, and the schema's retired-value coercion is what turns it into
    # the canonical level. Leaving the raw value alone keeps that path covered.
    assert programs[0].degree_level == DegreeLevel.BACHELORS
    assert programs[0].application_status == ApplicationStatus.OPEN


def test_json_repair_preserves_numeric_arrays():
    """
    The naive citation regex \\[\\s*\\d+(,\\d+)*\\s*\\] applied to the whole document
    deletes any all-numeric JSON array. "phone_numbers": [1234567] became
    "phone_numbers":  -- an unparseable document. Markers must be stripped only
    from inside string literals.
    """
    raw = '{"official_email": "a@b.pk [2]", "phone_numbers": [1234567, 89], "physical_address": "H-12 [3, 4]"}'
    contact = repair_and_validate_json(raw, ContactInfo)
    assert contact.phone_numbers == ["1234567", "89"]
    assert contact.official_email == "a@b.pk"
    assert contact.physical_address == "H-12"


def test_json_repair_ignores_prose_citation_before_json():
    """A leading '[1]' must not be mistaken for the start of the JSON array."""
    raw = 'Sure, here you go [1]:\n{"official_email": "x@y.pk", "phone_numbers": []}'
    contact = repair_and_validate_json(raw, ContactInfo)
    assert contact.official_email == "x@y.pk"


def test_json_repair_handles_trailing_commas():
    raw = '{"official_email": "x@y.pk", "phone_numbers": ["1",],}'
    assert repair_and_validate_json(raw, ContactInfo).phone_numbers == ["1"]


def test_json_repair_recovers_truncated_answer():
    """A response cut off mid-array must yield the complete objects it did contain."""
    raw = ('[{"name": "BS CS", "degree_level": "undergraduate", '
           '"summary_3_lines": "x", "eligibility_requirements": {}')
    programs = repair_and_validate_json(raw, List[ProgramItem])
    assert len(programs) == 1 and programs[0].name == "BS CS"


def test_json_repair_coerces_scalar_phone_number():
    raw = '{"phone_numbers": "+92-51-90851000"}'
    assert repair_and_validate_json(raw, ContactInfo).phone_numbers == ["+92-51-90851000"]


def test_json_repair_raises_on_unrecoverable_input():
    """Failures must surface as ExtractionError so the caller can re-ask."""
    with pytest.raises(ExtractionError):
        repair_and_validate_json("I could not find any programmes.", List[ProgramItem])


def test_strip_citation_markers_leaves_structure_intact():
    src = '{"a": "x [1]", "b": [1, 2, 3]}'
    assert json.loads(strip_citation_markers(src)) == {"a": "x", "b": [1, 2, 3]}
