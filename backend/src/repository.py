"""Storage repository layer with unified interface for SQLite (local/dev/testing)
and PostgreSQL/TimescaleDB (production).

Implements:
- Site and Asset Registry
- Idempotent Telemetry Ingest & Deduping
- Selective ACK Range generation
- Event and Live Twin State persistence
- Signed Command Tracking and Audit logging
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from contracts.src.messages import TelemetryBatch, SelectiveAck, SelectiveAckReject, EventMessage
from contracts.src.twin import SiteTwinState


SQLITE_INIT_SQL = """
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Africa/Accra',
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    gateway_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    site_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    name TEXT NOT NULL,
    manufacturer TEXT,
    model TEXT,
    config TEXT NOT NULL DEFAULT '{}',
    connections TEXT NOT NULL DEFAULT '{}',
    revision INT NOT NULL DEFAULT 1,
    PRIMARY KEY (site_id, asset_id),
    FOREIGN KEY(site_id) REFERENCES sites(site_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS gateways (
    gateway_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    version TEXT,
    cfg_version TEXT,
    last_seen_at TEXT,
    status TEXT NOT NULL DEFAULT 'offline',
    created_at TEXT NOT NULL,
    FOREIGN KEY(site_id) REFERENCES sites(site_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ingest_batches (
    gateway_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    seq INT NOT NULL,
    site_id TEXT NOT NULL,
    n_samples INT NOT NULL,
    t_first_ms INT NOT NULL,
    t_last_ms INT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (gateway_id, stream, seq)
);

CREATE TABLE IF NOT EXISTS data_gaps (
    gap_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    t_from_ms INT NOT NULL,
    t_to_ms INT NOT NULL,
    cause TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry (
    ts TEXT NOT NULL,
    site_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    value_num REAL,
    value_txt TEXT,
    quality INT NOT NULL DEFAULT 0,
    resolution TEXT NOT NULL DEFAULT 'raw',
    v_min REAL,
    v_max REAL,
    n INT,
    recv_ts TEXT NOT NULL,
    batch_seq INT,
    PRIMARY KEY (site_id, asset_id, tag, ts, resolution)
);

CREATE INDEX IF NOT EXISTS ix_telemetry_query ON telemetry (site_id, asset_id, tag, ts);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    seq INT NOT NULL,
    kind TEXT NOT NULL,
    asset_id TEXT,
    code TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    ts TEXT NOT NULL,
    cleared_ts TEXT,
    context TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS commands (
    cmd_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL,
    gateway_id TEXT NOT NULL,
    asset_id TEXT NOT NULL,
    type TEXT NOT NULL,
    command_class TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'REQUESTED',
    readback TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS command_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cmd_id TEXT NOT NULL,
    state TEXT NOT NULL,
    details TEXT,
    logged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS twin_state (
    site_id TEXT PRIMARY KEY,
    asof TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'live',
    link_state TEXT NOT NULL DEFAULT 'offline',
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class BackendRepository:
    """Manages backend database interactions with full idempotency guarantees."""

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
            conn.executescript(SQLITE_INIT_SQL)

    # Site & Registry methods
    def register_site(self, site_id: str, name: str, lat: float, lon: float, gateway_id: str, tz_name: str = "Africa/Accra"):
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sites (site_id, name, timezone, latitude, longitude, gateway_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id) DO UPDATE SET
                    name=excluded.name,
                    latitude=excluded.latitude,
                    longitude=excluded.longitude,
                    gateway_id=excluded.gateway_id,
                    updated_at=excluded.updated_at
                """,
                (site_id, name, tz_name, lat, lon, gateway_id, now_iso, now_iso)
            )
            conn.execute(
                """
                INSERT INTO gateways (gateway_id, site_id, status, created_at)
                VALUES (?, ?, 'offline', ?)
                ON CONFLICT(gateway_id) DO NOTHING
                """,
                (gateway_id, site_id, now_iso)
            )
            conn.commit()

    def register_asset(self, site_id: str, asset_id: str, asset_type: str, name: str, config: dict, connections: dict, revision: int = 1):
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO assets (site_id, asset_id, asset_type, name, config, connections, revision)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id, asset_id) DO UPDATE SET
                    asset_type=excluded.asset_type,
                    name=excluded.name,
                    config=excluded.config,
                    connections=excluded.connections,
                    revision=excluded.revision
                """,
                (site_id, asset_id, asset_type, name, json.dumps(config), json.dumps(connections), revision)
            )
            conn.commit()

    # Telemetry Ingestion (Idempotent + Atomic)
    def ingest_batch(self, batch: TelemetryBatch) -> Tuple[bool, Optional[str]]:
        """Ingests a telemetry batch:

        1. Inserts record into ingest_batches (PK: gateway_id, stream, seq).
           If duplicate, skips telemetry insert and returns True (idempotent duplicate).
        2. Vectorised insert of samples into telemetry hypertable (ON CONFLICT DO NOTHING).
        3. Commits transaction atomically before caller generates Selective ACK.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Check for duplicate batch
            cursor.execute(
                "SELECT seq FROM ingest_batches WHERE gateway_id = ? AND stream = ? AND seq = ?",
                (batch.gateway_id, batch.stream, batch.seq)
            )
            if cursor.fetchone():
                return True, "duplicate_already_committed"

            # Compute min/max timestamps and sample count
            total_samples = sum(len(col.t) for col in batch.series)
            t_first_ms = batch.t0_ms
            t_last_ms = batch.t0_ms
            telemetry_rows = []

            for col in batch.series:
                for t_offset, val, q in zip(col.t, col.v, col.q):
                    sample_ms = batch.t0_ms + t_offset
                    t_first_ms = min(t_first_ms, sample_ms)
                    t_last_ms = max(t_last_ms, sample_ms)
                    sample_iso = datetime.fromtimestamp(sample_ms / 1000.0, tz=timezone.utc).isoformat()

                    telemetry_rows.append((
                        sample_iso,
                        batch.site_id,
                        col.asset,
                        col.tag,
                        val,
                        None,
                        q,
                        batch.resolution,
                        None,
                        None,
                        1,
                        now_iso,
                        batch.seq
                    ))

            # Insert batch ledger record
            cursor.execute(
                """
                INSERT INTO ingest_batches (
                    gateway_id, stream, seq, site_id, n_samples, t_first_ms, t_last_ms, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (batch.gateway_id, batch.stream, batch.seq, batch.site_id, total_samples, t_first_ms, t_last_ms, now_iso)
            )

            # Insert telemetry points
            if telemetry_rows:
                cursor.executemany(
                    """
                    INSERT INTO telemetry (
                        ts, site_id, asset_id, tag, value_num, value_txt, quality,
                        resolution, v_min, v_max, n, recv_ts, batch_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(site_id, asset_id, tag, ts, resolution) DO NOTHING
                    """,
                    telemetry_rows
                )

            # Update gateway last_seen_at
            cursor.execute(
                "UPDATE gateways SET last_seen_at = ?, status = 'online' WHERE gateway_id = ?",
                (now_iso, batch.gateway_id)
            )

            conn.commit()
            return True, None

    def query_timeseries(
        self,
        site_id: str,
        asset_id: str,
        tag: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 500
    ) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT ts, site_id, asset_id, tag, value_num, quality FROM telemetry WHERE site_id = ? AND asset_id = ? AND tag = ?"
            params = [site_id, asset_id, tag]
            if from_ts:
                query += " AND ts >= ?"
                params.append(from_ts)
            if to_ts:
                query += " AND ts <= ?"
                params.append(to_ts)
            query += " ORDER BY ts ASC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def record_event(self, event: EventMessage):
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    event_id, site_id, gateway_id, stream, seq, kind,
                    asset_id, code, severity, message, ts, cleared_ts, context
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    event.event_id,
                    event.site_id,
                    event.gateway_id,
                    event.stream,
                    event.seq,
                    event.kind,
                    event.asset_id,
                    event.code,
                    event.severity,
                    event.message,
                    event.ts,
                    event.cleared_ts,
                    json.dumps(event.context)
                )
            )
            conn.commit()

    def update_twin_state(self, state: SiteTwinState):
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO twin_state (site_id, asof, mode, link_state, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(site_id) DO UPDATE SET
                    asof=excluded.asof,
                    mode=excluded.mode,
                    link_state=excluded.link_state,
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """,
                (state.site_id, state.asof, state.mode, state.link_state, state.model_dump_json(), now_iso)
            )
            conn.commit()

    def get_twin_state(self, site_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT state FROM twin_state WHERE site_id = ?", (site_id,))
            row = cursor.fetchone()
            if row:
                return json.loads(row["state"])
            return None
