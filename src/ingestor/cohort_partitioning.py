"""
Degree-cohort partitioning for massive university link corpora (> 300 links).

Partitions finalized links into staged degree cohorts (Undergraduate, Masters,
Doctoral & Specialized) so that massive universities with hundreds of departmental
links can be ingested and queried without exceeding NotebookLM's 300-source platform
ceiling or overflowing RPC response buffers.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from src.extractor.linkers.constants import DEGREE_LEVEL_TOKENS, PATH_TOKEN_SPLIT_REGEX

logger = logging.getLogger("Ingest.cohort")


@dataclass
class DegreeCohort:
    """One staged ingestion cohort and the queries assigned to it."""
    name: str
    links: List[Dict[str, Any]]
    query_keys: Tuple[str, ...]
    evict_degree_sources: bool = True

    @property
    def source_count(self) -> int:
        return len(self.links)


def _matches_tokens(text: str, tokens: Set[str]) -> bool:
    """Check if normalized text contains any of the target tokens."""
    text_lower = text.lower()
    words = set(PATH_TOKEN_SPLIT_REGEX.split(text_lower))
    if words & tokens:
        return True
    return any(token in text_lower for token in tokens if "-" in token or "_" in token or " " in token)


def classify_link_degree_level(link: Dict[str, Any]) -> Optional[str]:
    """
    Classify a link's target degree level based on URL path and anchor text tokens.
    Returns 'bachelors', 'masters', 'phd', 'diploma', or None if unclassified/general.
    """
    url = link.get("url") or link.get("href") or ""
    text = link.get("anchor_text") or link.get("text") or ""
    path = urlparse(url).path

    combined = f"{path} {text}".lower()

    # Priority order: phd -> diploma -> masters -> bachelors
    if _matches_tokens(combined, DEGREE_LEVEL_TOKENS.get("phd", set())):
        return "phd"
    if _matches_tokens(combined, DEGREE_LEVEL_TOKENS.get("diploma", set())):
        return "diploma"
    if _matches_tokens(combined, DEGREE_LEVEL_TOKENS.get("masters", set())):
        return "masters"
    if _matches_tokens(combined, DEGREE_LEVEL_TOKENS.get("bachelors", set())):
        return "bachelors"

    return None


UG_ADMISSION_TOKENS: Set[str] = {
    "undergraduate", "firstyear", "first-year", "freshman", "transfer",
    "mitadmissions", "ug", "bachelor", "bachelors",
}

GRAD_ADMISSION_TOKENS: Set[str] = {
    "graduate", "grad", "oge", "postgraduate", "master", "masters", "phd", "doctoral",
}


def classify_admissions_degree_level(link: Dict[str, Any]) -> Optional[str]:
    """
    Classify Tier 2 admissions links into undergraduate, graduate, or None (general).
    """
    url = link.get("url") or link.get("href") or ""
    text = link.get("anchor_text") or link.get("text") or ""
    path = urlparse(url).path
    netloc = urlparse(url).netloc
    combined = f"{netloc} {path} {text}".lower()

    if _matches_tokens(combined, UG_ADMISSION_TOKENS):
        return "bachelors"
    if _matches_tokens(combined, GRAD_ADMISSION_TOKENS):
        return "graduate"
    return None


def partition_links_into_cohorts(
    links: List[Dict[str, Any]],
    cohort_cap: int = 200,
) -> List[DegreeCohort]:
    """
    Partition links into staged degree cohorts for upload and query execution.

    - If total links <= 75: Returns a single unified cohort (single-pass).
    - If total links > 75: Partitions degree links and degree-specific admissions
      into staged cohorts (Undergraduate -> Masters -> Doctoral/Faculties), while
      retaining general baseline anchors (contacts, tuition, portals) across all stages.
    """
    if not links:
        return []

    # If within low-link threshold, run in standard fast single-pass
    if len(links) <= 75:
        return [
            DegreeCohort(
                name="unified",
                links=links,
                query_keys=("main_info_contact", "bachelors", "masters", "phd", "diploma", "faculties"),
                evict_degree_sources=False,
            )
        ]

    general_baseline: List[Dict[str, Any]] = []
    ug_admissions: List[Dict[str, Any]] = []
    grad_admissions: List[Dict[str, Any]] = []
    tier3_faculties: List[Dict[str, Any]] = []
    bachelors_links: List[Dict[str, Any]] = []
    masters_links: List[Dict[str, Any]] = []
    phd_links: List[Dict[str, Any]] = []
    diploma_links: List[Dict[str, Any]] = []
    general_tier1: List[Dict[str, Any]] = []

    for link in links:
        tier = int(link.get("tier", 1) or 1)
        if tier == 4:
            general_baseline.append(link)
        elif tier == 2:
            adm_level = classify_admissions_degree_level(link)
            if adm_level == "bachelors":
                ug_admissions.append(link)
            elif adm_level == "graduate":
                grad_admissions.append(link)
            else:
                general_baseline.append(link)
        elif tier == 3:
            tier3_faculties.append(link)
        elif tier == 1:
            level = classify_link_degree_level(link)
            if level == "bachelors":
                bachelors_links.append(link)
            elif level == "masters":
                masters_links.append(link)
            elif level == "phd":
                phd_links.append(link)
            elif level == "diploma":
                diploma_links.append(link)
            else:
                general_tier1.append(link)
        else:
            general_baseline.append(link)

    # Distribute general tier 1 links to bachelors and masters if under capacity
    for link in general_tier1:
        if len(bachelors_links) <= len(masters_links):
            bachelors_links.append(link)
        else:
            masters_links.append(link)

    # If links are heavily concentrated in only ONE degree level, single-pass suffices
    distinct_levels = sum(bool(x) for x in (bachelors_links, masters_links, phd_links, diploma_links))
    if distinct_levels <= 1 and len(links) <= 250:
        return [
            DegreeCohort(
                name="unified",
                links=links,
                query_keys=("main_info_contact", "bachelors", "masters", "phd", "diploma", "faculties"),
                evict_degree_sources=False,
            )
        ]

    logger.info(
        f"Partitioning {len(links)} finalized links into staged degree cohorts (cap={cohort_cap} per cohort)..."
    )

    cohorts: List[DegreeCohort] = []

    # 1. Undergraduate Cohort
    if bachelors_links or ug_admissions:
        ug_sources = general_baseline + ug_admissions + bachelors_links
        if len(ug_sources) > cohort_cap:
            ug_base = general_baseline + ug_admissions
            ug_sources = ug_base + bachelors_links[: max(0, cohort_cap - len(ug_base))]
        cohorts.append(
            DegreeCohort(
                name="undergraduate",
                links=ug_sources,
                query_keys=("main_info_contact", "bachelors"),
                evict_degree_sources=True,
            )
        )

    # 2. Masters Cohort(s)
    if masters_links:
        grad_base = general_baseline + grad_admissions
        remaining_masters = masters_links[:]
        part = 1
        while remaining_masters:
            chunk = remaining_masters[: max(1, cohort_cap - len(grad_base))]
            remaining_masters = remaining_masters[len(chunk):]
            cohort_name = "masters" if part == 1 and not remaining_masters else f"masters_part{part}"
            cohorts.append(
                DegreeCohort(
                    name=cohort_name,
                    links=grad_base + chunk,
                    query_keys=("masters",),
                    evict_degree_sources=True,
                )
            )
            part += 1

    # 3. Doctoral & Specialized Cohort
    if phd_links or diploma_links or tier3_faculties:
        doc_base = general_baseline + grad_admissions
        doc_sources = doc_base + phd_links + diploma_links + tier3_faculties
        if len(doc_sources) > cohort_cap:
            doc_sources = (
                doc_base
                + phd_links[: max(0, cohort_cap - len(doc_base) - len(diploma_links) - len(tier3_faculties))]
                + diploma_links
                + tier3_faculties
            )
        cohorts.append(
            DegreeCohort(
                name="doctoral_specialized",
                links=doc_sources,
                query_keys=("phd", "diploma", "faculties"),
                evict_degree_sources=False,  # Last stage; notebook deleted immediately after
            )
        )

    # Fallback if no cohorts were populated
    if not cohorts:
        return [
            DegreeCohort(
                name="unified",
                links=links,
                query_keys=("main_info_contact", "bachelors", "masters", "phd", "diploma", "faculties"),
                evict_degree_sources=False,
            )
        ]

    logger.info(
        f"Generated {len(cohorts)} staged cohorts: "
        + ", ".join(f"{c.name} ({c.source_count} sources)" for c in cohorts)
    )
    return cohorts
