"""Integration tests for Twin Physics Models, Live State Synchronization and What-If Simulations."""

import pytest
import time
from pathlib import Path

from backend.src.repository import BackendRepository
from twin.src.models.pv import PvArrayModel
from twin.src.models.battery import BessModel
from twin.src.state import LiveTwinSynchronizer
from twin.src.sim import WhatIfSimulationEngine, SimulationScenario


def test_pv_and_battery_models():
    # 1. Test PV Array Model
    pv = PvArrayModel()
    pv_state = pv.init_state({"capacity_kwp": 60.0}, {})
    new_state, outputs = pv.step(pv_state, {"poa": 1000.0, "t_module": 25.0}, dt_s=3600.0)

    assert outputs["p_dc_est"] > 50.0
    assert outputs["p_ac_est"] > 45.0
    assert new_state["cumulative_kwh"] > 45.0

    # 2. Test Battery Model
    bess = BessModel()
    b_state = bess.init_state({"rated_energy_kwh": 200.0}, {})
    # Discharge at 20 kW for 1 hour
    b_state2, b_out = bess.step(b_state, {"p_batt_kw": 20.0}, dt_s=3600.0)
    assert b_state2["soc"] < 75.0
    assert b_state2["throughput_kwh"] == 20.0
    assert b_state2["soh"] <= 100.0


def test_live_twin_synchronization(tmp_path):
    repo = BackendRepository(tmp_path / "sync.db")
    repo.register_site("VLK-ISL-001", "Volta Lake Island 001", 7.80, -0.05, "GW-VLK-001")

    # Ingest a recent telemetry sample
    with repo._get_connection() as conn:
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            """
            INSERT INTO telemetry (ts, site_id, asset_id, tag, value_num, quality, resolution, recv_ts)
            VALUES (?, 'VLK-ISL-001', 'PV_INV_01', 'p_ac', 42.0, 0, 'raw', ?)
            """,
            (now_iso, now_iso)
        )
        conn.execute(
            """
            INSERT INTO telemetry (ts, site_id, asset_id, tag, value_num, quality, resolution, recv_ts)
            VALUES (?, 'VLK-ISL-001', 'BESS_01', 'p_dc', -18.0, 0, 'raw', ?)
            """,
            (now_iso, now_iso)
        )
        conn.execute(
            """
            INSERT INTO telemetry (ts, site_id, asset_id, tag, value_num, quality, resolution, recv_ts)
            VALUES (?, 'VLK-ISL-001', 'MTR_PCC', 'p_active', 24.0, 0, 'raw', ?)
            """,
            (now_iso, now_iso)
        )
        conn.commit()

    sync = LiveTwinSynchronizer(repo, "VLK-ISL-001")
    twin_state = sync.sync_live_state()

    assert twin_state.site_id == "VLK-ISL-001"
    assert twin_state.link_state == "online"
    assert "PV_INV_01" in twin_state.assets
    assert "BESS_01" in twin_state.assets

    # Check Energy Balance: PV(42) + BESS(-18) = 24 kW load
    assert twin_state.energy_balance.pv_kw == 42.0
    assert twin_state.energy_balance.bess_kw == -18.0
    assert twin_state.energy_balance.load_kw == 24.0
    assert twin_state.energy_balance.imbalance_kw == 0.0


def test_what_if_simulation_engine():
    engine = WhatIfSimulationEngine()
    scenario = SimulationScenario(
        scenario_id="sim-volk-001",
        site_id="VLK-ISL-001",
        annual_load_growth_pct=10.0,
        sim_years=5,
        pv_kwp=60.0,
        bess_kwh=200.0,
        eol_soh_threshold_pct=70.0
    )
    result = engine.run_scenario(scenario)

    assert result.scenario_id == "sim-volk-001"
    assert result.years_simulated == 5
    assert len(result.yearly_unserved_energy_kwh) == 5
    assert len(result.soh_trajectory) == 6
    assert result.soh_trajectory[-1] <= result.soh_trajectory[0]
    assert "BESS" in result.recommendation
