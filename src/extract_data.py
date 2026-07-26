"""
Schema Extraction & Robustness Engine (extract_data.py)

Executes the 5-query suite against a prepared NotebookLM notebook, repairs and
validates the model's JSON, fills gaps from a deterministic rankings registry and
a domain-scoped Exa search, and returns a validated UniversityPayload.

Notebook deletion is the caller's decision and happens only after validation
succeeds -- see delete_notebook_after_success().
"""
import re
import sys
import json
import asyncio
import logging
from pathlib import Path
from functools import lru_cache
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Sequence, Tuple

from pydantic import BaseModel, TypeAdapter, ValidationError
from notebooklm import NotebookLMClient

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.config import config
    from src.notebook_logger import log_query_executed, log_json_repaired, log_notebook_deleted
    from src.schema import (
        UniversityPayload, MainInfo, ContactInfo, ProgramItem, FacultyItem,
        ProgramCategoryBlock, KeyLinks, RankingItem, UniversityType,
    )
except ImportError:
    from config import config
    from notebook_logger import log_query_executed, log_json_repaired, log_notebook_deleted
    from schema import (
        UniversityPayload, MainInfo, ContactInfo, ProgramItem, FacultyItem,
        ProgramCategoryBlock, KeyLinks, RankingItem, UniversityType,
    )

logger = logging.getLogger("ExtractData")


class Q1Payload(BaseModel):
    main_info: MainInfo
    contact: ContactInfo


class ExtractionError(RuntimeError):
    """Raised when a query could not be turned into valid schema data."""


# ---------------------------------------------------------------------------
# JSON repair
# ---------------------------------------------------------------------------

_FENCE_REGEX = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$", re.MULTILINE)

# NotebookLM appends grounding markers like [1] or [2, 5] after cited spans. They
# must be stripped, but ONLY where they cannot be JSON.
#
# The naive rule re.sub(r"\[\s*\d+(\s*,\s*\d+)*\s*\]", "", text) applied to the
# whole document is data-destroying: it deletes any legitimate all-numeric JSON
# array, so "phone_numbers": [1234567] becomes "phone_numbers": and the parse
# fails outright. These two patterns only match markers that sit INSIDE a JSON
# string literal (preceded by a word character or space, not by ':' or '[').
_CITATION_IN_STRING = re.compile(r"(?<=[\w\s.,;:)\]-])\s*\\?\[\s*\d+(?:\s*,\s*\d+)*\s*\\?\]")
_CITATION_TRAILING = re.compile(r"\s*\\?\[\s*\d+(?:\s*,\s*\d+)*\s*\\?\]\s*(?=[\"'])")


_CITATION_LEADING = re.compile(r"^\s*\\?\[\s*\d+(?:\s*,\s*\d+)*\s*\\?\]\s*")


def strip_citation_markers(text: str) -> str:
    """
    Remove NotebookLM grounding markers from inside JSON string values only.

    Operates string-by-string so that structural JSON arrays of numbers survive.

    Non-string regions are copied as whole slices located with str.find rather
    than one character per loop iteration. A 200 KB programme listing is >99%
    non-string structural text, so the per-character append dominated the cost
    of the entire repair stage.
    """
    out: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        quote = text.find('"', i)
        if quote == -1:
            out.append(text[i:])
            break
        out.append(text[i:quote])

        # Consume a complete JSON string literal, honouring escapes.
        j = quote + 1
        escaped = False
        while j < n:
            c = text[j]
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                break
            j += 1
        literal = text[quote + 1:j]
        cleaned = _CITATION_IN_STRING.sub("", literal)
        cleaned = _CITATION_LEADING.sub("", cleaned)
        out.append('"' + cleaned.rstrip() + '"')
        i = j + 1
    return "".join(out)


_FENCED_BLOCK_REGEX = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)


_CLOSERS = {"{": "}", "[": "]"}


