"""FastAPI local REST service for the edge gateway on the island.

Section 4.1 & 4.5 Implementation:
Endpoints:
  GET  /api/v1/status
  GET  /api/v1/queue
  GET  /api/v1/tags
  GET  /api/v1/events
  GET  /api/v1/config
  POST /api/v1/config/validate
  GET  /api/v1/config/versions
  POST /api/v1/config/revert
  GET  /api/v1/health
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from gateway.src.store import GatewayStorage
from gateway.src.configsvc import ConfigService
from gateway.src.health import HealthAgent


def create_gateway_app(db_path: Path, config_dir: Path) -> FastAPI:
    app = FastAPI(title="VRA Island Mini-Grid Edge Gateway API", version="1.0")

    db = GatewayStorage(db_path)
    cfg_svc = ConfigService(config_dir)
    health_agent = HealthAgent(db=db, gateway_id="GW-VLK-001", site_id="VLK-ISL-001")

    @app.get("/api/v1/status")
    def get_status() -> Dict[str, Any]:
        stats = db.get_queue_stats()
        return {
            "status": "ok",
            "active_version": cfg_svc.current_version,
            "queue": stats
        }

    @app.get("/api/v1/queue")
    def get_queue() -> Dict[str, Any]:
        return db.get_queue_stats()

    @app.get("/api/v1/tags")
    def get_tags(asset: Optional[str] = None) -> List[Dict[str, Any]]:
        with db._get_connection() as conn:
            cursor = conn.cursor()
            if asset:
                cursor.execute("SELECT * FROM tag_state WHERE asset_id = ? ORDER BY tag", (asset,))
            else:
                cursor.execute("SELECT * FROM tag_state ORDER BY asset_id, tag")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    @app.get("/api/v1/health")
    def get_health() -> Dict[str, Any]:
        h = health_agent.build_health_message()
        return h.model_dump()

    @app.get("/api/v1/config/versions")
    def get_config_versions() -> Dict[str, Any]:
        return {
            "current": cfg_svc.current_version,
            "versions": cfg_svc.list_versions()
        }

    @app.post("/api/v1/config/validate")
    def validate_config() -> Dict[str, Any]:
        res = cfg_svc.validate_current()
        return {
            "valid": res.is_valid,
            "errors": res.errors,
            "warnings": res.warnings
        }

    @app.post("/api/v1/config/revert")
    def revert_config(version: str) -> Dict[str, Any]:
        ok, msg = cfg_svc.rollback_to_version(version)
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"success": True, "message": msg}

    @app.get("/", response_class=HTMLResponse)
    def index_ui():
        stats = db.get_queue_stats()
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>VRA Island Gateway - VLK-ISL-001</title>
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 2rem; background: #f8fafc; }}
                .card {{ background: white; padding: 1.5rem; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 1.5rem; }}
                h1 {{ color: #0f172a; margin-bottom: 0.5rem; }}
                .badge {{ display: inline-block; padding: 0.25rem 0.5rem; border-radius: 4px; font-weight: bold; background: #e2e8f0; }}
                .badge-ok {{ background: #dcfce7; color: #166534; }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
                th, td {{ text-align: left; padding: 0.75rem; border-bottom: 1px solid #e2e8f0; }}
            </style>
        </head>
        <body>
            <h1>Volta River Authority Mini-Grid Gateway</h1>
            <p><strong>Site:</strong> VLK-ISL-001 | <strong>Gateway:</strong> GW-VLK-001 | <span class="badge badge-ok">System Healthy</span></p>

            <div class="card">
                <h2>Uplink Queue & Link Status</h2>
                <table>
                    <tr><th>Pending Outbox Batches</th><td>{stats['pending']}</td></tr>
                    <tr><th>Inflight Batches</th><td>{stats['inflight']}</td></tr>
                    <tr><th>Acked Batches</th><td>{stats['acked']}</td></tr>
                    <tr><th>Oldest Unsent Age</th><td>{stats['oldest_unsent_age_s']} s</td></tr>
                    <tr><th>Active Configuration</th><td>{cfg_svc.current_version}</td></tr>
                </table>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html)

    return app
