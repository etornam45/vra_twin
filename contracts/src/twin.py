"""Twin state models, energy balance, and live asset state representation."""

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import Field
from .common import BaseContract, Provenance, QualityCode


class TagValue(BaseContract):
    v: Optional[Union[float, str, int, bool]] = None
    u: Optional[str] = None
    t: str
    q: QualityCode = QualityCode.GOOD
    p: Provenance = Provenance.MEASURED


class AssetTwinState(BaseContract):
    status: Literal["ok", "warning", "fault", "offline", "unknown"] = "unknown"
    freshness_s: float = Field(0.0, ge=0)
    confidence: Literal["verified", "unverified", "estimated"] = "unverified"
    values: Dict[str, TagValue] = Field(default_factory=dict)
    derived: Dict[str, TagValue] = Field(default_factory=dict)
    residuals: Dict[str, float] = Field(default_factory=dict)
    active_alarms: List[str] = Field(default_factory=list)


class EnergyBalance(BaseContract):
    pv_kw: float = 0.0
    bess_kw: float = 0.0  # discharge +, charge -
    genset_kw: float = 0.0
    load_kw: float = 0.0
    losses_kw: float = 0.0
    imbalance_kw: float = 0.0
    renewable_fraction_pct: float = Field(100.0, ge=0.0, le=100.0)


class SiteTwinState(BaseContract):
    """Canonical live digital twin state."""
    schema: Literal["twin.state/1.0"] = "twin.state/1.0"
    site_id: str
    asof: str
    mode: Literal["live", "replay", "forecast", "what_if", "advisory"] = "live"
    link_state: Literal["online", "backfilling", "degraded", "offline"] = "offline"
    assets: Dict[str, AssetTwinState] = Field(default_factory=dict)
    energy_balance: EnergyBalance = Field(default_factory=EnergyBalance)
