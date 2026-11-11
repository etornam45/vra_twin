"""Site integrity and configuration validation engine."""

import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple
import yaml
from pydantic import ValidationError

from .common import SignConvention, TagClass
from .config import GatewayConfig, SiteConfig
from .assets import BessConfig, PvArrayConfig, InverterConfig, PcsConfig, MeterConfig


class ValidationResult:
    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


def validate_site_directory(site_dir_path: Path) -> ValidationResult:
    """Validates site.yaml, gateway.yaml and cross-asset references for an island site."""
    res = ValidationResult()
    site_file = site_dir_path / "site.yaml"
    gateway_file = site_dir_path / "gateway.yaml"

    if not site_file.exists():
        res.errors.append(f"Missing required file: {site_file}")
        return res

    # 1. Parse site.yaml
    try:
        with open(site_file, "r") as f:
            site_raw = yaml.safe_load(f)
        site_cfg = SiteConfig.model_validate(site_raw)
    except Exception as e:
        res.errors.append(f"Failed to parse or validate {site_file}: {e}")
        return res

    # 2. Parse gateway.yaml if present
    gateway_cfg = None
    if gateway_file.exists():
        try:
            with open(gateway_file, "r") as f:
                gw_raw = yaml.safe_load(f)
            gateway_cfg = GatewayConfig.model_validate(gw_raw)
        except Exception as e:
            res.errors.append(f"Failed to parse or validate {gateway_file}: {e}")
    else:
        res.warnings.append(f"gateway.yaml not found at {gateway_file}")

    # 3. Cross-asset topology checks
    assets = site_cfg.assets
    has_inverter_or_pcs = False

    for asset_id, item in assets.items():
        # Check parent / reference links
        if item.inverter and item.inverter not in assets:
            res.errors.append(f"Asset '{asset_id}' references non-existent inverter '{item.inverter}'")
        if item.pcs and item.pcs not in assets:
            res.errors.append(f"Asset '{asset_id}' references non-existent pcs '{item.pcs}'")
        if item.meter and item.meter not in assets:
            res.errors.append(f"Asset '{asset_id}' references non-existent meter '{item.meter}'")
        if item.feeder and item.feeder not in assets:
            res.errors.append(f"Asset '{asset_id}' references non-existent feeder '{item.feeder}'")

        if item.type in ["pcs", "pv_inverter"]:
            has_inverter_or_pcs = True

    if not has_inverter_or_pcs:
        res.warnings.append("Site has no grid-forming or PV inverter asset declared.")

    # 4. Gateway device references match site assets
    if gateway_cfg:
        if gateway_cfg.gateway != site_cfg.gateway:
            res.errors.append(f"Gateway ID mismatch: site specifies '{site_cfg.gateway}' but gateway.yaml has '{gateway_cfg.gateway}'")
        for dev in gateway_cfg.devices:
            if dev.asset not in assets:
                res.errors.append(f"gateway.yaml device references '{dev.asset}', which is not defined in site.yaml")

    return res


def export_all_json_schemas(output_dir: Path):
    """Exports JSON Schemas for all core contracts."""
    import json
    from .messages import TelemetryBatch, SelectiveAck, EventMessage, GatewayHealth, CommandMessage, CommandResult
    from .assets import AssetDefinition, BessConfig
    from .twin import SiteTwinState
    from .config import SiteConfig, GatewayConfig

    models = [
        ("telemetry_batch", TelemetryBatch),
        ("selective_ack", SelectiveAck),
        ("event_message", EventMessage),
        ("gateway_health", GatewayHealth),
        ("command_message", CommandMessage),
        ("command_result", CommandResult),
        ("asset_definition", AssetDefinition),
        ("bess_config", BessConfig),
        ("site_twin_state", SiteTwinState),
        ("site_config", SiteConfig),
        ("gateway_config", GatewayConfig)
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    for name, model in models:
        schema = model.model_json_schema()
        out_path = output_dir / f"{name}.json"
        with open(out_path, "w") as f:
            json.dump(schema, f, indent=2)
