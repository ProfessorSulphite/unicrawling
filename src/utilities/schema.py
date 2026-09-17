"""
Executable Pydantic Schemas for Education Counselor System (schema.py)
Serves as single source of truth for prompts, validation, and JSON export.
"""
from typing import Any, List, Optional
from enum import Enum
from pydantic import AliasChoices, BaseModel, Field, field_validator


class UniversityType(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    OTHER = "other"


class DegreeLevel(str, Enum):
    """The four levels a counselling student can actually apply to (C17).

    Replaces undergraduate / graduate / postgraduate_phd, which conflated a
    taught masters with an MPhil and had no home at all for the PGDs and
    diplomas that make up a large share of Pakistani enrolment. Post-doctoral
    is deliberately absent: it is a research appointment, not a programme.
    Legacy values still coerce -- see ProgramItem._coerce_degree_level.
    """
    BACHELORS = "bachelors"
    MASTERS = "masters"
    PHD = "phd"
    DIPLOMA = "diploma"


# Retired DegreeLevel values, kept only so payloads written before C17 still
# load. Module-level rather than a ProgramItem attribute: Pydantic claims
# leading-underscore class attributes as private attrs.
# No longer written by anything (C19 removed the fallback). Kept because every
# payload extracted before C19 contains this exact string, and the normalizer has
# to recognise it in order to refuse to carry it into `description`.
SUMMARY_FALLBACK = "Academic degree program offered by the university."

_RETIRED_DEGREE_LEVELS = {
    "undergraduate": "bachelors",
    "graduate": "masters",
    "postgraduate_phd": "phd",
    "postgraduate_and_phd": "phd",
}


class ApplicationStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ROLLING = "rolling"
    UPCOMING = "upcoming"


class RankingItem(BaseModel):
    source: str
    scope: str
    subject: Optional[str] = None
    year: int
    rank: int
    source_url: Optional[str] = None


class KeyLinks(BaseModel):
    academics_url: Optional[str] = None
    admissions_url: Optional[str] = None
    application_portal_url: Optional[str] = Field(None, description="PRIMARY FOCUS")


class MainInfo(BaseModel):
    name: str
    abbreviation: Optional[str] = None
    # C19: no default. This drove currency labelling and the eligibility guesses
    # for the whole record, so a university whose country the extractor missed
    # was silently processed as Pakistani.
    country: Optional[str] = Field(None, description="Country location of the university (e.g. Germany, USA, UK, Switzerland, Pakistan)")
    city: Optional[str] = Field(None, description="City location of campus e.g. Islamabad, Munich, Boston")
    established_year: Optional[int] = Field(None, description="Year university was founded")
    accreditation_body: Optional[str] = Field(None, description="Accrediting agency e.g. HEC, ABET, TEQSA, WASC")
    # C19: was ["Fall", "Spring"] -- northern-hemisphere naming asserted for
    # universities that run Semester 1 / Semester 2 from February.
    admission_cycles_offered: List[str] = Field(default_factory=list, description="Admission terms e.g. Fall, Spring, Summer, Winter")
    # C19: was "English", asserted worldwide.
    primary_instruction_language: Optional[str] = Field(None, description="Main teaching language")
    website: str
    # C19: was PUBLIC. Public-versus-private is roughly a coin flip and getting
    # it wrong is a fact a student would act on.
    type: Optional[UniversityType] = Field(None)
    description: str
    domain_verified: bool = False
    verification_note: Optional[str] = None
    key_links: KeyLinks
    rankings: List[RankingItem] = []
    exa_enriched: bool = False

    @field_validator("primary_instruction_language", mode="before")
    @classmethod
    def blank_language_is_none(cls, v: Any) -> Optional[str]:
        return v.strip() if isinstance(v, str) and v.strip() else None

    @field_validator("type", mode="before")
    @classmethod
    def unreadable_type_is_none(cls, v: Any) -> Optional[UniversityType]:
        if isinstance(v, UniversityType):
            return v
        if isinstance(v, str) and v.strip().lower() in ("public", "private", "other"):
            return UniversityType(v.strip().lower())
        return None


def coerce_deadline_list(v: Any) -> List[str]:
    """
    Normalise whatever a model emitted for a deadline field into a list of strings.

    Module-level so ProgramItem and ProgramGapFill share ONE implementation.
    Two copies of a coercion is how the gap-fill path and the detail path come
    to disagree about what "August 5, 2026" means.
    """
    if v is None:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if isinstance(v, list):
        return [str(d).strip() for d in v if d is not None and str(d).strip()]
    return [str(v).strip()]


class EligibilityRequirements(BaseModel):
    minimum_marks_percentage: Optional[str] = None
    entry_tests_accepted: List[str] = []
    aggregate_formula: Optional[str] = None


class ProgramItem(BaseModel):
    name: str
    program_info_link: Optional[str] = None
    department: Optional[str] = None
    degree_level: DegreeLevel
    duration: Optional[str] = None
    tuition_fee: Optional[str] = None
    # C19: was "PKR". A label for a fee nobody read is not a label, it is a claim.
    currency: Optional[str] = Field(None, description="Currency of tuition fee (e.g. EUR, USD, GBP, CHF, PKR)")
    scholarships_info: Optional[str] = None
    # C19: were ["Fall"] and "On-Campus". Both were assumptions, and the growing
    # share of online and hybrid provision makes the second one actively risky.
    intake_terms: List[str] = Field(default_factory=list, description="Intake terms for this program e.g. Fall, Spring, Winter")
    delivery_mode: Optional[str] = Field(None, description="On-Campus, Online, or Hybrid")
    application_fee: Optional[str] = Field(None, description="Application fee amount and currency")
    career_prospects: Optional[str] = Field(None, description="Target career outcomes or roles")
    courses_taught: List[str] = []
    # C18, plan section 5: the deliverable per-programme field. A full paragraph
    # covering focus areas, learning outcomes, career prospects and any
    # distinctive specialisations -- enough for a student to judge fit.
    # Optional, not required: a programme page that genuinely says nothing about
    # itself should normalize to None, not to invented prose.
    description: Optional[str] = Field(
        None,
        description=(
            "Full-paragraph overview of the programme: focus areas, learning "
            "outcomes, career prospects, distinctive features or specializations"
        ),
    )
    # DEPRECATED (C18): superseded by `description`, which asks for the same
    # thing without the arbitrary three-line cap. Kept because every payload
    # written before C18 carries it and inspect_cli re-validates those on read;
    # the prompts no longer request it. The normalizer carries a real value over
    # into `description` when that field is empty.
    summary_3_lines: Optional[str] = None
    admission_requirements: Optional[str] = Field(
        None,
        description=(
            "Admission process and requirements beyond raw eligibility marks: "
            "documents, interviews, portfolios, prerequisites, entry-test steps"
        ),
    )
    # Defaulted rather than required: every field inside EligibilityRequirements is
    # itself optional, so a missing block carries no less information than an empty
    # one -- but marking it required forces a repair re-ask that spends real budget
    # from the 500/day NotebookLM ceiling to learn nothing. Output shape is
    # unchanged; the object still always serialises.
    eligibility_requirements: EligibilityRequirements = Field(default_factory=EligibilityRequirements)
    # C19: was ROLLING, which told a student applications were open year-round.
    application_status: Optional[ApplicationStatus] = None
    # Plural since C18 (plan section 5, "Application deadline(s)"). A programme
    # with Fall and Spring intakes has two published deadlines, and the singular
    # field forced one of them to be dropped or crammed into prose.
    application_deadlines: List[str] = Field(
        default_factory=list,
        # The retired singular key is accepted as an alias rather than migrated
        # elsewhere: without it a pre-C18 payload's only deadline is silently
        # dropped at validation, which is a fact a student would act on.
        validation_alias=AliasChoices("application_deadlines", "application_deadline"),
        description="Every published application deadline, one entry per intake",
    )

    # Every payload written before C17 carries the old three-level names, and
    # inspect_cli re-validates those files on every read. Coercing them here
    # keeps historical output loadable without a migration pass.
    #
    # Kept deliberately self-contained rather than delegating to
    # extractor.normalizers.degree_names: utilities is the leaf layer and must
    # not import upward, even lazily. The richer name-based classifier lives
    # there and runs during normalization; this only translates the field's own
    # retired vocabulary. An unrecognised value is passed through untouched so
    # Pydantic still rejects it and the repair loop still gets its turn.
    @field_validator("degree_level", mode="before")
    @classmethod
    def _coerce_degree_level(cls, v: Any) -> Any:
        if isinstance(v, str):
            return _RETIRED_DEGREE_LEVELS.get(v.strip().lower(), v)
        return v

    @field_validator("summary_3_lines", mode="before")
    @classmethod
    def _coerce_summary(cls, v: Any) -> Optional[str]:
        # C19: an absent summary was replaced with SUMMARY_FALLBACK. The constant
        # survives only so the normalizer can RECOGNISE that string in payloads
        # written before this commit and decline to promote it into description.
        return v.strip() if isinstance(v, str) and v.strip() else None

    # Accepts the singular string the extractor still tends to emit, and the
    # retired application_deadline value that every pre-C18 payload carries.
    # Same shape as ContactInfo's phone-number coercion.
    @field_validator("application_deadlines", mode="before")
    @classmethod
    def _coerce_deadlines(cls, v: Any) -> List[str]:
        return coerce_deadline_list(v)


class ProgramGapFill(BaseModel):
    """
    A partial programme record: the fields a gap-fill ask is allowed to supply.

    Deliberately NOT a ProgramItem. The gap-fill stage asks one narrow question
    about programmes the roster has already named and levelled, so it must not
    be able to state a `degree_level` at all -- a second opinion on a level that
    is already settled can only introduce a contradiction, and the roster is the
    better evidence either way (it saw the programme in context).

    Making it a ProgramItem would also force the model to restate a required
    `degree_level` in an answer that has no business carrying one, and a missing
    one would fail validation for the whole batch of records.

    Field names match ProgramItem exactly, so `merge_detail_into_roster` reads
    them with the same getattr and needs no special case.
    """
    name: str
    application_fee: Optional[str] = None
    application_deadlines: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("application_deadlines", "application_deadline"),
    )
    tuition_fee: Optional[str] = None
    currency: Optional[str] = None

    @field_validator("application_deadlines", mode="before")
    @classmethod
    def _coerce_deadlines(cls, v: Any) -> List[str]:
        return coerce_deadline_list(v)


