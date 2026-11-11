"""Simulated Island SCADA and Modbus Server for VLK-ISL-001.

Simulates:
- PV Array / Inverter: PV_INV_01 (Device ID: 1)
- Battery BMS: BESS_01 (Device ID: 2)
- PCS (Bidirectional Inverter): PCS_01 (Device ID: 3)
- Grid / Feeder Meter: MTR_PCC (Device ID: 4)

Registers (Holding Registers):
PV_INV_01 (Device 1):
  - 100: p_ac (kW * 10)
  - 101: v_ac (V * 10)
  - 102: f_ac (Hz * 100)
  - 103: p_dc (kW * 10)
  - 104: e_today (kWh * 10)

BESS_01 (Device 2):
  - 200: soc (% * 10)
  - 201: p_dc (kW * 10, signed: - charge, + discharge)
  - 202: v_dc (V * 10)
  - 203: i_dc (A * 10)
  - 204: t_max (C * 10)
  - 205: soh (% * 10)

PCS_01 (Device 3):
  - 300: p_ac (kW * 10)
  - 301: q_ac (kvar * 10)
  - 302: mode (1=grid-forming, 2=following)

MTR_PCC (Device 4):
  - 400: p_active (kW * 10)
  - 401: v_grid (V * 10)
  - 402: f_grid (Hz * 100)
"""

import asyncio
import math
import time
from typing import Dict, List, Tuple
from pymodbus.server import StartAsyncTcpServer
from pymodbus.simulator import SimData, SimDevice, DataType


class IslandGridSimulator:
    """Simulates physical mini-grid dynamic state."""
    def __init__(self):
        self.t = 0.0
        self.pv_kwp = 60.0
        self.bess_capacity_kwh = 200.0
        self.soc = 75.0  # %
        self.base_load_kw = 25.0

    def step(self, dt_s: float = 1.0):
        self.t += dt_s
        # 1. Solar irradiance diurnal cycle (0 to 1 scaling)
        solar_factor = max(0.0, math.sin(self.t / 100.0))
        pv_p_ac = self.pv_kwp * solar_factor * 0.85
        pv_p_dc = pv_p_ac / 0.96

        # 2. Fluctuating load
        load_noise = 5.0 * math.sin(self.t / 15.0) + 2.0 * math.cos(self.t / 7.0)
        p_load = max(5.0, self.base_load_kw + load_noise)

        # 3. Energy balance: Net power to/from battery (discharge +, charge -)
        p_batt = p_load - pv_p_ac

        # Battery limits
        if self.soc >= 98.0 and p_batt < 0:
            p_batt = 0.0
        elif self.soc <= 15.0 and p_batt > 0:
            p_batt = 0.0

        # Update SOC
        delta_kwh = (p_batt * (dt_s / 3600.0))
        self.soc = max(10.0, min(100.0, self.soc - (delta_kwh / self.bess_capacity_kwh) * 100.0))

        v_dc = 48.0 + (self.soc / 100.0) * 6.0
        i_dc = (p_batt * 1000.0) / v_dc if v_dc > 0 else 0.0
        t_max = 28.0 + (abs(p_batt) / 50.0) * 10.0

        return {
            "pv": {
                "p_ac": pv_p_ac,
                "v_ac": 230.0 + 2.0 * math.sin(self.t / 20.0),
                "f_ac": 50.0 + 0.1 * math.sin(self.t / 30.0),
                "p_dc": pv_p_dc,
                "e_today": 120.0 + self.t * 0.01,
            },
            "bess": {
                "soc": self.soc,
                "p_dc": p_batt,
                "v_dc": v_dc,
                "i_dc": i_dc,
                "t_max": t_max,
                "soh": 98.5,
            },
            "pcs": {
                "p_ac": p_batt,
                "q_ac": 1.5,
                "mode": 1,
            },
            "meter": {
                "p_active": p_load,
                "v_grid": 230.0,
                "f_grid": 50.0,
            }
        }


def create_simulated_devices() -> Tuple[List[SimDevice], Dict[int, SimData]]:
    """Builds 4 SimDevice instances with holding register blocks."""
    devices = []
    hr_blocks = {}

    for dev_id in (1, 2, 3, 4):
        c = [SimData(0, count=1, values=[False], datatype=DataType.BITS)]
        d = [SimData(0, count=1, values=[False], datatype=DataType.BITS)]
        h = [SimData(0, count=600, values=[0] * 600, datatype=DataType.REGISTERS)]
        i = [SimData(0, count=1, values=[0], datatype=DataType.REGISTERS)]
        dev = SimDevice(dev_id, (c, d, h, i))
        devices.append(dev)
        hr_blocks[dev_id] = h[0]

    return devices, hr_blocks


async def update_registers_loop(hr_blocks: Dict[int, SimData], sim: IslandGridSimulator):
    """Continuously runs the physics sim and updates holding registers."""
    while True:
        try:
            state = sim.step(1.0)

            # Update PV (Device 1)
            pv = state["pv"]
            h1 = hr_blocks[1]
            h1.values[100] = int(pv["p_ac"] * 10)
            h1.values[101] = int(pv["v_ac"] * 10)
            h1.values[102] = int(pv["f_ac"] * 100)
            h1.values[103] = int(pv["p_dc"] * 10)
            h1.values[104] = int(pv["e_today"] * 10)

            # Update BESS (Device 2)
            bess = state["bess"]
            h2 = hr_blocks[2]
            h2.values[200] = int(bess["soc"] * 10)
            h2.values[201] = int(bess["p_dc"] * 10) & 0xFFFF
            h2.values[202] = int(bess["v_dc"] * 10)
            h2.values[203] = int(bess["i_dc"] * 10) & 0xFFFF
            h2.values[204] = int(bess["t_max"] * 10)
            h2.values[205] = int(bess["soh"] * 10)

            # Update PCS (Device 3)
            pcs = state["pcs"]
            h3 = hr_blocks[3]
            h3.values[300] = int(pcs["p_ac"] * 10) & 0xFFFF
            h3.values[301] = int(pcs["q_ac"] * 10)
            h3.values[302] = int(pcs["mode"])

            # Update Meter (Device 4)
            mtr = state["meter"]
            h4 = hr_blocks[4]
            h4.values[400] = int(mtr["p_active"] * 10)
            h4.values[401] = int(mtr["v_grid"] * 10)
            h4.values[402] = int(mtr["f_grid"] * 100)

        except Exception as e:
            print(f"Error in simulation update loop: {e}")

        await asyncio.sleep(1.0)


async def run_server(host="127.0.0.1", port=5020):
    print(f"Starting VRA SCADA Modbus TCP Server on {host}:{port} ...")
    devices, hr_blocks = create_simulated_devices()
    sim = IslandGridSimulator()

    asyncio.create_task(update_registers_loop(hr_blocks, sim))

    await StartAsyncTcpServer(
        context=devices,
        address=(host, port)
    )


if __name__ == "__main__":
    asyncio.run(run_server())