def _balanced_span(text: str, start_idx: int) -> Tuple[str, int]:
    """
    Return (span, unclosed_depth) for the bracket container opening at start_idx.

    Maintains a real stack of open brackets rather than a single depth counter, so
    a truncated answer is closed with the correct sequence. Counting only the outer
    bracket type produced '...{}]' for a cut-off array of objects -- still invalid,
    because the inner object's '}' was never emitted.
    Quote-aware, so brackets inside string values do not affect nesting.
    """
    stack: List[str] = []
    in_string = False
    escape = False
    for i in range(start_idx, len(text)):
        char = text[i]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in _CLOSERS:
            stack.append(_CLOSERS[char])
        elif char in ("}", "]"):
            if stack and stack[-1] == char:
                stack.pop()
                if not stack:
                    return text[start_idx:i + 1], 0
            else:
                break  # malformed nesting; stop and treat as truncated

    # Truncated answer: close the open containers innermost-first so the partial
    # result is still usable rather than being discarded wholesale.
    tail = text[start_idx:]
    if in_string:
        tail += '"'
    return tail + "".join(reversed(stack)), len(stack)


def extract_json_str(text: str) -> str:
    """
    Extract the outermost balanced JSON value from a raw LLM answer.

    Candidate start positions are validated by actually parsing them, rather than
    trusting the first '{' or '[' encountered. That naive rule breaks on the very
    common preamble "Here are the programmes [1]:\\n```json\\n{...}```" -- the
    citation marker's '[' precedes the real object, so the extractor returned the
    string "[1]" and every such answer failed validation.
    """
    text = text.strip()

    # A fenced block is an explicit, unambiguous delimiter -- prefer it.
    fenced = _FENCED_BLOCK_REGEX.search(text)
    if fenced:
        candidate = strip_citation_markers(fenced.group(1).strip())
        span, _ = _balanced_span(candidate, 0) if candidate[:1] in "{[" else (candidate, 0)
        if _parses(span):
            return span

    body = strip_citation_markers(_FENCE_REGEX.sub("", text))

    # Collect top-level candidates and take the LONGEST viable one.
    #
    # "First that parses" is wrong in both directions: a "[1]" citation marker in
    # the preamble parses and would win over the real object that follows, while a
    # truncated array's inner "{}" parses and would win over the outer array we
    # actually want. Length breaks both ties correctly.
    best_balanced: Optional[str] = None
    first_unbalanced: Optional[str] = None

    i = 0
    while i < len(body):
        if body[i] not in "{[":
            i += 1
            continue
        span, unclosed = _balanced_span(body, i)
        if unclosed == 0:
            if _parses(span) and (best_balanced is None or len(span) > len(best_balanced)):
                best_balanced = span
            i += len(span)          # skip nested containers
            continue
        if first_unbalanced is None:
            first_unbalanced = span
        i += 1

    if best_balanced and first_unbalanced:
        # A truncated outer container beats a small complete fragment inside it.
        return max(best_balanced, first_unbalanced, key=len)
    return best_balanced or first_unbalanced or body


_TRAILING_COMMA_REGEX = re.compile(r",\s*([}\]])")


def _parses(candidate: str) -> bool:
    """True when `candidate` is valid JSON, tolerating trailing commas."""
    try:
        json.loads(candidate)
        return True
    except json.JSONDecodeError:
        try:
            json.loads(_TRAILING_COMMA_REGEX.sub(r"\1", candidate))
            return True
        except json.JSONDecodeError:
            return False


@lru_cache(maxsize=None)
def _adapter_for(target_model: Any) -> TypeAdapter:
    """
    Cache one TypeAdapter per target type.

    Building a TypeAdapter compiles a pydantic-core validator; doing that inside
    every query call re-paid the compilation for List[ProgramItem] on all five
    queries of every university.
    """
    return TypeAdapter(target_model)


def _validate_against(target_model: Any, parsed: Any) -> Any:
    """Validate `parsed` using a cached adapter, or the model's own validator."""
    if isinstance(target_model, type) and issubclass(target_model, BaseModel):
        return target_model.model_validate(parsed)
    try:
        adapter = _adapter_for(target_model)
    except TypeError:
        adapter = TypeAdapter(target_model)  # unhashable annotation; do not cache
    return adapter.validate_python(parsed)


