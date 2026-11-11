"""Site configuration and gateway configuration models."""

from typing import Any, Dict, List, Literal, Optional, Tuple
from pydantic import Field, model_validator
from .common import BaseContract, SignConvention, TagClass


class UplinkProfile(str):
    NORMAL = "normal"
    SLOW_LINK = "slow_link"
    METERED = "metered"


class UplinkConfig(BaseContract):
    broker: str
    port: int = 8883
    use_tls: bool = True
    profile: Literal["normal", "slow_link", "metered"] = "normal"
    live_window_s: int = Field(120, ge=10)
    ack_timeout_s: int = Field(30, ge=5)
    inflight_max: int = Field(5, ge=1, le=50)
    backfill_max_kbps: int = Field(256, ge=16)


class DeviceConnection(BaseContract):
    asset: str
    protocol: Literal["modbus_tcp", "modbus_rtu", "opcua", "vendor_rest", "csv_tail"]
    host: Optional[str] = None
    port: Optional[int] = None
    unit_id: Optional[int] = None
    serial_port: Optional[str] = None
    baudrate: Optional[int] = None
    poll_interval_s: float = Field(1.0, gt=0)


class CommandsConfig(BaseContract):
    enabled: bool = False
    rate_limit_per_min: int = Field(6, ge=1)
    readback_timeout_s: int = Field(15, ge=1)
    allowed_types: List[str] = Field(default_factory=list)


class GatewayConfig(BaseContract):
    """Configuration stored in gateway.yaml on the edge device."""
    gateway: str
    uplink: UplinkConfig
    devices: List[DeviceConnection] = Field(default_factory=list)
    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    disk_budget_mb: int = Field(2048, ge=256)
    downsample_pressure_pct: float = Field(70.0, ge=50.0, le=95.0)
    drop_pressure_pct: float = Field(95.0, ge=80.0, le=99.0)


class SiteAssetEntry(BaseContract):
    type: str
    catalog: Optional[str] = None
    kwp: Optional[float] = None
    tilt: Optional[float] = None
    azimuth: Optional[float] = None
    inverter: Optional[str] = None
    pcs: Optional[str] = None
    meter: Optional[str] = None
    feeder: Optional[str] = None
    customers: Optional[int] = None
    category: Optional[str] = None
    role: Optional[str] = None
    waive: List[str] = Field(default_factory=list)
    extra: Dict[str, Any] = Field(default_factory=dict)


class SiteConfig(BaseContract):
    """Site configuration stored in site.yaml."""
    site: str
    name: str
    timezone: str = "Africa/Accra"
    location: Tuple[float, float] = Field(..., description="(latitude, longitude)")
    gateway: str
    assets: Dict[str, SiteAssetEntry] = Field(default_factory=dict)
