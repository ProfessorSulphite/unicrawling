# Unicrawling Refactor — Engineering Analysis & Commit-by-Commit To-Do

Companion to [refactoring_plan.md](./refactoring_plan.md). That document is the *what*.
This document is the *how*, in the order it must actually happen, with a test gate on every commit.

**Status: IN PROGRESS.** Phases 0–5 complete (C0–C11, plus C11b). Work is on branch
`refactor/modular-src`; `main` remains at `4d531f0` and the tag `pre-refactor` marks that same
commit. **Baseline: 116 at C0, now 187** (C4 +11 loaders, C9 +47 logger, C10 +10 config, C11 +2 export). Every commit must hold the current count.
(`/home/huzaifayaqob/miniconda3/envs/ise-env/bin/python -m pytest -q`).

> Note: the project runs on conda env `ise-env` (Python 3.11). The base conda python has no pytest.

---

## Part A — Analysis of the Current State

### A.0 BLOCKER: the repository does not import right now — ✅ RESOLVED in C0 (`81c8dcc`)

`src/config.py` was edited in the working tree: the field `uni_outputs_dir` was renamed to
`outputs_uni_outputs_dir`, but `ensure_directories()` (line 175) still references the old name.
Because `config.py` ends with a module-level `config.ensure_directories()`, **every import of
`src.config` raises at import time**:

```
AttributeError: 'Config' object has no attribute 'uni_outputs_dir'
```

Every module in `src/` imports `src.config`. So `cli.py`, the whole pipeline, and the entire test
suite are dead right now. `refactoring_plan.md` §2 correctly identifies this bug but schedules the
fix at step 2 of 14 — it has to be step 0, before anything is committed or measured. There is no
usable baseline until this is fixed.

**Outcome — worse than diagnosed.** The rename was not confined to `config.py`: 11 call sites across
`src/pipeline.py`, `src/inspect_cli.py` and `tests/test_cli_interactive.py` still used the old name.
Fixing only the line the plan named yielded **111 passed / 5 errors**; completing the rename yielded
**116 passed / 0 errors**. Anyone applying `refactoring_plan.md` §2 literally would have shipped a
still-broken tree.

### A.1 What the plan gets right

No argument with these; they are the correct calls and the to-do list below implements them as written:

- Domain packages over the current 13 flat files (`extract_links.py` alone is 61 KB / 1350 lines).
- `orchestrator.py` as a single thin entrypoint replacing the `pipeline.py` + `cli.py` split.
- Dropping Qdrant.
- Keeping JSONL alongside JSON.
- Structured `single_logs/` + `complete_logs/` with resumable IDs.
- Programs as the primary data target.
- Pre-flight health sampling before spending quota on a notebook.
- One commit per step, so any step can be rolled back.

### A.2 Gaps and contradictions found in the codebase that the plan does not cover

These are the ones that will bite mid-refactor if they are not decided up front.

| # | Finding | Evidence | Consequence |
|---|---------|----------|-------------|
| 1 | **`schema.py` cannot be a "direct move".** The plan's §7 migration table calls it a direct move, but §5 mandates a schema change. | `src/schema.py:16` `DegreeLevel` enum and `ProgramCategoryBlock` use `undergraduate` / `graduate` / `postgraduate_and_phd`. The plan requires `Bachelors` / `Masters` / `PhD` / `Diploma`. **Diploma does not exist anywhere in the current system.** | Taxonomy change touches schema, prompts, normalizers, inspector aggregation, `university_payload_schema.json`, and all existing output files. Needs its own commit, not a `git mv`. |
| 2 | **The NotebookLM prompts must change, and the plan never mentions them.** | `src/extract_data.py:350-460`. `QUERY_SUITE` is 5 queries. `_PROGRAM_STRUCTURE` has no `admission_requirements`, and has `summary_3_lines` where §5 requires a full descriptive paragraph. | §5's "per-program required fields" is unachievable by refactoring alone — the data is never requested from NotebookLM. Prompt work is a *feature* commit, kept separate from *move* commits. |
| 3 | **Adding Diploma breaks the query-budget arithmetic.** | `config.queries_per_university = 5`, `daily_query_budget = 500`; §6.5 of the plan says "reserve the full 5-query budget". | A 6th query per university is +20% quota per uni. `queries_per_university`, the budget, and the §6.5 reservation wording all need updating together, or `reserve_queries()` under-reserves and runs die mid-extraction. |
| 4 | **Deleting `rankings_pk.json` breaks live code and 3 tests.** | `extract_data.py:290-341` (`load_rankings_registry`, `lookup_registry`, `apply_registry_facts`) reads `config.rankings_json_path` → `resources/rankings_pk.json`. `tests/test_pipeline.py:685-687` asserts `lookup_registry("nust.edu.pk") is not None`. | Cannot just `rm`. Either merge the PK entries into the already-existing `resources/rankings_global.json` (which `universal_normalizer.py:42` already uses) and repoint `rankings_json_path`, or drop registry enrichment entirely. **Recommend: merge into `rankings_global.json`** — one global registry, zero behaviour lost. |
| 5 | **Qdrant is not confined to the two files the plan lists.** | ~230 lines of `inspect_cli.export_dataset()` (`src/inspect_cli.py:491-720`) interleave the `csv` / `qdrant` / `pinecone` / `json` export paths; `pipeline.py:443` calls `export_dataset(sync=settings["sync_qdrant"])`; `qdrant-client` is in `requirements.txt`; `QDRANT_URL`/`QDRANT_API_KEY` are in `.env.example`. | Removing the Config fields *before* untangling `export_dataset` leaves the tree broken. Qdrant excision must be one atomic commit spanning config + inspector + config.json + requirements + .env.example. Note Pinecone shares the embedding/chunking code path — do not delete it by accident. |
| 6 | **There is a circular import between the two modules being rewritten.** | `src/pipeline.py` imports `src.inspect_cli` (3 function-local imports: `iter_all_records`, `export_dataset`, `audit_analytics`); `src/inspect_cli.py:` imports `from src.pipeline import run_master_pipeline`. It only works today because the imports are inside functions. | The refactor must declare a direction. **Recommend: `inspector/` never imports `orchestrator` at module level; `orchestrator` may import `inspector`.** The `retry` command in `inspect_cli.py:437` keeps a lazy import. Decide this before writing `orchestrator.py`, not during. |
| 7 | **The normalizer fabricates data, which contradicts §5's quality rules.** | `universal_normalizer.py:88-140` writes `"Standard University Application Fee"`, `"Refer to Official Tuition Portal"`, `"Intermediate / HSSC (60% Minimum)"`, `"Matric (10%) + HSSC (40%) + Entry Test (50%)"` into empty fields. | These are Pakistan-specific invented values in a system the plan says now targets universities worldwide, and they make the §5 "empty/NaN fields audited by inspector" rule impossible — every empty field is pre-filled with a plausible lie. **Recommend: strip the fabrication, leave nulls, let the auditor report them.** Needs a user decision (see D2). |
| 8 | **Currency: the plan and the code actually agree — keep it that way.** | `resolve_universal_currency()` only *labels* currency, it never converts. §5 says keep original currency. | `normalizers/currency_tuition.py` must stay a labeller. Flagging because the filename invites someone to add conversion later. |
| 9 | **`tests/test_pipeline.py` has a name collision with the plan.** | The existing 33 KB `tests/test_pipeline.py` is a unit-test grab-bag over `extract_links`, `extract_data`, `ingest`, `state`, `schema`. The plan wants `tests/test_pipeline.py` to be the *end-to-end orchestration* suite. | Its contents must be split out into `test_extractor/`, `test_ingestor/`, `test_utilities/` first, and the root name freed, before the new e2e suite is written. |
| 10 | **Unlisted files that still need decisions.** | `cli.py` (root entrypoint), `pytest.ini`, `tests/conftest.py` (`sys.path` shim), `.gitignore`, `requirements.txt`, `.env.example`, `README.md` (14 KB), `COMMANDS.md` (23 KB, ~10 `config.json` references), `CHANGELOG.md`, `loggings/notebook_audit.jsonl` (1.3 MB to migrate), `src_summary.md` (40 KB, will be stale). | The plan's step 13 says only "update `AGENTS.md`". Docs are a real commit's worth of work, and `COMMANDS.md` documents every command that is about to be renamed. |
| 11 | **Log-based resume overlaps with existing SQLite resume.** | `state.py:34` already has `STATUS_SEQUENCE = (pending, crawled, ingested, extracted, completed)` plus `get_completed_slugs()`, and `pipeline.py` already skips completed universities. | `--resume s_42` must be defined *relative to* `state.sqlite`, not as a second source of truth. **Recommend: the log stores the run manifest (which universities, which settings, where it stopped); state.sqlite remains authoritative for per-university progress.** Needs a user decision (see D3). |
| 12 | **`config.py` carries dead path fields from an abandoned naming pass.** | `src_utils_dir`, `src_extraction_dir`, `extraction_linkers_dir`, `extraction_payloaders_dir`, `extraction_normalizers_dir`, `src_ingestion_dir`, `src_inspection_dir` — these use the *old* `extraction`/`ingestion`/`inspection` names the plan §1.3 explicitly rejected, and nothing reads them. | Delete them all. Source directories do not belong in a runtime config; only *data* paths do. |
| 13 | **`config.output_jsonl_path` points at a file that was just deleted.** | Field points to `data/outputs/university_counseling_data.jsonl`; that file is deleted in the working tree, and the aggregate now lives at `data/outputs/all_uni_outputs/universities_crawling_data.json`. | Repoint during the config commit or the first real run writes the aggregate back to the old location. |

