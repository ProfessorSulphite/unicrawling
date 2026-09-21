"""
Canonical degree deduplication: collapses multi-campus mirrors and historical
intake-year variants of the same programme down to one best link.

get_discipline_tokens lives here rather than in semantic_scoring, where the
refactoring plan filed it, because deduplicate_canonical_degree_links is its only
caller -- and keeping it beside its caller is what breaks the import cycle the
plan's original assignment would have created.
"""

import asyncio
from typing import Dict, List, Tuple
from urllib.parse import urlparse

from src.extractor.linkers.constants import DEDUP_STOPWORDS, DEGREE_LEVEL_TOKENS, logger
from src.extractor.linkers.filteration import _tokenize_path
from src.utilities.typesafe_client import evaluate_noul, is_typesafe_available


def are_potential_duplicates(set_a: set, set_b: set) -> bool:
    """Heuristic to identify candidate duplicate pairs worth verifying with Jev Noul."""
    if not set_a or not set_b:
        return False
    # Direct overlap in discipline tokens (e.g. computer-engineering vs software-engineering)
    if set_a & set_b:
        return True
    # Abbreviation / prefix match (e.g. bio vs biological, stats vs statistics)
    for t_a in set_a:
        for t_b in set_b:
            if len(t_a) >= 3 and len(t_b) >= 3:
                if (
                    t_a.startswith(t_b)
                    or t_b.startswith(t_a)
                    or (len(t_a) >= 4 and len(t_b) >= 4 and t_a[:4] == t_b[:4])
                ):
                    return True
    # Acronym match (e.g. cs vs computer-science, ee vs electrical-engineering)
    def _check_acronym(short_set: set, long_set: set) -> bool:
        for short in short_set:
            if 2 <= len(short) <= 4:
                initials = "".join(w[0] for w in sorted(long_set))
                if all(c in initials for c in short):
                    return True
        return False

    return _check_acronym(set_a, set_b) or _check_acronym(set_b, set_a)


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
        # 1. Identify candidate duplicate pairs with matching tier, level, and discipline potential
        candidate_pairs: List[Tuple[int, int, Dict[str, str], Dict[str, str]]] = []
        tokens_by_idx = [
            get_discipline_tokens(item["href"], item.get("text", ""))
            for item in deduped_candidates
        ]

        for i in range(len(deduped_candidates)):
            level_a, disc_a = tokens_by_idx[i]
            set_a = set(disc_a)
            tier_a = deduped_candidates[i]["priority_tier_num"]

            for j in range(i + 1, len(deduped_candidates)):
                if deduped_candidates[j]["priority_tier_num"] != tier_a:
                    continue
                level_b, disc_b = tokens_by_idx[j]
                if level_a != level_b:
                    continue

                set_b = set(disc_b)
                if are_potential_duplicates(set_a, set_b):
                    candidate_pairs.append((i, j, deduped_candidates[i], deduped_candidates[j]))

        # 2. Evaluate all candidate pairs concurrently with Jev Noul
        if candidate_pairs:
            logger.debug(f"Evaluating {len(candidate_pairs)} candidate duplicate degree pairs concurrently with Jev Noul...")
            eval_results = await asyncio.gather(
                *(are_duplicate_degree_variants_jev(p[2], p[3]) for p in candidate_pairs),
                return_exceptions=True,
            )

            drop_indices = set()
            for (i, j, item_a, item_b), is_dup in zip(candidate_pairs, eval_results):
                if is_dup is True:
                    logger.debug(f"Jev aligned duplicate degree variants: '{item_a['href']}' and '{item_b['href']}'")
                    # Drop the lower-scoring variant
                    if item_a["weighted_score"] >= item_b["weighted_score"]:
                        drop_indices.add(j)
                    else:
                        drop_indices.add(i)

            deduped_candidates = [
                item for idx, item in enumerate(deduped_candidates)
                if idx not in drop_indices
            ]


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
