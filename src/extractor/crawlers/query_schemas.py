"""
Shared query suite, extraction report, and consolidated prompt contracts.

Shared by every direct-extraction engine (DeepSeek, Gemini). Deliberately
leaf-like: it imports the payload schema and nothing else from the package, so
both engines and the orchestrator can depend on it freely.

Two things live here that used to be duplicated per engine:

  * the six-block QUERY_SUITE, used for targeted single-block re-asks; and
  * the two consolidated prompts (programmes, then identity), which are what a
    full extraction actually sends -- two requests instead of six, for the same
    coverage.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from src.utilities.schema import (
    ContactInfo,
    FacultyItem,
    MainInfo,
    ProgramItem,
)


class Q1Payload(BaseModel):
    """Combined identity + contact extraction (Query 1)."""
    main_info: MainInfo
    contact: ContactInfo


# ---------------------------------------------------------------------------
# Consolidated response schemas
#
# Passed to Gemini as a native response_schema, and used as the parse target for
# DeepSeek's json_object output. Declaring them as real models rather than an
# inline JSON blob means the two engines cannot drift apart on shape.
# ---------------------------------------------------------------------------

class ProgramsResponse(BaseModel):
    """Pass 1: every degree programme, split by level."""
    bachelors: List[ProgramItem] = []
    masters: List[ProgramItem] = []
    phd: List[ProgramItem] = []
    diploma: List[ProgramItem] = []


class IdentityResponse(BaseModel):
    """Pass 2: identity, contact details and constituent faculties."""
    main_info: MainInfo
    contact: ContactInfo
    faculties: List[FacultyItem] = []


PROGRAM_LEVELS: Tuple[str, ...] = ("bachelors", "masters", "phd", "diploma")


# ---------------------------------------------------------------------------
# Query suite
# ---------------------------------------------------------------------------

@dataclass
class QuerySpec:
    """One block in the suite, bound to the link tiers that can answer it."""
    key: str
    prompt: str
    model: Any
    tiers: Tuple[int, ...]
    # True when the block is one object rather than a list of them.
    single: bool = False


_JSON_CONTRACT = (
    "Return ONLY a single valid JSON value and nothing else. No prose, no markdown "
    "fences, no explanation. Use null for unknown scalar fields and [] for unknown "
    "lists. Never invent a value that is not supported by the provided sources."
)

Q1_PROMPT = """Extract the university's main information and contact details.
Match this exact structure:
{
  "main_info": {
    "name": "<University Name>", "abbreviation": "<or null>", "country": "<Country e.g. Pakistan, Germany, USA>",
    "city": "<City location of main campus, or null>", "established_year": null,
    "accreditation_body": "<e.g. HEC, ABET, WASC, or null>",
    "admission_cycles_offered": ["<terms as the university names them; [] if unstated>"],
    "primary_instruction_language": "<main teaching language, or null>",
    "website": "<Website URL>", "type": "public" or "private",
    "description": "<Concise overview>",
    "key_links": {
      "academics_url": "<or null>",
      "admissions_url": "<or null>",
      "application_portal_url": "<the page where an applicant actually submits an application, or null>"
    },
    "rankings": []
  },
  "contact": {
    "official_email": "<or null>", "phone_numbers": [], "physical_address": "<or null>",
    "admissions_office_location": "<or null>", "sub_campuses_contact": []
  }
}
Leave "rankings" as an empty array; do not state any numeric rank.
""" + _JSON_CONTRACT

# Every field plan section 5 lists as required per programme. C18 replaced the
# three-line summary with a full-paragraph `description` and added
# `admission_requirements`; deadlines became a list because a programme with
# Fall and Spring intakes has two, and the singular field forced one to be
# dropped. `currency` is a LABEL for whatever the university published -- the
# pipeline never converts (Finding 8).
_PROGRAM_STRUCTURE = """[
  {
    "name": "<Program Name>", "program_info_link": "<URL or null>",
    "department": "<or null>", "degree_level": "%s",
    "duration": "<e.g. 4 Years>", "tuition_fee": "<fee exactly as published, or null>",
    "currency": "<currency the university publishes the fee in, e.g. PKR, EUR, USD>",
    "scholarships_info": "<or null>", "intake_terms": ["<intake terms as the university names them; [] if unstated>"],
    "delivery_mode": "<On-Campus, Online or Hybrid, or null>", "application_fee": "<or null>",
    "career_prospects": "<or null>", "courses_taught": [],
    "description": "<ONE FULL PARAGRAPH: what the programme covers, its focus areas, learning outcomes, career prospects, and any distinctive specializations>",
    "admission_requirements": "<how to apply and what is required beyond marks: documents, interviews, portfolios, prerequisites, entry-test steps, or null>",
    "eligibility_requirements": {
      "minimum_marks_percentage": "<or null>", "entry_tests_accepted": [],
      "aggregate_formula": "<or null>"
    },
    "application_status": "open" | "closed" | "rolling" | "upcoming",
    "application_deadlines": ["<one entry per published deadline; [] if none stated>"]
  }
]"""

# Appended to every programme query. `description` is the plan's headline
# deliverable and the field a model is most likely to skimp on, so it is called
# out separately from the structure block rather than left as one line of JSON.
_PROGRAM_FIELD_NOTE = (
    "\"description\" must be a full paragraph, not a phrase and not a list -- a "
    "student should be able to read it alone and know what the programme is. "
    "Leave a field null rather than guessing; an unstated deadline is [] and an "
    "unstated fee is null.\n"
)


QUERY_SUITE: List[QuerySpec] = [
    QuerySpec(
        key="main_info_contact",
        prompt=Q1_PROMPT,
        model=Q1Payload,
        # Admissions/fees pages (T2) carry portal links; T4 carries contact details.
        tiers=(1, 2, 3, 4),
        single=True,
    ),
    QuerySpec(
        key="bachelors",
        prompt=(
            "List every BACHELORS degree programme (BS, BSc, BA, BBA, BE, B.Ed, BFA, "
            "MBBS, LLB, PharmD, DPT) offered by this university, as a JSON array "
            "matching:\n"
            + (_PROGRAM_STRUCTURE % "bachelors") + "\n"
            + "MBBS, PharmD and DPT are bachelors-level entry programmes here; list "
            "them in this query, not the PhD one.\n"
            + _PROGRAM_FIELD_NOTE + _JSON_CONTRACT
        ),
        model=List[ProgramItem],
        tiers=(1, 2),
        single=False,
    ),
    QuerySpec(
        key="masters",
        prompt=(
            "List every MASTERS degree programme (MS, MSc, MA, MBA, MPhil, M.Ed, LLM, "
            "ME) offered by this university, as a JSON array matching:\n"
            + (_PROGRAM_STRUCTURE % "masters") + "\n"
            + "Include MPhil programmes here. Exclude postgraduate diplomas and "
            "certificates -- those belong to the diploma query.\n"
            + _PROGRAM_FIELD_NOTE + _JSON_CONTRACT
        ),
        model=List[ProgramItem],
        tiers=(1, 2),
        single=False,
    ),
    QuerySpec(
        key="phd",
        prompt=(
            "List every PhD and research doctorate programme offered by this "
            "university, as a JSON array matching:\n"
            + (_PROGRAM_STRUCTURE % "phd") + "\n"
            + "Research doctorates only. Do not include post-doctoral fellowships, "
            "which are appointments rather than programmes.\n"
            + _PROGRAM_FIELD_NOTE + _JSON_CONTRACT
        ),
        model=List[ProgramItem],
        tiers=(1, 2),
        single=False,
    ),
    # Sixth query, added in C17. Postgraduate diplomas and certificates are a
    # large share of Pakistani enrolment and had no bucket at all under the old
    # three-level taxonomy -- they were either dropped or misfiled as masters.
    QuerySpec(
        key="diploma",
        prompt=(
            "List every DIPLOMA and CERTIFICATE programme (postgraduate diploma, PGD, "
            "advanced diploma, professional certificate) offered by this university, "
            "as a JSON array matching:\n"
            + (_PROGRAM_STRUCTURE % "diploma") + "\n"
            + "Award-bearing programmes only. Do not list individual courses or "
            "modules that are part of a degree.\n"
            + _PROGRAM_FIELD_NOTE + _JSON_CONTRACT
        ),
        model=List[ProgramItem],
        tiers=(1, 2),
        single=False,
    ),
    QuerySpec(
        key="faculties",
        prompt=(
            "List all faculties, schools, and their constituent departments, as a JSON "
            "array matching:\n"
            '[{"faculty_name": "<Name>", "description": "<or null>", '
            '"departments": [], "faculty_website": "<URL or null>"}]\n' + _JSON_CONTRACT
        ),
        model=List[FacultyItem],
        tiers=(3, 1),
        single=False,
    ),
]


# ---------------------------------------------------------------------------
# Consolidated prompts
#
# A full extraction sends these two instead of walking QUERY_SUITE, which is what
# takes the request count per university from six to two. Shared by both engines
# so a prompt improvement lands on both at once.
# ---------------------------------------------------------------------------

CONSOLIDATED_PROGRAMS_SYSTEM_PROMPT = (
    "You are an expert, strict, zero-hallucination data extraction agent for an international university education counseling system.\n"
    "Extract the requested academic degree programs STRICTLY from the provided university document sources.\n"
    "Rules:\n"
    "1. Never invent or hallucinate facts, programs, fees, or deadlines. If a field is not stated in the source text, use null (or [] for lists).\n"
    "2. Keep tuition fees in their original stated currency and format.\n"
    "3. description must be a complete, informative paragraph explaining the programme focus, curriculum, and career outcomes.\n"
    "4. Output ONLY a valid JSON object matching the requested schema.\n"
)

CONSOLIDATED_IDENTITY_SYSTEM_PROMPT = (
    "You are an expert, strict, zero-hallucination data extraction agent for an international university education counseling system.\n"
    "Extract the requested identity, contact, and faculty information STRICTLY from the provided university document sources.\n"
    "Rules:\n"
    "1. Never invent or hallucinate facts or rankings. Leave rankings as [].\n"
    "2. Output ONLY a valid JSON object matching the requested schema.\n"
)

TARGETED_BLOCK_SYSTEM_PROMPT = (
    "You are an expert, strict, zero-hallucination data extraction agent for an international university education counseling system.\n"
    "Extract the requested academic information STRICTLY from the provided university document sources.\n"
    "Rules:\n"
    "1. Never invent or hallucinate facts, programs, fees, or deadlines. If a field is not stated in the source text, use null (or [] for lists).\n"
    "2. Keep tuition fees in their original stated currency and format.\n"
    "3. Respond ONLY with a valid JSON object matching the requested schema.\n"
)


def build_consolidated_programs_prompt(uni_name: str, uni_domain: str, corpus_text: str) -> str:
    """Pass 1 user prompt: all four degree levels in one request."""
    return (
        f"Target University: {uni_name} ({uni_domain})\n\n"
        "Task: Extract ALL degree programmes offered by this university from the sources, "
        "categorized by degree level into 'bachelors', 'masters', 'phd', and 'diploma'.\n\n"
        "Schema Contract:\n"
        "{\n"
        '  "bachelors": [ ...list of bachelors degrees (BS, BSc, BA, BBA, BE, B.Ed, BFA, MBBS, LLB, PharmD, DPT)... ],\n'
        '  "masters": [ ...list of masters degrees (MS, MSc, MA, MBA, MPhil, M.Ed, LLM, ME)... ],\n'
        '  "phd": [ ...list of PhD and research doctorates (exclude post-doctoral fellowships)... ],\n'
        '  "diploma": [ ...list of award-bearing diploma and certificate programs (PGDs, certificates)... ]\n'
        "}\n\n"
        "Each programme in the lists must match:\n"
        "{\n"
        '  "name": "<Program Name>", "program_info_link": "<URL or null>",\n'
        '  "department": "<or null>", "degree_level": "bachelors" | "masters" | "phd" | "diploma",\n'
        '  "duration": "<e.g. 4 Years or null>", "tuition_fee": "<fee exactly as published, or null>",\n'
        '  "currency": "<currency published in, e.g. USD, PKR, EUR, or null>",\n'
        '  "scholarships_info": "<or null>", "intake_terms": ["<e.g. Fall; [] if unstated>"],\n'
        '  "delivery_mode": "<On-Campus, Online, or Hybrid, or null>", "application_fee": "<or null>",\n'
        '  "career_prospects": "<or null>",\n'
        '  "description": "<ONE FULL PARAGRAPH: overview, focus areas, career prospects>",\n'
        '  "admission_requirements": "<how to apply and requirements beyond marks, or null>",\n'
        '  "eligibility_requirements": {\n'
        '    "minimum_marks_percentage": "<or null>", "entry_tests_accepted": [], "aggregate_formula": "<or null>"\n'
        '  },\n'
        '  "application_status": "open" | "closed" | "rolling" | "upcoming" | null,\n'
        '  "application_deadlines": ["<one entry per deadline; [] if unstated>"]\n'
        "}\n\n"
        f"Document Sources:\n{corpus_text}\n"
    )


def build_consolidated_identity_prompt(uni_name: str, uni_domain: str, corpus_text: str) -> str:
    """Pass 2 user prompt: identity, contact and faculties in one request."""
    return (
        f"Target University: {uni_name} ({uni_domain})\n\n"
        "Task: Extract university identity details, contact information, and constituent faculties/schools.\n\n"
        "Schema Contract:\n"
        "{\n"
        '  "main_info": {\n'
        '    "name": "<University Name>", "abbreviation": "<or null>", "country": "<Country e.g. USA, Pakistan, Germany>",\n'
        '    "city": "<City or null>", "established_year": null, "accreditation_body": "<or null>",\n'
        '    "admission_cycles_offered": [], "primary_instruction_language": "<or null>",\n'
        '    "website": "<URL>", "type": "public" or "private", "description": "<Concise overview>",\n'
        '    "key_links": {\n'
        '      "academics_url": "<or null>", "admissions_url": "<or null>", "application_portal_url": "<or null>"\n'
        '    },\n'
        '    "rankings": []\n'
        '  },\n'
        '  "contact": {\n'
        '    "official_email": "<or null>", "phone_numbers": [], "physical_address": "<or null>",\n'
        '    "admissions_office_location": "<or null>", "sub_campuses_contact": []\n'
        '  },\n'
        '  "faculties": [\n'
        '    {\n'
        '      "faculty_name": "<Faculty or School Name>", "description": "<or null>",\n'
        '      "departments": ["<Department 1>", "<Department 2>"], "faculty_website": "<or null>"\n'
        '    }\n'
        '  ]\n'
        "}\n\n"
        f"Document Sources:\n{corpus_text}\n"
    )


def build_targeted_block_prompt(spec: QuerySpec, uni_name: str, uni_domain: str, corpus_text: str) -> str:
    """User prompt for a single re-asked block during smart resume."""
    if spec.single:
        return (
            f"Target University: {uni_name} ({uni_domain})\n\n"
            f"Extraction Task:\n{spec.prompt}\n\n"
            f"Document Sources:\n{corpus_text}\n"
        )
    return (
        f"Target University: {uni_name} ({uni_domain})\n\n"
        f"Extraction Task:\n{spec.prompt}\n\n"
        f"IMPORTANT: Output your result as a JSON object with a single key 'items':\n"
        f"{{\"items\": [ ...list of items matching the requested schema... ]}}\n\n"
        f"Document Sources:\n{corpus_text}\n"
    )


# ---------------------------------------------------------------------------
# Extraction report
# ---------------------------------------------------------------------------

@dataclass
class ExtractionReport:
    """Per-query outcome, so a partially-failed extraction is never silently clean."""
    succeeded: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    queries_used: int = 0
    notes: List[str] = field(default_factory=list)
    consecutive_throttle_errors: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def record_success(self, key: str, duration_sec: float = 0.0) -> None:
        self.succeeded.append(key)
        self.queries_used += 1

    def record_failure(self, key: str, err: str) -> None:
        self.failed[key] = err
        self.queries_used += 1

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def merge(self, other: "ExtractionReport") -> None:
        """Fold a per-query sub-report into this one."""
        self.succeeded.extend(other.succeeded)
        self.failed.update(other.failed)
        self.queries_used += other.queries_used
        self.notes.extend(other.notes)
        self.consecutive_throttle_errors = other.consecutive_throttle_errors
