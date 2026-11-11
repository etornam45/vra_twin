import pytest
from pathlib import Path
from contracts.src.common import QualityCode, SignConvention
from contracts.src.messages import TelemetryBatch, SeriesColumn, SelectiveAck
from contracts.src.assets import BessConfig
from contracts.src.validate import validate_site_directory, export_all_json_schemas


def test_bess_semantic_validation():
    # Valid config
    bess = BessConfig(
        chemistry="LFP",
        rated_energy_kwh=200.0,
        usable_energy_kwh=180.0,
        max_charge_kw=50.0,
        max_discharge_kw=50.0,
        soc_min_pct=10.0,
        soc_max_pct=95.0
    )
    assert bess.chemistry == "LFP"

    # Invalid: usable > rated
    with pytest.raises(ValueError, match="usable_energy_kwh.*cannot exceed"):
        BessConfig(
            rated_energy_kwh=100.0,
            usable_energy_kwh=150.0,
            max_charge_kw=50.0,
            max_discharge_kw=50.0
        )

    # Invalid: soc_min >= soc_max (both within allowable bounds, e.g. min 50, max 50)
    with pytest.raises(ValueError, match="soc_min_pct.*must be less than"):
        BessConfig(
            rated_energy_kwh=200.0,
            usable_energy_kwh=180.0,
            max_charge_kw=50.0,
            max_discharge_kw=50.0,
            soc_min_pct=50.0,
            soc_max_pct=50.0
        )


def test_telemetry_batch_series_validation():
    # Mismatched array lengths should fail
    with pytest.raises(ValueError, match="Series length mismatch"):
        SeriesColumn(
            asset="BESS_01",
            tag="soc",
            u="%",
            t=[0, 1000],
            v=[78.0],  # only 1 value for 2 time entries
            q=[0, 0]
        )

    # Matching lengths should succeed
    col = SeriesColumn(
        asset="BESS_01",
        tag="soc",
        u="%",
        t=[0, 1000],
        v=[78.0, 78.1],
        q=[QualityCode.GOOD, QualityCode.GOOD]
    )
    batch = TelemetryBatch(
        site_id="VLK-ISL-001",
        gateway_id="GW-VLK-001",
        seq=1,
        t0_ms=1731319200000,
        cfg_version="v1.0",
        series=[col]
    )
    assert batch.seq == 1
    assert len(batch.series) == 1


def test_site_directory_validation(tmp_path):
    site_path = Path("sites/VLK-ISL-001")
    result = validate_site_directory(site_path)
    assert result.is_valid, f"Validation errors: {result.errors}"


def test_export_json_schemas(tmp_path):
    export_all_json_schemas(tmp_path)
    exported_files = list(tmp_path.glob("*.json"))
    assert len(exported_files) >= 8
