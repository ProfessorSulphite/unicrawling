"""
Per-programme required-field coverage, and the verdict that gates the push.

Plan section 5 ends with two rules that only mean something together:

    Empty/NaN fields audited and reported by inspector
    Local DB -> Supabase push only AFTER inspector validation passes

C19 made the first one possible. Before it, the normalizer filled every empty
field with a plausible value, so an audit of empty fields would have reported a
corpus in perfect health while 240 of 263 programmes carried an invented
Pakistani admission formula. An auditor is only as honest as the nulls it is
given.

This module supplies the second rule's verdict. It does not perform the push --
that is C29 -- it decides whether one is allowed.

Reads records; writes nothing.
"""
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from rich.panel import Panel
from rich.table import Table

from src.config import config
from src.extractor.normalizers.runner import PROGRAM_BUCKETS
from src.inspector.formatting import console
from src.inspector.records import iter_all_records
from src.utilities.schema import CRITICAL_PROGRAM_FIELDS, REQUIRED_PROGRAM_FIELDS

# Strings the extractor emits when a page said nothing. They are already mapped
# to real nulls for the eligibility block (C19), but they still arrive verbatim
# in free-text fields, and a field holding "N/A" is empty however it reads.
_EMPTY_TEXT = frozenset(
    {
        "",
        "-",
        "--",
        "n/a",
        "na",
        "n.a.",
        "null",
        "none",
        "nan",
        "nil",
        "not available",
        "not specified",
        "not mentioned",
        "not provided",
        "unknown",
        "tbd",
        "tba",
    }
)


