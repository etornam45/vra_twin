"""Asset models, nameplate specifications, and semantic cross-field validators."""

from typing import Any, Dict, List, Literal, Optional
from pydantic import Field, model_validator
from .common import BaseContract, SignConvention, TagClass


class BaseAssetConfig(BaseContract):
    """Base configuration for assets."""
    pass


class BessWarranty(BaseContract):
    years: int = Field(..., ge=1)
    min_soh_pct: float = Field(..., ge=0, le=100)
    cycles_at_dod: Optional[Dict[str, Any]] = None


class BessCapacityTest(BaseContract):
    date: str
    capacity_kwh: float = Field(..., gt=0)
    temp_c: float
    c_rate: float = Field(..., gt=0)


class BessConfig(BaseAssetConfig):
    """Battery Energy Storage System (BESS) configuration."""
    chemistry: str = "LFP"
    rated_energy_kwh: float = Field(..., gt=0)
    usable_energy_kwh: float = Field(..., gt=0)
    max_charge_kw: float = Field(..., gt=0)
    max_discharge_kw: float = Field(..., gt=0)
    soc_min_pct: float = Field(10.0, ge=0, le=50)
    soc_max_pct: float = Field(95.0, ge=50, le=100)
    temp_limits_c: Dict[str, float] = Field(default_factory=lambda: {
        "charge_min": 5.0,
        "charge_max": 45.0,
        "discharge_min": -10.0,
        "discharge_max": 55.0
    })
    warranty: Optional[BessWarranty] = None
    reference_capacity_test: Optional[BessCapacityTest] = None

    @model_validator(mode="after")
    def validate_bess_semantics(self) -> "BessConfig":
        if self.soc_min_pct >= self.soc_max_pct:
            raise ValueError(f"soc_min_pct ({self.soc_min_pct}) must be less than soc_max_pct ({self.soc_max_pct})")
        if self.usable_energy_kwh > self.rated_energy_kwh:
            raise ValueError(f"usable_energy_kwh ({self.usable_energy_kwh}) cannot exceed rated_energy_kwh ({self.rated_energy_kwh})")
        return self


class PvArrayConfig(BaseAssetConfig):
    """PV Array configuration."""
    capacity_kwp: float = Field(..., gt=0)
    tilt: float = Field(10.0, ge=0, le=90)
    azimuth: float = Field(180.0, ge=0, le=360)
    temp_coeff_pct_c: float = Field(-0.35, le=0)
    noct_c: float = Field(45.0, gt=0)
    annual_degradation_pct: float = Field(0.5, ge=0, le=5)
    baseline_soiling_pct: float = Field(2.0, ge=0, le=50)


class InverterConfig(BaseAssetConfig):
    """PV Inverter configuration."""
    rated_ac_kw: float = Field(..., gt=0)
    mppt_count: int = Field(1, ge=1)
    max_dc_v: float = Field(..., gt=0)
    efficiency_curve: Optional[List[List[float]]] = None
    curtailment_support: bool = True
    ramp_rate_kw_s: Optional[float] = None


class PcsConfig(BaseAssetConfig):
    """Power Conversion System (PCS) bidirectional inverter config."""
    rated_kva: float = Field(..., gt=0)
    rated_kw: float = Field(..., gt=0)
    grid_forming: bool = True
    droop_support: bool = True
    remote_setpoints_allowed: bool = False


class MeterConfig(BaseAssetConfig):
    """Electrical meter configuration."""
    role: Literal["pcc", "generation", "feeder", "customer"] = "pcc"
    ct_ratio: Optional[float] = 1.0
    accuracy_class: Optional[str] = "0.5S"
    direction_convention: SignConvention = SignConvention.AS_IS


class FeederConfig(BaseAssetConfig):
    """Distribution feeder configuration."""
    rated_current_a: float = Field(..., gt=0)
    length_km: float = Field(..., gt=0)
    conductor: str = "Copper/Aluminium"
    customers_count: int = Field(0, ge=0)
    remote_operable: bool = False


class LoadGroupConfig(BaseAssetConfig):
    """Mini-grid load group configuration."""
    category: Literal["residential", "commercial", "productive", "public"] = "residential"
    customers_count: int = Field(0, ge=0)
    connected_kw: float = Field(..., ge=0)
    growth_annual_pct: float = Field(5.0, ge=0)
    deferrable_kw: float = Field(0.0, ge=0)


class WeatherStationConfig(BaseAssetConfig):
    """Sensors and weather station config."""
    height_m: float = Field(2.0, gt=0)
    sensors: List[str] = Field(default_factory=lambda: ["ghi", "poa", "t_amb", "t_module"])
    cleaning_interval_days: int = Field(14, ge=1)


class AssetIdentity(BaseContract):
    manufacturer: str
    model: str
    serial: Optional[str] = None
    commissioned_on: Optional[str] = None
    firmware: Optional[str] = None


class AssetConnections(BaseContract):
    bus_id: Optional[str] = None
    parent: Optional[str] = None
    children: List[str] = Field(default_factory=list)


class AssetDefinition(BaseContract):
    """Generic asset contract."""
    asset_id: str
    asset_type: Literal[
        "bess",
        "pv_array",
        "pv_inverter",
        "pcs",
        "meter",
        "feeder",
        "load_group",
        "weather_station",
        "genset",
        "gateway"
    ]
    name: str
    identity: AssetIdentity
    config: Dict[str, Any]
    connections: AssetConnections = Field(default_factory=AssetConnections)
    revision: int = 1
