"""What-If Simulation Engine & Battery Replacement Planner.

Section 6.4 & 6.7 Implementation:
Simulates multi-year scenarios:
- Load growth scenarios (e.g. 5-15% annual demand growth)
- Battery calendar + cyclic degradation trajectory
- End-of-Life (EOL SOH 70%) replacement window projection
- Unserved energy and loss of load hours (LOLH)
"""

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from twin.src.models.pv import PvArrayModel
from twin.src.models.battery import BessModel


class SimulationScenario(BaseModel):
    scenario_id: str
    site_id: str
    annual_load_growth_pct: float = Field(8.0, ge=0.0, le=50.0)
    sim_years: int = Field(5, ge=1, le=20)
    pv_kwp: float = Field(60.0, gt=0.0)
    bess_kwh: float = Field(200.0, gt=0.0)
    eol_soh_threshold_pct: float = Field(70.0, ge=50.0, le=85.0)


class SimulationResult(BaseModel):
    scenario_id: str
    site_id: str
    years_simulated: int
    yearly_unserved_energy_kwh: List[float]
    yearly_lolh_hours: List[float]
    soh_trajectory: List[float]
    replacement_year: Optional[int] = None
    eol_reached: bool = False
    recommendation: str


class WhatIfSimulationEngine:
    def __init__(self):
        self.pv_model = PvArrayModel()
        self.bess_model = BessModel()

    def run_scenario(self, scenario: SimulationScenario) -> SimulationResult:
        pv_state = self.pv_model.init_state({"capacity_kwp": scenario.pv_kwp}, {})
        bess_state = self.bess_model.init_state({
            "rated_energy_kwh": scenario.bess_kwh,
            "usable_energy_kwh": scenario.bess_kwh * 0.90
        }, {})

        yearly_unserved = []
        yearly_lolh = []
        soh_trajectory = [100.0]
        replacement_year = None
        base_daily_kwh = 180.0  # Volta Lake typical island daily consumption

        for year in range(1, scenario.sim_years + 1):
            growth_factor = (1.0 + (scenario.annual_load_growth_pct / 100.0)) ** year
            daily_load = base_daily_kwh * growth_factor

            # Representative day simulation (hourly steps: 24h)
            day_unserved = 0.0
            day_lolh = 0.0

            for hour in range(24):
                # Solar profile: peak at 12:00
                solar_factor = max(0.0, math.sin((hour - 6) / 12.0 * math.pi)) if 6 <= hour <= 18 else 0.0
                poa = solar_factor * 950.0
                pv_state, pv_out = self.pv_model.step(pv_state, {"poa": poa}, dt_s=3600.0)
                pv_gen = pv_out["p_ac_est"]

                # Load profile: evening peak
                hourly_load_share = 0.03 if (0 <= hour <= 6) else (0.04 if hour <= 17 else 0.08)
                load_kw = daily_load * hourly_load_share

                # Power balance
                net_p = load_kw - pv_gen  # > 0 means battery must discharge
                bess_state, bess_out = self.bess_model.step(bess_state, {"p_batt_kw": net_p}, dt_s=3600.0)

                # Check if unserved energy occurred
                if bess_state["soc"] <= 10.0 and net_p > 0:
                    unserved_kw = net_p
                    day_unserved += unserved_kw * 1.0
                    day_lolh += 1.0

            year_unserved_total = day_unserved * 365.0
            year_lolh_total = day_lolh * 365.0

            yearly_unserved.append(round(year_unserved_total, 1))
            yearly_lolh.append(round(year_lolh_total, 1))

            current_soh = round(bess_state["soh"], 1)
            soh_trajectory.append(current_soh)

            if current_soh <= scenario.eol_soh_threshold_pct and replacement_year is None:
                replacement_year = year

        eol_reached = replacement_year is not None
        rec = (
            f"BESS reaches EOL threshold ({scenario.eol_soh_threshold_pct}%) in Year {replacement_year}. Schedule cell replacement."
            if eol_reached
            else f"BESS retains adequate SOH ({soh_trajectory[-1]}%) over {scenario.sim_years}-year horizon."
        )

        return SimulationResult(
            scenario_id=scenario.scenario_id,
            site_id=scenario.site_id,
            years_simulated=scenario.sim_years,
            yearly_unserved_energy_kwh=yearly_unserved,
            yearly_lolh_hours=yearly_lolh,
            soh_trajectory=soh_trajectory,
            replacement_year=replacement_year,
            eol_reached=eol_reached,
            recommendation=rec
        )