### A.3 Methodology recommendation: strangler shims, not big-bang moves

The plan's execution order (move everything in steps 4–11, *then* "update imports" at step 12,
*then* "run tests" at step 14) leaves the repository non-importable for ten consecutive commits.
That defeats the stated purpose of committing each step for rollback — you cannot roll back to a
green commit if none of them are green.

**Every commit below leaves the tree importable and the test suite passing.** The mechanism:

1. Move the real code to its new home.
2. Leave the old flat file as a 2-line re-export shim: `from src.utilities.json_io import *  # noqa`.
3. Existing callers and tests keep working, untouched.
4. Update call sites gradually, in later commits.
5. Delete all shims in **one** late commit (C24), once nothing imports them.

This makes the refactor bisectable and abortable at any point.

---

## Part B — Decisions Needed Before Starting

D1 is answered. D2–D6 remain open, and none of them gate work before Phase 7 — each is surfaced
again at the commit it affects.

- ~~**D1 — Commit the data outputs?**~~ ✅ **ANSWERED: remove them.** User confirmed an external
  backup. All 21 data files were deleted from git and disk in C1, and `data/` is now ignored
  wholesale. Recoverable from tag `pre-refactor`. `state.sqlite` was verified empty (0 universities)
  before removal, so no stale "completed" state survives pointing at deleted outputs.
- ~~**D2 — Strip the normalizer's fabricated defaults?**~~ ✅ **ANSWERED: yes, strip.** (Finding 7.)
  User's reasoning, 2026-09-03: *"We will be running this pipeline not only on German and Pakistani
  Unis but all Unis. Do what is reliable and sustainable in this context."* Every fabricated default
  encoded an assumption about Pakistan or western Europe — the eligibility `else` branch handed the
  entire rest of the world Pakistan's HSSC formula, which 240 of 263 existing programmes carried,
  including eight East African nursing degrees. Implemented in C19 (`1660fb5`).
- ~~**D3 — What does `--resume s_42` actually resume?**~~ ✅ **ANSWERED: option A.** The log is a
  manifest plus audit trail; `state.sqlite` stays the single source of truth for per-university
  completion. Implemented and pinned by a test in C9. Option C was disqualified on a fact rather
  than taste: `state.sqlite` also holds the *daily* query ledger (`reserve_queries` /
  `queries_used_today`), which spans runs and cannot move into a per-run log without breaking
  quota enforcement across two runs on the same day. Still shapes C22.
- **D4 — Root entrypoint after `cli.py` dies.** `python -m src.orchestrator`, or keep a 5-line root
  `run.py`? *Recommendation: keep a thin root shim — every doc example and muscle-memory command
  starts with `python3 cli.py`.* Affects C22 and C28.
- **D5 — Supabase table schema.** Nothing Supabase-related exists in the codebase yet (no client, no
  credentials, no DDL). `inspector/sync.py` is fully greenfield and depends on the final program
  schema. *Recommendation: defer to the last commit (C29) and design the tables after the schema
  changes in C17–C18 settle.* Affects C29.
- **D6 — `rankings_pk.json`: merge into `rankings_global.json` or drop registry enrichment?**
  (Finding 4.) *Recommendation: merge.* Affects C25.

---

## Part B2 — Working Conventions (agreed in session; recorded so they survive a context compact)

- **Interpreter:** `/home/huzaifayaqob/miniconda3/envs/ise-env/bin/python`. The base conda python
  has no pytest.
- **Branch:** all refactor work lands on `refactor/modular-src`. `main` stays at `4d531f0`, which
  is also tagged `pre-refactor` — two independent ways back. Nothing has been pushed.
- **Commit trailers:** `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` only. The
  `Claude-Session:` trailer was deliberately dropped and stripped from the first 9 commits — it was
  redundant with commit bodies that carry their own rationale, and it would have written a permanent
  identifier into the history of a repo with a public-capable GitHub remote.
