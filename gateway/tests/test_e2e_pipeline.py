"""End-to-end tests for simulator polling, durable delivery, and backend twin updates."""

import asyncio
import shutil
import socket
import subprocess
import threading
import time

import pytest
import paho.mqtt.client as mqtt

pymodbus_server = pytest.importorskip("pymodbus.server")
from contracts.src.catalog import RegisterMapEntry
from contracts.src.common import SignConvention
from backend.src.ingest import IngestionService
from backend.src.repository import BackendRepository
from gateway.src.adapters.modbus_tcp import ModbusDeviceAdapter
from gateway.src.forward import Forwarder
from gateway.src.store import GatewayStorage
from gateway.src.encoder import TelemetryEncoder
from twin.src.state import LiveTwinSynchronizer
from tools.scada_sim.server import create_simulated_devices, update_registers_loop, IslandGridSimulator


StartAsyncTcpServer = pymodbus_server.StartAsyncTcpServer


def test_end_to_end_sim_adapter_store(tmp_path):
    asyncio.run(_async_test_e2e(tmp_path))


async def _async_test_e2e(tmp_path):
    host = "127.0.0.1"
    port = _free_tcp_port()

    # 1. Start Modbus TCP SCADA server
    devices, hr_blocks = create_simulated_devices()
    sim = IslandGridSimulator()
    sim_task = asyncio.create_task(update_registers_loop(hr_blocks, sim))

    server_task = asyncio.create_task(
        StartAsyncTcpServer(context=devices, address=(host, port))
    )

    await asyncio.to_thread(_wait_for_port, host, port)

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
        _, n_samples, t_first, t_last = encoder.build_and_compress_batch(
            series_data=series_data,
            t0_ms=t0_ms
        )

        # 5. Commit to SQLite Outbox
        db_file = tmp_path / "gateway.db"
        store = GatewayStorage(db_file)
        seq = store.allocate_seq_and_insert_batch(
            stream="telemetry",
            topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
            payload=lambda seq: encoder.build_and_compress_batch(
                series_data=series_data,
                t0_ms=t0_ms,
                seq=seq
            )[0],
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
        await asyncio.gather(sim_task, server_task, return_exceptions=True)


def _free_tcp_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(host, port, process=None, timeout_s=10):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"mosquitto exited with status {process.returncode}")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"service did not listen on {host}:{port}")


def test_end_to_end_outage_replay_backend_ack_and_live_twin(tmp_path):
    if not shutil.which("mosquitto"):
        pytest.skip("mosquitto is required for the broker-backed integration test")
    asyncio.run(_async_test_full_pipeline(tmp_path))


