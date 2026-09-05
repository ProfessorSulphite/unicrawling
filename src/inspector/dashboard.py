"""
Rendered views of what the pipeline produced: one university, two side by side,
a global programme search, and the three system manifests (state, schema,
notebooks).

Read-only. No module here imports the orchestrator, at module scope or
otherwise -- see Finding 6. Triggering a run lives in cli.py.
"""
import asyncio
import json
from typing import Optional

from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

from src.config import config
from src.extractor.normalizers.runner import PROGRAM_BUCKETS
from src.inspector.formatting import (
    console,
    extract_numeric_fee,
    format_deadlines,
    show,
)
from src.inspector.records import find_university_record, load_all_records
from src.utilities.schema import UniversityPayload
from src.utilities.state_management import PARTIAL_EXTRACTION, StateManager


# ------------------------------------------------------------------------------
# 1. UNIVERSITY INSPECT COMMAND (inspect <slug>)
# ------------------------------------------------------------------------------

def inspect_university(query: str):
    """Deep inspect a specific university payload by slug or name."""
    record = find_university_record(query)

    if not record:
        console.print(
            f"[bold red]Error:[/bold red] No matching university payload found for '[bold yellow]{query}[/bold yellow]'."
        )
        console.print(f"Available files in [cyan]{config.outputs_uni_outputs_dir}[/cyan]:")
        if config.outputs_uni_outputs_dir.exists():
            for f in config.outputs_uni_outputs_dir.glob("*.json"):
                console.print(f" - [green]{f.stem}[/green] ({f.stat().st_size // 1024} KB)")
        return

    main = record.get("main_info", {})
    progs = record.get("programs", {})
    facs = record.get("faculties", [])
    contact = record.get("contact", {})

    # Header Panel
    portal_url = main.get("key_links", {}).get("application_portal_url")
    portal_badge = (
        f"[bold green]✓ {portal_url}[/bold green]"
        if portal_url
        else "[bold red]✗ Missing[/bold red]"
    )
    if main.get("exa_enriched"):
        portal_badge += " [yellow](Exa Enriched)[/yellow]"

    header_text = f"""[bold magenta]{show(main.get('name'), query)}[/bold magenta] ([yellow]{show(main.get('abbreviation'))}[/yellow])
[cyan]Website:[/cyan] {show(main.get('website'))}
[cyan]Type:[/cyan] {show(main.get('type'), 'Unknown').upper()} | [cyan]City:[/cyan] {show(main.get('city'))} | [cyan]Country:[/cyan] {show(main.get('country'), 'Unknown')}
[cyan]Application Portal:[/cyan] {portal_badge}
[dim]{show(main.get('description'), '')[:200]}[/dim]"""

    console.print(Panel(header_text, title="🏛️ Institution Profile", expand=False))

    # A payload that exists does not mean every query block in it succeeded -- a
    # block that failed every retry still yields a payload, just with that bucket
    # silently empty. This is how ITU shipped with zero bachelors programmes
    # despite 8 BS pages having been crawled.
    #
    # The payload now carries the reason itself, so this needs no database at
    # all; the state row is consulted only for the note attached to a partial or
    # failed run.
    warnings = []
    failed_blocks = record.get("failed_query_blocks") or []
    if failed_blocks:
        warnings.append(
            f"{len(failed_blocks)} query block(s) failed every retry: "
            f"{', '.join(failed_blocks)}. Those buckets are empty because the "
            f"query failed, NOT because the university offers none."
        )
    if record.get("programs_possibly_truncated"):
        warnings.append("Programme list looks truncated against the ingested source count.")

    if warnings:
        console.print(
            Panel(
                "[bold yellow]" + "\n".join(warnings) + "[/bold yellow]",
                title="⚠️  Extraction Warning",
                border_style="yellow",
                expand=False,
            )
        )

    # Programs Tables
    ug_list = progs.get("bachelors", [])
    gr_list = progs.get("masters", [])
    phd_list = progs.get("phd", [])
    dip_list = progs.get("diploma", [])

    console.print(
        f"\n[bold yellow]🎓 Academic Degree Programs "
        f"({len(ug_list) + len(gr_list) + len(phd_list) + len(dip_list)} Total)[/bold yellow]"
    )

    if ug_list:
        ug_table = Table(title="Bachelors Programs (BS / BSc)", show_lines=True)
        ug_table.add_column("Program Name", style="bold cyan")
        ug_table.add_column("Department", style="dim")
        ug_table.add_column("Duration", style="green")
        ug_table.add_column("Tuition Fee", style="yellow")
        ug_table.add_column("Eligibility / Entry Tests", style="magenta")
        ug_table.add_column("Deadline", style="red")

        for p in ug_list[:12]:
            elig = p.get("eligibility_requirements", {})
            tests = ", ".join(elig.get("entry_tests_accepted", [])) if isinstance(elig.get("entry_tests_accepted"), list) else ""
            elig_str = f"Min {show(elig.get('minimum_marks_percentage'))}"
            if tests:
                elig_str += f"\nTests: {tests}"

            ug_table.add_row(
                show(p.get("name")),
                show(p.get("department")),
                show(p.get("duration")),
                show(p.get("tuition_fee")),
                elig_str,
                format_deadlines(p),
            )
        console.print(ug_table)

    if gr_list:
        gr_table = Table(title="Graduate Programs (MS / MSc / EMBA)", show_lines=True)
        gr_table.add_column("Program Name", style="bold cyan")
        gr_table.add_column("Department", style="dim")
        gr_table.add_column("Duration", style="green")
        gr_table.add_column("Tuition Fee", style="yellow")
        gr_table.add_column("Deadline", style="red")

        for p in gr_list[:10]:
            gr_table.add_row(
                show(p.get("name")),
                show(p.get("department")),
                show(p.get("duration")),
                show(p.get("tuition_fee")),
                format_deadlines(p),
            )
        console.print(gr_table)

    if phd_list:
        phd_table = Table(title="PhD Programs", show_lines=True)
        phd_table.add_column("Program Name", style="bold magenta")
        phd_table.add_column("Department", style="dim")
        phd_table.add_column("Stipend / Fellowship Info", style="green")

        for p in phd_list:
            phd_table.add_row(
                show(p.get("name")),
                show(p.get("department")),
                show(p.get("scholarships_info")),
            )
        console.print(phd_table)

    if dip_list:
        dip_table = Table(title="Diploma & Certificate Programs", show_lines=True)
        dip_table.add_column("Program Name", style="bold blue")
        dip_table.add_column("Department", style="dim")
        dip_table.add_column("Duration", style="green")
        dip_table.add_column("Tuition Fee", style="yellow")

        for p in dip_list:
            dip_table.add_row(
                show(p.get("name")),
                show(p.get("department")),
                show(p.get("duration")),
                show(p.get("tuition_fee")),
            )
        console.print(dip_table)

    # Faculties Tree
    if facs:
        tree = Tree("[bold green]🏫 Faculties & Departments[/bold green]")
        for f in facs:
            f_node = tree.add(f"[bold cyan]{show(f.get('faculty_name'), 'Unnamed faculty')}[/bold cyan]")
            for d in f.get("departments", []):
                f_node.add(f"[dim]{d}[/dim]")
        console.print(tree)

    # Contact Info
    phones = contact.get("phone_numbers", [])
    phones_str = ", ".join(phones) if isinstance(phones, list) else str(phones)
    contact_panel = f"""[bold yellow]Official Email:[/bold yellow] {show(contact.get('official_email'))}
[bold yellow]Phones:[/bold yellow] {phones_str or 'N/A'}
[bold yellow]Address:[/bold yellow] {show(contact.get('physical_address'))}"""
    console.print(Panel(contact_panel, title="📞 Admissions Desk Contact", expand=False))


