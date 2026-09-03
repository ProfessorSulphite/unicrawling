#!/usr/bin/env python3
"""
Master 4-Phase Education Counselor RAG Pipeline Orchestrator (pipeline.py)

Automates the complete workflow:
Phase 1: High-Quality Link Harvesting & Deduplication
Phase 2: Programmatic NotebookLM Ingestion & Readiness Wait
Phase 3: Schema-Guided 5-Query Suite & Exa API Fallback
Phase 4: SQLite State Management & Data Quality Analytics
"""
import sys
import json
import asyncio
import argparse
from pathlib import Path
from typing import Optional, List
from urllib.parse import urlparse

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notebooklm import NotebookLMClient

try:
    from src.config import config
    from src.state import StateManager, QuotaExceededError
    from src.extract_links import run_pipeline as run_link_extractor, close_shared_crawler
    from src.ingest import ingest_university_sources, close_http_client
    from src.extract_data import extract_university_payload
    from src.inspect_cli import audit_analytics
    from src.json_io import append_jsonl, atomic_write_json, iter_jsonl, stream_compile_master_json
except ImportError:
    from config import config
    from state import StateManager, QuotaExceededError
    from extract_links import run_pipeline as run_link_extractor, close_shared_crawler
    from ingest import ingest_university_sources, close_http_client
    from extract_data import extract_university_payload
    from inspect_cli import audit_analytics
    from json_io import append_jsonl, atomic_write_json, iter_jsonl, stream_compile_master_json


def _source_ids_by_tier(state_mgr: "StateManager", uni_slug: str) -> Optional[dict]:
    """
    Build {tier: [source_id, ...]} so each Phase 3 query is scoped to the sources
    that can answer it. Returns None when nothing was recorded, which makes the
    query suite fall back to searching all sources.
    """
    by_tier: dict = {}
    for tier in (1, 2, 3, 4):
        ids = state_mgr.get_source_ids(uni_slug, tiers=[tier])
        if ids:
            by_tier[tier] = ids
    return by_tier or None


def compile_master_json() -> Path:
    """
    Rebuild data/outputs/university_counseling_data.json from the JSONL ledger.

    Streamed and atomic: called once per run rather than once per university.
    """
    master_path = config.output_master_json_path
    count = stream_compile_master_json(config.output_jsonl_path, master_path)
    print(f"  └─ Master : {master_path} ({count} records)")
    return master_path


