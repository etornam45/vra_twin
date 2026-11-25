"""Comprehensive integration tests for Gateway SQLite Store, Encoder, and Outbox."""

import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

from contracts.src.common import QualityCode
from contracts.src.messages import SelectiveAck
from gateway.src.store import GatewayStorage
from gateway.src.encoder import TelemetryEncoder, sample_thresholds, should_emit_sample


def test_sqlite_store_sequence_and_outbox(tmp_path):
    db_file = tmp_path / "gateway.db"
    store = GatewayStorage(db_file)

    # 1. Allocate sequence and insert batch
    payload = b"test-compressed-payload-1"
    seq1 = store.allocate_seq_and_insert_batch(
        stream="telemetry",
        topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
        payload=payload,
        n_samples=10,
        t_first_ms=1000,
        t_last_ms=2000,
        priority=5
    )
    assert seq1 == 1

    seq2 = store.allocate_seq_and_insert_batch(
        stream="telemetry",
        topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
        payload=b"test-compressed-payload-2",
        n_samples=15,
        t_first_ms=2000,
        t_last_ms=3000,
        priority=5
    )
    assert seq2 == 2

    # 2. Check next to send
    batch = store.next_to_send()
    assert batch is not None
    assert batch.seq == 1
    assert batch.payload == payload

    # 3. Mark inflight
    store.mark_inflight(batch.stream, batch.seq)
    stats = store.get_queue_stats()
    assert stats["pending"] == 1
    assert stats["inflight"] == 1

    # 4. Mark acked range [1, 1]
    store.mark_acked_ranges("telemetry", [(1, 1)])
    stats = store.get_queue_stats()
    assert stats["inflight"] == 0
    assert stats["acked"] == 1
    assert stats["pending"] == 1

    # 5. Check next to send is seq 2
    batch2 = store.next_to_send()
    assert batch2 is not None
    assert batch2.seq == 2


def test_telemetry_encoder_and_decompression():
    encoder = TelemetryEncoder(site_id="VLK-ISL-001", gateway_id="GW-VLK-001")
    series_data = {
        ("BESS_01", "soc", "%"): {
            "t": [0, 1000, 2000],
            "v": [78.0, 78.1, 78.2],
            "q": [0, 0, 0]
        },
        ("PV_INV_01", "p_ac", "kW"): {
            "t": [0, 1000, 2000],
            "v": [42.1, 42.5, 43.0],
            "q": [0, 0, 0]
        }
    }
    t0_ms = int(time.time() * 1000)
    compressed, n_samples, t_first, t_last = encoder.build_and_compress_batch(
        series_data=series_data,
        t0_ms=t0_ms
    )

    assert n_samples == 6
    assert t_first == t0_ms
    assert t_last == t0_ms + 2000
    assert len(compressed) > 0

    # Decompress and verify content
    decompressed = encoder.decompress_batch(compressed)
    assert decompressed["site_id"] == "VLK-ISL-001"
    assert decompressed["gateway_id"] == "GW-VLK-001"
    assert len(decompressed["series"]) == 2


def test_sequence_is_embedded_in_payload_and_allocated_atomically(tmp_path):
    store = GatewayStorage(tmp_path / "gateway.db")
    encoder = TelemetryEncoder("VLK-ISL-001", "GW-VLK-001")
    series_data = {
        ("BESS_01", "soc", "%"): {"t": [0], "v": [78.0], "q": [0]}
    }
    t0_ms = int(time.time() * 1000)

    def insert_batch(_index):
        return store.allocate_seq_and_insert_batch(
            stream="telemetry",
            topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
            payload=lambda seq: encoder.build_and_compress_batch(
                series_data, t0_ms, seq=seq
            )[0],
            n_samples=1,
            t_first_ms=t0_ms,
            t_last_ms=t0_ms
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(insert_batch, range(16)))

    assert sorted(sequences) == list(range(1, 17))
    payload_sequences = []
    with store._get_connection() as conn:
        rows = conn.execute(
            "SELECT seq, payload FROM outbox WHERE stream = 'telemetry' ORDER BY seq"
        ).fetchall()
    for row in rows:
        assert row["seq"] == encoder.decompress_batch(row["payload"])["seq"]
        payload_sequences.append(row["seq"])
    assert payload_sequences == list(range(1, 17))


def test_failed_payload_build_does_not_consume_sequence(tmp_path):
    store = GatewayStorage(tmp_path / "gateway.db")

    def fail_before_insert(_seq):
        raise RuntimeError("serialization failed")

    with pytest.raises(RuntimeError, match="serialization failed"):
        store.allocate_seq_and_insert_batch(
            stream="telemetry",
            topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
            payload=fail_before_insert,
            n_samples=1,
            t_first_ms=1000,
            t_last_ms=1000
        )

    seq = store.allocate_seq_and_insert_batch(
        stream="telemetry",
        topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
        payload=b"valid",
        n_samples=1,
        t_first_ms=1000,
        t_last_ms=1000
    )
    assert seq == 1


