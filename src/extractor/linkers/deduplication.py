"""
Canonical degree deduplication: collapses multi-campus mirrors and historical
intake-year variants of the same programme down to one best link.

get_discipline_tokens lives here rather than in semantic_scoring, where the
refactoring plan filed it, because deduplicate_canonical_degree_links is its only
caller -- and keeping it beside its caller is what breaks the import cycle the
plan's original assignment would have created.
"""

from typing import Dict, List, Tuple
from urllib.parse import urlparse

from src.extractor.linkers.constants import DEDUP_STOPWORDS, DEGREE_LEVEL_TOKENS, logger
from src.extractor.linkers.filteration import _tokenize_path
from src.utilities.typesafe_client import evaluate_noul, is_typesafe_available


def get_discipline_tokens(url: str, text: str = "") -> Tuple[str, Tuple[str, ...]]:
    """
    Build an exact deduplication key: (degree_level, sorted discipline tokens).

    Replaces the previous SequenceMatcher(ratio > 0.88) fuzzy merge, which was
    both O(n^2) over every link pair and wrong: 'bs-electrical-engineering' and
    'bs-electronic-engineering' score ~0.90 similar and were silently merged into
    a single programme. Exact token-set equality keeps them separate, while still
    merging the same programme mirrored across campus subdomains (the host is not
    part of the key) and across intake years (years are stopworded out).
    """
    parsed = urlparse(url)
    tokens = _tokenize_path(parsed.path) + _tokenize_path(text)

    level = "unspecified"
    for lvl, markers in DEGREE_LEVEL_TOKENS.items():
        if any(t in markers for t in tokens):
            level = lvl
            break

    all_level_markers = set().union(*DEGREE_LEVEL_TOKENS.values())
    discipline = {
        t for t in tokens
        if t not in DEDUP_STOPWORDS
        and t not in all_level_markers
        and not t.isdigit()
        and len(t) > 1
    }
    return level, tuple(sorted(discipline))


async def are_duplicate_degree_variants_jev(item_a: Dict[str, str], item_b: Dict[str, str]) -> bool:
    """
    Use Jev Noul to determine if two ambiguous degree URLs refer to the exact same academic offering.
    """
    if not is_typesafe_available():
        return False

    instructions = (
        "Do these two university URLs/titles refer to the exact same academic degree program "
        "offering at the same level (e.g. BS Computer Science and BS CS, or multi-campus mirrors "
        "of the same program), rather than distinct specializations (e.g. Electrical Engineering vs Electronic Engineering) "
        "or distinct levels (e.g. BS vs MS)?"
    )
    state = {
        "url_a": item_a.get("href", ""),
        "title_a": item_a.get("text", ""),
        "url_b": item_b.get("href", ""),
        "title_b": item_b.get("text", ""),
    }

    try:
        prob = await evaluate_noul(state=state, instructions=instructions)
        return bool(prob is not None and prob >= 0.70)
    except Exception as exc:
        logger.warning(f"Jev duplicate resolution failed for {item_a.get('href')} vs {item_b.get('href')}: {exc}")
        return False


async def deduplicate_canonical_degree_links_async(scored_results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Async canonical deduplication using fast token hashing followed by Jev entity alignment.
    """
    if not scored_results:
        return []

    logger.info(f"Running Post-Processing: Canonical Degree Deduplication on {len(scored_results)} links...")

    best_by_key: Dict[Tuple, Dict[str, str]] = {}
    passthrough: List[Dict[str, str]] = []

    # Phase 1: Fast exact discipline token set matching (O(N))
    for item in scored_results:
        if item["priority_tier_num"] not in (1, 2):
            passthrough.append(item)
            continue

        level, disciplines = get_discipline_tokens(item["href"], item.get("text", ""))
        if not disciplines:
            passthrough.append(item)
            continue

        key = (item["priority_tier_num"], level, disciplines)
        incumbent = best_by_key.get(key)
        if incumbent is None:
            best_by_key[key] = item
        elif item["weighted_score"] > incumbent["weighted_score"]:
            logger.debug(f"Replaced duplicate degree variant: '{incumbent['href']}' -> '{item['href']}'")
            best_by_key[key] = item

    deduped_candidates = list(best_by_key.values())

    # Phase 2: Jev Noul Ambiguity Alignment (Tier B)
    if is_typesafe_available() and len(deduped_candidates) > 1:
        merged_candidates: List[Dict[str, str]] = []
        skip_indices = set()

        for i, item_a in enumerate(deduped_candidates):
            if i in skip_indices:
                continue
            survivor = item_a
            level_a, disc_a = get_discipline_tokens(item_a["href"], item_a.get("text", ""))
            set_a = set(disc_a)

            for j in range(i + 1, len(deduped_candidates)):
                if j in skip_indices:
                    continue
                item_b = deduped_candidates[j]
                if item_b["priority_tier_num"] != survivor["priority_tier_num"]:
                    continue
                level_b, disc_b = get_discipline_tokens(item_b["href"], item_b.get("text", ""))
                if level_a != level_b:
                    continue

                set_b = set(disc_b)
                # Check for ambiguity: shared tokens or short acronyms (e.g. 'cs' vs 'computer', 'science')
                has_overlap = bool(set_a & set_b)
                has_short_token = any(len(t) <= 3 for t in (set_a | set_b))
                if has_overlap or has_short_token:
                    is_duplicate = await are_duplicate_degree_variants_jev(survivor, item_b)
                    if is_duplicate:
                        logger.debug(f"Jev aligned duplicate degree variants: '{survivor['href']}' and '{item_b['href']}'")
                        skip_indices.add(j)
                        if item_b["weighted_score"] > survivor["weighted_score"]:
                            survivor = item_b

            merged_candidates.append(survivor)
        deduped_candidates = merged_candidates

    final_deduped = passthrough + deduped_candidates
    final_deduped.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))

    logger.info(f"Canonical Deduplication complete: Reduced {len(scored_results)} links to {len(final_deduped)} distinct, non-redundant degree links.")
    return final_deduped


def deduplicate_canonical_degree_links(scored_results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Eliminate redundant programme variations using an exact structured key.

    Synchronous entry point that falls back to exact token matching, or delegates
    to deduplicate_canonical_degree_links_async when safe.
    """
    if not scored_results:
        return []

    # If TypeSafe is available and we are outside an active event loop, run async flow
    if is_typesafe_available():
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            return asyncio.run(deduplicate_canonical_degree_links_async(scored_results))

    logger.info(f"Running Post-Processing: Canonical Degree Deduplication on {len(scored_results)} links...")

    best_by_key: Dict[Tuple, Dict[str, str]] = {}
    passthrough: List[Dict[str, str]] = []

    for item in scored_results:
        if item["priority_tier_num"] not in (1, 2):
            passthrough.append(item)
            continue

        level, disciplines = get_discipline_tokens(item["href"], item.get("text", ""))
        if not disciplines:
            passthrough.append(item)
            continue

        key = (item["priority_tier_num"], level, disciplines)
        incumbent = best_by_key.get(key)
        if incumbent is None:
            best_by_key[key] = item
        elif item["weighted_score"] > incumbent["weighted_score"]:
            logger.debug(f"Replaced duplicate degree variant: '{incumbent['href']}' -> '{item['href']}'")
            best_by_key[key] = item

    final_deduped = passthrough + list(best_by_key.values())
    final_deduped.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))

    logger.info(f"Canonical Deduplication complete: Reduced {len(scored_results)} links to {len(final_deduped)} distinct, non-redundant degree links.")
    return final_deduped
