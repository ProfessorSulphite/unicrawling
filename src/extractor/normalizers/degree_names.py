"""
Canonical degree-level naming (C17).

Replaces the old three-way undergraduate / graduate / postgraduate_and_phd
split with four levels -- bachelors, masters, phd, diploma -- and maps any
observed degree string onto exactly one of them. Post-doctoral is excluded
entirely (plan section 1, note 5): a post-doc is a research appointment, not a
programme a counselling student applies to.

Two things make this harder than a dictionary lookup, and both were found in
real extracted payloads rather than imagined:

  "Post-RN Bachelor of Science in Nursing"  -- starts with "Post", is a bachelors
  "Doctor of Physical Therapy (DPT)"        -- says "Doctor", is a bachelors

So matching is on PATH-STYLE TOKENS, never substrings (the same lesson the link
filter learned the hard way), and entry-level professional doctorates are
resolved before the PhD rule ever runs.
"""
from __future__ import annotations

import re
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

from src.utilities.schema import DegreeLevel

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_SEPARATORS = re.compile(r"[^a-z0-9]+")

# Abbreviations that arrive split across a space instead of a dot: real payloads
# carry both "M.Phil. Economics" and "M Phil Management Sciences". Dots are
# stripped before tokenising, which folds the first form on its own; this table
# folds the second.
_SPLIT_ABBREVIATIONS: Dict[Tuple[str, str], str] = {
    ("m", "phil"): "mphil",
    ("m", "ed"): "med",
    ("m", "sc"): "msc",
    ("m", "a"): "ma",
    ("m", "s"): "ms",
    ("b", "sc"): "bsc",
    ("b", "ed"): "bed",
    ("b", "a"): "ba",
    ("b", "s"): "bs",
    ("ph", "d"): "phd",
    ("pharm", "d"): "pharmd",
    ("d", "phil"): "dphil",
}


def tokenize_degree_name(name: str) -> List[str]:
    """Lowercase token list, with dotted and space-split abbreviations folded.

    "M.Phil. in Applied Psychology" and "M Phil Management Sciences" both yield
    a bare "mphil" token, so one rule matches both.
    """
    text = (name or "").lower().replace(".", "")
    raw = [tok for tok in _SEPARATORS.split(text) if tok]

    folded: List[str] = []
    index = 0
    while index < len(raw):
        pair = (raw[index], raw[index + 1]) if index + 1 < len(raw) else None
        if pair is not None and pair in _SPLIT_ABBREVIATIONS:
            folded.append(_SPLIT_ABBREVIATIONS[pair])
            index += 2
        else:
            folded.append(raw[index])
            index += 1
    return folded


# ---------------------------------------------------------------------------
# Level markers
# ---------------------------------------------------------------------------

# An explicit research marker. Unambiguous, and it out-ranks the entry-level
# rule below: "PhD in Pharmacy Practice" is a research doctorate that merely
# happens to be about pharmacy.
_RESEARCH_DOCTORATE_TOKENS: FrozenSet[str] = frozenset({
    "phd", "dphil", "dsc", "edd",
})

# The vaguer "Doctor of ..." form, which in these markets covers both research
# doctorates and entry-level professional ones. Not a level on its own -- the
# entry-level check gets to veto it.
_PROFESSIONAL_DOCTOR_TOKENS: FrozenSet[str] = frozenset({
    "doctor", "doctorate", "doctoral",
})

_DOCTOR_TOKENS: FrozenSet[str] = _RESEARCH_DOCTORATE_TOKENS | _PROFESSIONAL_DOCTOR_TOKENS

# Professional doctorates that are ENTRY-LEVEL undergraduate degrees in the
# markets this pipeline covers: DPT and PharmD are five-year post-intermediate
# programmes, not research doctorates. The extractor has filed the same
# physical-therapy degree under "undergraduate" in one payload and
# "postgraduate_and_phd" in another; this rule settles it as bachelors.
_ENTRY_DOCTORATE_TOKENS: FrozenSet[str] = frozenset({
    "dpt", "pharmd", "dvm", "dds", "dpharm",
})
_ENTRY_DOCTORATE_SUBJECTS: Tuple[str, ...] = (
    "physical therapy", "physiotherapy", "pharmacy", "veterinary medicine",
    "dental surgery",
)

_DIPLOMA_TOKENS: FrozenSet[str] = frozenset({
    "diploma", "pgd", "pgdip", "certificate", "certification",
})

_MASTERS_TOKENS: FrozenSet[str] = frozenset({
    "ms", "msc", "mscs", "mscn", "ma", "mba", "emba", "mphil", "med", "llm",
    "mmed", "mbe", "mecd", "mhpm", "mhpe", "msph", "mshds", "mshm", "mpa",
    "mpp", "mph", "mfa", "mtech", "mcom", "mcs", "mse", "meng",
    "master", "masters",
})

