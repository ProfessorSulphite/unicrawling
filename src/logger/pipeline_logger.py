"""
Structured JSON run logs for pipeline executions.

Layout (refactoring_plan.md section 4)::

    loggings/single_logs/s_{id}.json      partial / single-university runs
    loggings/complete_logs/c_{id}.json    full batch runs

Format is JSON rather than JSONL by explicit decision (changes_to.txt note 2):
querying and searching a run log matters more here than append throughput. The
cost is that every `record()` rewrites the whole file -- acceptable at the scale
of one file per run, and each rewrite is atomic so a crash cannot leave a torn log.

**Authority model (decision D3, option A).** This log is a *manifest plus audit
trail*, never the arbiter of progress. It answers "what did this run target, under
what settings, and what happened" -- while ``state.sqlite`` remains the single
source of truth for whether a given university is complete. ``--resume s_42``
therefore replays the manifest and lets ``StateManager`` decide what to skip.
Deriving skip decisions from this file instead would create a second progress
record that drifts from the database the moment a run is killed between the
sqlite commit and the log write.
"""
import os
import re
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import config
from src.utilities.json_io import atomic_write_json

__all__ = [
    "RunKind",
    "InvalidRunTokenError",
    "RunNotFoundError",
    "PipelineLogger",
    "parse_run_token",
    "run_log_path",
    "load_run",
    "list_runs",
]

# A run token is exactly "s_<digits>" or "c_<digits>". Anchored so that
# "s_1; DROP", "../../etc", "s_-1" and "S_1" are all rejected rather than
# being coerced into a path.
#
# [0-9] rather than \d on purpose: Python's \d matches any Unicode decimal digit,
# so "s_\u0661\u0662\u0663" (Arabic-Indic) would match and int() would fold it onto 123 --
# two visually distinct tokens silently addressing one log file.
_TOKEN_RE = re.compile(r"^([sc])_([0-9]+)$")

_PREFIX_TO_KIND = {"s": "single", "c": "complete"}
_KIND_TO_PREFIX = {"single": "s", "complete": "c"}

_alloc_lock = threading.Lock()


class RunKind:
    """Namespace for the two run kinds, kept as plain strings for JSON round-tripping."""
    SINGLE = "single"
    COMPLETE = "complete"


class InvalidRunTokenError(ValueError):
    """Raised when a --resume token is not a well-formed s_<n> / c_<n>."""


