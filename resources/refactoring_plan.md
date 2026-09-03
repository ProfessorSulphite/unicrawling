# Unicrawling Refactoring Plan — Analysis & Agent Directive

## Part 1: Analysis of Proposed Changes

### ✅ Strong Decisions

| # | Proposal | Verdict | Rationale |
|---|----------|---------|-----------|
| 1 | Modular `src/` split into `utilities/`, `extractor/`, `ingestor/`, `inspector/`, `logger/` | **Excellent** | The current `src/` is 13 flat files totaling ~250 KB. A monolith like `extract_links.py` (61 KB) and `inspect_cli.py` (52 KB) are unmaintainable. Domain-oriented packages are the right call. |
| 2 | Single `orchestrator.py` as pipeline entrypoint | **Excellent** | Replaces the current `pipeline.py` + `cli.py` split. One orchestration surface that imports from modules is cleaner and easier to debug. |
| 3 | Remove Qdrant entirely | **Correct** | `qdrant_validator.py` and `query_qdrant.py` exist but the pipeline doesn't actually depend on Qdrant for its core flow. Removing dead weight simplifies config and dependencies. |
| 4 | Supabase as final destination DB | **Good strategic choice** | Local-first collection → inspection → push-to-Supabase is a clean separation of concerns. |
| 5 | Structured log directories (`single_logs/` + `complete_logs/`) with IDs | **Good** | Enables "resume from log ID" which is critical for a pipeline that hits API rate limits. The `s_{id}` / `c_{id}` convention is simple and unambiguous. |
| 6 | Programs as primary data focus with explicit degree-level categories | **Correct** | Aligns with the counselor system's core purpose — matching students to programs, not just universities. |
| 7 | Graceful NotebookLM failure handling | **Critical fix** | The current pipeline can waste quota on dead notebooks. "Check before sending more" is the right policy. |

### ⚠️ Points That Need Clarification or Refinement

