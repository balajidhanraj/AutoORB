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
from database.connection import get_db, normalize_database_url
from database.models import Base
from engine.risk_manager import ActivePosition, PositionState
from main import TradingSystemOrchestrator
import scripts.sync_mobile as sync_mobile_mod
from scripts.sync_mobile import sync_mobile_assets
from ui.dashboard import create_app


def _build_test_client(tmp_path: Path):
    settings = AppSettings(
        mode="PAPER",
        token_file_path=str(tmp_path / "daily_token.json"),
        data_dir=str(tmp_path / "data"),
        log_dir=str(tmp_path / "logs"),
        sebi_algo_id="ALGO_TEST_PROD",
        enable_nifty_trading=True,
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
            assert "/static/theme.css?v=13" in html
            assert "/static/api_client.js?v=13" in html
            assert "/static/icon-192.png" in html
            assert "handleMilestoneControl" in html
            assert "handleSyncBackupIp" in html
            assert "Inter" in html
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
                assert "/static/theme.css?v=13" in mhtml
                assert "/static/api_client.js?v=13" in mhtml
                assert "/static/icon-192.png" in mhtml
                assert "mControlMilestone" in mhtml
                assert "mSyncBackupIp" in mhtml
                assert "m-main-safe" in mhtml
                assert "sheet-safe" in mhtml
                assert "100dvh" in mhtml
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
                    "ma-btn-preset-prod",
                    "ma-btn-preset-dev",
                    "mc-btn-preset-prod",
                    "mc-btn-preset-dev",
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
                "/static/icon-192.png",
                "/static/icon-512.png",
                "/static/icon-maskable-512.png",
                "/static/apple-touch-icon.png",
                "/static/favicon.png",
            ):
                sres = client.get(static_path)
                assert sres.status_code == 200, f"{static_path} returned {sres.status_code}"
                if static_path == "/static/theme.css":
                    for css_tok in ("--fs-body-md", "--fs-label-caps", "text-size-adjust", "m-main-safe", "sheet-safe"):
                        assert css_tok in sres.text
                if static_path == "/static/manifest.json":
                    manifest_json = sres.json()
                    icon_srcs = {i["src"]: i for i in manifest_json["icons"]}
                    assert icon_srcs["/static/icon-192.png"]["sizes"] == "192x192"
                    assert icon_srcs["/static/icon-512.png"]["sizes"] == "512x512"
                    assert icon_srcs["/static/icon-maskable-512.png"]["purpose"] == "maskable"
                if static_path == "/static/api_client.js":
                    assert "normalizeLogEntry" in sres.text
                    assert "renderLogRowHtml" in sres.text
                    assert "formatLogsForCopy" in sres.text
                    assert "getPresetUrls" in sres.text
                    assert "getDefaultApiBase" in sres.text
                    assert "isPlaceholderUrl" in sres.text
                    assert "probeHealth" in sres.text
                    assert "if (!isNativeCapacitor())" in sres.text
                    assert "Inter" in sres.text

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

            # Daily schedule milestone user controls (reset -> PENDING, done -> DONE)
            ms_reset = client.post("/api/schedule/milestone", json={"milestone": "all", "action": "reset"})
            assert ms_reset.status_code == 200
            assert ms_reset.json()["ok"] is True
            for m_item in ms_reset.json()["snapshot"]["schedule"]["milestones"]:
                assert m_item["status"] == "PENDING"

            ms_done = client.post("/api/schedule/milestone", json={"milestone": "cache", "action": "done"})
            assert ms_done.status_code == 200
            assert ms_done.json()["ok"] is True
            cache_ms = next(
                m for m in ms_done.json()["snapshot"]["schedule"]["milestones"] if m["key"] == "cache"
            )
            assert cache_ms["status"] == "DONE"

            # Backup IP sync endpoint
            ip_sync = client.post("/api/broker/sync-backup-ip", json={"ip": "203.0.113.45"})
            assert ip_sync.status_code == 200
            ip_data = ip_sync.json()
            assert ip_data["public_ip"] == "203.0.113.45"
            assert ip_data["snapshot"]["last_synced_backup_ip"] == "203.0.113.45"
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

            neon_res = client.post(
                "/api/v1/auth/neon",
                json={"email": "neon.unit@example.com", "name": "Neon Unit"},
            )
            assert neon_res.status_code == 200
            neon_tok = neon_res.json()["access_token"]
            neon_hdr = {"Authorization": f"Bearer {neon_tok}"}

            rec_res = client.post(
                "/api/v1/trades/record",
                headers=neon_hdr,
                json={"symbol": "NIFTY26NOV25200CE", "side": "BUY", "quantity": 65, "entry_price": 150.0, "pnl": 650.0},
            )
            assert rec_res.status_code == 200
            assert rec_res.json()["kind"] == "committed"

            my_tr = client.get("/api/v1/trades/my-trades", headers=neon_hdr)
            assert my_tr.status_code == 200
            assert len(my_tr.json()) == 1
    finally:
        asyncio.run(engine.dispose())


