"""
Analytics for ONE pipeline run, as opposed to the accumulated dataset.

`analytics` reads the master JSONL and answers "what does the corpus look like?".
That is the wrong question after a run. The corpus is cumulative, so a run that
extracted nothing at all still leaves the dataset looking exactly as healthy as
it did before — every previous university is still in there. The question an
operator actually has at 3am is "what did *this* run do, what did it spend, and
what went wrong", and nothing answered it: the evidence was spread across four
places that had to be joined by hand.

    loggings/{single,complete}_logs/<token>.json   what was targeted, what happened
    loggings/notebook_logs/<notebook>.json         every ask, its status and cost
    data/state.sqlite                              per-university status and spend
    data/outputs/uni_outputs/<slug>.json           the payload that came out

This module does that join. It reads only; it never re-runs anything.

Run-scoped rather than day-scoped on purpose. Two runs on the same day are two
different questions, and the ledger's daily total cannot separate them — the
`s_2` and `s_3` ITU runs both landed on 2026-09-16 and the ledger showed their
sum, which is why the per-run ask count had to be reconstructed from the audit
documents instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import config
from src.logger.notebook_audit import list_notebook_logs, load_notebook_log
from src.logger.pipeline_logger import RunNotFoundError, list_runs, load_run

# Ask outcomes that cost quota and produced nothing. Named here so the summary
# and the detail table cannot disagree about what "wasted" means.
FAILED_STATUSES = ("oversized", "timeout", "parse_failed", "rate_limited", "error")


@dataclass
class UniversityRunFacts:
    """What one university did in one run, joined across the four sources."""
    slug: str
    outcome: str = "unknown"          # from the run manifest
    status: Optional[str] = None      # from state.sqlite
    notebook_id: Optional[str] = None
    asks: List[Dict[str, Any]] = field(default_factory=list)
    programmes: int = 0
    by_level: Dict[str, int] = field(default_factory=dict)
    faculties: int = 0
    failed_blocks: List[str] = field(default_factory=list)
    coverage: Dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None
    # True when a LATER run has since touched this university. The payload and
    # the state row are keyed by slug, not by run, so for an older run they
    # describe the current state of that university rather than the state this
    # run left it in -- ITU's `s_2` reports 0 programmes because `s_3` later
    # overwrote its payload with an empty one. The asks are unaffected: they are
    # joined on the run's own time window. Reporting the two as though they
    # belonged to this run would be a lie the tool cannot detect afterwards.
    superseded_by: Optional[str] = None

    @property
    def asks_ok(self) -> int:
        return sum(1 for a in self.asks if a.get("status") == "ok")

    @property
    def asks_failed(self) -> int:
        return sum(1 for a in self.asks if a.get("status") in FAILED_STATUSES)

    @property
    def seconds_spent(self) -> float:
        return sum(float(a.get("duration_sec") or 0.0) for a in self.asks)

    @property
    def seconds_wasted(self) -> float:
        """Time paid for asks that returned nothing. The number worth acting on."""
        return sum(
            float(a.get("duration_sec") or 0.0)
            for a in self.asks
            if a.get("status") in FAILED_STATUSES
        )


@dataclass
class RunFacts:
    """One run, joined."""
    run_id: str
    kind: str
    status: str
    started_at: Optional[str]
    finished_at: Optional[str]
    error: Optional[str]
    settings: Dict[str, Any]
    universities: List[UniversityRunFacts]

    @property
    def duration_sec(self) -> Optional[float]:
        if not (self.started_at and self.finished_at):
            return None
        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.finished_at)
        except ValueError:
            return None
        return (end - start).total_seconds()

    @property
    def total_asks(self) -> int:
        return sum(len(u.asks) for u in self.universities)

    @property
    def total_failed_asks(self) -> int:
        return sum(u.asks_failed for u in self.universities)

    @property
    def total_programmes(self) -> int:
        return sum(u.programmes for u in self.universities)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def _payload_facts(slug: str) -> Dict[str, Any]:
    """Read what the run actually wrote for one university, if anything."""
    path = config.outputs_uni_outputs_dir / f"{slug}.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    programs = payload.get("programs") or {}
    by_level = {k: len(v or []) for k, v in programs.items()}
    flat = [p for v in programs.values() for p in (v or [])]

    # Coverage is computed here rather than imported from the auditor: the
    # auditor rules on the whole CORPUS and returns a verdict, while this is a
    # per-run field census with no opinion attached.
    from src.utilities.schema import REQUIRED_PROGRAM_FIELDS

    coverage: Dict[str, float] = {}
    if flat:
        for name in REQUIRED_PROGRAM_FIELDS:
            answered = sum(1 for p in flat if _answered(p.get(name)))
            coverage[name] = answered / len(flat)

    return {
        "programmes": len(flat),
        "by_level": by_level,
        "faculties": len(payload.get("faculties") or []),
        "failed_blocks": list(payload.get("failed_query_blocks") or []),
        "coverage": coverage,
    }


def _answered(value: Any) -> bool:
    """Whether a payload field carries a real answer."""
    if value is None:
        return False
    if isinstance(value, (list, tuple, str, dict)):
        if isinstance(value, dict):
            # A nested block counts only if something inside it was answered --
            # `eligibility_requirements` always serialises, empty or not.
            return any(_answered(v) for v in value.values())
        return len(value) > 0
    return True


def _asks_for_run(
    notebook_id: Optional[str],
    slug: str,
    started_at: Optional[str],
    finished_at: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Every ask this university issued during this run's window.

    Joined on SLUG AND TIME WINDOW, never on the notebook id alone.

    The id in `pipeline_state` is whatever the university's *most recent* run
    left there, so restricting to it silently reports zero asks for every
    earlier run -- ITU's `s_2` came back empty while its audit document sat on
    disk, because `s_3` had since overwritten the row. The run manifest's own
    start and finish timestamps are the only boundary that belongs to the run
    being asked about.

    `notebook_id` is still accepted as a hint and used to order ties, but it
    cannot exclude an ask the window includes.
    """
    windows = []
    for nb in list_notebook_logs():
        try:
            doc = load_notebook_log(nb)
        except (json.JSONDecodeError, OSError):
            continue
        for event in doc.get("events") or []:
            if event.get("event_type") != "QUERY_EXECUTED":
                continue
            if event.get("uni_slug") not in (slug, None):
                continue
            at = event.get("at") or event.get("timestamp")
            if started_at and finished_at and at and not (started_at <= at <= finished_at):
                continue
            details = dict(event.get("details") or {})
            details["at"] = at
            details["notebook_id"] = nb
            windows.append(details)

    windows.sort(key=lambda d: (d.get("at") or "", d.get("query_index") or 0))
    return windows