# ------------------------------------------------------------------------------
# 2. SIDE-BY-SIDE COMPARISON COMMAND (diff <slug1> <slug2>)
# ------------------------------------------------------------------------------

def _declared_currency(programs: dict) -> Optional[str]:
    """
    The one currency a university's programmes agree on, or None.

    Returns None when the programmes disagree as well as when none declares a
    currency: a single range covering two currencies is not a range, and since
    C19 the currency field is only set when something in the record actually
    supports it.
    """
    found = {
        str(prog.get("currency")).strip().upper()
        for bucket in PROGRAM_BUCKETS
        for prog in programs.get(bucket, [])
        if prog.get("currency")
    }
    return found.pop() if len(found) == 1 else None


def _fee_range(fees: list, programs: dict) -> str:
    """Format a min-max fee span, labelled only if the currency is unambiguous."""
    if not fees:
        return "N/A"
    span = f"{min(fees):,.0f} - {max(fees):,.0f}"
    currency = _declared_currency(programs)
    return f"{currency} {span}" if currency else span


def compare_universities(slug1: str, slug2: str):
    """Compares two extracted university payloads side-by-side in a Rich Table."""
    rec1 = find_university_record(slug1)
    rec2 = find_university_record(slug2)

    if not rec1:
        console.print(f"[bold red]Error:[/bold red] Could not find university payload for '[yellow]{slug1}[/yellow]'.")
        return
    if not rec2:
        console.print(f"[bold red]Error:[/bold red] Could not find university payload for '[yellow]{slug2}[/yellow]'.")
        return

    m1 = rec1.get("main_info", {})
    m2 = rec2.get("main_info", {})
    p1 = rec1.get("programs", {})
    p2 = rec2.get("programs", {})
    c1 = rec1.get("contact", {})
    c2 = rec2.get("contact", {})

    # Compute program counts
    ug1 = len(p1.get("bachelors", []))
    ug2 = len(p2.get("bachelors", []))
    gr1 = len(p1.get("masters", []))
    gr2 = len(p2.get("masters", []))
    phd1 = len(p1.get("phd", []))
    phd2 = len(p2.get("phd", []))
    dip1 = len(p1.get("diploma", []))
    dip2 = len(p2.get("diploma", []))

    # Fee ranges
    fees1 = [
        extract_numeric_fee(prog.get("tuition_fee"))
        for cat in PROGRAM_BUCKETS
        for prog in p1.get(cat, [])
    ]
    fees1 = [f for f in fees1 if f is not None]
    fee_str1 = _fee_range(fees1, p1)

    fees2 = [
        extract_numeric_fee(prog.get("tuition_fee"))
        for cat in PROGRAM_BUCKETS
        for prog in p2.get(cat, [])
    ]
    fees2 = [f for f in fees2 if f is not None]
    fee_str2 = _fee_range(fees2, p2)

    portal1 = m1.get("key_links", {}).get("application_portal_url")
    portal2 = m2.get("key_links", {}).get("application_portal_url")

    table = Table(
        title=f"⚔️ University Side-by-Side Comparison: "
              f"{show(m1.get('name'), slug1)} vs {show(m2.get('name'), slug2)}",
        show_lines=True,
    )
    table.add_column("Metric / Feature", style="bold cyan")
    table.add_column(f"{show(m1.get('name'), slug1)} ({show(m1.get('abbreviation'), slug1).upper()})", style="bold yellow")
    table.add_column(f"{show(m2.get('name'), slug2)} ({show(m2.get('abbreviation'), slug2).upper()})", style="bold green")

    table.add_row("Institution Type", show(m1.get("type")).upper(), show(m2.get("type")).upper())
    table.add_row("City / Location", show(m1.get("city")), show(m2.get("city")))
    table.add_row("Country", show(m1.get("country"), "Unknown"), show(m2.get("country"), "Unknown"))
    table.add_row("Website Domain", show(m1.get("website")), show(m2.get("website")))
    table.add_row(
        "Application Portal URL",
        f"[green]{portal1}[/green]" if portal1 else "[red]Missing[/red]",
        f"[green]{portal2}[/green]" if portal2 else "[red]Missing[/red]",
    )
    table.add_row("Bachelors Programs (BS)", str(ug1), str(ug2))
    table.add_row("Graduate Programs (MS)", str(gr1), str(gr2))
    table.add_row("PhD & Doctoral Programs", str(phd1), str(phd2))
    table.add_row("Diploma & Certificate Programs", str(dip1), str(dip2))
    table.add_row("Total Degree Offerings", str(ug1 + gr1 + phd1 + dip1), str(ug2 + gr2 + phd2 + dip2))
    table.add_row("Tuition Fee Range", fee_str1, fee_str2)
    table.add_row("Faculties / Schools Count", str(len(rec1.get("faculties", []))), str(len(rec2.get("faculties", []))))

    e1_phones = c1.get("phone_numbers", [])
    p1_str = ", ".join(e1_phones) if isinstance(e1_phones, list) else str(e1_phones)
    e2_phones = c2.get("phone_numbers", [])
    p2_str = ", ".join(e2_phones) if isinstance(e2_phones, list) else str(e2_phones)

    table.add_row("Official Email", show(c1.get("official_email")), show(c2.get("official_email")))
    table.add_row("Admissions Office Phone", p1_str or "N/A", p2_str or "N/A")

    console.print(table)