def test_mobile_sync_output() -> None:
    out_html = sync_mobile_assets()
    assert out_html.exists()
    css_file = out_html.parent / "static" / "theme.css"
    assert css_file.exists()
    css_text = css_file.read_text(encoding="utf-8")
    for token in ("--fs-body-md", "--fs-label-caps", "text-size-adjust", "m-main-safe", "sheet-safe"):
        assert token in css_text
    assert (out_html.parent / "static" / "icon-192.png").exists()
    content = out_html.read_text(encoding="utf-8")
    assert "m-landing-screen" in content
    assert "showLandingScreen" in content
    assert "./static/theme.css?v=13" in content
    assert "./static/api_client.js?v=13" in content
    assert "./static/icon-192.png" in content
    assert (out_html.parent / "app" / "index.html").exists()
    assert '<meta name="autoorb-api-base" content="http' in content
    assert "yourdomain.com" not in content
    assert 'href="/static/' not in content
    assert 'src="/static/' not in content


def test_android_launcher_and_splash_resources() -> None:
    res_dir = ROOT_DIR / "mobile" / "android" / "app" / "src" / "main" / "res"
    static_dir = ROOT_DIR / "ui" / "static"
    png_sig = b"\x89PNG\r\n\x1a\n"

    for asset_root in (static_dir, res_dir):
        for png_file in asset_root.rglob("*.png"):
            raw = png_file.read_bytes()
            assert raw.startswith(png_sig), f"Invalid PNG header: {png_file}"
            assert len(raw) < 100_000, f"PNG exceeds 100KB limit ({len(raw)} bytes): {png_file}"

    for density in ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi"):
        for fname in ("ic_launcher.png", "ic_launcher_round.png", "ic_launcher_foreground.png"):
            fpath = res_dir / f"mipmap-{density}" / fname
            assert fpath.exists(), f"Missing {fpath}"
            raw = fpath.read_bytes()
            assert len(raw) > 200 and raw.startswith(png_sig), f"Invalid PNG: {fpath}"

    splash_xxxhdpi = res_dir / "drawable-port-xxxhdpi" / "splash.png"
    assert splash_xxxhdpi.exists()
    assert splash_xxxhdpi.read_bytes().startswith(png_sig)

    mono_xml = res_dir / "drawable" / "ic_launcher_monochrome.xml"
    assert mono_xml.exists() and "<vector" in mono_xml.read_text(encoding="utf-8")

    for anydpi in ("ic_launcher.xml", "ic_launcher_round.xml"):
        xml_txt = (res_dir / "mipmap-anydpi-v26" / anydpi).read_text(encoding="utf-8")
        assert "@drawable/ic_launcher_monochrome" in xml_txt
        assert "@mipmap/ic_launcher_foreground" in xml_txt

    bg_xml = (res_dir / "values" / "ic_launcher_background.xml").read_text(encoding="utf-8")
    assert "#FFFFFF" not in bg_xml.upper() and "#" in bg_xml


