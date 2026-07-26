# Engineering Review — Phase 1 Audit & Phase 2/3 Design Correction

**Date:** 2026-07-24 · **Scope:** `extract_links.py`, `README.md`, `resources/data_model_and_plan.md`
**Verified against installed deps:** Crawl4AI 0.9.2 · notebooklm-py 0.7.3 · sentence-transformers 5.6.1 · exa-py 2.16.0 · Python 3.13.14 (conda `ise-env`)

---

## 0. Context

Phase 1 ships and produces output. But the committed artifacts do not reflect the current code, several filters actively delete the pipeline's own stated primary target, and the Phase 2/3 throughput model omits the only bottleneck that actually matters. This document records what is provably broken, what is merely fragile, and the corrected design.

**First, a correction to an earlier read.** `extracted_links.txt` contains 17 `/news/` and `/events/` URLs. That is *not* proof that the exclusion filter is broken:

```
extracted_links.py   mtime 2026-07-24 15:11:43   ← newer
extracted_links.txt  mtime 2026-07-24 15:09:36   ← older
```

The detailed report header lacks the `Excluded Patterns Filter` line that current `export_dual_outputs()` writes (`extract_links.py:614`), and `loggings/extract_links.log` contains **zero** `Applying Exclusion Keyword Filter` lines. The outputs predate the exclude-keywords feature.

The real finding is worse in a different way: **the current filtering code has never been executed or verified**, and the artifacts on disk misrepresent output quality. Phase 2 is specified to consume `extracted_links.txt` — today that file is 287 stale URLs including news, events, and malformed links.

---

## 1. Blocking defects (code-level, proven by reading source)

### 1.1 The exclusion list deletes the pipeline's PRIMARY FOCUS

`EXCLUDED_PATH_KEYWORDS` (`extract_links.py:113`) contains `"portal"`. Matching is plain substring against `f"{domain}{path}?{query}"` (`:381-383`).

`data_model_and_plan.md:40` designates `application_portal_url` as **"PRIMARY FOCUS"**. Phase 1 discards every URL containing `portal`, then Phase 3 spends an Exa API call to recover the link Phase 1 just deleted. Pakistani universities routinely host admissions at `portal.uni.edu.pk` or `/admissions/portal`.

Same substring mechanism, same list:

| Keyword | Collateral kill | Cost |
|---|---|---|
| `admin` | `/business-administration/`, `/public-administration/` | **All BBA/MBA/DBA programs** |
| `audit` | `/accounting-and-auditing/` | BS Accounting programs |
| `portal` | `portal.uni.edu.pk`, `/admissions/portal` | The primary deliverable |

Corroboration: `grep -ic 'administration|auditing' extracted_links.txt` → **0**, on a NUST crawl (NUST Business School offers BBA/MBA).

**Fix:** match on path *segments*, not substrings — split path on `/`, `-`, `_` and compare against the token set. Remove `portal` and `admin` from the blocklist entirely; handle auth pages via a separate `login|signin|wp-admin` regex anchored to segment boundaries.

### 1.2 The "2026 up-to-date" filter boosts 2024 content

```python
CURRENT_YEAR_REGEX  = re.compile(r'\b(202[4-7]|...)\b')   # :197  — includes 2024
OUTDATED_YEAR_REGEX = re.compile(r'\b(200[0-9]|201[0-9]|202[0-3])\b')  # :198 — stops at 2023
```

Today is 2026. `2024` is boosted ×1.15 as "current" and can never be penalised. Worse, `CURRENT` is tested first (`:553`), so `2023-2024` matches `CURRENT` and is boosted despite also matching `OUTDATED`.

Observed in the report: `bachelor-of-software-engineering-for-fall-2024` → `Year: 2026 Up-to-Date (Boosted)`, ranked **#1 of 287** at score 1.1853. 12 `fall-2024` links present.

**Fix:** extract *all* 4-digit years, take `max()`, compare to a `--current-year` argument (default `datetime.now().year`). Boost `>= year`, neutral at `year-1`, decay below. Never hardcode 2026 — it rots.

### 1.3 `--max-links` truncation starves Tier 3 and Tier 4

`scored_links` is sorted by `(tier_num, -score)` (`:577`), then sliced `[:max_links]` (`:679`). Tier 1 fills the slice first. At the default `--max-links 100` on a large university, the result can be 100 undergraduate program links and **zero** faculty or contact links.

Phase 3 then runs Q5 (`faculties`) and Q6 (`contact`) against a notebook containing no faculty or contact sources, and burns 2 of the 6 daily queries per university guaranteeing empty blocks.

