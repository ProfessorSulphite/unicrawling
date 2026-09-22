#!/usr/bin/env python3
"""
The pipeline's single entry point: sequence the four phases, own nothing else.

    Phase 1  link harvesting and deduplication        extractor.linkers
    Phase 2  page-text corpus fetching                 extractor.crawlers
    Phase 3  schema extraction and web enrichment      extractor.crawlers
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
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple


# Kept ahead of the src imports: `python src/orchestrator.py` still has to work,
# and run that way sys.path[0] is src/, so the `src` package is not importable yet.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.panel import Panel  # noqa: E402
from tqdm import tqdm  # noqa: E402

from src.config import config  # noqa: E402
from src.extractor.crawlers.query_schemas import (  # noqa: E402
    ExtractionReport,
    Q1Payload,
)
from src.extractor.linkers.crawling import close_shared_crawler  # noqa: E402
from src.extractor.linkers.runner import run_pipeline as run_link_extractor  # noqa: E402
from src.extractor.normalizers.runner import load_global_registry  # noqa: E402
from src.inspector.analytics import generate_result_analytics  # noqa: E402
from src.inspector.formatting import console  # noqa: E402
from src.inspector.sync import export_dataset  # noqa: E402
from src.logger.pipeline_logger import PipelineLogger, RunKind, load_run  # noqa: E402
from src.logger.setup import setup_clean_logging  # noqa: E402
from src.utilities.json_io import append_jsonl, atomic_write_json, stream_compile_master_json  # noqa: E402
from src.utilities.naming import derive_uni_info  # noqa: E402
from src.utilities.schema import UniversityPayload  # noqa: E402
from src.utilities.state_management import (  # noqa: E402
    PARTIAL_EXTRACTION,
    QuotaExceededError,
    StateManager,
)
from src.utilities.gemini_client import GeminiQuotaError  # noqa: E402
from src.utilities.typesafe_client import close_shared_typesafe_client  # noqa: E402
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
    produced no payload and no error; the manifest said the run
    went fine and only sqlite disagreed.
    """
    status: str          # processed | failed | skipped
    detail: str = ""


def _persist_extraction_outputs(
    payload: UniversityPayload,
    report: ExtractionReport,
    uni_name: str,
    uni_slug: str,
    state_mgr: StateManager,
    compile_master: bool,
) -> "PipelineOutcome":
    """
    Write one university's payload, record its status, and run the audit.

    Shared by both engines. They differ only in which API answered the prompts;
    everything after the payload exists is identical, and duplicating it once per
    engine is how the two drift apart.
    """
    output_file = config.output_jsonl_path
    append_jsonl(output_file, payload.model_dump_json())

    config.outputs_uni_outputs_dir.mkdir(parents=True, exist_ok=True)
    uni_json_path = config.outputs_uni_outputs_dir / f"{uni_slug}.json"
    atomic_write_json(uni_json_path, payload.model_dump())

    print(f"\u2713 [PHASE 3 COMPLETE] Saved payload for {uni_name}:")
    print(f"  \u2514\u2500 JSONL  : {output_file}")
    print(f"  \u2514\u2500 Slug   : {uni_json_path}")

    partial_note = (
        f"Partial extraction: {len(report.failed)} query block(s) failed: {sorted(report.failed)}"
        if not report.ok else None
    )
    state_mgr.set_status(
        uni_slug,
        PARTIAL_EXTRACTION if partial_note else "completed",
        queries_executed=report.queries_used,
        error_log=partial_note,
        intake_year=config.default_intake_year,
        data_version=1,
    )
    if partial_note:
        print(f"\u26a0\ufe0f  [PHASE 3] {partial_note}")

    print(f"\n\U0001f50d [PHASE 4: DATA QUALITY AUDIT] Auditing extracted corpus health...")
    try:
        from src.inspector.auditor import audit_corpus
        verdict = audit_corpus()
        status_str = "READY FOR SUPABASE" if verdict.ready else "GAPS DETECTED (STRICT AUDIT)"
        print(f"\u2713 [PHASE 4 AUDIT COMPLETE] Verdict: {status_str}")
    except Exception as audit_err:  # noqa: BLE001 -- the payload is already durable
        print(f"\u26a0\ufe0f  [PHASE 4 AUDIT] Inspection skipped: {audit_err}")

    if compile_master:
        compile_master_json()
    print(f"\n🎉 MASTER RAG PIPELINE COMPLETED SUCCESSFULLY FOR {uni_name}!")
    return PipelineOutcome("processed")


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


