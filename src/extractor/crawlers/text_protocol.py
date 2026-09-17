"""
The delimited-text wire format, and the parser that turns it back into schema data.

JSON is a fragile contract over this channel. It carries four independent ways to
fail that have nothing to do with whether the model knew the answer -- brace
balance, quote balance, escape sequences, and trailing commas -- and every one of
them has cost a real query block:

  * Yale burned all three attempts on one ``Invalid \\escape`` (2026-09-16).
  * ITU's oversized answers had to be recovered through a bracket-stack repair
    that reconstructs the closing sequence of a truncated array of objects.
  * The citation-marker stripper in json_repairing.py exists entirely because
    ``[1]`` is indistinguishable from a JSON array without tracking string state.

None of those failures is possible in a format with no braces, no quotes and no
escapes. A record is a run of ``KEY: value`` lines between ``@@RECORD`` markers::

    @@RECORD
    NAME: BS Computer Science
    LEVEL: bachelors
    DEADLINES: 2026-08-15 (Fall) ;; 2026-12-01 (Spring)
    DESCRIPTION: <one paragraph, on one line>
    @@END

Every parser rule below exists to survive a specific way a model degrades, and
each degrades to *correct output* rather than to an exception.

This module is the single source of truth for both halves of the contract: the
prompt text is GENERATED from the same field table the parser reads, so a field
added to the schema cannot be requested without being parsed, or parsed without
being requested. That drift is what makes hand-written prompt/parser pairs rot.

Bottom of this package's dependency order -- imports only from utilities.
"""

import re

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Wire vocabulary
# ---------------------------------------------------------------------------

RECORD_START = "@@RECORD"
RECORD_END = "@@END"
LIST_SEPARATOR = " ;; "
NONE_SENTINEL = "NONE"

# Values that mean "the sources do not say". Accepting more than the one
# sentinel we asked for is deliberate: a model that writes "N/A" knew the answer
# was absent, and rejecting its phrasing would turn a correct non-answer into a
# parse failure and a wasted repair ask.
_EMPTY_VALUES = frozenset({
    "", "none", "null", "n/a", "na", "-", "--", "nil", "unknown",
    "not specified", "not stated", "not mentioned", "not available",
    "not provided", "not applicable", "unspecified",
})

# A key line, tolerating the ways models decorate one. The leading class absorbs
# markdown bolding (``**NAME:**``), list bullets (``- NAME:``), blockquote marks
# and heading hashes; the trailing class absorbs the closing ``**``. Anchored on
# an ALL-CAPS token so a sentence inside a DESCRIPTION cannot masquerade as a
# key -- and even then the key must be on the allowlist to be honoured.
_KEY_LINE = re.compile(
    r"^[\s>#*_\-•·]*"      # bullets, bolding, blockquotes, headings
    r"([A-Z][A-Z0-9_]*)"             # the key itself
    r"[\s*_]*:\s?"                   # separator, tolerating '**:' and ' :'
    r"(.*)$"
)

# Splits a multi-value field. The canonical separator is ' ;; ', but a model
# that omits the spaces meant the same thing.
_LIST_SPLIT = re.compile(r"\s*;;\s*")

# A record marker, however the model decorated it.
_RECORD_START_RE = re.compile(r"^[\s>#*_\-]*@@\s*RECORD\b", re.IGNORECASE)
_RECORD_END_RE = re.compile(r"^[\s>#*_\-]*@@\s*END\b", re.IGNORECASE)

# Residual markdown emphasis around a whole value. Stripped rather than kept:
# '**2,000**' is the fee 2,000, and carrying the asterisks into the payload
# would put them in front of a student.
_WRAPPING_EMPHASIS = re.compile(r"^\s*(\*\*|__|\*|_|`)+\s*|\s*(\*\*|__|\*|_|`)+\s*$")

# NotebookLM's grounding markers. Unlike the JSON path, stripping these here is
# unambiguous: there are no numeric arrays in this format for the pattern to
# collide with, which is the entire reason json_repairing.py needs 40 lines of
# string-state tracking to do the same job.
_CITATION_MARKER = re.compile(r"\s*\[\s*\d+(?:\s*,\s*\d+)*\s*\]")


# ---------------------------------------------------------------------------
# Field tables
# ---------------------------------------------------------------------------

SCALAR = "scalar"
LIST = "list"


@dataclass(frozen=True)
class FieldSpec:
    """One wire key, and where its value lands in the schema."""
    key: str                      # the ALL-CAPS wire key
    path: Tuple[str, ...]         # dotted destination, e.g. ('eligibility_requirements', 'aggregate_formula')
    kind: str = SCALAR            # SCALAR or LIST
    hint: str = ""                # what the prompt tells the model to put here
    required: bool = False        # emitted in the prompt's "always include" line


