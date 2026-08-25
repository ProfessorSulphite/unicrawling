# Source Code Summary (`src/`)

This document provides a comprehensive summary of all Python files in the `src/` directory, including their classes, methods, and functions.

---

## File: `config.py`
**Module Description:**
```text
Central Configuration Dataclass & Workspace Setup for Education Counselor System
```

### Classes
#### `class Config`
**Description:** Central configuration for paths, limits, and external service credentials.
**Methods:**
- `ensure_directories(self)`: Ensure all required workspace directories exist.
- `tier_quotas(self, total)`: Resolve fractional tier shares into integer link counts summing to `total`.

### Functions
#### `def _load_dotenv(path)`
**Description:** Minimal .env loader (no python-dotenv dependency).

Existing environment variables win, so an explicitly exported key is never
silently overridden by a stale file.

---
## File: `extract_data.py`
**Module Description:**
```text
Schema Extraction & Robustness Engine (extract_data.py)

Executes the 5-query suite against a prepared NotebookLM notebook, repairs and
validates the model's JSON, fills gaps from a deterministic rankings registry and
a domain-scoped Exa search, and returns a validated UniversityPayload.

Notebook deletion is the caller's decision and happens only after validation
succeeds -- see delete_notebook_after_success().
```

### Classes
#### `class Q1Payload`
**Description:** No docstring provided.

#### `class ExtractionError`
**Description:** Raised when a query could not be turned into valid schema data.

#### `class QuerySpec`
**Description:** One query in the suite, bound to the source tiers that can answer it.

#### `class ExtractionReport`
**Description:** Per-query outcome, so a partially-failed extraction is never silently clean.
**Methods:**
- `ok(self)`: No docstring provided.
- `merge(self, other)`: Fold a per-query sub-report into this one.

### Functions
#### `def strip_citation_markers(text)`
**Description:** Remove NotebookLM grounding markers from inside JSON string values only.

Operates string-by-string so that structural JSON arrays of numbers survive.

Non-string regions are copied as whole slices located with str.find rather
than one character per loop iteration. A 200 KB programme listing is >99%
non-string structural text, so the per-character append dominated the cost
of the entire repair stage.

#### `def _balanced_span(text, start_idx)`
**Description:** Return (span, unclosed_depth) for the bracket container opening at start_idx.

Maintains a real stack of open brackets rather than a single depth counter, so
a truncated answer is closed with the correct sequence. Counting only the outer
bracket type produced '...{}]' for a cut-off array of objects -- still invalid,
because the inner object's '}' was never emitted.
Quote-aware, so brackets inside string values do not affect nesting.

#### `def extract_json_str(text)`
**Description:** Extract the outermost balanced JSON value from a raw LLM answer.

