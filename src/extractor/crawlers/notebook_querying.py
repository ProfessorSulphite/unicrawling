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
from notebooklm.exceptions import RPCResponseTooLargeError
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Sequence, Tuple, get_origin

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


def is_oversized_response_error(error: BaseException) -> bool:
    """
    True when a chat.ask failed because the ANSWER did not fit, not because it
    was wrong.

    This is the distinction the retry loop was missing. An oversized response is
    a property of how much corpus the question was pointed at, so re-asking the
    same question of the same sources fails again, deterministically -- observed
    on every ITU run of 2026-09-03, three attempts landing within 60 KB of the
    50 MB ceiling each time, ~6 minutes and 3 queries of daily budget spent to
    arrive at the same wall.
    """
    if RPCResponseTooLargeError is not None and isinstance(error, RPCResponseTooLargeError):
        return True
    # Matched on the message too: the SDK wraps this error in a few places, and
    # a missed match costs a whole degree bucket.
    text = str(error).lower()
    return "response exceeded" in text or "responsetoolarge" in type(error).__name__.lower()


def _returns_a_list(model: Any) -> bool:
    """Whether a QuerySpec's model is List[...] rather than a single object."""
    return get_origin(model) is list


def _merge_item_key(item: Any) -> str:
    """Identity for merge-dedupe: the programme or faculty name, casefolded."""
    for attr in ("name", "faculty_name"):
        value = getattr(item, attr, None)
        if value:
            return str(value).strip().casefold()
    return repr(item)


def _merge_list_answers(parts: Sequence[Any]) -> List[Any]:
    """
    Concatenate the answers from split sub-asks, dropping repeats.

    Two source halves can both surface the same programme when its pages are
    split across them, and a duplicate row is worse than a missing one -- it
    reaches the payload as two programmes with one name.
    """
    merged: List[Any] = []
    seen: set = set()
    for part in parts:
        for item in part or []:
            key = _merge_item_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


async def _attempt_query(
    client: NotebookLMClient,
    notebook_id: str,
    spec: QuerySpec,
    source_ids: Optional[Sequence[str]],
    report: ExtractionReport,
) -> Tuple[Any, Optional[Exception]]:
    """
    One bounded repair loop against one set of sources.

    Returns (value, None) on success or (None, last_error) on exhaustion. The
    repair retry exists for answers that came back malformed; it is abandoned
    immediately for an oversized response, which no amount of re-asking fixes.
    """
    last_error: Optional[Exception] = None

    for attempt in range(config.max_query_retries + 1):
        # Reset per attempt: a stale answer from an earlier attempt must not be
        # quoted back to the model as "your previous answer" after a transport
        # failure that produced none.
        raw = ""
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
            return value, None
        except ExtractionError as e:
            # The ask itself succeeded and was already counted above; only the
            # parse failed. Counting again here double-charged the ledger.
            last_error = e
            logger.warning(f"[{spec.key}] attempt {attempt + 1} failed: {e}")
        except Exception as e:
            last_error = e
            report.queries_used += 1
            logger.warning(f"[{spec.key}] attempt {attempt + 1} errored: {e}")
            if is_oversized_response_error(e):
                # Deterministic at this scope. Stop burning attempts and let the
                # caller narrow the question instead.
                break

    return None, last_error


async def _query_over_source_splits(
    client: NotebookLMClient,
    notebook_id: str,
    spec: QuerySpec,
    source_ids: Sequence[str],
    report: ExtractionReport,
    depth: int = 0,
) -> Tuple[Any, Optional[Exception]]:
    """
    Re-ask the same question of halves of the source set, and merge the answers.

    Narrowing the SOURCES rather than the prompt is what makes the response
    smaller: the answer size tracks how much corpus the question is grounded in.
    Splitting is what the identical retry should always have been -- ITU's 17
    Tier-1 pages asked as two groups of 8 and 9 return two answers that each fit.

    A half that is still too large is split again, to config.max_query_split_depth.
    Below that, or with a single source left, the query genuinely cannot be
    answered and the failure is reported rather than papered over.
    """
    ids = list(source_ids)
    if depth >= config.max_query_split_depth or len(ids) < 2:
        return None, None

    mid = len(ids) // 2
    halves = [ids[:mid], ids[mid:]]
    logger.info(
        f"[{spec.key}] response too large over {len(ids)} sources; "
        f"re-asking as {len(halves)} narrower groups (depth {depth + 1})."
    )

    parts: List[Any] = []
    errors: List[Exception] = []
    for half in halves:
        value, error = await _attempt_query(client, notebook_id, spec, half, report)
        if value is None and error is not None and is_oversized_response_error(error):
            value, error = await _query_over_source_splits(
                client, notebook_id, spec, half, report, depth + 1
            )
        if value is not None:
            parts.append(value)
        elif error is not None:
            errors.append(error)

    if not parts:
        return None, (errors[0] if errors else None)

    if _returns_a_list(spec.model):
        merged = _merge_list_answers(parts)
        logger.info(
            f"[{spec.key}] merged {len(merged)} distinct items from "
            f"{len(parts)} narrowed sub-answers."
        )
        return merged, None

    # A single-object query (main_info/contact) cannot be merged: the halves
    # describe the same university, so the first complete answer is the answer.
    return parts[0], None


async def run_query(
    client: NotebookLMClient,
    notebook_id: str,
    spec: QuerySpec,
    source_ids: Optional[Sequence[str]],
    report: ExtractionReport,
) -> Any:
    """
    Execute one query, repairing malformed answers and narrowing oversized ones.

    Two distinct failure modes, two distinct remedies:

      - a malformed answer is re-asked with its own broken output and the exact
        error, which recovers the majority of them;
      - an oversized answer is re-asked over halves of the source set, because
        the response size is a property of the corpus the question is pointed
        at and an identical re-ask fails identically.

    Every attempt, including a narrowed one, is counted against the daily budget.
    """
    value, error = await _attempt_query(client, notebook_id, spec, source_ids, report)

    if error is not None and is_oversized_response_error(error) and source_ids:
        value, split_error = await _query_over_source_splits(
            client, notebook_id, spec, source_ids, report
        )
        if value is None:
            error = split_error or error
        else:
            error = None

    if error is None and value is not None:
        report.succeeded.append(spec.key)
        return value

    report.failed[spec.key] = str(error) if error else "query returned no value"
    return None
