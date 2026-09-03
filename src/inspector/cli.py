"""
The inspector's entry surface: the argparse command table and the Rich TUI menu.

This is the *only* module in the package that reaches the orchestrator, and it
does so through function-local imports inside `retry_pipeline` and the `batch`
command. That is the whole of Finding 6's rule: `inspector/` never imports the
orchestrator at module scope, so the orchestrator is free to import `inspector/`
without closing a cycle.

`interactive_menu` lives here rather than in dashboard.py for the same reason --
it offers "retry", so anything holding it would inherit that dependency.
"""
import argparse
import asyncio
import sys
from pathlib import Path

from rich.prompt import Confirm, Prompt

from src.config import config
from src.inspector.analytics import audit_analytics
from src.inspector.auditor import audit_corpus
from src.inspector.dashboard import (
    compare_universities,
    inspect_notebooks,
    inspect_schema,
    inspect_state,
    inspect_university,
    search_programs,
)
from src.inspector.formatting import console
from src.inspector.sync import export_dataset


# ------------------------------------------------------------------------------
# 4. PIPELINE STATE RETRY COMMAND (retry [slug|failed|pending|all])
# ------------------------------------------------------------------------------

def retry_pipeline(target: str = "failed"):
    """Connects to SQLite state database and re-runs pipeline for failed/pending runs."""
    try:
        from src.state import StateManager
        from src.pipeline import run_master_pipeline
    except ImportError:
        from state import StateManager
        from pipeline import run_master_pipeline

    sm = StateManager()
    all_states = sm.list_all()

    target_clean = target.lower().strip()
    to_retry = []

    if target_clean in ("failed", "pending", "all"):
        for r in all_states:
            status = r.get("status")
            if target_clean == "all" or status == target_clean:
                to_retry.append(r)
    else:
        # Match specific slug
        for r in all_states:
            if target_clean in r.get("university_slug", "").lower():
                to_retry.append(r)

    if not to_retry:
        console.print(f"[bold green]No pipeline runs matching '[yellow]{target}[/yellow]' require retry.[/bold green]")
        return

    console.print(f"[bold yellow]Found {len(to_retry)} university pipeline runs to retry:[/bold yellow]")
    for item in to_retry:
        console.print(f" - [cyan]{item['university_slug']}[/cyan] (Current Status: [bold red]{item['status']}[/bold red])")

    if not Confirm.ask("Do you want to initiate automatic pipeline execution for these universities?"):
        console.print("[yellow]Retry cancelled by user.[/yellow]")
        return

    for item in to_retry:
        slug = item["university_slug"]
        console.print(f"\n[bold magenta]🚀 Retrying pipeline execution for slug: {slug}...[/bold magenta]")
        # The state row is supposed to carry the URL that was crawled. The
        # .edu.pk guess below is a leftover from a Pakistan-only corpus and is
        # wrong for every other country, so it is used only as a last resort and
        # announced when it is.
        url = item.get("website") or item.get("url")
        if not url:
            url = f"https://{slug}.edu.pk"
            console.print(
                f"[yellow]No URL recorded for {slug}; guessing {url}. "
                f"This guess only holds for Pakistani institutions.[/yellow]"
            )
        try:
            asyncio.run(run_master_pipeline(url=url, max_links=60))
            console.print(f"[bold green]✓ Pipeline successfully completed for {slug}![/bold green]")
        except Exception as e:
            console.print(f"[bold red]❌ Retry failed for {slug}: {e}[/bold red]")


# ------------------------------------------------------------------------------
# 10. INTERACTIVE TUI MENU (interactive)
# ------------------------------------------------------------------------------

def interactive_menu():
    """Renders a Rich interactive menu prompting user choices."""
    while True:
        console.print("\n" + "=" * 55, style="cyan")
        console.print("🎓 Education Counselor RAG Developer Workstation", style="bold green")
        console.print("=" * 55, style="cyan")
        console.print("1. 🏛️ Deep Inspect University")
        console.print("2. ⚔️ Compare Two Universities (Diff)")
        console.print("3. 🔍 Search Degree Programs Globally")
        console.print("4. 🔄 Retry Failed / Pending Pipeline Runs")
        console.print("5. 📊 View Dataset Analytics Dashboard")
        console.print("A. 🔬 Audit Per-Programme Required Fields (Supabase readiness)")
        console.print("6. 🗄️ Inspect SQLite State Manifest")
        console.print("7. 📜 Display Master JSON Schema")
        console.print("8. ☁️ List Active NotebookLM Notebooks")
        console.print("9. 📤 Export Dataset (CSV / JSON)")
        console.print("0. 🚪 Exit")
        console.print("=" * 55, style="cyan")

        choice = Prompt.ask(
            "Select an option",
            choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "A"],
            default="0",
        ).upper()

        if choice == "0":
            console.print("[yellow]Exiting Developer Workstation. Goodbye![/yellow]")
            break
        elif choice == "1":
            slug = Prompt.ask("Enter university slug or name (e.g., itu, nust, ncbae)")
            if slug:
                inspect_university(slug)
        elif choice == "2":
            s1 = Prompt.ask("Enter first university slug (e.g., itu)")
            s2 = Prompt.ask("Enter second university slug (e.g., ncbae)")
            if s1 and s2:
                compare_universities(s1, s2)
        elif choice == "3":
            kw = Prompt.ask("Enter search keyword (e.g., data science, computer, scholarship)")
            lvl = Prompt.ask("Filter by level (BS, MS, PhD, or press Enter for all)", default="")
            max_f = Prompt.ask("Filter max tuition fee, in the fee's own currency (or press Enter for none)", default="")
            fee_val = float(max_f) if max_f.strip().isdigit() else None
            if kw:
                search_programs(kw, level=lvl or None, max_fee=fee_val)
        elif choice == "4":
            tgt = Prompt.ask("Target slug or state filter", choices=["failed", "pending", "all"], default="failed")
            retry_pipeline(tgt)
        elif choice == "5":
            audit_analytics()
        elif choice == "A":
            audit_corpus()
        elif choice == "6":
            inspect_state()
        elif choice == "7":
            inspect_schema()
        elif choice == "8":
            inspect_notebooks()
        elif choice == "9":
            fmt = Prompt.ask("Select export format", choices=["csv", "json"], default="json")
            export_dataset(fmt)


