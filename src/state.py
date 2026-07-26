"""
SQLite state machine for tracking Education Counselor pipeline execution states.

Three responsibilities:
  1. pipeline_state -- per-university resumable status, so a crashed 83-university
     batch restarts from where it stopped instead of re-ingesting everything.
  2. source_map     -- url -> source_id -> tier, so Phase 3 can scope each query to
     the sources that can actually answer it (chat.ask(source_ids=...)).
  3. query_ledger   -- an append-only count of NotebookLM queries per UTC day, so
     the 500/day Pro ceiling is enforced by the pipeline rather than discovered
     when the API starts refusing halfway through a run.
"""
import sys
import json
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Iterable, Tuple

logger = logging.getLogger("State")

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.config import config
except ImportError:
    from config import config


# Ordered lifecycle. Index in this tuple defines forward progress.
STATUS_SEQUENCE = ("pending", "crawled", "ingested", "extracted", "completed")
TERMINAL_FAILURE = "failed"
VALID_STATUSES = set(STATUS_SEQUENCE) | {TERMINAL_FAILURE}


class InvalidStatusError(ValueError):
    """Raised when a caller attempts a status outside the declared lifecycle."""


class QuotaExceededError(RuntimeError):
    """Raised when the daily NotebookLM query budget would be exceeded."""


