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
from src.extractor.linkers.deduplication import (
    deduplicate_canonical_degree_links,
    deduplicate_canonical_degree_links_async,
)
from src.extractor.linkers.filteration import compute_year_decay_factor
from src.utilities.typesafe_client import evaluate_system_one, is_typesafe_available

try:
    from typesafe_sdk import Choice, Noul
except ImportError:
    Choice = None
    Noul = None


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
        f"{len(scored_results) - n_passed} retained as tier reserve."
    )

    deduped_results = deduplicate_canonical_degree_links(scored_results)
    return deduped_results


JEV_TIER_CRITERIA: Dict[str, str] = {
    "tier_1_programs": "Specific degree program curriculum, syllabus, degree requirements, or academic prospectus page",
    "tier_2_admissions": "General admissions policy, fee structures, deadlines, eligibility criteria, or online application portal",
    "tier_3_faculties": "Faculties, schools, departments, and constituent academic units directory",
    "tier_4_contacts": "Contact details, campus address, phone numbers, admissions office, or FAQs",
    "noise": "News articles, events, tenders, convocation galleries, job postings, staff portals, or login pages",
}

JEV_TIER_MAPPING = {
    "tier_1_programs": ("Tier 1: Bachelor & Master Programs", 1, 1.50),
    "tier_2_admissions": ("Tier 2: Admissions, Fees & Deadlines", 2, 1.30),
    "tier_3_faculties": ("Tier 3: Faculties & Departments", 3, 1.15),
    "tier_4_contacts": ("Tier 4: FAQs & Contacts", 4, 1.00),
}


async def classify_and_score_links_jev(
    links: List[Dict[str, str]],
    threshold: Optional[float] = None,
    uptodate: bool = True,
) -> List[Dict[str, str]]:
    """
    Score and classify links into Priority Tiers using Jev System One speculative fan-out.
    """
    if not links:
        return []

    threshold = config.semantic_threshold if threshold is None else threshold
    batch_size = max(5, config.typesafe_batch_size)
    crawl_boost = _crawl_score_normaliser(links)
    scored_results = []

    logger.info(f"Classifying {len(links)} links using Jev System One (batch_size={batch_size})...")

    for start_idx in range(0, len(links), batch_size):
        chunk = links[start_idx : start_idx + batch_size]
        state = {
            f"link_{i}": {
                "url": link.get("href", ""),
                "text": link.get("text", ""),
                "path_words": link.get("path_words", ""),
            }
            for i, link in enumerate(chunk)
        }

        questions = {}
        for i in range(len(chunk)):
            questions[f"tier_{i}"] = Choice(
                instructions=f"Which priority tier does `link_{i}` belong to?",
                criteria=JEV_TIER_CRITERIA,
            )
            questions[f"rel_{i}"] = Noul(
                instructions=f"Is `link_{i}` an academic degree, faculty, admissions, or contact page rather than administrative noise?",
            )

        response = await evaluate_system_one(state=state, questions=questions)
        for i, link in enumerate(chunk):
            tier_choice = "tier_4_contacts"
            rel_prob = 0.50

            if response and hasattr(response, "choices") and f"tier_{i}" in response.choices:
                tier_choice = str(response.choices[f"tier_{i}"].choice)
            if response and hasattr(response, "nouls") and f"rel_{i}" in response.nouls:
                rel_prob = float(response.nouls[f"rel_{i}"].noul)

            # Noise detection
            if tier_choice == "noise" or rel_prob < 0.20:
                tier_choice = "noise"
                rel_prob = min(rel_prob, 0.15)

            if tier_choice in JEV_TIER_MAPPING:
                assigned_tier, tier_num, tier_weight = JEV_TIER_MAPPING[tier_choice]
            else:
                assigned_tier, tier_num, tier_weight = ("Tier 4: FAQs & Contacts", 4, 1.00)

            recency_factor = 1.00
            year_tag = "Current / Timeless"
            if uptodate:
                combined_str = f"{link.get('href', '')} {link.get('text', '')} {link.get('path_words', '')}"
                recency_factor, year_tag = compute_year_decay_factor(combined_str)

            weighted_score = round(rel_prob * tier_weight * recency_factor * crawl_boost(link), 4)
            passed = rel_prob >= threshold

            # Discard hard floor noise
            if rel_prob < threshold * RESERVE_FLOOR_RATIO:
                continue

            scored_results.append({
                "href": link["href"],
                "text": link["text"],
                "raw_text": link.get("raw_text", link["text"]),
                "matched_keyword": tier_choice,
                "raw_similarity_score": round(rel_prob, 4),
                "weighted_score": weighted_score,
                "category": assigned_tier,
                "priority_tier_num": tier_num,
                "year_tag": year_tag,
                "passed_threshold": passed,
            })

    scored_results.sort(key=lambda x: (x["priority_tier_num"], -x["weighted_score"]))
    n_passed = sum(1 for r in scored_results if r["passed_threshold"])
    logger.info(
        f"Jev link scoring complete: {n_passed} links passed quality threshold ({threshold}); "
        f"{len(scored_results) - n_passed} retained as tier reserve."
    )

    deduped_results = await deduplicate_canonical_degree_links_async(scored_results)
    return deduped_results


async def classify_and_score_links_async(
    links: List[Dict[str, str]],
    threshold: Optional[float] = None,
    uptodate: bool = True,
) -> List[Dict[str, str]]:
    """
    Asynchronously score and classify links, preferring Jev System One when available.
    """
    if not links:
        return []
    if is_typesafe_available():
        try:
            return await classify_and_score_links_jev(links, threshold=threshold, uptodate=uptodate)
        except Exception as e:
            logger.warning(f"Jev link scoring failed ({e}); falling back to SentenceTransformer.")
    return classify_and_score_links(links, threshold=threshold, uptodate=uptodate)
