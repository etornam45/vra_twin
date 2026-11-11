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
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from backend.src.repository import BackendRepository
from backend.src.commands import CommandService
from contracts.src.twin import SiteTwinState


class CommandRequestPayload(BaseModel):
    site_id: str
    gateway_id: str
    asset_id: str
    type: str
    command_class: str
    params: Dict[str, Any]
    reason: str
    requested_by: str


def create_backend_app(db_path: Path) -> FastAPI:
    app = FastAPI(title="VRA Volta Lake Mini-Grid Digital Twin Central API", version="1.0")

    repo = BackendRepository(db_path)
    cmd_service = CommandService(repo)

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

    @app.get("/v1/timeseries")
    def get_timeseries(
        site: str = Query(...),
        asset: str = Query(...),
        tag: str = Query(...),
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 500
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
