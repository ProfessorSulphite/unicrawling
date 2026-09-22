"""
Central Configuration Dataclass & Workspace Setup for Education Counselor System

Every field carries an inline comment stating what it controls and what changing it
does, per refactoring_plan.md section 2. Grouped by domain so a knob can be found by
what it affects rather than by when it was added.
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict

from src.utilities.loaders import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Populate os.environ before any field default_factory reads a key.
load_dotenv()


@dataclass
class Config:
    """Central configuration for paths, limits, and external service credentials."""

    # ═══════════════════════════════════════════════════════════════════════
    # PATHS
    # Everything derives from base_dir, so relocating the project moves the
    # whole tree. Only *data* paths belong here -- source-code directories were
    # removed in C10 because nothing read them and they duplicated the package
    # layout, which is already expressed by the imports.
    # ═══════════════════════════════════════════════════════════════════════
    base_dir: Path = BASE_DIR                                                    # Project root; every other path hangs off this
    data_dir: Path = BASE_DIR / "data"                                           # Parent for links/, outputs/ and state.sqlite
    data_links_dir: Path = BASE_DIR / "data" / "links"                           # Per-university JSONL link files from Phase 1
    data_outputs_dir: Path = BASE_DIR / "data" / "outputs"                       # Parent for the two output directories below
    outputs_uni_outputs_dir: Path = BASE_DIR / "data" / "outputs" / "uni_outputs"          # One validated JSON payload per university
    outputs_all_uni_outputs_dir: Path = BASE_DIR / "data" / "outputs" / "all_uni_outputs"  # Aggregated dataset across all universities

    state_db_path: Path = BASE_DIR / "data" / "state.sqlite"                     # SQLite pipeline state; authoritative for per-university progress and the daily request ledger
    output_jsonl_path: Path = BASE_DIR / "data" / "outputs" / "all_uni_outputs" / "universities_crawling_data.jsonl"  # Append-only ledger; one record per university as it completes
    output_master_json_path: Path = BASE_DIR / "data" / "outputs" / "all_uni_outputs" / "universities_crawling_data.json"  # Compiled array form of the ledger; the file downstream consumers read

    resources_dir: Path = BASE_DIR / "resources"                                 # Static inputs and planning documents
    resources_plans_dir: Path = BASE_DIR / "resources" / "plans"                 # Crawling and refactoring plans
    resources_analysis_dir: Path = BASE_DIR / "resources" / "analysis"           # Code, prompt and configuration analyses
    # C25 merged rankings_pk.json into this file: two registries with different
    # schemas, separate caches, and three domains in both. The pk one carried
    # rankings: [] for all 15 entries and the extractor assigned that over the
    # payload, so sourced QS ranks were written out empty on every run.
    rankings_json_path: Path = BASE_DIR / "resources" / "rankings_global.json"   # Sourced university identity and rankings registry, keyed by canonical domain

    tests_dir: Path = BASE_DIR / "tests"                                         # Test tree, mirroring the src/ package layout

    # Renamed in C23. The previous filename collided with this module's own
    # name: one file is the run's university list and per-run settings, the
    # other is the application's own configuration, and "the config" meant
    # either one depending on who was speaking.
    # Lives here rather than in the orchestrator so the inspector's `batch`
    # subcommand can default to it without importing the orchestrator at module
    # scope, which Finding 6 forbids.
    run_settings_path: Path = BASE_DIR / "run_settings.json"                     # Universities to crawl and per-run pipeline settings

    # ═══════════════════════════════════════════════════════════════════════
    # LOGGING
    # ═══════════════════════════════════════════════════════════════════════
    loggings_dir: Path = BASE_DIR / "loggings"                                   # Parent directory for all log output
    loggings_single_logs_dir: Path = BASE_DIR / "loggings" / "single_logs"       # s_{id}.json -- single/partial runs
    loggings_complete_logs_dir: Path = BASE_DIR / "loggings" / "complete_logs"   # c_{id}.json -- full batch runs

    # ═══════════════════════════════════════════════════════════════════════
    # CRAWLING LIMITS & THRESHOLDS (Phase 1)
    # ═══════════════════════════════════════════════════════════════════════
    max_links_per_university: int = 150      # Hard ceiling on links exported per university; raise to widen the corpus the engines read, at the cost of crawl time
    dynamic_link_ratio: float = 0.50         # Fraction of clean candidate links actually sent; lower = fewer but higher-quality sources
    # 15 pages at depth 2 reached little beyond the landing page's own menu: the
    # 2026-09-05 batch harvested 41 usable links for COMSATS and 36 for BNU, and
    # the sparse tiers were being filled from a candidate pool that barely had
    # any. Discovery is the cheapest stage in the pipeline -- it spends no
    # extraction quota -- so it is the right place to spend time.
    max_crawl_pages: int = 35                # Pages Crawl4AI visits per domain; raise for deeper discovery, costs proportionally more time
    crawl_max_depth: int = 3                 # Link hops Crawl4AI follows from the start URL; 2 rarely leaves the top-level menu, 4+ reaches leaf programme pages at a steep time cost
    # Dead links drop out of the corpus when the fetch fails, and the context the
    # engine reads simply ends up smaller. The reserve is exported alongside the
    # selection so those slots can be refilled from the next-best candidates.
    link_reserve_ratio: float = 0.35         # Extra ranked links exported beyond the selection, as a fraction of it, to backfill pre-flight casualties; 0 disables backfill
    # Sources must belong to the university being described. A live ITU run took
    # collegereadiness.collegeboard.org in as a Tier 2 source, so answers about
    # ITU's admissions were partly grounded in College Board's SAT pages.
    # Subdomains of the institution (application.itu.edu.pk) always count as
    # on-site; only a genuinely different registrable domain is dropped.
    restrict_links_to_university_domain: bool = True   # Drop harvested links outside the university's own registrable domain; disabling re-admits third-party pages as sources
    enable_department_hub_discovery: bool = True   # Automatically detect and crawl academic school, college, and department sub-sites
    max_department_hubs: int = 8             # Maximum number of departmental and school portals crawled per university domain
    department_crawl_max_pages: int = 10     # Page budget allocated for each detected department micro-crawl during fan-out
    department_crawl_max_depth: int = 2      # Maximum link hops followed inside each detected department hub micro-crawl


    # Proportional share of the source budget per priority tier. Flat top-N
    # slicing after a tier-major sort starves Tiers 3/4 entirely, which makes
    # the faculties and contact queries unanswerable.
    tier_quota_shares: Dict[int, float] = field(
        default_factory=lambda: {1: 0.48, 2: 0.30, 3: 0.13, 4: 0.09}
    )                                        # Per-tier slice of the source budget; shift weight toward 1/2 for programs, 3/4 for faculty and contact coverage

    # Calibrated against the prefixed + L2-normalised BGE distribution actually
    # produced by this pipeline. Measured on a live 473-link NUST crawl:
    # min 0.639, p25 0.696, median 0.713, max 0.875. The inherited 0.45 was a
    # dead knob under that distribution -- every single link cleared it, so the
    # threshold filtered nothing and tier quotas were doing all the selection.
    # 0.68 trims roughly the bottom quartile while leaving quotas fillable.
    semantic_threshold: float = 0.68         # BGE cosine cutoff; raise to be stricter on relevance, lower to admit more links
    # Crawl4AI scores every href it discovers against CRAWL4AI_SCORER_KEYWORDS,
    # and that score was harvested and then discarded by the filter. It is a
    # second, independent opinion on the same link -- keyword-and-structure based
    # where BGE is semantic -- so it breaks ties the embedding cannot: two links
    # whose anchor text reads alike but one of which sits under /admissions/.
    # Kept deliberately small; it ranks, it does not decide.
    crawl_score_weight: float = 0.15         # How much Crawl4AI's own link score adjusts the final ranking, as a max fractional boost; 0 ignores it entirely

    # ═══════════════════════════════════════════════════════════════════════
    # BROWSER POOL (Phase 1)
    # ═══════════════════════════════════════════════════════════════════════
    crawler_reuse_browser: bool = True       # Share one headless browser across all universities; disabling pays full startup cost per university
    crawler_headless: bool = True            # Run without a visible window; set False only to watch a crawl for debugging
    # Link discovery reads hrefs and anchor text only, so images, CSS and fonts
    # are pure memory cost. text_mode drops them at the network layer.
    crawler_text_mode: bool = True           # Skip images/CSS/fonts at the network layer; disable only if a site needs them to render links
    crawler_light_mode: bool = True          # Disable non-essential browser features for lower memory use
    # crawl4ai defaults max_pages_before_recycle to 0, i.e. never recycle -- the
    # page pool then grows unbounded for the whole life of the process. Recycling
    # caps resident DOM/JS heap regardless of how long a batch runs.
    crawler_memory_saving_mode: bool = True  # Recycle browser pages to bound memory; disable only when debugging a crawl
    crawler_max_pages_before_recycle: int = 30   # Pages before the browser is recycled; lower caps memory harder, raise to reduce restart overhead
    crawler_viewport_width: int = 1024       # Viewport width; affects which responsive links render
    crawler_viewport_height: int = 768       # Viewport height; affects which responsive links render
    # BAAI/bge-* are asymmetric retrieval models: the query side requires an
    # instruction prefix, the passage side must not have one.
    bge_query_prefix: str = "Represent this sentence for searching relevant passages: "   # Required query-side prefix; removing it silently degrades scoring

    # ═══════════════════════════════════════════════════════════════════════
    # QUERY & EXTRACTION (Phase 3)
    # ═══════════════════════════════════════════════════════════════════════
    # Bounds the whole of Phases 1-4 for one university: a wedged crawl, a page
    # fetch that never returns, an API call that hangs. No one university can
    # consume a batch window.
    university_timeout_sec: int = 4200       # Wall-clock ceiling for one university across all phases; exceeding it fails that university and the batch moves on
    # The daily request ledger in state_management. A direct-engine extraction
    # is two consolidated requests -- one for programmes, one for identity -- so
    # the budget is read in requests, not in universities.
    daily_query_budget: int = 500            # Daily request ceiling enforced by the state ledger; set it to the real per-day allowance of the engine in use
    queries_per_university: int = 2          # Requests one full extraction sends; must match what the engines actually issue

    # ═══════════════════════════════════════════════════════════════════════
    # AUDIT THRESHOLDS (plan section 5; consumed by inspector/auditor.py)
    # The gate on "Local DB -> Supabase push only AFTER inspector validation
    # passes". Knobs rather than constants so raising the bar is a recorded
    # config change and not an edit buried in the auditor.
    #
    # Starting values are deliberately reachable rather than aspirational: at a
    # 90% floor every real corpus fails on application_fee alone and the verdict
    # stops carrying information. Raise them as extraction improves.
    # ═══════════════════════════════════════════════════════════════════════
    audit_min_field_coverage: float = 0.60    # Floor each non-critical required field must clear on its own
    audit_min_overall_coverage: float = 0.70  # Floor for answered cells across the whole required-field grid

    # ═══════════════════════════════════════════════════════════════════════
    # DATA LIFECYCLE & TTL (Pillar 3)
    # ═══════════════════════════════════════════════════════════════════════
    default_intake_year: str = "2026"         # Academic intake cycle year tag used to invalidate stale annual program catalogs
    default_data_ttl_days: int = 180          # Days an extracted university payload remains valid before requiring re-extraction
    extraction_engine: str = "auto"           # Engine used for schema extraction: 'auto', 'deepseek', or 'gemini'

    # ═══════════════════════════════════════════════════════════════════════
    # EXTERNAL API KEYS
    # ═══════════════════════════════════════════════════════════════════════
    deepseek_api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))    # DeepSeek API credential used for fast direct extraction and financial normalization
    deepseek_model: str = "deepseek-flash"                                                       # DeepSeek-V4.1-Flash model offering 1M token context and 2500 concurrency
    deepseek_base_url: str = "https://api.deepseek.com"                                          # Standard OpenAI-compatible API base endpoint for DeepSeek services
    # Gemini. One key is the normal setup; several may be given comma-separated
    # and are rotated round-robin, which multiplies the per-minute allowance.
    # gemini_client also accepts GEMINI_API_KEY or GOOGLE_API_KEY, so no run
    # depends on a particular variable name or on having more than one key.
    gemini_api_keys: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEYS", "") or os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", ""))  # Gemini credential(s); one key, or several separated by commas
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "") or "gemini-3.6-flash")   # Gemini model used for schema extraction; override per account entitlement
    gemini_rpm_per_key: int = 15                                                                             # Free-tier requests per minute per key; requests are spaced to respect it across all configured keys
    exa_api_key: str = field(default_factory=lambda: os.getenv("EXA_API_KEY", ""))                            # Exa web search key; enables fallback enrichment when the extracted data is incomplete
    typesafe_api_key: str = field(default_factory=lambda: os.getenv("TYPESAFE_API_KEY", ""))                    # TypeSafe AI API key; enables Jev System One semantic decisions
    typesafe_model: str = "jev-latest"                                                                           # TypeSafe System One model identifier used for evaluation
    typesafe_enabled: bool = True                                                                                # Master toggle for Jev System One decisions; disabling falls back to deterministic rules
    typesafe_batch_size: int = 30                                                                                # Maximum questions or candidate links batched per System One speculative request
    # Supabase (C29). Absent by default: the push is opt-in, and inspector/sync.py
    # says exactly what is missing rather than failing obscurely. The SERVICE key
    # is a write credential -- it belongs in .env, never in run_settings.json.
    supabase_url: str = field(default_factory=lambda: os.getenv("SUPABASE_URL", ""))                            # Supabase project URL; the sync is skipped entirely when unset
    supabase_service_key: str = field(default_factory=lambda: os.getenv("SUPABASE_SERVICE_KEY", ""))            # Supabase service-role key; write access, keep out of version control

    # ═══════════════════════════════════════════════════════════════════════
    # EMBEDDING (Phase 1 semantic link scoring)
    #
    # These mirror the values classify_and_score_links() currently hardcodes.
    # Until C14 wires the scorer to read them they are documentation, not a knob --
    # test_embedding_config_matches_the_scorer pins them so the two cannot drift
    # apart silently. Note the model is bge-*small* (384-dim): the *base* value
    # previously sitting here was only ever read by the deleted vector exporter,
    # and never matched what the scorer actually loaded.
    # ═══════════════════════════════════════════════════════════════════════
    embedding_model_name: str = "BAAI/bge-small-en-v1.5"  # Sentence-transformer used to score link relevance; changing it changes vector dimension and invalidates semantic_threshold
    embedding_batch_size: int = 64                        # Links per embedding forward pass; raise for throughput, costs GPU/CPU memory

    def ensure_directories(self) -> None:
        """Create every workspace directory the pipeline writes into."""
        for path in [
            self.data_links_dir,
            self.data_outputs_dir,
            self.outputs_uni_outputs_dir,
            self.outputs_all_uni_outputs_dir,
            self.resources_dir,
            self.resources_plans_dir,
            self.resources_analysis_dir,
            self.loggings_dir,
            self.loggings_single_logs_dir,
            self.loggings_complete_logs_dir,
            self.tests_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def tier_quotas(self, total: int) -> Dict[int, int]:
        """Resolve fractional tier shares into integer link counts summing to `total`."""
        quotas = {t: int(total * share) for t, share in self.tier_quota_shares.items()}
        # Hand any rounding remainder to the highest-value tier.
        remainder = total - sum(quotas.values())
        if remainder > 0:
            quotas[min(quotas)] += remainder
        return quotas


# Default global config instance
config = Config()
config.ensure_directories()
