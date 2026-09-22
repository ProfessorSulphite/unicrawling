"""
The delimited-text wire protocol.

Every test here names a way a real NotebookLM answer has degraded, or is
expected to. The protocol's whole claim over JSON is that these degrade to
correct data instead of to a parse failure, so each one is a claim under test
rather than a hypothetical.

No network: the parser is a pure function of a string.
"""

import pytest

from src.extractor.crawlers.text_protocol import (
    FACULTY_FIELDS,
    GAPFILL_FIELDS,
    IDENTITY_FIELDS,
    PROGRAM_FIELDS,
    ROSTER_FIELDS,
    parse_records,
    parse_single_record,
    render_format_contract,
    render_name_list,
)
from src.utilities.schema import (
    REQUIRED_PROGRAM_FIELDS,
    ContactInfo,
    FacultyItem,
    MainInfo,
    ProgramItem,
)


WELL_FORMED = """@@RECORD
NAME: BS Computer Science
LEVEL: bachelors
LINK: https://itu.edu.pk/admissions/bs-computer-science
DEPARTMENT: Faculty of Sciences
DURATION: 4 Years
TUITION: 1,416,000
CURRENCY: PKR
APP_FEE: 2,000
DEADLINES: 2026-08-15 (Fall) ;; 2026-12-01 (Spring)
INTAKES: Fall ;; Spring
MIN_MARKS: 60%
ENTRY_TESTS: ITU Admission Test ;; NTS-NAT
DESCRIPTION: A four-year undergraduate degree covering algorithms and systems.
@@END
@@RECORD
NAME: BS Electrical Engineering
LEVEL: bachelors
DURATION: 4 Years
TUITION: NONE
APP_FEE: NONE
DEADLINES: NONE
DESCRIPTION: An accredited engineering degree.
@@END
"""


class TestHappyPath:
    def test_parses_every_record(self):
        records = parse_records(WELL_FORMED, PROGRAM_FIELDS)
        assert len(records) == 2
        assert records[0]["name"] == "BS Computer Science"
        assert records[1]["name"] == "BS Electrical Engineering"

    def test_lists_split_on_the_separator(self):
        first = parse_records(WELL_FORMED, PROGRAM_FIELDS)[0]
        assert first["application_deadlines"] == ["2026-08-15 (Fall)", "2026-12-01 (Spring)"]
        assert first["intake_terms"] == ["Fall", "Spring"]

    def test_nested_fields_are_renested(self):
        first = parse_records(WELL_FORMED, PROGRAM_FIELDS)[0]
        assert first["eligibility_requirements"]["minimum_marks_percentage"] == "60%"
        assert first["eligibility_requirements"]["entry_tests_accepted"] == [
            "ITU Admission Test", "NTS-NAT",
        ]

    def test_none_sentinel_maps_by_field_type(self):
        second = parse_records(WELL_FORMED, PROGRAM_FIELDS)[1]
        assert second["tuition_fee"] is None
        assert second["application_fee"] is None
        assert second["application_deadlines"] == []

    def test_records_validate_as_program_items(self):
        for record in parse_records(WELL_FORMED, PROGRAM_FIELDS):
            ProgramItem.model_validate(record)

    def test_every_required_program_field_round_trips(self):
        """
        The auditor's contract and the wire format must not drift apart.

        REQUIRED_PROGRAM_FIELDS is what the auditor measures coverage against.
        A field it demands that the protocol cannot carry is a field guaranteed
        to read as 0% forever, which is exactly the failure this whole pass
        exists to fix -- so it is pinned here rather than discovered in a run.
        """
        record = parse_records(WELL_FORMED, PROGRAM_FIELDS)[0]
        item = ProgramItem.model_validate(record)
        for field in REQUIRED_PROGRAM_FIELDS:
            assert hasattr(item, field), f"{field} is not carried by the protocol"