def repair_and_validate_json(raw_text: str, target_model: Any) -> Any:
    """
    Clean fences and citation markers, balance brackets, parse, and validate.

    Raises ExtractionError with the offending text when the answer cannot be
    coerced into `target_model`, so the caller can decide whether to re-ask.
    """
    cleaned = extract_json_str(raw_text)
    if cleaned != raw_text:
        log_json_repaired(notebook_id="N/A", query_key="schema_parse", fix_type="extracted_json_span")
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Trailing commas are the single most common LLM JSON defect.
        repaired = _TRAILING_COMMA_REGEX.sub(r"\1", cleaned)
        try:
            parsed = json.loads(repaired)
            log_json_repaired(notebook_id="N/A", query_key="schema_parse", fix_type="stripped_trailing_commas")
        except json.JSONDecodeError as e:
            raise ExtractionError(f"Unparseable JSON ({e}); cleaned prefix: {cleaned[:200]!r}") from e

    try:
        return _validate_against(target_model, parsed)
    except ValidationError as e:
        raise ExtractionError(f"Schema validation failed: {e}") from e


# ---------------------------------------------------------------------------
# Deterministic reference data
# ---------------------------------------------------------------------------

_RANKINGS_CACHE: Optional[Dict[str, Any]] = None


def load_rankings_registry() -> Dict[str, Any]:
    """Load resources/rankings_pk.json once per process."""
    global _RANKINGS_CACHE
    if _RANKINGS_CACHE is None:
        try:
            with open(config.rankings_json_path, "r", encoding="utf-8") as f:
                _RANKINGS_CACHE = json.load(f).get("universities", {})
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Rankings registry unavailable ({e}); proceeding without it.")
            _RANKINGS_CACHE = {}
    return _RANKINGS_CACHE


def lookup_registry(domain: str) -> Optional[Dict[str, Any]]:
    """Find a university registry entry by canonical domain or alias."""
    registry = load_rankings_registry()
    key = domain.lower().replace("www.", "").strip("/")
    if key in registry:
        return registry[key]
    for canonical, entry in registry.items():
        if key == canonical or key in entry.get("aliases", []):
            return entry
        # Subdomain of a known institution (seecs.nust.edu.pk -> nust.edu.pk).
        if key.endswith("." + canonical):
            return entry
    return None


def apply_registry_facts(main_info: MainInfo, domain: str) -> MainInfo:
    """
    Overwrite LLM-guessed identity fields with registry ground truth.

    Rankings in particular are never taken from the model: a numeric world rank is
    the most confidently hallucinated field in the whole payload. If the registry
    has no rank, the payload correctly reports none.
    """
    entry = lookup_registry(domain)
    if not entry:
        return main_info

    main_info.name = entry.get("name") or main_info.name
    main_info.abbreviation = entry.get("abbreviation") or main_info.abbreviation
    main_info.city = entry.get("city") or main_info.city
    if entry.get("type"):
        try:
            main_info.type = UniversityType(entry["type"])
        except ValueError:
            pass

    main_info.rankings = [RankingItem(**r) for r in entry.get("rankings", [])]
    main_info.domain_verified = True
    main_info.verification_note = "Identity fields sourced from resources/rankings_pk.json registry."
    return main_info


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

_PROGRAM_STRUCTURE = """[
  {
    "name": "<Program Name>", "program_info_link": "<URL or null>",
    "department": "<or null>", "degree_level": "%s",
    "duration": "<e.g. 4 Years>", "tuition_fee": "<or null>", "currency": "PKR",
    "scholarships_info": "<or null>", "intake_terms": ["Fall"],
    "delivery_mode": "On-Campus", "application_fee": "<or null>",
    "career_prospects": "<or null>", "courses_taught": [],
    "summary_3_lines": "<exactly 3 short lines describing the programme>",
    "eligibility_requirements": {
      "minimum_marks_percentage": "<or null>", "entry_tests_accepted": [],
      "aggregate_formula": "<or null>"
    },
    "application_status": "open" | "closed" | "rolling" | "upcoming",
    "application_deadline": "<or null>"
  }
]"""