# ------------------------------------------------------------ one university --

async def run_master_pipeline(
    url: str,
    uni_name_override: Optional[str] = None,
    max_links: int = 60,
    exclude_keywords: str = "news|events",
    uptodate: bool = True,
    compile_master: bool = True,
    force_rerun: bool = False,
    engine: Optional[str] = None,
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
            exclude_keywords, uptodate, compile_master, force_rerun,
            engine=engine,
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
    force_rerun: bool = False,
    engine: Optional[str] = None,
):
    """Phase 1-4 body. See run_master_pipeline for the public entry point."""
    uni_name, uni_slug, uni_domain = derive_uni_info(url, uni_name_override)

    # Check if already complete, unexpired, and matching intake cycle
    uni_json_path = config.outputs_uni_outputs_dir / f"{uni_slug}.json"
    if not force_rerun and state_mgr.is_complete(uni_slug, current_intake_year=config.default_intake_year) and uni_json_path.exists():
        print(f"✓ [ALREADY COMPLETED] University '{uni_name}' ({uni_slug}) has a valid unexpired payload. Skipping.")
        return PipelineOutcome("completed")

    print(f"\n================================================================================")
    print(f"🚀 STARTING MASTER RAG PIPELINE FOR: {uni_name} ({url})")
    print(f"================================================================================\n")

    state_mgr.set_status(uni_slug, "pending")

    # --------------------------------------------------------------------------
    # PHASE 1: LINK HARVESTING & DEDUPLICATION
    # --------------------------------------------------------------------------
    cached_links = _read_harvested_links(uni_slug)
    if cached_links and not force_rerun:
        links_list = cached_links
        selected_count = sum(1 for l in links_list if l.get("selected", True))
        print(
            f"✓ [PHASE 1 CACHED] Reusing {selected_count} clean links from "
            f"data/links/{uni_slug}.jsonl (skipping web crawl)."
        )
    else:
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
    # PHASE 2: CORPUS FETCHING & PHASE 3: SCHEMA EXTRACTION
    # --------------------------------------------------------------------------
    # Check for existing partial payload to enable smart resume
    uni_json_path = config.outputs_uni_outputs_dir / f"{uni_slug}.json"
    prior_payload: Optional[UniversityPayload] = None
    failed_blocks_to_query: Optional[List[str]] = None
    accumulated_results: Dict[str, Any] = {}

    if uni_json_path.exists() and not force_rerun:
        try:
            with open(uni_json_path, "r", encoding="utf-8") as f:
                existing_payload_data = json.load(f)
            raw_failed = existing_payload_data.get("failed_query_blocks")
            if raw_failed:
                failed_blocks_to_query = list(raw_failed)
                print(
                    f"📌 [SMART RESUME] Found existing output for {uni_slug} with "
                    f"failed blocks: {failed_blocks_to_query}. Only missing blocks will be queried."
                )
                try:
                    prior_payload = UniversityPayload(**existing_payload_data)
                    if "main_info_contact" not in failed_blocks_to_query:
                        accumulated_results["main_info_contact"] = Q1Payload(
                            main_info=prior_payload.main_info,
                            contact=prior_payload.contact,
                        )
                    for qk in ("bachelors", "masters", "phd", "diploma"):
                        if qk not in failed_blocks_to_query:
                            accumulated_results[qk] = getattr(prior_payload.programs, qk)
                    if "faculties" not in failed_blocks_to_query:
                        accumulated_results["faculties"] = prior_payload.faculties
                except Exception as parse_err:
                    print(f"⚠️  [SMART RESUME] Could not parse existing payload: {parse_err}. Re-querying all.")
                    accumulated_results = {}
                    failed_blocks_to_query = None
        except Exception as e:
            print(f"⚠️  [SMART RESUME] Could not read existing JSON: {e}")

    active_engine = (engine or config.extraction_engine).lower().strip()
    use_deepseek = False
    use_gemini = False
    if active_engine == "auto":
        # DeepSeek first when its key is present, Gemini otherwise. Both are
        # direct-extraction engines, so the choice costs nothing structurally.
        from src.utilities.deepseek_client import is_deepseek_available
        from src.utilities.gemini_client import is_gemini_available
        if is_deepseek_available():
            use_deepseek = True
        elif is_gemini_available():
            use_gemini = True
        else:
            error_msg = (
                "No extraction engine is available: set DEEPSEEK_API_KEY, or "
                "GEMINI_API_KEY (one key is enough) for the Gemini engine."
            )
            print(f"\n❌ [ENGINE] {error_msg}")
            state_mgr.set_status(uni_slug, "failed", error_log=error_msg)
            return PipelineOutcome("failed", error_msg)
    elif active_engine == "deepseek":
        use_deepseek = True
    elif active_engine == "gemini":
        use_gemini = True

    if use_deepseek:
        from src.utilities.deepseek_client import DeepSeekQuotaError
        try:
            print(f"\n⚡ [ENGINE: DEEPSEEK DIRECT] Running DeepSeek-V4.1-Flash extraction engine for {uni_name}...")
            from src.extractor.crawlers.deepseek_extractor import extract_with_deepseek_engine

            state_mgr.set_status(uni_slug, "ingested", sources_ingested=len(links_list))
            payload, report = await extract_with_deepseek_engine(
                links_list=links_list,
                uni_name=uni_name,
                uni_slug=uni_slug,
                uni_domain=uni_domain,
                failed_blocks=failed_blocks_to_query,
                accumulated_results=accumulated_results,
            )
            return _persist_extraction_outputs(
                payload, report, uni_name, uni_slug, state_mgr, compile_master
            )

        except DeepSeekQuotaError as quota_err:
            if active_engine == "auto":
                from src.utilities.gemini_client import is_gemini_available
                if is_gemini_available():
                    print(f"\n⚠️  [ENGINE FALLBACK] DeepSeek balance/quota exhausted: {quota_err}")
                    print(f"    Falling back to the Gemini engine for {uni_name} and the rest of the batch...\n")
                    use_gemini = True
                else:
                    msg = f"{quota_err} (no Gemini key configured to fall back to)"
                    print(f"\n❌ [DEEPSEEK ERROR] {msg}")
                    state_mgr.set_status(uni_slug, "failed", error_log=msg)
                    return PipelineOutcome("failed", msg)
            else:
                print(f"\n❌ [DEEPSEEK ERROR] Insufficient balance: {quota_err}")
                state_mgr.set_status(uni_slug, "failed", error_log=str(quota_err))
                return PipelineOutcome("failed", str(quota_err))

    if use_gemini:
        from src.utilities.gemini_client import GeminiQuotaError, describe_gemini_keys
        try:
            model_name = config.gemini_model or "gemini-2.0-flash"
            print(f"\n⚡ [ENGINE: GEMINI] Running {model_name} extraction for {uni_name} ({describe_gemini_keys()})...")
            from src.extractor.crawlers.gemini_extractor import extract_with_gemini_engine

            state_mgr.set_status(uni_slug, "ingested", sources_ingested=len(links_list))
            payload, report = await extract_with_gemini_engine(
                links_list=links_list,
                uni_name=uni_name,
                uni_slug=uni_slug,
                uni_domain=uni_domain,
                failed_blocks=failed_blocks_to_query,
                accumulated_results=accumulated_results,
            )
            return _persist_extraction_outputs(
                payload, report, uni_name, uni_slug, state_mgr, compile_master
            )

        except GeminiQuotaError as quota_err:
            # Raised rather than returned: the batch loop stops on this, so the
            # remaining universities are not burned against a spent quota and
            # smart resume picks them up on the next run.
            error_msg = f"Gemini quota/rate limit exhausted: {quota_err}"
            print(f"\n❌ [GEMINI ERROR] {error_msg}")
            state_mgr.set_status(uni_slug, PARTIAL_EXTRACTION, error_log=error_msg)
            raise
        except Exception as e:
            error_msg = f"Pipeline execution error: {type(e).__name__}: {e}"
            print(f"❌ [PIPELINE ERROR] {error_msg}")
            state_mgr.set_status(uni_slug, "failed", error_log=error_msg)
            raise e


