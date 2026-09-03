"""
The export seam: everything that leaves the corpus for somewhere else.

Three destinations: CSV (one row per programme), the country-grouped JSON tree,
and Supabase. Qdrant and Pinecone were removed in C11/C11b and are not coming
back.

The Supabase push is gated on a passing audit (plan section 5): a corpus that
fails inspection must not reach a database something else reads as
authoritative.
"""
import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from rich.panel import Panel
from rich.table import Table

from src.config import config
from src.extractor.normalizers.runner import PROGRAM_BUCKETS
from src.inspector.auditor import audit_records, readiness_verdict
from src.inspector.formatting import console, format_deadlines
from src.inspector.records import iter_all_records, load_all_records
from src.utilities.registry import canonical_domain


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


# ---------------------------------------------------------------- Supabase --
#
# The gate, not the transport. Plan section 5's last rule is "Local DB ->
# Supabase push only AFTER inspector validation passes", and that ordering is
# the part worth being strict about: a corpus that fails the audit must not
# reach a database that something else reads as authoritative.
#
# Table definitions live in resources/supabase_schema.sql. They mirror the
# post-C19 reality that almost every field is legitimately nullable -- the four
# NOT NULLs are the university's domain and name and the programme's name and
# degree_level, which are exactly what the auditor treats as critical.

SUPABASE_TABLES = ("universities", "programs", "faculties", "contacts", "rankings")


class SupabaseNotConfigured(RuntimeError):
    """Raised when a push is attempted without credentials."""


class AuditGateFailed(RuntimeError):
    """Raised when a push is attempted on a corpus that failed the audit."""


