# Single-Run Analysis — ITU (run `s_1`, 2026-09-16)

Scope: one `single` run against Information Technology University (`itu`,
`https://itu.edu.pk`). Evidence: `loggings/single_logs/s_1.json`,
`loggings/extract_links.log`, `loggings/notebook_lifecycle.log`,
`loggings/notebook_logs/d05425b3-df43-4151-8147-c3fe044ef846.json`,
`data/state.sqlite`, `data/links/itu.jsonl`,
`data/outputs/uni_outputs/itu.json`.

All log timestamps below are **local (PKT, UTC+5)** as written in
`extract_links.log`; the lifecycle/audit JSON files record the same events in
UTC, five hours earlier.

---

## 1. Verdict

The run **completed end-to-end without a single hard failure**: status
`completed`, 46/46 sources ingested, 0 failed, all six query blocks answered,
notebook deleted, quota ledger reconciled, payload written.

It is nonetheless **not shippable**. The corpus audit
(`src.inspector.auditor.audit_corpus`) returns:

```
✗ NOT READY — DO NOT PUSH
  • 'application_fee' answered for 0% of programmes, below the 60% floor (17 missing)
  • 'application_deadlines' answered for 0% of programmes, below the 60% floor (17 missing)
  ⚠ No programmes at all in: diploma
1 university · 17 programmes · 0 with every required field · overall coverage 78%
```

The cause is not the extractor. It is **source selection in Phase 1**: 25 of the
46 pages uploaded to NotebookLM are merit lists, and the five canonical
`/admissions/<programme>` pages that actually carry fee and deadline text were
ranked 47–51 and held back in reserve. The model answered faithfully from a
corpus that does not contain the answer. See §5, which is the single most
important finding in this report.

---

## 2. Run timeline

| Phase | Window (PKT) | Duration | Result |
|---|---|---|---|
| Phase 1 — crawl | 15:20:30 → 15:21:25 | 55 s | 43 pages visited, 35 succeeded, 142 raw links |
| Phase 1 — filter / score / export | 15:21:25 → 15:21:38 | 13 s | 46 selected + 16 reserve → `data/links/itu.jsonl` |
| Phase 2 — health check | 15:21:38 → 15:21:42 | 4 s | 5/5 sampled links reachable (100%, floor 50%) |
| Phase 2 — ingest | 15:21:44 → 15:24:44 | 180 s | notebook created, 46/46 sources `ready`, 0 failed |
| Phase 3 — query suite | 15:24:44 → 15:34:06 | **562 s (69% of the run)** | 6 blocks, 14 asks |
| Cleanup | 15:34:06 → 15:34:08 | 2 s | notebook deleted, shared browser closed |
| **Total** | 15:20:29 → 15:34:08 | **13 m 38 s** | `status: completed` |

Per-block query cost (from the per-notebook audit document):

| Block | Asks | Wall | Answer bytes | Items |
|---|---|---|---|---|
| `main_info_contact` | 1 | 50 s | 1,189 | 1 |
| `bachelors` | **5** (2 oversized, 3 narrowed) | 244 s | 13,689 ×3 | 8 |
| `masters` | **5** (2 oversized, 3 narrowed) | 197 s | 11,831 ×3 | 7 |
| `phd` | 1 | 29 s | 4,073 | 2 |
| `diploma` | 1 | 14 s | 2 (`[]`) | 0 |
| `faculties` | 1 | 28 s | 1,741 | 4 |

---

## 3. What worked

- **Ingestion is solid.** 46/46 sources uploaded and reached `ready` state, 0
  failures, tier histogram `{1: 24, 2: 13, 3: 5, 4: 4}` matching the Phase 1
  allocation exactly. Upload durations: min 3.3 s, median 7.0 s, max 20.8 s.
- **The reserve mechanism held correctly.** 16 links were held back to backfill
  pre-flight casualties; there were none, so none were spent.
- **Pre-flight health check passed** (5/5 reachable) and cost 4 s.
- **Quota accounting is exact.** 6 queries reserved up front, 8 settled as
  overage (`query_events` rows 1 and 2), `query_ledger` = 14, and 14 is the true
  number of `chat.ask` calls. The reconciliation logic in `orchestrator.py:306`
  works.
- **Notebook lifecycle is clean.** Created 15:21:44, deleted 15:34:08 with
  `{"trigger": "success_cleanup", "slot_freed": true}`. No leaked workspace slot.
- **The chat timeout is live.** `chat_timeout_sec=180` is now actually read; no
  ask hung. (This is the fix for the 7 h 11 m COMSATS stall.)
- **The oversized-response split recovered both blocks.** Without it,
  `bachelors` and `masters` would both be empty — that is the exact failure mode
  recorded for ITU on 2026-09-03.
