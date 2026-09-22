# Master Performance, Memory & Concurrency Optimization Plan
**Target Execution Model:** Claude Opus 5 (Architectural Lead) & Subagents (Task Workers)  
**Project:** Education Counselor Multi-Agent RAG System (`notebooklm_scripts`)  
**Document Status:** Actionable Master Engineering Specification  

---

## 1. Executive Architecture & Performance Overview

The Education Counselor Multi-Agent RAG System processes higher education datasets across Pakistan (HEC directory, degree requirements, fees, program details, and faculty registries) through a 4-phase automated pipeline:
1. **Phase 1 (Crawl4AI & Sanitization)**: Playwright/Crawl4AI deep link discovery, link filtering, and canonical degree deduplication.
2. **Phase 2 (NotebookLM Provisioning & Ingestion)**: Dynamic notebook creation, URL accessibility pre-flight verification, source batch uploading, and readiness wait loops.
3. **Phase 3 (Schema Extraction & Exa Fallback)**: 5-query suite execution via NotebookLM, citation stripping/JSON repair, schema validation via Pydantic, and Exa Web Search API gap filling.
4. **Phase 4 (State Management & Analytics)**: SQLite WAL-mode state ledger, vector indexing in Qdrant, and CLI health auditing.

### Identified Bottlenecks & Targets
| Phase / System Component | Current Bottleneck / Vulnerability | Root Cause | Target Optimization |
| :--- | :--- | :--- | :--- |
| **Phase 1: Web Crawling** | Memory accumulation & Playwright context leaks over long runs | Unbound DOM trees, Playwright browser instances spawned without connection pooling | **50% RAM reduction**; shared headless browser pool with strict page lifecycle guards |
| **Phase 2: Source Ingestion** | Unnecessary pre-flight HTTP latency & fixed wait intervals | `httpx.AsyncClient` created per URL, fixed `asyncio.sleep(5)` poll loops | **60% speedup**; persistent HTTP/2 connection pool & jittered exponential backoff readiness polling |
| **Phase 3: Schema Extraction** | $O(N^2)$ File I/O & serial NotebookLM query execution | Re-reading/writing master `university_counseling_data.json` array every run; sequential 5-query execution | **75% reduction in disk I/O & 40% speedup in query phase**; streaming JSON writes & semaphore-guarded parallel query execution |
| **Phase 4: SQLite & Vector Store** | Connection overhead & full in-memory dataset loads in audit CLI | SQLite connection opened/closed per query operation; non-streaming JSON parsing | Connection pooling/WAL mode optimization; generator-based streaming dataset evaluation |

---

## 2. Model Delegation & Multi-Agent Work Division

To maximize efficiency, balance token consumption, and execute with surgical precision, Claude Opus 5 will orchestrate the refactoring by delegating task execution based on complexity.

```mermaid
flowchart TD
    subgraph Opus 5 Master Orchestrator
        A[Architecture Review & Async Core Redesign] --> B[Sequential Thinking & Task Partitioning]
    end

    subgraph Subagent / Lower Model Tasks (Flash / Flash-Lite)
        B -->|Low-Hanging Fruit| C[Regex Pre-compilation & String Parsing]
        B -->|Low-Hanging Fruit| D[SQLite Indexing & Parametrized Queries]
        B -->|Low-Hanging Fruit| E[Unit Test Suite Expansion & Mock Fixtures]
        B -->|Low-Hanging Fruit| F[Logging Standardization & Docstrings]
    end

    subgraph Claude Opus 5 Core Implementation
        B -->|Complex Task| G[Playwright Shared Pool & Stream Memory Bounds]
        B -->|Complex Task| H[Parallelized Async Query Engine with Semaphores]
        B -->|Complex Task| I[Streaming JSON Parsing & $O(N^2)$ Disk I/O Fix]
        B -->|Complex Task| J[Adaptive Backoff & Rate-Limited Batch Ingest]
    end

    C & D & E & F & G & H & I & J --> K[Pytest Verification & Benchmark Auditing]
```

### Operational Rules for Claude Opus 5 During Execution

1. **Context Window Management (`/compact`)**:
   - As implementation progresses across multiple files, execute `/compact` whenever context depth exceeds standard operational limits or after finishing major milestones.
   - Use the `context-guard` skill to actively monitor context token pressure.

2. **Sequential Thinking Protocol**:
   - Activate the `sequentialthinking` MCP tool before implementing complex async changes, refactoring lock mechanisms, or changing state machine protocols.

3. **Subagent Delegation Strategy**:
   - Spawn subagents (`invoke_subagent` with `flash` or `flash_lite`) for low-hanging fruit: refactoring standalone regexes, adding database indexes, writing isolated mock unit tests, or cleaning up logging statements.
   - Retain Claude Opus 5 for high-reasoning tasks: memory leak isolation, async deadlock prevention, parallel query orchestration, and core architectural refactoring.

