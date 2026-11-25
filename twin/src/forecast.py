"""Persistence baseline forecasting, explicitly labeled as uncalibrated."""

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


def persistence_forecast(
    history: List[Dict[str, Any]],
    horizon_steps: int,
    step_s: int,
) -> Dict[str, Any]:
    if not 1 <= horizon_steps <= 288:
        raise ValueError("horizon_steps must be between 1 and 288")
    if not 1 <= step_s <= 86400:
        raise ValueError("step_s must be between 1 and 86400")
    usable = [
        row for row in history
        if row.get("value_num") is not None
        and int(row.get("quality", 0)) == 0
        and math.isfinite(float(row["value_num"]))
    ]
    if not usable:
        raise ValueError("No good-quality numeric measurements are available to forecast")
    latest = max(usable, key=lambda row: row["ts"])
    origin = datetime.fromisoformat(str(latest["ts"]).replace("Z", "+00:00"))
    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=timezone.utc)
    value = float(latest["value_num"])
    return {
        "method": "persistence_baseline",
        "calibration_status": "uncalibrated",
        "provenance": "predicted",
        "origin_ts": origin.isoformat(),
        "sample_count": len(usable),
        "points": [
            {
                "ts": (origin + timedelta(seconds=step_s * index)).isoformat(),
                "value": value,
            }
            for index in range(1, horizon_steps + 1)
        ],
    }
