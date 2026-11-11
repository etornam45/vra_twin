"""Tests for gateway health agent and command handler."""

import time
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from contracts.src.messages import CommandMessage, GatewayHealth
from gateway.src.store import GatewayStorage
from gateway.src.health import HealthAgent
from gateway.src.commands import CommandHandler


def test_health_agent_emission(tmp_path):
    db = GatewayStorage(tmp_path / "gw.db")
    agent = HealthAgent(db=db, gateway_id="GW-VLK-001", site_id="VLK-ISL-001")

    # Generate health object
    health = agent.build_health_message(link_state="ONLINE", rtt_ms=45)
    assert health.gateway_id == "GW-VLK-001"
    assert health.link["state"] == "ONLINE"
    assert health.clock["quality"] == "synced"

    # Emit to outbox
    seq = agent.emit_heartbeat()
    assert seq == 1

    batch = db.next_to_send()
    assert batch is not None
    assert batch.stream == "health"
    assert batch.priority == 2


def test_command_handler_execution_and_idempotency(tmp_path):
    db = GatewayStorage(tmp_path / "gw.db")
    applied_commands = []

    def mock_apply(asset_id, cmd_type, params):
        applied_commands.append((asset_id, cmd_type, params))
        return True

    def mock_readback(asset_id, cmd_type):
        return {"min_soc_pct": 25.0}

    handler = CommandHandler(
        db=db,
        site_id="VLK-ISL-001",
        gateway_id="GW-VLK-001",
        enabled=True,
        allowed_types=["set_soc_reserve"],
        apply_callback=mock_apply,
        readback_callback=mock_readback
    )

    now = datetime.now(timezone.utc)
    future = now + timedelta(minutes=5)
    past = now - timedelta(minutes=5)

    # 1. Valid command execution
    cmd_valid = CommandMessage(
        cmd_id="cmd-001",
        site_id="VLK-ISL-001",
        gateway_id="GW-VLK-001",
        asset_id="PCS_01",
        type="set_soc_reserve",
        **{"class": "B"},
        params={"min_soc_pct": 25.0},
        reason="Preserve battery for night",
        requested_by="admin",
        issued_at=now.isoformat(),
        expires_at=future.isoformat(),
        signature="mock-sig",
        key_id="k1"
    )

    res1 = handler.handle_command(cmd_valid)
    assert res1.state == "CONFIRMED"
    assert res1.readback["min_soc_pct"] == 25.0
    assert len(applied_commands) == 1

    # 2. Idempotent re-run: returns recorded result without re-executing
    res_repeat = handler.handle_command(cmd_valid)
    assert res_repeat.state == "CONFIRMED"
    assert len(applied_commands) == 1

    # 3. Expired command
    cmd_expired = CommandMessage(
        cmd_id="cmd-002",
        site_id="VLK-ISL-001",
        gateway_id="GW-VLK-001",
        asset_id="PCS_01",
        type="set_soc_reserve",
        **{"class": "B"},
        params={"min_soc_pct": 25.0},
        reason="Late arrival",
        requested_by="admin",
        issued_at=past.isoformat(),
        expires_at=past.isoformat(),
        signature="mock-sig",
        key_id="k1"
    )
    res_exp = handler.handle_command(cmd_expired)
    assert res_exp.state == "EXPIRED"

    # 4. Out of range command rejected
    cmd_oor = CommandMessage(
        cmd_id="cmd-003",
        site_id="VLK-ISL-001",
        gateway_id="GW-VLK-001",
        asset_id="PCS_01",
        type="set_soc_reserve",
        **{"class": "B"},
        params={"min_soc_pct": 99.0},  # Max allowed is 50.0
        reason="Invalid reserve",
        requested_by="admin",
        issued_at=now.isoformat(),
        expires_at=future.isoformat(),
        signature="mock-sig",
        key_id="k1"
    )
    res_oor = handler.handle_command(cmd_oor)
    assert res_oor.state == "REJECTED"
    assert res_oor.reason == "out_of_range_min_soc"
