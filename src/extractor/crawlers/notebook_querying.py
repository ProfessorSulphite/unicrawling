"""
The staged extraction plan and its execution against one notebook.

Each QuerySpec is one ask, bound to the source tiers that can answer it, so a
question about fees is not grounded in faculty pages. ExtractionReport records
the per-ask outcome, so a partially-failed extraction is never reported as clean.

**Why the plan is staged rather than a fixed suite (C32).**

The previous six-query suite asked, in one breath, "list every BACHELORS
programme" *and* "give me fifteen fields for each". That is one question doing
two jobs -- DISCOVER how many programmes exist, and DESCRIBE each one -- so the
response size was (unknown count) x (15 fields): a number nobody chose and
nothing bounded. On ITU it blew the 50 MB RPC ceiling four times across two
blocks, spending 29% of that university's quota and 24% of its runtime to
arrive at no data at all.

The plan now separates the two jobs:

    1. identity   -- one record
    2. faculties  -- a handful of records
    3. roster     -- name, level, link, department only: one SHORT line per
                     programme, over the full Tier-1+2 source set
    4. detail     -- the full field set for N programmes named by the roster
    5. gapfill    -- fees and deadlines still blank, over Tier-2 sources only

Stage 3 cannot overflow: its response is one line per programme. Stage 4 asks a
*closed* question -- "these five programmes, by name" -- so its size is
`chunk_size x fields`, which is a number we choose and can lower.

It also answers the obvious objection to splitting by discipline: there is no
partition for a programme to fall through. Stage 3 enumerates everything once,
and stage 4 only ever re-describes names stage 3 already produced. No taxonomy
to maintain, and no catch-all "any programmes you missed?" ask -- a negative
question models answer poorly.
"""

import asyncio
import logging
import math
import re

from dataclasses import dataclass, field
from notebooklm import NotebookLMClient
from notebooklm.exceptions import RPCResponseTooLargeError
try:
    from notebooklm.exceptions import RateLimitError
except ImportError:
    RateLimitError = None

from difflib import SequenceMatcher
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Sequence, Tuple, get_args, get_origin

from src.config import config
from src.logger.notebook_logger import log_query_executed
from src.utilities.schema import (
    ContactInfo,
    FacultyItem,
    MainInfo,
    ProgramGapFill,
    ProgramItem,
)

# Sideways within `extractor`, not upward: the normalizer already owns the hard
# cases in degree naming (MBBS/DPT/PharmD are bachelors-level despite reading as
# doctorates; MPhil is masters), and re-deriving them here is how two
# classifiers disagree about the same programme.
from src.extractor.normalizers.degree_names import classify_degree_level

from src.extractor.crawlers.json_repairing import (
    ExtractionError,
    repair_and_validate_json,
    validate_against,
)
from src.extractor.crawlers.text_protocol import (
    FACULTY_FIELDS,
    FieldSpec,
    GAPFILL_FIELDS,
    IDENTITY_FIELDS,
    PROGRAM_FIELDS,
    ROSTER_FIELDS,
    parse_records,
    parse_single_record,
    render_format_contract,
    render_name_list,
)

# Same registry entry as every other module in this package: logging.getLogger
# returns one object per name, so this is the logger extract_data.py created.
logger = logging.getLogger("ExtractData")


def is_rate_limit_exception(e: Exception) -> Tuple[bool, Optional[int], str]:
    """
    Detects if an exception is a Gemini/NotebookLM 5-hour usage limit or rate limit error.
    Returns: (is_rate_limit, retry_after_sec, message)
    """
    if RateLimitError is not None and isinstance(e, RateLimitError):
        retry_after = getattr(e, "retry_after", None)
        return True, retry_after, str(e)
    msg = str(e).lower()
    markers = ("usage limit", "limit reached", "rate limit", "429", "too many requests", "resets at")
    if any(m in msg for m in markers):
        retry_after = None
        match = re.search(r"retry[-_\s]after[^\d]*(\d+)", msg)
        if match:
            retry_after = int(match.group(1))
        return True, retry_after, str(e)
    return False, None, ""


class QueryTimeoutError(TimeoutError):
    """A single chat.ask exceeded config.chat_timeout_sec and was abandoned."""


class Q1Payload(BaseModel):
    main_info: MainInfo
    contact: ContactInfo


# ---------------------------------------------------------------------------
# Query specification
# ---------------------------------------------------------------------------

@dataclass
class QuerySpec:
    """
    One ask, bound to the source tiers that can answer it.

    `fields` is the delimited-text field table (text_protocol.FieldSpec). When
    it is present and config.response_format is 'text', the answer is parsed
    with the text protocol; otherwise the legacy JSON path runs. Both end at the
    same Pydantic models, so validators, aliases and coercions are shared.
    """
    key: str
    prompt: str
    model: Any
    tiers: Tuple[int, ...]
    fields: Optional[Tuple[FieldSpec, ...]] = None
    single: bool = False       # exactly one record expected, not a list
    stage: str = "base"        # base | detail | gapfill; for reporting only