# ------------------------------------------------------------------------------
# 3. GLOBAL PROGRAM & FEE SEARCH COMMAND (search <keyword>)
# ------------------------------------------------------------------------------

def search_programs(keyword: str, level: Optional[str] = None, max_fee: Optional[float] = None):
    """Searches across all extracted degree programs globally across all universities."""
    records = load_all_records()
    kw_clean = keyword.lower().strip()
    matches = []

    for rec in records:
        uni_name = rec.get("main_info", {}).get("name", "Unknown Uni")
        portal_url = rec.get("main_info", {}).get("key_links", {}).get("application_portal_url", "")
        progs = rec.get("programs", {})

        categories = [
            ("bachelors", "BS / BSc"),
            ("masters", "MS / MSc"),
            ("phd", "PhD"),
            ("diploma", "Diploma / Certificate"),
        ]

        for cat_key, cat_label in categories:
            if level and level.lower() not in cat_key and level.lower() not in cat_label.lower():
                continue

            for p in progs.get(cat_key, []):
                p_name = p.get("name", "")
                dept = p.get("department", "")
                summary = p.get("description") or p.get("summary_3_lines", "")
                elig = p.get("eligibility_requirements", {})
                elig_text = str(elig)
                courses = " ".join(p.get("courses_taught", [])) if isinstance(p.get("courses_taught"), list) else ""

                full_text = f"{p_name} {dept} {summary} {elig_text} {courses}".lower()

                if kw_clean in full_text:
                    fee_str = p.get("tuition_fee", "")
                    num_fee = extract_numeric_fee(fee_str)

                    if max_fee is not None and num_fee is not None and num_fee > max_fee:
                        continue

                    matches.append(
                        {
                            "university": show(uni_name, "Unknown University"),
                            "category": cat_label,
                            "program_name": show(p_name),
                            "department": show(dept),
                            "tuition_fee": fee_str or "N/A",
                            "deadline": format_deadlines(p),
                            "portal_url": portal_url,
                        }
                    )

    if not matches:
        console.print(
            f"[yellow]No degree programs matched keyword '[bold]{keyword}[/bold]'[/yellow] "
            f"(filters: level={level or 'all'}, max_fee={max_fee or 'unlimited'})."
        )
        return

    table = Table(
        title=f"🔍 Global Degree Search Results for '{keyword}' ({len(matches)} Matches Found)",
        show_lines=True,
    )
    table.add_column("University", style="bold cyan")
    table.add_column("Level", style="dim")
    table.add_column("Program Name", style="bold yellow")
    table.add_column("Department", style="magenta")
    table.add_column("Tuition Fee", style="green")
    table.add_column("Deadline", style="red")

    for m in matches[:25]:  # Limit output table display cap
        table.add_row(
            m["university"],
            m["category"],
            m["program_name"],
            m["department"],
            m["tuition_fee"],
            m["deadline"],
        )

    console.print(table)
    if len(matches) > 25:
        console.print(f"[dim]Showing top 25 of {len(matches)} matching degree programs.[/dim]")

    return matches