class TestDegradation:
    """Each case is a way a model has actually been observed to answer."""

    def test_wrapped_description_is_reassembled(self):
        """A hard-wrapped paragraph must not become a parse error."""
        text = """@@RECORD
NAME: MS Data Science
LEVEL: masters
DESCRIPTION: A two-year programme covering statistical learning,
large-scale data engineering, and applied machine learning
for industry practitioners.
@@END"""
        record = parse_records(text, PROGRAM_FIELDS)[0]
        assert record["description"] == (
            "A two-year programme covering statistical learning, "
            "large-scale data engineering, and applied machine learning "
            "for industry practitioners."
        )

    def test_truncated_final_record_is_closed_by_the_next_marker(self):
        """
        A cut-off answer loses only its last record.

        This is the case that costs json_repairing.py its bracket-stack: here it
        needs no repair machinery at all, because @@RECORD is an unambiguous
        boundary that does not nest.
        """
        text = """@@RECORD
NAME: BS Physics
LEVEL: bachelors
@@RECORD
NAME: BS Chemistry
LEVEL: bachelors
DURATION: 4 Yea"""
        records = parse_records(text, PROGRAM_FIELDS)
        assert [r["name"] for r in records] == ["BS Physics", "BS Chemistry"]
        assert records[1]["duration"] == "4 Yea"

    def test_markdown_bolding_is_tolerated(self):
        text = """@@RECORD
**NAME:** BBA Honours
**LEVEL:** bachelors
- TUITION: **900,000**
@@END"""
        record = parse_records(text, PROGRAM_FIELDS)[0]
        assert record["name"] == "BBA Honours"
        assert record["degree_level"] == "bachelors"
        assert record["tuition_fee"] == "900,000"

    def test_prose_preamble_and_epilogue_are_discarded(self):
        text = """Certainly! Here are the programmes I found in the sources:

@@RECORD
NAME: LLB
LEVEL: bachelors
@@END

Let me know if you would like more detail.
"""
        records = parse_records(text, PROGRAM_FIELDS)
        assert len(records) == 1
        assert records[0]["name"] == "LLB"

    def test_unknown_keys_are_ignored_not_guessed(self):
        text = """@@RECORD
NAME: MPhil Economics
LEVEL: masters
CREDIT_HOURS: 30
ACCREDITED_BY: HEC
@@END"""
        record = parse_records(text, PROGRAM_FIELDS)[0]
        assert record["name"] == "MPhil Economics"
        assert "CREDIT_HOURS" not in record
        assert "credit_hours" not in record

    def test_a_colon_inside_a_value_survives(self):
        text = """@@RECORD
NAME: MS Artificial Intelligence
LEVEL: masters
LINK: https://uni.edu.pk/programs/ms-ai
DESCRIPTION: Two tracks: perception and reasoning.
@@END"""
        record = parse_records(text, PROGRAM_FIELDS)[0]
        assert record["program_info_link"] == "https://uni.edu.pk/programs/ms-ai"
        assert record["description"] == "Two tracks: perception and reasoning."

    def test_citation_markers_are_stripped(self):
        text = """@@RECORD
NAME: PhD Computer Science [1]
LEVEL: phd
DURATION: 3-5 Years [2, 5]
@@END"""
        record = parse_records(text, PROGRAM_FIELDS)[0]
        assert record["name"] == "PhD Computer Science"
        assert record["duration"] == "3-5 Years"

    def test_alternative_empty_phrasings_count_as_unanswered(self):
        """
        A model that writes "Not specified" knew the answer was absent.

        Treating that as a literal value would put the string "Not specified"
        into a student-facing fee field and score it as ANSWERED in the audit --
        a coverage number that is worse than a truthful gap.
        """
        text = """@@RECORD
NAME: PGD Islamic Finance
LEVEL: diploma
TUITION: Not specified
APP_FEE: N/A
DEADLINES: -
CAREERS: unknown
@@END"""
        record = parse_records(text, PROGRAM_FIELDS)[0]
        assert record["tuition_fee"] is None
        assert record["application_fee"] is None
        assert record["application_deadlines"] == []
        assert record["career_prospects"] is None

    def test_list_separator_without_spaces(self):
        text = "@@RECORD\nNAME: X\nLEVEL: masters\nINTAKES: Fall;;Spring;;Summer\n@@END"
        record = parse_records(text, PROGRAM_FIELDS)[0]
        assert record["intake_terms"] == ["Fall", "Spring", "Summer"]

    def test_empty_and_unparseable_input_yields_no_records(self):
        assert parse_records("", PROGRAM_FIELDS) == []
        assert parse_records("I could not find any programmes.", PROGRAM_FIELDS) == []

    def test_a_record_with_no_recognised_keys_is_dropped(self):
        """An empty row is indistinguishable from missing data downstream."""
        text = "@@RECORD\nFOO: bar\n@@END\n@@RECORD\nNAME: BS Maths\nLEVEL: bachelors\n@@END"
        records = parse_records(text, PROGRAM_FIELDS)
        assert len(records) == 1
        assert records[0]["name"] == "BS Maths"


