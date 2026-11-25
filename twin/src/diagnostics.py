"""Threshold-explicit, explainable checks over a live twin state."""

import math
from typing import List, Literal

from pydantic import BaseModel, Field

from contracts.src.twin import SiteTwinState


class DiagnosticThresholds(BaseModel):
    max_asset_freshness_s: float = Field(gt=0)
    max_abs_imbalance_kw: float = Field(gt=0)
    max_underperformance_pct: float = Field(gt=0, le=100)
    max_abs_soc_residual_pct: float = Field(gt=0, le=100)


class DiagnosticFinding(BaseModel):
    code: str
    severity: Literal["warning", "critical"]
    message: str
    asset_id: str | None = None
    value: float | None = None
    threshold: float | None = None


def evaluate_twin_state(
    state: SiteTwinState,
    thresholds: DiagnosticThresholds,
) -> List[DiagnosticFinding]:
    findings: List[DiagnosticFinding] = []
    for asset_id, asset in state.assets.items():
        if asset.status in ("fault", "offline"):
            findings.append(DiagnosticFinding(
                code="ASSET_NOT_AVAILABLE",
                severity="critical" if asset.status == "fault" else "warning",
                message=f"Asset status is {asset.status}.",
                asset_id=asset_id,
            ))
        if asset.freshness_s > thresholds.max_asset_freshness_s:
            findings.append(DiagnosticFinding(
                code="ASSET_DATA_STALE",
                severity="warning",
                message="Latest asset measurement exceeds the configured freshness limit.",
                asset_id=asset_id,
                value=asset.freshness_s,
                threshold=thresholds.max_asset_freshness_s,
            ))
        underperformance = asset.residuals.get("underperformance_pct")
        if underperformance is not None and underperformance > thresholds.max_underperformance_pct:
            findings.append(DiagnosticFinding(
                code="PV_UNDERPERFORMANCE",
                severity="warning",
                message="Measured PV underperformance exceeds the configured threshold.",
                asset_id=asset_id,
                value=underperformance,
                threshold=thresholds.max_underperformance_pct,
            ))
        soc_residual = asset.residuals.get("soc_residual_pct")
        if soc_residual is not None and abs(soc_residual) > thresholds.max_abs_soc_residual_pct:
            findings.append(DiagnosticFinding(
                code="BATTERY_SOC_RESIDUAL",
                severity="warning",
                message="Battery model SOC residual exceeds the configured threshold.",
                asset_id=asset_id,
                value=soc_residual,
                threshold=thresholds.max_abs_soc_residual_pct,
            ))
        for alarm in asset.active_alarms:
            findings.append(DiagnosticFinding(
                code=alarm,
                severity="warning",
                message=f"Asset reports active alarm {alarm}.",
                asset_id=asset_id,
            ))

    imbalance = abs(state.energy_balance.imbalance_kw)
    if imbalance > thresholds.max_abs_imbalance_kw:
        findings.append(DiagnosticFinding(
            code="ENERGY_BALANCE_MISMATCH",
            severity="warning",
            message="Absolute power imbalance exceeds the configured threshold.",
            value=imbalance,
            threshold=thresholds.max_abs_imbalance_kw,
        ))
    return findings


class CalibrationMetrics(BaseModel):
    metric: str
    sample_count: int
    mae: float
    rmse: float
    max_abs_error: float
    pass_threshold: bool
    threshold: float
    unit: str
    status: Literal["measured_data_evaluation"] = "measured_data_evaluation"


def evaluate_soc_predictions(
    measured_soc_pct: List[float],
    predicted_soc_pct: List[float],
    max_abs_error_pct: float,
) -> CalibrationMetrics:
    if len(measured_soc_pct) != len(predicted_soc_pct) or not measured_soc_pct:
        raise ValueError("measured and predicted SOC arrays must have the same non-zero length")
    if max_abs_error_pct <= 0:
        raise ValueError("max_abs_error_pct must be positive")
    errors = [float(measured) - float(predicted) for measured, predicted in zip(
        measured_soc_pct, predicted_soc_pct
    )]
    values = [float(value) for value in [*measured_soc_pct, *predicted_soc_pct]]
    if any(not math.isfinite(value) or not 0 <= value <= 100 for value in values):
        raise ValueError("SOC values must be between 0 and 100 percent")
    absolute_errors = [abs(error) for error in errors]
    return CalibrationMetrics(
        metric="battery_soc_prediction_error",
        sample_count=len(errors),
        mae=sum(absolute_errors) / len(errors),
        rmse=(sum(error * error for error in errors) / len(errors)) ** 0.5,
        max_abs_error=max(absolute_errors),
        pass_threshold=max(absolute_errors) <= max_abs_error_pct,
        threshold=max_abs_error_pct,
        unit="percentage_points",
    )


class PvCalibrationMetrics(BaseModel):
    metric: str
    sample_count: int
    median_instantaneous_pr: float
    minimum_poa_w_m2: float
    status: Literal["measured_data_diagnostic_not_model_fit"] = (
        "measured_data_diagnostic_not_model_fit"
    )


def estimate_pv_instantaneous_pr(
    measured_ac_kw: List[float],
    poa_w_m2: List[float],
    capacity_kwp: float,
    minimum_poa_w_m2: float,
) -> PvCalibrationMetrics:
    if len(measured_ac_kw) != len(poa_w_m2) or not measured_ac_kw:
        raise ValueError("PV power and irradiance arrays must have the same non-zero length")
    if not math.isfinite(capacity_kwp) or capacity_kwp <= 0:
        raise ValueError("capacity_kwp must be a finite positive number")
    if not math.isfinite(minimum_poa_w_m2) or minimum_poa_w_m2 <= 0:
        raise ValueError("minimum_poa_w_m2 must be a finite positive number")
    ratios = []
    for power, irradiance in zip(measured_ac_kw, poa_w_m2):
        power = float(power)
        irradiance = float(irradiance)
        if not math.isfinite(power) or not math.isfinite(irradiance):
            continue
        if power < 0:
            raise ValueError("measured_ac_kw must be non-negative for PV generation")
        if irradiance >= minimum_poa_w_m2:
            ratios.append(power / (capacity_kwp * irradiance / 1000.0))
    if not ratios:
        raise ValueError("No valid paired samples meet the configured irradiance floor")
    from statistics import median

    return PvCalibrationMetrics(
        metric="pv_median_instantaneous_performance_ratio",
        sample_count=len(ratios),
        median_instantaneous_pr=median(ratios),
        minimum_poa_w_m2=minimum_poa_w_m2,
    )