def _f(key: str, path: str, kind: str = SCALAR, hint: str = "", required: bool = False) -> FieldSpec:
    return FieldSpec(key=key, path=tuple(path.split(".")), kind=kind, hint=hint, required=required)


# --- Programme detail: the full ProgramItem ---------------------------------
#
# Nested schema objects are FLAT on the wire. `eligibility_requirements` is
# three scalars, and asking a model to nest them buys nothing but a way to get
# the nesting wrong; the parser re-nests from `path`.
PROGRAM_FIELDS: Tuple[FieldSpec, ...] = (
    _f("NAME", "name", hint="programme name exactly as the university publishes it", required=True),
    _f("LEVEL", "degree_level", hint="one of: bachelors, masters, phd, diploma", required=True),
    _f("LINK", "program_info_link", hint="URL of this programme's own page"),
    _f("DEPARTMENT", "department", hint="owning department, school or faculty"),
    _f("DURATION", "duration", hint="e.g. 4 Years, 2 Years, 18 Months"),
    _f("TUITION", "tuition_fee", hint="tuition exactly as published, digits and units only"),
    _f("CURRENCY", "currency", hint="currency the university published the fee in, e.g. PKR, EUR, USD"),
    _f("APP_FEE", "application_fee", hint="application or processing fee; a university-wide fee counts"),
    _f("DEADLINES", "application_deadlines", LIST,
       hint="one entry per published deadline, e.g. 2026-08-15 (Fall) ;; 2026-12-01 (Spring)"),
    _f("INTAKES", "intake_terms", LIST, hint="intake terms as the university names them"),
    _f("STATUS", "application_status", hint="one of: open, closed, rolling, upcoming"),
    _f("DELIVERY", "delivery_mode", hint="On-Campus, Online or Hybrid"),
    _f("MIN_MARKS", "eligibility_requirements.minimum_marks_percentage",
       hint="minimum marks or grade required for entry"),
    _f("ENTRY_TESTS", "eligibility_requirements.entry_tests_accepted", LIST,
       hint="accepted entry tests"),
    _f("AGGREGATE", "eligibility_requirements.aggregate_formula",
       hint="how the merit aggregate is calculated"),
    _f("REQUIREMENTS", "admission_requirements",
       hint="how to apply and what is required beyond marks: documents, interviews, portfolios, prerequisites"),
    _f("SCHOLARSHIPS", "scholarships_info", hint="scholarships or financial aid available to this programme"),
    _f("CAREERS", "career_prospects", hint="career outcomes or roles this programme leads to"),
    _f("COURSES", "courses_taught", LIST, hint="notable courses in the curriculum"),
    _f("DESCRIPTION", "description",
       hint="ONE FULL PARAGRAPH on a SINGLE line: what the programme covers, its focus areas, "
            "learning outcomes and any distinctive specializations"),
)

# --- Roster: discovery only -------------------------------------------------
#
# Four short fields, one line each. This ask enumerates every programme the
# university offers and CANNOT overflow the response ceiling, which is the whole
# point of separating discovery from description.
ROSTER_FIELDS: Tuple[FieldSpec, ...] = (
    _f("NAME", "name", hint="programme name exactly as published", required=True),
    _f("LEVEL", "degree_level", hint="one of: bachelors, masters, phd, diploma", required=True),
    _f("LINK", "program_info_link", hint="URL of this programme's own page"),
    _f("DEPARTMENT", "department", hint="owning department, school or faculty"),
)

# --- Gap-fill: the two fields the audit blocks on ---------------------------
GAPFILL_FIELDS: Tuple[FieldSpec, ...] = (
    _f("NAME", "name", hint="programme name exactly as it appears in the list above", required=True),
    _f("APP_FEE", "application_fee",
       hint="application or processing fee; if the university charges ONE fee for all "
            "applicants, give that same figure for every programme"),
    _f("DEADLINES", "application_deadlines", LIST,
       hint="every published application deadline; a university-wide admission "
            "schedule applies to every programme in that intake"),
    _f("TUITION", "tuition_fee", hint="tuition exactly as published"),
    _f("CURRENCY", "currency", hint="currency of the fees above"),
)

# --- Faculties --------------------------------------------------------------
FACULTY_FIELDS: Tuple[FieldSpec, ...] = (
    _f("FACULTY", "faculty_name", hint="faculty or school name", required=True),
    _f("DESCRIPTION", "description", hint="one line on what this faculty covers"),
    _f("DEPARTMENTS", "departments", LIST, hint="constituent departments"),
    _f("WEBSITE", "faculty_website", hint="faculty homepage URL"),
)

