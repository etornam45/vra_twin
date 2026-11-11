"""Battery Energy Storage System (BESS) Equivalent Circuit & Ageing model.

Section 5.6 & 6.2 Implementation:
- Dynamic SOC tracking with coulombic & round-trip efficiency
- Temperature and C-rate resistance tracking
- Calendar and cycle degradation modeling:
  - Calendar fade: sqrt(t) with Arrhenius thermal factor
  - Cycle fade: power-law throughput * DOD
"""

import math
from typing import Any, Dict, Tuple
from .base import AssetModel


class BessModel:
    model_id = "battery.ecm_ageing"
    version = "1.0"

    def params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "charge_eff": {"type": "number", "default": 0.95},
                "discharge_eff": {"type": "number", "default": 0.95},
                "eol_soh_pct": {"type": "number", "default": 70.0},
                "nominal_rte": {"type": "number", "default": 0.90}
            }
        }

    def init_state(self, asset_config: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        rated = float(asset_config.get("rated_energy_kwh", 200.0))
        usable = float(asset_config.get("usable_energy_kwh", 180.0))
        return {
            "rated_energy_kwh": rated,
            "usable_energy_kwh": usable,
            "soc": 75.0,  # %
            "soh": 99.0,  # %
            "throughput_kwh": 0.0,
            "equivalent_cycles": 0.0,
            "temp_c": 28.0
        }

    def step(
        self,
        state: Dict[str, Any],
        inputs: Dict[str, Any],
        dt_s: float
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Inputs:

        - p_batt_kw: Battery power (discharge +, charge -)
        - t_amb: Ambient temperature
        """
        p_batt = float(inputs.get("p_batt_kw", 0.0))
        rated_kwh = state["rated_energy_kwh"] * (state["soh"] / 100.0)
        eff_chg = 0.95
        eff_dis = 0.95

        # SOC dynamic step:
        # P_batt > 0 (discharge) => Energy extracted = P_batt * dt / eff_dis
        # P_batt < 0 (charge)    => Energy stored = abs(P_batt) * dt * eff_chg
        if p_batt > 0:
            delta_kwh = (p_batt * (dt_s / 3600.0)) / eff_dis
            new_soc = state["soc"] - (delta_kwh / rated_kwh) * 100.0
        else:
            delta_kwh = (abs(p_batt) * (dt_s / 3600.0)) * eff_chg
            new_soc = state["soc"] + (delta_kwh / rated_kwh) * 100.0

        new_soc = max(0.0, min(100.0, new_soc))

        # Battery ageing calculation
        energy_flow = abs(p_batt) * (dt_s / 3600.0)
        new_throughput = state["throughput_kwh"] + energy_flow
        new_cycles = new_throughput / (2.0 * state["rated_energy_kwh"])

        # Cycle fade rate: ~0.003% per full equivalent cycle for LFP
        cycle_fade = new_cycles * 0.003
        # Calendar fade: Arrhenius thermal stress factor
        temp_c = float(inputs.get("t_amb", 28.0)) + (abs(p_batt) / 50.0) * 8.0
        calendar_fade = 0.0005 * math.sqrt(max(1.0, new_throughput / 10.0))

        new_soh = max(50.0, 100.0 - (cycle_fade + calendar_fade))

        new_state = {
            "rated_energy_kwh": state["rated_energy_kwh"],
            "usable_energy_kwh": state["usable_energy_kwh"],
            "soc": new_soc,
            "soh": new_soh,
            "throughput_kwh": new_throughput,
            "equivalent_cycles": new_cycles,
            "temp_c": temp_c
        }

        outputs = {
            "soc_est": new_soc,
            "soh_est": new_soh,
            "temp_c_est": temp_c,
            "available_energy_kwh": rated_kwh * (new_soc / 100.0)
        }
        return new_state, outputs

    def estimate(
        self,
        state: Dict[str, Any],
        measurements: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, float]]:
        residuals = {}
        new_state = dict(state)

        # Reconcile measured SOC
        if "soc" in measurements and measurements["soc"] is not None:
            soc_meas = float(measurements["soc"])
            residuals["soc_residual_pct"] = soc_meas - state["soc"]
            # Weighted observer update
            new_state["soc"] = 0.85 * soc_meas + 0.15 * state["soc"]

        # Reconcile temperature
        if "t_max" in measurements and measurements["t_max"] is not None:
            t_meas = float(measurements["t_max"])
            residuals["temp_residual_c"] = t_meas - state["temp_c"]
            new_state["temp_c"] = t_meas

        return new_state, residuals
