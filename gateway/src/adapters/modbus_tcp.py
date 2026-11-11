"""Modbus TCP Polling Adapter for Gateway Edge Ingestion."""

import asyncio
import time
from typing import Any, Dict, List, Optional
from pymodbus.client import AsyncModbusTcpClient

from contracts.src.common import QualityCode
from contracts.src.catalog import RegisterMapEntry


class ModbusDeviceAdapter:
    def __init__(self, asset_id: str, host: str, port: int, unit_id: int, registers: List[RegisterMapEntry]):
        self.asset_id = asset_id
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.registers = registers
        self.client: Optional[AsyncModbusTcpClient] = None
        self.last_poll_ts = 0.0

    async def connect(self) -> bool:
        self.client = AsyncModbusTcpClient(self.host, port=self.port)
        return await self.client.connect()

    async def poll(self) -> List[Dict[str, Any]]:
        """Polls mapped registers and returns normalised canonical tag readings."""
        if not self.client or not self.client.connected:
            connected = await self.connect()
            if not connected:
                return [{
                    "asset": self.asset_id,
                    "tag": reg.tag,
                    "v": None,
                    "u": reg.unit,
                    "q": QualityCode.BAD,
                    "ts_ms": int(time.time() * 1000)
                } for reg in self.registers]

        results = []
        now_ms = int(time.time() * 1000)

        for reg in self.registers:
            try:
                # Read holding register (device_id for slave/unit ID in pymodbus 3.x)
                rr = await self.client.read_holding_registers(
                    reg.register,
                    count=1,
                    device_id=self.unit_id
                )
                if rr.isError():
                    results.append({
                        "asset": self.asset_id,
                        "tag": reg.tag,
                        "v": None,
                        "u": reg.unit,
                        "q": QualityCode.BAD,
                        "ts_ms": now_ms
                    })
                    continue

                raw_val = rr.registers[0]

                # Convert signed 16-bit if configured or requested
                if reg.type == "int16" and raw_val >= 32768:
                    raw_val -= 65536

                # Apply scaling
                scaled_val = raw_val * reg.scale

                # Apply sign convention inversion if needed
                if reg.sign == "invert":
                    scaled_val = -scaled_val

                # Range check
                quality = QualityCode.GOOD
                if reg.min_val is not None and scaled_val < reg.min_val:
                    quality = QualityCode.UNCERTAIN
                if reg.max_val is not None and scaled_val > reg.max_val:
                    quality = QualityCode.UNCERTAIN

                results.append({
                    "asset": self.asset_id,
                    "tag": reg.tag,
                    "v": scaled_val,
                    "u": reg.unit,
                    "q": quality,
                    "ts_ms": now_ms
                })

            except Exception:
                results.append({
                    "asset": self.asset_id,
                    "tag": reg.tag,
                    "v": None,
                    "u": reg.unit,
                    "q": QualityCode.BAD,
                    "ts_ms": now_ms
                })

        return results

    async def close(self):
        if self.client:
            self.client.close()