# ---------------------------------------------------------------------------
# Legacy JSON suite (config.response_format == 'json')
#
# Kept reachable rather than deleted so a regression in the text path can be
# A/B'd against the same university instead of reasoned about. Text is the
# contract; this is the control group.
# ---------------------------------------------------------------------------

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

_PROGRAM_FIELD_NOTE = (
    "\"description\" must be a full paragraph, not a phrase and not a list -- a "
    "student should be able to read it alone and know what the programme is. "
    "Leave a field null rather than guessing; an unstated deadline is [] and an "
    "unstated fee is null.\n"
)

_LEVEL_GUIDANCE = {
    "bachelors": (
        "List every BACHELORS degree programme (BS, BSc, BA, BBA, BE, B.Ed, BFA, "
        "MBBS, LLB, PharmD, DPT). MBBS, PharmD and DPT are bachelors-level entry "
        "programmes here."
    ),
    "masters": (
        "List every MASTERS degree programme (MS, MSc, MA, MBA, MPhil, M.Ed, LLM, "
        "ME). Include MPhil. Exclude postgraduate diplomas and certificates."
    ),
    "phd": (
        "List every PhD and research doctorate programme. Research doctorates "
        "only -- a post-doctoral fellowship is an appointment, not a programme."
    ),
    "diploma": (
        "List every DIPLOMA and CERTIFICATE programme (postgraduate diploma, PGD, "
        "advanced diploma, professional certificate). Award-bearing programmes "
        "only; not individual courses within a degree."
    ),
}