# ------------------------------------------------------------------------------
# 7. PIPELINE STATE MANIFEST COMMAND (state)
# ------------------------------------------------------------------------------

def inspect_state():
    """Audits SQLite pipeline execution state."""
    try:
        sm = StateManager()
        records = sm.list_all()

        table = Table(title="🗄️ SQLite Pipeline State Manifest", show_lines=True)
        table.add_column("University Slug", style="bold cyan")
        table.add_column("Status", style="bold yellow")
        table.add_column("Notebook ID", style="dim")
        table.add_column("Sources", style="green")
        table.add_column("Queries", style="magenta")
        table.add_column("Last Updated", style="dim")
        table.add_column("Notes", style="dim")

        for r in records:
            error_log = r.get('error_log')
            status_label = r['status']
            status_style = "bold green" if r['status'] == 'completed' else "yellow"
            if r['status'] == 'failed':
                status_style = "bold red"
            elif r['status'] == PARTIAL_EXTRACTION:
                # A payload was written, but a query block in it failed every
                # retry, so a degree bucket is empty for a reason that has
                # nothing to do with the university. A resumed run retries these.
                status_style = "bold yellow"
                status_label = f"{status_label} ⚠"

            table.add_row(
                show(r.get('university_slug')).upper(),
                f"[{status_style}]{status_label}[/{status_style}]",
                r.get('notebook_id') or "N/A",
                str(r.get('sources_ingested', 0)),
                str(r.get('queries_executed', 0)),
                str(r.get('updated_at', 'N/A')),
                (error_log[:80] + "…") if error_log and len(error_log) > 80 else (error_log or "—"),
            )
        console.print(table)
    except Exception as e:
        console.print(f"[bold red]Error reading SQLite state database:[/bold red] {e}")