QUERY_SUITE: List[QuerySpec] = [
    QuerySpec(
        key="main_info_contact",
        prompt=Q1_PROMPT,
        model=Q1Payload,
        # Admissions/fees pages (T2) carry portal links; T4 carries contact details.
        tiers=(1, 2, 3, 4),
    ),
    QuerySpec(
        key="undergraduate",
        prompt=(
            "List every UNDERGRADUATE degree programme (BS, BSc, BBA, BA, BE, MBBS, "
            "LLB, PharmD) offered by this university, as a JSON array matching:\n"
            + (_PROGRAM_STRUCTURE % "undergraduate") + "\n" + _JSON_CONTRACT
        ),
        model=List[ProgramItem],
        tiers=(1, 2),
    ),
    QuerySpec(
        key="graduate",
        prompt=(
            "List every GRADUATE degree programme (MS, MSc, MBA, MPhil, MA, ME, LLM) "
            "offered by this university, as a JSON array matching:\n"
            + (_PROGRAM_STRUCTURE % "graduate") + "\n" + _JSON_CONTRACT
        ),
        model=List[ProgramItem],
        tiers=(1, 2),
    ),
    QuerySpec(
        key="postgraduate_phd",
        prompt=(
            "List every PhD and doctoral programme offered by this university, as a "
            "JSON array matching:\n"
            + (_PROGRAM_STRUCTURE % "postgraduate_phd") + "\n" + _JSON_CONTRACT
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


# ---------------------------------------------------------------------------
# Exa fallback
# ---------------------------------------------------------------------------

async def exa_find_application_portal(uni_domain: str, uni_name: str) -> Optional[str]:
    """
    Domain-scoped Exa search for an application portal URL.

    Restricted to the university's own domain and filtered by URL shape, because an
    unconstrained search returns third-party admissions aggregators that would be
    written into the payload as if they were official.
    """
    if not config.exa_api_key:
        return None

    try:
        from exa_py import AsyncExa
    except ImportError:
        logger.warning("exa_py not installed; skipping portal enrichment.")
        return None

    query = f"{uni_name} online admission application portal apply now"
    try:
        exa = AsyncExa(api_key=config.exa_api_key)
        res = await exa.search(query=query, include_domains=[uni_domain], num_results=5)
    except Exception as e:
        logger.warning(f"Exa search failed for {uni_domain}: {e}")
        return None

    results = getattr(res, "results", None) or []
    portal_markers = ("apply", "portal", "admission", "online-application", "register")
    for r in results:
        url = getattr(r, "url", "") or ""
        if any(m in url.lower() for m in portal_markers):
            logger.info(f"Exa portal candidate for {uni_domain}: {url}")
            return url
    return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def extract_university_payload(
    client: NotebookLMClient,
    notebook_id: str,
    uni_name: str,
    uni_domain: str,
    source_ids_by_tier: Optional[Dict[int, List[str]]] = None,
    tier1_source_count: int = 0,
) -> Tuple[UniversityPayload, ExtractionReport]:
    """
    Execute the 5-query suite and assemble a validated UniversityPayload.

    This function NEVER deletes the notebook. Deletion is a separate, explicit
    call made by the orchestrator only after the payload validates and has been
    persisted -- the previous implementation deleted inside a `finally:`, so any
    transient chat timeout destroyed all 60 ingested sources with no way to retry
    short of re-crawling and re-ingesting the whole university.

    Returns:
        (payload, report). Inspect report.ok / report.failed before trusting the
        payload: a query that failed yields an empty block, not an error.
    """
    report = ExtractionReport()

    def ids_for(spec: QuerySpec) -> Optional[List[str]]:
        if not source_ids_by_tier:
            return None
        ids: List[str] = []
        for tier in spec.tiers:
            ids.extend(source_ids_by_tier.get(tier, []))
        return ids or None

    # The five queries share no data, so they are issued concurrently under a
    # semaphore rather than in a serial loop.
    #
    # IMPORTANT -- measured, not assumed: against a single notebook this does NOT
    # currently reduce wall time. The notebooklm SDK takes a per-notebook_id lock
    # for the full duration of any chat.ask() made without a conversation_id
    # (_chat/api.py: `async with self._get_new_conversation_lock(notebook_id)`),
    # because the server treats concurrent unkeyed asks as racing turn N+1. The
    # SDK exposes no way to create independent conversations, so the suite
    # serialises inside the client no matter what we do here.
    #
    # This structure is kept because it is correct, costs nothing when
    # serialised, and is the piece that would have to exist anyway: the real
    # win available today is running multiple *notebooks* concurrently, where
    # the per-notebook lock no longer binds.
    sem = asyncio.Semaphore(max(1, config.query_concurrency))

    async def _run_one(spec: QuerySpec) -> Tuple[str, Any, ExtractionReport]:
        # Each query accumulates into its own sub-report, which is merged back in
        # QUERY_SUITE order below. Sharing one report across concurrent tasks
        # would make report.succeeded ordering depend on which answer landed
        # first, so identical inputs could produce different reports.
        sub = ExtractionReport()
        async with sem:
            value = await run_query(client, notebook_id, spec, ids_for(spec), sub)
        return spec.key, value, sub

    completed = await asyncio.gather(*(_run_one(spec) for spec in QUERY_SUITE))

    results: Dict[str, Any] = {}
    for key, value, sub in completed:   # gather preserves QUERY_SUITE order
        results[key] = value
        report.merge(sub)

    # --- Block 1 & 4: main_info + contact ---
    q1 = results.get("main_info_contact")
    if q1 is not None:
        main_info, contact_info = q1.main_info, q1.contact
    else:
        logger.error(f"{uni_name}: main_info query failed; emitting a minimal identity block.")
        main_info = MainInfo(
            name=uni_name,
            website=f"https://{uni_domain}",
            description=f"Identity block for {uni_name}; source extraction failed.",
            key_links=KeyLinks(),
        )
        contact_info = ContactInfo()

    main_info = apply_registry_facts(main_info, uni_domain)

    if not main_info.key_links.application_portal_url:
        portal = await exa_find_application_portal(uni_domain, main_info.name)
        if portal:
            main_info.key_links.application_portal_url = portal
            main_info.exa_enriched = True

    # --- Block 2: programs ---
    programs = ProgramCategoryBlock(
        undergraduate=results.get("undergraduate") or [],
        graduate=results.get("graduate") or [],
        postgraduate_and_phd=results.get("postgraduate_phd") or [],
    )

    # --- Truncation signal ---
    # A notebook built from N Tier-1 programme pages that yields far fewer
    # programmes than pages almost certainly had its answer cut short. This is a
    # free quality flag; the field was previously hardcoded to False.
    total_programs = (
        len(programs.undergraduate) + len(programs.graduate) + len(programs.postgraduate_and_phd)
    )
    truncated = bool(tier1_source_count) and total_programs < max(1, tier1_source_count // 2)
    if truncated:
        logger.warning(
            f"{uni_name}: {total_programs} programmes extracted from {tier1_source_count} "
            f"Tier-1 sources -- flagging programs_possibly_truncated."
        )

    payload = UniversityPayload(
        main_info=main_info,
        programs=programs,
        faculties=results.get("faculties") or [],
        contact=contact_info,
        programs_possibly_truncated=truncated,
    )
    return payload, report


async def delete_notebook_after_success(client: NotebookLMClient, notebook_id: str, uni_slug: Optional[str] = None) -> bool:
    """
    Free the workspace slot. Call ONLY once the payload is validated and written.

    Returns True on success; a failed delete leaks a slot but must never mask a
    successful extraction.
    """
    try:
        await client.notebooks.delete(notebook_id)
        log_notebook_deleted(notebook_id=notebook_id, trigger="success_cleanup", uni_slug=uni_slug)
        logger.info(f"Deleted notebook {notebook_id}.")
        return True
    except Exception as e:
        logger.warning(f"Failed to delete notebook {notebook_id}: {e}")
        return False
