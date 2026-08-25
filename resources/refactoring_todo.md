# Unicrawling Refactor — Engineering Analysis & Commit-by-Commit To-Do

Companion to [refactoring_plan.md](./refactoring_plan.md). That document is the *what*.
This document is the *how*, in the order it must actually happen, with a test gate on every commit.

**Status: IN PROGRESS.** Phases 0–5 complete (C0–C11). Work is on branch
`refactor/modular-src`; `main` remains at `4d531f0` and the tag `pre-refactor` marks that same
commit. **Baseline: 116 at C0, now 186** (C4 +11 loaders, C9 +47 logger, C10 +10 config, C11 +2 export). Every commit must hold the current count.
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
- **D2 — Strip the normalizer's fabricated defaults?** (Finding 7.) *Recommendation: yes, strip; nulls
  are honest and auditable.* Affects C19.
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

### Phase 6 — Ingestor  ← **NEXT**

- [ ] **C12 — Split `ingest.py` (20 KB) into `ingestor/`.**
  `notebook_lifecycle.py` (`_find_or_create_notebook`, deletion) · `source_management.py`
  (`sanitize_url`, `check_url_accessible`, `fetch_and_extract_text`, `IngestedSource`, `IngestResult`,
  `ingest_university_sources`) · `quota_management.py` (budget reservation) ·
  `readiness_polling.py` (`wait_for_sources_adaptive`, the jittered backoff) · shared HTTP client
  helpers. Shim `src/ingest.py`. `tests/test_ingest_resilience.py` → `tests/test_ingestor/`.
  **Gate:** existing ingest-resilience tests pass unchanged at the new import paths.
  `refactor(ingestor): split ingest.py into lifecycle/sources/quota/readiness`

- [ ] **C13 — Pre-flight link health sampling (new, plan §6).**
  Before committing a full source batch: sample `max(health_check_min_sample,
  ceil(ratio * len(links)))` links at random; if the majority fail acceptance/extraction, skip the
  university and log the failure rather than burning the batch. Isolate individual failures without
  discarding successes; never exceed `max_query_retries`; reserve the full per-university query
  budget before any query runs.
  New `tests/test_ingestor/test_health_sampling.py`: majority-fail → skip + logged; majority-pass →
  full batch proceeds; sample floor honoured on tiny link sets; a single mid-batch failure does not
  discard the successful sources.
  **Gate:** new tests pass; quota accounting verified against `StateManager.reserve_queries`.
  `feat(ingestor): pre-flight link health sampling before quota spend`

### Phase 7 — Extractor (largest phase; moves first, behaviour changes after)

- [ ] **C14 — Split `extract_links.py` (61 KB / 26 functions) into `extractor/linkers/`.**
  `crawling.py` (`build_browser_config`, `get_shared_crawler`, `close_shared_crawler`, `browser_pool`,
  `crawl_site_links`, `CrawlFailure`) · `filteration.py` (`is_excluded_path`, `sanitize_url`,
  `normalize_url`, `preprocess_and_filter_links`, `compute_year_decay_factor`) ·
  `deduplication.py` (`dedupe_key`, `deduplicate_canonical_degree_links`) ·
  `semantic_scoring.py` (`_get_embedding_model`, `classify_and_score_links`,
  `allocate_proportional_tier_quotas`, `get_discipline_tokens`) · `runner.py` (`run_pipeline`,
  `export_dual_outputs`, `export_partitioned_links`, `load_partitioned_links`, `slugify_university`).
  Shim `src/extract_links.py`. Split the P1 tests out of `tests/test_pipeline.py` into
  `tests/test_extractor/test_linkers_*.py`.
  **Gate:** every P1 test passes; one live-ish crawl smoke test produces the same link count as before.
  `refactor(extractor): split extract_links into linkers subpackage`

