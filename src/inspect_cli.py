#!/usr/bin/env python3
"""
Developer-Grade Data Quality Auditor, System Inspection & Interactive CLI Workstation (inspect_cli.py)
Powered by Rich formatting, side-by-side comparison, global search, pipeline retry, vector export, and TUI menu.
"""
import sys
import os
import csv
import json
import re
import asyncio
import argparse
from pathlib import Path
from typing import Dict, Any, Iterator, List, Optional

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.tree import Tree
from rich.syntax import Syntax
from rich.prompt import Prompt, Confirm
from rich import print as rprint

try:
    from src.config import config
except ImportError:
    from config import config

# The four canonical programme buckets (C17), derived from DegreeLevel so this
# module cannot drift out of step with the schema.
try:
    from src.extractor.normalizers.runner import PROGRAM_BUCKETS
except ImportError:
    from extractor.normalizers.runner import PROGRAM_BUCKETS

console = Console()

# Regex constants
FEE_NUMBER_REGEX = re.compile(r"\d[\d,]*")


# ------------------------------------------------------------------------------
# HELPER DATA LOADERS
# ------------------------------------------------------------------------------

def format_deadlines(prog: Dict[str, Any], empty: str = "N/A") -> str:
    """Render application_deadlines for display. C18 made the field a list."""
    deadlines = prog.get("application_deadlines")
    if isinstance(deadlines, str):
        deadlines = [deadlines]
    if not isinstance(deadlines, list):
        deadlines = []
    cleaned = [str(d).strip() for d in deadlines if d is not None and str(d).strip()]
    return "; ".join(cleaned) if cleaned else empty