- **Every commit must leave the suite green.** Current count is recorded in the status banner above.
- **Move code, never retype it.** C12 introduced a real bug by hand-writing `_extract_id` during a
  split. Every split commit is now verified by AST comparison against the pre-split file: all
  top-level definitions must be present with byte-identical bodies, and any delta must be
  intentional and named in the commit message.
- **Two monkeypatch patterns, only one of which survives a shim:**
  - `setattr("mod.config.field", ...)` — patches an attribute on the shared `config` singleton.
    Safe through any import path, because it is one object.
  - `setattr("mod.some_function", ...)` — rebinds a module-level name. **Becomes a silent no-op
    through a shim**, because the caller resolves the name in its own module globals. This bit in
    C12: a pre-flight test stopped testing pre-flight while still passing. Audited clean as of C12;
    re-audit after C14, C15 and C20.

### Open items not yet owned by a commit

- ~~Add a test asserting no test patches a module-level name on a shimmed module.~~ **Done** —
  `d7cf157`. `tests/test_shim_hygiene.py` AST-walks `tests/` for string targets of
  `monkeypatch.setattr` / `mock.patch`, splits each at the longest importable module prefix and
  fails on a one-component remainder against a shim. Attribute reads *through* a shim
  (`src.state.config.state_db_path` → the shared singleton) stay allowed. Shims are discovered
  from docstrings, so C14/C15/C20 are covered automatically and the guard self-skips after C24.
- **Multi-account query budget (user proposal, 2026-09-03).** C17 took the suite to 6 queries, so
  6 × 83 = 498 against a 500/day cap — no retry headroom, and a full batch no longer fits in a day.
  The user's answer: **run two NotebookLM accounts and fail over to the second when the first is
  exhausted.** That turns `daily_query_budget` from one number into a pool, and touches
  `StateManager.reserve_queries` / `remaining_query_budget` (the ledger is currently global, not
  per-account), the NotebookLM client construction, and `Config`. **Not yet owned by a commit** —
  it belongs after the extractor phase, alongside C21's config/wiring work, and needs a decision on
  whether the ledger keys usage by account or just tracks which account is live.
- **Program NAME canonicalisation is still unowned.** Plan section 5 says degree names, program
  titles and essential identifiers "MUST be normalized via `normalizers/degree_names.py`". C17 did
  degree *levels* only. Canonicalising the titles themselves (`M.Phil.` / `MPhil` / `M Phil
  Management Sciences` → one form) is a real behaviour change and deserves its own commit.

- Decisions **D4, D5, D6** are still unanswered; each is due at the commit that needs it
  (D4 → C21, D5 → C29, D6 → C26). **D2 is answered** — see above.

---

## Part C — The Commit-by-Commit To-Do List

Legend: **Gate** = what must be green before the commit is made. Every commit runs the full suite
(`python -m pytest -q`) at minimum; the Gate names the *additional* specific check.

### Phase 0 — Stabilize and baseline (do not skip) — ✅ COMPLETE

- [x] **C0 — Fix the import-time crash.** — `81c8dcc`
  Fixed `ensure_directories()` and added `outputs_all_uni_outputs_dir`,
  `loggings_single_logs_dir`, `loggings_complete_logs_dir`. **Scope grew:** also renamed 11 stale
  `config.uni_outputs_dir` call sites in `src/pipeline.py`, `src/inspect_cli.py` and
  `tests/test_cli_interactive.py`, which the plan had not identified.
  **Gate met:** import clean, all 5 new directories materialise, **116 passed / 0 errors**.

- [x] **C1 — Commit the current working state.** — `e72210d`
  Applied D1: removed all 21 data files from git and disk, ignored `data/` wholesale (the tree is
  rebuilt by `ensure_directories()` at import, so no `.gitkeep` needed). Also untracked
  `loggings/notebook_audit.jsonl` — 1.3 MB of append-only log that the test run rewrites on every
  invocation, dirtying every diff; **the file stays on disk for the C26 migration**. Committed the
  planning docs and `src_summary.md`.
  **Gate met:** `git status` clean; suite held at 116 — no test depended on the removed data.

- [x] **C2 — Mark the rollback point.**
  Tagged `pre-refactor` at `4d531f0` (the true pre-refactor state) and branched
  `refactor/modular-src` before any commit landed, so `main` is untouched.
  **Gate met:** `git tag -l` shows `pre-refactor`; `git branch --show-current` is `refactor/modular-src`.

### Phase 1 — Scaffolding — ✅ COMPLETE

- [x] **C3 — Create empty packages.** — `7a0bbde`
  `src/{utilities,extractor,extractor/linkers,extractor/crawlers,extractor/normalizers,ingestor,inspector,logger}/__init__.py`,
  `tests/{test_utilities,test_extractor,test_ingestor,test_inspector,test_logger}/`,
  `loggings/{single_logs,complete_logs}/`, `resources/{plans,analysis}/`.
  Each `__init__.py` carries a purpose docstring encoding the two dependency rules:
  `utilities/` imports nothing from sibling packages, and `inspector/` must not import
  `orchestrator` at module scope (Finding 6). **Deviation:** added `__init__.py` to the `tests/`
  subpackages too — without them pytest requires globally unique test basenames, and four packages
  will each contribute a `test_runner.py`. Relocating existing docs into `resources/plans|analysis/`
  deferred to C28 so in-flight `resources/*.md` references keep resolving.
  **Gate met:** all 8 packages import; suite 116.

### Phase 2 — Utilities (leaf modules, zero internal dependencies — safest first) — ✅ COMPLETE

- [x] **C4 — `utilities/loaders.py`.** — `46d3bc6`
  `loaders.py` derives the project root from its own path rather than importing `config`, which
  would be circular (config calls `load_dotenv()` at module scope). The call now sits directly
  after `BASE_DIR` so `os.environ` is populated before any field `default_factory` reads a key.
  **Gate met:** 11 new tests covering behaviour that previously had none, incl. a guard that the
  loader does not creep back into `config.py`. Suite 116 → **127**.

- [x] **C5 — `utilities/json_io.py`.** — `0c3bda4`
  `git mv` so blame survives. Shim re-exports the private `_fsync_dir` / `_default_record_key`
  alongside `__all__` — a shim that silently narrows the namespace is a trap for future
  monkeypatching. `pipeline.py` deliberately left importing through the shim: it is rewritten
  wholesale in C22, so repointing it now is churn on code about to be deleted.
  **Gate met:** all json_io tests pass at the new path. Suite 127.

- [x] **C6 — `utilities/state_management.py`.** — `bd905c1`
  Shim re-exports `config` and `logger` explicitly: `tests/test_notebook_logger.py` monkeypatches
  the *string* target `"src.state.config.state_db_path"`, which only resolves if the shim exposes
  that attribute path. Extracted the 8 "B5.6 SQLite state machine" tests; those symbols appeared
  nowhere else in `test_pipeline.py`, so its dead import was dropped.
  **Gate met:** 8 state tests green at the new path; a real `state.sqlite` opens. Suite 127.

