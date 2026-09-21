#!/usr/bin/env python3
"""
The pipeline's single entry point: sequence the four phases, own nothing else.

    Phase 1  link harvesting and deduplication        extractor.linkers
    Phase 2  NotebookLM ingestion and readiness wait   ingestor
    Phase 3  the query suite and Exa fallback          extractor.crawlers
    Phase 4  aggregation and the data-quality audit    inspector

Replaces pipeline.py (C22). Thin by rule, not by accident -- plan section 1
note 4: if this file starts accumulating logic, that logic belongs in a module.
What moved out of the old pipeline.py on the way here:

    derive_uni_info          -> utilities/naming.py
    backup_existing_outputs  -> utilities/workspace.py
    setup_clean_logging      -> logger/setup.py
    generate_result_analytics-> inspector/analytics.py
    _source_ids_by_tier      -> StateManager.source_ids_by_tier()

**Direction of dependency (Finding 6).** This module imports `inspector` freely.
Nothing under `inspector/` imports this one at module scope; `inspector/cli.py`
reaches it through function-local imports and nothing else does at all.

**Run logs and resume (decision D3, option A).** Every run writes a manifest to
loggings/{single,complete}_logs/. The manifest records what a run targeted and
what happened to each university; `state.sqlite` remains the sole authority on
whether a university is complete. `--resume c_7` therefore replays the
manifest's university list and settings, and lets StateManager decide what to
skip -- so a run killed between the sqlite commit and the log write resumes
correctly rather than redoing finished work.
"""
import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import httpx

# Kept ahead of the src imports: `python src/orchestrator.py` still has to work,
# and run that way sys.path[0] is src/, so the `src` package is not importable yet.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notebooklm import NotebookLMClient  # noqa: E402
from rich.panel import Panel  # noqa: E402
from tqdm import tqdm  # noqa: E402

from src.config import config  # noqa: E402
from src.extractor.crawlers.notebook_querying import ExtractionReport  # noqa: E402
from src.extractor.crawlers.runner import (  # noqa: E402
    delete_notebook_after_success,
    extract_university_payload,
)
from src.extractor.linkers.crawling import close_shared_crawler  # noqa: E402
from src.extractor.linkers.runner import run_pipeline as run_link_extractor  # noqa: E402
from src.extractor.normalizers.runner import load_global_registry  # noqa: E402
from src.ingestor.http_client import close_http_client  # noqa: E402
from src.ingestor.source_management import ingest_university_sources  # noqa: E402
from src.inspector.analytics import audit_analytics, generate_result_analytics  # noqa: E402
from src.inspector.formatting import console  # noqa: E402
from src.inspector.sync import export_dataset  # noqa: E402
from src.logger.pipeline_logger import PipelineLogger, RunKind, load_run  # noqa: E402
from src.logger.setup import setup_clean_logging  # noqa: E402
from src.utilities.json_io import append_jsonl, atomic_write_json, stream_compile_master_json  # noqa: E402
from src.utilities.naming import derive_uni_info  # noqa: E402
from src.utilities.state_management import (  # noqa: E402
    PARTIAL_EXTRACTION,
    QuotaExceededError,
    StateManager,
)
from src.utilities.workspace import backup_existing_outputs  # noqa: E402

# Defined on Config (C23) so the inspector's `batch` subcommand can share the
# default without importing this module at module scope -- Finding 6.
DEFAULT_RUN_SETTINGS_PATH = config.run_settings_path


class PipelineOutcome(NamedTuple):
    """
    What actually happened to one university, for the run manifest.

    Phases 1 and 2 report failure by returning early rather than raising -- a
    dead site must not abort an 83-university batch. But `_drain_queue` treated
    "did not raise" as success and wrote "processed" into the manifest for every
    one of them. LUMS is recorded as processed in run c_1 on 2026-09-05 having
    produced no notebook, no payload and no error; the manifest said the run
    went fine and only sqlite disagreed.
    """
    status: str          # processed | failed | skipped
    detail: str = ""


def compile_master_json() -> Path:
    """
    Rebuild the master JSON array from the JSONL ledger.

    Streamed and atomic, and called once per run rather than once per university:
    re-dumping every historical payload after each university made the batch
    O(N^2) in disk I/O.
    """
    master_path = config.output_master_json_path
    count = stream_compile_master_json(config.output_jsonl_path, master_path)
    print(f"  └─ Master : {master_path} ({count} records)")
    return master_path


