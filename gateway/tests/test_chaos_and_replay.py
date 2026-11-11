"""Tests for network outage replay, store-and-forward, and disk pressure handling."""

import time
import pytest
from pathlib import Path

from tools.netem.chaos import FlappingLinkController
from gateway.src.store import GatewayStorage


def test_network_chaos_controller():
    controller = FlappingLinkController(up_duration_s=0.1, down_duration_s=0.1)
    assert controller.is_online is True

    # Test link state toggle
    controller.is_online = False
    assert controller.is_online is False
    controller.stop_flapping(set_online=True)
    assert controller.is_online is True


def test_disk_pressure_queue_monitoring(tmp_path):
    db_file = tmp_path / "gw_pressure.db"
    store = GatewayStorage(db_file)

    # Insert 10 batches
    for i in range(1, 11):
        store.allocate_seq_and_insert_batch(
            stream="telemetry",
            topic="vra/VLK-ISL-001/telemetry/GW-VLK-001",
            payload=b"dummy-payload-" + str(i).encode(),
            n_samples=5,
            t_first_ms=1000 * i,
            t_last_ms=1000 * i + 500,
            priority=5
        )

    stats = store.get_queue_stats()
    assert stats["pending"] == 10
    assert stats["acked"] == 0

    # Simulate link restoration and selective ack of odd sequence numbers
    store.mark_acked_ranges("telemetry", [(1, 3), (7, 9)])
    stats_after_ack = store.get_queue_stats()
    assert stats_after_ack["acked"] == 6
    assert stats_after_ack["pending"] == 4