def test_deadband_emits_on_change_quality_or_max_interval():
    assert not should_emit_sample(78.1, 0, 10_000, 78.0, 0, 5_000, 0.2, 60)
    assert should_emit_sample(78.2, 0, 10_000, 78.0, 0, 5_000, 0.2, 60)
    assert should_emit_sample(78.0, 1, 10_000, 78.0, 0, 5_000, 0.2, 60)
    assert should_emit_sample(78.0, 0, 65_000, 78.0, 0, 5_000, 0.2, 60)
    assert should_emit_sample(78.0, 0, 10_000, 78.0, 0, 5_000, 0.0, 60)


def test_pressure_thresholds_relax_sampling_then_drop():
    assert sample_thresholds(0.2, 60, 69.9, 70, 95) == (0.2, 60)
    assert sample_thresholds(0.2, 60, 70, 70, 95) == (0.4, 120)
    assert sample_thresholds(0.2, 60, 95, 70, 95) is None


def test_data_gaps_coalesce_and_queue_as_sequenced_event(tmp_path):
    store = GatewayStorage(tmp_path / "gateway.db")
    first_gap = store.add_data_gap("BESS_01", "soc", 1000, "disk_pressure")
    second_gap = store.add_data_gap("BESS_01", "soc", 2000, "disk_pressure")

    assert second_gap == first_gap
    gaps = store.get_unreported_data_gaps()
    assert len(gaps) == 1
    assert (gaps[0]["t_from_ms"], gaps[0]["t_to_ms"]) == (1000, 2000)

    seq = store.queue_data_gap_event(
        gaps[0], site_id="VLK-ISL-001", gateway_id="GW-VLK-001"
    )
    assert seq == 1
    assert store.get_unreported_data_gaps() == []
    event = store.next_to_send()
    assert event is not None
    assert event.stream == "events"
    assert event.seq == seq
    payload = json.loads(event.payload)
    assert payload["kind"] == "data_gap"
    assert payload["context"]["t_from_ms"] == 1000
    assert payload["context"]["t_to_ms"] == 2000


def test_gateway_collection_applies_deadband_pressure_and_gap_policy(tmp_path):
    pytest.importorskip("pymodbus.client")
    from gateway.src.main import GatewayDaemon

    daemon = GatewayDaemon(
        Path("sites/VLK-ISL-001"),
        tmp_path / "gateway.db"
    )
    daemon.load_adapters_from_mapping()
    daemon.gw_cfg = daemon.gw_cfg.model_copy(update={"disk_budget_mb": 1})
    daemon.gw_cfg.downsample_pressure_pct = 50
    daemon.gw_cfg.drop_pressure_pct = 80
    register = daemon.register_map[("BESS_01", "soc")]
    timestamps = iter([
        int(time.time() * 1000),
        int(time.time() * 1000) + 10_000,
        int(time.time() * 1000) + 61_000,
        int(time.time() * 1000) + 62_000,
        int(time.time() * 1000) + 63_000
    ])
    values = iter([78.0, 78.0, 78.0, 78.3, 78.6])

    class FakeAdapter:
        async def poll(self):
            return [{
                "asset": "BESS_01",
                "tag": "soc",
                "v": next(values),
                "u": "%",
                "q": 0,
                "ts_ms": next(timestamps)
            }]

    daemon.adapters = [FakeAdapter()]

    async def collect():
        await daemon.collect_once()

    asyncio.run(collect())
    assert daemon.db.get_queue_stats()["pending"] == 1
    asyncio.run(collect())
    assert daemon.db.get_queue_stats()["pending"] == 1
    asyncio.run(collect())
    assert daemon.db.get_queue_stats()["pending"] == 2

    daemon.db.allocate_seq_and_insert_batch(
        stream="telemetry",
        topic="test",
        payload=b"x" * 600_000,
        n_samples=1,
        t_first_ms=1,
        t_last_ms=1
    )
    asyncio.run(collect())
    gaps = daemon.db.get_unreported_data_gaps()
    assert len(gaps) == 1
    assert gaps[0]["cause"] == "disk_downsample"

    daemon.db.allocate_seq_and_insert_batch(
        stream="telemetry",
        topic="test",
        payload=b"x" * 300_000,
        n_samples=1,
        t_first_ms=1,
        t_last_ms=1
    )
    asyncio.run(collect())
    gaps = daemon.db.get_unreported_data_gaps()
    assert {gap["cause"] for gap in gaps} == {"disk_downsample", "disk_pressure"}

    daemon.db.mark_acked_ranges("telemetry", [(1, 100)])
    daemon.adapters = []
    asyncio.run(collect())
    with daemon.db._get_connection() as conn:
        events = conn.execute(
            "SELECT payload FROM outbox WHERE stream = 'events' ORDER BY seq"
        ).fetchall()
    causes = {
        json.loads(event["payload"])["context"]["cause"]
        for event in events
    }
    assert causes == {"disk_downsample", "disk_pressure"}
