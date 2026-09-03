"""
One-shot migration of the legacy notebook_audit.jsonl into per-notebook JSON.

    python -m src.logger.migrate_audit           # migrate
    python -m src.logger.migrate_audit --check   # report, write nothing

Idempotent by construction: it rebuilds each notebook's document from the source
file rather than appending to what is already there, so running it twice yields
the same output as running it once. That matters more than it sounds -- a
migration that doubles the audit trail on a second run is worse than one that
never ran, because the damage is silent.

The count gate is the whole point: every record in must be a record out. The
migration refuses to report success otherwise, and never deletes the source.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.config import config
from src.logger.notebook_audit import notebook_log_path
from src.utilities.json_io import atomic_write_json


def read_legacy(path: Optional[Path] = None) -> Tuple[List[Dict[str, Any]], int]:
    """Every record in the legacy JSONL, plus a count of unparseable lines."""
    src = path or config.notebook_audit_jsonl_path
    records: List[Dict[str, Any]] = []
    malformed = 0
    if not src.exists():
        return records, malformed
    with open(src, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1
    return records, malformed


def group_by_notebook(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Fold a flat event stream into one document per notebook."""
    grouped: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "notebook_id": "",
            "uni_slug": None,
            "first_seen": None,
            "last_seen": None,
            "event_counts": {},
            "events": [],
        }
    )
    for rec in records:
        nb = rec.get("notebook_id") or "unknown"
        doc = grouped[nb]
        doc["notebook_id"] = nb
        doc["events"].append(rec)
        ts = rec.get("timestamp")
        if ts:
            doc["first_seen"] = min(doc["first_seen"] or ts, ts)
            doc["last_seen"] = max(doc["last_seen"] or ts, ts)
        doc["uni_slug"] = doc["uni_slug"] or rec.get("uni_slug")
        et = rec.get("event_type") or "UNKNOWN"
        doc["event_counts"][et] = doc["event_counts"].get(et, 0) + 1
    return dict(grouped)


def migrate(check_only: bool = False, source: Optional[Path] = None) -> Dict[str, Any]:
    """
    Convert the legacy stream into per-notebook documents.

    Returns a report. `ok` is True only when every parseable input record landed
    in an output document.
    """
    records, malformed = read_legacy(source)
    grouped = group_by_notebook(records)
    written = 0

    if not check_only:
        config.notebook_logs_dir.mkdir(parents=True, exist_ok=True)
        for nb, doc in grouped.items():
            # Rebuilt, not appended: this is what makes a second run a no-op.
            atomic_write_json(notebook_log_path(nb), doc)
            written += 1

    out_count = sum(len(d["events"]) for d in grouped.values())
    return {
        "source": str(source or config.notebook_audit_jsonl_path),
        "records_in": len(records),
        "records_out": out_count,
        "malformed_lines": malformed,
        "notebooks": len(grouped),
        "files_written": written,
        "ok": out_count == len(records),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report without writing")
    args = parser.parse_args(argv)

    report = migrate(check_only=args.check)
    print(json.dumps(report, indent=2))

    if report["malformed_lines"]:
        print(
            f"warning: {report['malformed_lines']} unparseable lines were skipped "
            f"and are NOT in the output; the source file is left in place.",
            file=sys.stderr,
        )
    if not report["ok"]:
        print("MIGRATION INCOMPLETE: record counts do not match.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
