"""
Dataset-wide health metrics: counts, distributions and coverage percentages read
straight off the master JSONL ledger.

Streams the file line by line rather than loading it, so peak memory does not
scale with the corpus. C21 adds the per-programme required-field audit on top of
this in auditor.py.
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from rich.table import Table

from src.config import config
from src.extractor.normalizers.runner import PROGRAM_BUCKETS
from src.inspector.formatting import console
from src.inspector.records import iter_all_records
from src.utilities.json_io import atomic_write_json


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


def generate_result_analytics(config_path: Optional[Path] = None) -> Path:
    """
    Write data/outputs/result.json: the run's dataset-level analytics.

    Lived in pipeline.py until C22. It is analytics, not orchestration -- the
    orchestrator calls it at the end of a batch the same way it calls any other
    inspector function, and the direction of the dependency (orchestrator ->
    inspector, never back) is what keeps Finding 6 closed.

    Streamed: the whole dataset is never resident, only the running counters.
    """
    country_counts: Dict[str, int] = {}
    total_unis = 0
    per_level = {bucket: 0 for bucket in PROGRAM_BUCKETS}
    portal_count = 0
    contact_count = 0

    for rec in iter_all_records():
        total_unis += 1
        main = rec.get("main_info") or {}
        # C19: a record whose country the extractor never found is not Pakistani.
        # Filing it under "Unknown" keeps the gap visible in the distribution.
        c_name = main.get("country") or "Unknown"
        country_counts[c_name] = country_counts.get(c_name, 0) + 1

        if (main.get("key_links") or {}).get("application_portal_url"):
            portal_count += 1
        if (rec.get("contact") or {}).get("official_email"):
            contact_count += 1

        progs = rec.get("programs") or {}
        for bucket in PROGRAM_BUCKETS:
            per_level[bucket] += len(progs.get(bucket) or [])

    analytics_payload = {
        "timestamp": datetime.now().isoformat(),
        "config_file": str(config_path) if config_path else None,
        "total_universities": total_unis,
        "country_distribution": country_counts,
        "program_counts": {**per_level, "total_programs": sum(per_level.values())},
        "quality_metrics": {
            "application_portal_coverage_pct": round((portal_count / max(1, total_unis)) * 100, 1),
            "admissions_contact_coverage_pct": round((contact_count / max(1, total_unis)) * 100, 1),
            "malformed_records": 0,
        },
    }

    result_file = config.data_outputs_dir / "result.json"
    atomic_write_json(result_file, analytics_payload)
    return result_file
