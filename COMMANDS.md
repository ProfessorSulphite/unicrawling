# COMMANDS.md — Complete CLI & Execution Reference

Exhaustive reference for every runnable entry point in the Education Counselor RAG pipeline.
Every command listed here was executed against this repository and verified to run.

> **Run everything from the project root** (`notebooklm_scripts/`). All entry points insert the
> project root into `sys.path` themselves, but relative default paths (`config.json`,
> `extracted_links.txt`) resolve against your current working directory.

---

## Table of Contents

1. [Entry Points at a Glance](#1-entry-points-at-a-glance)
2. [Setup & Prerequisites](#2-setup--prerequisites)
3. [`cli.py` — Main CLI](#3-clipy--main-cli)
4. [`src/pipeline.py` — Master Pipeline Runner](#4-srcpipelinepy--master-pipeline-runner)
5. [`src/extract_links.py` — Phase 1 Standalone](#5-srcextract_linkspy--phase-1-standalone)
6. [`query_qdrant.py` — Vector Search](#6-query_qdrantpy--vector-search)
7. [Testing](#7-testing)
8. [Configuration Reference](#8-configuration-reference)
9. [Output Files](#9-output-files)
10. [Common Workflows](#10-common-workflows)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Entry Points at a Glance

| Command | Purpose | Network | Consumes NotebookLM quota |
| :--- | :--- | :---: | :---: |
| `python3 cli.py <subcommand>` | Main CLI (inspect, export, batch, …) | varies | only `batch` / `retry` |
| `python3 src/pipeline.py` | Master 4-phase pipeline, single URL or batch | yes | **yes** |
| `python3 src/extract_links.py` | Phase 1 link harvesting only | yes | no |
| `python3 query_qdrant.py` | Query the Qdrant vector DB | yes | no |
| `pytest` | Test suite (116 tests) | no | no |

**Quota-consuming commands are marked ⚠️ throughout.** The daily NotebookLM Pro ceiling is
500 queries; the pipeline reserves 5 per university *before* querying and refuses to start a
university that cannot complete within the remaining budget.

---

## 2. Setup & Prerequisites

### 2.1 Install dependencies

```bash
pip install -r requirements.txt
```

Playwright browsers are required by Crawl4AI for Phase 1:

```bash
python3 -m playwright install chromium
```

### 2.2 Environment variables (`.env` in project root)

`src/config.py` loads `.env` automatically. **Existing exported environment variables always
win over the file**, so an explicitly exported key is never silently overridden.

```bash
EXA_API_KEY=...              # optional: application-portal gap filling in Phase 3
QDRANT_URL=...               # default: http://localhost:6333
QDRANT_API_KEY=...           # optional: omit for a local Qdrant container
QDRANT_COLLECTION_NAME=...   # default: education_counselor
PINECONE_API_KEY=...         # optional: only for --format pinecone
PINECONE_INDEX_NAME=...      # default: education-counselor
```

### 2.3 NotebookLM authentication

Phases 2 and 3 use `NotebookLMClient.from_storage()`, which reads a stored browser session
from `~/.notebooklm/` (created by the `notebooklm` package's own login flow — it is **not**
configured through `.env`).

Verify your session is live before starting a long batch:

```bash
python3 -c "
import asyncio
from notebooklm import NotebookLMClient
async def m():
    async with NotebookLMClient.from_storage() as c:
        print('AUTH OK —', len(await c.notebooks.list()), 'notebooks')
asyncio.run(m())"
```

### 2.4 Verify the install without touching the network

```bash
pytest -q                      # expect: 116 passed
python3 cli.py schema          # renders the master JSON schema
python3 cli.py state           # renders the SQLite state manifest
```

---

## 3. `cli.py` — Main CLI

```bash
python3 cli.py --help
python3 cli.py <subcommand> --help
```

`cli.py` is a thin wrapper around `src/inspect_cli.py`. Running `python3 cli.py` with no
subcommand prints the help text.

### 3.1 `batch` ⚠️ — run the multi-country pipeline from `config.json`

```bash
python3 cli.py batch
python3 cli.py batch --config config.json
python3 cli.py batch --rerun-all
```

| Flag | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `--config` | path | `config.json` | Batch configuration file |
| `--rerun-all` | flag | off | Back up and reprocess **every** configured university |

Behaviour:
- Universities already marked `completed` in `data/state.sqlite` are **skipped** unless
  `--rerun-all` is passed (or `force_rerun_all: true` is set in `config.json`).
- `--rerun-all` first copies `data/outputs/` to `data/outputs_backup_YYYYMMDD_HHMMSS/`,
  removes the original, and deletes the SQLite state files. **This is destructive to
  `data/outputs/` and the state DB** — the backup is your only copy.
- The master JSON array is aggregated **once**, after the whole queue drains.
- One headless browser and one HTTP/2 connection pool are shared across the entire batch and
  closed at the end.

### 3.2 `inspect <query>` — deep-inspect one university

```bash
python3 cli.py inspect itu
python3 cli.py inspect "Information Technology University"
```

Matches against the `uni_outputs/` filename stem first, then falls back to searching every
record by name and abbreviation. Accepts a slug, full name, or abbreviation.

### 3.3 `diff <slug1> <slug2>` — compare two universities

```bash
python3 cli.py diff itu nust
```

### 3.4 `search <keyword>` — search across all degree programs

```bash
python3 cli.py search "data science"
python3 cli.py search "computer science" --level BS
python3 cli.py search "engineering" --level MS --max-fee 500000
```

| Flag | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `--level` | str | none | Degree level filter (e.g. `BS`, `MS`, `PhD`) |
| `--max-fee` | float | none | Maximum tuition fee, numeric, in PKR |

### 3.5 `retry [target]` ⚠️ — re-run failed or pending universities

```bash
python3 cli.py retry            # target defaults to "failed"
python3 cli.py retry failed
python3 cli.py retry pending
python3 cli.py retry all
python3 cli.py retry itu        # a specific slug
```

### 3.6 `export` — export the dataset

```bash
python3 cli.py export --format csv
python3 cli.py export --format json
python3 cli.py export --format qdrant
python3 cli.py export --format qdrant --sync
python3 cli.py export --format pinecone
python3 cli.py export --format csv --output /tmp/universities.csv
```

| Flag | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `--format` | `csv` \| `qdrant` \| `pinecone` \| `json` | `csv` | Export format |
| `--output` | path | format-specific | Custom output path |
| `--sync` | flag | off | **Live** Qdrant push + 10-query validation suite |

Notes:
- `qdrant` and `pinecone` load `BAAI/bge-base-en-v1.5` and generate 768-dim normalized dense
  vectors. The first run downloads the model (hundreds of MB).
- Without `--sync`, the Qdrant payload is written locally and **nothing is pushed**.
- With `--sync`, points are upserted in batches of `qdrant_upsert_batch_size` (default 64) and
  the 10-query benchmark runs afterwards.
- If the collection exists with a different vector dimension, it is **deleted and recreated**.

### 3.7 `analytics` — dataset health audit

```bash
python3 cli.py analytics
python3 cli.py analytics --file data/outputs/university_counseling_data.jsonl
```

Streams the JSONL line by line, so memory does not scale with dataset size. Reports university
count, public/private split, program counts by level, faculty count, portal and contact
coverage, and malformed line count.

### 3.8 `state` — SQLite pipeline manifest

```bash
python3 cli.py state
```

Shows each university's slug, status (`pending` → `crawled` → `ingested` → `extracted` →
`completed`, or `failed`), notebook ID, sources ingested, queries executed, and last update.

### 3.9 `schema` — display the master JSON schema

```bash
python3 cli.py schema
```

### 3.10 `notebooks` ⚠️ — list live NotebookLM notebooks

```bash
python3 cli.py notebooks
```

Requires a valid NotebookLM session. Useful for confirming that
`delete_notebook_after_success()` is actually reclaiming workspace slots.

### 3.11 `interactive` — Rich TUI menu

```bash
python3 cli.py interactive
```

---

## 4. `src/pipeline.py` — Master Pipeline Runner

Run all four phases. Two modes: **single URL** (when `--url` is given) or **batch** (otherwise).

```bash
python3 src/pipeline.py --help
```

### 4.1 Single-university mode ⚠️

```bash
python3 src/pipeline.py --url https://itu.edu.pk --name "Information Technology University"
python3 src/pipeline.py --url https://itu.edu.pk --name "Information Technology University" --max-links 25
python3 src/pipeline.py --url https://nust.edu.pk --name "NUST" --exclude-keywords "news|events|alumni"
```

| Flag | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `--url` | str | none | Target university URL. **Presence of this flag selects single mode.** |
| `--name` | str | derived from domain | Full university name; also names the notebook |
| `--max-links` | int | `60` | Maximum links retained by Phase 1 |
| `--exclude-keywords` | str | `news\|events` | Pipe-separated exclusion patterns |
| `--uptodate` | flag | `True` | 2026 recency boosting |
| `--config` | path | `config.json` | Batch config (**batch mode only**) |
| `--rerun-all` | flag | off | Force rerun (**batch mode only**) |

> **Known quirk:** `--uptodate` is declared as `action="store_true", default=True`, so it is
> always `True` here and **cannot be disabled from this entry point**. To crawl with recency
> boosting off, use `src/extract_links.py --uptodate false` (Phase 1 standalone), which parses
> the value properly.

Always pass `--name` for a real institution. Without it the name is derived from the domain
(`itu.edu.pk` → `ITU`), which changes the notebook title and weakens the rankings-registry
lookup that supplies verified identity fields.

### 4.2 Batch mode ⚠️

Omitting `--url` runs the same batch as `python3 cli.py batch`:

```bash
python3 src/pipeline.py
python3 src/pipeline.py --config config.json --rerun-all
```

### 4.3 What each phase does

| Phase | Action | Failure behaviour |
| :--- | :--- | :--- |
| 1 | Crawl, filter, tier, deduplicate links → `data/links/<slug>.jsonl` | Zero links → marked `failed`, university skipped |
| 2 | Create notebook, pre-flight URLs, upload, wait for readiness | Individual sources may fail; the rest still proceed |
| 3 | Reserve quota, run the 5-query suite, repair JSON, Exa fallback | A failed query yields an empty block, not a crash |
| 4 | Aggregate master JSON, audit analytics | — |

The notebook is deleted **only** after the payload validates and is written to disk.

---

## 5. `src/extract_links.py` — Phase 1 Standalone

Harvest links without touching NotebookLM. No quota cost.

```bash
python3 src/extract_links.py --help
python3 src/extract_links.py --url https://itu.edu.pk
python3 src/extract_links.py --url https://itu.edu.pk --max-links 40 --max-pages 10
python3 src/extract_links.py --url https://itu.edu.pk --uptodate false
python3 src/extract_links.py --hec --hec-limit 10
python3 src/extract_links.py --url https://nust.edu.pk --exclude-keywords "news|events|tender|jobs"
```

| Flag | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `--url` | str | none | Target URL. If omitted and `--hec` is not set, defaults to `https://itu.edu.pk/admissions/` |
| `--hec` | flag | off | Auto-discover universities from the HEC directory |
| `--hec-limit` | int | `5` | Universities to process in HEC mode |
| `--max-links` | int | `100` | Max links retained per university |
| `--exclude-keywords` | str | `news\|events` | Pipe-separated exclusion patterns |
| `--threshold` | float | `0.45` | Semantic similarity threshold, 0.0–1.0 |
| `--max-pages` | int | `15` | Sub-pages crawled per site |
| `--uptodate` | bool | `true` | Accepts `true/false/yes/no/1/0` |
| `--output-links` | path | `extracted_links.txt` | Plain URL list |
| `--output-detailed` | path | `extracted_links_detailed.txt` | Full metadata report |

Exits with status **1** if every target produced zero links.

Actual link count is `min(max(15, 45% of scored candidates), --max-links)`, then allocated
across tiers proportionally (T1 45%, T2 30%, T3 15%, T4 10%) rather than by a flat top-N slice.

---

## 6. `query_qdrant.py` — Vector Search

```bash
python3 query_qdrant.py --help
python3 query_qdrant.py -q "BS Computer Science tuition fee and admission portal"
python3 query_qdrant.py --query "PhD scholarships in Lahore" --top-k 10
python3 query_qdrant.py -q "data science" -k 3 -c education_counselor
```

| Flag | Short | Type | Default | Meaning |
| :--- | :--- | :--- | :--- | :--- |
| `--query` | `-q` | str | none | Search query string |
| `--top-k` | `-k` | int | `5` | Results to retrieve |
| `--collection` | `-c` | str | `education_counselor` | Qdrant collection name |

Requires a populated collection — run `python3 cli.py export --format qdrant --sync` first.

---

## 7. Testing

```bash
pytest                                    # full suite — expect 116 passed
pytest -q                                 # quiet
pytest -v                                 # verbose, per-test names
pytest tests/test_pipeline.py             # core pipeline suite
pytest tests/test_json_io.py              # streaming/atomic JSON I/O (22 tests)
pytest tests/test_ingest_resilience.py    # ingestion resilience
pytest tests/test_cli_interactive.py      # interactive CLI
pytest tests/test_notebook_logger.py      # lifecycle logging
pytest -k "citation or json"              # filter by name
pytest -x                                 # stop at first failure
pytest --lf                               # rerun last failures
```

The suite is fully offline — it makes no network calls and consumes no NotebookLM quota.

---

## 8. Configuration Reference

### 8.1 `config.json` — batch targets and run settings

```json
{
  "universities": {
    "Pakistan": [
      { "name": "Information Technology University", "url": "https://itu.edu.pk" }
    ]
  },
  "pipeline_settings": {
    "max_links": 80,
    "exclude_keywords": "news|events",
    "uptodate": true,
    "sync_qdrant": true,
    "force_rerun_all": false,
    "clean_logging": true
  }
}
```

Entries may be objects (`{"name": ..., "url": ...}`) or bare URL strings. **Prefer objects** —
an explicit `name` produces a correct notebook title and registry lookup.

| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `max_links` | `60` | Links retained per university |
| `exclude_keywords` | `news\|events` | Pipe-separated exclusion patterns |
| `uptodate` | `true` | 2026 recency boosting |
| `sync_qdrant` | `true` | Live Qdrant sync during the post-batch export |
| `force_rerun_all` | `false` | Same as `--rerun-all`; **destructive** |
| `clean_logging` | `true` | Suppress noisy HTTP/crawler logs |

### 8.2 `src/config.py` — tuning knobs

Edit the `Config` dataclass to change these.

**Phase 1 — crawl and browser memory**

| Field | Default | Meaning |
| :--- | :--- | :--- |
| `max_sources_per_notebook` | `150` | Hard ceiling on sources per notebook |
| `dynamic_link_ratio` | `0.45` | Fraction of scored candidates selected |
| `semantic_threshold` | `0.68` | Minimum similarity to survive scoring |
| `max_crawl_pages` | `15` | Pages crawled per site |
| `crawler_reuse_browser` | `True` | Reuse one browser across universities |
| `crawler_headless` | `True` | Headless mode |
| `crawler_text_mode` | `True` | Skip images/CSS/fonts (large memory saving) |
| `crawler_light_mode` | `True` | Disable background browser features |
| `crawler_memory_saving_mode` | `True` | Crawl4AI memory-saving mode |
| `crawler_max_pages_before_recycle` | `30` | Recycle the page pool; `0` means never (crawl4ai's default, unbounded growth) |
| `crawler_viewport_width` / `_height` | `1024` / `768` | Viewport size |

**Phase 2 — ingestion, HTTP pool, readiness**

| Field | Default | Meaning |
| :--- | :--- | :--- |
| `concurrent_uploads` | `2` | Simultaneous source uploads |
| `source_ready_timeout_sec` | `600` | Per-source readiness timeout |
| `preflight_http_check` | `True` | Drop dead/403 URLs before upload |
| `preflight_concurrency` | `15` | Simultaneous pre-flight probes |
| `http_timeout_sec` | `10.0` | Shared client request timeout |
| `http_connect_timeout_sec` | `5.0` | Connect timeout |
| `http_max_connections` | `50` | Pool ceiling |
| `http_max_keepalive_connections` | `20` | Keep-alive ceiling |
| `http2_enabled` | `True` | HTTP/2 (falls back to 1.1 if `h2` is missing) |
| `readiness_poll_concurrency` | `10` | Simultaneous readiness pollers |
| `readiness_initial_interval_sec` | `1.5` | First poll interval, before jitter |
| `readiness_max_interval_sec` | `5.0` | Backoff ceiling |
| `readiness_backoff_factor` | `1.5` | Backoff multiplier |
| `readiness_jitter_ratio` | `0.35` | Randomisation applied to the first interval |

**Phase 3 — queries**

| Field | Default | Meaning |
| :--- | :--- | :--- |
| `chat_timeout_sec` | `180` | Per-query timeout |
| `max_query_retries` | `2` | Repair retries per query |
| `query_concurrency` | `3` | Concurrent queries in the suite (see note below) |
| `daily_query_budget` | `500` | NotebookLM Pro daily ceiling — **enforced** |
| `queries_per_university` | `5` | Reserved per university before querying |

> **Note on `query_concurrency`:** the suite is issued concurrently, but against a *single*
> notebook this does not reduce wall time. The `notebooklm` SDK holds a per-`notebook_id` lock
> for the full duration of any `chat.ask()` made without a `conversation_id`, and exposes no
> API to create independent conversations — so the five queries serialise inside the client
> regardless of this setting.

**Phase 4 — vectors**

| Field | Default | Meaning |
| :--- | :--- | :--- |
| `embedding_model_name` | `BAAI/bge-base-en-v1.5` | 768-dim embedding model |
| `qdrant_upsert_batch_size` | `64` | Points per upsert request |
| `embedding_batch_size` | `32` | Texts per embedding forward pass |

---

## 9. Output Files

| Path | Written by | Contents |
| :--- | :--- | :--- |
| `data/links/<slug>.jsonl` | Phase 1 | Tiered candidate links per university |
| `extracted_links.txt` | Phase 1 | Plain URL list (last run) |
| `extracted_links_detailed.txt` | Phase 1 | Full metadata report (last run) |
| `data/outputs/university_counseling_data.jsonl` | Phase 3 | **Append-only ledger** — one payload per line, fsynced |
| `data/outputs/university_counseling_data.json` | Phase 4 | Master array, aggregated once per run |
| `data/outputs/uni_outputs/<slug>.json` | Phase 3 | Per-university pretty JSON |
| `data/outputs/country_outputs/` | `export --format json` | Per-country groupings |
| `data/outputs/qdrant_export.json` | `export --format qdrant` | 768-dim vector payload |
| `data/outputs/result.json` | Batch end | Global analytics summary |
| `data/state.sqlite` | All phases | Resumable state, source map, quota ledger, audit log |
| `loggings/` | All phases | Notebook lifecycle logs |

Notes:
- **The JSONL is the source of truth.** The master `.json` is derived from it and can be
  regenerated at any time.
- The master array is **deduplicated by university name, last-write-wins** — reprocessing a
  university replaces its earlier entry rather than appending a duplicate.
- Every whole-file write goes to a `.tmp` sibling, is fsynced, then atomically renamed. A
  crash leaves either the previous complete file or the new one, never a partial one.

---

## 10. Common Workflows

### First full run, from scratch

```bash
pip install -r requirements.txt
python3 -m playwright install chromium
# populate config.json, then:
python3 cli.py batch
python3 cli.py analytics
```

### Test one university before committing to a batch

```bash
python3 src/pipeline.py --url https://itu.edu.pk --name "Information Technology University" --max-links 25
python3 cli.py inspect itu
```

### Tune Phase 1 without spending quota

```bash
python3 src/extract_links.py --url https://itu.edu.pk --max-links 40 --threshold 0.7
head -40 extracted_links_detailed.txt
```

### Resume an interrupted batch

```bash
python3 cli.py state          # see what completed
python3 cli.py batch          # completed universities are skipped automatically
```

### Retry only the failures

```bash
python3 cli.py retry failed
```

### Rebuild the vector database

```bash
python3 cli.py export --format qdrant --sync
python3 query_qdrant.py -q "BS Computer Science admission portal"
```

### Check remaining daily quota before a large batch

```bash
python3 -c "
from src.state import StateManager
sm = StateManager()
print('used today:', sm.queries_used_today(), '| remaining:', sm.remaining_query_budget())
sm.close()"
```

### Regenerate the master JSON from the ledger

```bash
python3 -c "
from src.pipeline import compile_master_json
compile_master_json()"
```

---

## 11. Troubleshooting

**`Phase 1 produced zero links` / exit code 1**
The site blocked the crawler or the threshold is too strict. Retry Phase 1 standalone with a
lower `--threshold` and more pages: `python3 src/extract_links.py --url <url> --threshold 0.5 --max-pages 20`.

**`RPCError rpc_code=9` during upload**
NotebookLM's server-side crawler could not fetch the URL. The pre-flight check
(`preflight_http_check`) filters most of these, and unreachable pages fall back to local text
extraction. Persistent failures are recorded in `IngestResult.failed_urls` and the run
continues.

**Fewer sources "ready" than uploaded**
Expected and non-fatal. Readiness is polled per source with failure isolation, so two bad
sources out of twenty-five report `23 ready` — the notebook is still queried against the ones
that succeeded. Only a `0 ready` result is worth investigating.

**`Daily NotebookLM query budget exhausted`**
The 500/day ceiling was reached. The ledger is keyed by **UTC** day. Check with the quota
snippet in §10; wait for UTC rollover or raise `daily_query_budget` if your plan allows.

**Notebooks accumulating in your NotebookLM account**
`delete_notebook_after_success()` runs only after the payload validates and is written. A
crashed run leaves its notebook behind by design, so the ingested sources can be reused.
Audit with `python3 cli.py notebooks`.

**`Qdrant Connection Notice: ...`**
The vector payload is still saved locally to `data/outputs/qdrant_export.json`. Start a local
instance with `docker run -p 6333:6333 qdrant/qdrant`, or check `QDRANT_URL` / `QDRANT_API_KEY`.

**HTTP/2 unavailable warning**
The optional `h2` package is missing. The pooled client falls back to HTTP/1.1 keep-alive,
which retains most of the connection-reuse benefit. Install with `pip install "httpx[http2]"`.

**Stale browser processes after a crash**
The shared browser is closed by the batch and CLI drivers. After a hard kill, clear leftovers
with `pkill -f chromium`.

**Tests pass but the pipeline fails**
The suite is fully mocked and offline by design. Real failures are almost always
authentication (§2.3), network, or quota — check those three first.