QUERY_SUITE: List[QuerySpec] = [
    QuerySpec(
        key="main_info_contact",
        prompt=Q1_PROMPT,
        model=Q1Payload,
        tiers=(1, 2, 3, 4),
    ),
    *[
        QuerySpec(
            key=level,
            prompt=(
                f"{_LEVEL_GUIDANCE[level]} Return a JSON array matching:\n"
                + (_PROGRAM_STRUCTURE % level) + "\n"
                + _PROGRAM_FIELD_NOTE + _JSON_CONTRACT
            ),
            model=List[ProgramItem],
            tiers=(1, 2),
        )
        for level in ("bachelors", "masters", "phd", "diploma")
    ],
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


# ---------------------------------------------------------------------------
# Staged text plan (config.response_format == 'text')
# ---------------------------------------------------------------------------

_NO_INVENTION = (
    "Use only what the provided sources state. Never infer, estimate or "
    "generalise a value from another university or from common practice.\n"
)


def build_identity_spec() -> QuerySpec:
    """Stage 1: one record describing the institution and how to contact it."""
    return QuerySpec(
        key="identity",
        prompt=(
            "Describe this university's core identity and contact details.\n"
            "Do not state any numeric ranking; leave rankings out entirely.\n\n"
            + _NO_INVENTION + "\n"
            + render_format_contract(IDENTITY_FIELDS, plural=False)
        ),
        model=Q1Payload,
        # Contact details live on Tier 4 pages and portal links on Tier 2, so
        # this is the one ask that legitimately reads everything.
        tiers=(1, 2, 3, 4),
        fields=IDENTITY_FIELDS,
        single=True,
    )


def build_faculties_spec() -> QuerySpec:
    """Stage 2: the faculty/school structure. Naturally small, never chunked."""
    return QuerySpec(
        key="faculties",
        prompt=(
            "List every faculty and school at this university, with the "
            "departments belonging to each.\n\n"
            + _NO_INVENTION + "\n"
            + render_format_contract(FACULTY_FIELDS)
        ),
        model=List[FacultyItem],
        tiers=(3, 1),
        fields=FACULTY_FIELDS,
    )


_ROSTER_LEVELS = {
    "bachelors": "BACHELORS degree programmes (BS, BSc, BA, BBA, BE, B.Ed, BFA, "
                 "MBBS, LLB, PharmD, DPT) this university offers",
    "masters":   "MASTERS degree programmes (MS, MSc, MA, MBA, MPhil, M.Ed, LLM, "
                 "ME) this university offers",
    "phd":       "PhD and research doctorate programmes this university offers",
    "diploma":   "DIPLOMA and CERTIFICATE programmes (postgraduate diploma, PGD, "
                 "advanced diploma, professional certificate) this university offers",
}

_ROSTER_ALL_LEVELS = (
    "degree programmes this university offers, across all four levels: "
    "bachelors, masters, PhD, and diplomas or certificates"
)


def _roster_prompt(scope: str, level: Optional[str] = None) -> str:
    """
    The roster prompt, deliberately stripped of every phrase that invites a loop.

    What was removed after run `s_3`, where this ask streamed past 52 MB on both
    attempts -- ~50 million characters for an answer whose correct form is about
    1.5 KB:

      * "EVERY ... at every level" and "named anywhere in the sources, including
        ones mentioned only in a list or a table". An open-ended exhaustiveness
        instruction over a large heterogeneous corpus, with a repeating output
        template underneath it, is the classic shape for runaway generation.
      * "Completeness matters more than detail here", which explicitly told the
        model to prefer more output to less.

    What replaced them is a bounded, closed request: name the thing being
    listed, cap the count, and say when to stop. Completeness is still the job,
    but it is now expressed as "each programme once" rather than as "keep
    going" -- and the cap in the format contract gives it a terminating
    condition it previously did not have.
    """
    return (
        f"List the {scope}.\n"
        f"This is an inventory, not a description. Give ONLY the four fields "
        f"below for each programme -- no fees, no deadlines, no descriptions.\n"
        f"List each programme exactly once. Do not repeat a programme you have "
        f"already listed, and do not list the same programme under a different "
        f"wording.\n\n"
        + _NO_INVENTION + "\n"
        + render_format_contract(
            ROSTER_FIELDS,
            max_items=int(getattr(config, "roster_max_items", 120)),
        )
        + (f"\nLEVEL is {level} for every record in this answer." if level else "")
    )


def build_roster_specs() -> List[QuerySpec]:
    """
    Stage 3: enumerate the programmes, four short fields each.

    This is the ask the rest of the plan is sized by, and it is the completeness
    guarantee: everything downstream describes names it produced, so "did we
    miss a discipline?" reduces to "did this ask enumerate everything?" -- one
    question about one answer, rather than a property of a partition nobody can
    verify.

    **Scoped to Tier 1 (config.roster_tiers) since C34.** Programme pages are
    where programmes are enumerated. Tier 2 is fee schedules, test patterns and
    sample papers: 15 further sources on ITU that cannot name a programme the
    Tier-1 set does not, and every one of them is more repetitive corpus for the
    model to loop over.

    **Optionally split by degree level** (config.roster_split_by_level), which
    is the fallback if one ask still runs away. It costs 3 extra asks and each
    answer is a quarter the size. It does NOT reintroduce the partition problem
    the design exists to avoid: these are discovery asks over the same full
    source set, so no programme can fall between them -- only the question is
    narrowed, and the levels are exhaustive over DegreeLevel by construction.
    """
    tiers = tuple(getattr(config, "roster_tiers", (1,))) or (1,)

    if not getattr(config, "roster_split_by_level", False):
        return [QuerySpec(
            key="roster",
            prompt=_roster_prompt(_ROSTER_ALL_LEVELS),
            model=List[ProgramItem],
            tiers=tiers,
            fields=ROSTER_FIELDS,
        )]

    return [
        QuerySpec(
            key=f"roster:{level}",
            prompt=_roster_prompt(scope, level=level),
            model=List[ProgramItem],
            tiers=tiers,
            fields=ROSTER_FIELDS,
        )
        for level, scope in _ROSTER_LEVELS.items()
    ]


def build_roster_spec() -> QuerySpec:
    """The single roster ask. Retained for callers and tests that want just one."""
    return build_roster_specs()[0]


def build_detail_specs(
    roster: Sequence[ProgramItem],
    chunk_size: Optional[int] = None,
) -> List[QuerySpec]:
    """
    Stage 4: full detail for the named programmes, in bounded chunks.

    Chunks are cut within a degree level rather than across it, so every ask
    carries one consistent LEVEL value and the model is never asked to switch
    taxonomy mid-answer. The level is stated in the prompt as a fact, not a
    question -- the roster already established it.

    Response size is `len(chunk) x len(PROGRAM_FIELDS)`. That is the whole
    point: it is a number chosen here, not discovered when the transport fails.
    """
    chunk_size = chunk_size or config.program_detail_chunk_size
    chunk_size = max(1, int(chunk_size))

    by_level: Dict[str, List[ProgramItem]] = {}
    for item in roster:
        level = getattr(item.degree_level, "value", str(item.degree_level))
        by_level.setdefault(level, []).append(item)

    specs: List[QuerySpec] = []
    for level in ("bachelors", "masters", "phd", "diploma"):
        items = by_level.get(level, [])
        for start in range(0, len(items), chunk_size):
            chunk = items[start:start + chunk_size]
            names = [p.name for p in chunk]
            index = len(specs) + 1
            specs.append(QuerySpec(
                key=f"detail:{level}:{index}",
                prompt=(
                    f"For EACH of the following {len(names)} {level} programmes at this "
                    f"university, give every field listed below.\n\n"
                    f"{render_name_list(names)}\n\n"
                    f"Emit one record per programme, in the same order, using the "
                    f"programme name exactly as written above so the records can be "
                    f"matched back. LEVEL is {level} for all of them.\n"
                    f"If a programme has no page of its own, use whatever the "
                    f"faculty, admissions or fee pages say about it.\n\n"
                    + _NO_INVENTION + "\n"
                    + render_format_contract(PROGRAM_FIELDS)
                ),
                model=List[ProgramItem],
                tiers=(1, 2),
                fields=PROGRAM_FIELDS,
                stage="detail",
            ))
    return specs


def build_gapfill_spec(names: Sequence[str]) -> QuerySpec:
    """
    Stage 5: one narrow ask for the fees and deadlines still missing.

    Scoped to Tier 2 -- fee structures and admission schedules -- because these
    two values almost always live in ONE shared table rather than on each
    programme's page. That is why a single targeted ask beats re-asking per
    programme, and why the previous suite's 0% coverage was a source-selection
    failure rather than an extraction one: no ingested source contained them.

    The prompt says explicitly that a university-wide figure counts. Models
    decline to answer per-programme questions when the source states the fact
    once, institution-wide -- which is the normal way universities publish it.
    """
    return QuerySpec(
        key="gapfill",
        prompt=(
            "The fee and deadline information is missing for the programmes "
            "below. Using the fee schedules, admission schedules and academic "
            "calendars in the sources, give what is published for each.\n\n"
            f"{render_name_list(names)}\n\n"
            "IMPORTANT: if the university publishes ONE application fee, or ONE "
            "admission deadline, that applies to all applicants, then report that "
            "same value for every programme listed. A university-wide figure is "
            "the correct answer, not a missing one.\n"
            f"Only write NONE where the sources genuinely publish no figure.\n\n"
            + _NO_INVENTION + "\n"
            + render_format_contract(GAPFILL_FIELDS)
        ),
        # ProgramGapFill, not ProgramItem: this ask names no degree level and
        # must not be able to. The roster already settled that, and requiring a
        # level here would fail validation for a record that is otherwise good.
        model=List[ProgramGapFill],
        tiers=(2,),
        fields=GAPFILL_FIELDS,
        stage="gapfill",
    )


def roster_level_sizes(roster: Sequence[ProgramItem]) -> List[int]:
    """How many programmes sit at each degree level present in the roster."""
    counts: Dict[str, int] = {}
    for item in roster:
        level = getattr(item.degree_level, "value", str(item.degree_level))
        counts[level] = counts.get(level, 0) + 1
    return [counts[k] for k in sorted(counts)]


def _asks_for(level_sizes: Sequence[int], chunk: int) -> int:
    """
    Ask count for a chunk size, counting the way build_detail_specs chunks.

    Chunks never straddle a degree level, so the total is the SUM of per-level
    ceilings and not `ceil(total / chunk)`. Getting this wrong under-counts
    badly on a spread-out roster -- three programmes at three levels need three
    asks at any chunk size, while `ceil(3/5)` says one -- and the under-count
    then reads as "the quota only granted 1 of 1", so two thirds of the
    university silently goes undescribed with nothing reported.
    """
    return sum(math.ceil(n / chunk) for n in level_sizes if n > 0)


def plan_query_budget(
    roster: Any,
    budget: Optional[int] = None,
) -> Tuple[int, int]:
    """
    Choose a chunk size that fits the roster into `budget` detail asks.

    `roster` is a list of ProgramItem, a list of per-level counts, or a plain
    int meaning "this many programmes, all at one level".

    Returns (chunk_size, ask_count).

    The direction of the trade matters and is deliberate: when the budget is
    tight this raises the CHUNK SIZE, it never drops programmes. A programme
    omitted here is invisible downstream -- it looks exactly like a university
    that does not offer it -- whereas a larger chunk merely risks a big response,
    which is a failure that announces itself and can be retried.

    The chunk size is still clamped at config.max_program_detail_chunk_size:
    beyond that the response is large enough to be the original problem again,
    and the honest outcome is to describe fewer programmes and SAY SO rather
    than to issue an ask that predictably fails.
    """
    if isinstance(roster, int):
        level_sizes = [roster] if roster > 0 else []
    elif roster and isinstance(roster[0], int):
        level_sizes = [n for n in roster if n > 0]
    else:
        level_sizes = roster_level_sizes(roster or [])

    total = sum(level_sizes)
    if total <= 0:
        return config.program_detail_chunk_size, 0

    chunk = max(1, int(config.program_detail_chunk_size))
    asks = _asks_for(level_sizes, chunk)

    if budget is not None and budget > 0 and asks > budget:
        ceiling = max(chunk, int(config.max_program_detail_chunk_size))
        # Walk the chunk size up until it fits, rather than solving for it:
        # the per-level ceiling sum is not invertible in closed form, and the
        # search is at most `ceiling` cheap iterations once per university.
        while chunk < ceiling and asks > budget:
            chunk += 1
            asks = _asks_for(level_sizes, chunk)

    return chunk, asks


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass
class ExtractionReport:
    """Per-ask outcome, so a partially-failed extraction is never silently clean."""
    succeeded: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    queries_used: int = 0
    # Things that are neither success nor failure but must not be invisible:
    # a detail record whose name is on no roster row, a programme dropped
    # because the budget could not stretch, a cross-level duplicate arbitrated
    # away. Each one is a silent data change if it is not recorded here.
    anomalies: List[str] = field(default_factory=list)
    # C32. Each spec used to get a fresh ExtractionReport, so `query_index` in
    # the audit restarted at 1 for every block: `bachelors` and `masters` both
    # logged indices 3, 4, 5 and the audit could not order the asks of a run.
    index_offset: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def note(self, message: str) -> None:
        logger.warning(message)
        self.anomalies.append(message)

    def merge(self, other: "ExtractionReport") -> None:
        """Fold a per-ask sub-report into this one."""
        self.succeeded.extend(other.succeeded)
        self.failed.update(other.failed)
        self.queries_used += other.queries_used
        self.anomalies.extend(other.anomalies)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

async def _reset_conversation(
    client: NotebookLMClient,
    notebook_id: str,
    conversation_id: Optional[str] = None,
) -> None:
    """
    Clear this notebook's current conversation so the next ask starts fresh.

    Best-effort and never raises. A conversation that could not be deleted is a
    quality problem for the NEXT ask -- it arrives as a follow-up turn instead
    of a question -- but it is not a reason to fail an ask that has already
    produced a good answer, and it spends no query quota either way.

    `conversation_id` is supplied on the success path, where the SDK hands it
    back. On the failure paths there is no result to read it from, so it is
    looked up: an ask that failed at the transport still recorded a turn, and
    that turn is the one most worth clearing.
    """
    try:
        if not conversation_id:
            getter = getattr(client.chat, "get_conversation_id", None)
            if getter is None:
                return
            conversation_id = await getter(notebook_id)
        if conversation_id:
            await client.chat.delete_conversation(notebook_id, conversation_id)
            logger.debug(f"Cleared conversation {conversation_id} on notebook {notebook_id}.")
    except Exception as e:
        # Logged at debug: on a healthy run this is silent, and on an unhealthy
        # one the failed ASK is the headline, not the cleanup behind it.
        logger.debug(f"Could not clear conversation on notebook {notebook_id}: {e}")


async def _ask(
    client: NotebookLMClient,
    notebook_id: str,
    prompt: str,
    source_ids: Optional[Sequence[str]] = None,
    conversation_id: Optional[str] = None,
) -> str:
    """
    Issue one chat.ask, under a deadline, and return the answer text.

    The deadline is the whole point of this wrapper. `config.chat_timeout_sec`
    was declared and documented but read by nothing, and this await had no bound
    of any kind: when a COMSATS ask stopped responding on 2026-09-05 the call sat
    there for 7 hours 11 minutes until the operator killed the process, and the
    twelve universities queued behind it never ran. A hung ask is now an ordinary
    failed attempt -- retried by the loop above, and fatal to nothing but itself.

    **Each ask runs in its own conversation (C33).** The SDK documents that
    "repeated ask() calls without conversation_id all extend the same
    most-recent conversation", so every stage of the plan was arriving as a
    FOLLOW-UP TURN rather than as a question. By the roster ask on ITU's `s_2`
    run that conversation already held the identity turn, the faculties turn,
    and a 52 MB aborted turn -- and the roster came back with 7 bachelors
    programmes and no masters or PhD at all, from a corpus holding 5 MS and 2
    PhD programme pages. A model four turns deep does what a model in a
    conversation does: it does not restate what has already been said.

    Deleting the conversation afterwards is the SDK's documented way to force
    the next ask to start fresh -- the server then has nothing to extend. It
    costs one API round-trip and NO query quota.

    This also removes the cross-contamination class structurally rather than by
    relying on serial execution: two asks that share no conversation cannot
    return each other's turns.
    """
    reset_after = conversation_id is None and getattr(
        config, "isolate_query_conversations", True
    )
    try:
        res = await asyncio.wait_for(
            client.chat.ask(
                notebook_id=notebook_id,
                question=prompt,
                source_ids=list(source_ids) if source_ids else None,
                conversation_id=conversation_id,
            ),
            timeout=config.chat_timeout_sec,
        )
    except asyncio.TimeoutError as e:
        # A timed-out ask may still have been recorded server-side, and the
        # turn it left behind is exactly the kind that poisons the next one --
        # so the conversation is cleared on this path too, best-effort.
        if reset_after:
            await _reset_conversation(client, notebook_id)
        # Re-raised as our own type so the retry loop's log line says what
        # happened. A bare TimeoutError here is indistinguishable from an HTTP
        # read timeout raised several layers down.
        raise QueryTimeoutError(
            f"NotebookLM did not answer within {config.chat_timeout_sec}s"
        ) from e
    except BaseException:
        # Includes RPCResponseTooLargeError. The client aborted the read, but
        # the server still recorded a turn -- and on the `s_2` run the two asks
        # that followed an aborted one returned the two SHORTEST answers of the
        # nine. Clearing it is the whole point of the retry being worth making.
        if reset_after:
            await _reset_conversation(client, notebook_id)
        raise

    if reset_after:
        await _reset_conversation(client, notebook_id, getattr(res, "conversation_id", None))

    answer = getattr(res, "answer", None)
    return str(answer) if answer else str(res)


def is_oversized_response_error(error: BaseException) -> bool:
    """
    True when a chat.ask failed because the ANSWER did not fit.

    **Revised reading of this failure (C32).** The previous docstring asserted
    the error was deterministic -- a property of how much corpus the question
    was pointed at -- and the retry policy was built on that. The evidence does
    not support it:

      * `phd` and `diploma`, asked over the SAME 37 sources as `bachelors` and
        `masters`, both succeeded on the first ask.
      * The four failures reported 52,449,458-52,487,729 bytes against a
        52,428,800 ceiling. That tight clustering says nothing about the true
        answer size; it is simply where the client aborts the read.
      * All three "narrowed" sub-answers from the split path came back
        byte-identical (13,689 bytes x3), so splitting produced no new evidence.

    The likeliest explanation is degenerate repetition -- the model looping on a
    JSON array and streaming indefinitely -- which is transient. Hence the
    policy change: prefer ONE identical re-ask (config.oversize_single_reask)
    over re-entering the source-splitting path, which is now off by default.
    """
    if RPCResponseTooLargeError is not None and isinstance(error, RPCResponseTooLargeError):
        return True
    # Matched on the message too: the SDK wraps this error in a few places, and
    # a missed match costs a whole block.
    text = str(error).lower()
    return "response exceeded" in text or "responsetoolarge" in type(error).__name__.lower()


def _error_status(error: BaseException) -> str:
    """Classify a failed ask for the audit trail."""
    if is_oversized_response_error(error):
        return "oversized"
    if isinstance(error, QueryTimeoutError):
        return "timeout"
    if isinstance(error, ExtractionError):
        return "parse_failed"
    if is_rate_limit_exception(error)[0]:
        return "rate_limited"
    return "error"


# ---------------------------------------------------------------------------
# Parsing and merging
# ---------------------------------------------------------------------------

def _returns_a_list(model: Any) -> bool:
    """Whether a QuerySpec's model is List[...] rather than a single object."""
    return get_origin(model) is list


def _item_type(model: Any) -> Any:
    """The element type of a List[...] annotation."""
    args = get_args(model)
    return args[0] if args else model


def _use_text_protocol(spec: QuerySpec) -> bool:
    return bool(spec.fields) and getattr(config, "response_format", "text") == "text"


def _validate_leniently(
    item_model: Any,
    records: Sequence[Dict[str, Any]],
    spec_key: str,
    report: ExtractionReport,
) -> List[Any]:
    """
    Validate records one at a time, keeping the ones that pass.

    Previously a list answer was validated whole, so a SINGLE unreadable row --
    one programme whose LEVEL the model wrote as "Bachelor's" -- failed the
    entire block and every other programme in it was lost with it. Twenty good
    records are worth more than a clean error, and the rejects are recorded as
    anomalies rather than dropped quietly.
    """
    valid: List[Any] = []
    rejected = 0
    for record in records:
        try:
            valid.append(validate_against(item_model, record))
        except Exception as e:
            rejected += 1
            if rejected <= 3:   # bounded: a bad prompt can reject everything
                report.note(
                    f"[{spec_key}] dropped unreadable record "
                    f"{record.get('name') or record.get('faculty_name') or '<unnamed>'!r}: {e}"
                )
    if rejected > 3:
        report.note(f"[{spec_key}] dropped {rejected} unreadable records in total.")
    if records and not valid:
        raise ExtractionError(
            f"all {len(records)} parsed records failed validation for '{spec_key}'"
        )
    return valid


def parse_answer(
    raw: str,
    spec: QuerySpec,
    report: ExtractionReport,
    notebook_id: str = "N/A",
    uni_slug: Optional[str] = None,
) -> Any:
    """
    Turn one raw answer into validated schema data, by whichever protocol applies.

    Both protocols end at the same Pydantic models. That is the point: the text
    format changes the wire encoding and nothing else, so every validator,
    alias and coercion already written and tested still runs.
    """
    if not _use_text_protocol(spec):
        return repair_and_validate_json(raw, spec.model, notebook_id, spec.key, uni_slug)

    if spec.single:
        record = parse_single_record(raw, spec.fields)
        if not record:
            raise ExtractionError(
                f"no parseable record in the answer for '{spec.key}' "
                f"(prefix: {raw[:200]!r})"
            )
        try:
            return validate_against(spec.model, record)
        except Exception as e:
            # ExtractionError, not the raw ValidationError. The distinction is
            # load-bearing in three places: the ask has ALREADY been counted by
            # this point, so falling through to the transport-error branch
            # charges it twice; that branch logs the wrong status; and it clears
            # the text the repair prompt is supposed to quote. The JSON path has
            # always converted here -- the text path has to as well.
            raise ExtractionError(
                f"schema validation failed for '{spec.key}': {e}"
            ) from e

    records = parse_records(raw, spec.fields)
    if not records:
        raise ExtractionError(
            f"no parseable records in the answer for '{spec.key}' "
            f"(prefix: {raw[:200]!r})"
        )
    return _validate_leniently(_item_type(spec.model), records, spec.key, report)


def _merge_item_key(item: Any) -> str:
    """Identity for merge-dedupe: the programme or faculty name, casefolded."""
    for attr in ("name", "faculty_name"):
        value = getattr(item, attr, None)
        if value:
            return str(value).strip().casefold()
    return repr(item)


# Decorations a model adds to a name that carry no identity: the roster may say
# "BS Computer Science" where the detail ask says "BS Computer Science (BSCS)".
_NAME_NOISE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*|[.,;:]+")


def normalize_program_name(name: str) -> str:
    """Casefolded, de-punctuated name used to match a detail record to its roster row."""
    return " ".join(_NAME_NOISE.sub(" ", str(name or "")).split()).casefold()


def match_roster_name(name: str, candidates: Sequence[str]) -> Optional[str]:
    """
    Find the roster name a detail record belongs to, or None.

    Exact casefolded match first; a similarity floor
    (config.roster_match_min_ratio) only as a fallback. This is the single most
    likely source of churn in the staged plan -- a roster row reading
    "BS Computer Science" against a detail record reading "Bachelor of Science
    in Computer Science" is a mismatch that loses a whole record -- so the
    fallback exists, but it is deliberately conservative: a WRONG match writes
    one programme's fees onto another, which is worse than an orphan, because an
    orphan is visible in the report and a wrong match is not.
    """
    key = normalize_program_name(name)
    if not key:
        return None

    lookup = {normalize_program_name(c): c for c in candidates}
    if key in lookup:
        return lookup[key]

    # A string-similarity floor alone is NOT safe here, and the counter-example
    # is the commonest shape in the data: "MS Computer Science" and
    # "BS Computer Science" differ by one character out of nineteen and score
    # 0.95 similar, so any usable floor matches them. They are different
    # programmes at different levels, and merging them writes a masters
    # programme's fees onto a bachelors one.
    #
    # So the degree level is checked first and is a veto, not a tiebreak: where
    # both names are readable and disagree, there is no match at any similarity.
    level = classify_degree_level(name)

    floor = getattr(config, "roster_match_min_ratio", 0.82)
    best, best_ratio = None, 0.0
    for candidate_key, original in lookup.items():
        if level is not None:
            candidate_level = classify_degree_level(original)
            if candidate_level is not None and candidate_level != level:
                continue
        ratio = SequenceMatcher(None, key, candidate_key).ratio()
        if ratio > best_ratio:
            best, best_ratio = original, ratio
    return best if best_ratio >= floor else None


def _merge_list_answers(parts: Sequence[Any]) -> List[Any]:
    """
    Concatenate answers from several asks, dropping repeats.

    Two chunks can both surface the same programme when its pages overlap, and a
    duplicate row is worse than a missing one -- it reaches the payload as two
    programmes with one name.
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


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

async def _attempt_query(
    client: NotebookLMClient,
    notebook_id: str,
    spec: QuerySpec,
    source_ids: Optional[Sequence[str]],
    report: ExtractionReport,
    uni_slug: Optional[str] = None,
) -> Tuple[Any, Optional[Exception]]:
    """
    One bounded repair loop against one set of sources.

    Returns (value, None) on success or (None, last_error) on exhaustion.

    **Every ask is logged, whatever happens to it (C32).** The error path used
    to increment `report.queries_used` without logging anything, so the audit
    document recorded 10 asks for a run whose ledger charged 14 -- and the four
    missing ones were the most expensive asks in the run.
    """
    last_error: Optional[Exception] = None
    # Held separately from `raw` so the repair prompt can quote the answer that
    # failed to parse. The previous code assigned `raw = ""` at the top of each
    # attempt and then interpolated `raw[:1500]` into the repair prompt three
    # lines later, so EVERY repair re-ask said "Previous answer (truncated):"
    # followed by nothing -- the feature was disabled by its own guard. The
    # guard's intent was right (a stale answer must not be quoted after a
    # transport failure that produced none) but it fired unconditionally. Yale
    # burned attempts 1 and 3 on the byte-identical error because of this.
    failed_text = ""

    for attempt in range(config.max_query_retries + 1):
        prompt = spec.prompt
        if attempt > 0 and last_error is not None:
            reminder = (
                "Re-emit the SAME data in the EXACT record format specified below."
                if _use_text_protocol(spec) else
                "Re-emit the SAME data as strictly valid JSON only."
            )
            previous = (
                f"Previous answer (truncated):\n{failed_text[:1500]}\n\n"
                if failed_text else
                "Your previous answer did not arrive.\n\n"
            )
            prompt = (
                f"Your previous answer could not be parsed.\n"
                f"Error: {last_error}\n"
                f"{previous}{reminder}\n\n{spec.prompt}"
            )

        raw = ""
        t0 = asyncio.get_event_loop().time()
        try:
            raw = await _ask(client, notebook_id, prompt, source_ids)
            dur = asyncio.get_event_loop().time() - t0
            report.queries_used += 1
            try:
                value = parse_answer(raw, spec, report, notebook_id, uni_slug)
            except ExtractionError:
                log_query_executed(
                    notebook_id=notebook_id,
                    query_index=report.index_offset + report.queries_used,
                    query_key=spec.key,
                    prompt_len=len(prompt),
                    response_bytes=len(raw.encode("utf-8")),
                    duration_sec=dur,
                    uni_slug=uni_slug,
                    status="parse_failed",
                )
                raise
            log_query_executed(
                notebook_id=notebook_id,
                query_index=report.index_offset + report.queries_used,
                query_key=spec.key,
                prompt_len=len(prompt),
                response_bytes=len(raw.encode("utf-8")),
                duration_sec=dur,
                uni_slug=uni_slug,
                status="ok",
            )
            return value, None

        except ExtractionError as e:
            # The ask itself succeeded and was already counted above; only the
            # parse failed. Counting again here double-charged the ledger.
            last_error = e
            failed_text = raw
            logger.warning(f"[{spec.key}] attempt {attempt + 1} failed: {e}")

        except Exception as e:
            is_limit, wait_sec, limit_msg = is_rate_limit_exception(e)
            if is_limit:
                backoff_sec = wait_sec or getattr(config, "rate_limit_default_sleep_sec", 300)
                logger.warning(
                    f"[{spec.key}] 5-Hour Gemini usage limit encountered: {limit_msg}. "
                    f"Wait time: {backoff_sec}s"
                )
                log_query_executed(
                    notebook_id=notebook_id,
                    query_index=report.index_offset + report.queries_used,
                    query_key=spec.key,
                    prompt_len=len(prompt),
                    response_bytes=0,
                    duration_sec=asyncio.get_event_loop().time() - t0,
                    uni_slug=uni_slug,
                    status="rate_limited",
                    error=limit_msg,
                )
                policy = getattr(config, "on_rate_limit", "auto_sleep")
                if policy == "auto_sleep":
                    print(f"\n⏳ [GEMINI USAGE LIMIT] 5-Hour usage quota reached ({limit_msg}).")
                    print(f"   Auto-sleeping for {backoff_sec}s before retrying query '{spec.key}'...")
                    await asyncio.sleep(backoff_sec)
                    continue  # Retry same attempt without burning retry counter
                report.failed[spec.key] = f"Rate limit reached: {limit_msg}"
                raise e

            last_error = e
            failed_text = ""       # a transport failure produced no answer to quote
            report.queries_used += 1
            log_query_executed(
                notebook_id=notebook_id,
                query_index=report.index_offset + report.queries_used,
                query_key=spec.key,
                prompt_len=len(prompt),
                response_bytes=0,
                duration_sec=asyncio.get_event_loop().time() - t0,
                uni_slug=uni_slug,
                status=_error_status(e),
                error=str(e),
            )
            logger.warning(f"[{spec.key}] attempt {attempt + 1} errored: {e}")
            if is_oversized_response_error(e):
                # Handled by run_query's own policy, which knows whether a
                # single re-ask has already been spent.
                break

    return None, last_error


async def _query_over_source_splits(
    client: NotebookLMClient,
    notebook_id: str,
    spec: QuerySpec,
    source_ids: Sequence[str],
    report: ExtractionReport,
    depth: int = 0,
    uni_slug: Optional[str] = None,
) -> Tuple[Any, Optional[Exception]]:
    """
    Re-ask the same question of halves of the source set, and merge the answers.

    **Legacy path, off by default since C32** (config.enable_oversize_split).
    It was built on the premise that response size tracks corpus size, so
    narrowing the sources shrinks the answer. The ITU evidence contradicts that
    premise -- see is_oversized_response_error -- and the three narrowed
    sub-answers it produced were byte-identical, so it spent three asks to learn
    nothing. The staged plan bounds response size by construction instead.

    Kept for one release rather than deleted, so it can be turned back on if a
    corpus ever does behave the way this assumed.
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
        value, error = await _attempt_query(client, notebook_id, spec, half, report, uni_slug)
        if value is None and error is not None and is_oversized_response_error(error):
            value, error = await _query_over_source_splits(
                client, notebook_id, spec, half, report, depth + 1, uni_slug
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

    # A single-object query cannot be merged: the halves describe the same
    # university, so the first complete answer is the answer.
    return parts[0], None


async def run_query(
    client: NotebookLMClient,
    notebook_id: str,
    spec: QuerySpec,
    source_ids: Optional[Sequence[str]],
    report: ExtractionReport,
    uni_slug: Optional[str] = None,
) -> Any:
    """
    Execute one ask, repairing malformed answers and recovering oversized ones.

    Three failure modes, three remedies:

      - a malformed answer is re-asked with its own broken output and the exact
        error, which recovers the majority of them;
      - an oversized answer is re-asked ONCE, identically. The evidence says the
        failure is transient rather than a property of the corpus, so the cheap
        remedy is tried before the expensive one;
      - only if that also fails, and config.enable_oversize_split is on, is the
        legacy source-splitting path entered.

    Every attempt, including a re-ask, is counted against the daily budget.
    """
    value, error = await _attempt_query(client, notebook_id, spec, source_ids, report, uni_slug)

    if error is not None and is_oversized_response_error(error):
        if getattr(config, "oversize_single_reask", True):
            logger.info(
                f"[{spec.key}] oversized response; re-asking once identically "
                f"(the failure pattern looks transient, not deterministic)."
            )
            value, error = await _attempt_query(
                client, notebook_id, spec, source_ids, report, uni_slug
            )

        if (
            value is None
            and error is not None
            and is_oversized_response_error(error)
            and source_ids
            and getattr(config, "enable_oversize_split", False)
        ):
            value, split_error = await _query_over_source_splits(
                client, notebook_id, spec, source_ids, report, uni_slug=uni_slug
            )
            error = split_error or error if value is None else None

    if error is None and value is not None:
        report.succeeded.append(spec.key)
        return value

    report.failed[spec.key] = str(error) if error else "query returned no value"
    return None