def _read_harvested_links(uni_slug: str) -> List[Dict[str, Any]]:
    """
    Phase 1's output for one university, in the shape the engines expect:
    records of at least {"url": str, "tier": int}, in Phase 1's rank order.

    `selected` is carried through where the partition records it. A false value
    marks a reserve link -- ranked and tiered like any other, held out of the
    corpus unless a selected link is unavailable. Older partitions and the
    flat-file fallback record no flag at all, and default to selected, which is
    exactly their pre-reserve behaviour.

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
      2. the flat file yields bare URL strings, which normalise to **tier 1**.
         Every source was therefore tier 1, and the tier-ordered corpus -- the
         whole reason the tier is recorded -- was a no-op.
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
    engine: Optional[str] = None,
):
    """
    Run every configured university, skipping those already complete.

    `resume` names a prior run token (`c_7`, `s_42`). Its manifest supplies the
    university list and the settings the original run used, so a resumed run
    reproduces the original rather than silently picking up whatever the
    settings file says today. What to skip still comes from StateManager.

    `dry_run` walks the whole queue, writes a real run log, and executes no
    phase. It spends no extraction quota, so it is the cheap way to check that a
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

    if engine:
        settings["engine"] = engine

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

        # Deliberately outlives individual universities so the batch reuses one
        # Chromium process throughout.
        await close_shared_crawler()
        await close_shared_typesafe_client()

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
                        force_rerun=settings.get("force_rerun_all", False),
                        engine=settings.get("engine"),
                    ),
                    timeout=config.university_timeout_sec,
                )
                # What actually happened, not merely "nothing was raised".
                # Phases 1 and 2 report failure by returning, so recording
                # "processed" for every non-raising call put LUMS in run c_1's
                # manifest as a success with no payload and no error.
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
            except GeminiQuotaError as qte:
                msg = f"Gemini quota/rate limit exhausted: {qte}"
                console.print(f"\n[bold red]🛑 {msg}[/bold red]")
                console.print(
                    f"[bold yellow]Pausing batch execution to protect account and avoid wasted retries. "
                    f"Smart resume will continue from this point when re-run.[/bold yellow]\n"
                )
                run_log.record(entry["slug"], "partial", error=msg)
                break
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
        "--engine",
        type=str,
        default=config.extraction_engine,
        choices=["auto", "deepseek", "gemini"],
        help="Extraction engine: 'auto' (DeepSeek if its key is present, else Gemini), 'deepseek', or 'gemini'",
    )
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
                engine=args.engine,
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
        "engine": args.engine,
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
                engine=args.engine,
            )
            run_log.record(slug, "processed", url=args.url)
        finally:
            # Closed here rather than in run_master_pipeline: both resources are
            # shared across a batch and must survive one university's failure.
            await close_shared_crawler()
            await close_shared_typesafe_client()


if __name__ == "__main__":
    sys.exit(main())
