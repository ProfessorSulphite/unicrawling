# Dual-Agent Master Execution Plan & Architecture (v3.0)

**Date:** 2026-07-24 · **Status:** Partitioned Dual-Agent Execution Plan
**Role Assignments:**
- **Part A (Gemini 3.6 Flash)**: High-Budget Heavy Execution, Phase 1 Engine Refactoring, File System Setup, CHANGELOG Tracking, & Production Test Execution.
- **Part B (Claude Opus 4.8)**: High-Precision Core Engineering (`schema.py`, `ingest.py`, `extract_data.py`, `state.py`) & Production-Level Pytest Suite Authoring.

---

## 0. Model Quota & Context Optimization Protocols

### ⚡ Gemini 3.6 Flash Protocol (High Usage Budget)
- **Role**: Heavy CLI execution, batch site crawling, link sanitization, environment initialization, CHANGELOG Part 1 management, and production test suite execution.
- **Quota**: Full session budget available. Unrestricted tool calls for file operations, compilation, and batch execution.

### 🧠 Claude Opus 4.8 Protocol (Constrained Budget: ~10% Session Quota Remaining)
- **Role**: High-precision Pydantic schemas, async connection pooling, JSON repair regexes, SQLite transaction state machine, and production-level Pytest suite authoring.
- **Context Guard & Token Preservation Rules**:
  1. **Subagent Model Delegation**: MUST delegate broad codebase search or research tasks to lighter models (`Model='flash_lite'` or `Model='flash'`). NEVER spawn subagents with `pro` or `inherit` unless strictly necessary.
  2. **CHANGELOG First Protocol**: MUST read `CHANGELOG.md` Part 1 before initiating work to pick up Gemini 3.6 Flash's progress without requesting re-explanations.
  3. **Sequential Thinking Efficiency**: Use `sequentialthinking` concisely (max 2-3 focused thoughts per tool call) to conserve prompt tokens.
  4. **Headroom Compression**: Use `headroom` mcp for compression and for optimizing token usage to avoid hitting usage limit.

---

## 1. PART A: Gemini 3.6 Flash Execution Plan

*(Handled by Gemini 3.6 Flash — Full Usage Capacity)*

### Task A1: Environment & File System Setup
- Create workspace directory structure:
  - `data/links/` (Partitioned per-university link files)
  - `data/outputs/` (Exported JSONL payloads)
  - `resources/` (Rankings DB & planning artifacts)
  - `tests/` (Production test directory)
- Create `config.py` dataclass for central configuration (replacing 10-arg function signatures).
- Populate `resources/rankings_pk.json` with static Webometrics/QS Pakistan university rankings.

### Task A2: Phase 1 Engine Refactoring (`extract_links.py`)
- **Tokenized Path Exclusion**: Implement `is_excluded_path()` matching against path tokens (`re.split(r'[/_.-]', path)`), stripping `"portal"` and `"admin"` from path blocklists.
- **URL Sanitization**: Implement `normalize_url()` with `html.unescape()`, zero-width character stripping, double-slash collapse, and tracking parameter removal.
- **Dynamic Year Decay**: Implement `compute_year_decay_factor()` comparing candidate years against `datetime.now().year` (2026).
- **Proportional Tier Quotas**: Implement `allocate_proportional_tier_quotas()` (Tier 1: 45%, Tier 2: 30%, Tier 3: 15%, Tier 4: 10%), capping total sources at 60 URLs/university.
- **Structured Token Key Dedup**: Implement `get_discipline_tokens()` matching `(degree_level, sorted(discipline_tokens))`.
- **Partitioned Link Export**: Output per-university links to `data/links/<uni_slug>.jsonl`.

### Task A3: CHANGELOG Part 1 Maintenance
- Record all code changes, function signatures, URL normalization metrics, and link yields into `CHANGELOG.md` (Section 1).

### Task A4: Production Test Execution & Verification
- Execute production Pytest suite (`pytest tests/`) authored by Claude Opus 4.8.
- Verify end-to-end link harvesting, zero garbage filtering, Pydantic validation, and SQLite state transitions.

---

## 2. PART B: Claude Opus 4.8 Architecture & Testing Plan

*(Handled by Claude Opus 4.8 — Low Token Budget ~10%, High Precision)*

### Task B1: Core Executable Pydantic Schemas (`schema.py`)
- Define strict, executable Pydantic models serving as single source of truth for prompts and validation:
  - `UniversityPayload`, `MainInfo`, `ProgramItem`, `FacultyItem`, `ContactInfo`, `EligibilityRequirements`, `RankingItem`, `KeyLinks`.
  - Enforce enum constraints (`UniversityType`, `DegreeLevel`, `ApplicationStatus`).

### Task B2: Async NotebookLM Source Ingestion Engine (`ingest.py`)
- Implement `ingest_university_sources()` using Python SDK `NotebookLMClient`:
  - Bounded concurrent URL uploads (`add_url`).
  - Tier mapping (`url -> source_id -> tier`).
  - Batch readiness waiting (`client.sources.wait_for_sources`).