Candidate start positions are validated by actually parsing them, rather than
trusting the first '{' or '[' encountered. That naive rule breaks on the very
common preamble "Here are the programmes [1]:\n```json\n{...}```" -- the
citation marker's '[' precedes the real object, so the extractor returned the
string "[1]" and every such answer failed validation.

#### `def _parses(candidate)`
**Description:** True when `candidate` is valid JSON, tolerating trailing commas.

#### `def _adapter_for(target_model)`
**Description:** Cache one TypeAdapter per target type.

Building a TypeAdapter compiles a pydantic-core validator; doing that inside
every query call re-paid the compilation for List[ProgramItem] on all five
queries of every university.

#### `def _validate_against(target_model, parsed)`
**Description:** Validate `parsed` using a cached adapter, or the model's own validator.

#### `def repair_and_validate_json(raw_text, target_model)`
**Description:** Clean fences and citation markers, balance brackets, parse, and validate.

Raises ExtractionError with the offending text when the answer cannot be
coerced into `target_model`, so the caller can decide whether to re-ask.

#### `def load_rankings_registry()`
**Description:** Load resources/rankings_pk.json once per process.

#### `def lookup_registry(domain)`
**Description:** Find a university registry entry by canonical domain or alias.

#### `def apply_registry_facts(main_info, domain)`
**Description:** Overwrite LLM-guessed identity fields with registry ground truth.

Rankings in particular are never taken from the model: a numeric world rank is
the most confidently hallucinated field in the whole payload. If the registry
has no rank, the payload correctly reports none.

#### `def _ask(client, notebook_id, prompt, source_ids, conversation_id)`
**Description:** Issue one chat.ask and return the answer text.

#### `def run_query(client, notebook_id, spec, source_ids, report)`
**Description:** Execute one query with a bounded repair loop.

On a parse or validation failure the model is re-asked with its own broken
output and the exact error, which recovers the majority of malformed answers.
Each attempt is counted against the daily query budget by the caller.

#### `def exa_find_application_portal(uni_domain, uni_name)`
**Description:** Domain-scoped Exa search for an application portal URL.

Restricted to the university's own domain and filtered by URL shape, because an
unconstrained search returns third-party admissions aggregators that would be
written into the payload as if they were official.

#### `def extract_university_payload(client, notebook_id, uni_name, uni_domain, source_ids_by_tier, tier1_source_count)`
**Description:** Execute the 5-query suite and assemble a validated UniversityPayload.

This function NEVER deletes the notebook. Deletion is a separate, explicit
call made by the orchestrator only after the payload validates and has been
persisted -- the previous implementation deleted inside a `finally:`, so any
transient chat timeout destroyed all 60 ingested sources with no way to retry
short of re-crawling and re-ingesting the whole university.

Returns:
    (payload, report). Inspect report.ok / report.failed before trusting the
    payload: a query that failed yields an empty block, not an error.

#### `def delete_notebook_after_success(client, notebook_id, uni_slug)`
**Description:** Free the workspace slot. Call ONLY once the payload is validated and written.

Returns True on success; a failed delete leaks a slot but must never mask a
successful extraction.

---
## File: `extract_links.py`
**Module Description:**
```text
================================================================================
HEC Recognized University & Education Counselor Link Extractor (Phase 1)
================================================================================
Author: Antigravity AI Engineering Team
Description:
    Phase 1 of the Education Counseling RAG Pipeline.
    This script extracts ultra-high-quality, high-relevance web links from
    official HEC-recognized Pakistani universities (e.g., NUST, ITU, LUMS, FAST)
    and specific university web pages.

    Key Features:
    - HEC Directory Discovery: Scrapes / loads official HEC-recognized university links.
    - Zero Garbage Policy: Aggressively filters out administrative noise, portals,
      logins, tenders, job vacancies, legal footers, and static assets.
    - Custom Noise & Pattern Exclude Filter (--exclude-keywords "news|events"):
      Filters out news, event announcements, press releases, convocations, seminars,
      and custom pipe-separated keyword patterns.
    - Priority Tiering & Semantic Scoring: Uses BAAI/bge-small-en-v1.5 to score
      and rank links into 4 Education Counseling Tiers:
        * Tier 1: Programs (BS, MS, PhD, Undergraduate/Postgraduate majors)
        * Tier 2: Fees, Admissions, Eligibility Criteria, Portals, Merit Lists
        * Tier 3: Faculties, Academic Departments, Schools, Campuses
        * Tier 4: FAQs, Prospectus, Scholarships, Hostel, Admission Contacts
    - Up-To-Date 2026 Information Filter (--uptodate):
        * When enabled (default: True), boosts 2025-2027 current links and penalizes
          outdated historical years (2010-2023).
    - Canonical Degree Deduplication Post-Processing:
        * Identifies and eliminates redundant degree variations (e.g. multi-campus
          subdomain mirrors and historical year intake duplicates like fall-2024 vs
          fall-2025-onward). Retains only the single highest-quality, most recent canonical link.
    - RAM & Flow Optimization: Generator-based processing, explicit tensor batching,
      and garbage collection to maintain low memory footprint.
    - Dual Output Generation:
        1. extracted_links.txt (Clean URLs only, 1 per line)
        2. extracted_links_detailed.txt (Full report: Rank, Tier, Score, Category, Keyword, URL)

Usage Examples:
    # Single university URL with news|events filtering and 2026 up-to-date filter:
    python3 extract_links.py --url https://nust.edu.pk --exclude-keywords "news|events|convocation" --max-links 100

    # HEC Recognized Universities Directory mode for top 5 universities:
    python3 extract_links.py --hec --hec-limit 5 --max-links 50
================================================================================
```

### Classes
#### `class CrawlFailure`
**Description:** Raised when a site could not be crawled at all, so callers can mark state.

### Functions
#### `def _tokenize_path(path)`
**Description:** Split a URL path into lowercase alphanumeric tokens.

#### `def is_excluded_path(url)`
**Description:** Decide whether a URL is administrative noise, using EXACT PATH TOKEN matching.

Token matching rather than substring matching is the whole point of this
function. Under the previous substring rule these were all wrongly deleted:
    /programs/bs-business-administration   (matched "admin")
    /programs/bs-accounting-and-auditing   (matched "audit")
    /admissions/apply-online-portal        (matched "portal")
while the noise it was meant to catch (/wp-admin/, /news/) is still caught
here -- by the "wp-admin" phrase rule and the "news" token rule respectively.

#### `def sanitize_url(url)`
**Description:** Sanitizes URL path by stripping trailing hyphens, stray punctuation, and malformed characters.

Prevents malformed/truncated URLs like '/program/bs-biotechnology-for-fall-2024-entry-'
from being generated or passed downstream.

#### `def normalize_url(url, base_url)`
**Description:** Produce a canonical, fetchable URL, or None if the URL can never be ingested.

Fixes observed in real NUST/LUMS crawl output:
  - '&amp;' left HTML-escaped inside the query string
  - a U+200B zero-width space inside an MBBS programme slug
  - 'sines.nust.edu.pk//program/...' double slashes producing duplicate sources
  - utm_* / fbclid campaign params splitting one page into several sources

#### `def dedupe_key(url)`
**Description:** Identity key for exact-duplicate collapse. Ignores scheme and a leading 'www.'
so http://uni.edu.pk/x and https://www.uni.edu.pk/x count as one source --
they would otherwise consume two of the 60 per-notebook slots for one page.

#### `def compute_year_decay_factor(text, now_year)`
**Description:** Score recency against the *current* year, resolved at runtime.

Returns (multiplier, human_readable_tag). The most recent year mentioned wins,
so 'fall-2025-onward' and '2025-2026' are treated as current in 2026 rather
than being penalised for also containing an older number.

#### `def get_discipline_tokens(url, text)`
**Description:** Build an exact deduplication key: (degree_level, sorted discipline tokens).

Replaces the previous SequenceMatcher(ratio > 0.88) fuzzy merge, which was
both O(n^2) over every link pair and wrong: 'bs-electrical-engineering' and
'bs-electronic-engineering' score ~0.90 similar and were silently merged into
a single programme. Exact token-set equality keeps them separate, while still
merging the same programme mirrored across campus subdomains (the host is not
part of the key) and across intake years (years are stopworded out).

#### `def allocate_proportional_tier_quotas(scored_links, total_cap, shares)`
**Description:** Select `total_cap` links with a guaranteed floor per priority tier.

The previous `scored_links[:max_links]` ran after a tier-major sort, so Tier 1
consumed the entire budget and Tiers 3 and 4 contributed zero sources. Phase 3
then asked a notebook containing no faculty or contact pages to answer the
faculties and contact queries. Unfilled tier quota is redistributed by score
so a small site still fills its budget.

#### `def slugify_university(name, url)`
**Description:** Stable filesystem-safe identifier used for per-university partitioned output.

#### `def extract_hec_universities(limit)`
**Description:** Fetches official HEC-recognized Pakistani universities.
Tries live scraping from official HEC portals, falling back gracefully to the curated top list.

#### `def build_browser_config()`
**Description:** Memory-bounded browser settings for link discovery.

#### `def get_shared_crawler()`
**Description:** Return the process-wide crawler, starting it on first use.

Rebinds if the running event loop changed: a browser started under a previous
asyncio.run() holds transports attached to a now-closed loop and every call
against it would fail.

#### `def close_shared_crawler()`
**Description:** Shut the shared browser down. Safe to call repeatedly and when none is open.

The globals are cleared before awaiting close() so that a hang or error in
teardown cannot leave a half-dead crawler installed as the shared instance.

#### `def browser_pool()`
**Description:** Scope the shared browser to a block, guaranteeing teardown on exit.

#### `def _crawler_scope()`
**Description:** Yield the shared crawler, or a private one when reuse is disabled.

A private crawler is always closed here; the shared one deliberately outlives
the block and is closed by the batch driver.

#### `def crawl_site_links(start_url, max_pages)`
**Description:** Uses Crawl4AI BestFirstCrawlingStrategy with KeywordRelevanceScorer
to discover internal and external links across high-relevance pages.
Automatically retries with alternative URL candidates (e.g. https:// vs http://)
if the initial URL times out or fails.

#### `def preprocess_and_filter_links(links, base_url, exclude_keywords)`
**Description:** Enforces the Zero Garbage Policy:
- Filters out non-http schemes, static non-document assets, administrative noise, and external social media.
- Dynamically filters out news, events, announcements, and custom pipe-separated exclude keywords (e.g. 'news|events').
- Resolves vague anchor text using human-readable words from URL path slugs.
- Normalizes and deduplicates URLs.

#### `def deduplicate_canonical_degree_links(scored_results)`
**Description:** Eliminate redundant programme variations using an exact structured key.

Group key is (degree_level, sorted discipline tokens) from get_discipline_tokens.
Because the host is not part of the key, the same BS mirrored on seecs./mcs./
ceme. subdomains collapses to one; because years are stopworded, fall-2024 and
fall-2025-onward collapse to one, and the recency-weighted score picks the
survivor. Because the match is exact rather than a similarity ratio, distinct
programmes with near-identical slugs (electrical vs electronic engineering) are
preserved. This is O(n) with a dict instead of the previous O(n^2) pairwise
SequenceMatcher scan (~2M comparisons at 287 links, ~41M at 60 universities).

#### `def _get_embedding_model()`
**Description:** Process-wide lazy singleton for the sentence encoder.

The previous code constructed SentenceTransformer inside the per-university
scoring function, so an --hec batch of 83 universities paid the model load
83 times. The weights are stateless across calls; one instance is correct.

#### `def classify_and_score_links(links, threshold, uptodate)`
**Description:** Computes cosine similarity between clean link text representation and counselor keywords
using SentenceTransformer ('BAAI/bge-small-en-v1.5').
Applies 2026 recency weighting when uptodate=True (boosts 2025-2027, penalizes 2010-2023).
Ranks links by Priority Tier and weighted similarity score. RAM-optimized.

#### `def export_dual_outputs(results, output_links_path, output_detailed_path, university_name, uptodate, exclude_keywords)`
**Description:** Generates two distinct files:
1. extracted_links.txt -> Clean list of canonical URLs only (one per line)
2. extracted_links_detailed.txt -> Full structured breakdown per link

#### `def export_partitioned_links(results, uni_slug, uni_name, uni_url)`
**Description:** Write one JSONL file per university to data/links/<slug>.jsonl.

This is the Phase 2 contract. The single shared extracted_links.txt cannot
satisfy it: an --hec batch run wrote 287 undifferentiated links of which ~284
were NUST, ~10 were LUMS and 0 were ITU, with nothing in the file recording
which university a given URL belonged to. Phase 2 provisions one notebook per
university and therefore needs the partition, plus the tier of each URL so
Phase 3 can scope its queries with source_ids.

#### `def load_partitioned_links(uni_slug)`
**Description:** Read back a per-university link partition written by export_partitioned_links.

#### `def run_pipeline(url, hec_mode, hec_limit, max_links, threshold, max_pages, uptodate, exclude_keywords, output_links, output_detailed)`
**Description:** Main orchestration function managing single site or HEC batch processing.

#### `def str2bool(v)`
**Description:** No docstring provided.

#### `def main()`
**Description:** No docstring provided.

---
## File: `ingest.py`
**Module Description:**
```text
Async NotebookLM Ingestion & Lifecycle Engine (ingest.py)

Provisions one notebook per university and uploads its curated link partition with
bounded concurrency, preserving the url -> source_id -> tier mapping that Phase 3
needs in order to scope each query to the sources that can answer it.
```

### Classes
#### `class IngestedSource`
**Description:** One successfully registered NotebookLM source and the tier it came from.

#### `class IngestResult`
**Description:** Outcome of ingesting one university.

Carries the tier mapping rather than a bare count. The previous signature
returned (notebook_id, count), which destroyed the tier association at the
Phase 2/Phase 3 boundary and forced every query to run unscoped.
**Methods:**
- `ingested_count(self)`: No docstring provided.
- `source_ids_for_tiers(self, tiers)`: No docstring provided.
- `tier_histogram(self)`: No docstring provided.

### Functions
#### `def _new_http_client()`
**Description:** Construct a pooled client, degrading gracefully when h2 is unavailable.

#### `def get_http_client()`
**Description:** Return the pooled client for the running loop, creating it on first use.

Every pre-flight probe and text-fallback fetch shares one connection pool
instead of paying a fresh TCP + TLS handshake per URL. Contains no await, so
concurrent callers cannot interleave and create duplicate clients.

#### `def close_http_client()`
**Description:** Close and drop the pooled client for the running loop.

#### `def http_session()`
**Description:** Scope the pooled HTTP client to a block, closing it on exit.

#### `def sanitize_url(url)`
**Description:** Sanitizes URL path by stripping trailing hyphens, stray punctuation, and malformed characters.

Prevents malformed/truncated URLs like '/program/bs-biotechnology-for-fall-2024-entry-'
from being generated or passed downstream to NotebookLM.

#### `def check_url_accessible(url, timeout, client)`
**Description:** Fast async pre-flight check to ensure URL returns HTTP 200 OK before sending to NotebookLM.

Filters out dead 404 links, 403 Forbidden bot blocks, or unreachable subdomains that would
cause Google NotebookLM's server-side crawler to fail with RPCError rpc_code=9.

Uses the shared HTTP/2 pool unless an explicit `client` is supplied.

#### `def fetch_and_extract_text(url, timeout, client)`
**Description:** Fetch web page content locally using custom browser headers and extract readable text and page title.

Used as a fallback when Google NotebookLM's server-side crawler fails to fetch the URL
(e.g., 403 blocks, SPAs, or Cloudflare protections).

Uses the shared HTTP/2 pool unless an explicit `client` is supplied.

#### `def _extract_id(obj)`
**Description:** Pull a source/notebook id out of an SDK object or a bare string.

#### `def wait_for_sources_adaptive(client, notebook_id, source_ids, timeout)`
**Description:** Wait for uploaded sources to reach `ready`, with jitter and per-source isolation.

Two problems with delegating straight to ``client.sources.wait_for_sources``:

  1. **Thundering herd.** Every source was uploaded within seconds of the
     others, so identical backoff schedules keep all N pollers landing on the
     same instants for the whole wait. A randomised first interval spreads
     them out permanently, since the offset survives every backoff step.

  2. **All-or-nothing reporting.** ``wait_for_sources`` raises if *any*
     single source times out or errors, which previously collapsed the whole
     result to ``ready_count = 0`` even when 59 of 60 sources were ready --
     discarding a usable notebook on one bad link.

Returns:
    The number of sources that actually reached ready state.

#### `def _find_or_create_notebook(client, title, uni_slug)`
**Description:** Reuse an existing notebook with this title, else create one.

Reuse matters for resumability: a run that crashed after uploading 40 of 60
sources must not leave an orphan notebook consuming a workspace slot and then
create a second one on retry.

#### `def ingest_university_sources(uni_slug, uni_name, links, client, max_sources)`
**Description:** Provision a notebook for one university and upload its curated links.

Args:
    uni_slug:    Filesystem/DB identifier for the university.
    uni_name:    Human-readable name, used for the notebook title.
    links:       Records from data/links/<slug>.jsonl. Each needs at least
                 {"url": str, "tier": int}. Plain strings are accepted and
                 default to tier 1.
    client:      A connected NotebookLMClient. Required -- the caller owns the
                 session lifecycle, because constructing one per university
                 would re-authenticate 83 times per batch.
    max_sources: Override for config.max_sources_per_notebook.

Returns:
    IngestResult carrying the notebook id and the full tier mapping.

---
## File: `inspect_cli.py`
**Module Description:**
```text
Developer-Grade Data Quality Auditor, System Inspection & Interactive CLI Workstation (inspect_cli.py)
Powered by Rich formatting, side-by-side comparison, global search, pipeline retry, vector export, and TUI menu.
```

### Functions
#### `def iter_all_records()`
**Description:** Yield normalized university payloads one at a time.

Streaming generator: only the current record plus the set of seen names is
resident, so dataset-wide analytics no longer scale their peak RAM with the
size of the corpus. `load_all_records()` remains the eager list form for
callers that genuinely need random access.

#### `def load_all_records()`
**Description:** Loads all university payload records from output JSONL or JSON files.

#### `def find_university_record(query)`
**Description:** Finds a single university payload by slug, name, or abbreviation.

#### `def extract_numeric_fee(fee_str)`
**Description:** Parses numeric PKR tuition fee from fee string.

#### `def inspect_university(query)`
**Description:** Deep inspect a specific university payload by slug or name.

#### `def compare_universities(slug1, slug2)`
**Description:** Compares two extracted university payloads side-by-side in a Rich Table.

#### `def search_programs(keyword, level, max_fee)`
**Description:** Searches across all extracted degree programs globally across all universities.

#### `def retry_pipeline(target)`
**Description:** Connects to SQLite state database and re-runs pipeline for failed/pending runs.

#### `def export_dataset(format_type, output_path, sync)`
**Description:** Exports structured outputs to CSV, Qdrant vectors, Pinecone Index, or Country-Grouped JSON.

#### `def audit_analytics(file_path)`
**Description:** Audits dataset health, metrics, and quality scores.

#### `def inspect_state()`
**Description:** Audits SQLite pipeline execution state.

#### `def inspect_schema()`
**Description:** Outputs colorized Master JSON Schema definition.

#### `def _inspect_notebooks_async()`
**Description:** No docstring provided.

#### `def inspect_notebooks()`
**Description:** No docstring provided.

#### `def interactive_menu()`
**Description:** Renders a Rich interactive menu prompting user choices.

#### `def main()`
**Description:** No docstring provided.

---
## File: `json_io.py`
**Module Description:**
```text
Streaming & crash-safe JSON I/O primitives (json_io.py)

Two guarantees the pipeline depends on:

  1. **Bounded memory.** Master aggregation streams the JSONL ledger record by
     record and writes the output array incrementally. Nothing here ever holds
     the whole dataset in RAM, so a 5,000-university ledger costs the same
     working set as a 5-university one.

  2. **No torn writes.** Every whole-file write lands in a sibling ``.tmp`` file
     that is flushed and fsynced before an atomic ``os.replace``. A crash mid-run
     therefore leaves either the previous complete file or the new complete file,
     never a half-serialised one.
```

### Functions
#### `def _fsync_dir(directory)`
**Description:** Persist a rename in the parent directory's own metadata.

#### `def atomic_write_text(path, text, encoding)`
**Description:** Write `text` to `path` via a fsynced temp file and an atomic rename.

#### `def atomic_write_json(path, obj, indent)`
**Description:** Serialise `obj` to `path` atomically. Use for small/medium objects.

#### `def append_jsonl(path, obj)`
**Description:** Append one record to a JSONL ledger and force it to disk.

The append is the pipeline's durable record of a completed university, so it
is fsynced immediately: an unflushed line lost to a crash would make the
university look unprocessed while its notebook had already been deleted.

#### `def iter_jsonl(path, skip_malformed)`
**Description:** Yield records from a JSONL file one at a time.

Malformed lines are skipped rather than aborting the sweep -- a single
truncated line from an old crash must not make the entire ledger unreadable.

#### `def _default_record_key(record)`
**Description:** Identity of a university payload, used for last-write-wins dedupe.

#### `def stream_compile_master_json(jsonl_path, master_path, indent, dedupe, key_fn)`
**Description:** Compile the JSONL ledger into the master JSON array in a single streamed pass.

This replaces the previous per-university "read every record, re-dump every
record" cycle, which was O(N^2) in both disk I/O and parse cost across a
batch: university 83 re-parsed and re-wrote the 82 payloads before it.
Aggregation now happens once, at the end of a run.

When `dedupe` is set, a re-run of an already-recorded university replaces its
earlier entry instead of appending a duplicate. The first pass records only
the *last* line number per key -- keys, not payloads -- so peak memory stays
proportional to the university count rather than the dataset size.

Returns:
    Number of records written to `master_path`.

---
## File: `notebook_logger.py`
**Module Description:**
```text
NotebookLM Lifecycle Audit Logger (src/notebook_logger.py)

Captures every stage of a NotebookLM notebook's life cycle:
  - NOTEBOOK_CREATED
  - SOURCE_UPLOADED
  - QUERY_EXECUTED
  - JSON_REPAIRED
  - NOTEBOOK_DELETED

Persists structured events to three destinations:
  1. loggings/notebook_lifecycle.log (human-readable log)
  2. loggings/notebook_audit.jsonl (machine-readable JSONL stream)
  3. data/state.sqlite (notebook_audit table via StateManager)
```

### Classes
#### `class NotebookLifecycleLogger`
**Description:** Thread-safe multi-destination lifecycle logger for NotebookLM workspaces.
**Methods:**
- `__init__(self)`: No docstring provided.
- `_timestamp(self)`: No docstring provided.
- `log_event(self, event_type, notebook_id, uni_slug, details)`: Core logging engine writing atomically to log, JSONL, and SQLite.
- `log_notebook_created(self, notebook_id, title, uni_slug)`: Log workspace creation event.
- `log_source_uploaded(self, notebook_id, source_id, url, status, duration_sec, uni_slug)`: Log source link upload event.
- `log_query_executed(self, notebook_id, query_index, query_key, prompt_len, response_bytes, duration_sec, uni_slug)`: Log query suite execution event.
- `log_json_repaired(self, notebook_id, query_key, fix_type, uni_slug)`: Log JSON syntax repair event.
- `log_notebook_deleted(self, notebook_id, trigger, uni_slug)`: Log atomic notebook deletion event.

### Functions
#### `def get_notebook_logger()`
**Description:** No docstring provided.

#### `def log_notebook_created(notebook_id, title, uni_slug)`
**Description:** No docstring provided.

#### `def log_source_uploaded(notebook_id, source_id, url, status, duration_sec, uni_slug)`
**Description:** No docstring provided.

#### `def log_query_executed(notebook_id, query_index, query_key, prompt_len, response_bytes, duration_sec, uni_slug)`
**Description:** No docstring provided.

#### `def log_json_repaired(notebook_id, query_key, fix_type, uni_slug)`
**Description:** No docstring provided.

#### `def log_notebook_deleted(notebook_id, trigger, uni_slug)`
**Description:** No docstring provided.

---
## File: `pipeline.py`
**Module Description:**
```text
Master 4-Phase Education Counselor RAG Pipeline Orchestrator (pipeline.py)

Automates the complete workflow:
Phase 1: High-Quality Link Harvesting & Deduplication
Phase 2: Programmatic NotebookLM Ingestion & Readiness Wait
Phase 3: Schema-Guided 5-Query Suite & Exa API Fallback
Phase 4: SQLite State Management & Data Quality Analytics
```

### Functions
#### `def _source_ids_by_tier(state_mgr, uni_slug)`
**Description:** Build {tier: [source_id, ...]} so each Phase 3 query is scoped to the sources
that can answer it. Returns None when nothing was recorded, which makes the
query suite fall back to searching all sources.

#### `def compile_master_json()`
**Description:** Rebuild data/outputs/university_counseling_data.json from the JSONL ledger.

Streamed and atomic: called once per run rather than once per university.

#### `def derive_uni_info(url, name_override)`
**Description:** Derives uni_name, uni_slug, and uni_domain from target URL.

#### `def run_master_pipeline(url, uni_name_override, max_links, exclude_keywords, uptodate, compile_master)`
**Description:** Executes the master 4-phase pipeline end-to-end for a target university.

Owns the StateManager lifecycle so an 83-university batch does not accumulate
one open SQLite handle per university. The pooled HTTP client deliberately
outlives this call and is closed once by the batch/CLI driver.

#### `def _run_master_pipeline(state_mgr, url, uni_name_override, max_links, exclude_keywords, uptodate, compile_master)`
**Description:** Phase 1-4 body. See run_master_pipeline for the public entry point.

#### `def setup_clean_logging()`
**Description:** Suppresses noisy HTTP and internal trace logs for clean CLI output.

#### `def backup_existing_outputs()`
**Description:** Moves existing data/outputs directory to a timestamped backup folder.

#### `def run_batch_pipeline(config_file_path, force_rerun_all)`
**Description:** Reads config.json, checks for processed universities, backs up if rerun requested,
executes remaining links with tqdm progress bars and clean logging, and saves result.json analytics.

#### `def generate_result_analytics(config_path)`
**Description:** Generates result.json containing global dataset analytics.

#### `def main()`
**Description:** No docstring provided.

---
## File: `qdrant_validator.py`
**Module Description:**
```text
Automated Qdrant 10-Query Benchmark Suite & Rollback Protection Engine
(src/qdrant_validator.py)

Executes 10 comprehensive natural language queries against the newly synced
Qdrant Cloud database, validates score thresholds (>0.50) and metadata payload
completeness, and automatically rolls back if validation fails (<80% pass rate).
```

### Classes
#### `class QueryBenchmark`
**Description:** No docstring provided.

### Functions
#### `def validate_qdrant_database(q_client, collection_name, model_name, score_threshold)`
**Description:** Executes the 10-query benchmark suite against the active Qdrant collection.

Returns (is_passed: bool, detailed_results: List[Dict]).

---
## File: `schema.py`
**Module Description:**
```text
Executable Pydantic Schemas for Education Counselor System (schema.py)
Serves as single source of truth for prompts, validation, and JSON export.
```

### Classes
#### `class UniversityType`
**Description:** No docstring provided.

#### `class DegreeLevel`
**Description:** No docstring provided.

#### `class ApplicationStatus`
**Description:** No docstring provided.

#### `class RankingItem`
**Description:** No docstring provided.

#### `class KeyLinks`
**Description:** No docstring provided.

#### `class MainInfo`
**Description:** No docstring provided.
**Methods:**
- `default_language(cls, v)`: No docstring provided.
- `default_type(cls, v)`: No docstring provided.

#### `class EligibilityRequirements`
**Description:** No docstring provided.

#### `class ProgramItem`
**Description:** No docstring provided.
**Methods:**
- `_coerce_summary(cls, v)`: No docstring provided.

#### `class ProgramCategoryBlock`
**Description:** No docstring provided.

#### `class FacultyItem`
**Description:** No docstring provided.

#### `class SubCampusContact`
**Description:** No docstring provided.

#### `class ContactInfo`
**Description:** No docstring provided.
**Methods:**
- `_coerce_phone_numbers(cls, v)`: Accept numbers, or a single string, where a list of strings is expected.
- `_coerce_sub_campuses_contact(cls, v)`: Coerce string representations of sub-campus contacts into SubCampusContact dicts.

#### `class UniversityPayload`
**Description:** No docstring provided.

---
## File: `state.py`
**Module Description:**
```text
SQLite state machine for tracking Education Counselor pipeline execution states.

Three responsibilities:
  1. pipeline_state -- per-university resumable status, so a crashed 83-university
     batch restarts from where it stopped instead of re-ingesting everything.
  2. source_map     -- url -> source_id -> tier, so Phase 3 can scope each query to
     the sources that can actually answer it (chat.ask(source_ids=...)).
  3. query_ledger   -- an append-only count of NotebookLM queries per UTC day, so
     the 500/day Pro ceiling is enforced by the pipeline rather than discovered
     when the API starts refusing halfway through a run.
```

### Classes
#### `class InvalidStatusError`
**Description:** Raised when a caller attempts a status outside the declared lifecycle.

#### `class QuotaExceededError`
**Description:** Raised when the daily NotebookLM query budget would be exceeded.

#### `class StateManager`
**Description:** Manages pipeline execution state per university in SQLite database.
**Methods:**
- `__init__(self, db_path)`: No docstring provided.
- `_connect(self)`: Open one tuned connection. Pragmas are set once, outside any transaction.
- `_get_connection(self)`: Return this thread's persistent connection, opening it on first use.
- `close(self)`: Close this thread's connection, if one is open.
- `__enter__(self)`: No docstring provided.
- `__exit__(self, exc_type, exc, tb)`: No docstring provided.
- `_init_db(self)`: Initialize tables and indexes if they do not exist.
- `_validate_status(status)`: No docstring provided.
- `get_status(self, slug)`: Retrieve current execution status for given university slug.
- `get_completed_slugs(self)`: Retrieve list of university slugs that completed processing successfully.
- `get_state(self, slug)`: Retrieve full state dictionary for given university slug.
- `set_status(self, slug, status, notebook_id, sources_ingested, queries_executed, error_log)`: Upsert pipeline state, preserving any field the caller left as None.
- `is_complete(self, slug)`: True when this university needs no further work in a resumed run.
- `list_all(self)`: Retrieve all pipeline state records sorted by latest update time.
- `reset_state(self, slug)`: Reset one university, or everything when slug is None or 'all'.
- `record_sources(self, slug, entries)`: Persist (source_id, url, tier) triples for a university.
- `clear_sources(self, slug)`: Drop a university's recorded sources without touching its pipeline state.
- `get_source_ids(self, slug, tiers)`: Source ids for a university, optionally restricted to given tiers.
- `count_sources_by_tier(self, slug)`: Tier histogram of ingested sources; drives programs_possibly_truncated.
- `_today()`: No docstring provided.
- `queries_used_today(self)`: Total NotebookLM queries recorded for the current UTC day.
- `remaining_query_budget(self)`: Queries still available today against config.daily_query_budget.
- `reserve_queries(self, slug, count)`: Record `count` queries against today's budget, refusing to overrun it.
- `record_notebook_audit(self, event_type, notebook_id, uni_slug, details)`: Record a structured NotebookLM lifecycle event into SQLite.
- `list_notebook_audits(self, notebook_id, limit)`: Retrieve audit log records sorted by latest timestamp.

---
## File: `universal_normalizer.py`
**Module Description:**
```text
Universal Schema Normalizer & Data Enrichment Engine
(src/universal_normalizer.py)

Provides deterministic, cross-country normalization for:
1. Universal Currency Resolution (EUR, USD, GBP, PKR, CHF, CAD, AUD, etc.)
2. Tuition Fee & Application Fee Normalization (e.g., Tuition-Free German public policy)
3. International Eligibility Requirements (Abitur NC, ECTS, GPA, HSSC)
4. Identity & Metadata Completion (Established Year, Accreditation Body, City)
```

### Functions
#### `def load_global_registry()`
**Description:** No docstring provided.

#### `def resolve_universal_currency(tuition_str, country)`
**Description:** Detects exact currency from tuition text or country locale.

#### `def normalize_universal_program(prog, country)`
**Description:** Applies universal normalization rules to a single program dictionary.

#### `def normalize_universal_payload(record)`
**Description:** Applies universal schema normalization across the entire record dictionary.

---