- **Identity enrichment worked.** `main_info` carries `domain_verified: true`
  and `verification_note: "Identity fields sourced from rankings_global.json
  registry."`; `application_portal_url` resolved to
  `https://admissions.itu.edu.pk/login`, so the Exa fallback was correctly
  skipped (`exa_enriched: false`; `exa_api_key` is empty anyway).
- **Cross-contamination flagging found nothing**, and `programs_possibly_truncated`
  is `false`, `failed_query_blocks` is `[]` — all honest.
- **Extracted data is accurate where present.** 17 programmes with plausible
  ITU values (BS 4 Years @ PKR 1,416,000; MS 2 Years @ PKR 327,000), 4 real
  faculties with departments and websites, correct contact block.

---

## 4. Warnings raised during the run

| Count | Warning | Assessment |
|---|---|---|
| 8 | `HEC_Link_Extractor: Page failed: …*.pdf -> Unexpected error in _crawl_web` | **Material.** All 8 are ITU test-pattern / sample-paper PDFs. crawl4ai's Playwright strategy cannot render PDFs (`net::ERR_FAILED` / `Download is starting`). These documents are exactly where admission-test and fee detail lives. |
| 4 | `RPCResponseTooLargeError: RPC response exceeded 52428800 bytes` on `bachelors` ×2 and `masters` ×2 | **Material** — see §6. |
| 1 | `huggingface_hub: You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN` | Cosmetic. The BGE model is cached; only affects cold-start rate limits. |
| several | `TargetClosedError: Target page, context or browser has been closed` during crawl teardown | Benign. Emitted as the shared browser closes with in-flight navigations (`/admissions/eligibility-criteria/`, `/admissions/faqs/`, `/admissions/bs-management-and-technology/`). **But note those three URLs are high-value and were lost this way.** |
| 2 | `crawl4ai: Max pages limit (35) reached, stopping crawl` | **Material.** `config.max_crawl_pages = 35` truncated the crawl at 35 of 43 attempted pages; fee/deadline pages deeper in the tree were never discovered. |

Nothing at `[ERROR]` or `[CRITICAL]` level belongs to this run. The three
`asyncio: Future exception was never retrieved` lines at 14:45:43 belong to the
earlier cancelled *complete* run `c_1`, not to `s_1`.

---

## 5. Root cause of the 0% coverage: merit-list flooding

This is the finding that matters.

`data/links/itu.jsonl` holds 62 scored links. **29 of them (47%) are merit
lists** — per-programme cutoff-aggregate pages. Of the 46 that were selected
and uploaded, **25 are merit lists (54%)**, and they occupy 11 of the Tier-1
slots and 17 of the Tier-2 slots. The `bachelors`, `masters`, `phd` and
`diploma` queries are all bound to `tiers=(1, 2)`, so **more than half of the
evidence those queries were grounded in is merit-list tables.**

Merit lists contain no tuition fee, no application fee, and no deadline.

Meanwhile, these Tier-1 pages were ranked 47–51 and pushed into reserve —
never uploaded:

```
rank 47  https://itu.edu.pk/admissions/bs-electrical-engineering
rank 48  https://itu.edu.pk/admissions/bs-financial-technology
rank 49  https://itu.edu.pk/admissions/ms-computer-science
rank 50  https://itu.edu.pk/admissions/bs-artificial-intelligence
rank 51  https://itu.edu.pk/admissions/ms-management-technology
```

Three separate merit lists for BS Financial Technology were selected while
`/admissions/bs-financial-technology` — the programme's own page — was not.

Contributing mechanics:

1. **A keyword is over-firing.** `matched_keyword` across the 46 selected links
   is led by `"merit list closing aggregate formula 2026"` with 13 hits, ahead
   of `"bs computer science software engineering"` (9). One keyword in
   `src/extractor/linkers/constants.py` is buying a quarter of the corpus.
2. **Canonical dedup does not recognise merit-list families.** The log reads
   `Reduced 96 links to 93` — 3 collapsed. It did not collapse
   `bs-software-engineering-1st-merit-list-2026`,
   `.../faculty-of-engineering/bs-software-engineering-1st-merit-list-2026` and
   `bs-software-engineering-2nd-merit-list-2026`, which are the same programme
   in three URL shapes.
3. **`financial-assistance` landed in Tier 4**, which no programme query reads.
4. **The only fee/deadline documents on the site are PDFs**, and all 8 failed to
   crawl (§4).

Suggested fixes, in order of expected payoff:

- Cap merit-list URLs at a small fraction of the selection (or demote the whole
  `merit-list` path family to Tier 3), and extend canonical dedup to fold
  `Nth-merit-list` / `faculty-of-*` variants of one programme into one entry.
- Retune or split the `merit list closing aggregate formula 2026` keyword; its
  embedding is clearly matching generic admissions language.
