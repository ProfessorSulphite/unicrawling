"""
The export seam: everything that leaves the corpus for somewhere else.

Today that is CSV (one row per programme) and the country-grouped JSON tree.
The Supabase destination lands here in C29; Qdrant and Pinecone were removed in
C11/C11b and are not coming back.
"""
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.panel import Panel

from src.config import config
from src.inspector.formatting import console, format_deadlines
from src.inspector.records import load_all_records


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
