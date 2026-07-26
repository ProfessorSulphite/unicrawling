"""
Central Configuration Dataclass & Workspace Setup for Education Counselor System
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    """
    Minimal .env loader (no python-dotenv dependency).

    Existing environment variables win, so an explicitly exported key is never
    silently overridden by a stale file.
    """
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


@dataclass
class Config:
    """Central configuration for paths, limits, and external service credentials."""

    # ---- File Paths ----
    base_dir: Path = BASE_DIR
    data_links_dir: Path = BASE_DIR / "data" / "links"
    data_outputs_dir: Path = BASE_DIR / "data" / "outputs"
    uni_outputs_dir: Path = BASE_DIR / "data" / "outputs" / "uni_outputs"
    resources_dir: Path = BASE_DIR / "resources"
    loggings_dir: Path = BASE_DIR / "loggings"
    tests_dir: Path = BASE_DIR / "tests"
    state_db_path: Path = BASE_DIR / "data" / "state.sqlite"
    output_jsonl_path: Path = BASE_DIR / "data" / "outputs" / "university_counseling_data.jsonl"
    rankings_json_path: Path = BASE_DIR / "resources" / "rankings_pk.json"
    notebook_lifecycle_log_path: Path = BASE_DIR / "loggings" / "notebook_lifecycle.log"
    notebook_audit_jsonl_path: Path = BASE_DIR / "loggings" / "notebook_audit.jsonl"

    # ---- Phase 1: Crawl & Link Selection ----
    # Maximum ceiling cap on sources ingested per university (default: 150).
    # Actual source count is calculated dynamically as 45% of total clean candidate links.
    max_sources_per_notebook: int = 150
    dynamic_link_ratio: float = 0.45

    # Proportional share of the source budget per priority tier. Flat top-N
    # slicing after a tier-major sort starves Tiers 3/4 entirely, which makes
    # the faculties and contact queries unanswerable.
    tier_quota_shares: Dict[int, float] = field(
        default_factory=lambda: {1: 0.45, 2: 0.30, 3: 0.15, 4: 0.10}
    )
    # Calibrated against the prefixed + L2-normalised BGE distribution actually
    # produced by this pipeline. Measured on a live 473-link NUST crawl:
    # min 0.639, p25 0.696, median 0.713, max 0.875. The inherited 0.45 was a
    # dead knob under that distribution -- every single link cleared it, so the
    # threshold filtered nothing and tier quotas were doing all the selection.
    # 0.68 trims roughly the bottom quartile while leaving quotas fillable.
    semantic_threshold: float = 0.68
    max_crawl_pages: int = 15

    # ---- Phase 1: Browser pool & memory bounds ----
    # One headless browser is started once and reused for every university
    # instead of being launched and torn down 83 times per batch.
    crawler_reuse_browser: bool = True
    crawler_headless: bool = True
    # Link discovery reads hrefs and anchor text only, so images, CSS and fonts
    # are pure memory cost. text_mode drops them at the network layer.
    crawler_text_mode: bool = True
    crawler_light_mode: bool = True
    # crawl4ai defaults max_pages_before_recycle to 0, i.e. never recycle -- the
    # page pool then grows unbounded for the whole life of the process. Recycling
    # caps resident DOM/JS heap regardless of how long a batch runs.
    crawler_memory_saving_mode: bool = True
    crawler_max_pages_before_recycle: int = 30
    crawler_viewport_width: int = 1024
    crawler_viewport_height: int = 768
    # BAAI/bge-* are asymmetric retrieval models: the query side requires an
    # instruction prefix, the passage side must not have one.
    bge_query_prefix: str = "Represent this sentence for searching relevant passages: "

    # ---- Phase 2: Ingestion ----
    concurrent_uploads: int = 2
    source_ready_timeout_sec: int = 600
    upload_max_retries: int = 3
    preflight_http_check: bool = True
    # Pre-flight is pure network wait, so it parallelises far wider than the
    # upload path, which is bounded by NotebookLM's own write rate limits.
    preflight_concurrency: int = 15

    # ---- Shared HTTP connection pool ----
    # One HTTP/2 client is reused for every pre-flight probe and text-fallback
    # fetch. Building an AsyncClient per URL paid a fresh TCP + TLS handshake
    # on every one of ~150 links per university.
    http_timeout_sec: float = 10.0
    http_connect_timeout_sec: float = 5.0
    http_max_connections: int = 50
    http_max_keepalive_connections: int = 20
    http_keepalive_expiry_sec: float = 30.0
    http2_enabled: bool = True

    # ---- Source readiness polling ----
    # Sources are all created within a few seconds of each other, so pollers
    # started in lockstep re-converge on the same instants for the whole run.
    # A randomised first interval spreads the fan-out permanently.
    readiness_poll_concurrency: int = 10
    readiness_initial_interval_sec: float = 1.5
    readiness_max_interval_sec: float = 5.0
    readiness_backoff_factor: float = 1.5
    readiness_jitter_ratio: float = 0.35

    # ---- Phase 3: Query & Extraction ----
    chat_timeout_sec: int = 180
    max_query_retries: int = 2
    # Independent queries in the 5-query suite run concurrently. Kept low: the
    # ceiling here is NotebookLM's per-notebook chat rate limit, not our CPU.
    query_concurrency: int = 3
    # NotebookLM Pro daily ceiling. The 5-query suite x 83 universities = 415,
    # leaving 85 queries of retry headroom.
    daily_query_budget: int = 500
    queries_per_university: int = 5
    exa_api_key: str = field(default_factory=lambda: os.getenv("EXA_API_KEY", ""))
    pinecone_api_key: str = field(default_factory=lambda: os.getenv("PINECONE_API_KEY", ""))
    pinecone_index_name: str = field(default_factory=lambda: os.getenv("PINECONE_INDEX_NAME", "education-counselor"))
    qdrant_url: str = field(default_factory=lambda: os.getenv("QDRANT_URL", "http://localhost:6333"))
    qdrant_api_key: str = field(default_factory=lambda: os.getenv("QDRANT_API_KEY", ""))
    qdrant_collection_name: str = field(default_factory=lambda: os.getenv("QDRANT_COLLECTION_NAME", "education_counselor"))
    embedding_model_name: str = "BAAI/bge-base-en-v1.5"
    # Points per Qdrant upsert request and texts per embedding forward pass.
    qdrant_upsert_batch_size: int = 64
    embedding_batch_size: int = 32

    def ensure_directories(self) -> None:
        """Ensure all required workspace directories exist."""
        for path in [
            self.data_links_dir,
            self.data_outputs_dir,
            self.uni_outputs_dir,
            self.resources_dir,
            self.loggings_dir,
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
