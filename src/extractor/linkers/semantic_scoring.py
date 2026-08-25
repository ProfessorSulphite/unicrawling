"""
Sentence-transformer relevance scoring, tier classification and proportional
quota allocation.

One embedding model is loaded lazily and reused for the whole batch.
"""

import gc

from sentence_transformers import SentenceTransformer
from typing import Dict, List, Optional

from src.config import config

from src.extractor.linkers.constants import (
    ALL_COUNSELOR_KEYWORDS,
    PRIORITY_TIERS,
    RESERVE_FLOOR_RATIO,
    logger,
)
from src.extractor.linkers.deduplication import deduplicate_canonical_degree_links
from src.extractor.linkers.filteration import compute_year_decay_factor


def allocate_proportional_tier_quotas(
    scored_links: List[Dict[str, str]],
    total_cap: int,
    shares: Optional[Dict[int, float]] = None,
) -> List[Dict[str, str]]:
    """
    Select `total_cap` links with a guaranteed floor per priority tier.

    The previous `scored_links[:max_links]` ran after a tier-major sort, so Tier 1
    consumed the entire budget and Tiers 3 and 4 contributed zero sources. Phase 3
    then asked a notebook containing no faculty or contact pages to answer the
    faculties and contact queries. Unfilled tier quota is redistributed by score
    so a small site still fills its budget.
    """
    if total_cap <= 0 or not scored_links:
        return []

    shares = shares or config.tier_quota_shares
    quotas = {t: int(total_cap * s) for t, s in shares.items()}
    remainder = total_cap - sum(quotas.values())
    if remainder > 0 and quotas:
        quotas[min(quotas)] += remainder

    by_tier: Dict[int, List[Dict[str, str]]] = {t: [] for t in quotas}
    for item in scored_links:
        by_tier.setdefault(item["priority_tier_num"], []).append(item)
    for tier in by_tier:
        # Above-threshold links first, then by score. Items carry passed_threshold
        # from classify_and_score_links; absent key means "passed" (legacy callers).
        by_tier[tier].sort(key=lambda x: (not x.get("passed_threshold", True), -x["weighted_score"]))

    selected: List[Dict[str, str]] = []
    leftovers: List[Dict[str, str]] = []
    for tier, quota in sorted(quotas.items()):
        pool = by_tier.get(tier, [])
        # A tier fills its own quota from its own reserve before any cross-tier
        # redistribution. Otherwise a global threshold tuned for Tier 1 density
        # silently starves Tier 3: a live NUST run at threshold 0.68 left only 4
        # faculty sources of a 9 quota, and the faculties query degrades with it.
        selected.extend(pool[:quota])
        leftovers.extend(pool[quota:])

    # Redistribute any genuinely unused quota to the best remaining links, again
    # preferring above-threshold candidates over any tier's reserve.
    shortfall = total_cap - len(selected)
    if shortfall > 0:
        leftovers.sort(key=lambda x: (not x.get("passed_threshold", True), -x["weighted_score"]))
        selected.extend(leftovers[:shortfall])

    selected.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))
    filled = {t: sum(1 for i in selected if i["priority_tier_num"] == t) for t in sorted(quotas)}
    logger.info(f"Tier quota allocation (cap={total_cap}): {filled}")
    return selected

_EMBEDDING_MODEL: Optional[SentenceTransformer] = None


def _get_embedding_model() -> SentenceTransformer:
    """
    Process-wide lazy singleton for the sentence encoder.

    The previous code constructed SentenceTransformer inside the per-university
    scoring function, so an --hec batch of 83 universities paid the model load
    83 times. The weights are stateless across calls; one instance is correct.
    """
    global _EMBEDDING_MODEL
    if _EMBEDDING_MODEL is None:
        logger.info("Loading SentenceTransformer ('BAAI/bge-small-en-v1.5') [once per process]...")
        _EMBEDDING_MODEL = SentenceTransformer("BAAI/bge-small-en-v1.5")
    return _EMBEDDING_MODEL


