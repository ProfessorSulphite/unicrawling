# Interactive CLI Expansion & NotebookLM Lifecycle Logging Architecture Plan (`CLI_plan.md`)

This document presents the detailed architectural design and implementation plan for upgrading the **Education Counselor RAG CLI** with interactive features (`diff`, `search`, `retry`, `export`, `interactive` TUI menu) and structured **NotebookLM Lifecycle Audit Logging**.

---

## 1. Executive Summary & Objectives

The goal of this upgrade is twofold:
1. **Interactive CLI Expansion**: Transform `cli.py` / `src/inspect_cli.py` into a full-featured developer workstation tool allowing users to compare universities side-by-side, search degree programs globally across all extracted universities, export datasets into CSV or Vector DB chunks, retry failed state pipelines, and use an interactive TUI menu.
2. **NotebookLM Lifecycle Audit Logging**: Implement dedicated structured logging (`loggings/notebook_lifecycle.log` & `loggings/notebook_audit.jsonl`) that captures every stage of a NotebookLM notebook's life: creation, source link upload, query streaming execution, JSON repair events, and atomic notebook deletion.

---

## 2. Master Architecture Overview

```mermaid
flowchart TD
    subgraph CLI Workstation Interface (cli.py / src/inspect_cli.py)
        A[cli.py Entrypoint] --> B{Command Selector}
        B -->|inspect| C[University Profile Panel]
        B -->|diff| D[Rich Side-by-Side Comparison]
        B -->|search| E[Global Program & Fee Search]
        B -->|retry| F[Resumable SQLite Pipeline Retry]
        B -->|export| G[CSV & Vector DB Chunk Exporter]
        B -->|interactive| H[Interactive TUI Prompt Menu]
    end

    subgraph NotebookLM Lifecycle Audit Logger (src/notebook_logger.py)
        I[NotebookLM Activity] -->|Creation Event| J[loggings/notebook_lifecycle.log]
        I -->|Source Ingest Event| J
        I -->|Query Stream Event| K[loggings/notebook_audit.jsonl]
        I -->|Deletion Event| K
        K --> L[SQLite notebook_audit Table]
    end
```

---

## 3. Component Specifications & Proposed Changes

### Component A: Interactive CLI Expansion (`cli.py` & `src/inspect_cli.py`)

#### 1. `cli.py diff <slug1> <slug2>` (Side-by-Side Comparison)
Compares two extracted universities in a Rich side-by-side table:
- **Metrics Compared**: Institution Type, City, Portal URL status, Total Degree Programs (BS, MS, PhD), Tuition Fee range, Fellowship Availability, Admission Deadlines, and Primary Contact Info.

#### 2. `cli.py search <keyword>` (Global Program & Fee Search)
Searches across all extracted degree programs across all universities in `data/outputs/uni_outputs/*.json` or master datasets:
- Filters by keyword, program level (`BS`, `MS`, `PhD`), max tuition fee, or aggregate rules.
- *Examples*:
  - `python3 cli.py search "data science"`
  - `python3 cli.py search "scholarship"`
  - `python3 cli.py search "cyber security"`

#### 3. `cli.py retry [slug|failed|pending]` (Pipeline Resumability)
Connects directly to `src/state.py` SQLite state database to find failed or pending runs and automatically re-executes `src/pipeline.py` for those universities.

#### 4. `cli.py export [--format csv|qdrant|json]` (Data Exporter)
Exports structured outputs:
- **`--format csv`**: Flattens degree programs into a CSV spreadsheet for non-technical counselors.
- **`--format qdrant`**: Chunks programs and faculties into vector embedding payloads with official web source citations.

#### 5. `cli.py interactive` (Interactive TUI Menu)
Renders a Rich interactive menu prompting the user to select an action without typing CLI flags:
```
=====================================================
🎓 Education Counselor RAG Developer Workstation
=====================================================
1. 🏛️ Deep Inspect University
2. ⚔️ Compare Two Universities (Diff)
3. 🔍 Search Degree Programs Globally
4. 🔄 Retry Failed / Pending Pipeline Runs
5. 📊 View Dataset Analytics Dashboard
6. 🗄️ Inspect SQLite State Manifest
7. 📜 Display JSON Schema
8. ☁️ List Active NotebookLM Notebooks
9. 📤 Export Dataset (CSV / Vector DB)
0. 🚪 Exit
```

---

### Component B: NotebookLM Lifecycle Audit Logger (`src/notebook_logger.py`)

Dedicated logger capturing the complete lifecycle of NotebookLM cloud notebooks:

#### Log Target Locations:
1. **`loggings/notebook_lifecycle.log`**: Human-readable formatted log file.
2. **`loggings/notebook_audit.jsonl`**: Machine-readable JSONL audit trail.
3. **`data/state.sqlite` (Table: `notebook_audit`)**: SQLite table tracking active & deleted notebooks.

#### Structured Audit Event Types:
- **`NOTEBOOK_CREATED`**: Timestamp, `notebook_id`, title, `uni_slug`.
- **`SOURCE_UPLOADED`**: `notebook_id`, `source_id`, `url`, readiness status, duration.
- **`QUERY_EXECUTED`**: `notebook_id`, query index (1-5), prompt length, raw response byte length, streaming duration.
- **`JSON_REPAIRED`**: `notebook_id`, syntax fix applied (stripping markdown fences / citation markers).
- **`NOTEBOOK_DELETED`**: Timestamp, `notebook_id`, deletion trigger, freed slot status.

---

## 4. Verification & Testing Plan

### Automated Tests (`tests/test_cli_interactive.py`)
- Unit tests verifying `diff` comparison formatting logic.
- Search query filtering tests across mock university payloads.
- Audit logger tests verifying that notebook creation and deletion emit valid JSONL records.

### Manual Verification Commands
```bash
# 1. Test side-by-side diff:
python3 cli.py diff itu ncbae

# 2. Test global program search:
python3 cli.py search "computer science"

# 3. Test interactive TUI menu:
python3 cli.py interactive

# 4. Test NotebookLM audit log inspection:
cat loggings/notebook_audit.jsonl | jq .
```