# --- Identity: MainInfo + ContactInfo as one record -------------------------
IDENTITY_FIELDS: Tuple[FieldSpec, ...] = (
    _f("NAME", "main_info.name", hint="official university name", required=True),
    _f("ABBREVIATION", "main_info.abbreviation", hint="common abbreviation, e.g. ITU, MIT"),
    _f("COUNTRY", "main_info.country", hint="country, e.g. Pakistan, Germany, USA"),
    _f("CITY", "main_info.city", hint="city of the main campus"),
    _f("ESTABLISHED", "main_info.established_year", hint="year founded, digits only"),
    _f("ACCREDITATION", "main_info.accreditation_body", hint="accrediting body, e.g. HEC, ABET, WASC"),
    _f("CYCLES", "main_info.admission_cycles_offered", LIST,
       hint="admission terms as the university names them"),
    _f("LANGUAGE", "main_info.primary_instruction_language", hint="main teaching language"),
    _f("WEBSITE", "main_info.website", hint="main website URL", required=True),
    _f("TYPE", "main_info.type", hint="public or private"),
    _f("DESCRIPTION", "main_info.description",
       hint="concise overview of the university, on a SINGLE line", required=True),
    _f("ACADEMICS_URL", "main_info.key_links.academics_url", hint="academics or programmes index URL"),
    _f("ADMISSIONS_URL", "main_info.key_links.admissions_url", hint="admissions section URL"),
    _f("PORTAL_URL", "main_info.key_links.application_portal_url",
       hint="the page where an applicant actually SUBMITS an application"),
    _f("EMAIL", "contact.official_email", hint="official contact email"),
    _f("PHONES", "contact.phone_numbers", LIST, hint="contact phone numbers"),
    _f("ADDRESS", "contact.physical_address", hint="postal address of the main campus"),
    _f("ADMISSIONS_OFFICE", "contact.admissions_office_location", hint="where the admissions office is"),
    _f("SUB_CAMPUSES", "contact.sub_campuses_contact", LIST,
       hint="other campuses as 'Campus Name: contact details'"),
)


# ---------------------------------------------------------------------------
# Prompt generation
# ---------------------------------------------------------------------------

def render_format_contract(
    fields: Sequence[FieldSpec],
    plural: bool = True,
    max_items: Optional[int] = None,
) -> str:
    """
    Build the format instructions for a prompt from the field table itself.

    Generated rather than written by hand so the prompt and the parser cannot
    drift: a field added to the table is requested and parsed in the same edit,
    and one removed stops being both. The previous JSON prompts embedded a
    literal structure block that had to be kept in step with schema.py by hand.

    `max_items` writes an explicit ceiling and an explicit stop instruction into
    the contract. Nothing else in a list prompt bounds the number of records,
    and an unbounded repeating template is the shape that makes a model loop:
    ITU's roster ask streamed past the 52 MB ceiling on both attempts of run
    `s_3`, which is ~50 million characters for an answer whose correct form is
    about 1.5 KB. A cap the caller chooses is the only thing here that can say
    "and then stop".
    """
    lines = [
        "Return your answer as plain text records in EXACTLY this format, and nothing else.",
        "No JSON, no markdown, no tables, no preamble, no closing remarks.",
        "",
        RECORD_START,
    ]
    for spec in fields:
        lines.append(f"{spec.key}: <{spec.hint}>" if spec.hint else f"{spec.key}: <value>")
    lines.append(RECORD_END)
    lines.append("")
    lines.append("Rules:")
    if plural:
        # Phrased as "one block per item, then stop" rather than "repeat the
        # block once per item". The instruction is the same; the first wording
        # names a termination condition and the second names an action to keep
        # doing, and the second is what was in the prompt that ran away.
        lines.append(f"- Emit ONE {RECORD_START}..{RECORD_END} block per item, then stop.")
        lines.append("- Never emit the same item twice. Each item appears exactly once.")
    if max_items:
        lines.append(
            f"- Emit AT MOST {max_items} blocks in total. If there are more than "
            f"{max_items}, emit the first {max_items} and stop."
        )
    lines.append("- One key per line. Keep each value on a SINGLE line, however long it is.")
    lines.append(f"- Separate multiple values within one field with ' {LIST_SEPARATOR.strip()} '.")
    lines.append(
        f"- If the sources do not state a value, write exactly {NONE_SENTINEL}. "
        f"Never guess, and never omit the line."
    )
    lines.append("- Do not add keys that are not listed above.")
    lines.append("- Do not use quotes, braces or brackets around values.")

    required = [s.key for s in fields if s.required]
    if required:
        lines.append(f"- {', '.join(required)} must always carry a real value.")
    return "\n".join(lines)


def render_name_list(names: Sequence[str]) -> str:
    """Render programme names for a closed-question prompt, one per line."""
    return "\n".join(f"- {n}" for n in names)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _clean_value(raw: str) -> str:
    """Strip citation markers and wrapping emphasis from one value."""
    value = _CITATION_MARKER.sub("", raw).strip()
    # Applied twice: the regex strips one side per pass.
    value = _WRAPPING_EMPHASIS.sub("", value)
    value = _WRAPPING_EMPHASIS.sub("", value)
    return value.strip()


