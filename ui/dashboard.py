"""FastAPI routes and REST API for the live Shoonya ORB trading terminal UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


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
    index_name: str = "CRUDEOILM"


class SetLotsRequest(BaseModel):
    index_name: str = "NIFTY"
    lots: int = 2


class SetProductTypeRequest(BaseModel):
    index_name: str = "CRUDEOILM"
    product_type: str = "NRML"


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

    @app.get("/backtest", response_class=HTMLResponse)
    def backtest_view() -> HTMLResponse:
        dash_path = Path(__file__).resolve().parent.parent / "data" / "backtest" / "backtest_dashboard.html"
        if dash_path.exists():
            return HTMLResponse(content=dash_path.read_text(encoding="utf-8"))
        return HTMLResponse(content="<h1>Backtest dashboard file not found.</h1>")

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
        return JSONResponse(content={"status": "ok", "mode": orchestrator.settings.mode})

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
        "api_reset_trades": api_reset_trades,
        "health": health,
    }
    return app