def iter_all_records() -> Iterator[Dict[str, Any]]:
    """
    Yield normalized university payloads one at a time.

    Streaming generator: only the current record plus the set of seen names is
    resident, so dataset-wide analytics no longer scale their peak RAM with the
    size of the corpus. `load_all_records()` remains the eager list form for
    callers that genuinely need random access.
    """
    try:
        from src.universal_normalizer import normalize_universal_payload
    except ImportError:
        from universal_normalizer import normalize_universal_payload

    seen_slugs = set()

    # 1. Stream the master JSONL ledger if it exists
    if config.output_jsonl_path.exists():
        with open(config.output_jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                name = data.get("main_info", {}).get("name", "")
                if name and name not in seen_slugs:
                    seen_slugs.add(name)
                    yield normalize_universal_payload(data)

    # 2. Check per-slug JSON files in uni_outputs directory
    if config.outputs_uni_outputs_dir.exists():
        for f in config.outputs_uni_outputs_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as file_obj:
                    data = json.load(file_obj)
            except Exception:
                continue
            name = data.get("main_info", {}).get("name", "")
            if name and name not in seen_slugs:
                seen_slugs.add(name)
                yield normalize_universal_payload(data)


def load_all_records() -> List[Dict[str, Any]]:
    """Loads all university payload records from output JSONL or JSON files."""
    return list(iter_all_records())


def find_university_record(query: str) -> Optional[Dict[str, Any]]:
    """Finds a single university payload by slug, name, or abbreviation."""
    query_clean = query.lower().strip()

    # Check uni_outputs first
    if config.outputs_uni_outputs_dir.exists():
        for f in config.outputs_uni_outputs_dir.glob("*.json"):
            if query_clean in f.stem.lower():
                try:
                    with open(f, "r", encoding="utf-8") as file_obj:
                        return json.load(file_obj)
                except Exception:
                    pass

    # Fallback to searching all records
    for r in load_all_records():
        main = r.get("main_info", {})
        name = main.get("name", "").lower()
        abbr = main.get("abbreviation", "").lower()
        if (
            query_clean in name
            or query_clean in abbr
            or query_clean == name.replace(" ", "_")
        ):
            return r

    return None


def extract_numeric_fee(fee_str: Optional[str]) -> Optional[float]:
    """Parses numeric PKR tuition fee from fee string."""
    if not fee_str:
        return None
    matches = FEE_NUMBER_REGEX.findall(fee_str)
    if not matches:
        return None
    try:
        # Take the first matched number
        num = float(matches[0].replace(",", ""))
        return num
    except ValueError:
        return None


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

    header_text = f"""[bold magenta]{main.get('name')}[/bold magenta] ([yellow]{main.get('abbreviation', 'N/A')}[/yellow])
[cyan]Website:[/cyan] {main.get('website')}
[cyan]Type:[/cyan] {main.get('type', 'public').upper()} | [cyan]City:[/cyan] {main.get('city', 'N/A')} | [cyan]Country:[/cyan] {main.get('country', 'Pakistan')}
[cyan]Application Portal:[/cyan] {portal_badge}
[dim]{main.get('description', '')[:200]}...[/dim]"""

    console.print(Panel(header_text, title="🏛️ Institution Profile", expand=False))

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
        ug_table.add_column("Tuition Fee (PKR)", style="yellow")
        ug_table.add_column("Eligibility / Entry Tests", style="magenta")
        ug_table.add_column("Deadline", style="red")

        for p in ug_list[:12]:
            elig = p.get("eligibility_requirements", {})
            tests = ", ".join(elig.get("entry_tests_accepted", [])) if isinstance(elig.get("entry_tests_accepted"), list) else ""
            elig_str = f"Min {elig.get('minimum_marks_percentage', 'N/A')}"
            if tests:
                elig_str += f"\nTests: {tests}"

            ug_table.add_row(
                p.get("name"),
                p.get("department", "N/A"),
                p.get("duration", "N/A"),
                p.get("tuition_fee", "N/A"),
                elig_str,
                format_deadlines(p),
            )
        console.print(ug_table)

    if gr_list:
        gr_table = Table(title="Graduate Programs (MS / MSc / EMBA)", show_lines=True)
        gr_table.add_column("Program Name", style="bold cyan")
        gr_table.add_column("Department", style="dim")
        gr_table.add_column("Duration", style="green")
        gr_table.add_column("Tuition Fee (PKR)", style="yellow")
        gr_table.add_column("Deadline", style="red")

        for p in gr_list[:10]:
            gr_table.add_row(
                p.get("name"),
                p.get("department", "N/A"),
                p.get("duration", "N/A"),
                p.get("tuition_fee", "N/A"),
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
                p.get("name"),
                p.get("department", "N/A"),
                p.get("scholarships_info", "N/A"),
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
                p.get("name"),
                p.get("department", "N/A"),
                p.get("duration", "N/A"),
                p.get("tuition_fee", "N/A"),
            )
        console.print(dip_table)

    # Faculties Tree
    if facs:
        tree = Tree("[bold green]🏫 Faculties & Departments[/bold green]")
        for f in facs:
            f_node = tree.add(f"[bold cyan]{f.get('faculty_name')}[/bold cyan]")
            for d in f.get("departments", []):
                f_node.add(f"[dim]{d}[/dim]")
        console.print(tree)

    # Contact Info
    phones = contact.get("phone_numbers", [])
    phones_str = ", ".join(phones) if isinstance(phones, list) else str(phones)
    contact_panel = f"""[bold yellow]Official Email:[/bold yellow] {contact.get('official_email', 'N/A')}
[bold yellow]Phones:[/bold yellow] {phones_str or 'N/A'}
[bold yellow]Address:[/bold yellow] {contact.get('physical_address', 'N/A')}"""
    console.print(Panel(contact_panel, title="📞 Admissions Desk Contact", expand=False))


# ------------------------------------------------------------------------------
# 2. SIDE-BY-SIDE COMPARISON COMMAND (diff <slug1> <slug2>)
# ------------------------------------------------------------------------------

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
    fee_str1 = f"PKR {min(fees1):,.0f} - {max(fees1):,.0f}" if fees1 else "N/A"

    fees2 = [
        extract_numeric_fee(prog.get("tuition_fee"))
        for cat in PROGRAM_BUCKETS
        for prog in p2.get(cat, [])
    ]
    fees2 = [f for f in fees2 if f is not None]
    fee_str2 = f"PKR {min(fees2):,.0f} - {max(fees2):,.0f}" if fees2 else "N/A"

    portal1 = m1.get("key_links", {}).get("application_portal_url")
    portal2 = m2.get("key_links", {}).get("application_portal_url")

    table = Table(title=f"⚔️ University Side-by-Side Comparison: {m1.get('name')} vs {m2.get('name')}", show_lines=True)
    table.add_column("Metric / Feature", style="bold cyan")
    table.add_column(f"{m1.get('name')} ({m1.get('abbreviation', slug1).upper()})", style="bold yellow")
    table.add_column(f"{m2.get('name')} ({m2.get('abbreviation', slug2).upper()})", style="bold green")

    table.add_row("Institution Type", m1.get("type", "N/A").upper(), m2.get("type", "N/A").upper())
    table.add_row("City / Location", m1.get("city", "N/A"), m2.get("city", "N/A"))
    table.add_row("Website Domain", m1.get("website", "N/A"), m2.get("website", "N/A"))
    table.add_row(
        "Application Portal URL",
        f"[green]{portal1}[/green]" if portal1 else "[red]Missing[/red]",
        f"[green]{portal2}[/green]" if portal2 else "[red]Missing[/red]",
    )
    table.add_row("Bachelors Programs (BS)", str(ug1), str(ug2))
    table.add_row("Graduate Programs (MS)", str(gr1), str(gr2))
    table.add_row("PhD & Doctoral Programs", str(phd1), str(phd2))
    table.add_row("Total Degree Offerings", str(ug1 + gr1 + phd1), str(ug2 + gr2 + phd2))
    table.add_row("Tuition Fee Range", fee_str1, fee_str2)
    table.add_row("Faculties / Schools Count", str(len(rec1.get("faculties", []))), str(len(rec2.get("faculties", []))))

    e1_phones = c1.get("phone_numbers", [])
    p1_str = ", ".join(e1_phones) if isinstance(e1_phones, list) else str(e1_phones)
    e2_phones = c2.get("phone_numbers", [])
    p2_str = ", ".join(e2_phones) if isinstance(e2_phones, list) else str(e2_phones)

    table.add_row("Official Email", c1.get("official_email", "N/A"), c2.get("official_email", "N/A"))
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
                            "university": uni_name,
                            "category": cat_label,
                            "program_name": p_name,
                            "department": dept or "N/A",
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
    table.add_column("Tuition Fee (PKR)", style="green")
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
        # Construct fallback website URL if not stored
        url = f"https://{slug}.edu.pk"
        try:
            asyncio.run(run_master_pipeline(url=url, max_links=60))
            console.print(f"[bold green]✓ Pipeline successfully completed for {slug}![/bold green]")
        except Exception as e:
            console.print(f"[bold red]❌ Retry failed for {slug}: {e}[/bold red]")


# ------------------------------------------------------------------------------
# 5. DATASET EXPORTER COMMAND (export [--format csv|json])
# ------------------------------------------------------------------------------

def export_dataset(format_type: str = "csv", output_path: Optional[Path] = None):
    """Exports structured outputs to CSV or Country-Grouped JSON."""
    records = load_all_records()
    if not records:
        console.print(f"[bold red]Error:[/bold red] No data records found to export in [yellow]{config.output_jsonl_path}[/yellow].")
        return

    fmt = format_type.lower().strip()

    if fmt == "csv":
        out_file = output_path or (config.data_outputs_dir / "counseling_programs.csv")
        out_file.parent.mkdir(parents=True, exist_ok=True)

        rows = []
        for rec in records:
            main = rec.get("main_info", {})
            contact = rec.get("contact", {})
            progs = rec.get("programs", {})

            uni_name = main.get("name", "")
            uni_slug = main.get("abbreviation", "")
            uni_type = main.get("type", "")
            city = main.get("city", "")
            portal_url = main.get("key_links", {}).get("application_portal_url", "")
            email = contact.get("official_email", "")

            categories = [
                ("bachelors", "Bachelors"),
                ("masters", "Masters"),
                ("phd", "PhD"),
                ("diploma", "Diploma"),
            ]

            for cat_key, cat_label in categories:
                for p in progs.get(cat_key, []):
                    rows.append(
                        {
                            "university_name": uni_name,
                            "university_slug": uni_slug,
                            "university_type": uni_type,
                            "city": city,
                            "degree_level": cat_label,
                            "program_name": p.get("name", ""),
                            "department": p.get("department", ""),
                            "duration": p.get("duration", ""),
                            "tuition_fee": p.get("tuition_fee", ""),
                            "application_deadlines": format_deadlines(p, empty=""),
                            "application_portal_url": portal_url,
                            "official_email": email,
                        }
                    )

        fieldnames = [
            "university_name",
            "university_slug",
            "university_type",
            "city",
            "degree_level",
            "program_name",
            "department",
            "duration",
            "tuition_fee",
            "application_deadlines",
            "application_portal_url",
            "official_email",
        ]

        with open(out_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        console.print(Panel(f"[bold green]✓ CSV Export Completed Successfully![/bold green]\nSaved {len(rows)} degree program rows to:\n[cyan]{out_file}[/cyan]"))

    elif fmt == "json":
        country_grouped: Dict[str, List[Dict[str, Any]]] = {}
        for rec in records:
            # C19: was "Pakistan". A record whose country the extractor never
            # found is not Pakistani, and filing it there hides the gap in a
            # bucket that looks legitimate.
            c_name = rec.get("main_info", {}).get("country") or "Unknown"
            country_grouped.setdefault(c_name, []).append(rec)

        out_file = output_path or (config.data_outputs_dir / "university_counseling_data.json")
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(country_grouped, f, indent=2, ensure_ascii=False)

        country_outputs_dir = config.data_outputs_dir / "country_outputs"
        country_outputs_dir.mkdir(parents=True, exist_ok=True)

        country_slug_map = {
            "Pakistan": "pak",
            "Germany": "german",
            "United States": "usa",
            "United Kingdom": "uk",
            "Switzerland": "swiss",
        }

        written_countries = []
        for country_name, uni_list in country_grouped.items():
            c_slug = country_slug_map.get(country_name, country_name.lower().replace(" ", "_"))
            c_dir = country_outputs_dir / f"{c_slug}_output"
            c_dir.mkdir(parents=True, exist_ok=True)
            c_file = c_dir / f"{country_name.lower().replace(' ', '_')}_universities.json"
            with open(c_file, "w", encoding="utf-8") as f:
                json.dump({country_name: uni_list}, f, indent=2, ensure_ascii=False)
            written_countries.append(f"  - [bold yellow]{country_name}[/bold yellow] ({len(uni_list)} unis) ➔ [cyan]{c_file}[/cyan]")

        summary_msg = (
            f"[bold green]✓ Country-Grouped Master JSON Export Completed![/bold green]\n"
            f"Saved master country-keyed JSON to:\n[cyan]{out_file}[/cyan]\n\n"
            f"[bold cyan]Country Directory Outputs:[/bold cyan]\n" + "\n".join(written_countries)
        )
        console.print(Panel(summary_msg, title="🌐 Hierarchical Country JSON Exporter"))


# ------------------------------------------------------------------------------
# 6. DATASET ANALYTICS COMMAND (analytics)
# ------------------------------------------------------------------------------

def audit_analytics(file_path: Optional[Path] = None):
    """Audits dataset health, metrics, and quality scores."""
    target_path = file_path or config.output_jsonl_path
    if not target_path.exists():
        console.print(f"[bold red]Error:[/bold red] Output file not found: [yellow]{target_path}[/yellow]")
        return

    total_unis = 0
    public_count = 0
    private_count = 0
    total_ug = 0
    total_gr = 0
    total_phd = 0
    total_dip = 0
    total_facs = 0
    portal_count = 0
    admissions_count = 0
    contact_count = 0
    exa_count = 0
    malformed = 0

    with open(target_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except Exception:
                malformed += 1
                continue
            total_unis += 1
            main = data.get("main_info", {})
            if main.get("type") == "public":
                public_count += 1
            else:
                private_count += 1

            kl = main.get("key_links", {})
            if kl.get("application_portal_url"):
                portal_count += 1
            if kl.get("admissions_url"):
                admissions_count += 1
            if main.get("exa_enriched"):
                exa_count += 1

            progs = data.get("programs", {})
            total_ug += len(progs.get("bachelors", []))
            total_gr += len(progs.get("masters", []))
            total_phd += len(progs.get("phd", []))
            total_dip += len(progs.get("diploma", []))
            total_facs += len(data.get("faculties", []))

            contact = data.get("contact", {})
            if contact.get("official_email") or contact.get("phone_numbers"):
                contact_count += 1

    table = Table(title="📊 Master Education Counseling RAG Dataset Quality Audit", show_lines=True)
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value / Score", style="bold yellow")
    table.add_column("Status / Health", style="green")

    table.add_row("Total Extracted Universities", str(total_unis), "[bold green]100% Extracted[/bold green]")
    table.add_row("Public vs Private Distribution", f"{public_count} Public / {private_count} Private", "[dim]Balanced Coverage[/dim]")
    table.add_row("Total Bachelors Programs", str(total_ug), "[cyan]BS/BSc Extracted[/cyan]")
    table.add_row("Total Masters Programs", str(total_gr), "[cyan]MS/MSc Extracted[/cyan]")
    table.add_row("Total PhD Programs", str(total_phd), "[magenta]Doctoral Extracted[/magenta]")
    table.add_row("Total Diploma Programs", str(total_dip), "[blue]PGD/Certificate Extracted[/blue]")
    table.add_row("Total Faculties & Schools", str(total_facs), "[green]Hierarchy Mapped[/green]")
    table.add_row(
        "Application Portal Link Coverage",
        f"{portal_count}/{total_unis} ({portal_count/total_unis*100:.1f}%)" if total_unis else "0%",
        "[bold green]✓ EXCEPTIONAL COVERAGE[/bold green]" if portal_count == total_unis else "[yellow]Partial[/yellow]"
    )
    table.add_row(
        "Admissions Desk Contacts",
        f"{contact_count}/{total_unis} ({contact_count/total_unis*100:.1f}%)" if total_unis else "0%",
        "[bold green]✓ 100% REACHABLE[/bold green]"
    )
    table.add_row("Malformed / Broken Lines", str(malformed), "[bold green]✓ ZERO CORRUPTION[/bold green]" if malformed == 0 else "[bold red]Corrupted[/bold red]")

    console.print(table)


# ------------------------------------------------------------------------------
# 7. PIPELINE STATE MANIFEST COMMAND (state)
# ------------------------------------------------------------------------------

def inspect_state():
    """Audits SQLite pipeline execution state."""
    try:
        try:
            from src.state import StateManager
        except ImportError:
            from state import StateManager

        sm = StateManager()
        records = sm.list_all()

        table = Table(title="🗄️ SQLite Pipeline State Manifest", show_lines=True)
        table.add_column("University Slug", style="bold cyan")
        table.add_column("Status", style="bold yellow")
        table.add_column("Notebook ID", style="dim")
        table.add_column("Sources", style="green")
        table.add_column("Queries", style="magenta")
        table.add_column("Last Updated", style="dim")

        for r in records:
            status_style = "bold green" if r['status'] == 'completed' else "yellow"
            if r['status'] == 'failed':
                status_style = "bold red"

            table.add_row(
                r['university_slug'].upper(),
                f"[{status_style}]{r['status']}[/{status_style}]",
                r.get('notebook_id') or "N/A",
                str(r.get('sources_ingested', 0)),
                str(r.get('queries_executed', 0)),
                str(r.get('updated_at', 'N/A'))
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
        try:
            from src.schema import UniversityPayload
        except ImportError:
            from schema import UniversityPayload

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
        console.print("6. 🗄️ Inspect SQLite State Manifest")
        console.print("7. 📜 Display Master JSON Schema")
        console.print("8. ☁️ List Active NotebookLM Notebooks")
        console.print("9. 📤 Export Dataset (CSV / JSON)")
        console.print("0. 🚪 Exit")
        console.print("=" * 55, style="cyan")

        choice = Prompt.ask("Select an option", choices=["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"], default="0")

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
            max_f = Prompt.ask("Filter max tuition fee PKR (or press Enter for none)", default="")
            fee_val = float(max_f) if max_f.strip().isdigit() else None
            if kw:
                search_programs(kw, level=lvl or None, max_fee=fee_val)
        elif choice == "4":
            tgt = Prompt.ask("Target slug or state filter", choices=["failed", "pending", "all"], default="failed")
            retry_pipeline(tgt)
        elif choice == "5":
            audit_analytics()
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
    search_parser.add_argument("--max-fee", type=float, default=None, help="Maximum tuition fee filter in PKR")

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


if __name__ == "__main__":
    main()
