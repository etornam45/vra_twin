"""Integration tests for Twin Physics Models, Live State Synchronization and What-If Simulations."""

import pytest
import time
from pathlib import Path

from backend.src.repository import BackendRepository
from contracts.src.twin import SiteTwinState, AssetTwinState, EnergyBalance
from twin.src.models.pv import PvArrayModel
from twin.src.models.battery import BessModel
from twin.src.state import LiveTwinSynchronizer
from twin.src.sim import WhatIfSimulationEngine, SimulationScenario
from twin.src.diagnostics import (
    DiagnosticThresholds,
    estimate_pv_instantaneous_pr,
    evaluate_soc_predictions,
    evaluate_twin_state,
)
from twin.src.forecast import persistence_forecast


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


def test_battery_ageing_is_incremental_and_parameterized():
    model = BessModel()
    state = model.init_state(
        {"rated_energy_kwh": 200.0, "usable_energy_kwh": 180.0},
        {
            "initial_soh_pct": 90.0,
            "calendar_fade_pct_per_year": 2.0,
            "cycle_fade_pct_per_1000_efc": 2.5,
            "temperature_q10": 2.0,
            "reference_temp_c": 25.0,
        },
    )
    one_year = 365.25 * 24 * 3600

    aged_state, outputs = model.step(state, {"p_batt_kw": 0.0, "t_amb": 25.0}, one_year)
    assert aged_state["soh"] == pytest.approx(88.0)
    assert outputs["calendar_fade_pct"] == pytest.approx(2.0)
    assert outputs["cycle_fade_pct"] == 0.0

    hot_state, _ = model.step(state, {"p_batt_kw": 0.0, "t_amb": 35.0}, one_year)
    assert hot_state["soh"] == pytest.approx(86.0)

    cycled_state, cycle_outputs = model.step(
        state,
        {"p_batt_kw": 20.0, "t_amb": 25.0},
        3600.0,
    )
    assert cycled_state["equivalent_cycles"] == pytest.approx(0.05)
    assert cycle_outputs["cycle_fade_pct"] == pytest.approx(0.000125)
    assert cycled_state["soh"] < state["soh"]


def test_battery_model_rejects_invalid_capacity_and_time():
    model = BessModel()
    with pytest.raises(ValueError, match="rated_energy_kwh"):
        model.init_state({"rated_energy_kwh": 0.0}, {})
    state = model.init_state({"rated_energy_kwh": 200.0}, {})
    with pytest.raises(ValueError, match="dt_s"):
        model.step(state, {"p_batt_kw": 1.0}, -1.0)


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
    assert result.data_basis == "illustrative_assumptions"
    assert result.calibration_status == "uncalibrated"


def test_diagnostics_and_measured_data_calibration_metrics():
    state = SiteTwinState(
        site_id="test-site",
        asof="2026-11-25T12:00:00Z",
        assets={
            "BESS_01": AssetTwinState(
                status="ok",
                freshness_s=30,
                confidence="verified",
                residuals={"soc_residual_pct": 6.0},
            )
        },
        energy_balance=EnergyBalance(imbalance_kw=4.0),
    )
    findings = evaluate_twin_state(
        state,
        DiagnosticThresholds(
            max_asset_freshness_s=60,
            max_abs_imbalance_kw=3,
            max_underperformance_pct=20,
            max_abs_soc_residual_pct=5,
        ),
    )
    assert {finding.code for finding in findings} == {
        "BATTERY_SOC_RESIDUAL",
        "ENERGY_BALANCE_MISMATCH",
    }

    metrics = evaluate_soc_predictions([80, 75], [78, 76], max_abs_error_pct=3)
    assert metrics.mae == pytest.approx(1.5)
    assert metrics.rmse == pytest.approx((5 / 2) ** 0.5)
    assert metrics.pass_threshold

    pv_metrics = estimate_pv_instantaneous_pr(
        measured_ac_kw=[45, 50, 0],
        poa_w_m2=[900, 1000, 100],
        capacity_kwp=60,
        minimum_poa_w_m2=200,
    )
    assert pv_metrics.sample_count == 2
    assert pv_metrics.median_instantaneous_pr == pytest.approx(0.8333333333)
    assert pv_metrics.status == "measured_data_diagnostic_not_model_fit"


def test_persistence_forecast_uses_only_good_numeric_samples():
    result = persistence_forecast(
        [
            {"ts": "2026-11-25T10:00:00+00:00", "value_num": 75.0, "quality": 0},
            {"ts": "2026-11-25T11:00:00+00:00", "value_num": 80.0, "quality": 2},
        ],
        horizon_steps=2,
        step_s=3600,
    )
    assert result["method"] == "persistence_baseline"
    assert result["calibration_status"] == "uncalibrated"
    assert [point["value"] for point in result["points"]] == [75.0, 75.0]