_BACHELORS_TOKENS: FrozenSet[str] = frozenset({
    "bs", "bsc", "bscs", "bsn", "ba", "bba", "be", "bed", "bfa", "bds",
    "bdes", "bse", "bee", "bcs", "bcom", "btech", "barch", "llb",
    "mbbs", "mbchb", "bachelor", "bachelors",
})

# Order matters. Diploma runs before the degree rules so "Postgraduate Diploma
# in Education" is a diploma rather than a masters; PhD runs before masters so
# "Doctor of Philosophy" is not caught by a stray "philosophy"; masters runs
# before bachelors, which is safe because no observed name carries markers for
# both.
_ORDERED_RULES: Sequence[Tuple[DegreeLevel, FrozenSet[str]]] = (
    (DegreeLevel.DIPLOMA, _DIPLOMA_TOKENS),
    (DegreeLevel.PHD, _DOCTOR_TOKENS),
    (DegreeLevel.MASTERS, _MASTERS_TOKENS),
    (DegreeLevel.BACHELORS, _BACHELORS_TOKENS),
)

# Values the extractor may emit in the degree_level field itself, including the
# three pre-C17 names still sitting in every payload written before this commit.
_DECLARED_ALIASES: Dict[str, DegreeLevel] = {
    "undergraduate": DegreeLevel.BACHELORS,
    "undergrad": DegreeLevel.BACHELORS,
    "bachelor": DegreeLevel.BACHELORS,
    "bachelors": DegreeLevel.BACHELORS,
    "graduate": DegreeLevel.MASTERS,
    "postgraduate": DegreeLevel.MASTERS,
    "master": DegreeLevel.MASTERS,
    "masters": DegreeLevel.MASTERS,
    "postgraduate_phd": DegreeLevel.PHD,
    "postgraduate_and_phd": DegreeLevel.PHD,
    "phd": DegreeLevel.PHD,
    "doctorate": DegreeLevel.PHD,
    "doctoral": DegreeLevel.PHD,
    "diploma": DegreeLevel.DIPLOMA,
    "certificate": DegreeLevel.DIPLOMA,
}


def _is_entry_level_doctorate(tokens: FrozenSet[str], text: str) -> bool:
    if tokens & _ENTRY_DOCTORATE_TOKENS:
        return True
    if tokens & _RESEARCH_DOCTORATE_TOKENS:
        # "PhD in Pharmacy Practice" -- the subject is pharmacy, the degree is not.
        return False
    if tokens & _PROFESSIONAL_DOCTOR_TOKENS:
        return any(subject in text for subject in _ENTRY_DOCTORATE_SUBJECTS)
    return False


def classify_degree_level(name: str) -> Optional[DegreeLevel]:
    """Map a programme NAME onto one of the four levels, or None if unreadable.

    None is deliberate. A name this function cannot read is not a bachelors by
    default -- guessing here would fabricate a fact a student could act on, which
    is the exact failure mode C19 exists to remove elsewhere.
    """
    tokens = tokenize_degree_name(name)
    if not tokens:
        return None
    token_set = frozenset(tokens)
    text = " ".join(tokens)

    if _is_entry_level_doctorate(token_set, text):
        return DegreeLevel.BACHELORS

    for level, markers in _ORDERED_RULES:
        if token_set & markers:
            return level
    return None


def coerce_declared_level(declared: object) -> Optional[DegreeLevel]:
    """Map whatever sits in a degree_level FIELD onto a level, or None."""
    if isinstance(declared, DegreeLevel):
        return declared
    if not isinstance(declared, str):
        return None
    key = declared.strip().lower().replace(" ", "_").replace("-", "_")
    if key in _DECLARED_ALIASES:
        return _DECLARED_ALIASES[key]
    try:
        return DegreeLevel(key)
    except ValueError:
        return None


def resolve_degree_level(name: str, declared: object = None) -> Optional[DegreeLevel]:
    """Reconcile a programme name against its declared level. Name wins.

    The name is the degree title as the university published it; the declared
    level is the extractor's opinion about that title, and the extractor has
    been observed filing one physical-therapy doctorate as "undergraduate" and
    an identical one as "postgraduate_and_phd" in the same batch. Where the two
    disagree and the name is readable, the name is the better evidence.

    None when neither is readable -- the caller leaves the field alone rather
    than inventing a level.
    """
    return classify_degree_level(name) or coerce_declared_level(declared)


def apply_degree_level(prog: Dict[str, object]) -> Dict[str, object]:
    """Normalizer step: settle prog["degree_level"] onto the canonical four.

    Mutates and returns prog, matching apply_currency_and_tuition and
    apply_eligibility_defaults. A programme whose level cannot be resolved keeps
    whatever it had, so nothing is silently invented and the schema still gets
    its chance to reject it.
    """
    resolved = resolve_degree_level(str(prog.get("name") or ""), prog.get("degree_level"))
    if resolved is not None:
        prog["degree_level"] = resolved.value
    return prog