# ------------------------------------------------------------------------------
# CLI ENTRY POINT
# ------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Developer-Grade Education Counselor RAG CLI Tool")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: inspect <query>
    inspect_parser = subparsers.add_parser("inspect", help="Deep inspect a university payload by slug or name")
    inspect_parser.add_argument("query", type=str, help="University slug or name (e.g. 'itu', 'ncbae', 'nust')")

    # Command: diff <slug1> <slug2>
    diff_parser = subparsers.add_parser("diff", help="Compare two universities side-by-side")
    diff_parser.add_argument("slug1", type=str, help="First university slug")
    diff_parser.add_argument("slug2", type=str, help="Second university slug")

    # Command: search <keyword>
    search_parser = subparsers.add_parser("search", help="Global search across all degree programs")
    search_parser.add_argument("keyword", type=str, help="Keyword to search (e.g., 'data science')")
    search_parser.add_argument("--level", type=str, default=None, help="Degree level filter (BS, MS, PhD)")
    search_parser.add_argument(
        "--max-fee",
        type=float,
        default=None,
        # Compares the first number in the fee string, whatever currency it is in,
        # so across a multi-country corpus this filter is only meaningful when
        # combined with a single-currency slice. See extract_numeric_fee.
        help="Maximum tuition fee filter, compared in the fee's own currency",
    )

    # Command: retry [target]
    retry_parser = subparsers.add_parser("retry", help="Retry failed or pending pipeline state runs")
    retry_parser.add_argument("target", type=str, nargs="?", default="failed", help="Target slug or status ('failed', 'pending', 'all')")

    # Command: export
    export_parser = subparsers.add_parser("export", help="Export dataset into CSV or Country-Grouped JSON format")
    export_parser.add_argument("--format", type=str, default="csv", choices=["csv", "json"], help="Export format")
    export_parser.add_argument("--output", type=Path, default=None, help="Custom output file path")

    # Command: analytics
    analytics_parser = subparsers.add_parser("analytics", help="Audit dataset health & quality metrics")
    analytics_parser.add_argument("--file", type=Path, default=config.output_jsonl_path, help="Path to JSONL file")

    # Command: audit
    subparsers.add_parser(
        "audit",
        help="Audit per-programme required-field coverage and rule on push readiness",
    )

    # Command: state
    subparsers.add_parser("state", help="Inspect SQLite pipeline execution state manifest")

    # Command: schema
    subparsers.add_parser("schema", help="Display Master JSON Data Schema")

    # Command: notebooks
    subparsers.add_parser("notebooks", help="List active NotebookLM notebooks and source counts")

    # Command: batch
    batch_parser = subparsers.add_parser("batch", help="Run multi-country batch pipeline from config.json")
    batch_parser.add_argument("--config", type=Path, default=Path("config.json"), help="Configuration JSON file")
    batch_parser.add_argument("--rerun-all", action="store_true", help="Force backup and rerun all configured universities")

    # Command: interactive
    subparsers.add_parser("interactive", help="Launch Rich Interactive TUI Menu")

    args = parser.parse_args()

    if args.command == "batch":
        try:
            from src.pipeline import run_batch_pipeline
        except ImportError:
            from pipeline import run_batch_pipeline
        asyncio.run(run_batch_pipeline(config_file_path=args.config, force_rerun_all=args.rerun_all))
    elif args.command == "inspect":
        inspect_university(args.query)
    elif args.command == "diff":
        compare_universities(args.slug1, args.slug2)
    elif args.command == "search":
        search_programs(args.keyword, level=args.level, max_fee=args.max_fee)
    elif args.command == "retry":
        retry_pipeline(args.target)
    elif args.command == "export":
        export_dataset(format_type=args.format, output_path=args.output)
    elif args.command == "analytics":
        audit_analytics(args.file)
    elif args.command == "audit":
        # The one command whose answer a caller may need to branch on: a
        # non-zero exit is what stops a push script at the gate.
        return 0 if audit_corpus().ready else 1
    elif args.command == "state":
        inspect_state()
    elif args.command == "schema":
        inspect_schema()
    elif args.command == "notebooks":
        inspect_notebooks()
    elif args.command == "interactive":
        interactive_menu()
    else:
        parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