# Plan section 5, "Per-program required fields". The canonical list lives here,
# in the leaf layer, so the model that declares the fields and the auditor that
# reports them missing cannot drift apart -- the auditor imports this rather than
# keeping its own copy, and test_program_fields asserts every entry is a real
# field on ProgramItem.
#
# "Required" here means *required to be answered*, not required by Pydantic.
# Every one of these is Optional on the model on purpose: forcing them at
# validation would reject a whole university's payload over one unanswered field,
# and spend real NotebookLM budget on a repair re-ask that learns nothing. The
# schema lets a gap through; the auditor is what refuses to ship it.
REQUIRED_PROGRAM_FIELDS = (
    "name",
    "degree_level",
    "duration",
    "tuition_fee",
    "currency",
    "eligibility_requirements",
    "admission_requirements",
    "application_fee",
    "application_deadlines",
    "description",
)

# Without these a row is not a programme at all: name is what a student searches
# and degree_level is which bucket it lives in. Everything else can be a
# documented gap; these two cannot.
CRITICAL_PROGRAM_FIELDS = ("name", "degree_level")


class ProgramCategoryBlock(BaseModel):
    """One bucket per DegreeLevel; the key names match the enum values exactly.

    Pre-C17 the keys were undergraduate / graduate / postgraduate_and_phd, and
    the last of those did not even match its own enum value
    ("postgraduate_phd"). Keys and enum values are now the same four strings, so
    a bucket name can be derived from a level rather than translated.
    """
    bachelors: List[ProgramItem] = []
    masters: List[ProgramItem] = []
    phd: List[ProgramItem] = []
    diploma: List[ProgramItem] = []


