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


def partition_links_into_cohorts(
    links: List[Dict[str, Any]],
    cohort_cap: int = 250,
) -> List[DegreeCohort]:
    """
    Partition links into staged cohorts for upload and query execution.

    - If total links <= 300: Returns a single unified cohort (single-pass).
    - If total links > 300: Partitions Tier 1 degree links into staged cohorts
      while retaining Tier 2 (admissions) and Tier 4 (contact) baseline anchors
      across all stages.
    """
    if not links:
        return []

    # If within NotebookLM's platform limit of 300, run in standard single-pass
    if len(links) <= 300:
        return [
            DegreeCohort(
                name="unified",
                links=links,
                query_keys=("main_info_contact", "bachelors", "masters", "phd", "diploma", "faculties"),
                evict_degree_sources=False,
            )
        ]

    logger.info(
        f"Finalized links ({len(links)}) exceed 300-source platform ceiling. "
        f"Partitioning into staged degree cohorts (cap={cohort_cap} per cohort)..."
    )

    baseline: List[Dict[str, Any]] = []
    tier3_faculties: List[Dict[str, Any]] = []
    bachelors_links: List[Dict[str, Any]] = []
    masters_links: List[Dict[str, Any]] = []
    phd_links: List[Dict[str, Any]] = []
    diploma_links: List[Dict[str, Any]] = []
    general_tier1: List[Dict[str, Any]] = []

    for link in links:
        tier = int(link.get("tier", 1) or 1)
        if tier in (2, 4):
            baseline.append(link)
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
            baseline.append(link)

    # Distribute general tier 1 links to bachelors and masters if under capacity
    for link in general_tier1:
        if len(bachelors_links) <= len(masters_links):
            bachelors_links.append(link)
        else:
            masters_links.append(link)

    cohorts: List[DegreeCohort] = []

    # 1. Undergraduate Cohort
    ug_sources = baseline + bachelors_links
    if len(ug_sources) > cohort_cap:
        ug_sources = baseline + bachelors_links[: max(0, cohort_cap - len(baseline))]
    cohorts.append(
        DegreeCohort(
            name="undergraduate",
            links=ug_sources,
            query_keys=("main_info_contact", "bachelors"),
            evict_degree_sources=True,
        )
    )

    # 2. Masters Cohort(s)
    # If masters links alone are massive, chunk them to prevent 50MB RPC overflows
    remaining_masters = masters_links[:]
    part = 1
    while remaining_masters or part == 1:
        chunk = remaining_masters[: max(1, cohort_cap - len(baseline))]
        remaining_masters = remaining_masters[len(chunk):]
        cohort_name = "masters" if part == 1 and not remaining_masters else f"masters_part{part}"
        cohorts.append(
            DegreeCohort(
                name=cohort_name,
                links=baseline + chunk,
                query_keys=("masters",),
                evict_degree_sources=True,
            )
        )
        part += 1

    # 3. Doctoral & Specialized Cohort
    doc_sources = baseline + phd_links + diploma_links + tier3_faculties
    if len(doc_sources) > cohort_cap:
        doc_sources = (
            baseline
            + phd_links[: max(0, cohort_cap - len(baseline) - len(diploma_links) - len(tier3_faculties))]
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

    logger.info(
        f"Generated {len(cohorts)} staged cohorts: "
        + ", ".join(f"{c.name} ({c.source_count} sources)" for c in cohorts)
    )
    return cohorts
