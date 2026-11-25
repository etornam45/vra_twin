"""Central FastAPI REST service for VRA Digital Twin platform.

Section 2.5 Implementation:
Endpoints:
  GET  /v1/health
  GET  /v1/sites
  GET  /v1/sites/{site_id}
  GET  /v1/sites/{site_id}/assets
  GET  /v1/sites/{site_id}/state
  GET  /v1/timeseries
  POST /v1/commands
"""

from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.src.repository import BackendRepository, PostgresBackendRepository
from backend.src.commands import CommandService
from contracts.src.twin import SiteTwinState
from twin.src.diagnostics import DiagnosticThresholds, evaluate_twin_state
from twin.src.forecast import persistence_forecast


class CommandRequestPayload(BaseModel):
    site_id: str
    gateway_id: str
    asset_id: str
    type: str
    command_class: str
    params: Dict[str, Any]
    reason: str
    requested_by: str


def create_backend_app(
    db_path: Optional[Path] = None,
    database_url: Optional[str] = None,
) -> FastAPI:
    app = FastAPI(title="VRA Volta Lake Mini-Grid Digital Twin Central API", version="1.0")

    if database_url:
        repo = PostgresBackendRepository(database_url)
    elif db_path:
        repo = BackendRepository(db_path)
    else:
        raise ValueError("Provide either db_path for SQLite or database_url for PostgreSQL/TimescaleDB")
    cmd_service = CommandService(repo)
    dashboard_dir = Path(__file__).resolve().parents[3] / "web"
    app.mount("/dashboard-assets", StaticFiles(directory=dashboard_dir / "assets"), name="dashboard-assets")

    @app.get("/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(dashboard_dir / "index.html")

    @app.get("/v1/health")
    def health() -> Dict[str, str]:
        return {"status": "ok", "service": "vra-twin-backend"}

    @app.get("/v1/sites")
    def list_sites() -> List[Dict[str, Any]]:
        with repo._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sites")
            return [dict(r) for r in cursor.fetchall()]

    @app.get("/v1/sites/{site_id}")
    def get_site(site_id: str) -> Dict[str, Any]:
        with repo._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sites WHERE site_id = ?", (site_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Site not found")
            return dict(row)

    @app.get("/v1/sites/{site_id}/assets")
    def list_site_assets(site_id: str) -> List[Dict[str, Any]]:
        with repo._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM assets WHERE site_id = ?", (site_id,))
            return [dict(r) for r in cursor.fetchall()]

    @app.get("/v1/sites/{site_id}/state")
    def get_site_state(site_id: str) -> Dict[str, Any]:
        state = repo.get_twin_state(site_id)
        if not state:
            raise HTTPException(status_code=404, detail="Twin state not found or site offline")
        return state

    @app.get("/v1/sites/{site_id}/dashboard")
    def get_site_dashboard(site_id: str) -> Dict[str, Any]:
        data = repo.get_dashboard_data(site_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Site not found")
        return data

    @app.get("/v1/sites/{site_id}/diagnostics")
    def get_site_diagnostics(
        site_id: str,
        max_asset_freshness_s: float = Query(..., gt=0),
        max_abs_imbalance_kw: float = Query(..., gt=0),
        max_underperformance_pct: float = Query(..., gt=0, le=100),
        max_abs_soc_residual_pct: float = Query(..., gt=0, le=100),
    ) -> Dict[str, Any]:
        state = repo.get_twin_state(site_id)
        if state is None:
            raise HTTPException(status_code=404, detail="Twin state not found or site offline")
        try:
            thresholds = DiagnosticThresholds(
                max_asset_freshness_s=max_asset_freshness_s,
                max_abs_imbalance_kw=max_abs_imbalance_kw,
                max_underperformance_pct=max_underperformance_pct,
                max_abs_soc_residual_pct=max_abs_soc_residual_pct,
            )
            findings = evaluate_twin_state(SiteTwinState.model_validate(state), thresholds)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "site_id": site_id,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "thresholds": thresholds.model_dump(),
            "status": "warning" if findings else "clear",
            "findings": [finding.model_dump() for finding in findings],
        }

    @app.get("/v1/sites/{site_id}/forecast")
    def get_site_forecast(
        site_id: str,
        asset: str = Query(..., min_length=1),
        tag: str = Query(..., min_length=1),
        horizon_steps: int = Query(24, ge=1, le=288),
        step_s: int = Query(3600, ge=1, le=86400),
    ) -> Dict[str, Any]:
        if repo.get_dashboard_data(site_id) is None:
            raise HTTPException(status_code=404, detail="Site not found")
        history = repo.query_timeseries(
            site_id,
            asset,
            tag,
            to_ts=datetime.now(timezone.utc).isoformat(),
            limit=500,
        )
        try:
            result = persistence_forecast(history, horizon_steps, step_s)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "site_id": site_id,
            "asset_id": asset,
            "tag": tag,
            **result,
        }

    @app.get("/v1/timeseries")
    def get_timeseries(
        site: str = Query(...),
        asset: str = Query(...),
        tag: str = Query(...),
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = Query(500, ge=1, le=5000)
    ) -> List[Dict[str, Any]]:
        return repo.query_timeseries(site, asset, tag, from_ts, to_ts, limit)

    @app.post("/v1/commands")
    def issue_command(req: CommandRequestPayload) -> Dict[str, Any]:
        ok, cmd, msg = cmd_service.request_command(
            site_id=req.site_id,
            gateway_id=req.gateway_id,
            asset_id=req.asset_id,
            cmd_type=req.type,
            command_class=req.command_class,
            params=req.params,
            reason=req.reason,
            requested_by=req.requested_by
        )
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"status": "dispatched", "command": cmd.model_dump(), "message": msg}

    return app
