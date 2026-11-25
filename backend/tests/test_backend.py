"""Integration tests for Backend Ingestion, Idempotency, Repository and API."""

import json
import os
import pytest
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient

from contracts.src.messages import TelemetryBatch, SeriesColumn
from contracts.src.twin import SiteTwinState, AssetTwinState, EnergyBalance, TagValue
from backend.src.repository import BackendRepository, PostgresBackendRepository
from backend.src.ingest import IngestionService
from backend.src.api.app import create_backend_app
from gateway.src.encoder import TelemetryEncoder


def test_backend_repository_and_ingestion(tmp_path):
    db_file = tmp_path / "backend.db"
    repo = BackendRepository(db_file)
    ingest_svc = IngestionService(repo)

    # 1. Register site & assets
    repo.register_site("VLK-ISL-001", "Volta Lake Island 001", 7.80, -0.05, "GW-VLK-001")
    repo.register_asset("VLK-ISL-001", "BESS_01", "bess", "Main Battery", {}, {})

    # 2. Build and compress a batch
    encoder = TelemetryEncoder("VLK-ISL-001", "GW-VLK-001")
    series_data = {
        ("BESS_01", "soc", "%"): {
            "t": [0, 1000],
            "v": [78.0, 78.2],
            "q": [0, 0]
        }
    }
    t0_ms = int(time.time() * 1000)
    compressed, n_samples, t_first, t_last = encoder.build_and_compress_batch(
        series_data=series_data,
        t0_ms=t0_ms
    )

    # 3. Ingest batch
    ack = ingest_svc.process_raw_telemetry_payload(compressed, is_compressed=True)
    assert ack.site_id == "VLK-ISL-001"
    assert len(ack.ranges) == 1
    assert len(ack.rejected) == 0

    # 4. Ingest duplicate (Idempotency verification)
    ack_dup = ingest_svc.process_raw_telemetry_payload(compressed, is_compressed=True)
    assert len(ack_dup.ranges) == 1
    assert len(ack_dup.rejected) == 0

    # 5. Query timeseries from repository
    ts_points = repo.query_timeseries("VLK-ISL-001", "BESS_01", "soc")
    assert len(ts_points) == 2
    assert ts_points[0]["value_num"] == 78.0
    assert ts_points[1]["value_num"] == 78.2
    latest_point = repo.query_timeseries("VLK-ISL-001", "BESS_01", "soc", limit=1)
    assert len(latest_point) == 1
    assert latest_point[0]["value_num"] == 78.2


