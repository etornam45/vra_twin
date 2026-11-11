"""Tests for the local Gateway REST API & UI."""

import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from gateway.src.api.app import create_gateway_app
from gateway.src.store import GatewayStorage


def test_gateway_api_endpoints(tmp_path):
    db_file = tmp_path / "gw.db"
    config_dir = Path("sites/VLK-ISL-001")

    # Seed tag data in store
    store = GatewayStorage(db_file)
    store.allocate_seq_and_insert_batch(
        stream="telemetry",
        topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
        payload=b"test",
        n_samples=1,
        t_first_ms=1000,
        t_last_ms=1000,
        tag_updates=[("BESS_01", "soc", 78.5, None, 0, 1000, 1000)]
    )

    app = create_gateway_app(db_file, config_dir)
    client = TestClient(app)

    # 1. Test status
    r_status = client.get("/api/v1/status")
    assert r_status.status_code == 200
    data = r_status.json()
    assert data["status"] == "ok"
    assert data["queue"]["pending"] == 1

    # 2. Test tags
    r_tags = client.get("/api/v1/tags")
    assert r_tags.status_code == 200
    tags = r_tags.json()
    assert len(tags) == 1
    assert tags[0]["asset_id"] == "BESS_01"
    assert tags[0]["value_num"] == 78.5

    # 3. Test config validate
    r_val = client.post("/api/v1/config/validate")
    assert r_val.status_code == 200
    assert r_val.json()["valid"] is True

    # 4. Test HTML UI index
    r_ui = client.get("/")
    assert r_ui.status_code == 200
    assert "Volta River Authority Mini-Grid Gateway" in r_ui.text