# ------------------------------------------------------------------------------
# 8. JSON SCHEMA COMMAND (schema)
# ------------------------------------------------------------------------------

def inspect_schema():
    """Outputs colorized Master JSON Schema definition."""
    try:
        schema_json = json.dumps(UniversityPayload.model_json_schema(), indent=2)
        syntax = Syntax(schema_json, "json", theme="monokai", line_numbers=True)
        console.print(Panel(syntax, title="📜 Master UniversityPayload JSON Schema"))
    except Exception as e:
        console.print(f"[bold red]Failed to generate schema:[/bold red] {e}")


# ------------------------------------------------------------------------------
# 9. NOTEBOOKLM ACTIVE WORKSPACES COMMAND (notebooks)
# ------------------------------------------------------------------------------

async def _inspect_notebooks_async():
    from notebooklm import NotebookLMClient
    async with NotebookLMClient.from_storage() as client:
        notebooks = await client.notebooks.list()
        table = Table(title=f"☁️ Active NotebookLM Notebooks (Total: {len(notebooks)})", show_lines=True)
        table.add_column("Notebook ID", style="dim")
        table.add_column("Title", style="bold cyan")
        table.add_column("Sources Count", style="bold yellow")

        for nb in notebooks:
            try:
                sources = await client.sources.list(nb.id)
                count_str = str(len(sources))
            except Exception:
                count_str = "N/A"
            table.add_row(nb.id, getattr(nb, "title", "Untitled"), count_str)
        console.print(table)

def inspect_notebooks():
    try:
        asyncio.run(_inspect_notebooks_async())
    except Exception as e:
        console.print(f"[bold red]NotebookLM Inspection Error:[/bold red] {e}")