class FacultyItem(BaseModel):
    faculty_name: str
    description: Optional[str] = None
    departments: List[str] = []
    faculty_website: Optional[str] = None


class SubCampusContact(BaseModel):
    campus_name: str
    city: Optional[str] = None
    contact_details: Optional[str] = None


class ContactInfo(BaseModel):
    official_email: Optional[str] = None
    phone_numbers: List[str] = []
    physical_address: Optional[str] = None
    admissions_office_location: Optional[str] = None
    sub_campuses_contact: List[SubCampusContact] = []

    @field_validator("phone_numbers", mode="before")
    @classmethod
    def _coerce_phone_numbers(cls, v: Any) -> Any:
        """
        Accept numbers, or a single string, where a list of strings is expected.

        Models routinely emit "phone_numbers": [1234567] or "phone_numbers":
        "+92-51-90851000". Rejecting those wastes a repair round-trip against the
        daily query budget over a difference that carries no information.
        """
        if v is None:
            return []
        if isinstance(v, (str, int, float)):
            return [str(v)]
        if isinstance(v, list):
            return [str(item) for item in v if item is not None]
        return v

    @field_validator("sub_campuses_contact", mode="before")
    @classmethod
    def _coerce_sub_campuses_contact(cls, v: Any) -> Any:
        """
        Coerce string representations of sub-campus contacts into SubCampusContact dicts.
        Handles cases where NotebookLM returns strings like "Kenya Campus: 3rd Parklands (Tel: +254...)"
        """
        if v is None:
            return []
        if isinstance(v, list):
            res = []
            for item in v:
                if isinstance(item, str):
                    if ":" in item:
                        name, _, details = item.partition(":")
                        res.append({"campus_name": name.strip(), "contact_details": details.strip()})
                    else:
                        res.append({"campus_name": item.strip()})
                else:
                    res.append(item)
            return res
        return v



class UniversityPayload(BaseModel):
    main_info: MainInfo
    programs: ProgramCategoryBlock
    faculties: List[FacultyItem] = []
    contact: ContactInfo
    programs_possibly_truncated: bool = False
    # Which query blocks failed every retry, e.g. ["bachelors"]. A failed block
    # yields an EMPTY bucket, not an error, so without this field a consumer
    # cannot tell "this university offers no bachelors programmes" from "the
    # bachelors query failed". ITU shipped with an empty bachelors bucket on
    # 2026-09-03 and nothing in the payload recorded why.
    failed_query_blocks: List[str] = []

    @property
    def extraction_complete(self) -> bool:
        """True when every query block in the suite answered."""
        return not self.failed_query_blocks
