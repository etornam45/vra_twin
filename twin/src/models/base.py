"""Base protocol for digital twin component behavioral models.

Section 6.1 Implementation:
Same model used across:
- Live twin state stepping & estimation
- Replay validation
- Forecast & What-if simulation
"""

from typing import Any, Dict, Protocol, Tuple, runtime_checkable


@runtime_checkable
class AssetModel(Protocol):
    model_id: str
    version: str

    def params_schema(self) -> Dict[str, Any]:
        """Returns JSON-schema compatible parameter dictionary."""
        ...

    def init_state(self, asset_config: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """Initializes internal model dynamic state."""
        ...

    def step(
        self,
        state: Dict[str, Any],
        inputs: Dict[str, Any],
        dt_s: float
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Steps dynamic simulation forward by dt_s.

        Returns (new_state, outputs).
        """
        ...

    def estimate(
        self,
        state: Dict[str, Any],
        measurements: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, float]]:
        """Reconciles internal state with live telemetry measurements.

        Returns (estimated_state, residuals).
        """
        ...