def test_backend_persists_data_gap_events_before_acknowledging(tmp_path):
    repo = BackendRepository(tmp_path / "backend.db")
    repo.register_site("VLK-ISL-001", "Volta Lake Island 001", 7.80, -0.05, "GW-VLK-001")
    ingest_svc = IngestionService(repo)
    event_payload = {
        "event_id": "gap-001",
        "site_id": "VLK-ISL-001",
        "gateway_id": "GW-VLK-001",
        "stream": "events",
        "seq": 1,
        "kind": "data_gap",
        "asset_id": "BESS_01",
        "code": "TELEMETRY_DROPPED",
        "severity": "warning",
        "message": "Telemetry samples omitted for BESS_01.soc",
        "ts": "2026-11-25T10:00:02+00:00",
        "context": {
            "tag": "soc",
            "t_from_ms": 1000,
            "t_to_ms": 2000,
            "cause": "disk_pressure"
        }
    }

    ack = ingest_svc.process_raw_event_payload(
        json.dumps(event_payload).encode()
    )
    duplicate_ack = ingest_svc.process_raw_event_payload(
        json.dumps(event_payload).encode()
    )
    assert ack.stream == "events"
    assert ack.ranges == [(1, 1)]
    assert duplicate_ack.ranges == [(1, 1)]
    with repo._get_connection() as conn:
        rows = conn.execute(
            "SELECT event_id, code FROM events WHERE event_id = ?",
            ("gap-001",)
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["code"] == "TELEMETRY_DROPPED"


def test_backend_api_endpoints(tmp_path):
    db_file = tmp_path / "backend.db"
    repo = BackendRepository(db_file)
    repo.register_site("VLK-ISL-001", "Volta Lake Island 001", 7.80, -0.05, "GW-VLK-001")
    repo.register_asset("VLK-ISL-001", "BESS_01", "bess", "Main Battery", {"capacity_kwh": 200}, {})

    # Seed twin state
    state = SiteTwinState(
        site_id="VLK-ISL-001",
        asof="2026-11-11T12:00:00Z",
        mode="live",
        link_state="online",
        assets={
            "BESS_01": AssetTwinState(
                status="ok",
                freshness_s=2.0,
                confidence="verified",
                values={"soc": TagValue(v=78.2, u="%", t="2026-11-11T12:00:00Z")}
            )
        },
        energy_balance=EnergyBalance(pv_kw=45.0, bess_kw=-20.0, load_kw=23.0)
    )
    repo.update_twin_state(state)
    sample_time = datetime.now(timezone.utc).isoformat()
    with repo._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO telemetry (
                ts, site_id, asset_id, tag, value_num, quality, resolution, recv_ts
            ) VALUES (?, 'VLK-ISL-001', 'BESS_01', 'soc', 78.2, 0, 'raw', ?)
            """,
            (sample_time, sample_time)
        )
        conn.execute(
            """
            INSERT INTO events (
                event_id, site_id, gateway_id, stream, seq, kind, asset_id,
                code, severity, message, ts, context
            ) VALUES (
                'dashboard-gap-001', 'VLK-ISL-001', 'GW-VLK-001', 'events', 1,
                'data_gap', 'BESS_01', 'TELEMETRY_DROPPED', 'warning',
                'Battery telemetry gap recorded', ?, '{"cause":"disk_pressure"}'
            )
            """,
            (sample_time,)
        )
        conn.commit()

    app = create_backend_app(db_file)
    client = TestClient(app)

    # 1. Health
    r_health = client.get("/v1/health")
    assert r_health.status_code == 200
    assert r_health.json()["status"] == "ok"

    # 2. Sites list
    r_sites = client.get("/v1/sites")
    assert r_sites.status_code == 200
    sites = r_sites.json()
    assert len(sites) == 1
    assert sites[0]["site_id"] == "VLK-ISL-001"

    # 3. Site assets
    r_assets = client.get("/v1/sites/VLK-ISL-001/assets")
    assert r_assets.status_code == 200
    assets = r_assets.json()
    assert len(assets) == 1
    assert assets[0]["asset_id"] == "BESS_01"

    # 4. Site live twin state
    r_state = client.get("/v1/sites/VLK-ISL-001/state")
    assert r_state.status_code == 200
    st_data = r_state.json()
    assert st_data["site_id"] == "VLK-ISL-001"
    assert st_data["assets"]["BESS_01"]["status"] == "ok"

    # 5. Read dashboard snapshot and static UI
    r_dashboard = client.get("/v1/sites/VLK-ISL-001/dashboard")
    assert r_dashboard.status_code == 200
    dashboard_data = r_dashboard.json()
    assert dashboard_data["site"]["site_id"] == "VLK-ISL-001"
    assert dashboard_data["gateway"]["gateway_id"] == "GW-VLK-001"
    assert dashboard_data["latest_readings"][0]["tag"] == "soc"
    assert dashboard_data["telemetry_samples_24h"] == 1
    assert dashboard_data["recent_events"][0]["context"]["cause"] == "disk_pressure"
    assert dashboard_data["twin_state"]["site_id"] == "VLK-ISL-001"
    assert client.get("/dashboard").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/dashboard-assets/dashboard.js").status_code == 200
    assert client.get("/v1/sites/not-registered/dashboard").status_code == 404
    r_diagnostics = client.get(
        "/v1/sites/VLK-ISL-001/diagnostics",
        params={
            "max_asset_freshness_s": 120,
            "max_abs_imbalance_kw": 5,
            "max_underperformance_pct": 20,
            "max_abs_soc_residual_pct": 5,
        },
    )
    assert r_diagnostics.status_code == 200
    assert r_diagnostics.json()["status"] == "clear"
    r_forecast = client.get(
        "/v1/sites/VLK-ISL-001/forecast",
        params={"asset": "BESS_01", "tag": "soc", "horizon_steps": 2, "step_s": 3600},
    )
    assert r_forecast.status_code == 200
    assert r_forecast.json()["calibration_status"] == "uncalibrated"
    assert [point["value"] for point in r_forecast.json()["points"]] == [78.2, 78.2]

    # 6. Issue command
    cmd_payload = {
        "site_id": "VLK-ISL-001",
        "gateway_id": "GW-VLK-001",
        "asset_id": "PCS_01",
        "type": "set_soc_reserve",
        "command_class": "B",
        "params": {"min_soc_pct": 20.0},
        "reason": "Storm coming",
        "requested_by": "operator_ama"
    }
    r_cmd = client.post("/v1/commands", json=cmd_payload)
    assert r_cmd.status_code == 200
    cmd_data = r_cmd.json()
    assert cmd_data["status"] == "dispatched"
    assert cmd_data["command"]["asset_id"] == "PCS_01"

    # 7. Issue forbidden Class X command -> Should fail with 400
    cmd_x = dict(cmd_payload)
    cmd_x["command_class"] = "X"
    r_x = client.post("/v1/commands", json=cmd_x)
    assert r_x.status_code == 400
    assert "strictly prohibited" in r_x.json()["detail"]


@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL is needed for PostgreSQL/TimescaleDB integration",
)
def test_postgres_timescale_repository_dashboard_and_ingestion():
    repo = PostgresBackendRepository(os.environ["DATABASE_URL"])
    suffix = uuid.uuid4().hex[:8]
    site_id = f"TEST-{suffix}"
    gateway_id = f"GW-TEST-{suffix}"
    asset_id = "BESS_01"
    repo.register_site(site_id, "Timescale integration test", 0.0, 0.0, gateway_id)
    repo.register_asset(site_id, asset_id, "bess", "Test battery", {"capacity_kwh": 10}, {})

    encoder = TelemetryEncoder(site_id, gateway_id)
    payload, _, _, _ = encoder.build_and_compress_batch(
        { (asset_id, "soc", "%"): {"t": [0], "v": [72.5], "q": [0]} },
        t0_ms=int(time.time() * 1000),
    )
    ack = IngestionService(repo).process_raw_telemetry_payload(payload)

    assert ack.ranges == [(1, 1)]
    dashboard = repo.get_dashboard_data(site_id)
    assert dashboard is not None
    assert dashboard["latest_readings"][0]["value_num"] == 72.5
    assert repo.query_timeseries(site_id, asset_id, "soc", limit=1)[0]["value_num"] == 72.5
