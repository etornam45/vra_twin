"""Comprehensive integration tests for Gateway SQLite Store, Encoder, and Outbox."""

import os
import time
import pytest
from pathlib import Path

from contracts.src.common import QualityCode
from contracts.src.messages import SelectiveAck
from gateway.src.store import GatewayStorage
from gateway.src.encoder import TelemetryEncoder


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