class RunNotFoundError(FileNotFoundError):
    """Raised when a well-formed token names a log that does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_dir(kind: str) -> Path:
    if kind == RunKind.SINGLE:
        return config.loggings_single_logs_dir
    if kind == RunKind.COMPLETE:
        return config.loggings_complete_logs_dir
    raise ValueError(f"unknown run kind {kind!r}; expected 'single' or 'complete'")


def parse_run_token(token: str) -> tuple:
    """
    Resolve a ``--resume`` token to ``(kind, id)``.

    Rejects anything that is not exactly s_<digits> / c_<digits>, so a token can
    never be used to escape the log directory.
    """
    if not isinstance(token, str):
        raise InvalidRunTokenError(f"run token must be a string, got {type(token).__name__}")
    m = _TOKEN_RE.match(token.strip())
    if not m:
        raise InvalidRunTokenError(
            f"{token!r} is not a valid run token. Expected 's_<n>' for a single run "
            f"or 'c_<n>' for a complete run, e.g. 's_42'."
        )
    return _PREFIX_TO_KIND[m.group(1)], int(m.group(2))


def run_log_path(token: str) -> Path:
    """Absolute path of the log file a token names (whether or not it exists)."""
    kind, run_id = parse_run_token(token)
    return _log_dir(kind) / f"{_KIND_TO_PREFIX[kind]}_{run_id}.json"


def _existing_ids(kind: str) -> List[int]:
    directory = _log_dir(kind)
    if not directory.exists():
        return []
    prefix = _KIND_TO_PREFIX[kind]
    ids = []
    for path in directory.glob(f"{prefix}_*.json"):
        m = _TOKEN_RE.match(path.stem)
        if m:
            ids.append(int(m.group(2)))
    return ids


def _claim_run_id(kind: str) -> tuple:
    """
    Atomically claim the next free id for `kind`.

    Scanning for max+1 and then writing is a check-then-act race: two runs
    starting together both see the same max and both write the same file, and one
    silently overwrites the other. Claiming with O_CREAT|O_EXCL makes the
    filesystem itself arbitrate -- the loser gets FileExistsError and tries the
    next id.
    """
    directory = _log_dir(kind)
    directory.mkdir(parents=True, exist_ok=True)
    prefix = _KIND_TO_PREFIX[kind]

    with _alloc_lock:  # cheap in-process fast path; O_EXCL is the real guarantee
        candidate = max(_existing_ids(kind), default=0) + 1
        for _ in range(10_000):
            path = directory / f"{prefix}_{candidate}.json"
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                candidate += 1
                continue
            os.close(fd)
            return f"{prefix}_{candidate}", path
    raise RuntimeError(f"could not claim a free {kind} run id after 10000 attempts")


class PipelineLogger:
    """
    One instance per pipeline run; owns exactly one log file.

    The id is claimed in ``__init__`` so that a run appears in the log directory
    the moment it starts, not only once it finishes -- an interrupted run must
    still be resumable.
    """

    def __init__(
        self,
        kind: str = RunKind.COMPLETE,
        settings: Optional[Dict[str, Any]] = None,
        universities: Optional[List[Dict[str, Any]]] = None,
    ):
        if kind not in (RunKind.SINGLE, RunKind.COMPLETE):
            raise ValueError(f"unknown run kind {kind!r}; expected 'single' or 'complete'")
        self.run_id, self.path = _claim_run_id(kind)
        self.kind = kind
        self._lock = threading.Lock()
        self._doc: Dict[str, Any] = {
            "run_id": self.run_id,
            "kind": kind,
            "status": "running",
            "started_at": _now(),
            "finished_at": None,
            "error": None,
            # Snapshot of run_settings so a resume reproduces the original run
            # rather than silently picking up whatever the file says today.
            "settings": dict(settings or {}),
            "universities": list(universities or []),
            "events": [],
        }
        self._flush()

    def _flush(self) -> None:
        atomic_write_json(self.path, self._doc)

    def record(self, slug: str, outcome: str, **detail: Any) -> None:
        """
        Append one audit event.

        Audit only: recording "completed" here does NOT make a university
        complete. StateManager owns that (D3 option A).
        """
        with self._lock:
            self._doc["events"].append(
                {"at": _now(), "slug": slug, "outcome": outcome, **detail}
            )
            self._flush()

    def finish(self, status: str = "completed", error: Optional[str] = None) -> Path:
        with self._lock:
            self._doc["status"] = status
            self._doc["error"] = error
            self._doc["finished_at"] = _now()
            self._flush()
        return self.path

    def __enter__(self) -> "PipelineLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # An abandoned run must not stay "running" forever -- resume needs to
        # tell a crashed run from one still in flight.
        if exc_type is not None:
            self.finish("failed", error=f"{exc_type.__name__}: {exc}")
        elif self._doc["status"] == "running":
            self.finish("completed")

    @property
    def document(self) -> Dict[str, Any]:
        """Defensive copy; callers must not mutate the live log."""
        return json.loads(json.dumps(self._doc))


def load_run(token: str) -> Dict[str, Any]:
    """Load the manifest a token names. Raises RunNotFoundError if absent."""
    path = run_log_path(token)
    if not path.exists():
        raise RunNotFoundError(f"no run log at {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_runs(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Summarise runs on disk, newest id first.

    A malformed or half-written log is skipped rather than raising: one corrupt
    file must not make every other run unlistable.
    """
    kinds = [kind] if kind else [RunKind.SINGLE, RunKind.COMPLETE]
    out: List[Dict[str, Any]] = []
    for k in kinds:
        prefix = _KIND_TO_PREFIX[k]
        for run_id in sorted(_existing_ids(k), reverse=True):
            token = f"{prefix}_{run_id}"
            try:
                doc = load_run(token)
            except (json.JSONDecodeError, OSError, RunNotFoundError):
                continue
            if not isinstance(doc, dict):
                continue
            out.append({
                "run_id": token,
                "kind": k,
                "status": doc.get("status"),
                "started_at": doc.get("started_at"),
                "finished_at": doc.get("finished_at"),
                "universities": len(doc.get("universities") or []),
                "events": len(doc.get("events") or []),
            })
    return out
