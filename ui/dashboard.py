"""FastAPI routes and REST API for the live Shoonya ORB trading terminal UI."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class BrokerConnectRequest(BaseModel):
    user_id: str
    password: str
    totp_secret: str
    secret_key: str
    account_id: str = ""
    api_key: str = ""
    sebi_algo_id: str = ""


class BrokerFundsRequest(BaseModel):
    amount: float
    action: str = "add"


class BrokerOrderRequest(BaseModel):
    exchange: str = "NSE"
    tradingsymbol: str = "TCS-EQ"
    buy_or_sell: str = "B"
    quantity: int = 1
    price_type: str = "LMT"
    price: float = 0.0
    product_type: str = "C"


class OrbWindowRequest(BaseModel):
    minutes: int = 15


class OrbAnchorRequest(BaseModel):
    start_time: str = "09:15"
    minutes: int = 15


class EntryCutoffRequest(BaseModel):
    cutoff_time: str = "14:15"


class AutoTriggerRequest(BaseModel):
    index_name: str = "BANKNIFTY"
    signal_type: Optional[str] = "AUTO"


class PositionExitRequest(BaseModel):
    index_name: str = "NIFTY"


class ModifySlRequest(BaseModel):
    index_name: str = "NIFTY"
    sl_trigger_price: float


class SetLotsRequest(BaseModel):
    index_name: str = "NIFTY"
    lots: int = 2


class SetProductTypeRequest(BaseModel):
    index_name: str = "NIFTY"
    product_type: str = "NRML"


class ScheduleMilestoneRequest(BaseModel):
    milestone: str = "all"
    action: str = "restart"


class SyncBackupIpRequest(BaseModel):
    ip: str = ""


def create_app(orchestrator: Any) -> FastAPI:
    """Build and return the FastAPI web dashboard application."""
    from api.auth_router import router as auth_router
    from api.broker_router import router as broker_router
    from api.risk_router import router as risk_router
    from api.trades_router import router as trades_router
    from database.connection import get_db, init_db

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if get_db not in _app.dependency_overrides:
            await init_db()
        yield

    app = FastAPI(title="Shoonya ORB Algo Terminal", version="2.0.0", lifespan=lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    raw_origins = os.getenv("CORS_ORIGINS", "*").strip()
    allow_origins = ["*"] if raw_origins == "*" else [o.strip() for o in raw_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount multi-tenant production routers
    app.include_router(auth_router)
    app.include_router(broker_router)
    app.include_router(risk_router)
    app.include_router(trades_router)

    STATIC_DIR.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        snapshot = orchestrator.get_status_snapshot()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"snapshot": snapshot},
        )

    @app.get("/app", response_class=HTMLResponse)
    @app.get("/mobile", response_class=HTMLResponse)
    def mobile_app_view(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="mobile_app.html",
            context={},
        )

    @app.get("/download/apk")
    def download_apk() -> Response:
        apk_path = Path(__file__).resolve().parent.parent / "builds" / "app-debug.apk"
        if apk_path.exists():
            return FileResponse(
                path=str(apk_path),
                filename="AutoORB-debug.apk",
                media_type="application/vnd.android.package-archive",
                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
            )
        release_url = os.getenv(
            "APK_DOWNLOAD_URL",
            "https://github.com/balajidhanraj/AutoORB/releases/download/latest-apk/AutoORB-debug.apk",
        )
        return RedirectResponse(url=release_url, status_code=302)

    @app.get("/backtest", response_class=HTMLResponse)
    def backtest_view() -> HTMLResponse:
        dash_path = Path(__file__).resolve().parent.parent / "data" / "backtest" / "backtest_dashboard.html"
        if dash_path.exists():
            return HTMLResponse(content=dash_path.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>Backtest dashboard file not found.</h1>")

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        return JSONResponse(content=orchestrator.get_status_snapshot())

    @app.get("/api/logs")
    def api_logs(limit: int = 150) -> JSONResponse:
        from utils.logger import get_recent_logs
        logs = get_recent_logs(limit=limit)
        return JSONResponse(content={"ok": True, "count": len(logs), "logs": logs})

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

    @app.get("/api/backtest/trades")
    def api_backtest_trades() -> JSONResponse:
        p = Path("data/backtest/all_trades_180d.json")
        if p.exists():
            import json
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return JSONResponse(content={"ok": True, "count": len(data), "trades": data})
            except Exception as e:
                return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
        return JSONResponse(content={"ok": True, "count": 0, "trades": []})

    @app.get("/api/backtest/csv")
    def api_backtest_csv() -> Response:
        import io, csv, json
        p = Path("data/backtest/all_trades_180d.json")
        trades = []
        if p.exists():
            try:
                trades = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        output = io.StringIO()
        fieldnames = [
            "trade_id", "date", "index_name", "tradingsymbol", "mode", "signal_type",
            "entry_time", "entry_price", "lots", "qty", "exit_time", "exit_price",
            "pnl", "reason", "duration_minutes"
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for t in trades:
            writer.writerow({
                "trade_id": t.get("id") or t.get("trade_id", ""),
                "date": t.get("date", ""),
                "index_name": t.get("idx") or t.get("index_name", ""),
                "tradingsymbol": t.get("tradingsymbol", ""),
                "mode": "BACKTEST",
                "signal_type": t.get("side") or t.get("signal_type", ""),
                "entry_time": t.get("in_t") or t.get("entry_time", ""),
                "entry_price": t.get("in_px", t.get("entry_price", 0.0)),
                "lots": t.get("lots", 0),
                "qty": t.get("qty", 0),
                "exit_time": t.get("out_t") or t.get("exit_time", ""),
                "exit_price": t.get("out_px", t.get("exit_price", 0.0)),
                "pnl": t.get("net", t.get("pnl", 0.0)),
                "reason": t.get("reason", ""),
                "duration_minutes": t.get("duration_minutes", 0),
            })
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=master_alpha_backtest_180d.csv"},
        )

    @app.post("/api/broker/connect")
    def api_broker_connect(payload: BrokerConnectRequest) -> JSONResponse:
        ok, msg = orchestrator.connect_shoonya_account(
            user_id=payload.user_id,
            password=payload.password,
            totp_secret=payload.totp_secret,
            secret_key=payload.secret_key,
            account_id=payload.account_id,
            api_key=payload.api_key,
            sebi_algo_id=payload.sebi_algo_id,
        )
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "oauth_status": orchestrator.auth.last_login_status,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/broker/refresh")
    def api_broker_refresh() -> JSONResponse:
        broker = orchestrator.refresh_broker_data()
        return JSONResponse(
            content={
                "ok": bool(broker.get("connected")),
                "broker": broker,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/broker/sync-backup-ip")
    def api_sync_backup_ip(payload: Optional[SyncBackupIpRequest] = None) -> JSONResponse:
        target_ip = payload.ip.strip() if payload and payload.ip else ""
        ok, msg = orchestrator.sync_shoonya_backup_ip(target_ip=target_ip)
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "public_ip": orchestrator.public_ip,
                "backup_ip_status": orchestrator.auth.last_ip_sync_status,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/broker/funds")
    def api_broker_funds(payload: BrokerFundsRequest) -> JSONResponse:
        ok, msg = orchestrator.add_or_update_funds(amount=payload.amount, action=payload.action)
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "capital": orchestrator.settings.capital,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/broker/order")
    def api_broker_order(payload: BrokerOrderRequest) -> JSONResponse:
        ok, msg, resp = orchestrator.place_manual_broker_order(
            exchange=payload.exchange,
            tradingsymbol=payload.tradingsymbol,
            buy_or_sell=payload.buy_or_sell,
            quantity=payload.quantity,
            price_type=payload.price_type,
            price=payload.price,
            product_type=payload.product_type,
        )
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "response": resp,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/settings/orb-window")
    def api_set_orb_window(payload: OrbWindowRequest) -> JSONResponse:
        ok, msg = orchestrator.set_orb_window(payload.minutes)
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "orb_start_time": orchestrator.settings.orb_start_time.strftime("%H:%M"),
                "orb_window_minutes": orchestrator.settings.orb_window_minutes,
                "orb_end_time": orchestrator.settings.orb_end_time.strftime("%H:%M"),
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/settings/orb-anchor")
    def api_set_orb_anchor(payload: OrbAnchorRequest) -> JSONResponse:
        ok, msg = orchestrator.set_orb_anchor(start_time=payload.start_time, minutes=payload.minutes)
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "orb_start_time": orchestrator.settings.orb_start_time.strftime("%H:%M"),
                "orb_window_minutes": orchestrator.settings.orb_window_minutes,
                "orb_end_time": orchestrator.settings.orb_end_time.strftime("%H:%M"),
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/trades/reset")
    def api_reset_trades() -> JSONResponse:
        ok, msg = orchestrator.reset_daily_trades()
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/schedule/milestone")
    def api_schedule_milestone(payload: ScheduleMilestoneRequest) -> JSONResponse:
        ok, msg = orchestrator.control_schedule_milestone(
            milestone_key=payload.milestone,
            action=payload.action,
        )
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/settings/entry-cutoff")
    def api_set_entry_cutoff(payload: EntryCutoffRequest) -> JSONResponse:
        ok, msg = orchestrator.set_entry_cutoff_time(payload.cutoff_time)
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "entry_cutoff_time": orchestrator.settings.entry_cutoff_time.strftime("%H:%M"),
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/strategy/auto-trigger")
    def api_auto_trigger(payload: AutoTriggerRequest) -> JSONResponse:
        ok, msg, data = orchestrator.trigger_auto_order(
            index_name=payload.index_name,
            signal_type=payload.signal_type,
        )
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "data": data,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/positions/exit")
    def api_exit_position(payload: PositionExitRequest) -> JSONResponse:
        ok, msg = orchestrator.exit_position(payload.index_name)
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/positions/modify-sl")
    def api_modify_sl(payload: ModifySlRequest) -> JSONResponse:
        ok, msg = orchestrator.modify_position_sl(payload.index_name, payload.sl_trigger_price)
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.get("/api/config/public")
    def api_public_config() -> JSONResponse:
        from services.neon_service import get_neon_config_summary

        neon_summary = get_neon_config_summary()
        return JSONResponse(
            content={
                "app_name": "Shoonya Automated ORB Terminal",
                "version": "2.0.0",
                "mode": orchestrator.settings.mode,
                "sebi_algo_id": orchestrator.settings.sebi_algo_id,
                "auth_providers": ["email", "google", "facebook", "phone"],
                "google_oauth_enabled": bool(os.getenv("GOOGLE_CLIENT_ID")),
                "facebook_oauth_enabled": bool(os.getenv("FACEBOOK_APP_ID")),
                "firebase_enabled": bool(os.getenv("FIREBASE_WEB_API_KEY")),
                "neon_auth_enabled": neon_summary["auth_enabled"],
                "neon_realtime_enabled": neon_summary["realtime_enabled"],
                "neon": neon_summary,
                "env": os.getenv("APP_ENV", "development"),
            }
        )

    @app.post("/api/settings/lots")
    def api_set_lots(payload: SetLotsRequest) -> JSONResponse:
        ok, msg = orchestrator.set_index_lots(payload.index_name, payload.lots)
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.post("/api/settings/product-type")
    def api_set_product_type(payload: SetProductTypeRequest) -> JSONResponse:
        ok, msg = orchestrator.set_index_product_type(payload.index_name, payload.product_type)
        return JSONResponse(
            content={
                "ok": ok,
                "message": msg,
                "snapshot": orchestrator.get_status_snapshot(),
            }
        )

    @app.get("/health")
    def health() -> JSONResponse:
        from services.neon_service import get_neon_config_summary

        return JSONResponse(
            content={
                "status": "ok",
                "mode": orchestrator.settings.mode,
                "env": os.getenv("APP_ENV", "development"),
                "neon": get_neon_config_summary(),
            }
        )

    app.state.handlers = {
        "index": index,
        "api_status": api_status,
        "api_kill_switch": api_kill_switch,
        "api_oauth_login": api_oauth_login,
        "api_toggle_mode": api_toggle_mode,
        "api_trades_csv": api_trades_csv,
        "api_broker_connect": api_broker_connect,
        "api_broker_refresh": api_broker_refresh,
        "api_broker_funds": api_broker_funds,
        "api_broker_order": api_broker_order,
        "api_set_orb_window": api_set_orb_window,
        "api_set_orb_anchor": api_set_orb_anchor,
        "api_set_entry_cutoff": api_set_entry_cutoff,
        "api_set_lots": api_set_lots,
        "api_set_product_type": api_set_product_type,
        "api_auto_trigger": api_auto_trigger,
        "api_exit_position": api_exit_position,
        "api_modify_sl": api_modify_sl,
        "api_public_config": api_public_config,
        "api_reset_trades": api_reset_trades,
        "health": health,
    }
    return app
