"""End-to-end integration test demonstrating:
1. SCADA Modbus TCP server emitting simulated mini-grid physical state.
2. Modbus adapter polling the registers.
3. TelemetryEncoder packing and compressing columnar delta series into zstd.
4. GatewayStorage atomically persisting to SQLite outbox.
5. Inflight marking and selective ACK range processing.
"""

import asyncio
import time
import pytest
from pathlib import Path

from tools.scada_sim.server import create_simulated_devices, update_registers_loop, IslandGridSimulator
from pymodbus.server import StartAsyncTcpServer
from contracts.src.catalog import RegisterMapEntry
from contracts.src.common import SignConvention, TagClass
from gateway.src.adapters.modbus_tcp import ModbusDeviceAdapter
from gateway.src.store import GatewayStorage
from gateway.src.encoder import TelemetryEncoder


def test_end_to_end_sim_adapter_store(tmp_path):
    asyncio.run(_async_test_e2e(tmp_path))


async def _async_test_e2e(tmp_path):
    port = 5025
    host = "127.0.0.1"

    # 1. Start Modbus TCP SCADA server
    devices, hr_blocks = create_simulated_devices()
    sim = IslandGridSimulator()
    sim_task = asyncio.create_task(update_registers_loop(hr_blocks, sim))

    server_task = asyncio.create_task(
        StartAsyncTcpServer(context=devices, address=(host, port))
    )

    # Allow server to initialize
    await asyncio.sleep(0.5)

    try:
        # 2. Setup Modbus Adapter for BESS_01 (Unit 2)
        registers = [
            RegisterMapEntry(
                asset="BESS_01",
                tag="soc",
                register=200,
                type="uint16",
                scale=0.1,
                unit="%",
                sign=SignConvention.AS_IS
            ),
            RegisterMapEntry(
                asset="BESS_01",
                tag="p_dc",
                register=201,
                type="int16",
                scale=0.1,
                unit="kW",
                sign=SignConvention.AS_IS
            )
        ]
        adapter = ModbusDeviceAdapter(
            asset_id="BESS_01",
            host=host,
            port=port,
            unit_id=2,
            registers=registers
        )

        # 3. Poll data
        readings = await adapter.poll()
        assert len(readings) == 2
        soc_reading = next(r for r in readings if r["tag"] == "soc")
        assert soc_reading["v"] is not None
        assert 10.0 <= soc_reading["v"] <= 100.0
        await adapter.close()

        # 4. Encode into compressed batch
        encoder = TelemetryEncoder(site_id="VLK-ISL-001", gateway_id="GW-VLK-001")
        t0_ms = int(time.time() * 1000)
        series_data = {
            ("BESS_01", "soc", "%"): {
                "t": [0],
                "v": [soc_reading["v"]],
                "q": [int(soc_reading["q"])]
            }
        }
        compressed, n_samples, t_first, t_last = encoder.build_and_compress_batch(
            series_data=series_data,
            t0_ms=t0_ms
        )

        # 5. Commit to SQLite Outbox
        db_file = tmp_path / "gateway.db"
        store = GatewayStorage(db_file)
        seq = store.allocate_seq_and_insert_batch(
            stream="telemetry",
            topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
            payload=compressed,
            n_samples=n_samples,
            t_first_ms=t_first,
            t_last_ms=t_last
        )
        assert seq == 1

        # 6. Verify Outbox queue
        batch = store.next_to_send()
        assert batch is not None
        assert batch.seq == 1
        store.mark_inflight(batch.stream, batch.seq)

        # 7. Process selective ACK
        store.mark_acked_ranges("telemetry", [(1, 1)])
        stats = store.get_queue_stats()
        assert stats["acked"] == 1
        assert stats["inflight"] == 0

    finally:
        sim_task.cancel()
        server_task.cancel()