---

## 3. Comprehensive Optimization Strategy by Module

### 3.1. Phase 1 (`src/extract_links.py`): Web Crawling & Link Extraction

#### Bottlenecks
- Inefficient Playwright context handling leading to browser process leaks.
- Synchronous regex compilation inside hot parsing loops.
- In-memory collection of huge URL trees before sanitization and deduplication.

#### Actionable Refactoring Tasks
- [ ] **Task 1.1 (Opus 5)**: Implement a singleton/pooled browser context manager for Crawl4AI / Playwright that reuses browser instances across universities while strictly closing browser pages upon completion.
- [ ] **Task 1.2 (Subagent / Flash)**: Move all regular expression compilations (e.g. key link patterns, asset filters, trailing slash strippers) to module-level `re.compile(...)` constants.
- [ ] **Task 1.3 (Opus 5)**: Replace heavy BeautifulSoup recursive string matching in link classification with lightweight `selectolax` parser or optimized single-pass DOM traversals.
- [ ] **Task 1.4 (Subagent / Flash)**: Add streaming output capabilities so extracted links are written directly to disk incrementally instead of holding large data structures in memory.

---

### 3.2. Phase 2 (`src/ingest.py`): NotebookLM Ingestion & Pre-Flight Checks

#### Bottlenecks
- `httpx.AsyncClient` created repeatedly inside `check_url_accessible` and `fetch_and_extract_text`.
- Linear pre-flight URL verification (checking URLs one by one).
- Polling `source list` with static `asyncio.sleep(5)` intervals causing long idle delays.

#### Actionable Refactoring Tasks
- [ ] **Task 2.1 (Opus 5)**: Refactor `check_url_accessible` and `fetch_and_extract_text` to accept a shared persistent `httpx.AsyncClient` with HTTP/2 enabled, custom connection limits (`limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)`), and SSL verification handling.
- [ ] **Task 2.2 (Opus 5)**: Implement concurrent pre-flight URL accessibility checking using `asyncio.gather` with an `asyncio.Semaphore(15)` boundary.
- [ ] **Task 2.3 (Opus 5)**: Replace fixed 5-second polling loops in readiness checks with an adaptive exponential backoff strategy (initial check at 1.5s, scaling up to 5s, with random jitter to prevent thundering herd).
- [ ] **Task 2.4 (Subagent / Flash)**: Add defensive URL sanitization helpers and optimize link tier mapping logic using set-based lookups instead of list iterations.

---

### 3.3. Phase 3 & Pipeline (`src/extract_data.py` & `src/pipeline.py`): Schema Extraction & File I/O

#### Bottlenecks
- **Severe Disk I/O Bottleneck in `src/pipeline.py`**:
  ```python
  # Current implementation in pipeline.py (Lines 167-175):
  master_json_path = output_file.parent / "university_counseling_data.json"
  all_records = []
  if output_file.exists():
      with open(output_file, "r", encoding="utf-8") as f:
          for line in f:
              all_records.append(json.loads(line))
  with open(master_json_path, "w", encoding="utf-8") as f:
      json.dump(all_records, f, indent=2, ensure_ascii=False)
  ```
  *Analysis*: Every single university execution re-reads the *entire* historical JSONL file into memory, parses every JSON object, and re-writes the entire `university_counseling_data.json` file. For 100 universities, this scales as $O(N^2)$ reads and writes, consuming gigabytes of redundant I/O and RAM!

- **Sequential Query Execution in Phase 3**:
  - Queries Q1 through Q5 execute sequentially against NotebookLM despite some having zero dependencies on each other.

#### Actionable Refactoring Tasks
- [ ] **Task 3.1 (Opus 5 - High Priority)**: Eliminate the $O(N^2)$ master file write bottleneck. Implement an atomic single-record appender or batch compilation script (`compile_master_json.py`) that only aggregates master JSON at the end of a full pipeline run, or updates the master JSON file using streaming JSON writing.
- [ ] **Task 3.2 (Opus 5)**: Refactor `extract_university_payload` to execute independent queries (Q1, Q2, Q3, Q4, Q5) concurrently using `asyncio.gather` with a configurable `asyncio.Semaphore(3)` to stay within API rate limits.
- [ ] **Task 3.3 (Subagent / Flash)**: Optimize `strip_citation_markers` and `_balanced_span` in `src/extract_data.py`. Pre-compile citation regexes and optimize string buffer concatenations using list joins.
- [ ] **Task 3.4 (Subagent / Flash)**: Optimize Pydantic schema validation by using compiled `TypeAdapter` objects at the module level rather than instantiating TypeAdapters per query function call.

