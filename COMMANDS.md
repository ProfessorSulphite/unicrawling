# COMMANDS.md — Complete CLI & Execution Reference

Exhaustive reference for every runnable entry point in the Education Counselor RAG pipeline.
Every command listed here was executed against this repository and verified to run.

> **Run everything from the project root** (`notebooklm_scripts/`). All entry points insert the
> project root into `sys.path` themselves, but relative default paths (`run_settings.json`,
> `extracted_links.txt`) resolve against your current working directory.

---

## Table of Contents

1. [Entry Points at a Glance](#1-entry-points-at-a-glance)
2. [Setup & Prerequisites](#2-setup--prerequisites)
3. [`cli.py` — Main CLI](#3-clipy--main-cli)
4. [`run.py` — Pipeline Runner](#4-runpy--pipeline-runner)
5. [Standalone module entry points](#5-standalone-module-entry-points)
6. [Maintenance](#6-maintenance)
7. [Testing](#7-testing)
8. [Configuration Reference](#8-configuration-reference)
9. [Output Files](#9-output-files)
10. [Common Workflows](#10-common-workflows)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Entry Points at a Glance

| Command | Purpose | Network | Consumes NotebookLM quota |
| :--- | :--- | :---: | :---: |
| `python3 cli.py <subcommand>` | Inspector: inspect, audit, export, … | varies | only `batch` / `retry` |
| `python3 run.py` | Master 4-phase pipeline, single URL or batch | yes | **yes** |
| `python3 -m src.inspector` | The same inspector, as a module | varies | only `batch` / `retry` |
| `python3 -m src.orchestrator` | The same pipeline, as a module | yes | **yes** |
| `pytest` | Test suite | no | no |

**Quota-consuming commands are marked ⚠️ throughout.** The daily NotebookLM Pro ceiling is
500 queries; the pipeline reserves `config.queries_per_university` (6 since the diploma query was
added) *before* querying, and refuses to start a university that cannot complete within the
remaining budget. `run.py --dry-run` walks the whole queue and spends none of it.

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

`cli.py` is a thin root entry point for `src/inspector/`. Running `python3 cli.py` with no
subcommand prints the help text.

### 3.1 `batch` ⚠️ — run the multi-country pipeline from `run_settings.json`

```bash
python3 cli.py batch
python3 cli.py batch --config run_settings.json
python3 cli.py batch --rerun-all
```

| Flag | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `--config` | path | `run_settings.json` | Batch run-settings file |
| `--rerun-all` | flag | off | Back up and reprocess **every** configured university |

Behaviour:
- Universities already marked `completed` in `data/state.sqlite` are **skipped** unless
  `--rerun-all` is passed (or `force_rerun_all: true` is set in `run_settings.json`).
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
python3 cli.py export --format csv --output /tmp/universities.csv
```

| Flag | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `--format` | `csv` \| `json` | `csv` | Export format |
| `--output` | path | format-specific | Custom output path |
| `--sync` | flag | off | **Live** Qdrant push + 10-query validation suite |

Notes:
- Without `--sync`, the Qdrant payload is written locally and **nothing is pushed**.
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

A university whose payload was written but had a query block fail every retry is `partial`,
flagged ⚠ with the failed blocks in the Notes column. `partial` is deliberately not a
completion: a resumed batch retries those universities rather than skipping them, because the
empty bucket is an artefact of the failure and not a fact about the university.

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

## 4. `run.py` — Pipeline Runner

The 4-phase pipeline. `python3 -m src.orchestrator` is the same program.

```bash
python3 run.py --help
```

### 4.1 Single-university mode ⚠️

```bash
python3 run.py --url https://itu.edu.pk --name "Information Technology University"
python3 run.py --url https://itu.edu.pk --name "ITU" --max-links 25
python3 run.py --url https://nust.edu.pk --name "NUST" --exclude-keywords "news|events|alumni"
```

Writes a run manifest to `loggings/single_logs/s_{id}.json`.

### 4.2 Batch mode ⚠️

```bash
python3 run.py
python3 run.py --config run_settings.json --rerun-all
```

Reads `run_settings.json`, skips universities `state.sqlite` already reports complete, and
writes `loggings/complete_logs/c_{id}.json`. `--rerun-all` archives `data/outputs/` to a
timestamped sibling and resets the state database — the two must move together, or every
university would still read "completed" against payloads that had just been archived.

### 4.3 Dry run — no network, no quota

```bash
python3 run.py --config run_settings.json --dry-run
```

Expands the queue and writes a real run log without executing a phase. This is the cheap way
to check that a settings file resolves to the universities you meant.

### 4.4 Resuming

```bash
python3 run.py --resume c_7
python3 run.py --resume s_42
```

Replays that run's university list and the settings it ran under, so a resume reproduces the
original rather than picking up whatever `run_settings.json` says today. What to *skip* still
comes from `state.sqlite`, which is the only authority on completion.

### 4.5 What each phase does

| Phase | Module | Output |
| :--- | :--- | :--- |
| 1 — Link harvesting | `src/extractor/linkers/` | `data/links/<slug>.jsonl`, one record per link with its tier |
| 2 — NotebookLM ingestion | `src/ingestor/` | a notebook, its sources, and the url→source_id→tier map in `state.sqlite` |
| 3 — Schema extraction | `src/extractor/crawlers/` | a validated `UniversityPayload` |
| 4 — Audit & aggregation | `src/inspector/` | `data/outputs/`, the master array, and the health report |

Phase 3 issues its queries **serially** against a notebook. That is a correctness requirement,
not a throughput choice: concurrent asks share a conversation and return each other's answers.

---

## 5. Standalone module entry points

```bash
python3 -m src.orchestrator --help
python3 -m src.inspector --help
```

Phase 1 can be run alone through the linker package's own `__main__` block:

```bash
python3 -m src.extractor.linkers.runner --help
```

---

## 6. Maintenance

### 6.1 Migrating the legacy notebook audit trail

```bash
python3 -m src.logger.migrate_audit --check
python3 -m src.logger.migrate_audit
```

Converts the pre-C26 append-only `loggings/notebook_audit.jsonl` into one JSON document per
notebook under `loggings/notebook_logs/`. Idempotent — it rebuilds each document from the
source rather than appending — and it never deletes the source file.

---

## 7. Testing

```bash
pytest                                    # full suite — expect 116 passed
pytest -q                                 # quiet
pytest -v                                 # verbose, per-test names
pytest tests/test_pipeline.py             # core pipeline suite
pytest tests/test_utilities/test_json_io.py   # streaming/atomic JSON I/O
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

### 8.1 `run_settings.json` — batch targets and run settings

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
| `restrict_links_to_university_domain` | `True` | Drop harvested links outside the university's own registrable domain |
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
| `max_query_split_depth` | `2` | Times an oversized query may be halved over its sources (see note below) |
| `query_concurrency` | `3` | Notebooks queried in parallel (see note below) |
| `daily_query_budget` | `500` | NotebookLM Pro daily ceiling — **enforced** |
| `queries_per_university` | `6` | Reserved per university before querying |

> **Note on `query_concurrency`:** the suite against one notebook is **serial**, and must stay
> that way. Concurrent unkeyed asks share a conversation, and an ask still waiting when a later
> ask's turn lands returns *that* turn's answer — observed live on 2026-09-03, where `bachelors`
> and `phd` came back byte-identical and the PhD programmes were filed as bachelors with no
> error raised. This setting governs notebook-level parallelism only, where no conversation is
> shared.

> **Note on `max_query_split_depth`:** a `RPCResponseTooLargeError` means the answer did not
> fit, not that it was wrong, so re-asking the same question of the same sources fails
> identically. Such a query is re-asked over halves of its source set and the answers merged.
> Each level doubles the sub-asks, so this trades daily query budget for coverage; `0` disables
> narrowing entirely.

**Phase 4 — vectors**

| Field | Default | Meaning |
| :--- | :--- | :--- |
| `embedding_model_name` | `BAAI/bge-small-en-v1.5` | 384-dim embedding model |
| `embedding_batch_size` | `64` | Texts per embedding forward pass |

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
# populate run_settings.json, then:
python3 cli.py batch
python3 cli.py analytics
```

### Test one university before committing to a batch

```bash
python3 run.py --url https://itu.edu.pk --name "Information Technology University" --max-links 25
python3 cli.py inspect itu
```

### Tune Phase 1 without spending quota

```bash
python3 -m src.extractor.linkers.runner --url https://itu.edu.pk --max-links 40 --threshold 0.7
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

### Check the corpus is fit to publish

```bash
python3 cli.py audit
```

Exits non-zero when required-field coverage is below the floors in `src/config.py`, so it can
gate a push script rather than only a human reading a table.

### Check remaining daily quota before a large batch

```bash
python3 -c "
from src.utilities.state_management import StateManager
sm = StateManager()
print('used today:', sm.queries_used_today(), '| remaining:', sm.remaining_query_budget())
sm.close()"
```

### Regenerate the master JSON from the ledger

```bash
python3 -c "
from src.orchestrator import compile_master_json
compile_master_json()"
```

---

## 11. Troubleshooting

**`Phase 1 produced zero links` / exit code 1**
The site blocked the crawler or the threshold is too strict. Retry Phase 1 standalone with a
lower `--threshold` and more pages:
`python3 -m src.extractor.linkers.runner --url <url> --threshold 0.5 --max-pages 20`.

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

**Two degree buckets holding identical programmes**
One query received another's answer. The extractor refuses to file that and reports both
blocks failed; `python3 cli.py audit` also detects it in payloads written before the guard
existed. Re-extract the university.

**HTTP/2 unavailable warning**
The optional `h2` package is missing. The pooled client falls back to HTTP/1.1 keep-alive,
which retains most of the connection-reuse benefit. Install with `pip install "httpx[http2]"`.

**Stale browser processes after a crash**
The shared browser is closed by the batch and CLI drivers. After a hard kill, clear leftovers
with `pkill -f chromium`.

**Tests pass but the pipeline fails**
The suite is fully mocked and offline by design. Real failures are almost always
authentication (§2.3), network, or quota — check those three first.
