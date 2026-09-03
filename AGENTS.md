# Education Counselor Multi-Agent RAG System Architecture (AGENTS.md)

This document outlines the multi-agent system architecture designed to collect, process, index, and retrieve official university guidance data for the **AI Education Counselor System**.

---

## Master Architecture Overview

```mermaid
flowchart TD
    subgraph Phase 1: High-Quality Discovery & Extraction
        A[Agent 1: Web Crawling & Link Extraction Agent] -->|Produces extracted_links.txt & detailed report| B[Quality Filter, Tier Classifier & Deduplicator]
    end

    subgraph Phase 2: Programmatic Ingestion & Notebook Lifecycle
        B -->|Prioritized Quality URLs| C[Agent 2: NotebookLM Ingestion & Lifecycle Manager Agent]
        C -->|Provisions Notebooks & Uploads Sources| D[NotebookLM Source Queue]
    end

    subgraph Phase 3: Schema-Guided Extraction & Exa Enrichment
        D -->|Ready Web Sources| E[Agent 3: Schema Query & Exa Enrichment Agent]
        E <-->|Exa API Search for Missing Rankings/Portals| F[Exa Web Search API]
        E -->|Structured 4-Block JSON Payload| G[(Vector DB: Qdrant / Chroma / PGVector)]
    end

    subgraph Phase 4: Data Quality Audit & Interactive Counseling
        G --> H[Agent 4: Data Quality Auditor & Interactive Counselor Agent]
        H -->|Inspect CLI Analytics & Reports| I[Data Quality Dashboard]
        H -->|RAG Student Queries| J[Student UI / Chatbot]
    end
```

---

## Agent Specifications & Responsibilities

### Agent 1: Web Crawling & Link Extraction Agent (`extract_links.py`)
* **Role**: Primary discovery and link sanitization agent for HEC-recognized universities and high-value sub-pages.
* **Execution Modes**:
  - **Standalone CLI**: Can be run independently (`python3 extract_links.py --url ...` or `--hec`).
  - **Automated Pipeline**: Executed automatically as Phase 1 in the end-to-end master pipeline.
* **Responsibilities**:
  - Discover official recognized universities from the Higher Education Commission (HEC) directory.
  - Execute Crawl4AI `BestFirstCrawlingStrategy` across target university domains.
  - Enforce strict **Zero Garbage Policy** (strip portals, logins, tenders, job posts, legal disclaimers, static assets).
  - Filter out news and event links via `--exclude-keywords "news|events"`.
  - Apply 2026 recency score boosting via `--uptodate true`.
  - Perform **Canonical Degree Deduplication** (`deduplicate_canonical_degree_links`) to merge multi-campus subdomain mirrors and intake year duplicates.
  - Export dual text outputs: `extracted_links.txt` (URLs only) and `extracted_links_detailed.txt` (Full metadata breakdown).

---

### Agent 2: NotebookLM Ingestion & Lifecycle Manager Agent (Phase 2)
* **Role**: NotebookLM workspace provisioning and batch source ingestion agent.
* **Responsibilities**:
  - Provision isolated NotebookLM notebooks per university (`notebooklm create "UniName_Counseling_DB"`).
  - Manage Pro account quotas (300 sources per notebook, 500 queries per day).
  - Batch upload URLs from the per-university partition (`data/links/<slug>.jsonl`) into NotebookLM sources, over a shared HTTP/2 connection pool.
  - Monitor processing readiness **per source**, with jittered exponential backoff and failure isolation: a source that times out or errors is reported as not-ready without discarding the sources that did succeed, so the notebook remains queryable against its ready corpus.
  - Persist the `url -> source_id -> tier` map to SQLite so Agent 3 can scope each query with `source_ids`.
  - Automatically delete notebooks after JSON extraction to clear workspace slots while preserving raw link files and JSON outputs.

---

### Agent 3: Schema Query & Exa Enrichment Agent (Phase 3)
* **Role**: Schema-guided query extraction and Exa API fallback agent.
* **Responsibilities**:
  - Execute a 5 to 6 targeted query suite per university against NotebookLM using strict JSON schema prompt files:
    - **Q1**: `main_info` (Metadata, Rankings, Academics/Admissions/Application Portal URLs).
    - **Q2**: `programs.bachelors` (BS, BSc, BA, BBA, MBBS, LLB, PharmD, DPT with full-paragraph descriptions, fees, eligibility, admission requirements, deadlines).
    - **Q3**: `programs.masters` (MS, MSc, MA, MBA, MPhil, LLM with full-paragraph descriptions, fees, eligibility, admission requirements, deadlines).
    - **Q4**: `programs.phd` (research doctorates only; post-doctoral fellowships are excluded).
    - **Q4b**: `programs.diploma` (postgraduate diplomas, PGDs, certificates) — added in C17.
    - **Q5**: `faculties` (Faculties, Schools, and constituent departments).
    - **Q6**: `contact` (Emails, phone numbers, physical address, admissions desk).
  - Reserve the suite against the 500/day NotebookLM budget **before** issuing any query, refusing to start a university that cannot complete within the remaining allowance.
  - Trigger **Exa API web search fallback** if ranking data or application portal URLs are missing from NotebookLM sources.
  - Assemble complete 4-block JSON payload and append to the fsynced append-only ledger `university_counseling_data.jsonl`. The master JSON array is aggregated separately in one streamed pass at the end of a run, never per-university.

---

### Agent 4: Data Quality Auditor & Interactive Counselor Agent (Phase 4)
* **Role**: Data health analytics, inspection, and conversational counseling agent.
* **Responsibilities**:
  - Execute the **Inspect CLI Utility** (`inspect_cli.py`) to audit dataset health:
    - Unique university count and type distribution (Public vs Private).
    - Total program breakdown (Bachelors, Masters, PhD, Diploma counts).
    - Empty and NaN field audit across all 4 blocks.
    - Application portal link coverage and completeness rankings.
  - Process natural language student inquiries against the Vector DB with direct official source citations and links.
