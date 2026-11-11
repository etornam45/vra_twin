"""Integration tests for the 6-step gateway configuration change pipeline."""

import pytest
from pathlib import Path
from gateway.src.configsvc import ConfigService


def test_config_pipeline_staging_and_rollback(tmp_path):
    # Setup test config directory with base configuration
    cfg_dir = tmp_path / "site_cfg"
    cfg_dir.mkdir()

    base_site = """
site: VLK-ISL-001
name: Volta Lake Island 001
location: [7.80, -0.05]
gateway: GW-VLK-001
assets:
  BESS_01:
    type: bess
    catalog: examplebatt/EB-LFP-200
    pcs: PCS_01
  PCS_01:
    type: pcs
    catalog: exampleinv/EI-GF50
"""
    base_gw = """
gateway: GW-VLK-001
uplink:
  broker: localhost
devices:
  - asset: BESS_01
    protocol: modbus_tcp
commands:
  enabled: false
"""
    (cfg_dir / "site.yaml").write_text(base_site)
    (cfg_dir / "gateway.yaml").write_text(base_gw)

    service = ConfigService(cfg_dir)
    res = service.validate_current()
    assert res.is_valid

    # 1. Apply a valid change
    new_site = base_site.replace("Volta Lake Island 001", "Updated Volta Island 001")
    ok, msg = service.stage_and_apply(new_site_yaml=new_site)
    assert ok is True
    assert "Successfully applied" in msg

    # Verify updated content on disk
    assert "Updated Volta Island 001" in (cfg_dir / "site.yaml").read_text()
    v1 = service.current_version
    assert v1.startswith("v_")

    # 2. Try applying an invalid change (referencing non-existent asset)
    invalid_site = base_site.replace("pcs: PCS_01", "pcs: NON_EXISTENT_PCS")
    ok_inv, msg_inv = service.stage_and_apply(new_site_yaml=invalid_site)
    assert ok_inv is False
    assert "Validation failed" in msg_inv

    # Ensure unchanged
    assert "Updated Volta Island 001" in (cfg_dir / "site.yaml").read_text()

    # 3. Apply second valid change
    new_gw = base_gw.replace("commands:\n  enabled: false", "commands:\n  enabled: true")
    ok2, msg2 = service.stage_and_apply(new_gateway_yaml=new_gw)
    assert ok2 is True
    v2 = service.current_version
    assert "enabled: true" in (cfg_dir / "gateway.yaml").read_text()

    # 4. Rollback to v1
    ok_rb, msg_rb = service.rollback_to_version(v1)
    assert ok_rb is True
    assert "enabled: false" in (cfg_dir / "gateway.yaml").read_text()