@asynccontextmanager
async def _resilient_notebooklm_client(max_retries: int = 3, initial_delay: float = 2.0):
    """
    Acquire NotebookLMClient with retry against transient DNS and network connection drops.
    """
    client = None
    delay = initial_delay
    for attempt in range(1, max_retries + 1):
        try:
            client = await NotebookLMClient.from_storage().__aenter__()
            break
        except (httpx.ConnectError, httpx.NetworkError, TimeoutError, OSError) as e:
            if attempt == max_retries:
                raise
            print(f"⚠️  [NOTEBOOKLM] Client connection attempt {attempt}/{max_retries} encountered transient error ({e}). Retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)
            delay *= 2.0

    try:
        yield client
    finally:
        if client is not None:
            await client.__aexit__(None, None, None)


async def _release_notebook(client, state_mgr, uni_slug: str, notebook_id: str) -> None:
    """
    Give a notebook back after a failed or abandoned extraction.

    Shielded, because the most common caller is a task that is itself being
    cancelled -- a watchdog timeout or a Ctrl-C. A plain `await` in that state
    raises CancelledError before the request is ever sent; the shield lets the
    delete run to completion on the loop while this coroutine unwinds.

    Best-effort by design: whatever happens here, the exception that brought us
    in is the one the caller re-raises. A notebook that survives anyway is caught
    on the next run by reap_orphaned_notebooks(), which reads from sqlite and so
    also covers the case no handler can -- a process killed outright.
    """
    try:
        await asyncio.shield(
            delete_notebook_after_success(client, notebook_id, uni_slug=uni_slug)
        )
        state_mgr.clear_notebook(uni_slug)
        print(f"  └─ Released notebook {notebook_id} after a failed extraction.")
    except BaseException as e:  # noqa: BLE001 -- must never mask the real failure
        print(f"⚠️  Could not release notebook {notebook_id}: {type(e).__name__}: {e}")


# ------------------------------------------------------------ one university --

async def run_master_pipeline(
    url: str,
    uni_name_override: Optional[str] = None,
    max_links: int = 60,
    exclude_keywords: str = "news|events",
    uptodate: bool = True,
    compile_master: bool = True,
):
    """
    Run all four phases for one university.

    Owns the StateManager lifecycle so an 83-university batch does not accumulate
    one open SQLite handle per university. The pooled HTTP client and the shared
    crawler deliberately outlive this call and are closed once by the caller.
    """
    state_mgr = StateManager()
    try:
        return await _run_master_pipeline(
            state_mgr, url, uni_name_override, max_links,
            exclude_keywords, uptodate, compile_master,
        )
    finally:
        state_mgr.close()


async def _run_master_pipeline(
    state_mgr: StateManager,
    url: str,
    uni_name_override: Optional[str] = None,
    max_links: int = 60,
    exclude_keywords: str = "news|events",
    uptodate: bool = True,
    compile_master: bool = True,
):
    """Phase 1-4 body. See run_master_pipeline for the public entry point."""
    uni_name, uni_slug, uni_domain = derive_uni_info(url, uni_name_override)

    print(f"\n================================================================================")
    print(f"🚀 STARTING MASTER RAG PIPELINE FOR: {uni_name} ({url})")
    print(f"================================================================================\n")

    state_mgr.set_status(uni_slug, "pending")

    # --------------------------------------------------------------------------
    # PHASE 1: LINK HARVESTING & DEDUPLICATION
    # --------------------------------------------------------------------------
    print(f"📌 [PHASE 1] Harvesting & Sanitizing Links for {uni_name}...")

    # Phase 1 raises CrawlFailure when a site yields nothing. Caught here so the
    # state row records the failure: uncaught, it left the row on whatever the
    # previous phase wrote and the university looked merely unstarted.
    try:
        await run_link_extractor(
            url=url,
            max_links=max_links,
            exclude_keywords=exclude_keywords,
            uptodate=uptodate,
            # Passed explicitly. Omitting them took the linker CLI's argparse
            # defaults instead of the calibrated Config fields, so every batch
            # ran at threshold 0.45 and 15 pages no matter what config said.
            threshold=config.semantic_threshold,
            max_pages=config.max_crawl_pages,
            output_links=str(config.base_dir / "extracted_links.txt"),
            output_detailed=str(config.base_dir / "extracted_links_detailed.txt"),
        )
    except Exception as e:
        error_msg = f"Phase 1 failed: {type(e).__name__}: {e}"
        state_mgr.set_status(uni_slug, "failed", error_log=error_msg)
        print(f"❌ [PHASE 1 FAILED] {error_msg}")
        return PipelineOutcome("failed", error_msg)

    links_list = _read_harvested_links(uni_slug)
    selected_count = sum(1 for l in links_list if l.get("selected", True))

    print(
        f"✓ [PHASE 1 COMPLETE] Retained {selected_count} clean canonical links"
        + (f" (+{len(links_list) - selected_count} reserve)."
           if len(links_list) > selected_count else ".")
    )
    state_mgr.set_status(uni_slug, "crawled", sources_ingested=0)

    if not links_list:
        error_msg = "Phase 1 produced zero links."
        state_mgr.set_status(uni_slug, "failed", error_log=error_msg)
        print(f"❌ [PHASE 1 FAILED] {error_msg}")
        return PipelineOutcome("failed", error_msg)

    # --------------------------------------------------------------------------
    # PHASE 2: NOTEBOOKLM INGESTION
    # --------------------------------------------------------------------------
    print(f"\n📌 [PHASE 2] Ingesting Sources into NotebookLM...")

    try:
        async with _resilient_notebooklm_client() as client:
            ingest_res = await ingest_university_sources(
                uni_slug=uni_slug,
                uni_name=uni_name,
                links=links_list,
                client=client,
            )
            # The pre-flight health check can refuse the batch before any
            # notebook or query budget is spent. That is a skip, not a crash:
            # record it and move to the next university.
            if ingest_res.skipped:
                error_msg = f"Phase 2 skipped: {ingest_res.skip_reason}."
                state_mgr.set_status(uni_slug, "failed", error_log=error_msg)
                print(f"⏭️  [PHASE 2 SKIPPED] {error_msg}")
                return PipelineOutcome("skipped", error_msg)

            notebook_id = ingest_res.notebook_id
            ingested_count = ingest_res.ingested_count
            print(
                f"✓ [PHASE 2 COMPLETE] Provisioned Notebook ID: {notebook_id} "
                f"({ingested_count} uploaded, {ingest_res.ready_count} ready)."
            )
            state_mgr.set_status(
                uni_slug,
                "ingested",
                notebook_id=notebook_id,
                sources_ingested=ingested_count,
            )

            # Persist url -> source_id -> tier so Phase 3 can scope its queries.
            # Cleared first: a previous run's notebook is deleted on success, so
            # its source_ids are dead and must not survive into this run's scope.
            if ingest_res.sources:
                state_mgr.clear_sources(uni_slug)
                state_mgr.record_sources(
                    uni_slug,
                    ((s.source_id, s.url, s.tier) for s in ingest_res.sources),
                )

            # ------------------------------------------------------------------
            # PHASE 3: SCHEMA EXTRACTION & EXA FALLBACK
            # ------------------------------------------------------------------
            print(
                f"\n📌 [PHASE 3] Executing {config.queries_per_university}-Query "
                f"Schema Extraction & Exa Fallback..."
            )

            # Reserve the whole suite against today's budget BEFORE issuing any
            # query. The suite itself is serial (see config.query_concurrency --
            # concurrent unkeyed asks share a conversation and return each
            # other's answers), but universities are not: reserving up front is
            # what stops a run walking into the 500/day ceiling mid-suite and
            # leaving a notebook ingested but never extracted.
            state_mgr.reserve_queries(uni_slug, config.queries_per_university)
            reserved = config.queries_per_university

            # Owned here, not inside the extractor, so that a failed extraction
            # still reports what it spent. A report the extractor keeps privately
            # dies with the call, and the refund below would then hand back the
            # whole suite including the queries that really were issued.
            report = ExtractionReport()
            try:
                payload, report = await extract_university_payload(
                    client=client,
                    notebook_id=notebook_id,
                    uni_name=uni_name,
                    uni_domain=uni_domain,
                    source_ids_by_tier=state_mgr.source_ids_by_tier(uni_slug),
                    tier1_source_count=ingest_res.tier_histogram().get(1, 0),
                    report=report,
                )
            except BaseException:
                # Includes CancelledError and the university watchdog's timeout.
                # Two things are owed back here and neither was ever returned:
                # the unspent part of the reservation, and the notebook itself.
                # COMSATS was billed all 6 queries for the 2 it issued, and its
                # notebook 714feb18 is still holding a workspace slot.
                state_mgr.release_queries(uni_slug, max(0, reserved - report.queries_used))
                await _release_notebook(client, state_mgr, uni_slug, notebook_id)
                raise

            # Reconcile the reservation against what the suite actually spent.
            # Repair retries and narrowed re-asks cost real queries beyond the
            # suite; a run that ended early spent fewer. Only the overage was
            # ever settled, so the ledger drifted upward and the daily budget
            # ran out earlier than the real quota did.
            delta = report.queries_used - reserved
            if delta > 0:
                try:
                    state_mgr.reserve_queries(uni_slug, delta)
                except QuotaExceededError as qe:
                    print(f"⚠️  [QUOTA] Retry overage exceeded the daily budget: {qe}")
            elif delta < 0:
                state_mgr.release_queries(uni_slug, -delta)

            output_file = config.output_jsonl_path
            append_jsonl(output_file, payload.model_dump_json())

            config.outputs_uni_outputs_dir.mkdir(parents=True, exist_ok=True)
            uni_json_path = config.outputs_uni_outputs_dir / f"{uni_slug}.json"
            atomic_write_json(uni_json_path, payload.model_dump())

            print(f"✓ [PHASE 3 COMPLETE] Saved payload for {uni_name}:")
            print(f"  └─ JSONL  : {output_file}")
            print(f"  └─ Slug   : {uni_json_path}")

            # A query block that failed every retry does not fail the pipeline --
            # the other five blocks are still real data worth keeping -- but it
            # must not vanish silently either. Before this, `report.failed` was
            # only ever printed to stdout: nothing captured it, `set_status`
            # always wrote a bare "completed", and the run log recorded no
            # error. A university could ship with an entire degree-level bucket
            # empty from a transient API failure and nothing downstream -- not
            # `cli.py state`, not the run manifest, not the payload itself --
            # could distinguish that from "this university genuinely offers
            # none". Persisting it here is what let ITU's missing bachelors
            # bucket (query failed silently, 2026-09-03) go unnoticed.
            partial_note = (
                f"Partial extraction: {len(report.failed)} query block(s) failed "
                f"after retries: {sorted(report.failed)}"
                if not report.ok else None
            )
            # 'partial', not 'completed'. get_completed_slugs() drives what a
            # resumed batch skips, so writing "completed" here meant one
            # transient API failure cost a degree level permanently: the next
            # run saw a completed university and never asked again.
            state_mgr.set_status(
                uni_slug,
                PARTIAL_EXTRACTION if partial_note else "completed",
                notebook_id=notebook_id,
                queries_executed=report.queries_used,
                error_log=partial_note,
            )
            if partial_note:
                print(f"⚠️  [PHASE 3] {partial_note}")

            # Free the notebook workspace slot now that the payload is durable
            # and the terminal status is written. The id stays on the row on
            # purpose -- it is the only thing tying this university to its
            # loggings/notebook_logs/<id>.json audit document, and the reaper
            # never looks at rows in a terminal status.
            await delete_notebook_after_success(client, notebook_id, uni_slug=uni_slug)

    except Exception as e:
        error_msg = f"Pipeline execution error: {type(e).__name__}: {e}"
        print(f"❌ [PIPELINE ERROR] {error_msg}")
        state_mgr.set_status(uni_slug, "failed", error_log=error_msg)
        raise e

    # --------------------------------------------------------------------------
    # PHASE 4: INSPECTION & DATA HEALTH AUDIT
    # --------------------------------------------------------------------------
    print(f"\n📌 [PHASE 4] Data Quality & Pipeline Audit:")
    # Batch runs defer this to a single sweep after the whole queue drains; a
    # standalone --url run has no later sweep, so it aggregates here.
    if compile_master:
        compile_master_json()
    audit_analytics(config.output_jsonl_path)
    print(f"\n================================================================================")
    print(f"🎉 MASTER RAG PIPELINE COMPLETED SUCCESSFULLY FOR {uni_name}!")
    print(f"================================================================================\n")
    return PipelineOutcome("processed")


def _read_harvested_links(uni_slug: str) -> List[Dict[str, Any]]:
    """
    Phase 1's output for one university, in the shape Phase 2 documents:
    records of at least {"url": str, "tier": int}, in Phase 1's rank order.

    `selected` is carried through where the partition records it. A false value
    marks a reserve link -- ranked and tiered like any other, held back from the
    notebook unless a selected link fails Phase 2's pre-flight. Older partitions
    and the flat-file fallback record no flag at all, and default to selected,
    which is exactly their pre-reserve behaviour.

    Reads data/links/<slug>.jsonl -- the per-university partition, written
    atomically with a tier per link. Falls back to the shared extracted_links.txt
    only when no partition exists; that file is non-atomic and, in an --hec batch,
    undifferentiated across universities, which is the exact problem the partition
    was introduced to solve.

    Two bugs lived here from the original release until C22's follow-up, both
    from never reading the partition:

      1. the loop tested `item.get("href")`, and the partition writes "url" --
         so the per-slug branch appended nothing, ever, and every run silently
         used the flat file;
      2. the flat file yields bare URL strings, which ingest_university_sources
         normalises to **tier 1**. Every source in every notebook was therefore
         tier 1, and Phase 3's per-query source scoping -- the whole reason the
         tier is recorded -- was a no-op that scoped every query to everything.
    """
    records: List[Dict[str, Any]] = []
    links_file = config.data_links_dir / f"{uni_slug}.jsonl"
    if links_file.exists():
        with open(links_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line must not cost the university every link
                    # that was written before it.
                    continue
                # "href" is accepted alongside "url" only so a partition written
                # by an older build still loads; nothing writes it today.
                url = item.get("url") or item.get("href")
                if url:
                    records.append({
                        "url": url,
                        "tier": int(item.get("tier", 1) or 1),
                        "selected": bool(item.get("selected", True)),
                    })

    if not records:
        txt_path = config.base_dir / "extracted_links.txt"
        if txt_path.exists():
            with open(txt_path, "r", encoding="utf-8") as f:
                # No tier was ever recorded for these; 1 is what Phase 2 assumes.
                records = [
                    {"url": line.strip(), "tier": 1, "selected": True}
                    for line in f if line.strip()
                ]
    return records


# ------------------------------------------------------------ orphan reaper --

async def reap_orphaned_notebooks() -> int:
    """
    Delete notebooks left behind by runs that never reached a terminal state.

    A notebook is created in Phase 2 and deleted at the end of Phase 3, so a
    university sitting at 'crawled' or 'ingested' while still holding a
    notebook_id is holding a workspace slot nothing will ever free. Run c_1 was
    killed mid-extraction on 2026-09-05 and leaked COMSATS notebook
    714feb18-841a-4f22-888e-91b69e5b075e; no in-process cleanup handler can cover
    that case, because the process does not survive to run one.

    Runs before the queue for exactly that reason -- the slots have to be free
    before this batch starts asking for more. Returns the number reaped, and
    never raises: a workspace it cannot reach is not a reason to refuse the run.
    """
    state_mgr = StateManager()
    try:
        orphans = state_mgr.get_orphaned_notebooks()
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]Could not read orphaned notebooks: {e}[/yellow]")
        state_mgr.close()
        return 0

    if not orphans:
        state_mgr.close()
        return 0

    console.print(
        f"\n[bold cyan]🧹 Reaping {len(orphans)} notebook(s) left behind by an "
        f"earlier run...[/bold cyan]"
    )
    reaped = 0
    try:
        async with _resilient_notebooklm_client() as client:
            for row in orphans:
                slug, notebook_id = row["university_slug"], row["notebook_id"]
                if await delete_notebook_after_success(client, notebook_id, uni_slug=slug):
                    state_mgr.clear_notebook(slug)
                    reaped += 1
                    console.print(f"  └─ freed {notebook_id} ({slug}, was '{row['status']}')")
                else:
                    # Already gone is the common case, and indistinguishable from
                    # a transient API error at this layer. Dropping the id either
                    # way stops the reaper retrying a ghost on every future run.
                    state_mgr.clear_notebook(slug)
                    console.print(
                        f"  └─ [yellow]{notebook_id} ({slug}) could not be deleted; "
                        f"clearing the stale reference[/yellow]"
                    )
    except Exception as e:  # noqa: BLE001 -- never block a batch on cleanup
        console.print(f"[yellow]Notebook reaping stopped early: {e}[/yellow]")
    finally:
        state_mgr.close()
    return reaped


# ------------------------------------------------------------------- batch --

def load_run_settings(config_file_path: Path) -> Optional[Dict[str, Any]]:
    """Read a run-settings file. Returns None (with a message) when absent."""
    if not config_file_path.exists():
        console.print(
            f"[bold red]Error:[/bold red] Configuration file "
            f"[yellow]{config_file_path}[/yellow] not found!"
        )
        return None
    with open(config_file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_queue(
    uni_dict: Dict[str, List[Any]],
    processed_slugs: set,
    rerun: bool,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """
    Expand the configured universities into (queue, skipped).

    Skipping is driven by `processed_slugs`, which the caller reads from
    StateManager -- the single authority on completion (D3). Nothing here
    consults a run log.
    """
    queue: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []

    for country, item_list in uni_dict.items():
        for item in item_list or []:
            if isinstance(item, dict):
                url = item.get("url", "")
                custom_name = item.get("name")
            else:
                url = str(item)
                custom_name = None

            name, slug, domain = derive_uni_info(url, name_override=custom_name)

            # Prefer the registry's official name over a slug shouted in caps.
            if not custom_name:
                reg_fact = load_global_registry().get(domain, {})
                if reg_fact.get("name"):
                    name = reg_fact["name"]

            entry = {"country": country, "url": url, "name": name, "slug": slug}
            (skipped if (not rerun and slug in processed_slugs) else queue).append(entry)

    return queue, skipped


async def run_batch_pipeline(
    config_file_path: Path = DEFAULT_RUN_SETTINGS_PATH,
    force_rerun_all: bool = False,
    dry_run: bool = False,
    resume: Optional[str] = None,
):
    """
    Run every configured university, skipping those already complete.

    `resume` names a prior run token (`c_7`, `s_42`). Its manifest supplies the
    university list and the settings the original run used, so a resumed run
    reproduces the original rather than silently picking up whatever the
    settings file says today. What to skip still comes from StateManager.

    `dry_run` walks the whole queue, writes a real run log, and executes no
    phase. It spends no NotebookLM quota, so it is the cheap way to check that a
    settings file expands to the universities you meant.
    """
    resumed_from = None
    if resume:
        manifest = load_run(resume)
        resumed_from = resume
        settings = dict(manifest.get("settings") or {})
        uni_dict = _universities_to_dict(manifest.get("universities") or [])
        console.print(
            f"[bold cyan]↻ Resuming from run [yellow]{resume}[/yellow][/bold cyan] "
            f"({len(manifest.get('universities') or [])} universities in the original manifest)"
        )
    else:
        cfg = load_run_settings(config_file_path)
        if cfg is None:
            return
        uni_dict = cfg.get("universities", {})
        settings = cfg.get("pipeline_settings", {})

    if settings.get("clean_logging", True):
        setup_clean_logging()

    rerun = force_rerun_all or settings.get("force_rerun_all", False)
    if rerun and not dry_run:
        archived = backup_existing_outputs()
        if archived:
            console.print(f"[bold yellow]📦 Backed up existing outputs to:[/bold yellow] [cyan]{archived}[/cyan]")

    state_mgr = StateManager()
    try:
        processed_slugs = set(state_mgr.get_completed_slugs())
    finally:
        state_mgr.close()

    queue, skipped = build_queue(uni_dict, processed_slugs, rerun)

    console.print(
        Panel(
            f"[bold cyan]🚀 BATCH PIPELINE ORCHESTRATOR[/bold cyan]\n\n"
            f"Source: [yellow]{resumed_from or config_file_path}[/yellow]\n"
            f"Force Rerun All: [bold {'yellow' if rerun else 'green'}]{rerun}[/bold {'yellow' if rerun else 'green'}]\n"
            f"Dry Run: [bold {'yellow' if dry_run else 'green'}]{dry_run}[/bold {'yellow' if dry_run else 'green'}]\n"
            f"Total Configured Links: [bold yellow]{len(queue) + len(skipped)}[/bold yellow]\n"
            f"Already Processed (Skipping): [bold green]{len(skipped)}[/bold green]\n"
            f"Remaining to Process: [bold magenta]{len(queue)}[/bold magenta]",
            title="⚡ Master RAG Pipeline Batch Execution",
        )
    )

    # The manifest carries the FULL configured list, not just the queue: a
    # resume of this run must see the universities this run skipped, so that a
    # later state reset re-runs them rather than losing them.
    with PipelineLogger(
        kind=RunKind.COMPLETE,
        settings={**settings, "dry_run": dry_run, "resumed_from": resumed_from},
        universities=queue + skipped,
    ) as run_log:
        for entry in skipped:
            run_log.record(entry["slug"], "skipped", reason="already completed")

        if not queue:
            console.print(
                "[bold green]✓ All configured universities are already processed! "
                "Run with --rerun-all to re-extract.[/bold green]"
            )
        else:
            if not dry_run:
                await reap_orphaned_notebooks()
            await _drain_queue(queue, settings, dry_run, run_log)

        if dry_run:
            console.print(
                Panel(
                    f"[bold yellow]DRY RUN — no phase was executed and no quota was spent.[/bold yellow]\n"
                    f"Run log: [cyan]{run_log.path}[/cyan]",
                    title="🧪 Dry Run Complete",
                )
            )
            return run_log.path

        # Both deliberately outlive individual universities so the batch reuses
        # one Chromium process and one connection pool throughout.
        await close_shared_crawler()
        await close_http_client()

        console.print("\n[bold cyan]🌐 Aggregating master JSON array (single streamed pass)...[/bold cyan]")
        compile_master_json()

        console.print("\n[bold cyan]🌐 Performing Master Universal Export & Directory Structuring...[/bold cyan]")
        export_dataset(format_type="json")

        # Record where this run's universities actually came from, which for a
        # resume is the token rather than whatever run_settings.json says today.
        result_analytics_file = generate_result_analytics(resumed_from or config_file_path)
        console.print(
            Panel(
                f"[bold green]🎉 BATCH PIPELINE COMPLETED SUCCESSFULLY![/bold green]\n"
                f"Master analytics saved to:\n[cyan]{result_analytics_file}[/cyan]\n"
                f"Run log:\n[cyan]{run_log.path}[/cyan]",
                title="📊 Final Analytics Summary",
            )
        )
        return run_log.path


async def _drain_queue(queue, settings, dry_run, run_log) -> None:
    """Process the queue one university at a time, recording each outcome."""
    with tqdm(total=len(queue), desc="🌐 Processing Universities", unit="uni") as pbar:
        for entry in queue:
            pbar.set_postfix({"uni": entry["slug"], "country": entry["country"]})
            console.print(
                f"\n[bold magenta]🚀 Processing ({entry['country']}): "
                f"{entry['name']} ({entry['url']})[/bold magenta]"
            )
            if dry_run:
                run_log.record(entry["slug"], "dry-run", url=entry["url"])
                pbar.update(1)
                continue
            try:
                # The watchdog. Every individual await inside has its own bound
                # now, but "every part is bounded" is not the same claim as "the
                # whole is bounded", and the whole is what a batch window is made
                # of. Run c_1 spent 7h11m of an 11-hour window inside one
                # university and never reached the twelve queued behind it.
                outcome = await asyncio.wait_for(
                    run_master_pipeline(
                        url=entry["url"],
                        uni_name_override=entry["name"],
                        max_links=settings.get("max_links", 60),
                        exclude_keywords=settings.get("exclude_keywords", "news|events"),
                        uptodate=settings.get("uptodate", True),
                        # Aggregated once after the queue drains, not per university.
                        compile_master=False,
                    ),
                    timeout=config.university_timeout_sec,
                )
                # What actually happened, not merely "nothing was raised".
                # Phases 1 and 2 report failure by returning, so recording
                # "processed" for every non-raising call put LUMS in run c_1's
                # manifest as a success with no notebook and no payload.
                outcome = outcome or PipelineOutcome("processed")
                run_log.record(
                    entry["slug"], outcome.status, url=entry["url"],
                    **({"error": outcome.detail} if outcome.detail else {}),
                )
            except asyncio.TimeoutError:
                msg = (
                    f"exceeded the per-university ceiling of "
                    f"{config.university_timeout_sec}s and was abandoned"
                )
                console.print(f"[bold red]⏱️  {entry['name']} {msg}.[/bold red]")
                run_log.record(entry["slug"], "failed", error=f"TimeoutError: {msg}")
                _record_timeout(entry["slug"], msg)
            except Exception as e:
                # One university's failure must not end the batch. The state row
                # records the failure; this only keeps the loop alive.
                console.print(f"[bold red]❌ Failed to process {entry['name']}: {e}[/bold red]")
                run_log.record(entry["slug"], "failed", error=f"{type(e).__name__}: {e}")
            pbar.update(1)


def _record_timeout(uni_slug: str, msg: str) -> None:
    """
    Mark a timed-out university failed in its own StateManager.

    Its own, because the cancelled pipeline closed the one it owned on the way
    out of `run_master_pipeline`'s finally block, and writing through a closed
    handle is how a timeout would turn into a second, more confusing error.
    """
    state_mgr = StateManager()
    try:
        state_mgr.set_status(uni_slug, "failed", error_log=f"Watchdog: {msg}.")
    except Exception as e:  # noqa: BLE001 -- the timeout is the news, not this
        console.print(f"[yellow]Could not record timeout for {uni_slug}: {e}[/yellow]")
    finally:
        state_mgr.close()


def _universities_to_dict(universities: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """Fold a manifest's flat university list back into {country: [entry, ...]}."""
    grouped: Dict[str, List[Any]] = {}
    for entry in universities:
        country = entry.get("country") or "Unknown"
        grouped.setdefault(country, []).append(
            {"url": entry.get("url", ""), "name": entry.get("name")}
        )
    return grouped


# ------------------------------------------------------------- entry point --

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Master 4-Phase Education Counselor RAG Pipeline"
    )
    parser.add_argument("--url", type=str, default=None, help="Target university URL (single-university run)")
    parser.add_argument("--name", type=str, default=None, help="Full university name")
    parser.add_argument("--config", type=Path, default=DEFAULT_RUN_SETTINGS_PATH, help="Batch run-settings JSON file")
    parser.add_argument("--rerun-all", action="store_true", help="Back up existing outputs and rerun every configured university")
    parser.add_argument("--max-links", type=int, default=60, help="Maximum links to retain")
    parser.add_argument("--exclude-keywords", type=str, default="news|events", help="Exclude keyword patterns")
    parser.add_argument("--uptodate", action="store_true", default=True, help="Enable recency filter")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Expand the queue and write a run log without executing any phase or spending quota",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        metavar="TOKEN",
        help=(
            "Resume a prior run by token (e.g. c_7, s_42). Replays that run's "
            "university list and settings; StateManager decides what to skip."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.url and args.resume:
        console.print("[bold red]Error:[/bold red] --url and --resume are mutually exclusive.")
        return 2

    if args.url:
        asyncio.run(_run_single(args))
    else:
        asyncio.run(
            run_batch_pipeline(
                config_file_path=args.config,
                force_rerun_all=args.rerun_all,
                dry_run=args.dry_run,
                resume=args.resume,
            )
        )
    return 0


async def _run_single(args) -> None:
    """One university, with its own run log and guaranteed resource teardown."""
    _, slug, _ = derive_uni_info(args.url, args.name)
    settings = {
        "max_links": args.max_links,
        "exclude_keywords": args.exclude_keywords,
        "uptodate": args.uptodate,
    }
    with PipelineLogger(
        kind=RunKind.SINGLE,
        settings=settings,
        universities=[{"url": args.url, "name": args.name, "slug": slug}],
    ) as run_log:
        try:
            await run_master_pipeline(
                url=args.url,
                uni_name_override=args.name,
                max_links=args.max_links,
                exclude_keywords=args.exclude_keywords,
                uptodate=args.uptodate,
            )
            run_log.record(slug, "processed", url=args.url)
        finally:
            # Closed here rather than in run_master_pipeline: both resources are
            # shared across a batch and must survive one university's failure.
            await close_shared_crawler()
            await close_http_client()


if __name__ == "__main__":
    sys.exit(main())