- [ ] **C15 — Split `extract_data.py` (27 KB) into `extractor/crawlers/`.**
  `notebook_querying.py` (`QuerySpec`, `QUERY_SUITE`, `_ask`, `run_query`, `ExtractionReport`) ·
  `exa_enriching.py` (`exa_find_application_portal`) · `json_repairing.py`
  (`strip_citation_markers`, `_balanced_span`, `extract_json_str`, `repair_and_validate_json`,
  adapters) · `runner.py` (`extract_university_payload`, registry facts, notebook deletion).
  Shim `src/extract_data.py`. Tests → `tests/test_extractor/test_crawlers_*.py`.
  **Gate:** the JSON-repair test battery (the highest-value tests in the suite) passes unchanged.
  `refactor(extractor): split extract_data into crawlers subpackage`

- [ ] **C16 — Split `universal_normalizer.py` into `extractor/normalizers/`.**
  `currency_tuition.py` (`resolve_universal_currency`, tuition handling — **labels currency, never
  converts**, Finding 8) · `eligibility.py` (eligibility/requirement consolidation) ·
  `degree_names.py` (**new, empty scaffold** — filled in C17) · `runner.py`
  (`normalize_universal_payload`, `load_global_registry`).
  Shim `src/universal_normalizer.py`.
  **Gate:** normalizing an existing `uni_outputs/*.json` file produces byte-identical output to
  pre-commit (capture a golden file first).
  `refactor(extractor): split universal_normalizer into normalizers subpackage`

- [ ] **C17 — Degree taxonomy: 4 levels (behaviour change, Findings 1 + 3).**
  `DegreeLevel` → `bachelors` / `masters` / `phd` / `diploma`; `ProgramCategoryBlock` keys renamed;
  **add a 6th `diploma` query** to `QUERY_SUITE` and rewrite the three existing program prompts to the
  new level names; bump `queries_per_university` 5 → 6 and re-derive `daily_query_budget`; update the
  plan §6.5 "5-query budget" wording. Implement `normalizers/degree_names.py`: map every observed
  degree string (BS/BSc/BA/BBA/BE/B.Ed/MBBS/LLB/PharmD → bachelors; MS/MSc/MA/MBA/MPhil/M.Ed/LLM →
  masters; PhD/Doctorate → phd; PGD/Diploma/Certificate → diploma) to exactly one canonical level.
  **Post-doctoral is excluded entirely** (plan §1 note 5).
  New `tests/test_extractor/test_degree_names.py` with a table of real degree strings from the existing
  outputs; assert every program maps to exactly one of the 4 and that nothing lands in a fallback bucket.
  **Gate:** new taxonomy tests pass; `university_payload_schema.json` regenerated and validating.
  `feat(schema): replace 3-tier degree levels with Bachelors/Masters/PhD/Diploma`

- [ ] **C18 — Per-program required fields (behaviour change, Finding 2).**
  Add to `ProgramItem`: full-paragraph `description` (focus areas, learning outcomes, career
  prospects, distinctive features — replacing/superseding `summary_3_lines`), `admission_requirements`,
  and deadline handling. Rewrite `_PROGRAM_STRUCTURE` and the program prompts to request all of §5's
  required fields. Regenerate `university_payload_schema.json`.
  **Gate:** schema round-trips; one real NotebookLM query (or a recorded fixture) returns a payload
  that validates against the new model.
  `feat(extractor): request and model full per-program field set`

- [ ] **C19 — Strip fabricated normalizer defaults** *(only if D2 = yes; Finding 7).*
  Remove the invented `"Standard University Application Fee"` / `"Refer to Official Tuition Portal"` /
  HSSC-aggregate fallbacks; leave nulls for the auditor to report.
  **Gate:** a test asserts a program with no published fee normalizes to `None`, not to prose.
  `fix(normalizers): stop fabricating values for missing fields`

### Phase 8 — Inspector

- [ ] **C20 — Split `inspect_cli.py` (52 KB) into `inspector/`.**
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
  references and every renamed command), `CHANGELOG.md` (the refactor entry), regenerate or delete
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
