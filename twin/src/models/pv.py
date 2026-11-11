"""PV Array behavioral model.

Section 6.2 Implementation:
- Irradiance & module temperature transposition
- Performance ratio and temperature coefficient derating
- Residual computation: measured vs modeled DC/AC power
"""

from typing import Any, Dict, Tuple
from .base import AssetModel


class PvArrayModel:
    model_id = "pv.single_diode_derate"
    version = "1.0"

    def params_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "temp_coeff_pct_c": {"type": "number", "default": -0.35},
                "soiling_loss_pct": {"type": "number", "default": 2.0},
                "mismatch_loss_pct": {"type": "number", "default": 1.5},
                "inverter_eff": {"type": "number", "default": 0.96}
            }
        }

    def init_state(self, asset_config: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "kwp": float(asset_config.get("capacity_kwp", asset_config.get("kwp", 60.0))),
            "cumulative_kwh": 0.0,
            "pr_actual": 0.82
        }

    def step(
        self,
        state: Dict[str, Any],
        inputs: Dict[str, Any],
        dt_s: float
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Inputs:

        - poa: Plane of array irradiance (W/m2)
        - t_module: Module temperature (deg C, default 45C or ambient + delta)
        """
        poa = max(0.0, float(inputs.get("poa", inputs.get("ghi", 0.0))))
        t_mod = float(inputs.get("t_module", 45.0))
        kwp = state["kwp"]

        # Standard test condition (STC): 1000 W/m2, 25 deg C
        temp_factor = 1.0 + ((-0.35 / 100.0) * (t_mod - 25.0))
        derate = (1.0 - 0.035) * temp_factor  # 3.5% soiling & mismatch

        p_dc_modeled = (poa / 1000.0) * kwp * derate
        p_ac_modeled = p_dc_modeled * 0.96

        new_kwh = state["cumulative_kwh"] + (p_ac_modeled * (dt_s / 3600.0))
        new_state = dict(state)
        new_state["cumulative_kwh"] = new_kwh

        outputs = {
            "p_dc_est": p_dc_modeled,
            "p_ac_est": p_ac_modeled,
            "derate_factor": derate
        }
        return new_state, outputs

    def estimate(
        self,
        state: Dict[str, Any],
        measurements: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, float]]:
        residuals = {}
        if "p_ac" in measurements and "p_ac_est" in measurements:
            residuals["p_ac_residual_kw"] = measurements["p_ac"] - measurements["p_ac_est"]
            if measurements["p_ac_est"] > 5.0:
                residuals["underperformance_pct"] = max(0.0, (measurements["p_ac_est"] - measurements["p_ac"]) / measurements["p_ac_est"] * 100)
        return state, residuals