- [x] **C7 — `utilities/schema.py`.** — `43cacac`
  Pure move; taxonomy and field changes held for C17/C18. Unlike C6, the schema symbols are still
  used by the JSON-repair, extraction and rankings sections of `test_pipeline.py`, so that import
  block stays until C14/C15.
  **Gate met — adapted:** the planned fixtures were deleted in C1, so all 8 real payloads were
  recovered from tag `pre-refactor` and validated against the moved models. **8/8 pass** — a
  stronger check than the working-tree fixtures. Suite 127.

### Phase 3 — Logger — ✅ COMPLETE

- [x] **C8 — `logger/notebook_logger.py`.** — `f8e6ccb`
  **Deviation:** this shim uses explicit imports, not `import *`, and deliberately omits the
  `_logger_instance` singleton. `from X import name` binds by value, so a shim-level copy would
  freeze at `None` while the real module populated its own — anything resetting the singleton
  through the shim would silently fail. Verified `get_notebook_logger()` returns one shared
  instance through both paths.
  **Gate met:** notebook-logger tests pass at the new path. Suite 127.

- [x] **C9 — `logger/pipeline_logger.py` (new).** — `094e93e` Structured JSON run logs per plan §4:
  `s_{id}.json` / `c_{id}.json`, monotonic ID allocation (scan directory, take max+1, **allocate under
  a lock or an O_EXCL create** — two concurrent runs must not claim the same ID), atomic writes via
  `utilities.json_io.atomic_write_json`, and a reader that resolves a token like `s_42` back to a run
  manifest. Implements D3. **Not wired into the pipeline yet** — that is C22.
  New `tests/test_logger/test_pipeline_logger.py`: ID increments, ID collision under concurrency,
  malformed log file is skipped not fatal, `--resume` token parsing rejects garbage.
  **Gate met — 47 new tests.** Two bugs the tests caught in my own implementation:
  (a) `\d` matches *any* Unicode decimal digit, so `s_١٢٣` parsed and `int()` folded it onto
  `123` — two visually distinct tokens addressing one log file; tightened to `[0-9]`.
  (b) confirmed the deliberate `.strip()` so a shell-quoted or pipe-fed token resolves.
  ID allocation uses `O_CREAT|O_EXCL`, asserted by a 20-thread collision test; the id is claimed
  in `__init__` so an interrupted run stays resumable, and `__exit__` records the exception rather
  than leaving a run marked `running` forever. Suite 127 → **174**.

### Phase 4 — Config — ✅ COMPLETE

- [x] **C10 — Reorganize `Config`.** — `1df6109` Group into PATHS / CRAWLING LIMITS & THRESHOLDS / BROWSER POOL /
  HTTP POOL / INGESTION / QUERY & EXTRACTION / EXTERNAL APIS / LOGGING, **with an inline comment on
  every single field** (plan §2, non-negotiable). Delete the 7 dead `src_*_dir` fields (Finding 12).
  Repoint `output_jsonl_path` at `all_uni_outputs/` (Finding 13). Add the health-check knobs C13 will
  need (`health_check_sample_ratio = 0.10`, `health_check_min_sample = 5`). **Leave the Qdrant fields
  alone — they die in C11.**
  **Gate met:** `test_config.py` walks the source with `ast` to prove all 67 fields carry an
  inline comment, and rejects placeholder comments under 15 chars. **Extra fix:** `pipeline.py`
  derived the master JSON filename from `output_jsonl_path.parent`, which after the repoint would
  have written a wrongly-named file — added an explicit `output_master_json_path` and used it at
  the call site. `ensure_directories()` now covers 11 directories. Suite 174 → **184**.

### Phase 5 — Qdrant excision (one atomic commit — Finding 5) — ✅ COMPLETE

- [x] **C11 — Remove Qdrant everywhere at once.** — `00c1f4e`
  Delete `query_qdrant.py`, `src/qdrant_validator.py`. Strip the qdrant branch from
  `inspect_cli.export_dataset()` **while preserving the csv / pinecone / json paths and the shared
  chunking+embedding code**. Remove `sync_qdrant` from `config.json` and from the `pipeline.py:443`
  call site. Remove the 4 Qdrant `Config` fields, the `QDRANT_*` lines in `.env.example`, and
  `qdrant-client` from `requirements.txt`.
  **Gate met:** no `qdrant` match outside the planning docs; csv/pinecone/json all export;
  `cli.py export --help` offers exactly `{csv,pinecone,json}`. 86 lines of export/sync logic and
  349 lines of standalone files removed. The `sync` parameter went too — its only consumer was the
  Qdrant branch. **Coverage gap found:** `--format json` is the path `run_batch_pipeline` actually
  calls in production and had **no test at all**, which made this excision riskier than it looked;
  now covered, plus a guard that the removed format is gone from the CLI. Suite 184 → **186**.

- [x] **C11b — Remove Pinecone too** *(added mid-refactor at user request)* — `bf5ae3b`
  Supabase is the only destination, so a second vector store earns nothing. Deletes the whole
  vector-export path — 138 lines of chunk building, SentenceTransformer encoding and upsert — plus
  both Config credentials and the `pinecone-client` dependency. `inspect_cli.py` 1060 → 921 lines;
  `export` now offers `{csv, json}`.
  **Finding — config lied about the scorer.** Nothing in Phase 1 read the embedding fields:
  `classify_and_score_links` hardcodes `bge-small-en-v1.5` / `batch_size=64`, while Config declared
  `bge-**base**-en-v1.5` / `32` — a different model with a different vector dimension (768 vs 384).
  They were read only by the deleted exporter, so the C10 comment claiming they drive link scoring
  was wrong. Corrected to match reality and pinned by `test_embedding_config_matches_the_scorer`;
  **C14 must wire the scorer to read them.**
  **Side effect:** suite runtime 14s → 6s — the deleted Pinecone test loaded a sentence-transformer
  model on every run.
  Suite 186 → **187**.

### Phase 6 — Ingestor — ✅ COMPLETE

