"""
The five-query extraction suite and its execution against one notebook.

Each QuerySpec is bound to the source tiers that can answer it, so a query about
fees is not grounded in faculty pages. ExtractionReport records the per-query
outcome, so a partially-failed extraction is never reported as clean.
"""

import asyncio
import logging

from dataclasses import dataclass, field, replace
from notebooklm import NotebookLMClient
from notebooklm.exceptions import RPCResponseTooLargeError
from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Sequence, Tuple, get_args, get_origin

from src.config import config
from src.logger.notebook_logger import log_query_executed
from src.utilities.schema import ContactInfo, FacultyItem, MainInfo, ProgramItem

# Shared with the direct-extraction engines (C33). The suite, the report and the
# Q1 payload live in query_schemas now; this module adds only the text-protocol
# attributes NotebookLM's wire format needs on top of them.
from src.extractor.crawlers.query_schemas import (  # noqa: F401  (re-exported)
    ExtractionReport,
    Q1Payload,
    QuerySpec,
)
from src.extractor.crawlers.query_schemas import QUERY_SUITE as _BASE_QUERY_SUITE

from src.extractor.crawlers.json_repairing import (
    ExtractionError,
    repair_and_validate_json,
    validate_against,
)
from src.extractor.crawlers.text_protocol import (
    FACULTY_FIELDS,
    IDENTITY_FIELDS,
    PROGRAM_FIELDS,
    RECORD_END,
    RECORD_START,
    FieldSpec,
    parse_records,
    parse_single_record,
    render_format_contract,
)
from src.ingestor.notebook_lifecycle import patch_notebooklm_rpc_size_limit

# Same registry entry as every other module in this package: logging.getLogger
# returns one object per name, so this is the logger extract_data.py created.
logger = logging.getLogger("ExtractData")

# Ensure the streaming size cap is widened for query execution
patch_notebooklm_rpc_size_limit()


class QueryTimeoutError(TimeoutError):
    """A single chat.ask exceeded config.chat_timeout_sec and was abandoned."""


class QuotaThrottledError(RuntimeError):
    """Raised when NotebookLM streaming terminates with 0 chunks due to usage/rate limits."""


def is_empty_stream_throttle_error(error: BaseException) -> bool:
    """True when NotebookLM returns 0 parseable chunks in a stream (rate limiting / usage quota)."""
    text = str(error).lower()
    return (
        "no parseable chunks in streaming chat response" in text
        or "4 lines scanned" in text
        or "the response was empty" in text
    )

Q1_TEXT_PROMPT = (
    "Extract the university's main information and contact details from the provided sources.\n\n"
    + render_format_contract(IDENTITY_FIELDS, plural=False)
)


def _make_degree_text_prompt(level: str, keywords: str, specific_rules: str = "") -> str:
    rules_block = f"\n{specific_rules}\n" if specific_rules else ""
    return (
        f"Extract ALL {level.upper()} degree programmes ({keywords}) offered by this university from the provided sources.{rules_block}\n"
        + render_format_contract(PROGRAM_FIELDS, plural=True)
    )


FACULTIES_TEXT_PROMPT = (
    "Extract all faculties, schools, and their constituent departments from the provided sources.\n\n"
    + render_format_contract(FACULTY_FIELDS, plural=True)
)

# The shared suite carries the JSON prompts; NotebookLM additionally needs the
# delimited text-protocol prompt and field spec per block, layered on here so
# the prompt text itself is never duplicated between the two engines.
_TEXT_PROTOCOL_OVERRIDES: Dict[str, Tuple[Tuple[FieldSpec, ...], str]] = {
    "main_info_contact": (IDENTITY_FIELDS, Q1_TEXT_PROMPT),
    "bachelors": (PROGRAM_FIELDS, _make_degree_text_prompt(
        "bachelors",
        "BS, BSc, BA, BBA, BE, B.Ed, BFA, MBBS, LLB, PharmD, DPT",
        "MBBS, PharmD and DPT are bachelors-level entry programmes here; list them in this query, not the PhD one.",
    )),
    "masters": (PROGRAM_FIELDS, _make_degree_text_prompt(
        "masters",
        "MS, MSc, MA, MBA, MPhil, M.Ed, LLM, ME",
        "Include MPhil programmes here. Exclude postgraduate diplomas and certificates -- those belong to the diploma query.",
    )),
    "phd": (PROGRAM_FIELDS, _make_degree_text_prompt(
        "phd",
        "PhD and research doctorates",
        "Research doctorates only. Do not include post-doctoral fellowships, which are appointments rather than programmes.",
    )),
    "diploma": (PROGRAM_FIELDS, _make_degree_text_prompt(
        "diploma",
        "DIPLOMA and CERTIFICATE programmes (postgraduate diploma, PGD, advanced diploma, professional certificate)",
        "Award-bearing programmes only. Do not list individual courses or modules that are part of a degree.",
    )),
    "faculties": (FACULTY_FIELDS, FACULTIES_TEXT_PROMPT),
}

