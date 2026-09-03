"""
The five-query extraction suite and its execution against one notebook.

Each QuerySpec is bound to the source tiers that can answer it, so a query about
fees is not grounded in faculty pages. ExtractionReport records the per-query
outcome, so a partially-failed extraction is never reported as clean.
"""

import asyncio
import logging

from dataclasses import dataclass, field
from notebooklm import NotebookLMClient
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.config import config
from src.logger.notebook_logger import log_query_executed
from src.utilities.schema import ContactInfo, FacultyItem, MainInfo, ProgramItem

from src.extractor.crawlers.json_repairing import ExtractionError, repair_and_validate_json

# Same registry entry as every other module in this package: logging.getLogger
# returns one object per name, so this is the logger extract_data.py created.
logger = logging.getLogger("ExtractData")


class Q1Payload(BaseModel):
    main_info: MainInfo
    contact: ContactInfo

# ---------------------------------------------------------------------------
# Query suite
# ---------------------------------------------------------------------------

@dataclass
class QuerySpec:
    """One query in the suite, bound to the source tiers that can answer it."""
    key: str
    prompt: str
    model: Any
    tiers: Tuple[int, ...]


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
    "admission_cycles_offered": ["Fall", "Spring"],
    "primary_instruction_language": "English",
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
    "scholarships_info": "<or null>", "intake_terms": ["Fall"],
    "delivery_mode": "On-Campus", "application_fee": "<or null>",
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
    ),
]


@dataclass
class ExtractionReport:
    """Per-query outcome, so a partially-failed extraction is never silently clean."""
    succeeded: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    queries_used: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def merge(self, other: "ExtractionReport") -> None:
        """Fold a per-query sub-report into this one."""
        self.succeeded.extend(other.succeeded)
        self.failed.update(other.failed)
        self.queries_used += other.queries_used


async def _ask(
    client: NotebookLMClient,
    notebook_id: str,
    prompt: str,
    source_ids: Optional[Sequence[str]] = None,
    conversation_id: Optional[str] = None,
) -> str:
    """Issue one chat.ask and return the answer text."""
    res = await client.chat.ask(
        notebook_id=notebook_id,
        question=prompt,
        source_ids=list(source_ids) if source_ids else None,
        conversation_id=conversation_id,
    )
    answer = getattr(res, "answer", None)
    return str(answer) if answer else str(res)


async def run_query(
    client: NotebookLMClient,
    notebook_id: str,
    spec: QuerySpec,
    source_ids: Optional[Sequence[str]],
    report: ExtractionReport,
) -> Any:
    """
    Execute one query with a bounded repair loop.

    On a parse or validation failure the model is re-asked with its own broken
    output and the exact error, which recovers the majority of malformed answers.
    Each attempt is counted against the daily query budget by the caller.
    """
    last_error: Optional[Exception] = None
    raw = ""

    for attempt in range(config.max_query_retries + 1):
        prompt = spec.prompt
        if attempt > 0:
            prompt = (
                f"Your previous answer could not be parsed.\n"
                f"Error: {last_error}\n"
                f"Previous answer (truncated):\n{raw[:1500]}\n\n"
                f"Re-emit the SAME data as strictly valid JSON only.\n\n{spec.prompt}"
            )
        try:
            t0 = asyncio.get_event_loop().time()
            raw = await _ask(client, notebook_id, prompt, source_ids)
            dur = asyncio.get_event_loop().time() - t0
            report.queries_used += 1
            log_query_executed(
                notebook_id=notebook_id,
                query_index=report.queries_used,
                query_key=spec.key,
                prompt_len=len(prompt),
                response_bytes=len(raw.encode("utf-8")),
                duration_sec=dur,
            )
            value = repair_and_validate_json(raw, spec.model)
            report.succeeded.append(spec.key)
            return value
        except ExtractionError as e:
            last_error = e
            logger.warning(f"[{spec.key}] attempt {attempt + 1} failed: {e}")
        except Exception as e:
            last_error = e
            report.queries_used += 1
            logger.warning(f"[{spec.key}] attempt {attempt + 1} errored: {e}")

    report.failed[spec.key] = str(last_error)
    return None