- Promote `financial-assistance` / fee-schedule / academic-calendar paths into
  the tiers the programme queries read, or add them as a guaranteed floor.
- Add a PDF ingestion path (fetch bytes + text-extract rather than navigate), or
  at minimum pass PDF URLs straight to NotebookLM, which ingests PDFs natively.
- Raise `config.max_crawl_pages` above 35 for sites with deep admissions trees.

---

## 6. The oversized-response retries cost 29% of the query budget for nothing

`bachelors` and `masters`, asked over the same 37 Tier-1+2 sources, each failed
twice with `RPC response exceeded 52428800 bytes` (read 52,449,458–52,487,729
bytes — all within 60 KB of the ceiling), then split to depth 2 and succeeded.
`phd` and `diploma`, asked over **the same 37 sources**, succeeded on the first
ask. So the failure is not a deterministic property of corpus size, as
`is_oversized_response_error`'s docstring assumes — it is closer to a
server-side streaming pathology. A ~52 MB chat response for 8 programmes is not
a real payload.

Two consequences:

1. **Cost.** 4 of 14 asks (29% of the quota spent on this university) and
   ~195 s (24% of total runtime) produced nothing.
2. **The split does not diversify evidence.** All three narrowed `bachelors`
   sub-answers returned **exactly 13,689 bytes**, and all three `masters`
   sub-answers **exactly 11,831 bytes**. Three different source subsets
   returning byte-identical answers means the `source_ids` scoping had no
   measurable effect on what the model grounded on; `_merge_list_answers`
   dedupes three copies of the same list down to the one list. The split
   "worked" only because a re-ask happened to not trip the ceiling.

Worth trying before more splitting: a single bounded re-ask of the *identical*
question on the oversized error (currently explicitly abandoned at
`notebook_querying.py:400`), since the evidence here says the error is
transient rather than deterministic.

---

## 7. Code defects confirmed while reading the run

### 7.1 The JSON-repair retry never shows the model its broken answer
`src/extractor/crawlers/notebook_querying.py:352`

```python
raw = ""                       # reset at the top of every attempt
prompt = spec.prompt
if attempt > 0:
    prompt = (... f"Previous answer (truncated):\n{raw[:1500]}\n\n" ...)
```

`raw` is cleared immediately before it is interpolated, so the repair prompt
always contains an empty "previous answer" block. The comment says the reset
exists to avoid quoting a stale answer after a *transport* failure, but it fires
unconditionally, disabling the feature for the parse failures it was built for.
ITU was unaffected (no parse failures this run), but it explains Yale at
07:15/07:23 the same day: attempts 1 and 3 failed with the byte-identical error
`Invalid \escape: line 15 column 25 (char 537)`.

**Fix:** keep the raw answer from the failed attempt in a separate variable that
is cleared only on the transport-failure path.

### 7.2 The audit log under-reports query spend by 29%
`src/extractor/crawlers/notebook_querying.py:366` and `:394`

`log_query_executed` is called only on the success path. The oversized/errored
path increments `report.queries_used` but logs no event. Result for this run:

- `query_ledger.queries` = **14** (correct — asks actually issued)
- `pipeline_state.queries_executed` = **14** (correct)
- `QUERY_EXECUTED` events in the audit document = **10**

Anyone reconstructing cost from `loggings/notebook_logs/*.json` will be 29% low,
and the 4 most expensive asks — the ones that failed — are invisible there.

**Fix:** log a `QUERY_EXECUTED` (or a `QUERY_FAILED`) event on the error path too.

### 7.3 `query_index` is per-block, not per-run
The audit shows `bachelors` at indices 3, 4, 5 and `masters` *also* at 3, 4, 5,
because `runner.py:211` gives each spec a fresh `ExtractionReport()` and merges
it afterwards. The field name reads as a run-level sequence number and is not
one. Cosmetic, but it makes the audit document hard to read; consider renaming
it or seeding the sub-report's counter.

### 7.4 `uni_slug` is missing from every `QUERY_EXECUTED` event
`notebook_lifecycle.log` shows `[slug:N/A]` for all 10 query events, while
`NOTEBOOK_CREATED` / `SOURCE_UPLOADED` / `NOTEBOOK_DELETED` all carry
`[slug:itu]`. `log_query_executed` accepts `uni_slug` (default `None`) and the
call site at `notebook_querying.py:366` never passes it. One-line fix.

### 7.5 Normalization never touches the persisted artifact
`normalize_universal_payload` is called from exactly one place —
`src/inspector/records.py:32`, on the **read** path. Phase 3 writes
`payload.model_dump()` straight to `data/outputs/uni_outputs/itu.json`
(`orchestrator.py:325`). The stored file is raw model output. Visible symptoms
in `itu.json`:

- `currency: "PKR"` on programmes whose `tuition_fee` is `null` (3 of 17) — a
  currency label for a fee that does not exist;
- `summary_3_lines: null` retained on all 17 programmes, a field C18 removed
  from the prompts.

This may be deliberate (store raw, normalize on read), but it means the files in
`data/outputs/` and the Supabase upload path see different data than the
inspector does. Worth an explicit decision.

---

## 8. Field-level data quality

Per-programme coverage across all 17 programmes:

| Field | Answered | Coverage | Status |
|---|---|---|---|
| `name`, `degree_level` | 17 | 100% | required, OK |
| `description`, `eligibility_requirements`, `admission_requirements`, `currency`, `intake_terms`, `delivery_mode`, `application_status`, `program_info_link` | 17 | 100% | OK |
| `duration` | 16 | 94% | OK |
| `tuition_fee` | 14 | 82% | OK |
| `department` | 16 | 94% | OK |
| `career_prospects` | 7 | 41% | thin |
| `scholarships_info` | 2 | 12% | thin |
| `application_fee` | 0 | **0%** | **blocks upload** |
| `application_deadlines` | 0 | **0%** | **blocks upload** |
| `courses_taught` | 0 | 0% | not in the required set |
| `summary_3_lines` | 0 | 0% | dead field (see §7.5) |

Other observations:

- All 8 bachelors carry the identical fee `1,416,000 PKR` and all 7 masters
  `327,000 PKR`. Plausible for ITU's flat per-faculty structure, but it is also
  the shape a model produces when it finds one fee table and applies it
  everywhere. Worth a spot check against the site.
- `phd` returned only 2 programmes (CS, EE) and both have `tuition_fee: null`;
  `PhD Computer Science` also has `duration: null`.
- `diploma: []` is almost certainly correct — ITU offers none — but the auditor
  flags an empty bucket as a possible silent query failure. The audit document
  confirms the `diploma` ask ran and returned a legitimate `[]` (2 bytes,
  13.5 s), so this warning is a false positive here.
- `main_info.rankings` is `[]` by design (the prompt forbids numeric ranks).

---

## 9. Output artifacts written

| Path | Written | Content |
|---|---|---|
| `data/links/itu.jsonl` | 15:21:38 | 62 rows (46 `selected: true`, 16 reserve) |
| `data/outputs/uni_outputs/itu.json` | 15:34 | 35.5 KB payload |
| `data/outputs/all_uni_outputs/universities_crawling_data.jsonl` | 15:34 | 1 record |
| `data/outputs/all_uni_outputs/universities_crawling_data.json` | 15:34 | compiled master, 1 record |
| `loggings/single_logs/s_1.json` | 15:34 | run manifest, `status: completed` |
| `loggings/notebook_logs/d05425b3-…json` | 15:34 | 58 events |
| `data/state.sqlite` | 15:34 | `pipeline_state`, `source_map` (46), `query_ledger`, `query_events` |
| `extracted_links.txt`, `extracted_links_detailed.txt` | 15:21 | repo-root scratch, gitignored, overwritten per university |

Two housekeeping notes:

- **`JsonIO` logged nothing this run.** Earlier batch runs emit
  `JsonIO: … N records written`; the single-university write path is silent.
  Worth a log line for symmetry.
- `data/` is gitignored, yet 21 link files and the output tree are currently
  staged in git (`git status`). That contradicts the ignore policy in
  `.gitignore:15`. Unrelated to this run, but it will grow.
- `loggings/notebook_audit.log` has no entries after 2026-09-14 (222 from 09-12,
  111 from 09-14, all synthetic `nb-1` test rows). The live path now writes
  `loggings/notebook_logs/<id>.json` instead. The stale file and the still-declared
  `config.notebook_audit_jsonl_path` are leftovers from the C26 migration.

---

## 10. Recommended next actions

**Blocking upload (fix before the next batch):**
1. Fix merit-list flooding in Phase 1 link selection (§5) — this alone should
   move `application_fee` and `application_deadlines` off 0%.
2. Add a PDF ingestion route, or hand PDF URLs to NotebookLM unrendered (§4).

**Correctness:**
3. Fix the empty repair prompt (§7.1) — cheap, and it recovers parse failures
   that currently burn all 3 attempts on the same error.
4. Log failed asks to the audit document (§7.2) and pass `uni_slug` (§7.4).

**Cost / reliability:**
5. Allow one identical re-ask on `RPCResponseTooLargeError` before splitting
   (§6); the evidence says the error is transient.
6. Raise `config.max_crawl_pages` from 35, and let the shared browser drain
   in-flight navigations before close (§4).

**Decide:**
7. Whether `data/outputs/` should hold normalized or raw payloads (§7.5).

---

*Generated 2026-09-16 from run `s_1`.*
