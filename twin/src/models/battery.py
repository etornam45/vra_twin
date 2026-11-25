"""Battery Energy Storage System (BESS) Equivalent Circuit & Ageing model.

Prototype implementation of the BESS state and ageing model:
- Dynamic SOC tracking with coulombic & round-trip efficiency
- Calendar fade is linear in elapsed time and accelerated by a configurable Q10
  temperature factor.
- Cycle fade is linear in equivalent full cycles (EFC).

The default ageing parameters are illustrative and are not site- or vendor-calibrated.
"""

import math
from typing import Any, Dict, Tuple


class BessModel:
    model_id = "battery.ecm_ageing"
    version = "1.1"

    DEFAULT_PARAMS = {
        "charge_eff": 0.95,
        "discharge_eff": 0.95,
        "initial_soh_pct": 100.0,
        "calendar_fade_pct_per_year": 2.0,
        "cycle_fade_pct_per_1000_efc": 2.5,
        "temperature_q10": 2.0,
        "reference_temp_c": 25.0,
        "eol_soh_pct": 70.0,
    }

    def params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "charge_eff": {"type": "number", "default": 0.95, "minimum": 0.0, "maximum": 1.0},
                "discharge_eff": {"type": "number", "default": 0.95, "minimum": 0.0, "maximum": 1.0},
                "initial_soh_pct": {"type": "number", "default": 100.0, "minimum": 0.0, "maximum": 100.0},
                "calendar_fade_pct_per_year": {"type": "number", "default": 2.0, "minimum": 0.0},
                "cycle_fade_pct_per_1000_efc": {"type": "number", "default": 2.5, "minimum": 0.0},
                "temperature_q10": {"type": "number", "default": 2.0, "minimum": 0.0},
                "reference_temp_c": {"type": "number", "default": 25.0},
                "eol_soh_pct": {"type": "number", "default": 70.0, "minimum": 0.0, "maximum": 100.0},
            }
        }

    def init_state(self, asset_config: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        rated = float(asset_config.get("rated_energy_kwh", 200.0))
        usable = float(asset_config.get("usable_energy_kwh", 180.0))
        if not math.isfinite(rated) or rated <= 0:
            raise ValueError("rated_energy_kwh must be a finite positive number")
        if not math.isfinite(usable) or not 0 < usable <= rated:
            raise ValueError("usable_energy_kwh must be positive and no greater than rated_energy_kwh")
        model_params = {**self.DEFAULT_PARAMS, **params}
        for key, value in model_params.items():
            if not math.isfinite(float(value)):
                raise ValueError(f"{key} must be finite")
        for key in ("charge_eff", "discharge_eff"):
            if not 0 < float(model_params[key]) <= 1:
                raise ValueError(f"{key} must be greater than 0 and no greater than 1")
        for key in (
            "calendar_fade_pct_per_year",
            "cycle_fade_pct_per_1000_efc",
        ):
            if float(model_params[key]) < 0:
                raise ValueError(f"{key} must be non-negative")
        if float(model_params["temperature_q10"]) <= 0:
            raise ValueError("temperature_q10 must be greater than 0")
        initial_soh = float(model_params["initial_soh_pct"])
        if not 0 <= initial_soh <= 100:
            raise ValueError("initial_soh_pct must be between 0 and 100")
        initial_soc = float(asset_config.get("initial_soc_pct", 75.0))
        if not math.isfinite(initial_soc) or not 0 <= initial_soc <= 100:
            raise ValueError("initial_soc_pct must be finite and between 0 and 100")
        return {
            "rated_energy_kwh": rated,
            "usable_energy_kwh": usable,
            "soc": initial_soc,
            "soh": initial_soh,
            "throughput_kwh": 0.0,
            "equivalent_cycles": 0.0,
            "elapsed_s": 0.0,
            "temp_c": float(model_params["reference_temp_c"]),
            "params": model_params,
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
        if not math.isfinite(p_batt):
            raise ValueError("p_batt_kw must be finite")
        if not math.isfinite(dt_s) or dt_s < 0:
            raise ValueError("dt_s must be finite and non-negative")
        params = state.get("params", self.DEFAULT_PARAMS)
        rated_kwh = float(state["rated_energy_kwh"])
        current_soh = float(state["soh"])
        effective_capacity_kwh = rated_kwh * (current_soh / 100.0)
        if effective_capacity_kwh <= 0:
            raise ValueError("battery effective capacity must be positive")
        eff_chg = float(params["charge_eff"])
        eff_dis = float(params["discharge_eff"])

        # SOC dynamic step:
        # P_batt > 0 (discharge) => Energy extracted = P_batt * dt / eff_dis
        # P_batt < 0 (charge)    => Energy stored = abs(P_batt) * dt * eff_chg
        if p_batt > 0:
            delta_kwh = (p_batt * (dt_s / 3600.0)) / eff_dis
            new_soc = state["soc"] - (delta_kwh / effective_capacity_kwh) * 100.0
        else:
            delta_kwh = (abs(p_batt) * (dt_s / 3600.0)) * eff_chg
            new_soc = state["soc"] + (delta_kwh / effective_capacity_kwh) * 100.0

        new_soc = max(0.0, min(100.0, new_soc))

        energy_flow = abs(p_batt) * (dt_s / 3600.0)
        new_throughput = state["throughput_kwh"] + energy_flow
        new_cycles = new_throughput / (2.0 * rated_kwh)
        previous_cycles = float(state["equivalent_cycles"])
        temp_c = float(inputs.get("t_max", inputs.get("t_amb", state["temp_c"])))
        if not math.isfinite(temp_c) or not -80 <= temp_c <= 200:
            raise ValueError("battery temperature must be finite and between -80 and 200 C")
        q10 = float(params["temperature_q10"])
        temperature_factor = q10 ** ((temp_c - float(params["reference_temp_c"])) / 10.0)
        calendar_fade = (
            float(params["calendar_fade_pct_per_year"])
            * (dt_s / (365.25 * 24.0 * 3600.0))
            * temperature_factor
        )
        cycle_fade = (
            float(params["cycle_fade_pct_per_1000_efc"])
            * (new_cycles - previous_cycles)
            / 1000.0
        )
        new_soh = max(0.0, current_soh - calendar_fade - cycle_fade)

        new_state = {
            "rated_energy_kwh": state["rated_energy_kwh"],
            "usable_energy_kwh": state["usable_energy_kwh"],
            "soc": new_soc,
            "soh": new_soh,
            "throughput_kwh": new_throughput,
            "equivalent_cycles": new_cycles,
            "elapsed_s": float(state.get("elapsed_s", 0.0)) + dt_s,
            "temp_c": temp_c,
            "params": params,
        }

        outputs = {
            "soc_est": new_soc,
            "soh_est": new_soh,
            "temp_c_est": temp_c,
            "available_energy_kwh": rated_kwh * (new_soh / 100.0) * (new_soc / 100.0),
            "equivalent_cycles": new_cycles,
            "calendar_fade_pct": calendar_fade,
            "cycle_fade_pct": cycle_fade,
            "eol_reached": new_soh <= float(params["eol_soh_pct"]),
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
            if not math.isfinite(soc_meas) or not 0 <= soc_meas <= 100:
                raise ValueError("measured soc must be finite and between 0 and 100")
            residuals["soc_residual_pct"] = soc_meas - state["soc"]
            # Weighted observer update
            new_state["soc"] = 0.85 * soc_meas + 0.15 * state["soc"]

        # Reconcile temperature
        if "t_max" in measurements and measurements["t_max"] is not None:
            t_meas = float(measurements["t_max"])
            if not math.isfinite(t_meas):
                raise ValueError("measured t_max must be finite")
            residuals["temp_residual_c"] = t_meas - state["temp_c"]
            new_state["temp_c"] = t_meas

        return new_state, residuals
