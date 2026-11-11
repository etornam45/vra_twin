"""Integration tests for Backend Ingestion, Idempotency, Repository and API."""

import pytest
import time
from pathlib import Path
from fastapi.testclient import TestClient

from contracts.src.messages import TelemetryBatch, SeriesColumn
from contracts.src.twin import SiteTwinState, AssetTwinState, EnergyBalance, TagValue
from backend.src.repository import BackendRepository
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

    # 5. Issue command
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

    # 6. Issue forbidden Class X command -> Should fail with 400
    cmd_x = dict(cmd_payload)
    cmd_x["command_class"] = "X"
    r_x = client.post("/v1/commands", json=cmd_x)
    assert r_x.status_code == 400
    assert "strictly prohibited" in r_x.json()["detail"]