QUERY_SUITE: List[QuerySpec] = [
    replace(spec, fields=_TEXT_PROTOCOL_OVERRIDES[spec.key][0],
            text_prompt=_TEXT_PROTOCOL_OVERRIDES[spec.key][1])
    if spec.key in _TEXT_PROTOCOL_OVERRIDES else spec
    for spec in _BASE_QUERY_SUITE
]


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
    """
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
        # Re-raised as our own type so the retry loop's log line says what
        # happened. A bare TimeoutError here is indistinguishable from an HTTP
        # read timeout raised several layers down.
        raise QueryTimeoutError(
            f"NotebookLM did not answer within {config.chat_timeout_sec}s"
        ) from e
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
    """Validate records one at a time, keeping the ones that pass."""
    valid: List[Any] = []
    rejected = 0
    for record in records:
        try:
            valid.append(validate_against(item_model, record))
        except Exception as e:
            rejected += 1
            if rejected <= 3:
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
) -> Any:
    """Parse an answer using either the delimited-text protocol or JSON repair."""
    use_text = _use_text_protocol(spec)
    has_text_markers = RECORD_START in raw or RECORD_END in raw

    if (use_text or has_text_markers) and spec.fields:
        if spec.single:
            record = parse_single_record(raw, spec.fields)
            if record:
                try:
                    return validate_against(spec.model, record)
                except Exception as e:
                    raise ExtractionError(f"schema validation failed for '{spec.key}': {e}") from e
            elif not has_text_markers:
                return repair_and_validate_json(raw, spec.model)
            else:
                raise ExtractionError(
                    f"no parseable record in the answer for '{spec.key}' (prefix: {raw[:200]!r})"
                )
        else:
            records = parse_records(raw, spec.fields)
            if records:
                return _validate_leniently(_item_type(spec.model), records, spec.key, report)
            elif not has_text_markers:
                return repair_and_validate_json(raw, spec.model)
            else:
                return []

    return repair_and_validate_json(raw, spec.model)


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
    use_text = _use_text_protocol(spec)
    default_prompt = spec.text_prompt if (use_text and spec.text_prompt) else spec.prompt

    for attempt in range(config.max_query_retries + 1):
        # Reset per attempt: a stale answer from an earlier attempt must not be
        # quoted back to the model as "your previous answer" after a transport
        # failure that produced none.
        raw = ""
        prompt = default_prompt
        if attempt > 0:
            if use_text:
                prompt = (
                    f"Your previous answer could not be parsed.\n"
                    f"Error: {last_error}\n"
                    f"Previous answer (truncated):\n{raw[:1500]}\n\n"
                    f"Re-emit the SAME data in EXACTLY the @@RECORD..@@END format requested.\n\n{default_prompt}"
                )
            else:
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
            value = parse_answer(raw, spec, report)
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
            if is_empty_stream_throttle_error(e):
                logger.warning(
                    f"[{spec.key}] Empty stream response detected (usage throttle). Halting retry loop."
                )
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
        report.consecutive_throttle_errors = 0
        report.succeeded.append(spec.key)
        return value

    if error is not None and is_empty_stream_throttle_error(error):
        report.consecutive_throttle_errors += 1
        if report.consecutive_throttle_errors >= 2:
            logger.error(
                f"NotebookLM empty stream rate limit / throttle detected consecutively "
                f"({report.consecutive_throttle_errors} blocks). Tripping circuit breaker."
            )
            report.failed[spec.key] = str(error)
            raise QuotaThrottledError(
                f"NotebookLM empty stream throttle detected consecutively across query blocks: {error}"
            )
    else:
        report.consecutive_throttle_errors = 0

    report.failed[spec.key] = str(error) if error else "query returned no value"
    return None