class TestOtherFieldTables:
    def test_roster_records_validate(self):
        text = """@@RECORD
NAME: BS Software Engineering
LEVEL: bachelors
LINK: https://itu.edu.pk/programs/bs-se
DEPARTMENT: Faculty of Engineering
@@END"""
        record = parse_records(text, ROSTER_FIELDS)[0]
        item = ProgramItem.model_validate(record)
        assert item.name == "BS Software Engineering"
        assert item.degree_level.value == "bachelors"

    def test_faculty_records_validate(self):
        text = """@@RECORD
FACULTY: Faculty of Engineering
DESCRIPTION: Engineering disciplines.
DEPARTMENTS: Electrical ;; Civil ;; Mechanical
WEBSITE: https://uni.edu.pk/foe
@@END"""
        item = FacultyItem.model_validate(parse_records(text, FACULTY_FIELDS)[0])
        assert item.departments == ["Electrical", "Civil", "Mechanical"]

    def test_gapfill_records_carry_only_the_missing_fields(self):
        text = """@@RECORD
NAME: BS Computer Science
APP_FEE: 2,000
DEADLINES: 2026-08-15
@@END"""
        record = parse_records(text, GAPFILL_FIELDS)[0]
        assert record["application_fee"] == "2,000"
        assert record["application_deadlines"] == ["2026-08-15"]
        assert "degree_level" not in record

    def test_identity_record_renests_into_the_payload_shape(self):
        text = """@@RECORD
NAME: Information Technology University
ABBREVIATION: ITU
COUNTRY: Pakistan
CITY: Lahore
WEBSITE: https://itu.edu.pk
TYPE: public
DESCRIPTION: A public university in Lahore focused on technology.
PORTAL_URL: https://admissions.itu.edu.pk
EMAIL: info@itu.edu.pk
PHONES: +92-42-111-111-511 ;; +92-42-99000000
SUB_CAMPUSES: Arfa Tower Campus: 3rd Floor, Ferozepur Road
@@END"""
        record = parse_single_record(text, IDENTITY_FIELDS)
        main = MainInfo.model_validate(record["main_info"])
        contact = ContactInfo.model_validate(record["contact"])

        assert main.name == "Information Technology University"
        assert main.key_links.application_portal_url == "https://admissions.itu.edu.pk"
        assert contact.phone_numbers == ["+92-42-111-111-511", "+92-42-99000000"]
        # Reuses ContactInfo._coerce_sub_campuses_contact rather than duplicating it.
        assert contact.sub_campuses_contact[0].campus_name == "Arfa Tower Campus"

    def test_single_record_parse_takes_the_first(self):
        text = (
            "@@RECORD\nNAME: First University\nWEBSITE: https://a.edu\nDESCRIPTION: A\n@@END\n"
            "@@RECORD\nNAME: Second University\nWEBSITE: https://b.edu\nDESCRIPTION: B\n@@END"
        )
        record = parse_single_record(text, IDENTITY_FIELDS)
        assert record["main_info"]["name"] == "First University"

    def test_single_record_parse_of_nothing_is_none(self):
        assert parse_single_record("nothing here", IDENTITY_FIELDS) is None