def test_mobile_sync_injects_custom_vps_urls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sync_mobile_mod, "DEST_WWW", tmp_path)
    monkeypatch.setattr(sync_mobile_mod, "DEST_STATIC", tmp_path / "static")
    out_html = sync_mobile_assets(
        api_base="https://dev.example.com/",
        prod_url="https://app.example.com/",
        dev_url="https://dev.example.com/",
    )
    content = out_html.read_text(encoding="utf-8")
    assert '<meta name="autoorb-api-base" content="https://dev.example.com"/>' in content
    assert '<meta name="autoorb-prod-api" content="https://app.example.com"/>' in content
    assert '<meta name="autoorb-dev-api" content="https://dev.example.com"/>' in content


def test_multi_env_vps_artifacts_and_db_normalization(tmp_path: Path) -> None:
    assert normalize_database_url("postgres://u:p@h:5432/db") == "postgresql+asyncpg://u:p@h:5432/db"
    assert normalize_database_url("postgresql://u:p@h:5432/db") == "postgresql+asyncpg://u:p@h:5432/db"
    assert (
        normalize_database_url("postgresql://u:p@ep.neon.tech/neondb?sslmode=require&channel_binding=require")
        == "postgresql+asyncpg://u:p@ep.neon.tech/neondb?ssl=require"
    )
    assert normalize_database_url("sqlite+aiosqlite:///data/autoorb.db") == "sqlite+aiosqlite:///data/autoorb.db"

    _orch, app, engine = _build_test_client(tmp_path)
    try:
        with TestClient(app) as client:
            hres = client.get("/health")
            assert hres.status_code == 200
            assert hres.json()["status"] == "ok"
            assert "env" in hres.json()
    finally:
        asyncio.run(engine.dispose())

    expected_files = (
        ROOT_DIR / "docker-compose.vps.yml",
        ROOT_DIR / ".env.prod.example",
        ROOT_DIR / ".env.dev.example",
        ROOT_DIR / "deploy" / ".env.vps.example",
        ROOT_DIR / "deploy" / "Caddyfile",
        ROOT_DIR / "deploy" / "postgres-init.sql",
        ROOT_DIR / "deploy" / "setup_vps_multi_env.sh",
        ROOT_DIR / "deploy" / "VPS_MULTI_ENV_GUIDE.md",
        ROOT_DIR / ".github" / "workflows" / "deploy_vps.yml",
    )
    for path in expected_files:
        assert path.exists(), f"Missing VPS deployment artifact: {path}"

    compose_txt = (ROOT_DIR / "docker-compose.vps.yml").read_text(encoding="utf-8")
    for key in ("postgres:", "autoorb-prod:", "autoorb-dev:", "caddy:"):
        assert key in compose_txt

    caddy_txt = (ROOT_DIR / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "reverse_proxy autoorb-prod:8000" in caddy_txt
    assert "reverse_proxy autoorb-dev:8000" in caddy_txt


def test_scheduler_0840_oauth_and_milestone_progression(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime
    from config import IST

    orch, _app, engine = _build_test_client(tmp_path)
    try:
        login_calls: list[bool] = []
        cache_calls: list[int] = []
        backfill_calls: list[int] = []

        def fake_login(force_refresh: bool = False) -> str:
            login_calls.append(force_refresh)
            orch.auth.last_token_date = "2026-11-20"
            return "tok_0840"

        def fake_cache() -> int:
            cache_calls.append(1)
            return 1396

        def fake_backfill() -> None:
            backfill_calls.append(1)

        monkeypatch.setattr(orch, "run_daily_login", fake_login)
        monkeypatch.setattr(orch, "run_instrument_cache", fake_cache)
        monkeypatch.setattr(orch, "_backfill_all_indices", fake_backfill)
        orch.client.ws_connected = True  # Even if already connected at startup

        # 1. User clicks RESET ALL at 08:30 IST (before 08:40 scheduled auth time)
        t_0830 = datetime(2026, 11, 20, 8, 30, 0, tzinfo=IST)
        ok, _msg = orch.control_schedule_milestone("all", "reset", now_ist=t_0830)
        assert ok is True
        st_0830 = orch._build_schedule_status(t_0830)
        assert all(m["status"] == "PENDING" for m in st_0830["milestones"])

        # 2. At 08:40:01 IST, step_scheduler must trigger run_daily_login(force_refresh=True)
        t_0840 = datetime(2026, 11, 20, 8, 40, 1, tzinfo=IST)
        orch.step_scheduler(t_0840)
        assert login_calls == [True], "08:40 IST scheduler must force a fresh OAuth login"
        st_0840 = orch._build_schedule_status(t_0840)
        ms_0840 = {m["key"]: m["status"] for m in st_0840["milestones"]}
        assert ms_0840["auth"] == "DONE"
        assert ms_0840["cache"] == "PENDING"

        # 3. At 08:55:01 IST, step_scheduler must trigger NFO contract cache
        t_0855 = datetime(2026, 11, 20, 8, 55, 1, tzinfo=IST)
        orch.step_scheduler(t_0855)
        assert len(cache_calls) == 1
        ms_0855 = {m["key"]: m["status"] for m in orch._build_schedule_status(t_0855)["milestones"]}
        assert ms_0855["cache"] == "DONE"

        # 4. At 09:14:01 IST, step_scheduler must trigger WS & 3D backfill even if ws_connected was True
        t_0914 = datetime(2026, 11, 20, 9, 14, 1, tzinfo=IST)
        orch.step_scheduler(t_0914)
        assert len(backfill_calls) == 1
        ms_0914 = {m["key"]: m["status"] for m in orch._build_schedule_status(t_0914)["milestones"]}
        assert ms_0914["ws"] == "DONE"
    finally:
        asyncio.run(engine.dispose())


def test_max_daily_loss_guardrail_and_halt_resets(tmp_path: Path) -> None:
    from datetime import datetime
    from config import IST

    orch, app, engine = _build_test_client(tmp_path)
    try:
        with TestClient(app) as client:
            # 1. Modify Daily Max Loss Guardrail to custom ₹7,500
            r_set = client.post("/api/settings/max-daily-loss", json={"amount": 7500})
            assert r_set.status_code == 200
            d_set = r_set.json()
            assert d_set["ok"] is True
            assert d_set["max_daily_loss"] == 7500.0
            assert d_set["is_custom_max_daily_loss"] is True
            assert d_set["snapshot"]["max_daily_loss"] == 7500.0

            # 2. Simulate daily order max, weekly loss circuit breaker, daily MTM loss, and system halt
            now_ist = datetime.now(IST)
            cal_week = (now_ist.year, now_ist.isocalendar()[1])
            orch.risk_manager.trades_taken_today = 2
            orch.risk_manager.weekly_losses[cal_week] = 2
            orch.risk_manager.realized_pnl_today = -7600.0
            orch.risk_manager.halted = True
            orch.risk_manager.halt_reason = "DAILY_LOSS_LIMIT_BREACHED"
            assert orch.risk_manager.can_take_new_trade("NIFTY", now_ist) is False

            # 3. Granular resets: daily_orders, weekly_loss, daily_mtm
            r_ord = client.post("/api/risk/reset-halt", json={"reset_type": "daily_orders"})
            assert r_ord.status_code == 200 and r_ord.json()["ok"] is True
            assert orch.risk_manager.trades_taken_today == 0
            assert orch.risk_manager.halted is False

            r_wk = client.post("/api/risk/reset-halt", json={"reset_type": "weekly_loss"})
            assert r_wk.status_code == 200 and r_wk.json()["ok"] is True
            assert orch.risk_manager.weekly_losses[cal_week] == 0
            assert r_wk.json()["snapshot"]["weekly_guard"]["is_halted"] is False

            r_mtm = client.post("/api/risk/reset-halt", json={"reset_type": "daily_mtm"})
            assert r_mtm.status_code == 200 and r_mtm.json()["ok"] is True
            assert orch.risk_manager.realized_pnl_today == 0.0
            assert orch.risk_manager.can_take_new_trade("NIFTY", now_ist) is True

            # 4. Restore Daily Max Loss Guardrail to dynamic AUTO
            r_auto = client.post("/api/settings/max-daily-loss", json={"amount": 0})
            assert r_auto.status_code == 200
            assert r_auto.json()["is_custom_max_daily_loss"] is False
    finally:
        asyncio.run(engine.dispose())


