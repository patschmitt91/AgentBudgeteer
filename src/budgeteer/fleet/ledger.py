"""SQLite-backed shard ledger.

Each Fleet run writes one row to `runs` and N rows to `shards`. Workers
claim pending shards atomically and report results. The ledger is the
single source of truth for what work has been done.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS shards (
    shard_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    worker_id TEXT,
    worktree_path TEXT,
    result_text TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shards_run_status ON shards(run_id, status);
"""

_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Shard:
    shard_id: str
    run_id: str
    description: str
    status: str
    worker_id: str | None
    worktree_path: str | None
    result_text: str | None
    cost_usd: float
    tokens_in: int
    tokens_out: int
    error: str | None


class ShardLedger:
    """Thread-safe SQLite shard ledger."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        is_memory = self._path == ":memory:"
        if not is_memory:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        # Concurrency + integrity tuning. Run BEFORE schema init.
        # See harden/phase-3 #3A.
        if not is_memory:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> ShardLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def record_run(self, run_id: str, task: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, task, created_at, status) VALUES(?,?,?,?)",
                (run_id, task, time.time(), "open"),
            )

    def finalize_run(self, run_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status=? WHERE run_id=?",
                (status, run_id),
            )

    def add_shard(self, shard_id: str, run_id: str, description: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO shards(shard_id, run_id, description) VALUES(?,?,?)",
                (shard_id, run_id, description),
            )

    def claim_next(self, run_id: str, worker_id: str) -> Shard | None:
        """Atomically move a pending shard to in_progress and return it.

        Loops internally if a concurrent claim wins the UPDATE race so a
        worker does not exit prematurely while pending shards remain. The
        per-instance lock already serializes callers in-process; the loop
        makes the claim correct even if this ledger is ever shared across
        processes on the same SQLite file.
        """
        while True:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT shard_id FROM shards WHERE run_id=? AND status='pending' "
                    "ORDER BY shard_id LIMIT 1",
                    (run_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                shard_id = row[0]
                cur2 = self._conn.execute(
                    "UPDATE shards SET status='in_progress', worker_id=? "
                    "WHERE shard_id=? AND status='pending'",
                    (worker_id, shard_id),
                )
                if cur2.rowcount == 0:
                    # Raced with another claimer; try the next pending row.
                    continue
                return self._load(shard_id)

    def complete_shard(
        self,
        shard_id: str,
        *,
        result_text: str,
        cost_usd: float,
        tokens_in: int,
        tokens_out: int,
        worktree_path: str | None,
    ) -> None:
        from budgeteer.redaction import redact

        # Worker output may echo prompts containing secrets; scrub at the
        # ledger boundary. See harden/phase-3 #3B.
        safe_result = redact(result_text) if result_text else result_text
        with self._lock:
            self._conn.execute(
                "UPDATE shards SET status='done', result_text=?, cost_usd=?, "
                "tokens_in=?, tokens_out=?, worktree_path=? WHERE shard_id=?",
                (safe_result, cost_usd, tokens_in, tokens_out, worktree_path, shard_id),
            )

    def fail_shard(self, shard_id: str, error: str) -> None:
        from budgeteer.redaction import redact

        safe_error = redact(error) if error else error
        with self._lock:
            self._conn.execute(
                "UPDATE shards SET status='error', error=? WHERE shard_id=?",
                (safe_error, shard_id),
            )

    def list_shards(self, run_id: str) -> list[Shard]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT shard_id, run_id, description, status, worker_id, worktree_path, "
                "result_text, cost_usd, tokens_in, tokens_out, error "
                "FROM shards WHERE run_id=? ORDER BY shard_id",
                (run_id,),
            )
            rows = cur.fetchall()
        return [_row_to_shard(r) for r in rows]

    def _load(self, shard_id: str) -> Shard:
        cur = self._conn.execute(
            "SELECT shard_id, run_id, description, status, worker_id, worktree_path, "
            "result_text, cost_usd, tokens_in, tokens_out, error "
            "FROM shards WHERE shard_id=?",
            (shard_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"shard {shard_id} not found")
        return _row_to_shard(row)


def _row_to_shard(row: tuple[object, ...]) -> Shard:
    return Shard(
        shard_id=str(row[0]),
        run_id=str(row[1]),
        description=str(row[2]),
        status=str(row[3]),
        worker_id=None if row[4] is None else str(row[4]),
        worktree_path=None if row[5] is None else str(row[5]),
        result_text=None if row[6] is None else str(row[6]),
        cost_usd=float(row[7] or 0.0),  # type: ignore[arg-type]
        tokens_in=int(row[8] or 0),  # type: ignore[call-overload]
        tokens_out=int(row[9] or 0),  # type: ignore[call-overload]
        error=None if row[10] is None else str(row[10]),
    )