### Task B3: Schema Extraction & Robustness Engine (`extract_data.py`)
- Implement 5-Query Suite (`main_info+contact`, `programs.undergraduate`, `programs.graduate`, `programs.postgraduate_and_phd`, `faculties`).
- Implement `repair_and_validate_json()`:
  - Strip Markdown code fences (` ```json `).
  - Strip inline citation markers (`\[\d+\]`).
  - Extract balanced bracket JSON spans.
  - Validate against Pydantic models.
- Implement domain-constrained Exa API fallback strictly for missing application portal URLs (`include_domains=[uni_domain]`).
- Atomic notebook deletion (extract $\rightarrow$ parse $\rightarrow$ validate $\rightarrow$ delete).

### Task B4: Resumable State Database (`state.py`)
- Implement SQLite manifest state machine (`state.sqlite`):
  - Table `pipeline_state` (`university_slug`, `status`, `notebook_id`, `sources_ingested`, `queries_executed`, `error_log`, `updated_at`).
  - Atomic status transitions (`pending` $\rightarrow$ `crawled` $\rightarrow$ `ingested` $\rightarrow$ `extracted` $\rightarrow$ `completed`).

### Task B5: Production-Level Pytest Suite Authoring (`tests/test_pipeline.py`)
- Author rigorous, non-dummy production tests:
  - `test_tokenized_exclusion()`: Assert BBA/MBA (`admin`) and portal links (`portal.uni.edu.pk`) are NOT excluded, while `wp-admin` and `news` ARE excluded.
  - `test_url_sanitization()`: Assert zero-width spaces, double slashes, and `&amp;` are cleaned.
  - `test_year_decay()`: Assert 2026/2027 links are boosted and 2023 links are decayed.
  - `test_discipline_token_dedup()`: Assert Electrical Engineering and Electronic Engineering remain separate, while multi-subdomain duplicates merge.
  - `test_json_repair()`: Assert raw LLM outputs containing ` ```json ` fences and `[1]` citation markers are cleanly repaired and validated against `schema.py`.
  - `test_state_machine()`: Assert SQLite manifest transitions correctly.

---

## 3. Master JSON Data Schema Reference (`schema.py` Contract)

```python
from typing import List, Optional
from enum import Enum
from pydantic import BaseModel, Field

class UniversityType(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    OTHER = "other"

class DegreeLevel(str, Enum):
    UNDERGRADUATE = "undergraduate"
    GRADUATE = "graduate"
    POSTGRADUATE_PHD = "postgraduate_phd"

class ApplicationStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ROLLING = "rolling"
    UPCOMING = "upcoming"

class RankingItem(BaseModel):
    source: str
    scope: str
    subject: Optional[str] = None
    year: int
    rank: int
    source_url: Optional[str] = None

class KeyLinks(BaseModel):
    academics_url: Optional[str] = None
    admissions_url: Optional[str] = None
    application_portal_url: Optional[str] = Field(None, description="PRIMARY FOCUS")

class MainInfo(BaseModel):
    name: str
    abbreviation: Optional[str] = None
    country: str = "Pakistan"
    city: Optional[str] = None
    website: str
    type: UniversityType = UniversityType.PUBLIC
    description: str
    domain_verified: bool = False
    verification_note: Optional[str] = None
    key_links: KeyLinks
    rankings: List[RankingItem] = []
    exa_enriched: bool = False

class EligibilityRequirements(BaseModel):
    minimum_marks_percentage: Optional[str] = None
    entry_tests_accepted: List[str] = []
    aggregate_formula: Optional[str] = None

class ProgramItem(BaseModel):
    name: str
    program_info_link: Optional[str] = None
    department: Optional[str] = None
    degree_level: DegreeLevel
    duration: Optional[str] = None
    tuition_fee: Optional[str] = None
    currency: str = "PKR"
    scholarships_info: Optional[str] = None
    courses_taught: List[str] = []
    summary_3_lines: str
    eligibility_requirements: EligibilityRequirements
    application_status: ApplicationStatus = ApplicationStatus.ROLLING
    application_deadline: Optional[str] = None

class ProgramCategoryBlock(BaseModel):
    undergraduate: List[ProgramItem] = []
    graduate: List[ProgramItem] = []
    postgraduate_and_phd: List[ProgramItem] = []

class FacultyItem(BaseModel):
    faculty_name: str
    description: Optional[str] = None
    departments: List[str] = []
    faculty_website: Optional[str] = None

class SubCampusContact(BaseModel):
    campus_name: str
    city: Optional[str] = None
    contact_details: Optional[str] = None

class ContactInfo(BaseModel):
    official_email: Optional[str] = None
    phone_numbers: List[str] = []
    physical_address: Optional[str] = None
    admissions_office_location: Optional[str] = None
    sub_campuses_contact: List[SubCampusContact] = []

class UniversityPayload(BaseModel):
    main_info: MainInfo
    programs: ProgramCategoryBlock
    faculties: List[FacultyItem] = []
    contact: ContactInfo
    programs_possibly_truncated: bool = False
```

---

## 4. Phase 4 Planning: Inspect CLI Utility (`inspect_cli.py`)

*Implemented at Phase 4 (post-extraction audit phase).*

```bash
# 1. Inspect Active NotebookLM Notebooks & Sources
python3 inspect_cli.py notebooks

# 2. Inspect Executable Data Schema
python3 inspect_cli.py schema

# 3. Run Data Quality & Analytics Audit on Output JSONL
python3 inspect_cli.py analytics --file data/outputs/university_counseling_data.jsonl
```
