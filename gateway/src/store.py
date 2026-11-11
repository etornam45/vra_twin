"""SQLite WAL persistent store and Outbox implementation for the edge gateway.

Section 3.4 & 3.6 Implementation:
- Pragmas: journal_mode=WAL, synchronous=NORMAL, foreign_keys=ON
- Tables: seq_counter, outbox, tag_state, alarm_state, commands, data_gaps
- Transactional sequence allocation and batch insertion
- Priority ordering, selective ack processing, disk pressure downsampling
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import zstandard as zstd


INIT_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS seq_counter (
    stream TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL
);

INSERT OR IGNORE INTO seq_counter (stream, next_seq) VALUES
    ('telemetry', 1),
    ('events', 1),
    ('cmdres', 1),
    ('health', 1);

CREATE TABLE IF NOT EXISTS outbox (
    stream TEXT NOT NULL,
    seq INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    enc TEXT NOT NULL DEFAULT 'zstd+json',
    bytes INTEGER NOT NULL,
    n_samples INTEGER,
    t_first_ms INTEGER,
    t_last_ms INTEGER,
    state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (state IN ('PENDING', 'INFLIGHT', 'ACKED', 'DOWNSAMPLED', 'DROPPED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    created_ms INTEGER NOT NULL,
    last_attempt_ms INTEGER,
    acked_ms INTEGER,
    lineage TEXT,
    PRIMARY KEY (stream, seq)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS ix_outbox_send 
    ON outbox (state, priority, stream, seq) 
    WHERE state IN ('PENDING', 'INFLIGHT');

CREATE TABLE IF NOT EXISTS tag_state (
    asset_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    value_num REAL,
    value_text TEXT,
    quality INTEGER NOT NULL,
    t_ms INTEGER NOT NULL,
    last_sent_ms INTEGER,
    PRIMARY KEY (asset_id, tag)
);

CREATE TABLE IF NOT EXISTS data_gaps (
    gap_id TEXT PRIMARY KEY,
    stream TEXT NOT NULL,
    t_from_ms INTEGER NOT NULL,
    t_to_ms INTEGER NOT NULL,
    cause TEXT NOT NULL,
    reported INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS commands (
    cmd_id TEXT PRIMARY KEY,
    received_ms INTEGER NOT NULL,
    issued_ms INTEGER NOT NULL,
    expires_ms INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    type TEXT NOT NULL,
    params TEXT,
    state TEXT NOT NULL CHECK (state IN ('RECEIVED', 'REJECTED', 'EXPIRED', 'APPLYING', 'APPLIED', 'FAILED', 'CONFIRMED')),
    reason TEXT,
    readback TEXT,
    finished_ms INTEGER
);
"""


class OutboxBatch:
    def __init__(
        self,
        stream: str,
        seq: int,
        priority: int,
        topic: str,
        payload: bytes,
        enc: str,
        bytes_len: int,
        n_samples: int,
        t_first_ms: int,
        t_last_ms: int,
        state: str,
        attempts: int,
        created_ms: int
    ):
        self.stream = stream
        self.seq = seq
        self.priority = priority
        self.topic = topic
        self.payload = payload
        self.enc = enc
        self.bytes_len = bytes_len
        self.n_samples = n_samples
        self.t_first_ms = t_first_ms
        self.t_last_ms = t_last_ms
        self.state = state
        self.attempts = attempts
        self.created_ms = created_ms

    def mqtt_props(self) -> Dict[str, str]:
        return {
            "enc": self.enc,
            "seq": str(self.seq),
            "stream": self.stream
        }