def is_missing(value: Any) -> bool:
    """
    True when a field carries no answer, whatever shape the emptiness took.

    Four shapes reach here and all four have to count as missing, because a
    student reading the exported row learns nothing from any of them:

      None                        the field was never answered
      "" / "N/A" / "not specified"  the model answered that it does not know
      [] / {}                     a container that was built and never filled
      {"a": None, "b": []}        a container whose every member is itself missing

    The last one is why this recurses. `eligibility_requirements` always
    serialises as a three-key object, so a programme with no eligibility data
    still ships a dict -- counting that as present would score the single most
    frequently absent field in the corpus at 100%.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str):
        return value.strip().lower() in _EMPTY_TEXT
    if isinstance(value, dict):
        return all(is_missing(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return all(is_missing(v) for v in value)
    return False


@dataclass(frozen=True)
class FieldCoverage:
    """How many programmes answered one required field."""

    name: str
    present: int
    total: int

    @property
    def missing(self) -> int:
        return self.total - self.present

    @property
    def ratio(self) -> float:
        # An empty corpus has no gaps to report. Whether it is shippable is a
        # separate question, and readiness_verdict answers it separately.
        return 1.0 if self.total == 0 else self.present / self.total


@dataclass(frozen=True)
class ProgramGap:
    """One programme and the required fields it failed to answer."""

    university: str
    degree_level: str
    program: str
    missing_fields: Tuple[str, ...]


@dataclass(frozen=True)
class MisfiledProgram:
    """A programme sitting in a bucket its own degree_level contradicts."""

    university: str
    program: str
    bucket: str
    declared_level: str


@dataclass
class CoverageReport:
    universities: int = 0
    programs: int = 0
    by_level: Dict[str, int] = field(default_factory=dict)
    fields: Dict[str, FieldCoverage] = field(default_factory=dict)
    gaps: List[ProgramGap] = field(default_factory=list)
    universities_without_programs: List[str] = field(default_factory=list)
    misfiled: List[MisfiledProgram] = field(default_factory=list)
    duplicated_buckets: List[Tuple[str, str, str]] = field(default_factory=list)

    @property
    def complete_programs(self) -> int:
        """Programmes with every required field answered."""
        return self.programs - len(self.gaps)

    @property
    def overall_ratio(self) -> float:
        """Answered cells over total cells, across the whole required grid."""
        total = sum(f.total for f in self.fields.values())
        present = sum(f.present for f in self.fields.values())
        return 1.0 if total == 0 else present / total

    def worst_fields(self, limit: int = 5) -> List[FieldCoverage]:
        return sorted(self.fields.values(), key=lambda f: (f.ratio, f.name))[:limit]

    def gaps_by_university(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for gap in self.gaps:
            counts[gap.university] = counts.get(gap.university, 0) + 1
        return counts


def audit_program(prog: Dict[str, Any]) -> Tuple[str, ...]:
    """The required fields this one programme failed to answer, in schema order."""
    return tuple(f for f in REQUIRED_PROGRAM_FIELDS if is_missing(prog.get(f)))


def audit_records(records: Iterable[Dict[str, Any]]) -> CoverageReport:
    """
    Walk every programme in every record and count what is answered.

    Takes an iterable so it can stream a corpus that does not fit in memory, and
    so a test can hand it a list.
    """
    report = CoverageReport()
    present_counts = {f: 0 for f in REQUIRED_PROGRAM_FIELDS}
    report.by_level = {bucket: 0 for bucket in PROGRAM_BUCKETS}

    for record in records:
        report.universities += 1
        main = record.get("main_info") or {}
        uni = str(main.get("name") or "Unnamed university")
        programs = record.get("programs") or {}

        found_any = False
        for bucket in PROGRAM_BUCKETS:
            for prog in programs.get(bucket) or []:
                if not isinstance(prog, dict):
                    continue
                found_any = True
                report.programs += 1
                report.by_level[bucket] += 1

                # A programme whose own degree_level contradicts the bucket it
                # sits in was filed by something other than what it says it is.
                declared = str(prog.get("degree_level") or "").strip().lower()
                if declared and declared != bucket:
                    report.misfiled.append(
                        MisfiledProgram(
                            university=uni,
                            program=str(prog.get("name") or "Unnamed programme"),
                            bucket=bucket,
                            declared_level=declared,
                        )
                    )

                missing = audit_program(prog)
                for name in REQUIRED_PROGRAM_FIELDS:
                    if name not in missing:
                        present_counts[name] += 1
                if missing:
                    report.gaps.append(
                        ProgramGap(
                            university=uni,
                            degree_level=bucket,
                            program=str(prog.get("name") or "Unnamed programme"),
                            missing_fields=missing,
                        )
                    )

        if not found_any:
            report.universities_without_programs.append(uni)

        # Two buckets holding the same programmes means one query received
        # another's answer. Detected at extraction since the 2026-09-03 ITU run,
        # but a corpus already on disk predates that guard, so it is checked on
        # read as well -- this is the check that would have reported the ITU
        # payload without anyone reading the JSON by hand.
        filled = [(b, programs.get(b) or []) for b in PROGRAM_BUCKETS]
        for i, (bucket_a, items_a) in enumerate(filled):
            for bucket_b, items_b in filled[i + 1:]:
                if items_a and items_b and items_a == items_b:
                    report.duplicated_buckets.append((uni, bucket_a, bucket_b))

    report.fields = {
        name: FieldCoverage(name=name, present=present_counts[name], total=report.programs)
        for name in REQUIRED_PROGRAM_FIELDS
    }
    return report


# ------------------------------------------------------------ the verdict --

@dataclass(frozen=True)
class ReadinessVerdict:
    """Whether this corpus may be pushed, and what stands in the way."""

    ready: bool
    blocking: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()


def readiness_verdict(
    report: CoverageReport,
    min_field_coverage: Optional[float] = None,
    min_overall_coverage: Optional[float] = None,
) -> ReadinessVerdict:
    """
    A hard yes or no on pushing this corpus onward.

    Deliberately not a score. A number invites someone to ship at 0.62 "for now";
    a verdict makes shipping a gappy corpus an explicit override rather than a
    rounding decision. The thresholds are Config fields so tuning them is a
    recorded change rather than an edit here.
    """
    min_field = config.audit_min_field_coverage if min_field_coverage is None else min_field_coverage
    min_overall = (
        config.audit_min_overall_coverage if min_overall_coverage is None else min_overall_coverage
    )

    blocking: List[str] = []
    warnings: List[str] = []

    if report.programs == 0:
        blocking.append(
            "No programmes found. Programmes are the primary data target, so a "
            "corpus without them has nothing to push."
        )
        return ReadinessVerdict(ready=False, blocking=tuple(blocking))

    # Critical fields are all-or-nothing: a row with no name or no level is not
    # a programme, and no threshold makes it one.
    for name in CRITICAL_PROGRAM_FIELDS:
        coverage = report.fields.get(name)
        if coverage and coverage.missing:
            blocking.append(
                f"{coverage.missing} of {coverage.total} programmes are missing "
                f"'{name}', which every row must carry."
            )

    for coverage in report.fields.values():
        if coverage.name in CRITICAL_PROGRAM_FIELDS:
            continue
        if coverage.ratio < min_field:
            blocking.append(
                f"'{coverage.name}' is answered for {coverage.ratio:.0%} of programmes, "
                f"below the {min_field:.0%} floor ({coverage.missing} missing)."
            )

    # Structural corruption, not a coverage shortfall: no threshold applies.
    for uni, bucket_a, bucket_b in report.duplicated_buckets:
        blocking.append(
            f"{uni}: the '{bucket_a}' and '{bucket_b}' buckets hold identical "
            f"programmes -- one query received the other's answer, so one of the "
            f"two degree levels is wrong. Re-extract this university."
        )

    if report.misfiled:
        shown = ", ".join(
            f"{m.program!r} ({m.declared_level} filed under {m.bucket})"
            for m in report.misfiled[:3]
        )
        blocking.append(
            f"{len(report.misfiled)} programmes sit in a bucket their own "
            f"degree_level contradicts: {shown}."
        )

    if report.overall_ratio < min_overall:
        blocking.append(
            f"Overall required-field coverage is {report.overall_ratio:.0%}, "
            f"below the {min_overall:.0%} floor."
        )

    if report.universities_without_programs:
        names = ", ".join(sorted(report.universities_without_programs)[:5])
        warnings.append(
            f"{len(report.universities_without_programs)} universities produced no "
            f"programmes at all ({names}). Their extraction likely failed rather "
            f"than the universities having no programmes."
        )

    empty_levels = [level for level, count in report.by_level.items() if count == 0]
    if empty_levels:
        warnings.append(
            f"No programmes at all in: {', '.join(empty_levels)}. If a query in the "
            f"suite is failing, this is where it shows."
        )

    return ReadinessVerdict(
        ready=not blocking, blocking=tuple(blocking), warnings=tuple(warnings)
    )


# ------------------------------------------------------------- rendering --

def render_audit(report: CoverageReport, verdict: ReadinessVerdict) -> None:
    """Print the coverage grid, the level distribution and the verdict."""
    summary = Table(title="🔬 Per-Programme Required-Field Audit", show_lines=True)
    summary.add_column("Required Field", style="bold cyan")
    summary.add_column("Answered", style="bold yellow", justify="right")
    summary.add_column("Missing", style="bold red", justify="right")
    summary.add_column("Coverage", style="green", justify="right")
    summary.add_column("", style="dim")

    for name in REQUIRED_PROGRAM_FIELDS:
        coverage = report.fields[name]
        critical = name in CRITICAL_PROGRAM_FIELDS
        if critical:
            mark = "[bold green]required[/bold green]" if not coverage.missing else "[bold red]REQUIRED[/bold red]"
        elif coverage.ratio >= config.audit_min_field_coverage:
            mark = "[green]ok[/green]"
        else:
            mark = "[bold red]below floor[/bold red]"
        summary.add_row(
            name,
            str(coverage.present),
            str(coverage.missing),
            f"{coverage.ratio:.0%}",
            mark,
        )
    console.print(summary)

    levels = Table(title="🎓 Degree-Level Distribution", show_lines=True)
    levels.add_column("Level", style="bold cyan")
    levels.add_column("Programmes", style="bold yellow", justify="right")
    levels.add_column("Share", style="green", justify="right")
    for level, count in report.by_level.items():
        share = f"{count / report.programs:.0%}" if report.programs else "-"
        levels.add_row(level, str(count), share)
    levels.add_row("[bold]total[/bold]", f"[bold]{report.programs}[/bold]", "")
    console.print(levels)

    console.print(
        f"\n[bold]{report.universities}[/bold] universities · "
        f"[bold]{report.programs}[/bold] programmes · "
        f"[bold]{report.complete_programs}[/bold] with every required field · "
        f"overall coverage [bold]{report.overall_ratio:.0%}[/bold]"
    )

    worst = report.gaps_by_university()
    if worst:
        offenders = Table(title="📉 Programmes With At Least One Unanswered Field", show_lines=True)
        offenders.add_column("University", style="bold cyan")
        offenders.add_column("Gappy Programmes", style="bold red", justify="right")
        for uni, count in sorted(worst.items(), key=lambda kv: -kv[1])[:15]:
            offenders.add_row(uni, str(count))
        console.print(offenders)

    if report.misfiled:
        misfiled = Table(title="🚩 Programmes Filed Under the Wrong Degree Level", show_lines=True)
        misfiled.add_column("University", style="bold cyan")
        misfiled.add_column("Programme", style="bold yellow")
        misfiled.add_column("Filed under", style="bold red")
        misfiled.add_column("Says it is", style="green")
        for m in report.misfiled[:15]:
            misfiled.add_row(m.university, m.program, m.bucket, m.declared_level)
        console.print(misfiled)

    for note in verdict.warnings:
        console.print(f"[yellow]⚠ {note}[/yellow]")

    if verdict.ready:
        console.print(
            Panel(
                "[bold green]✓ READY[/bold green]\n"
                "Required-field coverage clears every floor. This corpus may be pushed.",
                title="🚦 Supabase Readiness",
                expand=False,
            )
        )
    else:
        reasons = "\n".join(f"  • {r}" for r in verdict.blocking)
        console.print(
            Panel(
                f"[bold red]✗ NOT READY — DO NOT PUSH[/bold red]\n{reasons}",
                title="🚦 Supabase Readiness",
                expand=False,
            )
        )


def audit_corpus() -> ReadinessVerdict:
    """Audit everything on disk and print the report. Returns the verdict."""
    report = audit_records(iter_all_records())
    verdict = readiness_verdict(report)
    render_audit(report, verdict)
    return verdict
