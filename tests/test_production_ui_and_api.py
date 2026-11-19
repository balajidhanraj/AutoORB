"""Production UI, Multi-Tenant Auth, Risk, SL Modification, and Mobile Sync Tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncGenerator

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from broker.instrument_mapper import InstrumentContract
from config.settings import AppSettings
from database.connection import get_db
from database.models import Base
from engine.risk_manager import ActivePosition, PositionState
from main import TradingSystemOrchestrator
from scripts.sync_mobile import sync_mobile_assets
from ui.dashboard import create_app


def _build_test_client(tmp_path: Path):
    settings = AppSettings(
        mode="PAPER",
        token_file_path=str(tmp_path / "daily_token.json"),
        data_dir=str(tmp_path / "data"),
        log_dir=str(tmp_path / "logs"),
        sebi_algo_id="ALGO_TEST_PROD",
    )
    orch = TradingSystemOrchestrator(settings=settings)
    app = create_app(orch)

    db_path = tmp_path / "test_prod.db"
    test_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    TestSession = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

    async def _init_tables() -> None:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init_tables())

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with TestSession() as session:
            try:
                yield session
            finally:
                await session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return orch, app, test_engine


def test_desktop_mobile_and_static_assets(tmp_path: Path) -> None:
    _orch, app, engine = _build_test_client(tmp_path)
    try:
        with TestClient(app) as client:
            home = client.get("/")
            assert home.status_code == 200
            html = home.text
            assert "Shoonya Automated ORB Terminal" in html
            assert "/static/theme.css?v=5" in html
            assert "/static/api_client.js?v=5" in html
            for marker in (
                "desk-landing-screen",
                "desk-app-header",
                "desk-app-main",
                "showDesktopLanding",
                "enterDesktopApp",
                "btn-hdr-logout",
                "view-radar",
                "view-positions",
                "view-logs",
                "view-analytics",
                "modal-guide",
            ):
                assert marker in html

            for mobile_path in ("/app", "/mobile"):
                mres = client.get(mobile_path)
                assert mres.status_code == 200
                mhtml = mres.text
                assert "/static/theme.css?v=5" in mhtml
                assert "/static/api_client.js?v=5" in mhtml
                for marker in (
                    "m-landing-screen",
                    "showLandingScreen",
                    "ma-btn-close",
                    "m-auth-screen",
                    "m-app-header",
                    "m-app-nav",
                    "showLoginScreen",
                    "enterAppShell",
                    "ma-api-base",
                    "mtab-radar",
                    "mtab-positions",
                    "mtab-logs",
                    "mtab-controls",
                    "kill-slider-track",
                    "mGoogleAuth",
                    "mFacebookAuth",
                    "mSendOtp",
                    "mEmailAuth",
                ):
                    assert marker in mhtml

            for static_path in (
                "/static/theme.css",
                "/static/api_client.js",
                "/static/manifest.json",
                "/static/sw.js",
                "/static/icon.svg",
            ):
                sres = client.get(static_path)
                assert sres.status_code == 200, f"{static_path} returned {sres.status_code}"
                if static_path == "/static/api_client.js":
                    assert "normalizeLogEntry" in sres.text
                    assert "renderLogRowHtml" in sres.text
                    assert "formatLogsForCopy" in sres.text

            logs_res = client.get("/api/logs")
            assert logs_res.status_code == 200
            logs_payload = logs_res.json()
            assert logs_payload["ok"] is True
            assert isinstance(logs_payload["logs"], list)
            if logs_payload["logs"]:
                first = logs_payload["logs"][0]
                assert set(first.keys()) >= {"timestamp", "level", "logger", "message"}
    finally:
        asyncio.run(engine.dispose())


def test_public_config_cors_and_modify_sl(tmp_path: Path) -> None:
    orch, app, engine = _build_test_client(tmp_path)
    try:
        with TestClient(app) as client:
            cfg = client.get("/api/config/public", headers={"Origin": "http://localhost"})
            assert cfg.status_code == 200
            assert cfg.headers.get("access-control-allow-origin") in ("*", "http://localhost")
            data = cfg.json()
            assert data["app_name"] == "Shoonya Automated ORB Terminal"
            assert data["mode"] == "PAPER"
            assert set(data["auth_providers"]) == {"email", "google", "facebook", "phone"}
            for key in ("google_oauth_enabled", "facebook_oauth_enabled", "firebase_enabled", "env", "sebi_algo_id"):
                assert key in data

            # Missing position -> ok: False
            missing_sl = client.post("/api/positions/modify-sl", json={"index_name": "NIFTY", "sl_trigger_price": 120.0})
            assert missing_sl.status_code == 200
            assert missing_sl.json()["ok"] is False

            # Active position -> updates SL trigger price cleanly
            contract = InstrumentContract(
                index_name="NIFTY",
                exchange="NFO",
                token="99999",
                tradingsymbol="NIFTY24NOV25200CE",
                strike=25200,
                option_type="CE",
                expiry="24NOV26",
                lot_size=65,
            )
            orch.risk_manager.positions["NIFTY"] = ActivePosition(
                trade_id="T_SL_1",
                index_name="NIFTY",
                contract=contract,
                signal_type="CE_BUY",
                state=PositionState.OPEN_FULL,
                initial_qty=65,
                remaining_qty=65,
                entry_order_id="ORD1",
                entry_limit_price=150.0,
                entry_submitted_monotonic=0.0,
                entry_avg_price=150.0,
                sl_order_id=None,
                sl_trigger_price=125.0,
                sl_limit_price=122.0,
                last_ltp=170.0,
            )

            mod_sl = client.post("/api/positions/modify-sl", json={"index_name": "NIFTY", "sl_trigger_price": 148.5})
            assert mod_sl.status_code == 200
            body = mod_sl.json()
            assert body["ok"] is True
            assert orch.risk_manager.positions["NIFTY"].sl_trigger_price == 148.5
            assert body["snapshot"]["positions"][0]["sl_trigger_price"] == 148.5
    finally:
        asyncio.run(engine.dispose())


def test_multitenant_auth_and_risk_endpoints(tmp_path: Path) -> None:
    _orch, app, engine = _build_test_client(tmp_path)
    try:
        with TestClient(app) as client:
            reg = client.post(
                "/api/v1/auth/register",
                json={"email": "alpha@example.com", "password": "StrongPassword!1", "name": "Alpha Trader"},
            )
            assert reg.status_code == 200
            token = reg.json()["access_token"]
            auth_hdr = {"Authorization": f"Bearer {token}"}

            login = client.post(
                "/api/v1/auth/login",
                json={"email": "alpha@example.com", "password": "StrongPassword!1"},
            )
            assert login.status_code == 200
            assert login.json()["user"]["email"] == "alpha@example.com"

            me = client.get("/api/v1/auth/me", headers=auth_hdr)
            assert me.status_code == 200
            assert me.json()["name"] == "Alpha Trader"

            risk_get = client.get("/api/v1/risk/settings", headers=auth_hdr)
            assert risk_get.status_code == 200

            risk_put = client.put(
                "/api/v1/risk/settings",
                headers=auth_hdr,
                json={"capital": 300000.0, "max_daily_loss": 6000.0, "nifty_lots": 3},
            )
            assert risk_put.status_code == 200
            assert client.get("/api/v1/risk/settings", headers=auth_hdr).json()["nifty_lots"] == 3

            g_res = client.post(
                "/api/v1/auth/google",
                json={"id_token": "dev_tok", "email": "google.user@example.com", "name": "Google User"},
            )
            assert g_res.status_code == 200
            assert g_res.json()["user"]["provider"] == "google"

            fb_res = client.post(
                "/api/v1/auth/facebook",
                json={"access_token": "dev_fb", "user_id": "fb_999", "name": "FB User"},
            )
            assert fb_res.status_code == 200
            assert fb_res.json()["user"]["provider"] == "facebook"

            otp_send = client.post("/api/v1/auth/phone/send-otp", json={"phone": "+919876540000"})
            assert otp_send.status_code == 200
            otp_code = otp_send.json()["dev_otp"]

            otp_ver = client.post("/api/v1/auth/phone/verify-otp", json={"phone": "+919876540000", "otp": otp_code})
            assert otp_ver.status_code == 200
            assert otp_ver.json()["user"]["provider"] == "phone"

            out = client.post("/api/v1/auth/logout")
            assert out.status_code == 200
            assert out.json()["status"] == "success"
    finally:
        asyncio.run(engine.dispose())


def test_mobile_sync_output() -> None:
    out_html = sync_mobile_assets()
    assert out_html.exists()
    css_file = out_html.parent / "static" / "theme.css"
    assert css_file.exists()
    content = out_html.read_text(encoding="utf-8")
    assert "m-landing-screen" in content
    assert "showLandingScreen" in content
    assert "./static/theme.css?v=5" in content
    assert "./static/api_client.js?v=5" in content
    assert 'href="/static/' not in content
    assert 'src="/static/' not in content