def collect_run_facts(token: Optional[str] = None) -> RunFacts:
    """
    Join one run's manifest, audit documents, state rows and payloads.

    `token` defaults to the most recent run of any kind. Raises RunNotFoundError
    when there is nothing to report, so the caller can say so plainly rather
    than printing an empty table that looks like a healthy run with no work.
    """
    if token is None:
        runs = list_runs()
        if not runs:
            raise RunNotFoundError("no pipeline runs have been recorded yet")
        # `list_runs` orders by id within a kind, so the newest of ALL kinds has
        # to be chosen on the timestamp -- a single run and a batch run number
        # independently, and s_9 may well be older than c_2.
        token = max(runs, key=lambda r: (r.get("started_at") or "", r["run_id"]))["run_id"]

    doc = load_run(token)
    started, finished = doc.get("started_at"), doc.get("finished_at")

    outcomes = {e.get("slug"): e.get("outcome", "unknown") for e in doc.get("events") or []}

    # The manifest carries the full configured list; a university with no event
    # never reached a terminal state, which is itself worth showing.
    slugs: List[str] = []
    for uni in doc.get("universities") or []:
        slug = uni.get("slug")
        if slug and slug not in slugs:
            slugs.append(slug)
    for slug in outcomes:
        if slug and slug not in slugs:
            slugs.append(slug)

    # Which later run, if any, has overwritten each slug's payload and state.
    later_runs = [
        r for r in list_runs()
        if (r.get("started_at") or "") > (started or "") and r["run_id"] != token
    ]
    superseded: Dict[str, str] = {}
    for run in sorted(later_runs, key=lambda r: r.get("started_at") or ""):
        try:
            other = load_run(run["run_id"])
        except (json.JSONDecodeError, OSError, RunNotFoundError):
            continue
        touched = {
            e.get("slug") for e in other.get("events") or []
            if e.get("outcome") in ("processed", "failed")
        }
        for slug in touched:
            superseded.setdefault(slug, run["run_id"])

    state_rows: Dict[str, Any] = {}
    try:
        from src.utilities.state_management import StateManager

        state = StateManager()
        try:
            for slug in slugs:
                row = state.get_state(slug)
                if row:
                    state_rows[slug] = dict(row)
        finally:
            state.close()
    except Exception:
        # A missing or locked state DB must not make the run unreadable; the
        # manifest and the audit documents still answer most of the question.
        state_rows = {}

    universities: List[UniversityRunFacts] = []
    for slug in slugs:
        row = state_rows.get(slug) or {}
        facts = UniversityRunFacts(
            slug=slug,
            outcome=outcomes.get(slug, "no outcome recorded"),
            status=row.get("status"),
            notebook_id=row.get("notebook_id") or None,
            error=row.get("error_log") or None,
            superseded_by=superseded.get(slug),
        )
        facts.asks = _asks_for_run(facts.notebook_id, slug, started, finished)
        for key, value in _payload_facts(slug).items():
            setattr(facts, key, value)
        universities.append(facts)

    return RunFacts(
        run_id=doc.get("run_id", token),
        kind=doc.get("kind", "?"),
        status=doc.get("status", "?"),
        started_at=started,
        finished_at=finished,
        error=doc.get("error"),
        settings=doc.get("settings") or {},
        universities=universities,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_STATUS_STYLE = {
    "ok": "green",
    "oversized": "red",
    "timeout": "red",
    "parse_failed": "yellow",
    "rate_limited": "yellow",
    "error": "red",
}


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def render_run_analytics(token: Optional[str] = None, show_asks: bool = True) -> int:
    """
    Print one run's analytics. Returns a process exit code.

    Non-zero when the run did not fully succeed, so this is usable as a gate in
    a shell script rather than only as something to read.
    """
    from rich.panel import Panel
    from rich.table import Table

    from src.inspector.formatting import console

    try:
        facts = collect_run_facts(token)
    except RunNotFoundError as e:
        console.print(f"[bold red]No run to report:[/bold red] {e}")
        return 1

    status_colour = {"completed": "green", "failed": "red"}.get(facts.status, "yellow")
    console.print(Panel(
        f"[bold cyan]{facts.run_id}[/bold cyan]  ({facts.kind})\n"
        f"Status     : [bold {status_colour}]{facts.status}[/bold {status_colour}]\n"
        f"Started    : {facts.started_at or '?'}\n"
        f"Duration   : {_fmt_duration(facts.duration_sec)}\n"
        f"Universities: {len(facts.universities)}   "
        f"Asks: {facts.total_asks} "
        f"({facts.total_failed_asks} failed)   "
        f"Programmes: {facts.total_programmes}"
        + (f"\n[red]Error      : {facts.error}[/red]" if facts.error else ""),
        title="📊 Pipeline Run Analytics",
    ))

    if facts.settings:
        console.print("[dim]settings: " + "  ".join(
            f"{k}={v}" for k, v in facts.settings.items()) + "[/dim]\n")

    summary = Table(title="Per-University Outcome", header_style="bold magenta")
    for col in ("University", "Outcome", "State", "Asks", "Failed", "Programmes",
                "Faculties", "Wasted"):
        summary.add_column(col)
    for uni in facts.universities:
        state_colour = {
            "completed": "green", "partial": "yellow", "failed": "red",
        }.get(uni.status or "", "white")
        summary.add_row(
            uni.slug,
            uni.outcome,
            f"[{state_colour}]{uni.status or '-'}[/{state_colour}]",
            str(len(uni.asks)),
            f"[red]{uni.asks_failed}[/red]" if uni.asks_failed else "0",
            str(uni.programmes),
            str(uni.faculties),
            _fmt_duration(uni.seconds_wasted) if uni.seconds_wasted else "-",
        )
    console.print(summary)

    stale = [u for u in facts.universities if u.superseded_by]
    if stale:
        console.print(
            "[yellow]Note:[/yellow] the payload, state and coverage figures below are "
            "keyed by university, not by run, so for these they describe the CURRENT "
            "state rather than what this run produced — "
            + ", ".join(f"{u.slug} (since overwritten by {u.superseded_by})" for u in stale)
            + ".\n[dim]The ask tables are unaffected: those are joined on this run's own "
              "time window.[/dim]"
        )

    for uni in facts.universities:
        if uni.failed_blocks:
            console.print(
                f"\n[yellow]⚠ {uni.slug}: query blocks that failed every retry:[/yellow] "
                f"{', '.join(uni.failed_blocks)}"
            )
        if uni.error:
            console.print(f"[dim]  {uni.slug}: {uni.error[:200]}[/dim]")

        if uni.by_level and uni.programmes:
            console.print(
                f"\n[bold]{uni.slug}[/bold] programmes by level: "
                + "  ".join(f"{k}={v}" for k, v in uni.by_level.items())
            )

        if uni.coverage:
            cov = Table(title=f"{uni.slug}: required-field coverage",
                        header_style="bold blue")
            cov.add_column("Field")
            cov.add_column("Answered", justify="right")
            floor = config.audit_min_field_coverage
            for name, ratio in sorted(uni.coverage.items(), key=lambda kv: kv[1]):
                colour = "green" if ratio >= floor else "red"
                cov.add_row(name, f"[{colour}]{ratio:.0%}[/{colour}]")
            console.print(cov)

        if show_asks and uni.asks:
            asks = Table(title=f"{uni.slug}: every ask this run issued",
                         header_style="bold magenta")
            for col in ("#", "Query", "Status", "Bytes", "Time"):
                asks.add_column(col)
            for ask in uni.asks:
                status = str(ask.get("status", "?"))
                colour = _STATUS_STYLE.get(status, "white")
                asks.add_row(
                    str(ask.get("query_index", "?")),
                    str(ask.get("query_key", "?")),
                    f"[{colour}]{status}[/{colour}]",
                    f"{int(ask.get('response_bytes') or 0):,}",
                    f"{float(ask.get('duration_sec') or 0):.1f}s",
                )
            console.print(asks)

            # The line an operator acts on: quota spent for nothing.
            if uni.asks_failed:
                console.print(
                    f"[red]  {uni.asks_failed} of {len(uni.asks)} asks returned nothing, "
                    f"costing {_fmt_duration(uni.seconds_wasted)} and "
                    f"{uni.asks_failed} of the 5-hour budget's "
                    f"{config.budget_5_hours}.[/red]"
                )

    return 0 if facts.status == "completed" and not facts.total_failed_asks else 1


def render_run_list(limit: int = 10) -> int:
    """Print the most recent runs, newest first, so a token can be chosen."""
    from rich.table import Table

    from src.inspector.formatting import console

    runs = list_runs()
    if not runs:
        console.print("[yellow]No pipeline runs have been recorded yet.[/yellow]")
        return 1

    runs.sort(key=lambda r: (r.get("started_at") or "", r["run_id"]), reverse=True)
    table = Table(title="Recent Pipeline Runs", header_style="bold magenta")
    for col in ("Run", "Kind", "Status", "Started", "Universities", "Events"):
        table.add_column(col)
    for run in runs[:limit]:
        colour = {"completed": "green", "failed": "red"}.get(run.get("status"), "yellow")
        table.add_row(
            run["run_id"], run.get("kind", "?"),
            f"[{colour}]{run.get('status', '?')}[/{colour}]",
            (run.get("started_at") or "?")[:19],
            str(run.get("universities", 0)), str(run.get("events", 0)),
        )
    console.print(table)
    return 0