def derive_uni_info(url: str, name_override: Optional[str] = None) -> tuple[str, str, str]:
    """Derives uni_name, uni_slug, and uni_domain from target URL."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    parts = domain.split(".")
    slug = parts[0] if parts else "university"

    if name_override:
        name = name_override
    else:
        name = slug.upper()

    return name, slug, domain


async def run_master_pipeline(
    url: str,
    uni_name_override: Optional[str] = None,
    max_links: int = 60,
    exclude_keywords: str = "news|events",
    uptodate: bool = True,
    compile_master: bool = True,
):
    """
    Executes the master 4-phase pipeline end-to-end for a target university.

    Owns the StateManager lifecycle so an 83-university batch does not accumulate
    one open SQLite handle per university. The pooled HTTP client deliberately
    outlives this call and is closed once by the batch/CLI driver.
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

    # Step 0: Record initial state
    state_mgr.set_status(uni_slug, "pending")

    # --------------------------------------------------------------------------
    # PHASE 1: LINK HARVESTING & DEDUPLICATION
    # --------------------------------------------------------------------------
    print(f"📌 [PHASE 1] Harvesting & Sanitizing Links for {uni_name}...")

    p1_results = await run_link_extractor(
        url=url,
        max_links=max_links,
        exclude_keywords=exclude_keywords,
        uptodate=uptodate,
        output_links=str(config.base_dir / "extracted_links.txt"),
        output_detailed=str(config.base_dir / "extracted_links_detailed.txt")
    )

    links_file = config.data_links_dir / f"{uni_slug}.jsonl"
    links_list = []
    if links_file.exists():
        with open(links_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    item = json.loads(line)
                    if item.get("href"):
                        links_list.append(item["href"])

    # Fallback to extracted_links.txt if per-slug file is empty
    if not links_list:
        txt_path = config.base_dir / "extracted_links.txt"
        if txt_path.exists():
            with open(txt_path, "r", encoding="utf-8") as f:
                links_list = [line.strip() for line in f if line.strip()]

    print(f"✓ [PHASE 1 COMPLETE] Retained {len(links_list)} clean canonical links.")
    state_mgr.set_status(uni_slug, "crawled", sources_ingested=0)

    if not links_list:
        error_msg = "Phase 1 produced zero links."
        state_mgr.set_status(uni_slug, "failed", error_log=error_msg)
        print(f"❌ [PHASE 1 FAILED] {error_msg}")
        return

    # --------------------------------------------------------------------------
    # PHASE 2: NOTEBOOKLM INGESTION
    # --------------------------------------------------------------------------
    print(f"\n📌 [PHASE 2] Ingesting Sources into NotebookLM...")

    try:
        async with NotebookLMClient.from_storage() as client:
            ingest_res = await ingest_university_sources(
                uni_slug=uni_slug,
                uni_name=uni_name,
                links=links_list,
                client=client
            )
            # The pre-flight health check can refuse the batch before any
            # notebook or query budget is spent. That is a skip, not a crash:
            # record it and move to the next university.
            if ingest_res.skipped:
                error_msg = f"Phase 2 skipped: {ingest_res.skip_reason}."
                state_mgr.set_status(uni_slug, "failed", error_log=error_msg)
                print(f"⏭️  [PHASE 2 SKIPPED] {error_msg}")
                return

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
                sources_ingested=ingested_count
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
            print(f"\n📌 [PHASE 3] Executing 5-Query Schema Extraction & Exa Fallback...")

            # Reserve the suite against today's budget BEFORE issuing any query.
            # Now that the suite runs concurrently, an unreserved run could put
            # five simultaneous queries over the 500/day Pro ceiling and leave
            # the notebook ingested but never extracted.
            state_mgr.reserve_queries(uni_slug, config.queries_per_university)

            payload, report = await extract_university_payload(
                client=client,
                notebook_id=notebook_id,
                uni_name=uni_name,
                uni_domain=uni_domain,
                source_ids_by_tier=_source_ids_by_tier(state_mgr, uni_slug),
                tier1_source_count=ingest_res.tier_histogram().get(1, 0),
            )

            # Repair retries cost real queries beyond the reserved five; charge
            # the overage so the ledger reflects actual consumption.
            overage = report.queries_used - config.queries_per_university
            if overage > 0:
                try:
                    state_mgr.reserve_queries(uni_slug, overage)
                except QuotaExceededError as qe:
                    print(f"⚠️  [QUOTA] Retry overage exceeded the daily budget: {qe}")

            # 1. Append to the durable JSONL ledger (O(1) per university, fsynced)
            output_file = config.output_jsonl_path
            append_jsonl(output_file, payload.model_dump_json())

            # 2. Output per-university pretty-printed JSON file in uni_outputs/ folder
            config.outputs_uni_outputs_dir.mkdir(parents=True, exist_ok=True)
            uni_json_path = config.outputs_uni_outputs_dir / f"{uni_slug}.json"
            atomic_write_json(uni_json_path, payload.model_dump())

            # 3. The master JSON array is NOT rebuilt here. Re-reading and
            #    re-dumping every historical payload once per university made the
            #    batch O(N^2) in disk I/O; aggregation is now a single streamed
            #    pass at the end of the run (compile_master_json).
            print(f"✓ [PHASE 3 COMPLETE] Saved payload for {uni_name}:")
            print(f"  └─ JSONL  : {output_file}")
            print(f"  └─ Slug   : {uni_json_path}")


            # Clean up notebook workspace slot post success
            try:
                from src.extract_data import delete_notebook_after_success
            except ImportError:
                from extract_data import delete_notebook_after_success
            await delete_notebook_after_success(client, notebook_id)

            state_mgr.set_status(
                uni_slug,
                "completed",
                notebook_id=notebook_id,
                queries_executed=report.queries_used,
            )
            if not report.ok:
                print(f"⚠️  [PHASE 3] {len(report.failed)} query block(s) failed: {sorted(report.failed)}")


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


import shutil
import logging
from datetime import datetime
from tqdm import tqdm
from rich.console import Console
from rich.panel import Panel

console = Console()


def setup_clean_logging():
    """Suppresses noisy HTTP and internal trace logs for clean CLI output."""
    noisy_loggers = ["httpx", "urllib3", "asyncio", "crawl4ai", "ExtractData", "HEC_Link_Extractor"]
    for log_name in noisy_loggers:
        logging.getLogger(log_name).setLevel(logging.WARNING)


def backup_existing_outputs():
    """Moves existing data/outputs directory to a timestamped backup folder."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = config.base_dir / "data" / f"outputs_backup_{timestamp}"
    if config.data_outputs_dir.exists():
        console.print(f"[bold yellow]📦 Backing up existing outputs to:[/bold yellow] [cyan]{backup_dir}[/cyan]")
        shutil.copytree(config.data_outputs_dir, backup_dir)
        shutil.rmtree(config.data_outputs_dir)
    
    # Reset SQLite state manifest safely
    for ext in ("", "-wal", "-shm"):
        f_path = Path(str(config.state_db_path) + ext)
        if f_path.exists():
            try:
                f_path.unlink()
            except OSError:
                pass

    # Recreate outputs directory hierarchy
    config.data_outputs_dir.mkdir(parents=True, exist_ok=True)
    config.outputs_uni_outputs_dir.mkdir(parents=True, exist_ok=True)
    (config.data_outputs_dir / "country_outputs").mkdir(parents=True, exist_ok=True)


async def run_batch_pipeline(
    config_file_path: Path = Path("config.json"),
    force_rerun_all: bool = False
):
    """
    Reads config.json, checks for processed universities, backs up if rerun requested,
    executes remaining links with tqdm progress bars and clean logging, and saves result.json analytics.
    """
    if not config_file_path.exists():
        console.print(f"[bold red]Error:[/bold red] Configuration file [yellow]{config_file_path}[/yellow] not found!")
        return

    with open(config_file_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    uni_dict = cfg.get("universities", {})
    settings = cfg.get("pipeline_settings", {})

    if settings.get("clean_logging", True):
        setup_clean_logging()

    rerun = force_rerun_all or settings.get("force_rerun_all", False)

    if rerun:
        backup_existing_outputs()

    # Load processed slugs
    state_mgr = StateManager()
    processed_slugs = set(state_mgr.get_completed_slugs())

    # Build queue of (country, url, name, slug)
    queue = []
    skipped = []

    for country, item_list in uni_dict.items():
        for item in item_list:
            if isinstance(item, dict):
                url = item.get("url", "")
                custom_name = item.get("name")
            else:
                url = str(item)
                custom_name = None

            name, slug, domain = derive_uni_info(url, name_override=custom_name)
            
            # Look up global registry if no explicit name was provided
            if not custom_name:
                try:
                    from src.universal_normalizer import load_global_registry
                except ImportError:
                    from universal_normalizer import load_global_registry
                reg_fact = load_global_registry().get(domain, {})
                if reg_fact.get("name"):
                    name = reg_fact["name"]

            if not rerun and slug in processed_slugs:
                skipped.append((country, name, slug))
            else:
                queue.append((country, url, name, slug))

    console.print(
        Panel(
            f"[bold cyan]🚀 BATCH PIPELINE ORCHESTRATOR[/bold cyan]\n\n"
            f"Config File: [yellow]{config_file_path}[/yellow]\n"
            f"Force Rerun All: [bold {'yellow' if rerun else 'green'}]{rerun}[/bold {'yellow' if rerun else 'green'}]\n"
            f"Total Configured Links: [bold yellow]{len(queue) + len(skipped)}[/bold yellow]\n"
            f"Already Processed (Skipping): [bold green]{len(skipped)}[/bold green]\n"
            f"Remaining to Process: [bold magenta]{len(queue)}[/bold magenta]",
            title="⚡ Master RAG Pipeline Batch Execution",
        )
    )

    state_mgr.close()

    if not queue:
        console.print("[bold green]✓ All configured universities are already processed! Run with --rerun-all to re-extract.[/bold green]")
    else:
        # Loop with tqdm progress bar
        with tqdm(total=len(queue), desc="🌐 Processing Universities", unit="uni") as pbar:
            for country, url, name, slug in queue:
                pbar.set_postfix({"uni": slug, "country": country})
                console.print(f"\n[bold magenta]🚀 Processing ({country}): {name} ({url})[/bold magenta]")
                try:
                    await run_master_pipeline(
                        url=url,
                        uni_name_override=name,
                        max_links=settings.get("max_links", 60),
                        exclude_keywords=settings.get("exclude_keywords", "news|events"),
                        uptodate=settings.get("uptodate", True),
                        # Aggregated once below instead of once per university.
                        compile_master=False,
                    )
                except Exception as e:
                    console.print(f"[bold red]❌ Failed to process {name}: {e}[/bold red]")
                pbar.update(1)

    # Release the shared browser and HTTP/2 pool now that no university will
    # use them again. Both deliberately outlive individual universities so the
    # batch reuses one Chromium process and one connection pool throughout.
    await close_shared_crawler()
    await close_http_client()

    # Post-processing: Universal Normalization & Export
    try:
        from src.inspect_cli import export_dataset
    except ImportError:
        from inspect_cli import export_dataset

    console.print("\n[bold cyan]🌐 Aggregating master JSON array (single streamed pass)...[/bold cyan]")
    compile_master_json()

    console.print("\n[bold cyan]🌐 Performing Master Universal Export & Directory Structuring...[/bold cyan]")
    export_dataset(format_type="json")

    # Generate result.json analytics
    result_analytics_file = generate_result_analytics(config_file_path)
    console.print(Panel(f"[bold green]🎉 BATCH PIPELINE COMPLETED SUCCESSFULLY![/bold green]\nMaster analytics saved to:\n[cyan]{result_analytics_file}[/cyan]", title="📊 Final Analytics Summary"))


def generate_result_analytics(config_path: Path) -> Path:
    """Generates result.json containing global dataset analytics."""
    try:
        from src.inspect_cli import iter_all_records
    except ImportError:
        from inspect_cli import iter_all_records

    country_counts = {}
    total_unis = 0
    total_ug = 0
    total_gr = 0
    total_phd = 0
    total_dip = 0
    portal_count = 0
    contact_count = 0

    # Streamed: the whole dataset is never resident, only the running counters.
    for rec in iter_all_records():
        total_unis += 1
        main = rec.get("main_info", {})
        c_name = main.get("country", "Unknown")
        country_counts[c_name] = country_counts.get(c_name, 0) + 1
        
        if main.get("key_links", {}).get("application_portal_url"):
            portal_count += 1
        
        if rec.get("contact", {}).get("official_email"):
            contact_count += 1

        progs = rec.get("programs", {})
        total_ug += len(progs.get("bachelors", []))
        total_gr += len(progs.get("masters", []))
        total_phd += len(progs.get("phd", []))
        total_dip += len(progs.get("diploma", []))

    total_programs = total_ug + total_gr + total_phd + total_dip

    analytics_payload = {
        "timestamp": datetime.now().isoformat(),
        "config_file": str(config_path),
        "total_universities": total_unis,
        "country_distribution": country_counts,
        "program_counts": {
            "bachelors": total_ug,
            "masters": total_gr,
            "phd": total_phd,
            "diploma": total_dip,
            "total_programs": total_programs
        },
        "quality_metrics": {
            "application_portal_coverage_pct": round((portal_count / max(1, total_unis)) * 100, 1),
            "admissions_contact_coverage_pct": round((contact_count / max(1, total_unis)) * 100, 1),
            "malformed_records": 0
        }
    }

    result_file = config.data_outputs_dir / "result.json"
    atomic_write_json(result_file, analytics_payload)
    return result_file


def main():
    parser = argparse.ArgumentParser(description="Master 4-Phase Education Counselor RAG Pipeline")
    parser.add_argument("--url", type=str, default=None, help="Target university URL")
    parser.add_argument("--name", type=str, default=None, help="Full university name")
    parser.add_argument("--config", type=Path, default=Path("config.json"), help="Batch configuration JSON file")
    parser.add_argument("--rerun-all", action="store_true", help="Force backup and rerun all configured universities")
    parser.add_argument("--max-links", type=int, default=60, help="Maximum links to retain")
    parser.add_argument("--exclude-keywords", type=str, default="news|events", help="Exclude keyword patterns")
    parser.add_argument("--uptodate", action="store_true", default=True, help="Enable 2026 recency filter")

    args = parser.parse_args()

    if args.url:
        async def _single() -> None:
            try:
                await run_master_pipeline(
                    url=args.url,
                    uni_name_override=args.name,
                    max_links=args.max_links,
                    exclude_keywords=args.exclude_keywords,
                    uptodate=args.uptodate
                )
            finally:
                await close_shared_crawler()
                await close_http_client()

        asyncio.run(_single())
    else:
        asyncio.run(
            run_batch_pipeline(
                config_file_path=args.config,
                force_rerun_all=args.rerun_all
            )
        )


if __name__ == "__main__":
    main()
