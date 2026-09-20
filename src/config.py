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
    notebook_lifecycle_log_path: Path = BASE_DIR / "loggings" / "notebook_lifecycle.log"   # Human-readable NotebookLM lifecycle trace
    notebook_logs_dir: Path = BASE_DIR / "loggings" / "notebook_logs"                      # One JSON audit document per notebook (C26)
    # Legacy single-file audit trail, replaced by notebook_logs_dir in C26. Kept
    # so the migration can find it; nothing writes here any more.
    notebook_audit_jsonl_path: Path = BASE_DIR / "loggings" / "notebook_audit.jsonl"       # DEPRECATED: pre-C26 append-only audit stream, migration input only

    # ═══════════════════════════════════════════════════════════════════════
    # CRAWLING LIMITS & THRESHOLDS (Phase 1)
    # ═══════════════════════════════════════════════════════════════════════
    max_sources_per_notebook: int = 150      # Hard ceiling on sources per notebook; raise to ingest more links per university, at the cost of upload time
    dynamic_link_ratio: float = 0.50         # Fraction of clean candidate links actually sent; lower = fewer but higher-quality sources
    # 15 pages at depth 2 reached little beyond the landing page's own menu: the
    # 2026-09-05 batch harvested 41 usable links for COMSATS and 36 for BNU, and
    # the sparse tiers were being filled from a candidate pool that barely had
    # any. Discovery is the cheapest stage in the pipeline -- it spends no
    # NotebookLM quota -- so it is the right place to spend time.
    max_crawl_pages: int = 35                # Pages Crawl4AI visits per domain; raise for deeper discovery, costs proportionally more time
    crawl_max_depth: int = 3                 # Link hops Crawl4AI follows from the start URL; 2 rarely leaves the top-level menu, 4+ reaches leaf programme pages at a steep time cost
    # Phase 2's pre-flight drops every link that no longer resolves, and the
    # notebook simply ended up smaller -- COMSATS ingested 41 of 80 selected
    # links. The reserve is exported alongside the selection so those slots can
    # be refilled from the next-best candidates instead of being lost.
    link_reserve_ratio: float = 0.35         # Extra ranked links exported beyond the selection, as a fraction of it, to backfill pre-flight casualties; 0 disables backfill
    # Sources must belong to the university being described. A live ITU notebook
    # ingested collegereadiness.collegeboard.org as a Tier 2 source, so answers
    # about ITU's admissions were partly grounded in College Board's SAT pages.
    # Subdomains of the institution (application.itu.edu.pk) always count as
    # on-site; only a genuinely different registrable domain is dropped.
    restrict_links_to_university_domain: bool = True   # Drop harvested links outside the university's own registrable domain; disabling re-admits third-party pages as sources

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
    preflight_concurrency: int = 24          # Parallel pre-flight probes; safe to raise, it is network-bound not API-bound
    # The probe carried its own literal 5.0s deadline while the pooled client was
    # built for 10.0s, so a merely slow university failed a check the pool would
    # have survived. NUST scored 0/8 on 2026-09-05 and was skipped for the batch.
    preflight_probe_timeout_sec: float = 12.0    # Per-URL deadline for a reachability probe; raise for slow or distant university servers

    # ═══════════════════════════════════════════════════════════════════════
    # LINK HEALTH PRE-FLIGHT (plan section 6; consumed by C13)
    # Sample a subset of links before committing a full batch, so a university
    # whose pages NotebookLM cannot ingest is skipped instead of burning quota.
    # ═══════════════════════════════════════════════════════════════════════
    health_check_enabled: bool = True        # Master switch for pre-flight sampling; disabling sends every batch unchecked
    health_check_sample_ratio: float = 0.10  # Fraction of candidate links sampled; raise for a more confident verdict at higher cost
    health_check_min_sample: int = 5         # Floor on sample size, so small link sets are still meaningfully tested
    health_check_min_pass_ratio: float = 0.5 # Fraction of the sample that must succeed to proceed with the full batch
    # A whole university is abandoned on this verdict, so a single bad minute on
    # the network must not decide it. Failures are probed once more, patiently,
    # before the batch is refused.
    health_check_recheck_failures: bool = True   # Re-probe failed sample links once with a longer deadline before condemning a university; disabling makes the first verdict final
    health_check_recheck_timeout_sec: float = 25.0   # Deadline for that second, patient probe; only paid for links that already failed

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
    # Wired into _ask() in C31. It was declared here and read by nothing: the
    # chat.ask await had no deadline at all, and on 2026-09-05 a single COMSATS
    # call hung for 7h11m, taking the remaining 12 universities of the batch with
    # it. Every ask now runs under this deadline.
    chat_timeout_sec: int = 180              # Seconds to wait for a NotebookLM response before timing out; a stalled ask fails and retries instead of hanging the batch
    # Bounds the whole of Phases 1-4 for one university. chat_timeout_sec bounds a
    # single ask; this bounds everything else -- a wedged crawl, a stuck upload, a
    # readiness poll that never converges -- so no one university can consume a
    # batch window. Sized for the worst observed healthy university (AKU, 51 min).
    university_timeout_sec: int = 4200       # Wall-clock ceiling for one university across all phases; exceeding it fails that university and the batch moves on
    max_query_retries: int = 2               # Retries per failing query before the university is marked failed
    # An oversized response (RPCResponseTooLargeError, 50 MB ceiling) is not
    # fixed by re-asking: the answer size tracks how much corpus the question is
    # pointed at. The query is instead re-asked over halves of its source set and
    # the answers merged. Each level doubles the sub-asks, so this trades daily
    # query budget for coverage -- 2 allows at most 4 narrowed asks per query.
    max_query_split_depth: int = 3           # How many times an oversized query may be halved over its sources; 0 disables narrowing
    # NOT the query suite. The suite against one notebook is serial and must stay
    # that way: concurrent unkeyed asks share a conversation, and an ask still
    # waiting when a later ask's turn lands returns THAT turn's answer. Observed
    # live on 2026-09-03 -- `bachelors` and `phd` came back byte-identical and the
    # PhD programmes were filed as bachelors, with no error raised. This knob now
    # governs notebook-level parallelism only, where no conversation is shared.
    query_concurrency: int = 3               # Notebooks queried in parallel; the per-notebook suite is always serial
    # NotebookLM Pro daily ceiling. C17 added the 6th (diploma) query, so the
    # arithmetic is now 6 x 83 universities = 498 -- which clears the cap by two
    # queries and leaves no retry headroom at all. A full 83-university batch
    # therefore no longer fits in one day: at 6 queries the budget covers 83
    # universities only if nothing is ever retried, and 75 with the same ~10%
    # retry headroom the 5-query suite had. Batch sizing, not this number, is
    # what has to give -- daily_query_budget is a real external quota, not a knob.
    daily_query_budget: int = 500            # Hard daily cap enforced by the state ledger; must match the real NotebookLM quota
    queries_per_university: int = 6          # Queries in the suite; reserved up-front per university, so it must match QUERY_SUITE

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
    # EXTERNAL API KEYS
    # ═══════════════════════════════════════════════════════════════════════
    exa_api_key: str = field(default_factory=lambda: os.getenv("EXA_API_KEY", ""))                              # Exa web search key; enables fallback enrichment when NotebookLM data is incomplete
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
            self.notebook_logs_dir,
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
