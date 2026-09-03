"""
Normalizer entry points: the global facts registry and the payload walk.

Top of this package's dependency order.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.extractor.normalizers.currency_tuition import apply_currency_and_tuition
from src.extractor.normalizers.degree_names import apply_degree_level
from src.extractor.normalizers.eligibility import apply_eligibility_defaults
from src.utilities.schema import DegreeLevel

# Bucket names in ProgramCategoryBlock, derived from the enum so the two cannot
# drift apart: since C17 a bucket name IS its DegreeLevel value.
PROGRAM_BUCKETS = tuple(level.value for level in DegreeLevel)

# Pre-C17 bucket names, and where their contents belong now. "graduate" folds
# into masters because that is what it held -- MS/MPhil/MBA; the PGDs it should
# have held were never asked for before the diploma query existed.
RETIRED_PROGRAM_BUCKETS = {
    "undergraduate": DegreeLevel.BACHELORS.value,
    "graduate": DegreeLevel.MASTERS.value,
    "postgraduate_and_phd": DegreeLevel.PHD.value,
    "postgraduate_phd": DegreeLevel.PHD.value,
}

# Same registry entry as the pre-split module: logging.getLogger returns one
# object per name.
logger = logging.getLogger("UniversalNormalizer")

# Known global university facts registry. Two directories deeper than
# universal_normalizer.py, so parents[3] replaces parent.parent.
GLOBAL_FACTS_FILE = Path(__file__).resolve().parents[3] / "resources" / "rankings_global.json"
_GLOBAL_REGISTRY: Optional[Dict[str, Any]] = None


def load_global_registry() -> Dict[str, Any]:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is not None:
        return _GLOBAL_REGISTRY

    if GLOBAL_FACTS_FILE.exists():
        try:
            with open(GLOBAL_FACTS_FILE, "r", encoding="utf-8") as f:
                _GLOBAL_REGISTRY = json.load(f).get("universities", {})
                return _GLOBAL_REGISTRY
        except Exception as e:
            logger.warning(f"Could not load global registry {GLOBAL_FACTS_FILE}: {e}")

    _GLOBAL_REGISTRY = {}
    return _GLOBAL_REGISTRY


def normalize_universal_program(prog: Dict[str, Any], country: str) -> Dict[str, Any]:
    """
    Applies universal normalization rules to a single program dictionary.

    C16 split the body into two named steps. Order and effects are unchanged: the
    currency/tuition step ran first and the eligibility step second, and neither
    reads a value the other writes.

    C17 added a third, apply_degree_level, which settles degree_level onto the
    four canonical levels using the programme name as the stronger evidence. It
    reads nothing the other two write either, so it is ordered last only for
    readability.
    """
    prog = apply_currency_and_tuition(prog, country)
    prog = apply_eligibility_defaults(prog, country)
    prog = apply_degree_level(prog)
    return prog


def normalize_universal_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Applies universal schema normalization across the entire record dictionary.
    """
    registry = load_global_registry()
    main = record.get("main_info", {})
    uni_name = main.get("name", "")
    website = main.get("website", "")
    
    # Extract domain for registry lookup
    domain = ""
    if website:
        domain = website.replace("https://", "").replace("http://", "").split("/")[0].lower()
        if domain.startswith("www."):
            domain = domain[4:]

    # Apply Registry Facts if available
    reg_fact = registry.get(domain, {})
    if reg_fact:
        if not main.get("established_year") and reg_fact.get("established_year"):
            main["established_year"] = reg_fact["established_year"]
        if not main.get("accreditation_body") and reg_fact.get("accreditation_body"):
            main["accreditation_body"] = reg_fact["accreditation_body"]
        if not main.get("city") and reg_fact.get("city"):
            main["city"] = reg_fact["city"]
        if reg_fact.get("rankings") and not main.get("rankings"):
            main["rankings"] = reg_fact["rankings"]

    # Fallbacks for Main Identity
    country = main.get("country") or "Pakistan"
    if not main.get("primary_instruction_language"):
        main["primary_instruction_language"] = "German / English" if country.lower() == "germany" else "English"

    if not main.get("established_year"):
        if domain == "lmu.de" or "lmu" in uni_name.lower():
            main["established_year"] = 1472
            main["accreditation_body"] = "Bavarian State Ministry of Science and the Arts"
        elif "itu" in uni_name.lower():
            main["established_year"] = 2012
            main["accreditation_body"] = "Higher Education Commission (HEC)"
        elif "nust" in uni_name.lower():
            main["established_year"] = 1991
            main["accreditation_body"] = "HEC / PEC"

    if not main.get("accreditation_body"):
        main["accreditation_body"] = f"Ministry of Higher Education ({country})"

    # Normalize Programs
    #
    # Retired bucket names are folded into the canonical four first (C17).
    # Without this, every payload written before C17 keeps keys that
    # ProgramCategoryBlock no longer declares, and each of its readers -- the
    # inspector's audits, search, and CSV export -- silently sees zero
    # programmes for those universities.
    progs = record.get("programs", {})
    if isinstance(progs, dict) and "programs" in record:
        for retired, canonical in RETIRED_PROGRAM_BUCKETS.items():
            if retired in progs:
                # Merged, not assigned: a half-migrated record carrying both
                # names must not lose whichever list is written second.
                merged = list(progs.pop(retired) or [])
                progs[canonical] = list(progs.get(canonical) or []) + merged
        for bucket in PROGRAM_BUCKETS:
            progs.setdefault(bucket, [])
        record["programs"] = progs

    for cat_key in PROGRAM_BUCKETS:
        program_list = progs.get(cat_key, [])
        for idx in range(len(program_list)):
            program_list[idx] = normalize_universal_program(program_list[idx], country)

    record["main_info"] = main
    return record
