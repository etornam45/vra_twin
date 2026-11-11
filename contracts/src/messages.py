"""Uplink and downlink message contracts for MQTT / edge transport."""

from typing import Any, Dict, List, Literal, Optional, Tuple
from pydantic import Field, model_validator
from .common import BaseContract, QualityCode


class SeriesColumn(BaseContract):
    """Columnar time-series segment for an individual asset tag."""
    asset: str = Field(..., description="Asset identifier (e.g. BESS_01)")
    tag: str = Field(..., description="Canonical tag name (e.g. soc, p_dc)")
    u: str = Field(..., description="Unit of measurement (e.g. %, kW, V)")
    t: List[int] = Field(..., description="Millisecond offsets from batch t0_ms")
    v: List[Optional[float]] = Field(..., description="Measured / sampled values")
    q: List[int] = Field(..., description="Quality codes per sample (0=good, 1=uncertain, 2=bad, 3=stale)")

    @model_validator(mode="after")
    def validate_lengths(self) -> "SeriesColumn":
        n_t = len(self.t)
        n_v = len(self.v)
        n_q = len(self.q)
        if not (n_t == n_v == n_q):
            raise ValueError(f"Series length mismatch for {self.asset}.{self.tag}: t({n_t}), v({n_v}), q({n_q}) must match")
        return self


class TelemetryBatch(BaseContract):
    """Columnar compressed telemetry batch payload."""
    schema: Literal["telemetry.batch/1.0"] = "telemetry.batch/1.0"
    site_id: str = Field(..., description="Site identifier (e.g. VLK-ISL-001)")
    gateway_id: str = Field(..., description="Gateway identifier (e.g. GW-VLK-001)")
    stream: Literal["telemetry"] = "telemetry"
    seq: int = Field(..., ge=0, description="Monotonically increasing sequence number per stream")
    t0_ms: int = Field(..., ge=0, description="Base epoch timestamp in milliseconds")
    clock_quality: Literal["synced", "unsynced"] = "synced"
    clock_offset_ms: int = Field(0, description="Estimated NTP offset in milliseconds")
    cfg_version: str = Field(..., description="Active configuration version string")
    resolution: Literal["raw", "agg_1m", "agg_5m"] = "raw"
    series: List[SeriesColumn] = Field(default_factory=list)


class SelectiveAckReject(BaseContract):
    """Specific rejection details for a poison or invalid batch."""
    seq: int = Field(..., ge=0)
    reason: str
    detail: Optional[str] = None


class SelectiveAck(BaseContract):
    """Selective ACK returned by the backend after database commit."""
    schema: Literal["ack/1.0"] = "ack/1.0"
    site_id: str
    gateway_id: str
    stream: str = "telemetry"
    ranges: List[Tuple[int, int]] = Field(
        default_factory=list,
        description="Inclusive sequence ranges committed to DB, e.g. [[10, 20], [25, 25]]"
    )
    rejected: List[SelectiveAckReject] = Field(
        default_factory=list,
        description="Batches explicitly rejected (quarantine by gateway)"
    )
    committed_at: str


class EventMessage(BaseContract):
    """System event, alarm, or data gap message."""
    schema: Literal["event/1.0"] = "event/1.0"
    event_id: str = Field(..., description="UUID or unique event identifier")
    site_id: str
    gateway_id: str
    stream: Literal["events"] = "events"
    seq: int = Field(..., ge=0)
    kind: Literal[
        "alarm_raise",
        "alarm_clear",
        "state_change",
        "comms_lost",
        "comms_restored",
        "data_gap",
        "gateway",
        "config_applied",
        "command"
    ]
    asset_id: Optional[str] = None
    code: str
    source_code: Optional[str] = None
    severity: Literal["info", "warning", "high", "critical"] = "info"
    message: str
    ts: str
    cleared_ts: Optional[str] = None
    context: Dict[str, Any] = Field(default_factory=dict)


class GatewayHealth(BaseContract):
    """Gateway heartbeat / health status report."""
    schema: Literal["gateway.health/1.0"] = "gateway.health/1.0"
    gateway_id: str
    ts: str
    boot_id: str
    uptime_s: int
    version: str
    cfg_version: str
    link: Dict[str, Any]
    queue: Dict[str, Any]
    store: Dict[str, Any]
    adapters: List[Dict[str, Any]] = Field(default_factory=list)
    clock: Dict[str, Any]
    host: Dict[str, Any]


class CommandMessage(BaseContract):
    """Signed remote command issued from central backend."""
    schema: Literal["command/1.0"] = "command/1.0"
    cmd_id: str
    site_id: str
    gateway_id: str
    asset_id: str
    type: str
    command_class: Literal["A", "B", "C", "D", "X"] = Field(alias="class")
    params: Dict[str, Any] = Field(default_factory=dict)
    reason: str
    simulation_ref: Optional[str] = None
    requested_by: str
    issued_at: str
    expires_at: str
    revert_after_s: Optional[int] = None
    signature: str
    key_id: str


class CommandResult(BaseContract):
    """Command execution result returned by gateway."""
    schema: Literal["command.result/1.0"] = "command.result/1.0"
    cmd_id: str
    site_id: str
    gateway_id: str
    state: Literal[
        "RECEIVED",
        "REJECTED",
        "EXPIRED",
        "APPLYING",
        "APPLIED",
        "FAILED",
        "CONFIRMED"
    ]
    reason: Optional[str] = None
    applied_at: Optional[str] = None
    readback: Dict[str, Any] = Field(default_factory=dict)
    finished_ms: Optional[int] = None
