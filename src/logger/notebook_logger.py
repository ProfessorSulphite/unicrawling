"""
NotebookLM Lifecycle Audit Logger (src/notebook_logger.py)

Captures every stage of a NotebookLM notebook's life cycle:
  - NOTEBOOK_CREATED
  - SOURCE_UPLOADED
  - QUERY_EXECUTED
  - JSON_REPAIRED
  - NOTEBOOK_DELETED

Persists structured events to three destinations:
  1. loggings/notebook_lifecycle.log (human-readable log)
  2. loggings/notebook_logs/<notebook_id>.json (machine-readable, per notebook)
  3. data/state.sqlite (notebook_audit table via StateManager)

Destination 2 was a single append-only notebook_audit.jsonl until C26. One file
held every event from every run since 2026-07-24 and nothing rotated it; see
logger/notebook_audit.py for why it is now scoped per notebook, and
logger/migrate_audit.py for the conversion.
"""
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from src.config import config
from src.logger.notebook_audit import append_event
from src.utilities.state_management import StateManager

_lock = threading.Lock()


class NotebookLifecycleLogger:
    """Thread-safe multi-destination lifecycle logger for NotebookLM workspaces."""

    def __init__(self):
        config.ensure_directories()
        self.log_file = config.notebook_lifecycle_log_path
        self.state_mgr = StateManager()

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def log_event(
        self,
        event_type: str,
        notebook_id: str,
        uni_slug: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Core logging engine writing atomically to log, JSONL, and SQLite."""
        event_type = event_type.upper().strip()
        ts = self._timestamp()
        payload = {
            "timestamp": ts,
            "event_type": event_type,
            "notebook_id": notebook_id,
            "uni_slug": uni_slug,
            "details": details or {},
        }

        with _lock:
            # 1. Human-readable text log
            try:
                msg = f"[{ts}] [{event_type}] [nb:{notebook_id}] [slug:{uni_slug or 'N/A'}] {json.dumps(details or {})}\n"
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(msg)
            except Exception as e:
                logging.warning(f"Failed to write to lifecycle log: {e}")

            # 2. Machine-readable per-notebook JSON audit document
            try:
                append_event(payload)
            except Exception as e:
                logging.warning(f"Failed to write notebook audit log: {e}")

            # 3. SQLite database table
            try:
                self.state_mgr.record_notebook_audit(
                    event_type=event_type,
                    notebook_id=notebook_id,
                    uni_slug=uni_slug,
                    details=details,
                )
            except Exception as e:
                logging.warning(f"Failed to record audit to SQLite: {e}")

        return payload

    def log_notebook_created(
        self,
        notebook_id: str,
        title: str,
        uni_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Log workspace creation event."""
        return self.log_event(
            event_type="NOTEBOOK_CREATED",
            notebook_id=notebook_id,
            uni_slug=uni_slug,
            details={"title": title},
        )

    def log_source_uploaded(
        self,
        notebook_id: str,
        source_id: str,
        url: str,
        status: str = "ready",
        duration_sec: float = 0.0,
        uni_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Log source link upload event."""
        return self.log_event(
            event_type="SOURCE_UPLOADED",
            notebook_id=notebook_id,
            uni_slug=uni_slug,
            details={
                "source_id": source_id,
                "url": url,
                "status": status,
                "duration_sec": round(duration_sec, 3),
            },
        )

    def log_query_executed(
        self,
        notebook_id: str,
        query_index: int,
        query_key: str,
        prompt_len: int,
        response_bytes: int,
        duration_sec: float = 0.0,
        uni_slug: Optional[str] = None,
        status: str = "ok",
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Log one NotebookLM ask, whatever its outcome.

        `status` is what makes this record worth keeping (C32). The caller used
        to log only on the success path, so the audit document recorded 10 asks
        for an ITU run whose ledger charged 14 -- and the four missing ones were
        the *most expensive* asks in the run, the oversized-response failures
        that each burned a full chat timeout. An audit that hides exactly the
        events worth investigating is worse than no audit, because the
        discrepancy reads as a ledger bug.

        Values: ok | oversized | timeout | parse_failed | rate_limited | error.
        """
        details: Dict[str, Any] = {
            "query_index": query_index,
            "query_key": query_key,
            "prompt_len": prompt_len,
            "response_bytes": response_bytes,
            "duration_sec": round(duration_sec, 3),
            "status": status,
        }
        if error:
            # Bounded: a failure message can carry a whole rejected payload, and
            # the audit document is read far more often than it is written.
            details["error"] = error[:500]
        return self.log_event(
            event_type="QUERY_EXECUTED",
            notebook_id=notebook_id,
            uni_slug=uni_slug,
            details=details,
        )

    def log_json_repaired(
        self,
        notebook_id: str,
        query_key: str,
        fix_type: str,
        uni_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Log JSON syntax repair event."""
        return self.log_event(
            event_type="JSON_REPAIRED",
            notebook_id=notebook_id,
            uni_slug=uni_slug,
            details={
                "query_key": query_key,
                "fix_type": fix_type,
            },
        )

    def log_notebook_deleted(
        self,
        notebook_id: str,
        trigger: str = "success_cleanup",
        uni_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Log atomic notebook deletion event."""
        return self.log_event(
            event_type="NOTEBOOK_DELETED",
            notebook_id=notebook_id,
            uni_slug=uni_slug,
            details={
                "trigger": trigger,
                "slot_freed": True,
            },
        )


# Global singleton instance
_logger_instance: Optional[NotebookLifecycleLogger] = None


def get_notebook_logger() -> NotebookLifecycleLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = NotebookLifecycleLogger()
    return _logger_instance


# Convenience functional exports
def log_notebook_created(notebook_id: str, title: str, uni_slug: Optional[str] = None):
    return get_notebook_logger().log_notebook_created(notebook_id, title, uni_slug)


def log_source_uploaded(notebook_id: str, source_id: str, url: str, status: str = "ready", duration_sec: float = 0.0, uni_slug: Optional[str] = None):
    return get_notebook_logger().log_source_uploaded(notebook_id, source_id, url, status, duration_sec, uni_slug)


def log_query_executed(notebook_id: str, query_index: int, query_key: str, prompt_len: int, response_bytes: int, duration_sec: float = 0.0, uni_slug: Optional[str] = None, status: str = "ok", error: Optional[str] = None):
    return get_notebook_logger().log_query_executed(notebook_id, query_index, query_key, prompt_len, response_bytes, duration_sec, uni_slug, status, error)


def log_json_repaired(notebook_id: str, query_key: str, fix_type: str, uni_slug: Optional[str] = None):
    return get_notebook_logger().log_json_repaired(notebook_id, query_key, fix_type, uni_slug)


def log_notebook_deleted(notebook_id: str, trigger: str = "success_cleanup", uni_slug: Optional[str] = None):
    return get_notebook_logger().log_notebook_deleted(notebook_id, trigger, uni_slug)