def classify_and_score_links(
    links: List[Dict[str, str]],
    threshold: float = 0.45,
    uptodate: bool = True
) -> List[Dict[str, str]]:
    """
    Computes cosine similarity between clean link text representation and counselor keywords
    using SentenceTransformer ('BAAI/bge-small-en-v1.5').
    Applies 2026 recency weighting when uptodate=True (boosts 2025-2027, penalizes 2010-2023).
    Ranks links by Priority Tier and weighted similarity score. RAM-optimized.
    """
    if not links:
        return []

    model = _get_embedding_model()

    link_texts = [
        f"{link['text']} {link['path_words']}".strip() for link in links
    ]

    # BAAI/bge-* are asymmetric: the retrieval instruction goes on the QUERY side
    # only. Here the counselor keywords are the queries and the links are the
    # passages, so the prefix is applied to the keywords and never to the links.
    # Encoding both sides bare (the previous behaviour) collapses the score spread
    # the --threshold was tuned against.
    prefixed_keywords = [f"{config.bge_query_prefix}{k}" for k in ALL_COUNSELOR_KEYWORDS]

    logger.info(f"Encoding {len(ALL_COUNSELOR_KEYWORDS)} keywords and {len(link_texts)} links (uptodate={uptodate})...")
    keyword_embeddings = model.encode(prefixed_keywords, convert_to_tensor=True, normalize_embeddings=True)
    link_embeddings = model.encode(
        link_texts, convert_to_tensor=True, normalize_embeddings=True, batch_size=64
    )

    similarity_matrix = model.similarity(link_embeddings, keyword_embeddings)

    scored_results = []
    for idx, link in enumerate(links):
        scores = similarity_matrix[idx]
        max_score = float(scores.max())
        best_keyword_idx = int(scores.argmax())
        matched_keyword = ALL_COUNSELOR_KEYWORDS[best_keyword_idx]

        # Sub-threshold links are retained but flagged, not dropped. They form each
        # tier's reserve so that a threshold tuned for the dense programme tier
        # cannot starve the sparse faculties/contacts tiers. Anything below the
        # hard floor is genuine noise and is discarded outright.
        passed = max_score >= threshold
        if max_score < threshold * RESERVE_FLOOR_RATIO:
            continue

        assigned_tier = "Tier 4: FAQs & Contacts"
        tier_weight = 1.00
        tier_num = 4

        for tier_name, (t_num, t_keywords, t_weight) in PRIORITY_TIERS.items():
            if matched_keyword in t_keywords:
                assigned_tier = tier_name
                tier_num = t_num
                tier_weight = t_weight
                break

        recency_factor = 1.00
        year_tag = "Current / Timeless"

        if uptodate:
            combined_str = f"{link['href']} {link['text']} {link['path_words']}"
            recency_factor, year_tag = compute_year_decay_factor(combined_str)

        weighted_score = round(max_score * tier_weight * recency_factor, 4)

        scored_results.append({
            "href": link["href"],
            "text": link["text"],
            "raw_text": link["raw_text"],
            "matched_keyword": matched_keyword,
            "raw_similarity_score": round(max_score, 4),
            "weighted_score": weighted_score,
            "category": assigned_tier,
            "priority_tier_num": tier_num,
            "year_tag": year_tag,
            "passed_threshold": passed,
        })

    # The model itself is a process-wide singleton and is intentionally NOT freed:
    # reloading BAAI/bge-small-en-v1.5 per university cost ~3-5s x 83 universities
    # per batch run for no benefit. Only the per-run tensors are released.
    del keyword_embeddings, link_embeddings, similarity_matrix
    gc.collect()

    scored_results.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))
    n_passed = sum(1 for r in scored_results if r["passed_threshold"])
    logger.info(
        f"Scoring complete: {n_passed} links passed quality threshold ({threshold}); "
        f"{len(scored_results) - n_passed} retained as tier reserve."
    )

    deduped_results = deduplicate_canonical_degree_links(scored_results)
    return deduped_results