**Fix:** proportional quota selection, not a flat slice — Tier 1 45%, Tier 2 30%, Tier 3 15%, Tier 4 10%, with unfilled quota cascading to the next tier.

### 1.4 HEC batch mode produces one undifferentiated file

`run_pipeline()` accumulates every university into `all_processed_results` (`:682`) and calls `export_dual_outputs()` **once** (`:684`). There is no per-university partitioning.

Phase 2's contract — "Batch upload URLs from `extracted_links.txt`" per university — is unsatisfiable. The 287-link file mixes NUST, LUMS, and ITU.

Evidence from the domain histogram: 287 links, ~284 on `nust.edu.pk` + subdomains, ~10 LUMS, 0 ITU. Which exposes the next defect.

### 1.5 Crawl failures are swallowed and reported as success

`crawl_site_links()` wraps the entire crawl in `except Exception` (`:325`) and returns whatever partial list it has. The log shows `net::ERR_ABORTED` on `itu.edu.pk` and SSL verification failure on `hec.gov.pk`. The pipeline continued and printed `SUCCESS: Extracted 287 total canonical high-quality links`.

A university that yields 0 links is indistinguishable from one that succeeded. In an 83-university/day batch this is silent, unbounded data loss.

**Fix:** per-target try/except that records `status: ok|partial|failed` plus link count into a manifest; fail the run if `failed_ratio > threshold`; never let the summary line claim success over partial data.

### 1.6 URL normalisation is incomplete

`normalized_url` (`:395`) strips the fragment and trailing slash and nothing else. Observed in output:

- `https://sines.nust.edu.pk//program/bs-bioinformatics-...` — **double slash**, 8+ occurrences
- `https://nust.edu.pk?p=959&amp;post_type=scholarship` — **undecoded HTML entity**; this URL 404s
- `mbbs--<U+200B>bachelor-of-medicine,...` — **zero-width space** inside the path (1 confirmed line)
- No `www.`/scheme unification → `http://uaf.edu.pk/x` and `https://www.uaf.edu.pk/x` upload as two sources, wasting the 300-source cap
- Query string retained verbatim → `?utm_source=` variants duplicate

**Fix:** `html.unescape()` → strip `​-‏﻿` → collapse `//` in path → lowercase host, drop `www.` → force `https` → drop tracking params → *then* dedupe.

### 1.7 Fuzzy dedup merges genuinely distinct programs, at O(n²)

`deduplicate_canonical_degree_links()` (`:479-486`) compares every candidate against every accepted item with `SequenceMatcher`, threshold 0.88.

`bs-electrical-engineering` vs `bs-electronic-engineering` scores ≈0.90 → **silently merged**. Two distinct degrees become one. At ~2000 pre-truncation links this is also ~2M `SequenceMatcher` calls, quadratic in the batch size.

Separately, `get_canonical_degree_key()` (`:432`) keys on the **last path segment only**, so `/programs/bs-cs` and `/admissions/bs-cs` collapse to `bs-cs` — different pages, one survives.

**Fix:** build a structured key — `(degree_level, sorted(discipline_tokens))` after stripping intake/campus tokens — and compare keys for equality. Deterministic, O(n), and cannot merge *electrical* with *electronic* because the discipline token differs.

### 1.8 Embedding model reloaded once per university

`classify_and_score_links()` calls `SentenceTransformer("BAAI/bge-small-en-v1.5")` (`:514`) on every invocation, then `del model; gc.collect()` (`:574`). It is called once per university (`:677`).

At the target 83 universities/day: 83 model loads and 83 recomputations of the same 22 keyword embeddings. ~3-5 s each — several minutes of pure waste per run, plus GPU/CPU thrash.

**Fix:** module-level lazy singleton; encode `ALL_COUNSELOR_KEYWORDS` once and cache the tensor.

### 1.9 BGE is being used without its retrieval prefix

`BAAI/bge-small-en-v1.5` is trained asymmetrically: queries require the instruction prefix `"Represent this sentence for searching relevant passages: "`; passages do not. The code (`:521-522`) encodes both sides bare.

This measurably degrades ranking quality, and it distorts the score distribution the `--threshold 0.45` default was tuned against — the observed range is 0.52–1.19 weighted, meaning the threshold is barely binding.

**Fix:** prefix the tier keywords (query side), leave link text bare (passage side), `normalize_embeddings=True`, and re-tune the threshold against the corrected distribution.

### 1.10 Crawl4AI already computes link scores; the code discards them