async def _async_test_full_pipeline(tmp_path):
    host = "127.0.0.1"
    scada_port = _free_tcp_port()
    broker_port = _free_tcp_port()
    mosquitto = None
    sim_task = None
    server_task = None
    forwarder_task = None
    forwarder = None
    adapter = None
    backend_client = None

    try:
        devices, hr_blocks = create_simulated_devices()
        sim_task = asyncio.create_task(
            update_registers_loop(hr_blocks, IslandGridSimulator())
        )
        server_task = asyncio.create_task(
            StartAsyncTcpServer(context=devices, address=(host, scada_port))
        )
        await asyncio.to_thread(_wait_for_port, host, scada_port)

        registers = [
            RegisterMapEntry(
                asset="BESS_01", tag="soc", register=200, type="uint16",
                scale=0.1, unit="%", sign=SignConvention.AS_IS
            ),
            RegisterMapEntry(
                asset="BESS_01", tag="p_dc", register=201, type="int16",
                scale=0.1, unit="kW", sign=SignConvention.AS_IS
            )
        ]
        adapter = ModbusDeviceAdapter(
            asset_id="BESS_01", host=host, port=scada_port, unit_id=2,
            registers=registers
        )
        store = GatewayStorage(tmp_path / "gateway.db")
        encoder = TelemetryEncoder("VLK-ISL-001", "GW-VLK-001")

        backend_repo = BackendRepository(tmp_path / "backend.db")
        backend_repo.register_site(
            "VLK-ISL-001", "Volta Lake Island 001", 7.8, -0.05, "GW-VLK-001"
        )
        backend_repo.register_asset(
            "VLK-ISL-001", "BESS_01", "bess", "Main Battery", {}, {}
        )
        ingestion = IngestionService(backend_repo)
        twin_sync = LiveTwinSynchronizer(backend_repo, "VLK-ISL-001")
        ingested_sequences = []
        ingested_event_sequences = []
        callback_errors = []
        backend_subscribed = threading.Event()
        received_lock = threading.Lock()

        backend_client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="vra-test-backend"
        )

        def on_backend_connect(client, userdata, flags, reason_code, properties):
            if reason_code != 0:
                callback_errors.append(f"backend MQTT connect failed: {reason_code}")
                return
            client.subscribe(
                [
                    ("vra/VLK-ISL-001/telemetry/GW-VLK-001", 1),
                    ("vra/VLK-ISL-001/events/GW-VLK-001", 1)
                ]
            )

        def on_backend_subscribe(client, userdata, mid, reason_code_list, properties):
            if any(reason_code.is_failure for reason_code in reason_code_list):
                callback_errors.append(
                    f"backend MQTT subscribe failed: {reason_code_list}"
                )
                return
            backend_subscribed.set()

        def on_backend_disconnect(client, userdata, disconnect_flags, reason_code, properties):
            backend_subscribed.clear()

        def on_telemetry(client, userdata, message):
            try:
                if message.topic.endswith("/events/GW-VLK-001"):
                    ack = ingestion.process_raw_event_payload(message.payload)
                    if not ack.ranges or ack.rejected:
                        raise AssertionError(f"backend rejected gap event: {ack}")
                    with received_lock:
                        ingested_event_sequences.append(ack.ranges[0][0])
                else:
                    ack = ingestion.process_raw_telemetry_payload(message.payload)
                    if ack.rejected or len(ack.ranges) != 1:
                        raise AssertionError(f"backend rejected telemetry batch: {ack}")
                    with received_lock:
                        ingested_sequences.append(ack.ranges[0][0])
                    twin_sync.sync_live_state()
                client.publish(
                    "vra/VLK-ISL-001/ack/GW-VLK-001",
                    ack.model_dump_json(),
                    qos=1
                )
            except Exception as exc:
                callback_errors.append(repr(exc))

        backend_client.on_connect = on_backend_connect
        backend_client.on_subscribe = on_backend_subscribe
        backend_client.on_disconnect = on_backend_disconnect
        backend_client.on_message = on_telemetry

        def start_broker():
            process = subprocess.Popen(
                ["mosquitto", "-p", str(broker_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            _wait_for_port(host, broker_port, process)
            return process

        mosquitto = await asyncio.to_thread(start_broker)
        backend_client.connect(host, broker_port, keepalive=30)
        backend_client.loop_start()

        async def wait_until(predicate, description, timeout_s=10):
            deadline = asyncio.get_running_loop().time() + timeout_s
            while asyncio.get_running_loop().time() < deadline:
                if predicate():
                    return
                if callback_errors:
                    raise AssertionError("; ".join(callback_errors))
                await asyncio.sleep(0.05)
            raise AssertionError(f"timed out waiting for {description}")

        await wait_until(backend_subscribed.is_set, "backend MQTT subscription")

        def make_forwarder():
            return Forwarder(
                db=store,
                site_id="VLK-ISL-001",
                gateway_id="GW-VLK-001",
                broker_host=host,
                broker_port=broker_port,
                ack_timeout_s=5
            )

        forwarder = make_forwarder()
        forwarder_task = asyncio.create_task(forwarder.forwarder_loop())
        await wait_until(
            lambda: forwarder is not None and forwarder.link_state == "ONLINE",
            "gateway MQTT connection"
        )

        async def queue_sample():
            readings = await adapter.poll()
            assert len(readings) == 2
            t0_ms = min(reading["ts_ms"] for reading in readings)
            series_data = {}
            for reading in readings:
                series_data[(reading["asset"], reading["tag"], reading["u"])] = {
                    "t": [reading["ts_ms"] - t0_ms],
                    "v": [reading["v"]],
                    "q": [int(reading["q"])]
                }
            store.allocate_seq_and_insert_batch(
                stream="telemetry",
                topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
                payload=lambda seq: encoder.build_and_compress_batch(
                    series_data, t0_ms, seq=seq
                )[0],
                n_samples=len(readings),
                t_first_ms=t0_ms,
                t_last_ms=max(reading["ts_ms"] for reading in readings)
            )
            return readings

        await queue_sample()
        await wait_until(
            lambda: store.get_queue_stats()["acked"] == 1,
            "first batch ACK"
        )

        mosquitto.terminate()
        mosquitto.wait(timeout=5)
        mosquitto = None
        await wait_until(
            lambda: forwarder.link_state == "OFFLINE",
            "gateway detecting broker outage"
        )
        assert not backend_subscribed.is_set()

        mosquitto = await asyncio.to_thread(start_broker)
        await wait_until(
            backend_subscribed.is_set,
            "backend resubscribing after broker restart"
        )
        await wait_until(
            lambda: forwarder.link_state == "ONLINE",
            "gateway automatically reconnecting"
        )

        mosquitto.terminate()
        mosquitto.wait(timeout=5)
        mosquitto = None
        await wait_until(
            lambda: forwarder.link_state == "OFFLINE",
            "gateway detecting second broker outage"
        )

        # Persist data while disconnected, then restart the gateway forwarder.
        await queue_sample()
        await queue_sample()
        assert store.get_queue_stats()["pending"] == 2
        gap_id = store.add_data_gap(
            "BESS_01", "soc", int(time.time() * 1000), "disk_pressure"
        )
        gap = next(
            gap for gap in store.get_unreported_data_gaps()
            if gap["gap_id"] == gap_id
        )
        store.queue_data_gap_event(
            gap, site_id="VLK-ISL-001", gateway_id="GW-VLK-001"
        )

        forwarder.stop()
        await asyncio.wait_for(forwarder_task, timeout=5)
        forwarder_task = None
        forwarder = None

        mosquitto = await asyncio.to_thread(start_broker)
        await wait_until(
            backend_subscribed.is_set,
            "backend resubscribing after broker restart"
        )

        forwarder = make_forwarder()
        forwarder_task = asyncio.create_task(forwarder.forwarder_loop())
        await wait_until(
            lambda: store.get_queue_stats()["acked"] == 4,
            "outage batches and gap event to be replayed and ACKed",
            timeout_s=15
        )

        assert not callback_errors
        assert sorted(ingested_sequences) == [1, 2, 3]
        assert ingested_event_sequences == [1]
        assert backend_repo.get_twin_state("VLK-ISL-001") is not None
        with backend_repo._get_connection() as conn:
            gap_events = conn.execute(
                "SELECT code FROM events WHERE event_id = ?",
                (gap_id,)
            ).fetchall()
        assert len(gap_events) == 1
        assert gap_events[0]["code"] == "TELEMETRY_DROPPED"
        timeseries = backend_repo.query_timeseries(
            "VLK-ISL-001", "BESS_01", "soc"
        )
        assert timeseries
        assert 10.0 <= timeseries[-1]["value_num"] <= 100.0
    finally:
        if forwarder_task is not None:
            if forwarder is not None:
                forwarder.stop()
            await asyncio.gather(forwarder_task, return_exceptions=True)
        if adapter is not None:
            await adapter.close()
        if backend_client is not None:
            backend_client.disconnect()
            backend_client.loop_stop()
        for task in (sim_task, server_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (sim_task, server_task) if task is not None),
            return_exceptions=True
        )
        if mosquitto is not None:
            mosquitto.terminate()
            try:
                mosquitto.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mosquitto.kill()
                mosquitto.wait(timeout=5)