class StateManager:
    """Manages pipeline execution state per university in SQLite database."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or config.state_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    # ------------------------------------------------------------ connection --

    def _connect(self) -> sqlite3.Connection:
        """Open one tuned connection. Pragmas are set once, outside any transaction."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL keeps readers from blocking the writer; NORMAL trades an fsync per
        # commit for one per checkpoint, which is the right durability point for a
        # resumable ledger whose worst case is re-running one university.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """
        Return this thread's persistent connection, opening it on first use.

        Every method previously opened and closed its own connection, so a batch
        run paid a file open, WAL header read, and pragma round-trip per status
        write. The connection is per-thread, so no handle is ever shared across
        threads; callers keep using `with self._get_connection() as conn:`, which
        on a sqlite3.Connection scopes a *transaction* and does not close it.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close this thread's connection, if one is open."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error as e:
                logger.debug(f"Ignoring error while closing state connection: {e}")
            self._local.conn = None

    def __enter__(self) -> "StateManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _init_db(self) -> None:
        """Initialize tables and indexes if they do not exist."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_state (
                    university_slug TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    notebook_id TEXT,
                    sources_ingested INTEGER DEFAULT 0,
                    queries_executed INTEGER DEFAULT 0,
                    error_log TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS source_map (
                    university_slug TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    PRIMARY KEY (university_slug, source_id)
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_source_map_tier
                ON source_map (university_slug, tier);
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS query_ledger (
                    day TEXT NOT NULL,
                    university_slug TEXT NOT NULL,
                    queries INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, university_slug)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS notebook_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    notebook_id TEXT NOT NULL,
                    uni_slug TEXT,
                    details_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_notebook_audit_id
                ON notebook_audit (notebook_id);
            """)

            # Indexes for the three hot read paths. Without them every resumed
            # batch full-scanned pipeline_state to answer get_completed_slugs(),
            # and the quota check full-scanned query_ledger before every suite.
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_pipeline_state_status
                ON pipeline_state (status);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_pipeline_state_updated_at
                ON pipeline_state (updated_at DESC);
            """)
            # Covering index: queries_used_today() sums `queries` for one day and
            # never touches the row, so the scan stays inside the index.
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_query_ledger_day
                ON query_ledger (day, queries);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_notebook_audit_slug
                ON notebook_audit (uni_slug);
            """)
            conn.commit()

    # ------------------------------------------------------------------ state --

    @staticmethod
    def _validate_status(status: str) -> str:
        clean = status.lower().strip()
        if clean not in VALID_STATUSES:
            raise InvalidStatusError(
                f"'{status}' is not a valid pipeline status. Expected one of {sorted(VALID_STATUSES)}."
            )
        return clean

    def get_status(self, slug: str) -> Optional[str]:
        """Retrieve current execution status for given university slug."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT status FROM pipeline_state WHERE university_slug = ?",
                (slug.lower().strip(),),
            ).fetchone()
            return row["status"] if row else None

    def get_completed_slugs(self) -> List[str]:
        """Retrieve list of university slugs that completed processing successfully."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT university_slug FROM pipeline_state WHERE status = 'completed'"
            ).fetchall()
            return [r["university_slug"] for r in rows]

    def get_state(self, slug: str) -> Optional[Dict[str, Any]]:
        """Retrieve full state dictionary for given university slug."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM pipeline_state WHERE university_slug = ?",
                (slug.lower().strip(),),
            ).fetchone()
            return dict(row) if row else None

    def set_status(
        self,
        slug: str,
        status: str,
        notebook_id: Optional[str] = None,
        sources_ingested: Optional[int] = None,
        queries_executed: Optional[int] = None,
        error_log: Optional[str] = None,
    ) -> None:
        """
        Upsert pipeline state, preserving any field the caller left as None.

        Rejects statuses outside the declared lifecycle. VALID_STATUSES was
        previously declared and then never referenced, so a typo like "ingesting"
        was written silently and the row became unresumable.
        """
        clean_slug = slug.lower().strip()
        clean_status = self._validate_status(status)

        with self._get_connection() as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT notebook_id, sources_ingested, queries_executed, error_log "
                "FROM pipeline_state WHERE university_slug = ?",
                (clean_slug,),
            ).fetchone()

            if row:
                cur.execute(
                    """
                    UPDATE pipeline_state
                       SET status = ?, notebook_id = ?, sources_ingested = ?,
                           queries_executed = ?, error_log = ?, updated_at = CURRENT_TIMESTAMP
                     WHERE university_slug = ?
                    """,
                    (
                        clean_status,
                        notebook_id if notebook_id is not None else row["notebook_id"],
                        sources_ingested if sources_ingested is not None else row["sources_ingested"],
                        queries_executed if queries_executed is not None else row["queries_executed"],
                        # Clearing the error is explicit: advancing past 'failed'
                        # on a retry must not leave a stale error attached.
                        error_log if error_log is not None else (
                            None if clean_status != TERMINAL_FAILURE else row["error_log"]
                        ),
                        clean_slug,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO pipeline_state
                        (university_slug, status, notebook_id, sources_ingested,
                         queries_executed, error_log, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        clean_slug, clean_status, notebook_id,
                        sources_ingested or 0, queries_executed or 0, error_log,
                    ),
                )
            conn.commit()

    def is_complete(self, slug: str) -> bool:
        """True when this university needs no further work in a resumed run."""
        return self.get_status(slug) == "completed"

    def list_all(self) -> List[Dict[str, Any]]:
        """Retrieve all pipeline state records sorted by latest update time."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM pipeline_state ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def reset_state(self, slug: Optional[str] = None) -> None:
        """Reset one university, or everything when slug is None or 'all'."""
        with self._get_connection() as conn:
            cur = conn.cursor()
            if slug and slug.lower().strip() != "all":
                target = slug.lower().strip()
                cur.execute("DELETE FROM pipeline_state WHERE university_slug = ?", (target,))
                cur.execute("DELETE FROM source_map WHERE university_slug = ?", (target,))
            else:
                cur.execute("DELETE FROM pipeline_state")
                cur.execute("DELETE FROM source_map")
            conn.commit()

    # ------------------------------------------------------------ source map --

    def record_sources(self, slug: str, entries: Iterable[Tuple[str, str, int]]) -> int:
        """
        Persist (source_id, url, tier) triples for a university.

        Phase 3 needs the tier of every ingested source to scope its queries. The
        previous ingest returned only a count, so that mapping was destroyed the
        moment ingestion finished and every query had to run against all sources.
        """
        clean_slug = slug.lower().strip()
        rows = [(clean_slug, sid, url, int(tier)) for sid, url, tier in entries]
        if not rows:
            return 0
        with self._get_connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO source_map (university_slug, source_id, url, tier) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        return len(rows)

    def clear_sources(self, slug: str) -> int:
        """
        Drop a university's recorded sources without touching its pipeline state.

        Must be called before re-recording on a re-ingest: the previous run's
        notebook has been deleted, so its source_ids are dead server-side. Left
        in place they would accumulate and be handed to chat.ask(source_ids=...)
        as if they were live, scoping Phase 3 against sources that no longer
        exist.
        """
        with self._get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM source_map WHERE university_slug = ?",
                (slug.lower().strip(),),
            )
            conn.commit()
            return cur.rowcount or 0

    def get_source_ids(self, slug: str, tiers: Optional[Iterable[int]] = None) -> List[str]:
        """Source ids for a university, optionally restricted to given tiers."""
        clean_slug = slug.lower().strip()
        with self._get_connection() as conn:
            if tiers is None:
                rows = conn.execute(
                    "SELECT source_id FROM source_map WHERE university_slug = ?",
                    (clean_slug,),
                ).fetchall()
            else:
                tier_list = list(tiers)
                if not tier_list:
                    return []
                placeholders = ",".join("?" * len(tier_list))
                rows = conn.execute(
                    f"SELECT source_id FROM source_map WHERE university_slug = ? "
                    f"AND tier IN ({placeholders})",
                    (clean_slug, *tier_list),
                ).fetchall()
            return [r["source_id"] for r in rows]

    def count_sources_by_tier(self, slug: str) -> Dict[int, int]:
        """Tier histogram of ingested sources; drives programs_possibly_truncated."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT tier, COUNT(*) AS n FROM source_map WHERE university_slug = ? GROUP BY tier",
                (slug.lower().strip(),),
            ).fetchall()
            return {int(r["tier"]): int(r["n"]) for r in rows}

    # --------------------------------------------------------- quota ledger --

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def queries_used_today(self) -> int:
        """Total NotebookLM queries recorded for the current UTC day."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(queries), 0) AS n FROM query_ledger WHERE day = ?",
                (self._today(),),
            ).fetchone()
            return int(row["n"])

    def remaining_query_budget(self) -> int:
        """Queries still available today against config.daily_query_budget."""
        return max(0, config.daily_query_budget - self.queries_used_today())

    def reserve_queries(self, slug: str, count: int) -> None:
        """
        Record `count` queries against today's budget, refusing to overrun it.

        Called before issuing the query suite. Without this the 5-query suite over
        83 universities silently walks into the 500/day wall mid-run, leaving a
        notebook ingested but never extracted.
        """
        if count <= 0:
            return
        remaining = self.remaining_query_budget()
        if count > remaining:
            raise QuotaExceededError(
                f"Daily NotebookLM query budget exhausted: requested {count}, "
                f"{remaining} of {config.daily_query_budget} remaining today."
            )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO query_ledger (day, university_slug, queries) VALUES (?, ?, ?)
                ON CONFLICT(day, university_slug)
                DO UPDATE SET queries = queries + excluded.queries
                """,
                (self._today(), slug.lower().strip(), int(count)),
            )
            conn.commit()

    # ----------------------------------------------------- notebook audit log --

    def record_notebook_audit(
        self,
        event_type: str,
        notebook_id: str,
        uni_slug: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a structured NotebookLM lifecycle event into SQLite."""
        details_str = json.dumps(details or {}, ensure_ascii=False)
        clean_slug = uni_slug.lower().strip() if uni_slug else None
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO notebook_audit (event_type, notebook_id, uni_slug, details_json)
                VALUES (?, ?, ?, ?)
                """,
                (event_type.upper().strip(), notebook_id, clean_slug, details_str),
            )
            conn.commit()

    def list_notebook_audits(self, notebook_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve audit log records sorted by latest timestamp."""
        with self._get_connection() as conn:
            if notebook_id:
                rows = conn.execute(
                    "SELECT * FROM notebook_audit WHERE notebook_id = ? ORDER BY id DESC LIMIT ?",
                    (notebook_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM notebook_audit ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                try:
                    item["details"] = json.loads(item["details_json"])
                except Exception:
                    item["details"] = {}
                results.append(item)
            return results