def _is_empty(value: str) -> bool:
    return value.strip().casefold() in _EMPTY_VALUES


def _split_list(value: str) -> List[str]:
    return [v for v in (_clean_value(p) for p in _LIST_SPLIT.split(value)) if v and not _is_empty(v)]


def _assign_container(target: Dict[str, Any], path: Sequence[str]) -> None:
    """Ensure an empty dict exists at a dotted path, without disturbing values."""
    node = target
    for part in path:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            return


def _assign(target: Dict[str, Any], path: Sequence[str], value: Any) -> None:
    """Write `value` at a dotted path, creating intermediate dicts."""
    node = target
    for part in path[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):  # a scalar already claimed this name
            return
    node[path[-1]] = value


def parse_records(text: str, fields: Sequence[FieldSpec]) -> List[Dict[str, Any]]:
    """
    Turn a delimited-text answer into dicts keyed by schema field names.

    Never raises on malformed input: a record this cannot read yields fewer
    fields, not an exception. Deciding that a result is unusable belongs to
    Pydantic validation downstream, which can say exactly which field was wrong.

    Degradation rules, each answering an observed model behaviour:

    * **Unknown keys are ignored, not guessed.** A model that invents
      ``CREDITS:`` has not told us anything the schema can hold.
    * **A non-key line extends the previous value.** A model that hard-wraps a
      long DESCRIPTION produces the whole paragraph, not a parse error. This is
      the single most likely way the format degrades in practice.
    * **``@@RECORD`` implicitly closes an open record.** A truncated answer
      loses only its final record, with no bracket-stack reconstruction needed.
    * **Text before the first ``@@RECORD`` is discarded**, so a prose preamble
      ("Here are the programmes:") costs nothing.
    * **A record with no recognised keys is dropped**, so stray markers do not
      produce empty rows that later look like missing data.
    """
    by_key = {spec.key: spec for spec in fields}

    records: List[Dict[str, Any]] = []
    current: Optional[Dict[str, str]] = None   # wire key -> raw value
    last_key: Optional[str] = None

    def flush() -> None:
        nonlocal current, last_key
        if current:
            parsed = _build_record(current, by_key)
            if parsed:
                records.append(parsed)
        current, last_key = None, None

    for line in text.splitlines():
        if _RECORD_START_RE.match(line):
            flush()                      # implicit close of an unterminated record
            current, last_key = {}, None
            continue
        if _RECORD_END_RE.match(line):
            flush()
            continue
        if current is None:
            continue                     # preamble, or prose between records

        match = _KEY_LINE.match(line)
        if match and match.group(1) in by_key:
            key = match.group(1)
            current[key] = match.group(2)
            last_key = key
            continue

        # Not a recognised key line. If it is a continuation of the previous
        # value, append it; a blank line separates nothing and is dropped.
        if last_key is not None and line.strip():
            current[last_key] = f"{current[last_key]} {line.strip()}".strip()

    flush()
    return records


def _build_record(raw: Dict[str, str], by_key: Dict[str, FieldSpec]) -> Dict[str, Any]:
    """
    Convert one record's raw wire values into a nested schema-shaped dict.

    Nested containers are created even when the model answered none of their
    keys. `MainInfo.key_links` and `Q1Payload.contact` are REQUIRED objects
    whose every member is optional, so a university that publishes no portal
    link and no phone number would otherwise produce a record with no
    `key_links` key at all -- and the whole identity block would fail validation
    over an answer that was entirely correct. The flattened wire format is what
    makes this possible; the nesting has to be restored whether or not the model
    had anything to put in it.
    """
    out: Dict[str, Any] = {}
    populated = False

    for spec in by_key.values():
        if len(spec.path) > 1:
            _assign_container(out, spec.path[:-1])

    for key, raw_value in raw.items():
        spec = by_key.get(key)
        if spec is None:
            continue
        value = _clean_value(raw_value)

        if spec.kind == LIST:
            items = [] if _is_empty(value) else _split_list(value)
            _assign(out, spec.path, items)
            populated = populated or bool(items)
        else:
            scalar = None if _is_empty(value) else value
            _assign(out, spec.path, scalar)
            populated = populated or scalar is not None

    return out if populated else {}


def parse_single_record(text: str, fields: Sequence[FieldSpec]) -> Optional[Dict[str, Any]]:
    """
    Parse an answer expected to hold exactly one record.

    The first record wins when a model emits several. For the identity query the
    alternative is merging two descriptions of the same university, which
    produces a record that is neither of them.
    """
    records = parse_records(text, fields)
    return records[0] if records else None
