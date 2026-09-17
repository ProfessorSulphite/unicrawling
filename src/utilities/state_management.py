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
import json
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any, Iterable, Tuple

logger = logging.getLogger("State")

from src.config import config


# Ordered lifecycle. Index in this tuple defines forward progress.
STATUS_SEQUENCE = ("pending", "crawled", "ingested", "extracted", "completed")
TERMINAL_FAILURE = "failed"
# A payload was written, but at least one query block failed every retry, so a
# degree bucket in it is empty for a reason that has nothing to do with the
# university. Deliberately NOT in STATUS_SEQUENCE and deliberately not
# "completed": a partial university has real data worth keeping and real data
# still missing, so it must survive a rerun as work outstanding.
PARTIAL_EXTRACTION = "partial"
# The quality gate's outcome: the extraction ran but produced nothing usable
# (target blocked NotebookLM's crawler, or the answer was empty). Distinct from
# TERMINAL_FAILURE because the pipeline itself did not error -- it succeeded in
# establishing that the source was unusable.
BLOCKED = "blocked"
VALID_STATUSES = set(STATUS_SEQUENCE) | {TERMINAL_FAILURE, PARTIAL_EXTRACTION, BLOCKED}


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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS query_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    university_slug TEXT NOT NULL,
                    queries INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_query_events_created_at
                ON query_events (created_at);
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
        """
        Slugs a resumed run may skip: fully extracted, nothing outstanding.

        'partial' is excluded on purpose. Those universities have a payload, but
        a query block in it failed every retry, and skipping them meant a
        transient API failure permanently cost a degree level -- the next batch
        saw "completed" and never asked again.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT university_slug FROM pipeline_state WHERE status = 'completed'"
            ).fetchall()
            return [r["university_slug"] for r in rows]

    def get_partial_slugs(self) -> List[str]:
        """Slugs whose payload is real but incomplete, so a rerun should retry them."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT university_slug FROM pipeline_state WHERE status = ?",
                (PARTIAL_EXTRACTION,),
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
                            None
                            if clean_status not in (TERMINAL_FAILURE, PARTIAL_EXTRACTION)
                            else row["error_log"]
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

    def source_ids_by_tier(self, slug: str) -> Optional[Dict[int, List[str]]]:
        """
        {tier: [source_id, ...]} so each Phase 3 query is scoped to the sources
        that can answer it.

        Returns None -- not an empty dict -- when nothing was recorded, because
        the query suite reads None as "search every source" and {} as "search
        nothing". A university whose source map was never written would otherwise
        be queried against no sources at all and come back empty.
        """
        by_tier: Dict[int, List[str]] = {}
        for tier in (1, 2, 3, 4):
            ids = self.get_source_ids(slug, tiers=[tier])
            if ids:
                by_tier[tier] = ids
        return by_tier or None

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

    def queries_used_in_window(self, hours: float = 5.0) -> int:
        """
        Total NotebookLM queries recorded in the rolling sliding window.

        A SIGNED sum since C32: `release_queries` writes a compensating negative
        row rather than mutating the original, so unspent reservations leave the
        window as they should.

        Floored at zero because the two rows expire independently. A charge made
        just outside the window and refunded just inside it leaves only the
        refund in range, and a negative "used" count would report more budget
        available than the quota holds -- turning an accounting artifact into a
        real over-spend against NotebookLM.
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(queries), 0) AS n FROM query_events "
                "WHERE created_at >= datetime('now', '-' || ? || ' hours')",
                (float(hours),),
            ).fetchone()
            return max(0, int(row["n"]))

    def remaining_5h_budget(self) -> int:
        """Queries remaining in the 5-hour rolling window against config.budget_5_hours."""
        budget_5h = getattr(config, "budget_5_hours", 75)
        return max(0, budget_5h - self.queries_used_in_window(5.0))

    def seconds_until_5h_budget_available(self, required: int = 5) -> float:
        """Calculates seconds until `required` queries become available in the 5-hour rolling window."""
        rem = self.remaining_5h_budget()
        if rem >= required:
            return 0.0

        needed = required - rem
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT queries, created_at FROM query_events "
                "WHERE created_at >= datetime('now', '-5 hours') "
                "ORDER BY created_at ASC"
            ).fetchall()

        freed = 0
        now = datetime.now(timezone.utc)
        for r in rows:
            # Only a real charge frees capacity when it ages out. A refund row
            # (negative, C32) does the opposite on expiry, so counting it here
            # would predict capacity that never arrives and busy-wait on it.
            queries = int(r["queries"])
            if queries <= 0:
                continue
            freed += queries
            if freed >= needed:
                created_str = str(r["created_at"])
                try:
                    created_dt = datetime.fromisoformat(created_str)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    created_dt = now
                expires_at = created_dt + timedelta(hours=5.0)
                diff = (expires_at - now).total_seconds()
                return max(1.0, diff + 1.0)

        return 300.0

    def remaining_query_budget(self) -> int:
        """Queries still available today against config.daily_query_budget."""
        return max(0, config.daily_query_budget - self.queries_used_today())

    def reserve_queries(self, slug: str, count: int) -> None:
        """
        Record `count` queries against both the daily and rolling 5-hour budgets.

        Called before issuing the query suite. Without this the 5-to-6 query suite over
        universities silently walks into daily and 5-hour rate limits mid-run.
        """
        if count <= 0:
            return
        remaining_daily = self.remaining_query_budget()
        if count > remaining_daily:
            raise QuotaExceededError(
                f"Daily NotebookLM query budget exhausted: requested {count}, "
                f"{remaining_daily} of {config.daily_query_budget} remaining today."
            )
        remaining_5h = self.remaining_5h_budget()
        budget_5h = getattr(config, "budget_5_hours", 75)
        if count > remaining_5h:
            raise QuotaExceededError(
                f"5-Hour Rolling NotebookLM budget exhausted: requested {count}, "
                f"{remaining_5h} of {budget_5h} remaining in current 5-hour window."
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
            conn.execute(
                """
                INSERT INTO query_events (university_slug, queries) VALUES (?, ?)
                """,
                (slug.lower().strip(), int(count)),
            )
            conn.commit()

    def release_queries(self, slug: str, count: int) -> None:
        """
        Hand back queries reserved but never issued, floored at zero.

        Refunds BOTH budgets (C32). `query_ledger` is mutable and was always
        credited here, but `query_events` -- which backs the rolling 5-hour
        window -- is append-only, so the refund never reached it. A university
        that reserved 6 and spent 2 kept all 6 charged against the 5-hour window
        for a full five hours, and the 5-hour window is the TIGHTER of the two
        ceilings (75 vs 500): it is what actually limits batch throughput, so
        the drift accumulated exactly where it hurt most.

        The refund is a compensating event row with a NEGATIVE count rather than
        a deletion or an update of the original. The window is a sum over rows
        in a time range, so a negative row expires on the same schedule as the
        charge it reverses -- which is the correct behaviour, and is what
        deleting the original row would get wrong. Append-only also keeps the
        table an audit trail: the reservation and its refund both remain
        visible, and `queries_used_in_window` already SUMs, so it needs no
        change to read signed values correctly.
        """
        if count <= 0:
            return
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE query_ledger SET queries = MAX(0, queries - ?)
                WHERE day = ? AND university_slug = ?
                """,
                (int(count), self._today(), slug.lower().strip()),
            )
            conn.execute(
                "INSERT INTO query_events (university_slug, queries) VALUES (?, ?)",
                (slug.lower().strip(), -int(count)),
            )
            conn.commit()

    # --------------------------------------------------- orphaned notebooks --

    def get_orphaned_notebooks(self) -> List[Dict[str, str]]:
        """
        Notebooks recorded against a university that never reached a terminal state.

        A notebook is deleted only after its payload is written, so a university
        still sitting at 'crawled' or 'ingested' while holding a notebook_id is
        holding a live workspace slot that nothing will ever free. An in-process
        `finally` cannot cover this: the run that leaked COMSATS notebook
        714feb18 was killed outright, and no cleanup handler survives that. The
        record in SQLite does, which is why the reaper reads from here.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT university_slug, notebook_id, status FROM pipeline_state
                WHERE notebook_id IS NOT NULL AND notebook_id != ''
                  AND status NOT IN ('completed', ?, 'failed')
                """,
                (PARTIAL_EXTRACTION,),
            ).fetchall()
            return [
                {
                    "university_slug": r["university_slug"],
                    "notebook_id": r["notebook_id"],
                    "status": r["status"],
                }
                for r in rows
            ]

    def clear_notebook(self, slug: str) -> None:
        """Forget a university's notebook id, once that notebook is actually gone."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE pipeline_state SET notebook_id = NULL WHERE university_slug = ?",
                (slug.lower().strip(),),
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
