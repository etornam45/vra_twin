"""Backend Ingestion Service for telemetry streams.

Section 2.4 & 3.9 Implementation:
- Ingestion contract: validate envelope, check lengths
- Decompresses zstandard payloads
- Commits batch to repository BEFORE issuing selective ACK
- Returns selective ACK ranges: e.g. [[128700, 128734]]
- Quarantines / rejects poison batches with specific failure reasons
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import zstandard as zstd

from contracts.src.messages import TelemetryBatch, SelectiveAck, SelectiveAckReject
from backend.src.repository import BackendRepository


class IngestionService:
    def __init__(self, repo: BackendRepository):
        self.repo = repo
        self.dctx = zstd.ZstdDecompressor()

    def process_raw_telemetry_payload(
        self,
        payload_bytes: bytes,
        is_compressed: bool = True
    ) -> SelectiveAck:
        """Processes raw incoming MQTT payload and produces a validated SelectiveAck."""
        now_iso = datetime.now(timezone.utc).isoformat()

        # 1. Decompress if necessary
        try:
            if is_compressed:
                raw_json = self.dctx.decompress(payload_bytes).decode("utf-8")
            else:
                raw_json = payload_bytes.decode("utf-8")
            data = json.loads(raw_json)
        except Exception as e:
            return SelectiveAck(
                site_id="UNKNOWN",
                gateway_id="UNKNOWN",
                ranges=[],
                rejected=[SelectiveAckReject(seq=0, reason="decompression_or_json_error", detail=str(e))],
                committed_at=now_iso
            )

        # 2. Validate batch model
        try:
            batch = TelemetryBatch.model_validate(data)
        except Exception as e:
            gateway_id = data.get("gateway_id", "UNKNOWN")
            site_id = data.get("site_id", "UNKNOWN")
            seq = data.get("seq", 0)
            return SelectiveAck(
                site_id=site_id,
                gateway_id=gateway_id,
                ranges=[],
                rejected=[SelectiveAckReject(seq=seq, reason="schema_validation_error", detail=str(e))],
                committed_at=now_iso
            )

        # 3. Commit to database before acknowledging
        try:
            success, reason = self.repo.ingest_batch(batch)
            if not success:
                return SelectiveAck(
                    site_id=batch.site_id,
                    gateway_id=batch.gateway_id,
                    ranges=[],
                    rejected=[SelectiveAckReject(seq=batch.seq, reason=reason or "db_insert_failed")],
                    committed_at=now_iso
                )

            # 4. Generate selective ACK range [seq, seq]
            return SelectiveAck(
                site_id=batch.site_id,
                gateway_id=batch.gateway_id,
                stream=batch.stream,
                ranges=[(batch.seq, batch.seq)],
                rejected=[],
                committed_at=now_iso
            )

        except Exception as e:
            return SelectiveAck(
                site_id=batch.site_id,
                gateway_id=batch.gateway_id,
                ranges=[],
                rejected=[SelectiveAckReject(seq=batch.seq, reason="unhandled_db_exception", detail=str(e))],
                committed_at=now_iso
            )
