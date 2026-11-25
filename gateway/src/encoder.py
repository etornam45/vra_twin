"""Batch encoder and deadband filter for gateway telemetry.

Features:
- Columnar delta-time compression (t ms offset from t0_ms)
- Deadband filtering (value changes < deadband are ignored unless max_interval_s elapsed)
- Zstandard compression
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple
import zstandard as zstd

from contracts.src.messages import TelemetryBatch, SeriesColumn


def sample_thresholds(
    deadband: float,
    max_interval_s: int,
    pressure_pct: float,
    downsample_pressure_pct: float,
    drop_pressure_pct: float
) -> Optional[Tuple[float, int]]:
    """Return normal, relaxed, or drop thresholds for the current queue pressure."""
    if pressure_pct >= drop_pressure_pct:
        return None
    if pressure_pct >= downsample_pressure_pct:
        return deadband * 2, max_interval_s * 2
    return deadband, max_interval_s


def should_emit_sample(
    value: Optional[float],
    quality: int,
    sample_ms: int,
    previous_value: Optional[float],
    previous_quality: Optional[int],
    last_sent_ms: Optional[int],
    deadband: float,
    max_interval_s: int
) -> bool:
    if last_sent_ms is None:
        return True
    if quality != previous_quality or value is None or previous_value is None:
        return True
    if sample_ms - last_sent_ms >= max_interval_s * 1000:
        return True
    if deadband == 0:
        return True
    return abs(value - previous_value) >= deadband


class TelemetryEncoder:
    def __init__(self, site_id: str, gateway_id: str, cfg_version: str = "v1.0"):
        self.site_id = site_id
        self.gateway_id = gateway_id
        self.cfg_version = cfg_version
        self.cctx = zstd.ZstdCompressor(level=3)
        self.dctx = zstd.ZstdDecompressor()

    def build_and_compress_batch(
        self,
        series_data: Dict[Tuple[str, str, str], Dict[str, list]],
        t0_ms: int,
        seq: int = 0,
        clock_quality: str = "synced",
        clock_offset_ms: int = 0
    ) -> Tuple[bytes, int, int, int]:
        """Builds a TelemetryBatch and compresses it with zstd.

        Returns (compressed_bytes, total_samples, t_first_ms, t_last_ms)
        """
        series_cols: List[SeriesColumn] = []
        total_samples = 0
        min_t_offset = 0
        max_t_offset = 0

        for (asset, tag, unit), data in series_data.items():
            if not data["t"]:
                continue
            col = SeriesColumn(
                asset=asset,
                tag=tag,
                u=unit,
                t=data["t"],
                v=data["v"],
                q=data["q"]
            )
            series_cols.append(col)
            total_samples += len(data["t"])
            min_t_offset = min(min_t_offset, min(data["t"]))
            max_t_offset = max(max_t_offset, max(data["t"]))

        batch = TelemetryBatch(
            site_id=self.site_id,
            gateway_id=self.gateway_id,
            seq=seq,
            t0_ms=t0_ms,
            clock_quality=clock_quality,
            clock_offset_ms=clock_offset_ms,
            cfg_version=self.cfg_version,
            series=series_cols
        )

        batch_json = batch.model_dump_json()
        compressed = self.cctx.compress(batch_json.encode("utf-8"))

        t_first_ms = t0_ms + min_t_offset
        t_last_ms = t0_ms + max_t_offset

        return compressed, total_samples, t_first_ms, t_last_ms

    def decompress_batch(self, compressed_bytes: bytes) -> Dict[str, Any]:
        decompressed = self.dctx.decompress(compressed_bytes)
        return json.loads(decompressed.decode("utf-8"))