- [x] **C12 — Split `ingest.py` (20 KB) into `ingestor/`.** — `2c5502e`
  `notebook_lifecycle.py` (`_find_or_create_notebook`, deletion) · `source_management.py`
  (`sanitize_url`, `check_url_accessible`, `fetch_and_extract_text`, `IngestedSource`, `IngestResult`,
  `ingest_university_sources`) · `quota_management.py` (budget reservation) ·
  `readiness_polling.py` (`wait_for_sources_adaptive`, the jittered backoff) · shared HTTP client
  helpers. Shim `src/ingest.py`. `tests/test_ingest_resilience.py` → `tests/test_ingestor/`.
  **Gate met:** the four ingest-resilience tests pass at the new import paths. Five modules:
  `http_client.py` (105) · `notebook_lifecycle.py` (47) · `source_management.py` (309) ·
  `quota_management.py` (30) · `readiness_polling.py` (102). **Deviation:** the plan named four
  modules; the shared HTTP/2 pool became a fifth rather than being duplicated.
  **Two bugs I introduced and had to fix — both from retyping instead of moving:**
  (1) I hand-wrote `_extract_id` in `readiness_polling.py`, dropping its `isinstance(obj, str)`
  branch and changing the attribute list. Bare-string SDK returns became upload failures —
  4 tests red. Restored verbatim from `git show HEAD:src/ingest.py`. **This is where the
  "move code, never retype it" rule in Part B2 comes from.**
  (2) `test_ingest_resilience.py` patched `src.ingest.check_url_accessible` — the *shim* — which
  rebinds the shim's global while the caller resolves its own. The pre-flight test silently
  stopped testing pre-flight **while still passing**. Repointed at
  `src.ingestor.source_management.*`; now caught automatically by `d7cf157`.
  `refactor(ingestor): split ingest.py into lifecycle/sources/quota/readiness`

