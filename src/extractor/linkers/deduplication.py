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

def deduplicate_canonical_degree_links(scored_results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Eliminate redundant programme variations using an exact structured key.

    Group key is (degree_level, sorted discipline tokens) from get_discipline_tokens.
    Because the host is not part of the key, the same BS mirrored on seecs./mcs./
    ceme. subdomains collapses to one; because years are stopworded, fall-2024 and
    fall-2025-onward collapse to one, and the recency-weighted score picks the
    survivor. Because the match is exact rather than a similarity ratio, distinct
    programmes with near-identical slugs (electrical vs electronic engineering) are
    preserved. This is O(n) with a dict instead of the previous O(n^2) pairwise
    SequenceMatcher scan (~2M comparisons at 287 links, ~41M at 60 universities).
    """
    if not scored_results:
        return []

    logger.info(f"Running Post-Processing: Canonical Degree Deduplication on {len(scored_results)} links...")

    best_by_key: Dict[Tuple, Dict[str, str]] = {}
    passthrough: List[Dict[str, str]] = []

    for item in scored_results:
        # Only programme-bearing tiers are deduplicated. A faculty index page and
        # a contact page share no discipline tokens and must not be merged.
        if item["priority_tier_num"] not in (1, 2):
            passthrough.append(item)
            continue

        level, disciplines = get_discipline_tokens(item["href"], item.get("text", ""))
        if not disciplines:
            # No discipline signal at all -- not a programme page, keep as-is.
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