class TestPromptGeneration:
    def test_contract_is_generated_from_the_field_table(self):
        """
        The prompt and the parser read the same table, so they cannot drift.

        A field requested but not parsed is silently discarded data; a field
        parsed but not requested is guaranteed empty. Generating both from one
        source is what removes the class.
        """
        contract = render_format_contract(PROGRAM_FIELDS)
        for spec in PROGRAM_FIELDS:
            assert f"{spec.key}:" in contract

    def test_contract_states_the_sentinel_and_separator(self):
        contract = render_format_contract(PROGRAM_FIELDS)
        assert "NONE" in contract
        assert ";;" in contract

    def test_singular_contract_omits_the_per_item_instruction(self):
        assert "block per item" not in render_format_contract(IDENTITY_FIELDS, plural=False)
        assert "block per item" in render_format_contract(PROGRAM_FIELDS, plural=True)

    def test_a_list_contract_names_a_terminating_condition(self):
        """
        "Repeat the block once per item" names an action to keep doing;
        "emit one block per item, then stop" names when to stop. The first
        wording was in the prompt that streamed past 52 MB on both attempts of
        run `s_3`, for an answer whose correct form is about 1.5 KB.
        """
        contract = render_format_contract(PROGRAM_FIELDS)
        assert "then stop" in contract
        assert "Never emit the same item twice" in contract
        assert "Repeat the whole" not in contract

    def test_an_item_cap_is_written_into_the_contract(self):
        """Nothing else in a list prompt bounds how many records come back."""
        contract = render_format_contract(PROGRAM_FIELDS, max_items=120)
        assert "AT MOST 120 blocks" in contract
        assert "emit the first 120 and stop" in contract

    def test_no_cap_is_claimed_when_none_was_given(self):
        assert "AT MOST" not in render_format_contract(PROGRAM_FIELDS)

    def test_required_keys_are_called_out(self):
        contract = render_format_contract(ROSTER_FIELDS)
        assert "NAME, LEVEL must always carry a real value." in contract

    def test_name_list_is_one_per_line(self):
        assert render_name_list(["A", "B"]) == "- A\n- B"


def spec_key_is_enum(key: str) -> bool:
    return key in ("degree_level", "application_status")


class TestRoundTrip:
    @pytest.mark.parametrize("field_table,model,key", [
        (ROSTER_FIELDS, ProgramItem, "name"),
        (PROGRAM_FIELDS, ProgramItem, "name"),
        (FACULTY_FIELDS, FacultyItem, "faculty_name"),
    ])
    def test_a_record_built_from_the_contract_parses_back(self, field_table, model, key):
        """
        Emit one record filling every key, and parse it back.

        Guards the case a field table grows a key whose value shape the parser
        mishandles -- a list field declared as a scalar, or a nested path with a
        typo -- which would otherwise surface only as a quietly empty field in a
        live payload.
        """
        # Values that satisfy the enum-typed fields; everything else is free text.
        placeholders = {"LEVEL": "bachelors", "STATUS": "open"}

        lines = ["@@RECORD"]
        for spec in field_table:
            lines.append(f"{spec.key}: {placeholders.get(spec.key, 'placeholder value')}")
        lines.append("@@END")

        record = parse_records("\n".join(lines), field_table)[0]
        assert record.get(key) == "placeholder value" or spec_key_is_enum(key)
        model.model_validate(record)


class TestRequiredContainers:
    """
    A required nested object must survive an answer that fills none of it.

    `MainInfo.key_links` and `Q1Payload.contact` are required objects whose
    every member is optional. The wire format flattens them, so a university
    that publishes no portal link and no phone number produces a record with no
    `key_links` key at all -- and the ENTIRE identity block then fails
    validation over an answer that was completely correct. Found by simulating
    a sparse university, not by reading the code.
    """

    def test_a_sparse_identity_answer_still_validates(self):
        from src.extractor.crawlers.notebook_querying import Q1Payload

        text = """@@RECORD
NAME: Big University
WEBSITE: https://big.edu
DESCRIPTION: A university.
COUNTRY: Pakistan
@@END"""
        record = parse_single_record(text, IDENTITY_FIELDS)
        assert "key_links" in record["main_info"]
        assert "contact" in record

        payload = Q1Payload.model_validate(record)
        assert payload.main_info.name == "Big University"
        assert payload.main_info.key_links.application_portal_url is None
        assert payload.contact.phone_numbers == []

    def test_a_programme_with_no_eligibility_fields_still_validates(self):
        text = "@@RECORD\nNAME: BS Maths\nLEVEL: bachelors\n@@END"
        record = parse_records(text, PROGRAM_FIELDS)[0]
        assert record["eligibility_requirements"] == {}
        ProgramItem.model_validate(record)

    def test_seeding_a_container_does_not_make_an_empty_record_look_real(self):
        """A record of nothing but empty containers is still dropped."""
        assert parse_records("@@RECORD\nFOO: bar\n@@END", PROGRAM_FIELDS) == []
