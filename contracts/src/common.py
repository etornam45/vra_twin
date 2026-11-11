"""Common enums, types, and base models for VRA Island Digital Twin contracts."""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class QualityCode(int, Enum):
    """Signal quality flags aligned to IEC / canonical specifications."""
    GOOD = 0
    UNCERTAIN = 1
    BAD = 2
    STALE = 3
    SUBSTITUTED = 4


class Provenance(str, Enum):
    """Data provenance indicator."""
    MEASURED = "measured"
    CALCULATED = "calculated"
    ESTIMATED = "estimated"
    PREDICTED = "predicted"
    SIMULATED = "simulated"


class TagClass(str, Enum):
    """Tag polling / telemetry update classification."""
    FAST = "fast"          # 1s - 10s (e.g. electrical instant values)
    STANDARD = "standard"  # 10s - 60s (e.g. temperatures, SOC)
    SLOW = "slow"          # 5m - 15m (e.g. energy counters, environmental)
    EVENT = "event"        # On change / alarm


class SignConvention(str, Enum):
    """Sign convention mapping for power/energy points."""
    AS_IS = "as_is"
    INVERT = "invert"


class BaseContract(BaseModel):
    """Base contract with standard configuration."""
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
        protected_namespaces=()
    )