| # | Proposal | Concern | Recommendation |
|---|----------|---------|----------------|
| 1 | Keep JSONL alongside JSON | **Confirmed** | JSONL is retained. The agent MUST preserve both JSON and JSONL I/O utilities in `src/utilities/json_io.py`. |
| 2 | `config.py` already partially has the new paths (`src_utils_dir`, `src_extraction_dir` etc.) but also dead references (`self.uni_outputs_dir` in `ensure_directories` which doesn't match any field name) | **Bug in current code** | Must be fixed during refactor — `ensure_directories()` references a field that doesn't exist. |
| 3 | Naming inconsistency: proposal says `/utilities` then `/utils`, says `/extraction` then `/extractor` | **Resolved** | Standardized on: `utilities/`, `extractor/`, `ingestor/`, `inspector/`, `logger/`. All references in codebase and config MUST use these exact names. |
| 4 | `orchestrator.py` described as "the largest file" | **Anti-pattern risk** | It should be a *thin* orchestration layer that calls into modules. If it grows large, logic is leaking back out of modules. The agent should keep it lean. |
| 5 | Program categories list `Post-doctoral` | **Removed** | User confirmed: only 4 degree levels — Bachelors, Masters, PhD, Diploma. Post-doctoral is excluded entirely. |

### ❌ Gaps Not Addressed

| Gap | Impact | Action Needed |
|-----|--------|---------------|
| No migration strategy for existing data files | Existing `university_counseling_data.jsonl` and per-university JSONs could break | **No backward compatibility needed.** Old data can be discarded — this is a clean-slate refactoring. |
| No mention of `schema.py` destination | `schema.py` defines the university payload schema — where does it live? | Move to `src/utilities/schema.py` |
| No mention of `state.py` destination | `state.py` (19 KB) manages SQLite state — which module owns it? | Move to `src/utilities/state_management.py` |
| Test directory not addressed | `tests/` exists but no guidance on how tests map to new modules | Tests MUST mirror the new module structure (`test_utilities/`, `test_extractor/`, `test_ingestor/`, `test_inspector/`, `test_logger/`). Additionally, a `test_pipeline.py` suite MUST exist at the `tests/` root to test the full end-to-end pipeline orchestration. |

---

## Part 2: Formalized Agent Directive

> [!IMPORTANT]
> This section is the **canonical refactoring specification**. The implementing agent MUST follow this document as the source of truth for all structural changes.

---

### 1. Directory Structure — Target State

```
unicrawling/
├── AGENTS.md
├── README.md
├── CHANGELOG.md
├── COMMANDS.md
├── requirements.txt
├── run_settings.json                   # Runtime overrides (renamed from config.json to avoid clash with src/config.py)
├── university_payload_schema.json
├── .env.example
├── .gitignore
│
├── data/
│   ├── links/                          # Per-university JSONL link files
│   ├── outputs/
│   │   ├── uni_outputs/                # Per-university processed JSON
│   │   └── all_uni_outputs/            # Aggregated JSON (universities_crawling_data.json)
│   └── state.sqlite                    # Pipeline state DB
│
├── loggings/
│   ├── single_logs/                    # Partial/single-run logs (JSON format, s_{id}.json)
│   └── complete_logs/                  # Full pipeline run logs (JSON format, c_{id}.json)
│
├── resources/
│   ├── plans/
│   └── analysis/
│
├── src/
│   ├── __init__.py
│   ├── orchestrator.py                 # Pipeline entrypoint — thin orchestration only
│   ├── config.py                       # Refactored Config dataclass
│   │
│   ├── utilities/
│   │   ├── __init__.py
│   │   ├── loaders.py                  # load_dotenv() moved from config.py
│   │   ├── json_io.py                  # JSON + JSONL read/write helpers (JSONL KEPT)
│   │   ├── state_management.py         # SQLite state management (from state.py)
│   │   └── schema.py                   # Payload schema definitions (from schema.py)
│   │
│   ├── extractor/
│   │   ├── __init__.py
│   │   ├── linkers/
│   │   │   ├── __init__.py
│   │   │   ├── crawling.py             # Crawl4AI link discovery
│   │   │   ├── filteration.py          # Zero Garbage Policy filters
│   │   │   ├── deduplication.py        # Canonical degree dedup
│   │   │   ├── semantic_scoring.py     # BGE embedding scoring
│   │   │   └── runner.py               # Linkers orchestration
│   │   ├── crawlers/
│   │   │   ├── __init__.py
│   │   │   ├── notebook_querying.py    # NotebookLM query execution
│   │   │   ├── exa_enriching.py        # Exa API fallback search
│   │   │   ├── json_repairing.py       # JSON response repair
│   │   │   └── runner.py               # Crawlers orchestration
│   │   └── normalizers/
│   │       ├── __init__.py
│   │       ├── currency_tuition.py     # Currency & tuition normalization
│   │       ├── degree_names.py         # Standardize degree & program names
│   │       ├── eligibility.py          # Eligibility & requirement consolidation
│   │       └── runner.py               # Normalizers orchestration
│   │
│   ├── ingestor/
│   │   ├── __init__.py
│   │   ├── notebook_lifecycle.py       # Create & delete NotebookLM notebooks
│   │   ├── source_management.py        # Upload sources, manage url→source_id map
│   │   ├── quota_management.py         # Daily query budget tracking & reservation
│   │   └── readiness_polling.py        # Jittered backoff source readiness checks
│   │
│   ├── inspector/
│   │   ├── __init__.py
│   │   ├── dashboard.py               # CLI dashboard rendering
│   │   ├── auditor.py                  # Data quality audits (empty fields, coverage)
│   │   ├── analytics.py               # Stats: program counts, type distributions
│   │   └── sync.py                     # Supabase sync after validation
│   │
│   └── logger/
│       ├── __init__.py
│       ├── pipeline_logger.py          # Structured JSON logging for pipeline runs
│       └── notebook_logger.py          # NotebookLM-specific audit logging
│
└── tests/                              # Mirror src/ module structure
    ├── test_utilities/
    ├── test_extractor/
    ├── test_ingestor/
    ├── test_inspector/
    ├── test_logger/
    └── test_pipeline.py                # End-to-end pipeline integration tests
```

### 2. Config Refactoring Rules

The `Config` dataclass in [config.py](file:///home/huzaifayaqob/Desktop/unicrawling/src/config.py) MUST be reorganized as follows:

```python
@dataclass
class Config:
    """Central configuration — grouped by domain."""

    # ═══════════════════════════════════════════
    # PATHS
    # ═══════════════════════════════════════════
    base_dir: Path = ...          # Root project dir; all other paths derive from this
    data_dir: Path = ...          # Parent for links/, outputs/, state.sqlite
    # ... all path fields grouped here, each with an inline comment ...

    # ═══════════════════════════════════════════
    # CRAWLING LIMITS & THRESHOLDS
    # ═══════════════════════════════════════════
    max_sources_per_notebook: int = 150   # Hard ceiling on sources per NotebookLM notebook; raise to ingest more links per uni
    dynamic_link_ratio: float = 0.45      # Fraction of clean candidate links actually sent; lower = fewer but higher-quality sources
    semantic_threshold: float = 0.68      # BGE cosine cutoff; raise to be stricter on link relevance, lower to admit more links
    max_crawl_pages: int = 15             # Max pages Crawl4AI visits per domain; increase for deeper crawls, costs more time
    # ...

    # ═══════════════════════════════════════════
    # BROWSER POOL
    # ═══════════════════════════════════════════
    crawler_reuse_browser: bool = True    # If True, one headless browser is shared across all universities; saves startup cost
    # ... every variable MUST have an inline comment explaining its effect when changed ...

    # ═══════════════════════════════════════════
    # HTTP CONNECTION POOL
    # ═══════════════════════════════════════════
    http_timeout_sec: float = 10.0        # Total request timeout; increase for slow university servers
    # ...

    # ═══════════════════════════════════════════
    # INGESTION (Phase 2)
    # ═══════════════════════════════════════════
    concurrent_uploads: int = 2           # Parallel source uploads to NotebookLM; keep low to avoid rate limits
    # ...

    # ═══════════════════════════════════════════
    # QUERY & EXTRACTION (Phase 3)
    # ═══════════════════════════════════════════
    chat_timeout_sec: int = 180           # Seconds to wait for a NotebookLM query response before timing out
    # ...

    # ═══════════════════════════════════════════
    # EXTERNAL API KEYS (keep Exa, Pinecone; REMOVE Qdrant)
    # ═══════════════════════════════════════════
    exa_api_key: str = ...                # Exa web search API key; used for fallback enrichment when NotebookLM data is incomplete
    # ...

    # ═══════════════════════════════════════════
    # LOGGING
    # ═══════════════════════════════════════════
    loggings_dir: Path = ...              # Parent directory for all log files
    loggings_single_logs_dir: Path = ...  # Stores s_{id}.json logs for single/partial runs
    loggings_complete_logs_dir: Path = ...# Stores c_{id}.json logs for full pipeline runs
```

> [!IMPORTANT]
> **Every single variable** in the Config dataclass MUST have a short inline comment (`# ...`) explaining what it controls and what effect changing it has. No variable should be left uncommented.

**Specific changes:**
- **REMOVE** `_load_dotenv()` from `config.py` → move to `src/utilities/loaders.py` as `load_dotenv()`
- **REMOVE** all Qdrant fields: `qdrant_url`, `qdrant_api_key`, `qdrant_collection_name`, `qdrant_upsert_batch_size`
- **FIX** `ensure_directories()` — it references `self.uni_outputs_dir` which doesn't exist; should be `self.outputs_uni_outputs_dir`
- **ADD** `outputs_all_uni_outputs_dir` to `ensure_directories()`
- **ADD** `loggings_single_logs_dir` and `loggings_complete_logs_dir` to `ensure_directories()`

### 3. File Removal Checklist

| File | Action | Reason |
|------|--------|--------|
| [query_qdrant.py](file:///home/huzaifayaqob/Desktop/unicrawling/query_qdrant.py) | **DELETE** | Qdrant removed per Note #1 |
| [src/qdrant_validator.py](file:///home/huzaifayaqob/Desktop/unicrawling/src/qdrant_validator.py) | **DELETE** | Qdrant removed per Note #1 |
| [loggings/.gitkeep](file:///home/huzaifayaqob/Desktop/unicrawling/loggings/.gitkeep) | **DELETE** | Replaced by `single_logs/` and `complete_logs/` subdirectories |
| [resources/rankings_pk.json](file:///home/huzaifayaqob/Desktop/unicrawling/resources/rankings_pk.json) | **DELETE** | Pakistan-only ranking data; system now targets all universities worldwide |
| `src/pipeline.py` | **REPLACE** | Logic absorbed into `orchestrator.py` |
| `cli.py` | **REPLACE** | Entry point moves to `orchestrator.py` |
| [config.json](file:///home/huzaifayaqob/Desktop/unicrawling/config.json) | **RENAME** → `run_settings.json` | Avoids clash with `src/config.py`; also remove `sync_qdrant` key from `pipeline_settings` since Qdrant is gone |

### 4. Logging System Specification

- **Format**: JSON (not JSONL) per the user's explicit decision (Note #2)
- **Single run logs**: `loggings/single_logs/s_{id}.json` — auto-incrementing integer ID
- **Complete run logs**: `loggings/complete_logs/c_{id}.json` — auto-incrementing integer ID
- **Resume feature**: The pipeline MUST support `--resume s_42` or `--resume c_7` to pick up from a specific log checkpoint
- **Remove** the `.gitkeep` file from `loggings/`
- **Migrate** `notebook_audit.jsonl` content to the new logging structure under `logger/notebook_logger.py`

### 5. Data Priority & Schema Rules

> [!IMPORTANT]
> Programs are the PRIMARY data target. University general info is secondary.

**Degree-level categories** (every program MUST be classified into exactly one):
1. **Bachelors** — BS, BSc, BA, BBA, BE, B.Ed, etc.
2. **Masters** — MS, MSc, MA, MBA, MPhil, M.Ed, etc.
3. **PhD** — PhD, Doctorate
4. **Diploma** — PGD, Diploma, Certificate programs

**Per-program required fields**:
- Program name (standardized — degree names, program titles, and all essential identifiers MUST be normalized via `src/extractor/normalizers/degree_names.py`)
- Degree level (from categories above)
- Duration
- Tuition/fee (**kept in original currency as published by the university** — do NOT convert or normalize to any base currency)
- Eligibility criteria
- Admission requirements
- Application fee
- Application deadline(s)
- Program description (a full paragraph providing a comprehensive overview of the program — its focus areas, learning outcomes, career prospects, and any distinctive features or specializations that would help a student understand what the program offers and whether it aligns with their goals)

**Data quality rules**:
- All degree names, program names, and essential identifiers normalized to a canonical form
- All eligibility criteria consolidated into structured format
- Tuition and fees stored in their original currency as stated by the university
- Empty/NaN fields audited and reported by inspector
- Local DB → Supabase push only AFTER inspector validation passes

### 6. NotebookLM Error Handling Policy

> [!CAUTION]
> Do NOT blindly send all links to a notebook without verifying extraction health first. This wastes API quota.

**Required behavior:**
1. **Smart link health sampling**: Before committing the full batch of sources to a notebook, run a pre-flight health check on a **random sample of 10% of links (minimum 5 links)**. If the majority of sampled links fail to be accepted or extracted, do NOT proceed with the remaining links for that university — skip it and log the failure. If the sample passes, proceed with the full batch.
2. If a source fails during the full upload, isolate it — do not discard successful sources
3. If the entire notebook fails, skip that university and log the failure
4. Never retry a failed notebook more than `max_query_retries` times
5. Always reserve the full per-university query budget BEFORE starting queries (6 since C17 added the diploma query: `config.queries_per_university`, pinned to `len(QUERY_SUITE)` by test)
6. The health check logic should be configurable via `Config` (sample percentage, minimum sample size) so it can be tuned without code changes

### 7. Migration File Mapping

| Current File | → New Location | Notes |
|-------------|----------------|-------|
| `src/extract_links.py` | `src/extractor/linkers/` (split) | Split across `crawling.py`, `filteration.py`, `deduplication.py`, `semantic_scoring.py`, `runner.py` |
| `src/extract_data.py` | `src/extractor/crawlers/` (split) | Split across `notebook_querying.py`, `exa_enriching.py`, `json_repairing.py`, `runner.py` |
| `src/universal_normalizer.py` | `src/extractor/normalizers/` (split) | Split across `currency_tuition.py`, `degree_names.py`, `eligibility.py`, `runner.py` |
| `src/ingest.py` | `src/ingestor/` (split) | Split across `notebook_lifecycle.py`, `source_management.py`, `quota_management.py`, `readiness_polling.py` |
| `src/inspect_cli.py` | `src/inspector/` (split) | Split across `dashboard.py`, `auditor.py`, `analytics.py`, `sync.py` |
| `src/json_io.py` | `src/utilities/json_io.py` | Keep both JSON AND JSONL utilities |
| `src/state.py` | `src/utilities/state_management.py` | Direct move |
| `src/schema.py` | `src/utilities/schema.py` | Direct move |
| `src/notebook_logger.py` | `src/logger/notebook_logger.py` | Direct move |
| `src/pipeline.py` | `src/orchestrator.py` | Rewrite as thin orchestration layer |
| `src/config.py` | `src/config.py` | Refactor in-place per Section 2 |

### 8. Execution Order

The agent MUST execute the refactoring in this order:

1. **Create directory scaffolding** — all new directories with `__init__.py` files
2. **Move and refactor `config.py`** — remove Qdrant, fix bugs, add new paths, reorganize groups
3. **Create `src/utilities/loaders.py`** — extract `load_dotenv()` from config
4. **Move utility files** — `json_io.py`, `state.py`, `schema.py` → `src/utilities/`
5. **Split and move extractor files** — `extract_links.py`, `extract_data.py`, `universal_normalizer.py`
6. **Split and move ingestor files** — `ingest.py`
7. **Split and move inspector files** — `inspect_cli.py`
8. **Move logger files** — `notebook_logger.py`, create `pipeline_logger.py`
9. **Create `orchestrator.py`** — import from all modules, replace `pipeline.py`
10. **Set up logging system** — `single_logs/`, `complete_logs/`, ID assignment, resume feature
11. **Delete deprecated files** — Qdrant files, `.gitkeep`, old flat files
12. **Update imports** across entire codebase
13. **Update `AGENTS.md`** to reflect new structure
14. **Run tests** to verify nothing is broken

> [!WARNING]
> This is a large refactoring. Each step MUST be committed individually to allow rollback. Do NOT attempt all changes in a single commit.
