"""FastAPI routes and REST API for the live Shoonya ORB trading terminal UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        "health": health,
    }
    return app
