"""FastAPI routes and REST API for the live Shoonya ORB trading terminal UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def create_app(orchestrator: Any) -> FastAPI:
    """Build and return the FastAPI web dashboard application."""
    app = FastAPI(title="Shoonya ORB Algo Terminal", version="1.0.0")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        snapshot = orchestrator.get_status_snapshot()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"snapshot": snapshot},
        )

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        return JSONResponse(content=orchestrator.get_status_snapshot())

    @app.post("/api/kill-switch")
    def api_kill_switch() -> JSONResponse:
        orchestrator.trigger_kill_switch(reason="MANUAL_UI_KILL_SWITCH")
        return JSONResponse(
            content={
                "ok": True,
                "message": "Emergency kill switch executed",
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/oauth-login")
    def api_oauth_login() -> JSONResponse:
        token = orchestrator.run_daily_login(force_refresh=True)
        return JSONResponse(
            content={
                "ok": bool(token),
                "oauth_status": orchestrator.auth.last_login_status,
                "token_date": orchestrator.auth.last_token_date,
            }
        )

    @app.post("/api/toggle-mode")
    def api_toggle_mode() -> JSONResponse:
        changed, msg = orchestrator.toggle_mode()
        if not changed:
            raise HTTPException(status_code=409, detail=msg)
        return JSONResponse(
            content={
                "ok": True,
                "mode": orchestrator.settings.mode,
                "message": msg,
            }
        )

    @app.get("/api/trades/csv")
    def api_trades_csv() -> Response:
        csv_path = orchestrator.risk_manager.export_daily_trades_csv()
        csv_text = csv_path.read_text(encoding="utf-8")
        return Response(
            content=csv_text,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{csv_path.name}"'},
        )

    @app.get("/health")
    def health() -> JSONResponse:
        return JSONResponse(content={"status": "ok", "mode": orchestrator.settings.mode})

    app.state.handlers = {
        "index": index,
        "api_status": api_status,
        "api_kill_switch": api_kill_switch,
        "api_oauth_login": api_oauth_login,
        "api_toggle_mode": api_toggle_mode,
        "api_trades_csv": api_trades_csv,
        "health": health,
    }
    return app