Verified in the installed 0.9.2 `models.py:373-384`, the `Link` model carries `intrinsic_score`, `contextual_score`, `total_score`, and `head_data`. `crawl_site_links()` reads only `href`, `text`, `title` (`:310-323`).

Two capabilities are being left on the table, both confirmed present in `async_configs.py`:

- `CrawlerRunConfig(score_links=True)` (`:1673`) — free structural quality prior
- `CrawlerRunConfig(link_preview_config=LinkPreviewConfig(...))` (`:1155`, `:1695`) — fetches each link's `<head>` title/description

The second is the single largest quality lever available. Semantic scoring currently runs on URL slug + anchor text alone; scoring on the target page's actual title and meta description is a categorically better signal for the same embedding cost.

---

## 2. The Phase 2/3 plan models the wrong bottleneck

`data_model_and_plan.md:183` computes throughput as `500 queries ÷ 6 per uni = 83 universities/day`.

Queries are not the constraint. **Source ingestion is**, and the plan does not model it at all.

| | Plan's implied numbers | Reality |
|---|---|---|
| Sources per university | 287 (current output) | vs. Pro cap of **300** — 4% headroom |
| Ingestions per day | not modelled | 83 × 287 = **23,821** |
| Processing time per source | not modelled | 30 s – 10 min (skill docs) |
| Wall-clock at 8-way concurrency, 45 s/source | — | **~37 hours** for a 24-hour target |

The plan is infeasible by roughly 1.5×–10× depending on where source latency lands.

**Second, subtler problem:** 6 queries × 83 universities = **498 of 500**. Zero margin. One malformed JSON response requiring a re-ask breaks the day's budget. LLM JSON extraction fails often enough that a retry budget is mandatory, not optional.

### Corrected throughput model

Cut sources to **~60 curated URLs/university** (tier-quota selected per §1.3) and merge the two cheapest queries:

| Query | Block | Rationale |
|---|---|---|
| Q1 | `main_info` + `contact` | Both single-object, site-wide metadata — no truncation risk |
| Q2 | `programs.undergraduate` | Largest array; keep isolated |
| Q3 | `programs.graduate` | |
| Q4 | `programs.postgraduate_and_phd` | |
| Q5 | `faculties` | |

**5 queries × 83 = 415/day, leaving 85 for retries.** Ingestion drops to 83 × 60 = 4,980 sources ≈ **7.8 h** at 8-way concurrency — feasible within a day with margin.

---

## 3. Design corrections for Phase 2/3

### 3.1 Use the Python client, not 287 CLI subprocesses

`notebooklm source add` takes exactly one `CONTENT` argument per invocation (verified via `--help`). Ingesting 60–287 URLs per university by shelling out means one process spawn + one auth load per URL.

The installed package exposes a full async client. Verified accessors on `NotebookLMClient`:

```
client.notebooks   .create .delete .list .get_source_ids .rename
client.sources     .add_url .add_file .add_text .delete .list
                   .wait_for_sources .wait_until_ready .get_fulltext
client.chat        .ask .delete_conversation .get_conversation_id .get_history
```

```python
NotebookLMClient(auth, max_concurrent_uploads=4, max_concurrent_rpcs=16,
                 rate_limit_max_retries=3, server_error_max_retries=3)
ChatAPI.ask(notebook_id, question, source_ids=None, conversation_id=None) -> AskResult
```

Concurrency limiting, rate-limit retry, and server-error retry are **already implemented in the client**. Do not rebuild them. `sources.wait_for_sources` (plural) batch-waits — use it instead of polling `source wait` per source.

### 3.2 Scope each query to the sources that can answer it

`chat.ask(..., source_ids=[...])` accepts a source subset. Phase 1 already knows every URL's tier — carry that tier through Phase 2, record the `url → source_id → tier` mapping, and scope:

- Q2/Q3/Q4 (programs) → Tier 1 sources
- Q1 (main_info + contact) → Tier 2 + Tier 4 sources
- Q5 (faculties) → Tier 3 sources

Higher precision, less noise in the answer, lower truncation risk. This is Phase 1 signal that the current plan throws away at the Phase 2 boundary.

### 3.3 Do not ask an LLM for rankings

QS/THE/Webometrics ranks are numeric facts. LLMs hallucinate them confidently, and the plan's Exa fallback query — `"<Uni> QS ranking 2026 application portal link"` — conflates two unrelated searches into one, which returns mush.

**Better:** scrape the Webometrics Pakistan country listing **once** (a single page, ~200 institutions) into `resources/rankings_pk.json`, keyed by normalised name + domain. One HTTP request replaces 83 Exa calls, is deterministic, and cannot hallucinate. Join at assembly time; set `exa_enriched=false`.

