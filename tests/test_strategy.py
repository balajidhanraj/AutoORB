"""Comprehensive unit tests for OAuth/TOTP, ORB rules, VWAP/RSI, strikes, Risk Manager, and Web UI."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pyotp
from starlette.requests import Request

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from broker.auth import (
    ShoonyaAuthenticator,
    parse_oauth_code_from_url,
    should_wait_for_totp_window,
)
from broker.instrument_mapper import (
    InstrumentMapper,
    get_target_expiry,
    round_to_strike,
)
from broker.shoonya_client import ShoonyaClient
from config.settings import IST, AppSettings
from engine.candle_builder import Candle, CandleBuilder
from engine.indicators import calculate_intraday_vwap, calculate_wilder_rsi
from engine.risk_manager import PositionState, RiskManager
from engine.strategy_orb import ORBSignal, ORBStrategy
from main import TradingSystemOrchestrator
from ui.dashboard import (
    BrokerConnectRequest,
    BrokerFundsRequest,
    BrokerOrderRequest,
    create_app,
)


def _ist_dt(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return IST.localize(datetime(year, month, day, hour, minute, second))


def _make_settings(tmp_path: Path, **overrides: Any) -> AppSettings:
    defaults: Dict[str, Any] = {
        "mode": "PAPER",
        "token_file_path": str(tmp_path / "daily_token.json"),
        "data_dir": str(tmp_path / "data"),
        "log_dir": str(tmp_path / "logs"),
        "sebi_algo_id": "ALGO_TEST_2026",
    }
    defaults.update(overrides)
    return AppSettings(**defaults)


# ---------------------------------------------------------------------------
# 1. OAuth 2.0 & TOTP Tests
# ---------------------------------------------------------------------------

def test_parse_oauth_code_from_url() -> None:
    url = "http://127.0.0.1:8080/callback?code=AUTH_CODE_98765&state=xyz"
    assert parse_oauth_code_from_url(url) == "AUTH_CODE_98765"
    assert parse_oauth_code_from_url("http://127.0.0.1:8080/callback?error=denied") is None
    assert parse_oauth_code_from_url("") is None


def test_totp_boundary_wait_and_generation(tmp_path: Path) -> None:
    wait_flag, wait_sec = should_wait_for_totp_window(current_epoch=27.5)
    assert wait_flag is True
    assert wait_sec >= 5.0

    no_wait, zero_sec = should_wait_for_totp_window(current_epoch=1700000010.0)
    assert no_wait is False
    assert zero_sec == 0.0

    sleeps: List[float] = []
    secret = pyotp.random_base32()
    settings = _make_settings(tmp_path, shoonya_totp_secret=secret)
    auth = ShoonyaAuthenticator(settings, api_client=MagicMock(), sleep_fn=sleeps.append)

    otp = auth.generate_totp(current_epoch=28.0)  # 28 % 30 = 28 > 26
    assert len(otp) == 6 and otp.isdigit()
    assert len(sleeps) == 1 and sleeps[0] >= 5.0


def test_load_cached_token_and_retry_alert(tmp_path: Path) -> None:
    token_path = tmp_path / "daily_token.json"
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    token_path.write_text(
        json.dumps({"date": today_str, "access_token": "TOK_123"}),
        encoding="utf-8",
    )

    api = MagicMock()
    api.get_limits.return_value = {"stat": "Ok", "cash": "200000"}
    settings = _make_settings(
        tmp_path,
        shoonya_user_id="USR1",
        shoonya_account_id="ACC1",
        token_file_path=str(token_path),
    )
    auth = ShoonyaAuthenticator(settings, api_client=api)
    token = auth.load_cached_token()
    assert token == "TOK_123"
    api.injectOAuthHeader.assert_called_once_with("TOK_123", "USR1", "ACC1")

    # Verify 3-attempt retry failure notifies Telegram
    notifier = MagicMock()
    sleeps: List[float] = []
    auth_fail = ShoonyaAuthenticator(
        _make_settings(tmp_path, shoonya_user_id="USR1", token_file_path=str(tmp_path / "missing.json")),
        api_client=MagicMock(),
        notifier=notifier,
        sleep_fn=sleeps.append,
    )
    auth_fail.login_via_selenium = MagicMock(side_effect=RuntimeError("Browser failed"))  # type: ignore[method-assign]
    assert auth_fail.ensure_authenticated() is None
    assert auth_fail.login_via_selenium.call_count == 3
    assert sleeps == [10, 25]
    notifier.send_async.assert_called_once()
    assert "CRITICAL: Morning Login Failed" in notifier.send_async.call_args[0][0]


# ---------------------------------------------------------------------------
# 2. Instrument Strike Rounding & Expiry Tests
# ---------------------------------------------------------------------------

def test_strike_rounding_and_delta_selection(tmp_path: Path) -> None:
    assert round_to_strike(24520.0, 50) == 24500
    assert round_to_strike(24530.0, 50) == 24550
    assert round_to_strike(51440.0, 100) == 51400
    assert round_to_strike(51460.0, 100) == 51500

    client = ShoonyaClient(_make_settings(tmp_path))
    mapper = InstrumentMapper(client)
    ce_strike = mapper.select_strike("NIFTY", 24500.0, "CE", vix=15.0, ref_date=date(2026, 11, 16))
    pe_strike = mapper.select_strike("NIFTY", 24500.0, "PE", vix=15.0, ref_date=date(2026, 11, 16))
    assert ce_strike in (24450, 24500)
    assert pe_strike in (24500, 24550)

    bnf_contract = mapper.select_option_contract("BANKNIFTY", 51500.0, "CE", vix=15.0, ref_date=date(2026, 11, 16))
    assert bnf_contract.lot_size == 30
    assert bnf_contract.strike in (51400, 51500)


def test_tuesday_weekly_and_monthly_expiry_resolution() -> None:
    # Mon Nov 16, 2026 -> Weekly Tuesday is Nov 17, 2026
    assert get_target_expiry("NIFTY", ref_date=date(2026, 11, 16)) == date(2026, 11, 17)
    # If Nov 17 is an exchange holiday, shifts to Monday Nov 16
    assert get_target_expiry("NIFTY", ref_date=date(2026, 11, 16), holidays={date(2026, 11, 17)}) == date(2026, 11, 16)

    # Nov 2026 last Tuesday: Nov 30 is Mon, so Nov 24 is last Tuesday
    assert get_target_expiry("BANKNIFTY", ref_date=date(2026, 11, 16)) == date(2026, 11, 24)
    # Past Nov 24 -> rolls to Dec 2026 last Tuesday (Dec 29, 2026)
    assert get_target_expiry("BANKNIFTY", ref_date=date(2026, 11, 25)) == date(2026, 12, 29)


# ---------------------------------------------------------------------------
# 3. Candle Builder, VWAP, and Wilder RSI(14) Tests
# ---------------------------------------------------------------------------

def test_candle_builder_tick_aggregation_and_close() -> None:
    builder = CandleBuilder("NIFTY", "NSE", "26000")
    closed_events: List[Candle] = []
    builder.register_close_callback(lambda _sym, c, _all: closed_events.append(c))

    builder.on_tick(24500.0, _ist_dt(2026, 11, 16, 9, 15, 5), volume=100)
    builder.on_tick(24525.0, _ist_dt(2026, 11, 16, 9, 17, 30), volume=150)
    builder.on_tick(24490.0, _ist_dt(2026, 11, 16, 9, 19, 45), volume=50)
    # Next bucket triggers close of 09:15 bar
    closed = builder.on_tick(24510.0, _ist_dt(2026, 11, 16, 9, 20, 1), volume=80)

    assert closed is not None
    assert len(closed_events) == 1
    assert closed.open == 24500.0
    assert closed.high == 24525.0
    assert closed.low == 24490.0
    assert closed.close == 24490.0
    assert closed.volume == 300


def test_vwap_and_wilder_rsi_calculations() -> None:
    # Include a prior day candle that must be ignored by intraday VWAP
    candles = [
        Candle(_ist_dt(2026, 11, 13, 15, 25), 24000, 24010, 23990, 24000, volume=1000, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 15), 24500, 24530, 24490, 24520, volume=100, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 20), 24520, 24560, 24510, 24550, volume=300, is_closed=True),
    ]
    vwap = calculate_intraday_vwap(candles, session_date=date(2026, 11, 16))
    tp1 = (24530 + 24490 + 24520) / 3.0
    tp2 = (24560 + 24510 + 24550) / 3.0
    expected_vwap = round((tp1 * 100 + tp2 * 300) / 400, 4)
    assert vwap == expected_vwap

    # Zero-volume spot fallback
    zero_vol_candles = [
        Candle(_ist_dt(2026, 11, 16, 9, 15), 24500, 24530, 24490, 24520, volume=0, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 20), 24520, 24560, 24510, 24550, volume=0, is_closed=True),
    ]
    assert calculate_intraday_vwap(zero_vol_candles) == round((tp1 + tp2) / 2.0, 4)

    # Wilder RSI
    rising = [100.0 + i * 2.0 for i in range(20)]
    falling = [200.0 - i * 2.0 for i in range(20)]
    assert calculate_wilder_rsi(rising, 14) == 100.0
    assert calculate_wilder_rsi(falling, 14) == 0.0
    assert calculate_wilder_rsi([100.0, 101.0], 14) == 50.0


# ---------------------------------------------------------------------------
# 4. ORB Strategy Signal & Filter Tests
# ---------------------------------------------------------------------------

def _build_warmup_candles(base_price: float, count: int = 20, step: float = 0.5) -> List[Candle]:
    start = _ist_dt(2026, 11, 13, 13, 30)
    out = []
    px = base_price
    for i in range(count):
        ts = start + timedelta(minutes=5 * i)
        out.append(Candle(ts, px, px + 2, px - 2, px + step, volume=0, is_closed=True))
        px += step
    return out


def test_orb_wide_range_rejection(tmp_path: Path) -> None:
    strategy = ORBStrategy(_make_settings(tmp_path))
    orb_candles = [
        Candle(_ist_dt(2026, 11, 16, 9, 15), 24500, 24550, 24480, 24540, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 20), 24540, 24720, 24530, 24700, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 25), 24700, 24710, 24500, 24680, is_closed=True),
    ]
    # Range = (24720 - 24480) / 24480 * 100 = 0.98% > 0.75%
    state = strategy.lock_opening_range("NIFTY", orb_candles)
    assert state.is_locked is True
    assert state.orh == 24720.0
    assert state.orl == 24480.0
    assert state.no_trade_reason is not None

    breakout_bar = Candle(_ist_dt(2026, 11, 16, 9, 30), 24680, 24800, 24680, 24790, is_closed=True)
    assert strategy.evaluate_candle_close("NIFTY", breakout_bar, orb_candles + [breakout_bar], india_vix=15.0) is None


def test_ce_and_pe_breakouts_with_vix_and_time_filters(tmp_path: Path) -> None:
    # Bullish CE setup
    strategy = ORBStrategy(_make_settings(tmp_path))
    warmup = _build_warmup_candles(24450.0, count=18, step=1.0)
    orb = [
        Candle(_ist_dt(2026, 11, 16, 9, 15), 24500, 24520, 24495, 24515, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 20), 24515, 24530, 24505, 24525, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 25), 24525, 24540, 24510, 24535, is_closed=True),
    ]
    strategy.lock_opening_range("NIFTY", orb, session_date=date(2026, 11, 16))
    breakout_ce = Candle(_ist_dt(2026, 11, 16, 9, 30), 24535, 24585, 24535, 24580, is_closed=True)
    all_bars = warmup + orb + [breakout_ce]

    # VIX < 12 blocks signal
    assert strategy.evaluate_candle_close("NIFTY", breakout_ce, all_bars, india_vix=11.5) is None
    # After 14:15 cutoff blocks signal
    assert strategy.evaluate_candle_close(
        "NIFTY", breakout_ce, all_bars, india_vix=14.0, current_time_ist=_ist_dt(2026, 11, 16, 14, 16)
    ) is None

    sig_ce = strategy.evaluate_candle_close("NIFTY", breakout_ce, all_bars, india_vix=14.0)
    assert sig_ce is not None
    assert sig_ce.signal_type == "CE"
    assert sig_ce.rsi > 60.0
    assert sig_ce.spot_close > sig_ce.vwap

    # Bearish PE setup
    strategy_pe = ORBStrategy(_make_settings(tmp_path))
    warmup_down = _build_warmup_candles(51600.0, count=18, step=-4.0)
    orb_bnf = [
        Candle(_ist_dt(2026, 11, 16, 9, 15), 51520, 51540, 51480, 51500, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 20), 51500, 51510, 51470, 51485, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 25), 51485, 51500, 51460, 51475, is_closed=True),
    ]
    strategy_pe.lock_opening_range("BANKNIFTY", orb_bnf, session_date=date(2026, 11, 16))
    breakout_pe = Candle(_ist_dt(2026, 11, 16, 9, 30), 51475, 51475, 51380, 51390, is_closed=True)
    sig_pe = strategy_pe.evaluate_candle_close(
        "BANKNIFTY", breakout_pe, warmup_down + orb_bnf + [breakout_pe], india_vix=15.0
    )
    assert sig_pe is not None
    assert sig_pe.signal_type == "PE"
    assert sig_pe.rsi < 40.0
    assert sig_pe.spot_close < sig_pe.vwap


# ---------------------------------------------------------------------------
# 5. Risk Manager Execution, Partial Target, Breakeven, Trailing & Kill Switch
# ---------------------------------------------------------------------------

def test_bracket_breakeven_target1_and_trailing_sl(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, mode="PAPER", paper_slippage_pts=1.0)
    client = ShoonyaClient(settings)
    mapper = InstrumentMapper(client)
    rm = RiskManager(settings, client, mapper)

    signal = ORBSignal(
        index_name="NIFTY",
        signal_type="CE",
        candle_time=_ist_dt(2026, 11, 16, 9, 30),
        spot_close=24580.0,
        orh=24540.0,
        orl=24495.0,
        vwap=24530.0,
        rsi=68.0,
        india_vix=15.0,
    )
    pos = rm.execute_entry(signal, option_ltp=200.0)
    assert pos is not None
    assert pos.state == PositionState.OPEN_FULL
    assert pos.initial_qty == 130  # 2 lots * 65
    assert pos.entry_avg_price == 201.0  # 200.0 + 1.0 paper slippage
    assert pos.sl_trigger_price == 181.0  # 201 - 20
    assert pos.sl_limit_price == 178.0    # 181 - 3

    opt_key = f"{pos.contract.exchange}|{pos.contract.token}"

    # +25 pts gain (226.0) -> move SL to cost (201.0)
    client.update_ltp(opt_key, 226.0)
    rm.on_option_tick("NIFTY", 226.0)
    assert pos.state == PositionState.OPEN_FULL_BREAKEVEN
    assert pos.sl_trigger_price == 201.0
    assert pos.remaining_qty == 130

    # +30 pts gain (231.0) -> Target 1 books 50% (65 qty) & keeps SL on remaining 65 qty
    client.update_ltp(opt_key, 231.0)
    rm.on_option_tick("NIFTY", 231.0)
    assert pos.state == PositionState.OPEN_PARTIAL_TRAILING
    assert pos.remaining_qty == 65
    assert pos.realized_pnl > 0

    # +15 pts above Target 1 (246.0) -> trail SL upward by 15 pts to 216.0
    client.update_ltp(opt_key, 246.0)
    rm.on_option_tick("NIFTY", 246.0)
    assert pos.sl_trigger_price == 216.0

    # Another +15 pts gain (261.0) -> trail SL to 231.0
    client.update_ltp(opt_key, 261.0)
    rm.on_option_tick("NIFTY", 261.0)
    assert pos.sl_trigger_price == 231.0

    # Drop below trailing SL (230.0) -> simulated SL-LMT executes and closes remaining 50%
    client.update_ltp(opt_key, 230.0)
    rm.on_option_tick("NIFTY", 230.0)
    assert "NIFTY" not in rm.positions
    assert len(rm.completed_trades) == 1
    assert rm.completed_trades[0].exit_reason == "TRAILING_SL"


def test_entry_order_timeout_modify_and_cancel(tmp_path: Path) -> None:
    mock_api = MagicMock()
    mock_api.place_order.return_value = {"stat": "Ok", "norenordno": "ORD_100"}
    mock_api.modify_order.return_value = {"stat": "Ok", "result": "ORD_100"}
    mock_api.cancel_order.return_value = {"stat": "Ok", "result": "ORD_100"}
    mock_api.get_order_book.return_value = [{"norenordno": "ORD_100", "status": "OPEN"}]

    settings = _make_settings(tmp_path, mode="LIVE")
    client = ShoonyaClient(settings, raw_api=mock_api)
    mapper = InstrumentMapper(client)
    rm = RiskManager(settings, client, mapper)

    signal = ORBSignal(
        index_name="NIFTY",
        signal_type="CE",
        candle_time=_ist_dt(2026, 11, 16, 9, 35),
        spot_close=24580.0,
        orh=24540.0,
        orl=24495.0,
        vwap=24530.0,
        rsi=68.0,
        india_vix=15.0,
    )
    pos = rm.execute_entry(signal, option_ltp=150.0)
    assert pos is not None and pos.state == PositionState.ENTERING
    t0 = pos.entry_submitted_monotonic

    # After 11 seconds -> modify once
    rm.check_pending_entries(now_monotonic=t0 + 11.0)
    mock_api.modify_order.assert_called_once()
    assert pos.entry_modified_once is True

    # After 21 seconds -> cancel and clear position
    rm.check_pending_entries(now_monotonic=t0 + 21.0)
    mock_api.cancel_order.assert_called_once_with(orderno="ORD_100")
    assert "NIFTY" not in rm.positions
    assert rm.trades_taken_today == 0


def test_mtm_kill_switch_and_csv_export(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, mode="PAPER", max_daily_loss=5000.0)
    client = ShoonyaClient(settings)
    mapper = InstrumentMapper(client)
    rm = RiskManager(settings, client, mapper)

    signal = ORBSignal(
        index_name="NIFTY",
        signal_type="CE",
        candle_time=_ist_dt(2026, 11, 16, 9, 35),
        spot_close=24580.0,
        orh=24540.0,
        orl=24495.0,
        vwap=24530.0,
        rsi=68.0,
        india_vix=15.0,
    )
    pos = rm.execute_entry(signal, option_ltp=250.0)
    assert pos is not None
    # Entry = 251.0 * 130 qty; drop to 210.0 -> unrealized = -5330 <= -5000
    # Wait: SL trigger is 231.0 in paper mode, so simulate sudden gap/realized loss directly on active pos
    pos.unrealized_pnl = -5200.0
    triggered = rm.check_mtm_kill_switch()

    assert triggered is True
    assert rm.halted is True
    assert "NIFTY" not in rm.positions
    assert len(rm.completed_trades) == 1

    csv_path = rm.export_daily_trades_csv()
    assert csv_path.exists()
    content = csv_path.read_text(encoding="utf-8")
    assert "NIFTY" in content and "KILL_SWITCH_MTM" in content


# ---------------------------------------------------------------------------
# 6. FastAPI Web Dashboard Endpoint Tests
# ---------------------------------------------------------------------------

def test_web_ui_dashboard_endpoints(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, mode="PAPER")
    orchestrator = TradingSystemOrchestrator(settings=settings)
    app = create_app(orchestrator)

    route_paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/", "/api/status", "/api/toggle-mode", "/api/kill-switch", "/api/trades/csv", "/health"}.issubset(
        route_paths
    )
    handlers = app.state.handlers

    fake_req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    home = handlers["index"](fake_req)
    assert home.status_code == 200
    assert "Shoonya Automated ORB Terminal" in home.body.decode("utf-8")

    status_resp = handlers["api_status"]()
    assert status_resp.status_code == 200
    payload = json.loads(status_resp.body.decode("utf-8"))
    assert payload["mode"] == "PAPER"
    assert "NIFTY" in payload["indices"]
    assert "BANKNIFTY" in payload["indices"]

    toggle_resp = handlers["api_toggle_mode"]()
    assert toggle_resp.status_code == 200
    assert json.loads(toggle_resp.body.decode("utf-8"))["mode"] == "LIVE"

    kill_resp = handlers["api_kill_switch"]()
    assert kill_resp.status_code == 200
    assert json.loads(kill_resp.body.decode("utf-8"))["snapshot"]["halted"] is True

    csv_resp = handlers["api_trades_csv"]()
    assert csv_resp.status_code == 200
    assert "trade_id,date,index_name" in csv_resp.body.decode("utf-8")

    health_resp = handlers["health"]()
    assert health_resp.status_code == 200
    assert json.loads(health_resp.body.decode("utf-8"))["status"] == "ok"


def test_live_broker_ui_endpoints(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, mode="PAPER", capital=200000.0)
    orchestrator = TradingSystemOrchestrator(settings=settings)

    # Mock raw broker API for sandbox-safe live terminal verification
    mock_api = MagicMock()
    mock_api.access_token = ""
    mock_api.get_limits.return_value = {
        "stat": "Ok",
        "prfname": "SHOONYA",
        "cash": "12500.50",
        "payin": "0.00",
        "payout": "0.00",
        "brkcollamt": "0.00",
        "marginused": "2500.00",
        "request_time": "10:15:00 16-11-2026",
    }
    mock_api.get_user_details.return_value = {
        "stat": "Ok",
        "uid": "DEMO999",
        "actid": "DEMO999",
        "uname": "DEMO USER",
    }
    mock_api.get_order_book.return_value = [
        {
            "norenordno": "26111400043194",
            "norentm": "22:25:09 15-11-2026",
            "exch": "NSE",
            "tsym": "TCS-EQ",
            "trantype": "B",
            "s_prdt_ali": "CNC",
            "prctyp": "LMT",
            "qty": "1",
            "fillshares": "0",
            "prc": "2407.50",
            "status": "REJECTED",
            "ordersource": "WEB",
            "rejreason": "Margin Shortfall",
        }
    ]
    mock_api.get_positions.return_value = []
    mock_api.place_order.return_value = {"stat": "Ok", "norenordno": "26111600000001"}

    orchestrator.client.api = mock_api
    orchestrator.auth.api = mock_api
    orchestrator.auth.login_via_selenium = MagicMock(side_effect=lambda: setattr(mock_api, "access_token", "LIVE_TOK") or "LIVE_TOK")  # type: ignore[method-assign]

    app = create_app(orchestrator)
    handlers = app.state.handlers

    # 1. Connect new Shoonya user account
    conn_resp = handlers["api_broker_connect"](
        BrokerConnectRequest(
            user_id="demo999",
            password="secret",
            totp_secret=pyotp.random_base32(),
            secret_key="a" * 64,
        )
    )
    assert conn_resp.status_code == 200
    conn_data = json.loads(conn_resp.body.decode("utf-8"))
    assert conn_data["ok"] is True
    assert orchestrator.settings.shoonya_user_id == "DEMO999"
    assert orchestrator.settings.shoonya_api_key == "DEMO999_U"
    assert conn_data["snapshot"]["broker"]["connected"] is True
    assert conn_data["snapshot"]["broker"]["cash"] == 12500.5
    assert conn_data["snapshot"]["broker"]["orders"][0]["norenordno"] == "26111400043194"

    # 2. Update strategy capital + sync limits
    funds_resp = handlers["api_broker_funds"](BrokerFundsRequest(amount=50000.0, action="add"))
    assert funds_resp.status_code == 200
    funds_data = json.loads(funds_resp.body.decode("utf-8"))
    assert funds_data["ok"] is True
    assert funds_data["capital"] == 250000.0

    # 3. Place manual live broker order + refresh
    order_resp = handlers["api_broker_order"](
        BrokerOrderRequest(
            exchange="NSE",
            tradingsymbol="TCS-EQ",
            buy_or_sell="B",
            quantity=1,
            price_type="LMT",
            price=2407.50,
            product_type="C",
        )
    )
    assert order_resp.status_code == 200
    order_data = json.loads(order_resp.body.decode("utf-8"))
    assert order_data["ok"] is True
    assert order_data["response"]["norenordno"] == "26111600000001"
    mock_api.place_order.assert_called_once()

    refresh_resp = handlers["api_broker_refresh"]()
    assert refresh_resp.status_code == 200
    assert json.loads(refresh_resp.body.decode("utf-8"))["ok"] is True
