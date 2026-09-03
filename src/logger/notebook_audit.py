"""
The notebook audit trail, one JSON document per notebook.

Replaces the single append-only loggings/notebook_audit.jsonl, which had two
problems that got worse the longer it ran:

  - it never stopped growing. One file held every event from every notebook of
    every run since 2026-07-24: 19,192 records by the time it was migrated, and
    nothing ever rotated or scoped it.
  - the only way to answer "what happened to this notebook" was to scan all of
    it, because the file's grouping was chronological and the question is never
    chronological.

Format follows pipeline_logger's decision for the same reason (plan section 4):
JSON rather than JSONL, because querying one notebook's history matters more
here than append throughput. Each write rewrites that notebook's file, which is
bounded -- a notebook sees roughly one event per source plus one per query, so
tens, not thousands -- and each rewrite is atomic, so a crash cannot leave a
torn document.

    loggings/notebook_logs/<notebook_id>.json
"""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import config
from src.utilities.json_io import atomic_write_json

# A notebook id reaches this module from the NotebookLM API and from test
# fixtures, and it is used to build a filename. Anything outside this set is
# replaced rather than trusted: an id of "../../etc/passwd" must not address a
# path outside the log directory.
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def notebook_log_path(notebook_id: str) -> Path:
    """The file one notebook's audit trail lives in."""
    safe = _SAFE_ID.sub("_", str(notebook_id or "unknown"))[:120] or "unknown"
    return config.notebook_logs_dir / f"{safe}.json"


def _empty(notebook_id: str) -> Dict[str, Any]:
    return {
        "notebook_id": notebook_id,
        "uni_slug": None,
        "first_seen": None,
        "last_seen": None,
        "event_counts": {},
        "events": [],
    }


def load_notebook_log(notebook_id: str) -> Dict[str, Any]:
    """One notebook's audit document, or an empty one if it has no events yet."""
    path = notebook_log_path(notebook_id)
    if not path.exists():
        return _empty(notebook_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        # A torn document must not make the notebook unauditable from here on.
        return _empty(notebook_id)


def append_event(event: Dict[str, Any]) -> Path:
    """
    Record one audit event against its notebook.

    Takes the same payload shape the legacy JSONL held -- timestamp, event_type,
    notebook_id, uni_slug, details -- so the migration and the live logger write
    identical records.
    """
    notebook_id = event.get("notebook_id") or "unknown"
    path = notebook_log_path(notebook_id)

    with _lock:
        doc = load_notebook_log(notebook_id)
        doc["events"].append(event)
        ts = event.get("timestamp") or _now()
        doc["first_seen"] = doc["first_seen"] or ts
        doc["last_seen"] = ts
        doc["uni_slug"] = event.get("uni_slug") or doc.get("uni_slug")
        et = event.get("event_type") or "UNKNOWN"
        doc["event_counts"][et] = doc["event_counts"].get(et, 0) + 1

        config.notebook_logs_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, doc)
    return path


def list_notebook_logs() -> List[str]:
    """Notebook ids with a log on disk."""
    if not config.notebook_logs_dir.exists():
        return []
    return sorted(p.stem for p in config.notebook_logs_dir.glob("*.json"))


def iter_events(notebook_id: Optional[str] = None):
    """Stream audit events, for one notebook or across all of them."""
    ids = [notebook_id] if notebook_id else list_notebook_logs()
    for nb in ids:
        for event in load_notebook_log(nb).get("events", []):
            yield event
