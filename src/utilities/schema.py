"""
Executable Pydantic Schemas for Education Counselor System (schema.py)
Serves as single source of truth for prompts, validation, and JSON export.
"""
from typing import Any, List, Optional
from enum import Enum
from pydantic import BaseModel, Field, field_validator


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
    country: str = Field("Pakistan", description="Country location of the university (e.g. Germany, USA, UK, Switzerland, Pakistan)")
    city: Optional[str] = Field(None, description="City location of campus e.g. Islamabad, Munich, Boston")
    established_year: Optional[int] = Field(None, description="Year university was founded")
    accreditation_body: Optional[str] = Field(None, description="Accrediting agency e.g. HEC, ABET, TEQSA, WASC")
    admission_cycles_offered: List[str] = Field(default_factory=lambda: ["Fall", "Spring"], description="Admission terms e.g. Fall, Spring, Summer, Winter")
    primary_instruction_language: Optional[str] = Field("English", description="Main teaching language")
    website: str
    type: Optional[UniversityType] = Field(UniversityType.PUBLIC)
    description: str
    domain_verified: bool = False
    verification_note: Optional[str] = None
    key_links: KeyLinks
    rankings: List[RankingItem] = []
    exa_enriched: bool = False

    @field_validator("primary_instruction_language", mode="before")
    @classmethod
    def default_language(cls, v: Any) -> str:
        return v if isinstance(v, str) and v.strip() else "English"

    @field_validator("type", mode="before")
    @classmethod
    def default_type(cls, v: Any) -> UniversityType:
        if isinstance(v, str) and v.lower() in ("public", "private", "other"):
            return UniversityType(v.lower())
        return UniversityType.PUBLIC


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
    currency: str = Field("PKR", description="Currency of tuition fee (e.g. EUR, USD, GBP, CHF, PKR)")
    scholarships_info: Optional[str] = None
    intake_terms: List[str] = Field(default_factory=lambda: ["Fall"], description="Intake terms for this program e.g. Fall, Spring, Winter")
    delivery_mode: Optional[str] = Field("On-Campus", description="On-Campus, Online, or Hybrid")
    application_fee: Optional[str] = Field(None, description="Application fee amount and currency")
    career_prospects: Optional[str] = Field(None, description="Target career outcomes or roles")
    courses_taught: List[str] = []
    summary_3_lines: str
    # Defaulted rather than required: every field inside EligibilityRequirements is
    # itself optional, so a missing block carries no less information than an empty
    # one -- but marking it required forces a repair re-ask that spends real budget
    # from the 500/day NotebookLM ceiling to learn nothing. Output shape is
    # unchanged; the object still always serialises.
    eligibility_requirements: EligibilityRequirements = Field(default_factory=EligibilityRequirements)
    application_status: ApplicationStatus = ApplicationStatus.ROLLING
    application_deadline: Optional[str] = None

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
    def _coerce_summary(cls, v: Any) -> str:
        if isinstance(v, str) and v.strip():
            return v.strip()
        return "Academic degree program offered by the university."


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