---

### 3.4. Phase 4 & Utilities (`src/state.py`, `src/inspect_cli.py`, `query_qdrant.py`)

#### Bottlenecks
- `StateManager._get_connection()` opens a new SQLite connection on every method invocation.
- `inspect_cli.py` reads the entire JSON payload into RAM to calculate data health metrics.
- Unbatched point insertion in Qdrant vector store scripts.

#### Actionable Refactoring Tasks
- [ ] **Task 4.1 (Opus 5)**: Implement a thread-safe connection pool or thread-local persistent connection context manager in `StateManager` (`src/state.py`) with `PRAGMA synchronous = NORMAL;` and `PRAGMA temp_store = MEMORY;`.
- [ ] **Task 4.2 (Subagent / Flash)**: Ensure indexes exist for all foreign keys and frequently queried fields in SQLite (`pipeline_state`, `source_map`, `query_ledger`).
- [ ] **Task 4.3 (Opus 5)**: Refactor `inspect_cli.py` to stream JSON records line-by-line (`ijson` or standard generator `json.loads` over lines) for memory-efficient dataset analytics without loading the entire payload into RAM.
- [ ] **Task 4.4 (Opus 5 / Flash)**: Batch vector embeddings and Qdrant payload upserts in `query_qdrant.py` / `src/qdrant_validator.py` with configurable chunk sizes (e.g. 64 records per batch).

---

## 4. Phased Implementation Plan & Execution Milestones

### Phase A: Low-Hanging Fruit & Fast Wins (Subagents / Flash)
*Target: Immediate 20% speedup and code cleanup with low risk.*
1. **Module-level Regex Compilation**: Refactor all inline `re.compile` calls across `src/extract_links.py`, `src/extract_data.py`, and `src/ingest.py`.
2. **Pydantic TypeAdapter Caching**: Move `TypeAdapter` initializations in `src/extract_data.py` to global scope.
3. **Database Index Verification**: Add explicit composite indexes in `src/state.py` for common query paths.
4. **Pytest Verification**: Execute `pytest tests/` to confirm zero regressions.

### Phase B: Core Async & I/O Optimization (Claude Opus 5)
*Target: Eliminate $O(N^2)$ disk I/O and accelerate network operations by 50%+.*
1. **Eliminate Master File Write Bottleneck**: Update `src/pipeline.py` to log payloads incrementally to JSONL and defer full master array aggregation.
2. **Shared HTTP/2 Client Pool**: Refactor `src/ingest.py` to use a global, lifecycle-managed `httpx.AsyncClient`.
3. **Concurrent Pre-Flight Checks & Backoff Polling**: Implement `asyncio.Semaphore` bounded URL pre-flight checks and jittered backoff readiness polling in `src/ingest.py`.
4. **Pytest Verification**: Validate pipeline resilience using `pytest tests/test_ingest_resilience.py`.

### Phase C: Concurrency Engine & Memory Hardening (Claude Opus 5)
*Target: Reduce Phase 3 query time by 40% and enforce RAM bounds.*
1. **Parallel Phase 3 Queries**: Refactor `extract_university_payload` in `src/extract_data.py` to execute independent queries concurrently with semaphore rate limiting.
2. **Playwright Process Lifecycle**: Enforce single browser context re-use with strict cleanup hooks in `src/extract_links.py`.
3. **Streaming Health Inspector**: Update `src/inspect_cli.py` to calculate analytics using generator streams.
4. **Pytest & Benchmark Verification**: Run full integration test suite (`pytest tests/test_pipeline.py`).

---

## 5. Verification, Benchmarking & Safety Guardrails

### Verification Protocol
After completing each phase or subagent task, execute the following validation commands to ensure system integrity:

```bash
# 1. Run unit and integration tests
pytest tests/ -v

# 2. Test state management integrity
python3 -c "from src.state import StateManager; sm = StateManager(); print('DB Health:', sm.get_status('test'))"

# 3. Test CLI inspector performance
python3 src/inspect_cli.py --summary
```

### Safety Guardrails
- **Daily NotebookLM Budget Protection**: Ensure `query_ledger` checks in `src/state.py` remain strictly atomic so concurrent query execution never exceeds the 500 query/day Pro threshold.
- **Zero Data Loss Integrity**: All payload writes must write to `.tmp` files before atomic rename operations to prevent partial JSON corruption during unexpected shutdowns.
- **Backwards Compatibility**: All public module signatures and schema definitions (`src/schema.py`) must remain fully compatible with existing RAG client interfaces.

---

*Plan prepared by Software Architect & Optimization Engineering Lead.*
