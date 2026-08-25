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

    state_db_path: Path = BASE_DIR / "data" / "state.sqlite"                     # SQLite pipeline state; authoritative for per-university progress and the daily query ledger
    output_jsonl_path: Path = BASE_DIR / "data" / "outputs" / "all_uni_outputs" / "universities_crawling_data.jsonl"  # Append-only ledger; one record per university as it completes
    output_master_json_path: Path = BASE_DIR / "data" / "outputs" / "all_uni_outputs" / "universities_crawling_data.json"  # Compiled array form of the ledger; the file downstream consumers read

    resources_dir: Path = BASE_DIR / "resources"                                 # Static inputs and planning documents
    resources_plans_dir: Path = BASE_DIR / "resources" / "plans"                 # Crawling and refactoring plans
    resources_analysis_dir: Path = BASE_DIR / "resources" / "analysis"           # Code, prompt and configuration analyses
    rankings_json_path: Path = BASE_DIR / "resources" / "rankings_pk.json"       # University identity/ranking registry; consolidated into rankings_global.json in C25

    tests_dir: Path = BASE_DIR / "tests"                                         # Test tree, mirroring the src/ package layout

    # ═══════════════════════════════════════════════════════════════════════
    # LOGGING
    # ═══════════════════════════════════════════════════════════════════════
    loggings_dir: Path = BASE_DIR / "loggings"                                   # Parent directory for all log output
    loggings_single_logs_dir: Path = BASE_DIR / "loggings" / "single_logs"       # s_{id}.json -- single/partial runs
    loggings_complete_logs_dir: Path = BASE_DIR / "loggings" / "complete_logs"   # c_{id}.json -- full batch runs
    notebook_lifecycle_log_path: Path = BASE_DIR / "loggings" / "notebook_lifecycle.log"   # Human-readable NotebookLM lifecycle trace
    notebook_audit_jsonl_path: Path = BASE_DIR / "loggings" / "notebook_audit.jsonl"       # Machine-readable NotebookLM audit trail; migrated to JSON logs in C26

    # ═══════════════════════════════════════════════════════════════════════
    # CRAWLING LIMITS & THRESHOLDS (Phase 1)
    # ═══════════════════════════════════════════════════════════════════════
    max_sources_per_notebook: int = 150      # Hard ceiling on sources per notebook; raise to ingest more links per university, at the cost of upload time
    dynamic_link_ratio: float = 0.45         # Fraction of clean candidate links actually sent; lower = fewer but higher-quality sources
    max_crawl_pages: int = 15                # Pages Crawl4AI visits per domain; raise for deeper discovery, costs proportionally more time

    # Proportional share of the source budget per priority tier. Flat top-N
    # slicing after a tier-major sort starves Tiers 3/4 entirely, which makes
    # the faculties and contact queries unanswerable.
    tier_quota_shares: Dict[int, float] = field(
        default_factory=lambda: {1: 0.45, 2: 0.30, 3: 0.15, 4: 0.10}
    )                                        # Per-tier slice of the source budget; shift weight toward 1/2 for programs, 3/4 for faculty and contact coverage

    # Calibrated against the prefixed + L2-normalised BGE distribution actually
    # produced by this pipeline. Measured on a live 473-link NUST crawl:
    # min 0.639, p25 0.696, median 0.713, max 0.875. The inherited 0.45 was a
    # dead knob under that distribution -- every single link cleared it, so the
    # threshold filtered nothing and tier quotas were doing all the selection.
    # 0.68 trims roughly the bottom quartile while leaving quotas fillable.
    semantic_threshold: float = 0.68         # BGE cosine cutoff; raise to be stricter on relevance, lower to admit more links

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
    # HTTP CONNECTION POOL
    # One HTTP/2 client is reused for every pre-flight probe and text-fallback
    # fetch. Building an AsyncClient per URL paid a fresh TCP + TLS handshake
    # on every one of ~150 links per university.
    # ═══════════════════════════════════════════════════════════════════════
    http_timeout_sec: float = 10.0           # Total request timeout; raise for slow university servers
    http_connect_timeout_sec: float = 5.0    # Connection-establishment timeout; raise for distant or slow hosts
    http_max_connections: int = 50           # Pool ceiling on simultaneous connections
    http_max_keepalive_connections: int = 20 # Idle connections kept warm for reuse
    http_keepalive_expiry_sec: float = 30.0  # How long an idle connection survives before being closed
    http2_enabled: bool = True               # Use HTTP/2 multiplexing; disable only for servers that mis-negotiate it

    # ═══════════════════════════════════════════════════════════════════════
    # INGESTION (Phase 2)
    # ═══════════════════════════════════════════════════════════════════════
    concurrent_uploads: int = 2              # Parallel source uploads to NotebookLM; keep low to avoid write rate limits
    source_ready_timeout_sec: int = 600      # Give-up time waiting for a source to become queryable
    upload_max_retries: int = 3              # Retries per failing source before it is abandoned
    preflight_http_check: bool = True        # Probe each URL before uploading; disabling is faster but wastes notebook slots on dead links
    # Pre-flight is pure network wait, so it parallelises far wider than the
    # upload path, which is bounded by NotebookLM's own write rate limits.
    preflight_concurrency: int = 15          # Parallel pre-flight probes; safe to raise, it is network-bound not API-bound

    # ═══════════════════════════════════════════════════════════════════════
    # LINK HEALTH PRE-FLIGHT (plan section 6; consumed by C13)
    # Sample a subset of links before committing a full batch, so a university
    # whose pages NotebookLM cannot ingest is skipped instead of burning quota.
    # ═══════════════════════════════════════════════════════════════════════
    health_check_enabled: bool = True        # Master switch for pre-flight sampling; disabling sends every batch unchecked
    health_check_sample_ratio: float = 0.10  # Fraction of candidate links sampled; raise for a more confident verdict at higher cost
    health_check_min_sample: int = 5         # Floor on sample size, so small link sets are still meaningfully tested
    health_check_min_pass_ratio: float = 0.5 # Fraction of the sample that must succeed to proceed with the full batch

    # ═══════════════════════════════════════════════════════════════════════
    # SOURCE READINESS POLLING
    # Sources are all created within a few seconds of each other, so pollers
    # started in lockstep re-converge on the same instants for the whole run.
    # A randomised first interval spreads the fan-out permanently.
    # ═══════════════════════════════════════════════════════════════════════
    readiness_poll_concurrency: int = 10     # Sources polled concurrently for readiness
    readiness_initial_interval_sec: float = 1.5  # First poll delay, before backoff begins
    readiness_max_interval_sec: float = 5.0      # Ceiling on the backed-off poll interval
    readiness_backoff_factor: float = 1.5        # Multiplier applied to the interval after each miss
    readiness_jitter_ratio: float = 0.35         # Randomisation applied to each interval to prevent lockstep polling

    # ═══════════════════════════════════════════════════════════════════════
    # QUERY & EXTRACTION (Phase 3)
    # ═══════════════════════════════════════════════════════════════════════
    chat_timeout_sec: int = 180              # Seconds to wait for a NotebookLM response before timing out
    max_query_retries: int = 2               # Retries per failing query before the university is marked failed
    # Independent queries in the suite run concurrently. Kept low: the ceiling
    # here is NotebookLM's per-notebook chat rate limit, not our CPU.
    query_concurrency: int = 3               # Queries issued in parallel per notebook; raising it risks rate limiting
    # NotebookLM Pro daily ceiling. At 5 queries x 83 universities = 415, this
    # leaves 85 queries of retry headroom. C17 adds a 6th (diploma) query, which
    # must be re-derived against this budget before a full batch is run.
    daily_query_budget: int = 500            # Hard daily cap enforced by the state ledger; must match the real NotebookLM quota
    queries_per_university: int = 5          # Queries in the suite; reserved up-front per university, so it must match QUERY_SUITE

    # ═══════════════════════════════════════════════════════════════════════
    # EXTERNAL API KEYS
    # ═══════════════════════════════════════════════════════════════════════
    exa_api_key: str = field(default_factory=lambda: os.getenv("EXA_API_KEY", ""))                              # Exa web search key; enables fallback enrichment when NotebookLM data is incomplete
    pinecone_api_key: str = field(default_factory=lambda: os.getenv("PINECONE_API_KEY", ""))                    # Pinecone key; only needed for `export --format pinecone`
    pinecone_index_name: str = field(default_factory=lambda: os.getenv("PINECONE_INDEX_NAME", "education-counselor"))  # Target Pinecone index name

    # ═══════════════════════════════════════════════════════════════════════
    # EMBEDDING & VECTOR EXPORT
    # ═══════════════════════════════════════════════════════════════════════
    embedding_model_name: str = "BAAI/bge-base-en-v1.5"   # Sentence-transformer used for link scoring and vector export; changing it changes the vector dimension
    embedding_batch_size: int = 32                        # Texts per embedding forward pass; raise for throughput, costs GPU/CPU memory

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
