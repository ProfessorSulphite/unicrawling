# HEC & Global University Education Counselor System Pipeline

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![Pydantic V2](https://img.shields.io/badge/pydantic-v2.13-red.svg)](https://docs.pydantic.dev/)
[![Crawl4AI](https://img.shields.io/badge/Crawl4AI-v0.4.3--latest-green.svg)](https://github.com/unclecode/crawl4ai)
[![NotebookLM](https://img.shields.io/badge/NotebookLM-v0.7.3--py-purple.svg)](https://github.com/teng-lin/notebooklm-py)
[![Qdrant Cloud](https://img.shields.io/badge/Qdrant-Cloud_768dim-red.svg)](https://qdrant.tech/)
[![Rich](https://img.shields.io/badge/CLI-Rich-orange.svg)](https://github.com/Textualize/rich)

An autonomous, multi-agent 4-phase data extraction, transformation, universal schema normalization, and inspection pipeline designed for building a production-ready **Education Counseling RAG Vector Database** covering universities globally (**Pakistan**, **Germany**, **United States**, **United Kingdom**, **Europe**).

> 📖 **Looking for commands?** [**`COMMANDS.md`**](COMMANDS.md) is the complete CLI reference —
> every entry point, every flag, every configuration knob, plus common workflows and
> troubleshooting. This README covers architecture and concepts.

---

## 💡 How NotebookLM Workspaces are Managed in Batch Execution

> [!IMPORTANT]
> **NotebookLM Provisioning & Lifecycle Policy**:
> - **Isolated Notebooks Per University**: During a batch run, the system creates an **isolated NotebookLM cloud workspace for each university individually** (e.g. `"ITU_Counseling_DB"`, `"LMU_Counseling_DB"`, `"MIT_Counseling_DB"`).
> - **No Notebook Clutter / Merging**: Sources are NOT dumped into a single monolithic notebook. Each university's candidate links are processed in its dedicated workspace.
> - **Automated Lifecycle Cleanup**: Once the 5-query schema extraction succeeds and passes Pydantic V2 validation, the pipeline calls `delete_notebook_after_success()`. This **deletes the cloud notebook automatically**, keeping your NotebookLM account quota clean (well below the 100-notebook account limit) while permanently preserving the raw links and extracted JSON payloads locally.

---

## 📂 Project Directory Structure

```
notebooklm_scripts/
├── config.json                           # ⚙️ Multi-country university links & pipeline settings
├── cli.py                                # ⚡ Root CLI executable entrypoint wrapper
├── query_qdrant.py                       # 🔍 Standalone Qdrant Cloud interactive CLI query tool
├── src/                                  # 📦 Core Source Package
│   ├── config.py                         # Central configuration dataclass & workspace paths
│   ├── schema.py                         # Strict Pydantic V2 models for master JSON hierarchy
│   ├── state.py                          # SQLite resumable state machine (pooled WAL connection)
│   ├── json_io.py                        # 💾 Streaming + crash-safe atomic JSON I/O primitives
│   ├── universal_normalizer.py           # 🌐 Universal currency, tuition & eligibility normalizer
│   ├── qdrant_validator.py               # ⚡ Automated 10-query Qdrant validation benchmark suite
│   ├── ingest.py                         # Async ingestion: shared HTTP/2 pool & adaptive readiness
│   ├── extract_data.py                   # 5-Query extraction, JSON repair & Exa fallback
│   ├── extract_links.py                  # Phase 1 harvesting w/ shared memory-bounded browser pool
│   ├── pipeline.py                       # Master 4-Phase pipeline & batch runner
│   └── inspect_cli.py                    # Developer-grade Rich-powered inspection CLI
├── tests/                                # Production Pytest suite (116 tests passing)
│   ├── test_pipeline.py                  # Core 4-phase pipeline & extraction tests
│   ├── test_json_io.py                   # Streaming/atomic JSON I/O tests
│   ├── test_ingest_resilience.py         # Ingestion resilience & pre-flight tests
│   ├── test_cli_interactive.py           # Interactive CLI tests
│   └── test_notebook_logger.py           # Notebook lifecycle logging tests
├── data/
│   ├── links/                            # Per-university candidate link partitions (.jsonl)
│   ├── outputs/
│   │   ├── country_outputs/              # 🌐 Per-country output folders (e.g. pak_output/, german_output/)
│   │   ├── uni_outputs/                  # 📄 Per-university formatted JSON payloads (.json)
│   │   ├── university_counseling_data.json   # 📄 Country-grouped master JSON file
│   │   ├── university_counseling_data.jsonl  # 📄 Master JSONL stream for Vector DB indexing
│   │   ├── qdrant_export.json            # ⚡ 768-dim normalized dense vector payload
│   │   └── result.json                   # 📊 Master batch execution analytics summary
│   └── state.sqlite                      # SQLite database tracking pipeline manifest
├── resources/
│   ├── data_model_and_plan.md            # Master 4-Block JSON Schema specification
│   ├── rankings_global.json              # Unified global university identity & rankings registry
│   └── rankings_pk.json                  # Pakistan university rankings registry
├── AGENTS.md                             # 4-Agent multi-agent system architecture
├── CHANGELOG.md                          # Master build and execution log
├── COMMANDS.md                           # 📖 Complete CLI & execution reference
└── README.md                             # Master documentation & architecture guide
```

---

## ⚡ Quick Start: Multi-Country Batch Ingestion (`config.json`)

### 1. Configure Target Links (`config.json`)
Specify target universities grouped by country and set pipeline parameters in `config.json`:

```json
{
  "universities": {
    "Pakistan": [
      { "name": "Information Technology University", "url": "https://itu.edu.pk" },
      { "name": "National University of Sciences and Technology", "url": "https://nust.edu.pk" }
    ],
    "Germany": [
      { "name": "Ludwig-Maximilians-Universität München", "url": "https://www.lmu.de/en/" },
      { "name": "Technical University of Munich", "url": "https://www.tum.de/en/" }
    ],
    "America": [
      { "name": "Massachusetts Institute of Technology", "url": "https://www.mit.edu" }
    ]
  },
  "pipeline_settings": {
    "max_links": 60,
    "exclude_keywords": "news|events",
    "uptodate": true,
    "sync_qdrant": true,
    "force_rerun_all": false,
    "clean_logging": true
  }
}
```

> [!TIP]
> Entries may be objects (`{"name": ..., "url": ...}`) or bare URL strings, but **prefer
> objects**. An explicit `name` produces a correct notebook title and a reliable rankings-registry
> lookup; without it the name is derived from the domain (`itu.edu.pk` → `ITU`).

### 2. Run Batch Execution
Execute batch ingestion with clean logging and `tqdm` progress tracking:
```bash
python3 cli.py batch
```

### 3. Force Rerun All Links (With Automatic Timestamped Backup)
If you want to re-process all links from scratch, use `--rerun-all`. The system automatically moves existing `data/outputs` into a timestamped backup directory (`data/outputs_backup_YYYYMMDD_HHMMSS`) before running:
```bash
python3 cli.py batch --rerun-all
```

> [!WARNING]
> `--rerun-all` is **destructive**. It removes `data/outputs/` and deletes the SQLite state
> database (`data/state.sqlite`, including `-wal`/`-shm`) after copying outputs to the backup
> directory — the timestamped backup is your only copy, and the resumable state manifest is
> not backed up at all. Omit the flag to resume normally: universities already marked
> `completed` are skipped automatically.

### 4. Resume an Interrupted Run
No special flag is needed. Completed universities are read from the SQLite manifest and skipped:
```bash
python3 cli.py state     # inspect what already completed
python3 cli.py batch     # resumes, skipping completed universities
python3 cli.py retry failed
```

---

## 🌐 Universal Schema Normalizer (`src/universal_normalizer.py`)

The pipeline includes a universal normalization engine to ensure consistency across all international datasets:

1. **Universal Currency Engine**:
   - Detects currency from tuition text or country locale (`Germany`/`EU` $\rightarrow$ `EUR`, `USA` $\rightarrow$ `USD`, `UK` $\rightarrow$ `GBP`, `Pakistan` $\rightarrow$ `PKR`, `Switzerland` $\rightarrow$ `CHF`).
2. **Tuition & Fee Normalizer**:
   - German public universities (tuition-free by law): Automatically normalizes `tuition_fee` to `"Tuition Free (Semester Contribution applies)"` and `application_fee` to `"Uni-Assist €75 / Free Direct Application"`.
3. **International Eligibility Standards**:
   - Adapts requirements by region: European degrees $\rightarrow$ `"Abitur NC Grade / ECTS Credit Prerequisites"`, US/UK $\rightarrow$ `"High School Diploma / GPA Equivalent"`, Pakistan $\rightarrow$ `"Intermediate / HSSC (60% Minimum)"`.
4. **Global Identity & Rankings Registry**:
   - Sourced from `resources/rankings_global.json` for deterministic founding year (`established_year`), accreditation body (`accreditation_body`), and official QS 2026 / THE rankings.

---

## ☁️ Qdrant Cloud Vector Database Sync & Benchmark Suite

Vector embeddings are generated using **768-dimensional normalized dense vectors (`BAAI/bge-base-en-v1.5`)**:

### Export & Sync to Qdrant Cloud
```bash
python3 cli.py export --format qdrant --sync
```

### Automated 10-Query Retrieval Validation Benchmark
When `--sync` is passed, the pipeline automatically executes a **10-Query Retrieval Benchmark Suite** against the active Qdrant Cloud collection. If validation achieves $\ge 80\%$ pass rate, live sync is retained; otherwise, automatic rollback safety is triggered.

### Interactive Standalone Qdrant Query CLI
Search the Qdrant Cloud vector database in real-time:
```bash
python3 query_qdrant.py -q "BS Computer Science tuition fee and admission portal"
```

---

## 📊 Master Analytics Report (`data/outputs/result.json`)

Upon completing a batch run, the system generates `data/outputs/result.json` summarizing global metrics:

```json
{
  "timestamp": "2026-07-25T13:40:00",
  "config_file": "config.json",
  "total_universities": 6,
  "country_distribution": {
    "Pakistan": 3,
    "Germany": 2,
    "America": 1
  },
  "program_counts": {
    "undergraduate": 85,
    "graduate": 92,
    "postgraduate_phd": 34,
    "total_programs": 211
  },
  "quality_metrics": {
    "application_portal_coverage_pct": 100.0,
    "admissions_contact_coverage_pct": 100.0,
    "malformed_records": 0
  }
}
```

---

## ⚙️ Performance, Memory & Durability Engineering

The pipeline is built to run an 83-university batch in one process without degrading. The
mechanisms below are what make that safe; see [`COMMANDS.md` §8.2](COMMANDS.md#82-srcconfigpy--tuning-knobs)
for every tuning knob and [`CHANGELOG.md` §3](CHANGELOG.md) for the full record.

### Linear, not quadratic, aggregation
Phase 3 appends each payload to an **append-only JSONL ledger** (`O(1)` per university, fsynced).
The master JSON array is compiled **once per run** in a single streamed pass, rather than being
rebuilt from scratch after every university.

> Measured at N=100 universities: **5,050 record-parses reduced to 100**, **10.9× faster**,
> **19× lower peak memory**, with byte-identical output.

The JSONL is the source of truth — the master array is derived and can be regenerated at any
time. It is deduplicated by university name (last-write-wins), so reprocessing replaces rather
than duplicates.

### Bounded browser memory
One headless browser is started and reused across every university instead of being launched
and torn down per site. Crawl4AI's page pool defaults to **never recycling**
(`max_pages_before_recycle=0`), which lets resident memory grow for the life of the process;
the pipeline sets a recycle bound plus `text_mode` / `light_mode` / `memory_saving_mode`, since
link discovery only needs hrefs and anchor text — not images, CSS, or fonts.

*Verified live against `itu.edu.pk`: one browser launch reused across crawls, Chromium process
count returning to its pre-run baseline with zero orphaned processes.*

### Connection reuse & resilient readiness
Pre-flight verification and text-fallback fetches share a single **HTTP/2 `httpx.AsyncClient`**
(50 max connections, 20 keep-alive) instead of paying a TCP + TLS handshake per URL.

Source readiness is polled **per source, with jitter and failure isolation**. Previously a
single bad source made the batch wait raise and collapse the ready count to zero; now two
failures out of twenty-five correctly report **23 ready** and the notebook stays usable.

### Crash safety
Every whole-file write lands in a fsynced `.tmp` sibling and is then atomically renamed
(`src/json_io.py`). A crash leaves either the previous complete file or the new one — never a
half-written payload, and never a truncated link partition handed to Phase 2.

### Enforced quota
Phase 3 reserves its 5 queries against the **500/day NotebookLM Pro ceiling before issuing any
query**, and refuses to start a university that cannot complete within the remaining budget.
Retry overage is charged afterwards, so the ledger reflects real consumption.

### Faster state layer
`StateManager` holds one persistent thread-local SQLite connection (`WAL`, `synchronous=NORMAL`,
`temp_store=MEMORY`) instead of opening a new one per method call, with indexes covering the
hot read paths — the query-ledger sum is served entirely from a covering index.

---

## 🧪 Unit Testing

Run full production test suite:
```bash
pytest
```
*(116 / 116 passed, 100% pass rate — fully offline, no network calls, no NotebookLM quota consumed)*
