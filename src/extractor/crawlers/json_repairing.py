"""
Turning NotebookLM's answer text into validated schema data.

The model returns prose-wrapped, fence-wrapped, citation-annotated, sometimes
truncated JSON. Everything here exists to recover a valid value from that without
ever inventing one: fence stripping, grounding-marker removal, balanced-span
extraction, trailing-comma repair, and pydantic validation against the target type.

Bottom of this package's dependency order -- imports nothing from its siblings.
"""

import json
import re

from functools import lru_cache
from pydantic import BaseModel, TypeAdapter, ValidationError
from typing import Any, List, Optional, Tuple

from src.logger.notebook_logger import log_json_repaired


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


validate_against = _validate_against


_VALID_OR_INVALID_ESCAPE = re.compile(
    r"""(
        \\["\\/bfnrt]
        | \\u[0-9a-fA-F]{4}
    ) | \\(.)""",
    re.VERBOSE
)


def sanitize_invalid_escapes(text: str) -> str:
    """
    Remove backslashes from invalid JSON escape sequences (e.g. \\$85,000, 100\\%, \\[1\\]).

    Preserves standard RFC 8259 JSON escape sequences:
    \\", \\\\, \\/, \\b, \\f, \\n, \\r, \\t, and \\uXXXX.
    """
    def repl(m: re.Match) -> str:
        if m.group(1):
            return m.group(1)  # valid escape, keep as-is
        return m.group(2)      # invalid escape, drop the backslash

    return _VALID_OR_INVALID_ESCAPE.sub(repl, text)


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
        # Trailing commas and invalid backslash escapes (e.g. \$85,000) are common LLM JSON defects.
        repaired = _TRAILING_COMMA_REGEX.sub(r"\1", cleaned)
        try:
            parsed = json.loads(repaired)
            log_json_repaired(notebook_id="N/A", query_key="schema_parse", fix_type="stripped_trailing_commas")
        except json.JSONDecodeError:
            repaired_escapes = sanitize_invalid_escapes(repaired)
            try:
                parsed = json.loads(repaired_escapes)
                log_json_repaired(notebook_id="N/A", query_key="schema_parse", fix_type="sanitized_invalid_escapes")
            except json.JSONDecodeError as e:
                raise ExtractionError(f"Unparseable JSON ({e}); cleaned prefix: {cleaned[:200]!r}") from e

    try:
        return _validate_against(target_model, parsed)
    except ValidationError as e:
        raise ExtractionError(f"Schema validation failed: {e}") from e