Reserve Exa exclusively for `application_portal_url`, and constrain it: `include_domains=[university_domain]`. Searching the open web for a portal link invites third-party aggregator spam.

### 3.4 Budget for LLM JSON being malformed

The plan says "Return ONLY raw JSON" and stops there. In practice NotebookLM will return fenced blocks, a preamble sentence, and — critically — **inline citation markers inside the JSON**:

```json
"courses_taught": ["Data Structures" [1], "Algorithms" [2]]
```

That is not parseable. A hardened extractor is required, in order: strip ``` fences → strip `\[\d+\]` markers → balanced-brace/bracket scan to isolate the JSON span → `json.loads` → **Pydantic validation against the schema** → on failure, one repair re-ask (charged against the 85-query retry budget) → on second failure, persist the raw answer to `failures/` and mark the block `null` rather than dropping the university.

The schema in `data_model_and_plan.md` is currently prose. Make it executable Pydantic models in `schema.py` and generate the prompt JSON examples *from* those models, so prompt and validator cannot drift.

### 3.5 Conversation isolation

`notebooklm ask` continues the previous conversation by default. Q1→Q5 would accumulate, inflating tokens and inviting `"same as above"` answers in Q3/Q4.

Use `chat.delete_conversation()` before each query, or pass a fresh `conversation_id`. Via CLI the equivalent is `ask --new --json` (`--json` implies `--yes`, so it will not block on the destructive-delete prompt — confirmed in `--help`).

### 3.6 Never delete a notebook before its output validates

`data_model_and_plan.md:208` deletes the notebook immediately after saving. If extraction produced garbage, the ingested sources are gone and must be re-crawled and re-ingested — the single most expensive operation in the pipeline.

**Order:** extract → parse → validate against Pydantic → *only then* delete. On validation failure, keep the notebook and record it in the manifest for retry.

### 3.7 Add resumability

83 universities × ~8 minutes is a multi-hour run with no checkpoint. A crash at university 40 currently restarts from zero.

Add `state.sqlite` (or a JSONL manifest) with one row per university: `slug, status, notebook_id, source_count, queries_used, last_error, updated_at`. Make every phase idempotent and resumable by slug. Track `queries_used` cumulatively so the pipeline can **hard-stop before exceeding the 500/day cap** rather than discovering it via API errors.

### 3.8 Detect truncated program lists for free

NotebookLM will silently truncate long arrays. Phase 1 knows how many Tier-1 program URLs were ingested. If the returned `programs` array is far smaller, set `programs_possibly_truncated: true` for the Phase 4 audit. Costs zero additional queries.

---

## 4. Recommended file layout

```
config.py           # dataclass config; no more 10-arg function signatures
schema.py           # Pydantic models — single source of truth for schema + prompts
extract_links.py    # Phase 1 (fixes §1.1–1.10)
ingest.py           # Phase 2 — async client, bounded concurrency, tier mapping
extract_data.py     # Phase 3 — 5-query suite, JSON repair, validation, enrichment
inspect_cli.py      # Phase 4
rankings.py         # one-shot Webometrics/QS scrape → resources/rankings_pk.json
state.py            # SQLite manifest, resumability, quota ledger
prompts/q1..q5.txt  # generated from schema.py
data/links/<slug>.jsonl        # per-university, replaces the flat shared .txt
data/university_counseling_data.jsonl
```

Also missing and required: `requirements.txt` (nothing pins the verified dependency set), `.env.example` (`EXA_API_KEY`), and `.gitignore`. The project is not currently a git repository.

---

## 5. Suggested order of work

1. **§1.1, §1.2, §1.3** — filters and selection. Everything downstream inherits this data; fixing it later means re-crawling.
2. **§1.6, §1.7** — normalisation and dedup. Directly determines how much of the 300-source cap is wasted.
3. **§1.4, §1.5, §3.7** — per-university partitioning, honest failure reporting, resumable state. Prerequisites for any multi-university run.
4. **§1.8, §1.9, §1.10** — scoring quality and cost.
5. **Phase 2** (§3.1, §3.2), then **Phase 3** (§3.4, §3.5, §3.6), then rankings (§3.3), then Phase 4.

**Verification for each stage:** re-run `--url https://nust.edu.pk --max-links 60` and assert — zero `/news/` or `/events/` survivors; ≥1 URL containing `portal`; non-zero counts in all four tiers; no `//` or `&amp;` or zero-width characters in any output URL; and `fall-2024` links ranked *below* `fall-2025`/`2026` equivalents. All five are currently violated or unverifiable.