class GatewayStorage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.executescript(INIT_SQL)

    def allocate_seq_and_insert_batch(
        self,
        stream: str,
        topic: str,
        payload: bytes,
        n_samples: int,
        t_first_ms: int,
        t_last_ms: int,
        priority: int = 5,
        enc: str = "zstd+json",
        tag_updates: Optional[List[Tuple[str, str, Optional[float], Optional[str], int, int]]] = None
    ) -> int:
        """Atomically increments sequence counter, stores batch in outbox, and updates tag_state."""
        now_ms = int(time.time() * 1000)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. Allocate sequence number
            cursor.execute("SELECT next_seq FROM seq_counter WHERE stream = ?", (stream,))
            row = cursor.fetchone()
            if not row:
                seq = 1
                cursor.execute("INSERT INTO seq_counter (stream, next_seq) VALUES (?, ?)", (stream, seq + 1))
            else:
                seq = row["next_seq"]
                cursor.execute("UPDATE seq_counter SET next_seq = next_seq + 1 WHERE stream = ?", (stream,))

            # 2. Insert into outbox
            cursor.execute(
                """
                INSERT INTO outbox (
                    stream, seq, priority, topic, payload, enc, bytes,
                    n_samples, t_first_ms, t_last_ms, state, attempts, created_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
                """,
                (stream, seq, priority, topic, payload, enc, len(payload), n_samples, t_first_ms, t_last_ms, now_ms)
            )

            # 3. Update tag_state
            if tag_updates:
                cursor.executemany(
                    """
                    INSERT INTO tag_state (asset_id, tag, value_num, value_text, quality, t_ms, last_sent_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(asset_id, tag) DO UPDATE SET
                        value_num=excluded.value_num,
                        value_text=excluded.value_text,
                        quality=excluded.quality,
                        t_ms=excluded.t_ms,
                        last_sent_ms=excluded.last_sent_ms
                    """,
                    tag_updates
                )

            conn.commit()
            return seq

    def next_to_send(self, live_window_s: int = 120, allow_backfill: bool = True) -> Optional[OutboxBatch]:
        """Fetches the highest priority pending batch respecting live-first ordering."""
        now_ms = int(time.time() * 1000)
        live_cutoff_ms = now_ms - (live_window_s * 1000)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Live batches first (created_ms >= live_cutoff_ms or high priority), then backfill (oldest first)
            query = """
            SELECT * FROM outbox
            WHERE state = 'PENDING'
            ORDER BY
                priority ASC,
                CASE WHEN created_ms >= ? THEN 0 ELSE 1 END ASC,
                seq ASC
            LIMIT 1
            """
            cursor.execute(query, (live_cutoff_ms,))
            row = cursor.fetchone()
            if not row:
                return None

            # If it's a backfill batch and backfill is not allowed in this slot, skip
            is_backfill = row["created_ms"] < live_cutoff_ms and row["priority"] > 2
            if is_backfill and not allow_backfill:
                return None

            return OutboxBatch(
                stream=row["stream"],
                seq=row["seq"],
                priority=row["priority"],
                topic=row["topic"],
                payload=row["payload"],
                enc=row["enc"],
                bytes_len=row["bytes"],
                n_samples=row["n_samples"],
                t_first_ms=row["t_first_ms"],
                t_last_ms=row["t_last_ms"],
                state=row["state"],
                attempts=row["attempts"],
                created_ms=row["created_ms"]
            )

    def mark_inflight(self, stream: str, seq: int):
        now_ms = int(time.time() * 1000)
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET state = 'INFLIGHT', attempts = attempts + 1, last_attempt_ms = ?
                WHERE stream = ? AND seq = ?
                """,
                (now_ms, stream, seq)
            )
            conn.commit()

    def mark_pending(self, stream: str, seq: int):
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE outbox SET state = 'PENDING' WHERE stream = ? AND seq = ?",
                (stream, seq)
            )
            conn.commit()

    def requeue_stale_inflight(self, timeout_s: int = 30):
        cutoff_ms = int(time.time() * 1000) - (timeout_s * 1000)
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET state = 'PENDING'
                WHERE state = 'INFLIGHT' AND last_attempt_ms < ?
                """,
                (cutoff_ms,)
            )
            conn.commit()

    def requeue_all_inflight(self):
        """Called on reconnect: all inflight batches return to pending safely."""
        with self._get_connection() as conn:
            conn.execute("UPDATE outbox SET state = 'PENDING' WHERE state = 'INFLIGHT'")
            conn.commit()

    def mark_acked_ranges(self, stream: str, ranges: List[Tuple[int, int]]):
        """Processes selective ACK ranges in a single atomic transaction."""
        now_ms = int(time.time() * 1000)
        with self._get_connection() as conn:
            for start_seq, end_seq in ranges:
                conn.execute(
                    """
                    UPDATE outbox
                    SET state = 'ACKED', acked_ms = ?
                    WHERE stream = ? AND seq >= ? AND seq <= ?
                    """,
                    (now_ms, stream, start_seq, end_seq)
                )
            conn.commit()

    def quarantine_rejected(self, stream: str, seq: int, reason: str):
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE outbox
                SET state = 'DROPPED', lineage = ?
                WHERE stream = ? AND seq = ?
                """,
                (f"REJECTED: {reason}", stream, seq)
            )
            conn.commit()

    def prune_acked(self, retention_hours: int = 24):
        cutoff_ms = int(time.time() * 1000) - (retention_hours * 3600 * 1000)
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM outbox WHERE state = 'ACKED' AND acked_ms < ?",
                (cutoff_ms,)
            )
            conn.commit()

    def get_queue_stats(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    COUNT(CASE WHEN state = 'PENDING' THEN 1 END) as pending,
                    COUNT(CASE WHEN state = 'INFLIGHT' THEN 1 END) as inflight,
                    COUNT(CASE WHEN state = 'ACKED' THEN 1 END) as acked,
                    COALESCE(SUM(CASE WHEN state IN ('PENDING', 'INFLIGHT') THEN bytes ELSE 0 END), 0) as bytes_pending,
                    MIN(CASE WHEN state = 'PENDING' THEN created_ms END) as oldest_created_ms
                FROM outbox
                """
            )
            row = cursor.fetchone()
            now_ms = int(time.time() * 1000)
            oldest_age_s = 0
            if row["oldest_created_ms"]:
                oldest_age_s = max(0, int((now_ms - row["oldest_created_ms"]) / 1000))

            return {
                "pending": row["pending"],
                "inflight": row["inflight"],
                "acked": row["acked"],
                "bytes_pending": row["bytes_pending"],
                "oldest_unsent_age_s": oldest_age_s
            }