- [x] **C13 — Pre-flight link health sampling (new, plan §6).** — `f46f51c`
  Before committing a full source batch: sample `max(health_check_min_sample,
  ceil(ratio * len(links)))` links at random; if the majority fail acceptance/extraction, skip the
  university and log the failure rather than burning the batch. Isolate individual failures without
  discarding successes; never exceed `max_query_retries`; reserve the full per-university query
  budget before any query runs.
  New `tests/test_ingestor/test_health_sampling.py`: majority-fail → skip + logged; majority-pass →
  full batch proceeds; sample floor honoured on tiny link sets; a single mid-batch failure does not
  discard the successful sources.
  **Gate met:** 15 new tests in `tests/test_ingestor/test_health_sampling.py`; quota accounting
  verified against `StateManager.reserve_queries` (a refused reservation is not partially applied).
  New `src/ingestor/health_sampling.py`.
  **Design notes worth keeping:**
  - The probe is **injected**, not imported — `source_management` both owns the real probe and
    consumes this module, so importing would be circular. It also lets the unit tests pass a plain
    function rather than monkeypatching a module attribute (the C12 hazard).
  - Sampling is **random, not head-of-list**: link lists arrive tier-ordered, so the head is
    systematically healthier than the batch and would clear a university with a dead tail.
  - `run_health_check` returns a verdict rather than raising, so the caller keeps one return path.
    A probe that raises counts as one dead link, not a failed check.
  - Sample size is `ceil(ratio · n)` floored at the minimum and **clamped to the population**, so a
    3-link university gets 3 probes, not an impossible request for 5.
  **Scope grew (all justified by the plan's intent):**
  - `ingest_university_sources` reordered to normalise → health check → **create notebook**. It
    previously provisioned the notebook first, which would have orphaned one per skipped university.
  - `IngestResult` gained `skipped` / `skip_reason` / `health`; `notebook_id` now defaults to `""`.
  - The empty-link-set early return became a skip — it used to provision a notebook and return zero
    sources.
  - Sampled verdicts carry over into the full pre-flight pass, so the sample costs no extra fetches.
  - `pipeline.py` branches on the verdict instead of writing an `ingested` status with an empty
    `notebook_id`.
  **Rest of plan §6 audited, not changed:** failure isolation (now pinned by a test), notebook-level
  failure → skip + log (`pipeline.py:279`), the `max_query_retries` cap (`extract_data.py:513`), and
  the up-front `queries_per_university` reservation, which already ran before any query.
  Suite 187 → **202** (→ **206** with `d7cf157`).
  `feat(ingestor): pre-flight link health sampling before quota spend`

### Phase 7 — Extractor (largest phase; moves first, behaviour changes after)

- [x] **C14 — Split `extract_links.py` (61 KB / 26 functions) into `extractor/linkers/`.** — `99402d1`, `5cc574a`, `1135804`
  `crawling.py` (`build_browser_config`, `get_shared_crawler`, `close_shared_crawler`, `browser_pool`,
  `crawl_site_links`, `CrawlFailure`) · `filteration.py` (`is_excluded_path`, `sanitize_url`,
  `normalize_url`, `preprocess_and_filter_links`, `compute_year_decay_factor`) ·
  `deduplication.py` (`dedupe_key`, `deduplicate_canonical_degree_links`) ·
  `semantic_scoring.py` (`_get_embedding_model`, `classify_and_score_links`,
  `allocate_proportional_tier_quotas`, `get_discipline_tokens`) · `runner.py` (`run_pipeline`,
  `export_dual_outputs`, `export_partitioned_links`, `load_partitioned_links`, `slugify_university`).
  Shim `src/extract_links.py`. Split the P1 tests out of `tests/test_pipeline.py` into
  `tests/test_extractor/test_linkers_*.py`.
  **Carried over from C11b:** wire `semantic_scoring.py` to read `config.embedding_model_name` and
  `config.embedding_batch_size` instead of hardcoding them, so the pinning test becomes a real
  contract rather than a drift guard.
  **Gate met:** all 60 P1 tests pass at the new import paths, and a differential probe of 8
  function families (normalize_url, is_excluded_path, dedupe_key, compute_year_decay_factor,
  get_discipline_tokens, slugify_university, preprocess_and_filter_links,
  allocate_proportional_tier_quotas) produces **byte-identical JSON** against a `pre-refactor`
  worktree. Split across **three commits** so a bisect can separate the move from the behaviour
  change — the lesson from C12.

  **`99402d1` — the pure move.** 1412 lines / 61 definitions → six modules, verified by AST diff
  (all 61 present exactly once, byte-identical bodies). Only intentional difference: `BASE_DIR`
  moved three directories deeper, `parent.parent` → `parents[3]`, asserted to resolve identically.
  - **The plan's module assignment does not import.** It filed `dedupe_key` under `deduplication`
    and `get_discipline_tokens` under `semantic_scoring`, producing
    `filteration → deduplication → semantic_scoring → filteration`. Following actual callers fixes
    it: `dedupe_key` is used only by `preprocess_and_filter_links`, `get_discipline_tokens` only by
    `deduplicate_canonical_degree_links`. Package is now a strict DAG:
    `constants ← filteration ← deduplication ← semantic_scoring ← runner`, `constants ← crawling ← runner`.
  - **Added a 6th module, `constants.py`** (the plan named five) — ~25 shared constants and regexes
    would otherwise have to be duplicated or create cycles. It also carries the `logging.basicConfig`
    import-time side effect verbatim; every module imports constants so it still fires once at the
    same point. **That belongs in a real logging setup — worth folding into C21/C28.**
  - Shim omits `_SHARED_CRAWLER`, `_SHARED_CRAWLER_LOOP`, `_EMBEDDING_MODEL` (rebindable globals —
    the C8 `_logger_instance` rule) and keeps a `__main__` guard so `python -m src.extract_links`
    still runs.

  **`5cc574a` — the C11b obligation, discharged.** `_get_embedding_model` and
  `classify_and_score_links` now read `config.embedding_model_name` / `embedding_batch_size`.
  The C11b pinning test only compared *source text*; replaced by
  `tests/test_extractor/test_linkers_semantic_scoring.py`, which observes what the scorer actually
  asks for through a fake model and asserts that **changing** either config value changes the call.
  No real model is loaded, so the suite stays ~7s. Also covered: single load per process, and the
  bge query prefix on the keyword side only.

  **`1135804` — tests moved** out of the `test_pipeline.py` grab-bag into
  `test_linkers_{filteration,deduplication,quotas,runner}.py`, importing the real modules instead of
  the shim. `test_pipeline.py` 677 → 337 lines; nine dead imports removed.

  Suite 206 → **212**.
  `refactor(extractor): split extract_links into linkers subpackage`

- [x] **C15 — Split `extract_data.py` (27 KB) into `extractor/crawlers/`.** — `c009a85`, `a178cf7`
  725 lines → four modules, exactly as planned. `json_repairing.py` (252) · `notebook_querying.py`
  (234) · `exa_enriching.py` (55) · `runner.py` (240). Shim `src/extract_data.py` (61 lines,
  30 names, omits the mutable `_RANKINGS_CACHE`).

  **Deviation:** no `constants.py` was needed, unlike C14. The only name shared across all four
  modules was `logger`, and each module now calls `logging.getLogger("ExtractData")` directly —
  same logger object, no shared module.

  DAG: `json_repairing ← notebook_querying ← runner`; `exa_enriching ← runner`.

  **Gates, all met:** AST diff 32/32 top-level definitions present exactly once with byte-identical
  bodies · differential probe against the `pre-refactor` worktree byte-identical · JSON-repair
  battery passes unchanged.

  `a178cf7` moved the Phase 3 tests out into `test_crawlers_{json_repairing,runner}.py` and
  **deleted `tests/test_pipeline.py`**, which had by then been emptied of everything it once held.
  Suite 212 → **212**.
  `refactor(extractor): split extract_data into crawlers subpackage`

- [x] **C16 — Split `universal_normalizer.py` into `extractor/normalizers/`.** — `ca6fe00`, `1855cda`
  `currency_tuition.py` (92) · `eligibility.py` (43) · `degree_names.py` (8, scaffold) ·
  `runner.py` (109). Shim `src/universal_normalizer.py` (33 lines, omits the mutable
  `_GLOBAL_REGISTRY`). DAG: `currency_tuition, eligibility ← runner`.

  **This one is not a pure move.** `normalize_universal_program` was a single 50-line function doing
  two unrelated jobs; it is now an extract-method into `apply_currency_and_tuition` and
  `apply_eligibility_defaults`, with the old function reduced to sequencing them. An AST diff cannot
  vouch for a body that changed shape, so `ca6fe00` landed **61 characterization tests first** —
  the module had zero coverage despite sitting on a production read path (`inspect_cli` normalizes
  every payload it yields).

  `GLOBAL_FACTS_FILE` moved one directory deeper: anchor is now `parents[3]`, not `parent.parent`.
  Verified to resolve to the same `resources/rankings_global.json`.

  **Gate, met:** `normalize_universal_payload` over all 8 real payloads from the `pre-refactor` tag,
  pre-split vs post-split — byte-identical on all 8, and identical again routed through the shim.
  Suite 212 → **273**.

  **Carried to C19:** the characterization tests mark every fabricated value with a `FABRICATED
  (D2/C19)` comment — invented tuition pointers, application fees, Abitur/GPA eligibility, a
  specific weighted admission formula, country-derived accreditation bodies, and hardcoded
  LMU/ITU/NUST founding years matched by *name substring* (so any university whose name contains
  "itu" inherits ITU Lahore's 2012). That set of assertions is C19's checklist.
  `refactor(extractor): split universal_normalizer into normalizers subpackage`

- [x] **C17 — Degree taxonomy: 4 levels (behaviour change, Findings 1 + 3).** — `ad689b4`
  `DegreeLevel` and `ProgramCategoryBlock` now carry the **same four strings** —
  `bachelors` / `masters` / `phd` / `diploma` — so a bucket name *is* its level and nothing has
  to be translated. Pre-C17 the enum value (`postgraduate_phd`) did not even match its own bucket
  name (`postgraduate_and_phd`).

  `QUERY_SUITE` 5 → 6 (diploma query added, three programme prompts rewritten);
  `queries_per_university` 5 → 6, pinned to `len(QUERY_SUITE)` by test because `reserve_queries()`
  claims that number up front and a suite that outgrew it would silently overrun the 500/day cap.

  **Budget re-derived, and it no longer clears:** 6 × 83 = 498 against a 500 cap — no retry
  headroom at all (the 5-query suite had 85). `config.py` records that **batch sizing** is what has
  to give; `daily_query_budget` is a real external quota, not a knob. **A full 83-university batch
  no longer fits in one day.**

  `normalizers/degree_names.py` is the deterministic mapper. It tokenises rather than
  substring-matches — the same lesson the link filter learned — because of two real strings:
  `"Post-RN Bachelor of Science in Nursing"` (starts with "Post", is a bachelors) and
  `"Doctor of Physical Therapy (DPT)"` (says "Doctor", is a bachelors). Entry-level professional
  doctorates resolve before the PhD rule, but an explicit `phd` marker vetoes that, or
  `"PhD in Pharmacy Practice"` comes out a bachelors. An unreadable name returns `None` rather than
  defaulting — guessing a level fabricates a fact a student could act on.

  **Two call sites, chosen to keep the DAG honest.** The normalizer runs `apply_degree_level` over
  every programme, where the **name outranks the declared level**. The schema coerces retired
  *values* only, with its own self-contained table: `utilities` is the leaf layer and must not
  import upward, even lazily.

  **Backward compatibility mattered more than expected:** `inspect_cli` re-normalizes every record
  it reads, so retired bucket names are folded into the canonical four (merged, not assigned) and
  retired enum values coerce. Without that, every university extracted before this commit would
  report **zero programmes** through the inspector's audits, search and CSV export.

  **Deviation (beyond the planned scope):** the rename reached further than the plan listed —
  `inspect_cli` (tables, comparison, search, CSV export, analytics), the pipeline run summary, and
  linkers' `DEGREE_LEVEL_TOKENS`, where `"pgd"` moved out of the masters set into its own diploma
  level. That last one was a live bug: a "PGD in Data Science" link was deduping against an
  "MS in Data Science" link and one was being dropped.

  **Gates, all met:** 40 real degree strings from the 8 `pre-refactor` payloads each map to exactly
  one level, none falling through · all 263 programmes in those payloads classify, 0 unresolved ·
  full 8-payload normalizer diff pre-C17 vs post-C17 shows no change to `main_info`, `contact`,
  `faculties` or the truncation flag, no count or order drift, and no field change other than
  `degree_level` — 260 the straight rename, 3 real corrections (`Doctorate of Physical Therapy`;
  `PHD Electrical Engineering` and `PHD Computer Science`, both filed as masters by the extractor) ·
  `university_payload_schema.json` regenerated and asserted against.

  Note the committed schema JSON was **stale by more than this change** — it predated
  `established_year`, `accreditation_body`, `admission_cycles_offered` and several other `MainInfo`
  fields. The regeneration picks those up too.

  Suite 277 → **338**.
  `feat(schema): replace 3-tier degree levels with Bachelors/Masters/PhD/Diploma`

- [x] **C18 — Per-program required fields (behaviour change, Finding 2).** — `c9e192a`
  `ProgramItem` now carries every field plan §5 lists as required, pinned by a parametrised test
  driven off a list rather than prose so a future field cannot be quietly dropped.

  - **`description`** (`Optional[str]`) — the full-paragraph overview §5 asks for. Supersedes
    `summary_3_lines`, which is kept as a deprecated field (every pre-C18 payload carries it and
    `inspect_cli` re-validates those on read) but is **no longer requested by any prompt**.
  - **`admission_requirements`** (`Optional[str]`) — process and documents, distinct from the raw
    marks in `eligibility_requirements`.
  - **`application_deadline` → `application_deadlines: List[str]`** — §5 says "deadline(s)"; a
    programme with Fall and Spring intakes has two, and the singular field forced one to be dropped.
    The retired singular key is accepted as a Pydantic **validation alias**, without which a pre-C18
    programme's only deadline was silently dropped at validation.

  Both new fields default to **`None`, not to prose** — a programme page that genuinely says nothing
  normalizes to nothing. That is the C19 posture, adopted early for the fields C18 introduces.

  `normalizers/program_fields.py` (new, 5th step in `normalize_universal_program`) carries pre-C18
  values onto the new fields. It **never invents**: `description` is filled from `summary_3_lines`
  only when that is real text, explicitly *not* when it is the schema's
  `SUMMARY_FALLBACK` stand-in — C19 removes that placeholder, and propagating it into a second
  field would have made C19's job worse. The fallback string was extracted to a named constant so
  the two modules cannot disagree about what it is.

  Also updated: all four programme prompts (a shared `_PROGRAM_FIELD_NOTE` calls out that
  `description` must be a paragraph, since it is the field a model most readily skimps on) and
  `inspect_cli` (new `format_deadlines()` helper, CSV column renamed, search now reads `description`
  and falls back to the retired summary).

  **Gates, all met:** all 8 `pre-refactor` payloads normalize **and validate against the new model**
  — 263/263 programmes came out with a `description`, deadlines carried wherever one was published ·
  `UniversityPayload.model_validate(payload.model_dump()) == payload` round-trips · a recorded real
  fixture (`pre_c18_programs.input.json`, three ITU programmes in their original pre-C18 shape) is
  the committed regression case · full C17→C18 payload diff shows **only** the three intended field
  changes: 263 `description` filled, 263 `application_deadlines` filled, 147 `application_deadline`
  removed. Nothing in `main_info`, `contact`, `faculties` or the truncation flag moved.

  Suite 338 → **361**.
  `feat(extractor): request and model full per-program field set`

- [x] **C19 — Strip fabricated normalizer defaults.** *(D2 = yes; Finding 7.)* — `1660fb5`
  **The deciding argument was scope.** Worldwide corpus, Pakistan/western-Europe assumptions. The
  eligibility rules had three branches — western Europe, the anglophone countries, and an `else`
  that gave **everywhere else** Pakistan's `"Intermediate / HSSC (60% Minimum)"` and
  `"Matric (10%) + HSSC (40%) + Entry Test (50%)"`. **240 of the 263 programmes in the corpus
  carried that formula**, including eight AKU nursing/midwifery degrees from Kenya, Tanzania and
  Uganda — none of which have an HSSC. It was also the most dangerous value the pipeline emitted:
  a specific admission calculation, unsourced, that a student could plan around.

  **Removed from the normalizer:** the tuition and application-fee strings (including the claim that
  four European countries are `"Tuition Free (Semester Contribution applies)"`) · the
  country-selected eligibility criteria and formulae · `"Ministry of Higher Education (<country>)"` ·
  the three name-substring-matched universities · `country` defaulting to Pakistan ·
  `primary_instruction_language` defaulting to English.

  **Removed from the schema, same reasoning one layer down:** `country="Pakistan"`,
  `primary_instruction_language="English"`, `type=PUBLIC`, `currency="PKR"`,
  `delivery_mode="On-Campus"`, `application_status=ROLLING`, `intake_terms=["Fall"]`,
  `admission_cycles_offered=["Fall","Spring"]` (northern-hemisphere naming, wrong for anywhere
  running Semester 1/2 from February), and the stand-in summary.

  **Removed from the prompts:** Q1 and the programme template showed `"English"` and `"On-Campus"`
  as example values, which primes the model to answer them.

  **What survives, because it is evidence and not invention:** currency read from the fee text ·
  currency from a known country (a *label* for a figure published in that country's currency — the
  map grew 18 → 50 entries and now returns `None` outside them, instead of falling through to PKR
  for every country on earth outside Europe) · the sourced registry lookup, which supplies exactly
  the identity facts the hardcoded branch was faking · turning the extractor's `"null"` / `"N/A"` /
  `""` into real nulls so the empty-field audit can see them.

  **Bug fixed in passing** (in code this commit was already rewriting): currency markers were
  substring-matched against an uppercased fee string, so `"RS" in s` hit inside **COURSE** and
  labelled any fee mentioning a course as PKR. Now word-boundary matched — the *third* appearance of
  the substring-versus-token defect, after the link filter and the degree mapper. Also, the HEC
  directory scrape was writing `"Pakistan"` into a **city** field.

  **`tests/test_extractor/test_no_fabrication.py` is the standing guard:** every removed string
  checked against the string literals of every module under `src/` (parsed via `ast`, so the
  comments *explaining* the removal do not trip it), plus one assertion per field that it does not
  default to a claim. The single allowed survivor is the retired summary placeholder — recognised so
  the normalizer can refuse to carry it into `description`, never written.

  **Gates, all met:** full 8-payload diff C18 → C19 shows only the intended removals — 230
  application fees, 113 tuition pointers, 240 eligibility blocks, 1 accreditation body — and nothing
  else moved · a record of pure nulls still **validates**, because an audit can only report an empty
  field on a record it could load.

  Suite 374 → **401**.
  `fix(normalizers): stop fabricating values for missing fields`

- [ ] **C20 — Split `inspect_cli.py` (52 KB) into `inspector/`.**  ← **NEXT**
  `dashboard.py` (`inspect_university`, `compare_universities`, `search_programs`,
  `interactive_menu`, `inspect_state`, `inspect_schema`, `inspect_notebooks`) · `auditor.py`
  (empty/NaN field audits, coverage) · `analytics.py` (`audit_analytics`, `iter_all_records`,
  `load_all_records`, `find_university_record`, distributions) · export/`sync.py` seam.
  **Break the `pipeline` ↔ `inspect_cli` cycle here per Finding 6** — `retry_pipeline` keeps a lazy
  import of the orchestrator; nothing in `inspector/` imports it at module level.
  Shim `src/inspect_cli.py`. `tests/test_cli_interactive.py` → `tests/test_inspector/`.
  **Gate:** `python -c "import src.inspector"` with no circular-import error; CLI interactive tests pass.
  `refactor(inspector): split inspect_cli into dashboard/auditor/analytics/sync`

- [ ] **C21 — Auditor rules for the new schema.**
  Empty/NaN reporting across the §5 required per-program fields; degree-level distribution counts;
  a hard "not ready for Supabase" verdict when coverage is below threshold.
  **Gate:** auditing a deliberately gappy fixture reports exactly the missing fields.
  `feat(inspector): audit per-program required-field coverage`

### Phase 9 — Orchestrator

- [ ] **C22 — Write `src/orchestrator.py`, retire `pipeline.py`.**
  Thin orchestration only — **if it starts accumulating logic, that logic belongs back in a module**
  (plan §1 note 4). Absorbs `run_master_pipeline`, `run_batch_pipeline`, `compile_master_json`,
  `derive_uni_info`, `backup_existing_outputs`, `generate_result_analytics`. Wires `pipeline_logger`
  (C9) and implements `--resume s_42` / `--resume c_7` per D3. Applies D4 for the entrypoint.
  `src/pipeline.py` becomes a shim.
  **Gate:** a dry-run batch over 2 universities completes and writes a `c_{id}.json`; `--resume`
  against that log resumes at the right university.
  `refactor(orchestrator): replace pipeline.py with thin orchestration entrypoint`

- [ ] **C23 — `config.json` → `run_settings.json`.**
  Rename the file; update the defaults in the orchestrator arg parser and the inspector `batch`
  subcommand; `sync_qdrant` is already gone from C11.
  **Gate:** `grep -rn "config\.json" src/ tests/` returns nothing.
  `refactor: rename config.json to run_settings.json`

### Phase 10 — Cleanup and cutover

- [ ] **C24 — Delete every shim; rewrite every import.**
  The one deliberately large commit. Remove `src/{extract_links,extract_data,ingest,inspect_cli,json_io,state,schema,notebook_logger,universal_normalizer,pipeline}.py`
  and `cli.py` (per D4). Every remaining import points at the real module.
  **Gate:** `grep -rn "^from src\.\(extract\|ingest\|inspect_cli\|json_io\|state\|schema\|notebook_logger\|universal_normalizer\|pipeline\) " src/ tests/` empty; full suite green.
  `refactor: remove compatibility shims and flat modules`

- [ ] **C25 — Registry consolidation** *(per D6; Finding 4).*
  Merge `rankings_pk.json` entries into `rankings_global.json`, repoint `config.rankings_json_path`,
  delete `rankings_pk.json`, fix the `test_pipeline.py:685-687` assertions.
  **Gate:** `lookup_registry("nust.edu.pk")` still resolves after the merge.
  `refactor(resources): consolidate rankings registries into rankings_global.json`

- [ ] **C26 — Migrate `notebook_audit.jsonl` (1.3 MB) into the new logging structure.**
  Convert to the JSON log format under `logger/notebook_logger.py`; delete `loggings/.gitkeep`.
  **Gate:** the migration is idempotent; no audit record is lost (count before == count after).
  `refactor(logger): migrate notebook audit trail to structured JSON logs`

- [ ] **C27 — Finalize the test tree.**
  Confirm the mirror is complete (`test_utilities/`, `test_extractor/`, `test_ingestor/`,
  `test_inspector/`, `test_logger/`), then write the **new** `tests/test_pipeline.py` as a genuine
  end-to-end orchestration suite (Finding 9) — full run over a stubbed NotebookLM, resume from log,
  health-check skip path, Supabase gate blocked by a failing audit. Update `conftest.py` / `pytest.ini`.
  **Gate:** the e2e suite passes; total test count ≥ the C0 baseline.
  `test: mirror module structure and add end-to-end pipeline suite`

- [ ] **C28 — Documentation.**
  `AGENTS.md` (new structure), `README.md` (tree + quick start), `COMMANDS.md` (~10 `config.json`
  references, every renamed command, **and the `qdrant`/`pinecone` export formats removed in
  C11/C11b that it still documents**), `CHANGELOG.md` (the refactor entry), regenerate or delete
  the now-stale `src_summary.md`, refresh `.env.example`.
  **Gate:** every command shown in `COMMANDS.md` actually runs.
  `docs: update all documentation for modular architecture`

- [ ] **C29 — `inspector/sync.py`: Supabase** *(per D5 — last, greenfield).*
  Design the tables against the final C17/C18 schema, add the client + credentials, and gate the push
  behind a passing C21 audit — local DB → inspect → validate → **only then** push.
  **Gate:** a dry-run sync against a Supabase branch; the push refuses to run when the audit fails.
  `feat(inspector): Supabase sync gated on passing data audit`

---

## Part D — Sequencing Notes

- **C0 is not optional and not deferrable.** There is no baseline until it lands.
- **Phases 2–3 are pure leaf moves** and can be done fast with high confidence.
- **C11 (Qdrant), C17 (taxonomy), and C24 (shim removal) are the three risky commits.** Each deserves
  its own review pass. Everything else is mechanical.
- **Behaviour changes are quarantined from moves.** C13, C17, C18, C19, C21, C29 change what the
  system *does*; every other commit only changes where code *lives*. Never mix the two in one commit —
  it makes a bisect useless.
- **The three most valuable existing test groups** — JSON repair (`extract_data`), state transitions
  (`state.py`), and ingest resilience — are the regression net for this entire refactor. Never let a
  commit land with any of them red.
- **`orchestrator.py` staying thin is a real risk.** The plan itself flags it (§1 note 4) while
  `changes_to.txt` predicts it "would be the largest file". Set a soft ceiling (~300 lines); if it
  exceeds that, logic has leaked out of a module and belongs back inside one.
