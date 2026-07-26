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
  2. loggings/notebook_audit.jsonl (machine-readable JSONL stream)
  3. data/state.sqlite (notebook_audit table via StateManager)
"""
import sys
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.config import config
    from src.state import StateManager
except ImportError:
    from config import config
    from state import StateManager

_lock = threading.Lock()


class NotebookLifecycleLogger:
    """Thread-safe multi-destination lifecycle logger for NotebookLM workspaces."""

    def __init__(self):
        config.ensure_directories()
        self.log_file = config.notebook_lifecycle_log_path
        self.jsonl_file = config.notebook_audit_jsonl_path
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

            # 2. Machine-readable JSONL audit file
            try:
                with open(self.jsonl_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            except Exception as e:
                logging.warning(f"Failed to write to audit jsonl: {e}")

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
    ) -> Dict[str, Any]:
        """Log query suite execution event."""
        return self.log_event(
            event_type="QUERY_EXECUTED",
            notebook_id=notebook_id,
            uni_slug=uni_slug,
            details={
                "query_index": query_index,
                "query_key": query_key,
                "prompt_len": prompt_len,
                "response_bytes": response_bytes,
                "duration_sec": round(duration_sec, 3),
            },
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


def log_query_executed(notebook_id: str, query_index: int, query_key: str, prompt_len: int, response_bytes: int, duration_sec: float = 0.0, uni_slug: Optional[str] = None):
    return get_notebook_logger().log_query_executed(notebook_id, query_index, query_key, prompt_len, response_bytes, duration_sec, uni_slug)


def log_json_repaired(notebook_id: str, query_key: str, fix_type: str, uni_slug: Optional[str] = None):
    return get_notebook_logger().log_json_repaired(notebook_id, query_key, fix_type, uni_slug)


def log_notebook_deleted(notebook_id: str, trigger: str = "success_cleanup", uni_slug: Optional[str] = None):
    return get_notebook_logger().log_notebook_deleted(notebook_id, trigger, uni_slug)