def flatten_for_supabase(record: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Turn one nested payload into the flat rows the tables expect.

    Pure: no client, no network, no credentials. That is deliberate -- the
    mapping from payload to rows is the part most likely to be wrong, and it
    should be testable without a database.
    """
    main = record.get("main_info") or {}
    domain = canonical_domain(main.get("website") or "")
    if not domain:
        raise ValueError(f"record for {main.get('name')!r} has no website to key on")

    key_links = main.get("key_links") or {}
    university = {
        "domain": domain,
        "slug": domain.split(".")[0],
        "name": main.get("name"),
        "abbreviation": main.get("abbreviation"),
        "country": main.get("country"),
        "city": main.get("city"),
        "website": main.get("website"),
        "type": main.get("type"),
        "established_year": main.get("established_year"),
        "accreditation_body": main.get("accreditation_body"),
        "primary_instruction_language": main.get("primary_instruction_language"),
        "admission_cycles_offered": main.get("admission_cycles_offered") or [],
        "description": main.get("description"),
        "academics_url": key_links.get("academics_url"),
        "admissions_url": key_links.get("admissions_url"),
        "application_portal_url": key_links.get("application_portal_url"),
        "domain_verified": bool(main.get("domain_verified")),
        "verification_note": main.get("verification_note"),
        "exa_enriched": bool(main.get("exa_enriched")),
        "programs_possibly_truncated": bool(record.get("programs_possibly_truncated")),
    }

    programs: List[Dict[str, Any]] = []
    for bucket in PROGRAM_BUCKETS:
        for prog in (record.get("programs") or {}).get(bucket) or []:
            elig = prog.get("eligibility_requirements") or {}
            programs.append({
                "university_domain": domain,
                "name": prog.get("name"),
                # The bucket, not the field: they agree since C17, and if they
                # ever disagree the auditor blocks the push before this runs.
                "degree_level": bucket,
                "department": prog.get("department"),
                "duration": prog.get("duration"),
                "tuition_fee": prog.get("tuition_fee"),
                "currency": prog.get("currency"),
                "application_fee": prog.get("application_fee"),
                "admission_requirements": prog.get("admission_requirements"),
                "eligibility_min_marks": elig.get("minimum_marks_percentage"),
                "eligibility_entry_tests": elig.get("entry_tests_accepted") or [],
                "eligibility_formula": elig.get("aggregate_formula"),
                "application_deadlines": prog.get("application_deadlines") or [],
                "application_status": prog.get("application_status"),
                "intake_terms": prog.get("intake_terms") or [],
                "delivery_mode": prog.get("delivery_mode"),
                "description": prog.get("description"),
                "career_prospects": prog.get("career_prospects"),
                "scholarships_info": prog.get("scholarships_info"),
                "courses_taught": prog.get("courses_taught") or [],
                "program_info_link": prog.get("program_info_link"),
            })

    faculties = [
        {"university_domain": domain,
         "faculty_name": f.get("faculty_name"),
         "departments": f.get("departments") or []}
        for f in (record.get("faculties") or [])
        if f.get("faculty_name")
    ]

    contact = record.get("contact") or {}
    contacts = [{
        "university_domain": domain,
        "official_email": contact.get("official_email"),
        "phone_numbers": contact.get("phone_numbers") or [],
        "physical_address": contact.get("physical_address"),
    }]

    rankings = [
        {"university_domain": domain, "source": r.get("source"), "scope": r.get("scope"),
         "subject": r.get("subject"), "year": r.get("year"), "rank": r.get("rank"),
         "source_url": r.get("source_url")}
        for r in (main.get("rankings") or [])
        if r.get("source") and r.get("year") and r.get("rank") is not None
    ]

    return {"universities": [university], "programs": programs,
            "faculties": faculties, "contacts": contacts, "rankings": rankings}


def build_sync_plan(records: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Every row that would be written, grouped by table. Writes nothing."""
    plan: Dict[str, List[Dict[str, Any]]] = {t: [] for t in SUPABASE_TABLES}
    for record in records:
        for table, rows in flatten_for_supabase(record).items():
            plan[table].extend(rows)
    return plan


def sync_to_supabase(dry_run: bool = True, force: bool = False) -> Dict[str, Any]:
    """
    Push the local corpus to Supabase, but only if the audit passes.

    `dry_run` builds the full row plan and reports it without contacting
    Supabase; it is the default because the destructive direction should be the
    one you have to ask for.

    `force` skips the audit gate. It exists because a human may have a reason,
    and refusing outright would just get the gate deleted -- but it is a
    deliberate override, it is reported in the result, and it is never the
    default.
    """
    report = audit_records(iter_all_records())
    verdict = readiness_verdict(report)

    if not verdict.ready and not force:
        for reason in verdict.blocking:
            console.print(f"[bold red]✗[/bold red] {reason}")
        raise AuditGateFailed(
            f"{len(verdict.blocking)} blocking issue(s); corpus not pushed. "
            f"Fix them, or pass force=True to override deliberately."
        )

    plan = build_sync_plan(iter_all_records())
    summary = {
        "dry_run": dry_run,
        "audit_passed": verdict.ready,
        "forced": bool(force and not verdict.ready),
        "rows": {table: len(rows) for table, rows in plan.items()},
    }

    if dry_run:
        table = Table(title="🧪 Supabase Sync — Dry Run", show_lines=True)
        table.add_column("Table", style="bold cyan")
        table.add_column("Rows", style="bold yellow", justify="right")
        for name in SUPABASE_TABLES:
            table.add_row(name, str(len(plan[name])))
        console.print(table)
        console.print("[dim]Nothing was written. Pass --no-dry-run to push.[/dim]")
        return summary

    client = _supabase_client()
    for name in SUPABASE_TABLES:
        rows = plan[name]
        if not rows:
            continue
        # Upsert, not insert: re-extracting a university must update its rows
        # rather than duplicate them. The conflict targets match the unique
        # constraints in resources/supabase_schema.sql.
        conflict = {
            "universities": "domain",
            "programs": "university_domain,name,degree_level",
            "faculties": "university_domain,faculty_name",
            "contacts": "university_domain",
            "rankings": "university_domain,source,year,subject",
        }[name]
        client.table(name).upsert(rows, on_conflict=conflict).execute()
        console.print(f"[green]✓[/green] {name}: {len(rows)} rows upserted")

    return summary


def _supabase_client():
    """Construct the client, or explain precisely what is missing."""
    url = getattr(config, "supabase_url", "") or ""
    key = getattr(config, "supabase_service_key", "") or ""
    if not url or not key:
        raise SupabaseNotConfigured(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env. "
            "Apply resources/supabase_schema.sql to the project first."
        )
    try:
        from supabase import create_client
    except ImportError as e:
        raise SupabaseNotConfigured(
            "the `supabase` package is not installed; `pip install supabase`"
        ) from e
    return create_client(url, key)
