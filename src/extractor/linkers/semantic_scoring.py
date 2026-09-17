"""
Sentence-transformer relevance scoring, tier classification and proportional
quota allocation.

One embedding model is loaded lazily and reused for the whole batch.
"""

import gc

from sentence_transformers import SentenceTransformer
from typing import Dict, List, Optional
from urllib.parse import urlparse

from src.config import config

from src.extractor.linkers.constants import (
    ALL_COUNSELOR_KEYWORDS,
    DEMOTED_PATH_PATTERNS,
    GUARANTEED_PATH_PATTERNS,
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

# tier number -> (display name, tier weight), derived from PRIORITY_TIERS so a
# structurally re-tiered link is weighted like every other link in its new tier.
_TIER_BY_NUM: Dict[int, tuple] = {
    num: (name, weight) for name, (num, _kw, weight) in PRIORITY_TIERS.items()
}

# Where a structurally demoted link lands. Not excluded: a merit list is real
# institutional content, it simply must not occupy a slot the programme and fee
# queries read.
DEMOTION_TIER = 3


def apply_structural_tier_rules(url: str, tier_num: int) -> tuple:
    """
    Let the URL path override the tier the embedding chose.

    Returns (tier_num, reason) where reason is None when nothing was overridden.

    The embedding decides tier by cosine argmax over 23 keyword phrases, with no
    structural input at all, and that monopoly fails in both directions. A merit
    list reads like an admissions page because it IS about admissions, so it won
    Tier 2 thirteen times on one ITU run and occupied slots the fee query then
    found nothing in. Conversely ITU's /financial-assistance page matched a
    Tier-4 phrase and landed in Tier 4, which no programme query reads.

    Promotion is checked first and demotion second, so an explicitly demoted
    path wins: /merit-lists-2026/fee-structure is a merit list, not a fee page.
    """
    path = urlparse(url).path.lower()

    promoted = next((p for p in GUARANTEED_PATH_PATTERNS if p in path), None)
    demoted = next((p for p in DEMOTED_PATH_PATTERNS if p in path), None)

    if demoted:
        if tier_num < DEMOTION_TIER:
            return DEMOTION_TIER, f"demoted: path matches '{demoted}'"
        return tier_num, None

    if promoted and tier_num > 2:
        return 2, f"promoted: path matches '{promoted}'"

    return tier_num, None


def _score_document_links(documents: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Rank harvested PDFs without the embedding model.

    A PDF has no page text to embed -- `path_words` and the anchor text are all
    the evidence there is -- so putting it through the same cosine pass as an
    HTML page compares a filename against a paragraph and loses. They are pinned
    to config.document_source_tier instead and ranked among themselves on how
    strongly their path matches the fee/deadline patterns, which is the whole
    reason for ingesting them.

    Fee schedules and admission calendars are published as PDFs at most
    universities; the pipeline discarded every one of them before C32.
    """
    tier = getattr(config, "document_source_tier", 2)
    tier_name, tier_weight = _TIER_BY_NUM.get(tier, ("Tier 2: Fees & Admissions", 1.20))

    scored: List[Dict[str, str]] = []
    for link in documents:
        haystack = f"{link['href']} {link.get('text', '')} {link.get('path_words', '')}".lower()
        hits = sum(1 for p in GUARANTEED_PATH_PATTERNS if p in haystack)
        # Bounded so a document can rank alongside a good HTML page but never
        # above the best of them: it is unread evidence until NotebookLM opens it.
        base = min(0.90, 0.60 + 0.06 * hits)
        recency, year_tag = compute_year_decay_factor(haystack)
        scored.append({
            "href": link["href"],
            "text": link.get("text") or link.get("path_words") or link["href"],
            "raw_text": link.get("raw_text", link.get("text", "")),
            "matched_keyword": "document: fee/deadline PDF" if hits else "document",
            "raw_similarity_score": round(base, 4),
            "weighted_score": round(base * tier_weight * recency, 4),
            "category": tier_name,
            "priority_tier_num": tier,
            "year_tag": year_tag,
            "passed_threshold": True,
            "is_document": True,
        })

    scored.sort(key=lambda x: -x["weighted_score"])
    cap = getattr(config, "max_document_sources", 8)
    if len(scored) > cap:
        logger.info(f"Documents: capped {len(scored)} harvested PDFs to the best {cap}.")
        scored = scored[:cap]
    if scored:
        logger.info(f"Documents: admitted {len(scored)} PDF sources at Tier {tier} (bypassed BGE scoring).")
    return scored


def ensure_guaranteed_coverage(
    selected: List[Dict[str, str]],
    scored_links: List[Dict[str, str]],
    total_cap: int,
) -> List[Dict[str, str]]:
    """
    Guarantee the selection contains the pages that carry fees and deadlines.

    Tier-proportional quota allocation is a good default and a bad guarantee.
    It fills Tier 2 with whatever scored highest in Tier 2, and on a site whose
    admissions section is dominated by one page type, every Tier-2 slot can go
    to that type -- ITU's went to merit lists, and the single page that actually
    published the fee schedule ranked below the cut.

    For each pattern in GUARANTEED_PATH_PATTERNS this admits the best-scoring
    unselected match, displacing the weakest selected link that is NOT itself a
    guaranteed match. The selection size is unchanged; only its composition is.
    One page per pattern, not all of them: this is a floor, not a preference.

    `application_fee` and `application_deadlines` were answered for 0% of
    programmes on the run this exists to prevent, and the cause was that no
    ingested source contained either value.
    """
    if not selected or not scored_links:
        return selected

    chosen_ids = {id(i) for i in selected}
    candidates = [i for i in scored_links if id(i) not in chosen_ids]
    if not candidates:
        return selected

    def matched_patterns(item: Dict[str, str]) -> set:
        path = urlparse(item["href"]).path.lower()
        return {p for p in GUARANTEED_PATH_PATTERNS if p in path}

    covered: set = set()
    for item in selected:
        covered |= matched_patterns(item)

    promotions: List[Dict[str, str]] = []
    for pattern in GUARANTEED_PATH_PATTERNS:
        if pattern in covered:
            continue
        best = max(
            (c for c in candidates if pattern in matched_patterns(c)),
            key=lambda x: x["weighted_score"],
            default=None,
        )
        if best is None or id(best) in {id(p) for p in promotions}:
            continue
        promotions.append(best)
        covered |= matched_patterns(best)

    if not promotions:
        return selected

    # Displace the weakest links that are not themselves guaranteed matches, so
    # a promotion never evicts another promotion.
    evictable = sorted(
        (i for i in selected if not matched_patterns(i)),
        key=lambda x: x["weighted_score"],
    )
    n = min(len(promotions), len(evictable))
    if n < len(promotions):
        promotions = promotions[:n]
    evicted = {id(i) for i in evictable[:n]}

    result = [i for i in selected if id(i) not in evicted] + promotions
    result.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))
    logger.info(
        f"Guaranteed coverage: promoted {len(promotions)} fee/deadline/apply page(s) "
        f"into the selection: {[p['href'] for p in promotions]}"
    )
    return result[:total_cap] if total_cap > 0 else result


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
        logger.info(f"Loading SentenceTransformer ('{config.embedding_model_name}') [once per process]...")
        _EMBEDDING_MODEL = SentenceTransformer(config.embedding_model_name)
    return _EMBEDDING_MODEL


def _crawl_score_normaliser(links: List[Dict[str, str]]):
    """
    Build the per-link Crawl4AI boost factor for this batch.

    Min-maxed across the batch rather than used raw, because KeywordRelevanceScorer's
    scale depends on the keyword list and on how many of them a URL happens to
    contain -- it is meaningful as a ranking within one crawl and meaningless as
    an absolute number across crawls. A link the crawler never scored, and a batch
    where every link scored the same, both come back as 1.0: no opinion, no
    adjustment. The factor spans [1.0, 1.0 + config.crawl_score_weight].
    """
    weight = config.crawl_score_weight
    values = [
        float(l["crawl_score"]) for l in links
        if isinstance(l.get("crawl_score"), (int, float))
    ]
    if weight <= 0 or not values:
        return lambda link: 1.0

    low, high = min(values), max(values)
    span = high - low
    if span <= 0:
        return lambda link: 1.0

    def boost(link: Dict[str, str]) -> float:
        raw = link.get("crawl_score")
        if not isinstance(raw, (int, float)):
            return 1.0
        return 1.0 + weight * ((float(raw) - low) / span)

    return boost


def classify_and_score_links(
    links: List[Dict[str, str]],
    threshold: Optional[float] = None,
    uptodate: bool = True
) -> List[Dict[str, str]]:
    """
    Computes cosine similarity between clean link text representation and counselor keywords
    using the SentenceTransformer named by config.embedding_model_name.
    Applies 2026 recency weighting when uptodate=True (boosts 2025-2027, penalizes 2010-2023).
    Ranks links by Priority Tier and weighted similarity score. RAM-optimized.
    """
    if not links:
        return []

    # None means "the calibrated value", not "0.45". The old literal default
    # silently overrode config.semantic_threshold for every caller that did not
    # pass one, which was all of them.
    threshold = config.semantic_threshold if threshold is None else threshold

    # Documents carry no page text to embed and are ranked separately (C32).
    documents = [l for l in links if l.get("is_document")]
    links = [l for l in links if not l.get("is_document")]
    document_results = _score_document_links(documents) if documents else []

    if not links:
        return document_results

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
        link_texts, convert_to_tensor=True, normalize_embeddings=True,
        batch_size=config.embedding_batch_size
    )

    similarity_matrix = model.similarity(link_embeddings, keyword_embeddings)

    crawl_boost = _crawl_score_normaliser(links)

    scored_results = []
    n_retiered = 0
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

        # The URL path gets a vote the embedding cannot override (C32).
        overridden, reason = apply_structural_tier_rules(link["href"], tier_num)
        if reason:
            tier_num = overridden
            assigned_tier, tier_weight = _TIER_BY_NUM.get(tier_num, (assigned_tier, tier_weight))
            n_retiered += 1
            logger.debug(f"Structural tier override ({reason}): {link['href']} -> Tier {tier_num}")

        recency_factor = 1.00
        year_tag = "Current / Timeless"

        if uptodate:
            combined_str = f"{link['href']} {link['text']} {link['path_words']}"
            recency_factor, year_tag = compute_year_decay_factor(combined_str)

        weighted_score = round(
            max_score * tier_weight * recency_factor * crawl_boost(link), 4
        )

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
        f"{len(scored_results) - n_passed} retained as tier reserve"
        + (f"; {n_retiered} structurally re-tiered." if n_retiered else ".")
    )

    deduped_results = deduplicate_canonical_degree_links(scored_results)

    # Documents join after deduplication: their keys are filenames, and a
    # fee-structure PDF must never dedupe against the HTML fee page it mirrors --
    # the two are different sources and NotebookLM reads both.
    if document_results:
        deduped_results = deduped_results + document_results
        deduped_results.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))
    return deduped_results
