"""
DEPRECATED compatibility shim -- extract_links.py was split into
src/extractor/linkers/ in C14.

  constants.py         exclusion rules, keyword tiers, compiled regexes, logging
  filteration.py       URL hygiene, tokenised exclusion, dedupe keys, year decay
  deduplication.py     canonical degree collapsing
  semantic_scoring.py  embedding model, tier classification, quota allocation
  crawling.py          Crawl4AI discovery and the shared browser pool
  runner.py            HEC discovery, the pipeline, exporters, CLI

Kept so existing call sites keep working while the refactor is in flight.
Removed in C24, once every import points at the new modules.

Deliberately NOT re-exported: _SHARED_CRAWLER, _SHARED_CRAWLER_LOOP and
_EMBEDDING_MODEL. `from X import name` binds by value, so a shim copy of a
rebindable global freezes at its initial None and silently diverges from the
real one -- the same trap that kept _logger_instance out of the notebook_logger
shim in C8.

Patching a name *on this module* is a no-op for the real callers; point
monkeypatch at the defining module instead. tests/test_shim_hygiene.py enforces this.
"""

from src.extractor.linkers.constants import (  # noqa: F401
    ALL_COUNSELOR_KEYWORDS,
    BASE_DIR,
    CRAWL4AI_SCORER_KEYWORDS,
    DEDUP_STOPWORDS,
    DEGREE_LEVEL_TOKENS,
    EXCLUDED_DOMAINS,
    EXCLUDED_EXTENSIONS,
    EXCLUDED_PATH_PHRASES,
    EXCLUDED_PATH_TOKENS,
    EXCLUDED_SCHEMES,
    GENERIC_LINK_TEXTS,
    INVISIBLE_CHARS_REGEX,
    LOG_DIR,
    LOG_FILE,
    MULTI_SLASH_REGEX,
    OPEN_ENDED_GRACE_YEARS,
    OPEN_ENDED_YEAR_REGEX,
    PATH_SEPARATOR_REGEX,
    PATH_TOKEN_SPLIT_REGEX,
    PRIORITY_TIERS,
    RESERVE_FLOOR_RATIO,
    SLUGIFY_REGEX,
    TIER1_PROGRAM_KEYWORDS,
    TIER2_ADMISSION_KEYWORDS,
    TIER3_FACULTY_KEYWORDS,
    TIER4_SERVICES_KEYWORDS,
    TRACKING_PARAMS,
    YEAR_SUFFIX_REGEX,
    YEAR_TOKEN_REGEX,
    logger,
)
from src.extractor.linkers.crawling import (  # noqa: F401
    CrawlFailure,
    _crawler_scope,
    browser_pool,
    build_browser_config,
    close_shared_crawler,
    crawl_site_links,
    get_shared_crawler,
)
from src.extractor.linkers.deduplication import (  # noqa: F401
    deduplicate_canonical_degree_links,
    get_discipline_tokens,
)
from src.extractor.linkers.filteration import (  # noqa: F401
    _tokenize_path,
    compute_year_decay_factor,
    dedupe_key,
    is_excluded_path,
    normalize_url,
    preprocess_and_filter_links,
    sanitize_url,
)
from src.extractor.linkers.runner import (  # noqa: F401
    HEC_RECOGNIZED_FALLBACK,
    export_dual_outputs,
    export_partitioned_links,
    extract_hec_universities,
    load_partitioned_links,
    main,
    run_pipeline,
    slugify_university,
    str2bool,
)
from src.extractor.linkers.semantic_scoring import (  # noqa: F401
    _get_embedding_model,
    allocate_proportional_tier_quotas,
    classify_and_score_links,
)

from src.config import config  # noqa: F401  -- tests reach the singleton via this module


if __name__ == "__main__":
    main()
