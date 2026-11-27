"""Comprehensive unit tests for OAuth/TOTP, ORB rules, VWAP/RSI, strikes, Risk Manager, and Web UI."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta
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
from config.settings import IST, AppSettings, is_nse_trading_day
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
        "enable_nifty_trading": True,
        "nifty_use_htf_swing_hybrid": False,
        "dynamic_sl_pct": 0.0,
        "require_vwap_slope": False,
        "use_supertrend_filter": False,
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
        Candle(_ist_dt(2026, 11, 16, 9, 30), 24680, 24700, 24650, 24690, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 35), 24690, 24710, 24670, 24705, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 40), 24705, 24715, 24685, 24700, is_closed=True),
    ]
    # Range = (24720 - 24480) / 24480 * 100 = 0.98% > 0.85%
    state = strategy.lock_opening_range("NIFTY", orb_candles)
    assert state.is_locked is True
    assert state.orh == 24720.0
    assert state.orl == 24480.0
    assert state.no_trade_reason is not None

    breakout_bar = Candle(_ist_dt(2026, 11, 16, 10, 5), 24680, 24800, 24680, 24790, is_closed=True)
    assert strategy.evaluate_candle_close("NIFTY", breakout_bar, orb_candles + [breakout_bar], india_vix=15.0) is None


def test_ce_and_pe_breakouts_with_vix_and_time_filters(tmp_path: Path) -> None:
    # Bullish CE setup
    strategy = ORBStrategy(_make_settings(tmp_path))
    warmup = _build_warmup_candles(24450.0, count=18, step=1.0)
    orb = [
        Candle(_ist_dt(2026, 11, 16, 9, 15), 24500, 24520, 24495, 24515, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 20), 24515, 24530, 24505, 24525, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 25), 24525, 24560, 24510, 24555, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 30), 24555, 24560, 24520, 24545, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 35), 24545, 24555, 24515, 24535, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 40), 24535, 24550, 24515, 24530, is_closed=True),
    ]
    strategy.lock_opening_range("NIFTY", orb, session_date=date(2026, 11, 16))
    breakout_ce = Candle(_ist_dt(2026, 11, 16, 10, 5), 24535, 24595, 24535, 24590, is_closed=True)
    all_bars = warmup + orb + [breakout_ce]

    # VIX < 12 blocks signal
    assert strategy.evaluate_candle_close("NIFTY", breakout_ce, all_bars, india_vix=11.5) is None
    # After 13:15 cutoff blocks signal
    assert strategy.evaluate_candle_close(
        "NIFTY", breakout_ce, all_bars, india_vix=14.0, current_time_ist=_ist_dt(2026, 11, 16, 13, 20)
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
        Candle(_ist_dt(2026, 11, 16, 9, 15), 51520, 51570, 51480, 51500, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 20), 51500, 51510, 51470, 51485, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 25), 51485, 51500, 51460, 51475, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 30), 51475, 51490, 51450, 51465, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 35), 51465, 51480, 51445, 51460, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 40), 51460, 51470, 51440, 51455, is_closed=True),
    ]
    strategy_pe.lock_opening_range("BANKNIFTY", orb_bnf, session_date=date(2026, 11, 16))
    breakout_pe = Candle(_ist_dt(2026, 11, 16, 10, 5), 51475, 51475, 51380, 51390, is_closed=True)
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
        candle_time=_ist_dt(2026, 11, 16, 10, 5),
        spot_close=24580.0,
        orh=24540.0,
        orl=24495.0,
        vwap=24530.0,
        rsi=68.0,
        india_vix=15.0,
    )
    pos = rm.execute_entry(signal, option_ltp=200.0, lots=2)
    assert pos is not None
    assert pos.state == PositionState.OPEN_FULL
    assert pos.initial_qty == 130  # 2 lots * 65
    assert pos.entry_avg_price == 201.0  # 200.0 + 1.0 paper slippage
    assert pos.sl_trigger_price == 176.0  # 201 - 25 (optimized SL)
    assert pos.sl_limit_price == 173.0    # 176 - 3

    opt_key = f"{pos.contract.exchange}|{pos.contract.token}"

    # +20 pts gain (221.0) -> move SL to cost (201.0)
    client.update_ltp(opt_key, 221.0)
    rm.on_option_tick("NIFTY", 221.0)
    assert pos.state == PositionState.OPEN_FULL_BREAKEVEN
    assert pos.sl_trigger_price == 201.0
    assert pos.remaining_qty == 130

    # +40 pts gain (241.0) -> Target 1 books 50% (65 qty) & keeps SL on remaining 65 qty
    client.update_ltp(opt_key, 241.0)
    rm.on_option_tick("NIFTY", 241.0)
    assert pos.state == PositionState.OPEN_PARTIAL_TRAILING
    assert pos.remaining_qty == 65
    assert pos.realized_pnl > 0

    # +15 pts above Target 1 (256.0) -> trail SL upward
    client.update_ltp(opt_key, 256.0)
    rm.on_option_tick("NIFTY", 256.0)
    assert pos.sl_trigger_price >= 201.0

    # Another gain -> trail SL higher
    client.update_ltp(opt_key, 271.0)
    rm.on_option_tick("NIFTY", 271.0)
    trail_sl = pos.sl_trigger_price
    assert trail_sl > 201.0

    # Drop below trailing SL -> simulated SL-LMT executes and closes remaining 50%
    client.update_ltp(opt_key, trail_sl - 1.0)
    rm.on_option_tick("NIFTY", trail_sl - 1.0)
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
    rm.set_max_daily_loss(5000.0)

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


def test_dynamic_max_daily_loss(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, mode="PAPER")
    client = ShoonyaClient(settings)
    mapper = InstrumentMapper(client)
    rm = RiskManager(settings, client, mapper)

    # Default Hybrid daily risk guardrail is ₹10,000 (Mon-Fri & Tuesday)
    mon_dt = _ist_dt(2026, 11, 16, 10, 30)  # Monday
    assert rm.get_dynamic_max_daily_loss(mon_dt) == 10000.0

    tue_dt = _ist_dt(2026, 11, 17, 10, 30)  # Tuesday
    assert rm.get_dynamic_max_daily_loss(tue_dt) == 10000.0

    # Custom user override via set_max_daily_loss works cleanly
    ok, _ = rm.set_max_daily_loss(6000.0)
    assert ok is True
    assert rm.get_dynamic_max_daily_loss(mon_dt) == 6000.0

    # Restoring AUTO returns to ₹10,000 default floor
    ok, _ = rm.set_max_daily_loss(0)
    assert ok is True
    assert rm.get_dynamic_max_daily_loss(mon_dt) == 10000.0


def test_hybrid_nifty_optA_and_banknifty_optH_pyramiding(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, mode="PAPER", max_trades_per_day=2, banknifty_be_pyramiding=True)
    client = ShoonyaClient(settings)
    mapper = InstrumentMapper(client)
    rm = RiskManager(settings, client, mapper)

    # 1. NIFTY (Option A): blocks overlapping Leg #2 even when Leg #1 is at Breakeven
    nf_sig = ORBSignal(
        index_name="NIFTY",
        signal_type="CE",
        candle_time=_ist_dt(2026, 11, 16, 10, 15),
        spot_close=24580.0,
        orh=24540.0,
        orl=24495.0,
        vwap=24530.0,
        rsi=68.0,
        india_vix=15.0,
    )
    nf_pos = rm.execute_entry(nf_sig, option_ltp=125.0)
    assert nf_pos is not None
    assert rm.can_take_new_trade("NIFTY") is False
    nf_pos.state = PositionState.OPEN_FULL_BREAKEVEN
    assert rm.can_take_new_trade("NIFTY") is False  # Strict sequential no-overlap on NIFTY

    # 2. BANKNIFTY (Option H): blocks Leg #2 while OPEN_FULL, allows Leg #2 once OPEN_FULL_BREAKEVEN
    bnf_sig = ORBSignal(
        index_name="BANKNIFTY",
        signal_type="CE",
        candle_time=_ist_dt(2026, 11, 16, 10, 20),
        spot_close=52500.0,
        orh=52400.0,
        orl=52200.0,
        vwap=52350.0,
        rsi=68.0,
        india_vix=15.0,
    )
    bnf_pos1 = rm.execute_entry(bnf_sig, option_ltp=260.0)
    assert bnf_pos1 is not None
    assert rm.can_take_new_trade("BANKNIFTY") is False  # Blocked while OPEN_FULL

    bnf_pos1.state = PositionState.OPEN_FULL_BREAKEVEN
    assert rm.can_take_new_trade("BANKNIFTY") is True  # Allowed once risk-free at BE!

    bnf_pos2 = rm.execute_entry(bnf_sig, option_ltp=310.0)
    assert bnf_pos2 is not None
    assert bnf_pos2.position_key == "BANKNIFTY#2"
    assert rm.trades_taken_by_index["BANKNIFTY"] == 2
    assert rm.can_take_new_trade("BANKNIFTY") is False  # Max 2/day reached for BANKNIFTY


def test_nf_bnf_30m_max4_global_single_order_defaults(tmp_path: Path) -> None:
    """Verify production defaults: ₹1L capital, NIFTY+BANKNIFTY, 30m ORB (10:00-13:15), global 1-active-order lock, Max 4/day."""
    prod_settings = AppSettings(
        mode="PAPER",
        token_file_path=str(tmp_path / "daily_token.json"),
        data_dir=str(tmp_path / "data"),
        log_dir=str(tmp_path / "logs"),
    )
    assert prod_settings.capital == 100000.0
    assert prod_settings.enable_nifty_trading is True
    assert prod_settings.enable_banknifty_trading is True
    assert prod_settings.use_supertrend_filter is True
    assert prod_settings.supertrend_period == 10
    assert prod_settings.supertrend_multiplier == 3.0
    assert prod_settings.nifty_use_ema_body_filter is True
    assert prod_settings.banknifty_be_pyramiding is False
    assert prod_settings.max_trades_per_day == 4
    assert prod_settings.orb_window_minutes == 30
    assert prod_settings.entry_start_time == time(10, 0, 0)
    assert prod_settings.nifty_htf_entry_start_time == time(10, 0, 0)
    assert prod_settings.entry_cutoff_time == time(13, 15, 0)
    assert prod_settings.require_vwap_slope is False
    assert prod_settings.dynamic_sl_pct == 0.0

    client = ShoonyaClient(prod_settings)
    mapper = InstrumentMapper(client)
    rm = RiskManager(prod_settings, client, mapper)

    # Default: both NIFTY and BANKNIFTY enabled with Supertrend(10, 3) + confirmations
    assert rm.can_take_new_trade("NIFTY") is True
    assert rm.can_take_new_trade("BANKNIFTY") is True

    # Toggle NIFTY OFF/ON via checkbox API helper
    ok, _ = rm.set_enabled_indices(nifty=False, banknifty=True)
    assert ok is True
    assert rm.can_take_new_trade("NIFTY") is False
    ok, _ = rm.set_enabled_indices(nifty=True, banknifty=True)
    assert ok is True
    assert rm.can_take_new_trade("NIFTY") is True

    bnf_sig = ORBSignal(
        index_name="BANKNIFTY",
        signal_type="CE",
        candle_time=_ist_dt(2026, 11, 16, 10, 15),
        spot_close=52500.0,
        orh=52400.0,
        orl=52200.0,
        vwap=52350.0,
        rsi=68.0,
        india_vix=15.0,
    )
    nf_sig = ORBSignal(
        index_name="NIFTY",
        signal_type="CE",
        candle_time=_ist_dt(2026, 11, 16, 10, 20),
        spot_close=24580.0,
        orh=24540.0,
        orl=24495.0,
        vwap=24530.0,
        rsi=68.0,
        india_vix=15.0,
    )
    for leg_idx in range(1, 5):
        sig = bnf_sig if leg_idx % 2 == 1 else nf_sig
        pos = rm.execute_entry(sig, option_ltp=500.0 if sig.index_name == "BANKNIFTY" else 150.0)
        assert pos is not None
        assert rm.trades_taken_today == leg_idx
        # Hold BOTH NIFTY and BANKNIFTY while ANY order is running, WITHOUT halting system
        assert rm.halted is False
        assert rm.can_take_new_trade("BANKNIFTY") is False
        assert rm.can_take_new_trade("NIFTY") is False
        # Once running order completes cleanly, next trade on either index is allowed up to Max 4/day
        rm._close_position_record_locked(pos, exit_price=pos.entry_avg_price, exit_reason="BREAKEVEN_SL")
        if leg_idx < 4:
            assert rm.can_take_new_trade("BANKNIFTY") is True
            assert rm.can_take_new_trade("NIFTY") is True
        else:
            assert rm.can_take_new_trade("BANKNIFTY") is False
            assert rm.can_take_new_trade("NIFTY") is False

    # Verify Supertrend(10, 3) Trailing Exit closes CE when spot closes below Supertrend line
    rm.reset_daily()
    ce_pos = rm.execute_entry(bnf_sig, option_ltp=500.0)
    assert ce_pos is not None
    # Spot above ST line -> stays open
    assert rm.check_supertrend_trailing_exit("BANKNIFTY", spot_close=52600.0, supertrend_line=52300.0, supertrend_dir=1) is False
    assert len(rm.positions) == 1
    # Spot closes below ST line -> exits with SUPERTREND_TRAIL
    assert rm.check_supertrend_trailing_exit("BANKNIFTY", spot_close=52250.0, supertrend_line=52300.0, supertrend_dir=-1) is True
    assert len(rm.positions) == 0
    assert rm.completed_trades[-1].exit_reason == "SUPERTREND_TRAIL"


def test_auto_square_off_all_and_exit_position(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, mode="PAPER")
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
    assert "NIFTY" in rm.positions

    # Test auto square off (which runs at 15:15 IST)
    rm.auto_square_off_all(reason="EOD_1515_SQUAREOFF")
    assert "NIFTY" not in rm.positions
    assert rm.halted is True
    assert rm.halt_reason == PositionState.COMPLETED_FOR_DAY.value
    assert len(rm.completed_trades) == 1
    assert rm.completed_trades[0].exit_reason == "EOD_1515_SQUAREOFF"


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
    assert "radar" in payload["indices"]["NIFTY"]
    assert "radar_state" in payload["indices"]["NIFTY"]["radar"]
    assert "schedule" in payload
    assert len(payload["schedule"]["milestones"]) == 6

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


def test_dynamic_orb_window_and_option_quote_fallback(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, mode="PAPER")
    orch = TradingSystemOrchestrator(settings=settings)
    app = create_app(orch)
    handlers = app.state.handlers

    # 1. Test invalid window rejected
    ok, msg = orch.set_orb_window(20)
    assert ok is False

    # 2. Test 30-minute ORB window
    ok, msg = orch.set_orb_window(30)
    assert ok is True
    assert orch.settings.orb_window_minutes == 30
    assert orch.settings.orb_end_time == time(9, 45, 0)
    assert len(orch.strategy.get_orb_candle_times()) == 6

    # 3. Test API endpoint for ORB window selection
    from ui.dashboard import OrbWindowRequest
    res = app.state.handlers.get("api_set_orb_window", None)
    if res is None:
        for route in app.routes:
            if getattr(route, "path", "") == "/api/settings/orb-window":
                res = route.endpoint
                break
    assert res is not None
    endpoint_resp = res(OrbWindowRequest(minutes=5))
    data = json.loads(endpoint_resp.body.decode("utf-8"))
    assert data["ok"] is True
    assert data["orb_window_minutes"] == 5
    assert orch.settings.orb_end_time == time(9, 20, 0)

    # 4. Test REST quote fallback in RiskManager execute_entry
    orch.client.get_quotes = MagicMock(return_value={"stat": "Ok", "lp": "145.50"})
    signal = ORBSignal(
        index_name="NIFTY",
        signal_type="CE",
        candle_time=_ist_dt(2026, 11, 16, 9, 30),
        spot_close=25320.0,
        orh=25310.0,
        orl=25200.0,
        vwap=25270.0,
        rsi=70.0,
        india_vix=15.0,
    )
    pos = orch.risk_manager.execute_entry(signal)
    assert pos is not None
    assert pos.index_name == "NIFTY"
    assert pos.last_ltp == 145.50


def test_afternoon_orb_anchor_and_reauth_resilience(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, mode="PAPER")
    orch = TradingSystemOrchestrator(settings=settings)
    app = create_app(orch)

    # 1. Test Afternoon 1:00 PM - 1:15 PM ORB Anchor
    ok, msg = orch.set_orb_anchor("13:00", 15)
    assert ok is True
    assert orch.settings.orb_start_time == time(13, 0, 0)
    assert orch.settings.orb_end_time == time(13, 15, 0)
    assert orch.settings.orb_window_minutes == 15
    candle_times = orch.strategy.get_orb_candle_times()
    assert candle_times == (time(13, 0, 0), time(13, 5, 0), time(13, 10, 0))

    # 2. Test Recent / Auto Anchor
    ok, msg = orch.set_orb_anchor("recent", 15)
    assert ok is True
    assert orch.settings.orb_window_minutes == 15

    # 3. Test Reset Trades Counter
    orch.risk_manager.trades_taken_today = 2
    orch.risk_manager.halted = True
    orch.risk_manager.halt_reason = "MAX_TRADES_REACHED"
    ok, msg = orch.reset_daily_trades()
    assert ok is True
    assert orch.risk_manager.trades_taken_today == 0
    assert orch.risk_manager.halted is False
    assert orch.risk_manager.halt_reason is None

    # 4. Test Auto Reauth Hook
    reauth_called = []
    def fake_reauth():
        reauth_called.append(True)
        return True

    orch.client.register_reauth_callback(fake_reauth)
    assert orch.client._reauth_callback is fake_reauth
    assert orch.client.api._reauth_callback is fake_reauth


def test_indices_specs_only_nifty_and_banknifty(tmp_path: Path) -> None:
    from config.settings import INDEX_SPECS
    settings = _make_settings(tmp_path, mode="PAPER")
    orch = TradingSystemOrchestrator(settings=settings)

    # 1. Verify CRUDEOILM is removed, only NIFTY and BANKNIFTY remain
    assert "CRUDEOILM" not in INDEX_SPECS
    assert set(INDEX_SPECS.keys()) == {"NIFTY", "BANKNIFTY"}
    assert INDEX_SPECS["NIFTY"].lot_size == 65
    assert INDEX_SPECS["BANKNIFTY"].lot_size == 30

    # 2. Verify Option Contract Selection for NIFTY
    contract = orch.mapper.select_option_contract(
        index_name="NIFTY",
        spot_price=24580.0,
        option_type="CE",
        ref_date=date(2026, 11, 17),
    )
    assert contract.index_name == "NIFTY"
    assert contract.exchange == "NFO"
    assert contract.lot_size == 65

    # 3. Verify Auto Order Trigger for BANKNIFTY
    orch.client.update_ltp("NSE|26009", 52100.0)
    ok, msg, data = orch.trigger_auto_order("BANKNIFTY", signal_type="CE")
    assert ok is True
    assert "BANKNIFTY" in msg
    assert "BANKNIFTY" in orch.risk_manager.positions


def test_custom_lots_and_margin_display(tmp_path: Path) -> None:
    from ui.dashboard import SetLotsRequest
    settings = _make_settings(tmp_path, mode="PAPER")
    orch = TradingSystemOrchestrator(settings=settings)
    app = create_app(orch)

    # 1. Test updating lots via orchestrator
    ok, msg = orch.set_index_lots("NIFTY", 3)
    assert ok is True
    assert orch.custom_lots["NIFTY"] == 3
    assert orch.risk_manager.custom_lots["NIFTY"] == 3

    ok, msg = orch.set_index_lots("BANKNIFTY", 4)
    assert ok is True
    assert orch.custom_lots["BANKNIFTY"] == 4
    assert orch.risk_manager.custom_lots["BANKNIFTY"] == 4

    # 2. Test invalid lot rejection
    ok, _ = orch.set_index_lots("NIFTY", 0)
    assert ok is False
    ok, _ = orch.set_index_lots("NIFTY", 55)
    assert ok is False

    # 3. Test API endpoint for setting lots
    set_lots_handler = app.state.handlers["api_set_lots"]
    resp = set_lots_handler(SetLotsRequest(index_name="BANKNIFTY", lots=5))
    assert resp.status_code == 200
    resp_data = json.loads(resp.body.decode("utf-8"))
    assert resp_data["ok"] is True
    assert orch.custom_lots["BANKNIFTY"] == 5

    # 4. Verify radar snapshot contains lots, lot_size, total_qty, and estimated margin
    orch.client.update_ltp("NSE|26000", 24200.0)
    orch.client.update_ltp("NSE|26009", 52100.0)
    snap = orch.get_status_snapshot()
    nifty_radar = snap["indices"]["NIFTY"]["radar"]
    assert nifty_radar["lots"] == 3
    assert nifty_radar["lot_size"] == 65
    assert nifty_radar["total_qty"] == 195
    assert nifty_radar["est_margin"] > 0
    assert nifty_radar["atm_premium"] > 0
    assert nifty_radar["product_type"] == "M"
    assert nifty_radar["product_type_label"] == "NRML"
    assert snap["indices"]["BANKNIFTY"]["radar"]["product_type"] == "M"
    assert snap["indices"]["BANKNIFTY"]["radar"]["product_type_label"] == "NRML"

    # 5. Test API endpoint for setting product type (NRML vs MIS)
    from ui.dashboard import SetProductTypeRequest
    set_prod_handler = app.state.handlers["api_set_product_type"]
    resp = set_prod_handler(SetProductTypeRequest(index_name="BANKNIFTY", product_type="MIS"))
    assert resp.status_code == 200
    assert orch.custom_product_types["BANKNIFTY"] == "I"
    assert orch.risk_manager.custom_product_types["BANKNIFTY"] == "I"

    resp = set_prod_handler(SetProductTypeRequest(index_name="NIFTY", product_type="NRML"))
    assert resp.status_code == 200
    assert orch.custom_product_types["NIFTY"] == "M"


def test_master_alpha_strategy_pillars(tmp_path: Path) -> None:
    settings = _make_settings(
        tmp_path,
        daily_bias_filter=True,
        use_ema_filter=True,
        tuesday_expiry_boost=True,
        max_weekly_losses=2,
    )
    client = ShoonyaClient(settings)
    mapper = InstrumentMapper(client)
    rm = RiskManager(settings, client, mapper)

    # 1. Test Day-based Position Sizing rule:
    # Tuesday: 4 lots (260 qty for NIFTY, 120 qty for BNF)
    # Mon-Fri: 2 lots (130 qty for NIFTY, 60 qty for BNF)
    now_ist = datetime.now(IST)
    cur_week = (now_ist.year, now_ist.isocalendar()[1])
    tue_dt = (now_ist - timedelta(days=(now_ist.weekday() - 1))).replace(
        hour=10, minute=30, second=0, microsecond=0
    )
    mon_dt = tue_dt - timedelta(days=1)
    tuesday_signal = ORBSignal(
        index_name="NIFTY",
        signal_type="CE",
        candle_time=tue_dt,  # Tuesday of current week
        spot_close=24580.0,
        orh=24540.0,
        orl=24495.0,
        vwap=24530.0,
        rsi=68.0,
        india_vix=15.0,
    )
    tue_lots = rm.calculate_dynamic_lots("NIFTY", 200.0, tuesday_signal.candle_time)
    assert tue_lots == 4
    mon_lots = rm.calculate_dynamic_lots("NIFTY", 200.0, mon_dt)
    assert mon_lots == 2

    pos = rm.execute_entry(tuesday_signal, option_ltp=200.0)
    assert pos is not None
    assert pos.initial_qty == 260

    # 2. Test Weekly Circuit Breaker
    # Close first trade with a loss (halts same-day re-entry on NIFTY; next day clears daily SL flag)
    rm._close_position_record_locked(pos, exit_price=170.0, exit_reason="STOP_LOSS")
    assert rm.can_take_new_trade("NIFTY") is False  # Stop-on-SL active for same day
    rm.sl_hit_today_by_index.clear()  # Simulate next trading day within same week
    assert rm.weekly_losses[cur_week] == 1
    guard1 = rm.get_weekly_guard_status(now_ist)
    assert guard1["losses"] == 1
    assert guard1["state"] == "CAUTION"
    assert rm.can_take_new_trade("NIFTY") is True

    # Take second trade and close with a loss
    rm.reset_trades_counter()
    pos2 = rm.execute_entry(tuesday_signal, option_ltp=200.0)
    assert pos2 is not None
    rm._close_position_record_locked(pos2, exit_price=170.0, exit_reason="STOP_LOSS")
    assert rm.weekly_losses[cur_week] == 2
    guard2 = rm.get_weekly_guard_status(now_ist)
    assert guard2["losses"] == 2
    assert guard2["is_halted"] is True
    assert guard2["state"] == "HALTED"

    # Now weekly circuit breaker triggers: new trade blocked
    rm.reset_trades_counter()
    assert rm.can_take_new_trade("NIFTY") is False
    assert rm.execute_entry(tuesday_signal, option_ltp=200.0) is None

    # 3. Test Daily Bias filter in ORBStrategy (CE rejected if spot <= day_open)
    strat = ORBStrategy(settings)
    orb = [
        Candle(_ist_dt(2026, 11, 16, 9, 15), 24600, 24620, 24500, 24550, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 20), 24550, 24560, 24520, 24530, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 25), 24530, 24540, 24510, 24525, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 30), 24525, 24545, 24515, 24530, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 35), 24530, 24550, 24520, 24535, is_closed=True),
        Candle(_ist_dt(2026, 11, 16, 9, 40), 24535, 24555, 24525, 24540, is_closed=True),
    ]
    strat.lock_opening_range("NIFTY", orb, session_date=date(2026, 11, 16))
    # Breakout close is 24580, which is > ORH (24555), but <= day_open (24600)
    fake_ce = Candle(_ist_dt(2026, 11, 16, 10, 5), 24530, 24590, 24520, 24580, is_closed=True)
    res = strat.evaluate_candle_close("NIFTY", fake_ce, orb + [fake_ce], india_vix=14.0)
    assert res is None  # Blocked by daily bias filter!


def test_nifty_htf_swing_hybrid_strategy_and_spot_sl(tmp_path: Path) -> None:
    """Verify H1 Hybrid NIFTY: 15m HTF Swing Break + VIX >= 12 + ST(10,3) Confirm & Pure Trail + Spot ATR SL."""
    from engine.indicators import build_htf_15m_series

    settings = _make_settings(
        tmp_path,
        mode="PAPER",
        paper_slippage_pts=0.5,
        nifty_use_htf_swing_hybrid=True,
        nifty_htf_swing_len=2,
        nifty_htf_break_buf_atr=0.1,
        nifty_htf_stop_atr_mult=0.5,
        nifty_htf_opt_sl_cap=35.0,
        nifty_htf_max_trades_per_day=2,
        use_supertrend_filter=True,
    )
    strat = ORBStrategy(settings)
    client = ShoonyaClient(settings)
    mapper = InstrumentMapper(client)
    rm = RiskManager(settings, client, mapper)

    # Build 16 completed 15m bars (48 x 5m bars across prior day + current morning) with a clear 15m pivot high at bar 12
    bars: List[Candle] = []
    # Day 1: 09:15 -> 14:45 (66 bars = 22 x 15m bars) to warm ATR(14) & form a 15m swing high around 24540
    t0 = _ist_dt(2026, 11, 13, 9, 15)
    for k in range(66):
        ts = t0 + timedelta(minutes=5 * k)
        # Mild wave with peak 24540 in 15m bucket 18 (k=54..56), then settling near 24480
        if 54 <= k <= 56:
            c = Candle(ts, 24510, 24540, 24505, 24525, is_closed=True)
        elif k > 56:
            c = Candle(ts, 24495, 24505, 24470, 24485, is_closed=True)
        else:
            px = 24480 + (k % 6) * 4
            c = Candle(ts, px, px + 10, px - 10, px + 2, is_closed=True)
        bars.append(c)

    # Day 2: 09:15..09:40 bullish expansion breaking above 24540 on 09:40 bar
    d2 = _ist_dt(2026, 11, 16, 9, 15)
    day2_seq = [
        Candle(d2 + timedelta(minutes=0), 24490, 24515, 24485, 24510, is_closed=True),
        Candle(d2 + timedelta(minutes=5), 24510, 24525, 24505, 24520, is_closed=True),
        Candle(d2 + timedelta(minutes=10), 24520, 24535, 24515, 24530, is_closed=True),
        Candle(d2 + timedelta(minutes=15), 24530, 24542, 24522, 24538, is_closed=True),
        Candle(d2 + timedelta(minutes=20), 24538, 24595, 24535, 24590, is_closed=True),  # 09:35 bar closes well above 24540 + 0.1*ATR
    ]
    all_bars = bars + day2_seq
    htf_series = build_htf_15m_series(all_bars, swing_len=2, atr_period=14)
    assert len(htf_series) == len(all_bars)
    htf_c, _htf_f, htf_a, _htf_b = htf_series[-1]
    assert htf_c == 24540.0
    assert htf_a is not None and htf_a > 0

    # 09:35 bar is blocked by default 10:00 Option 2 entry_start_time
    assert strat.evaluate_candle_close("NIFTY", day2_seq[-1], all_bars, india_vix=14.0, current_time_ist=_ist_dt(2026, 11, 16, 9, 40)) is None

    # After 10:00 IST: VIX < 12 blocks signal, VIX >= 12 fires HTF_SWING_HYBRID CE
    break_1005 = Candle(_ist_dt(2026, 11, 16, 10, 5), 24538, 24595, 24535, 24590, is_closed=True)
    all_bars_1005 = bars + day2_seq[:-1] + [break_1005]
    assert strat.evaluate_candle_close("NIFTY", break_1005, all_bars_1005, india_vix=11.4, current_time_ist=_ist_dt(2026, 11, 16, 10, 10)) is None

    sig = strat.evaluate_candle_close("NIFTY", break_1005, all_bars_1005, india_vix=14.0, current_time_ist=_ist_dt(2026, 11, 16, 10, 10))
    assert sig is not None
    assert sig.strategy_mode == "HTF_SWING_HYBRID"
    assert sig.signal_type == "CE"
    assert sig.opt_sl_cap_pts == 35.0
    assert sig.pure_supertrend_trail is True
    assert sig.spot_sl_price is not None and sig.spot_sl_price < 24540.0

    # Execute into RiskManager -> uses 35pt SL cap & skips early BE/T1 cut
    pos = rm.execute_entry(sig, option_ltp=150.0, lots=2)
    assert pos is not None
    assert pos.strategy_mode == "HTF_SWING_HYBRID"
    assert pos.pure_supertrend_trail is True
    assert pos.sl_trigger_price == round(pos.entry_avg_price - 35.0, 1)

    opt_key = f"{pos.contract.exchange}|{pos.contract.token}"
    # Even at +45 pt gain (above T1=40), pure_supertrend_trail keeps full 130 qty without partial cut or BE lock
    client.update_ltp(opt_key, pos.entry_avg_price + 45.0)
    rm.on_option_tick("NIFTY", pos.entry_avg_price + 45.0)
    assert pos.state == PositionState.OPEN_FULL
    assert pos.remaining_qty == 130

    # Spot ATR stop exits on spot breach
    exited = rm.check_spot_atr_stop_exit("NIFTY", spot_low=pos.spot_sl_price - 2.0, spot_high=24595.0)
    assert exited is True
    assert "NIFTY" not in rm.positions
    assert rm.completed_trades[-1].exit_reason == "SPOT_ATR_SL"

    # Second trade allowed same day (max 2/day), third trade blocked
    rm.reset_daily()
    pos_a = rm.execute_entry(sig, option_ltp=150.0, lots=2)
    assert pos_a is not None
    rm._close_position_record_locked(pos_a, exit_price=115.0, exit_reason="STOP_LOSS")
    pos_b = rm.execute_entry(sig, option_ltp=150.0, lots=2)
    assert pos_b is not None  # Trade #2 allowed even after Trade #1 hit SL
    rm._close_position_record_locked(pos_b, exit_price=190.0, exit_reason="SUPERTREND_TRAIL")
    assert rm.execute_entry(sig, option_ltp=150.0, lots=2) is None  # Trade #3 blocked by max 2/day cap


def test_scheduler_unattended_retry_and_reset_resilience(tmp_path: Path) -> None:
    """Verify unattended scheduler resets after 08:40, retries failed auth/cache/ws, and backfills 7D."""
    settings = _make_settings(
        tmp_path,
        mode="LIVE",
        shoonya_user_id="USR1",
        shoonya_password="pwd",
        shoonya_totp_secret=pyotp.random_base32(),
        shoonya_secret_key="sec",
    )
    orch = TradingSystemOrchestrator(settings=settings)
    mon_day = date(2026, 11, 23)
    fri_day = date(2026, 11, 20)
    orch._milestone_done["auth"] = fri_day

    # 1. At 08:45 IST (after 08:40 auth_time): daily reset must still clear stale milestones and reset for today;
    #    failed login must NOT mark auth DONE and must retry after cooldown.
    orch.run_daily_login = MagicMock(return_value=None)  # type: ignore[method-assign]
    orch.step_scheduler(now_ist=_ist_dt(2026, 11, 23, 8, 45, 0))
    assert orch._milestone_done.get("reset") == mon_day
    assert "auth" not in orch._milestone_done  # failed login did not mark auth DONE
    assert orch.run_daily_login.call_count == 1

    # Retry succeeds once cooldown expires
    orch._last_auth_attempt = 0.0
    orch.run_daily_login = MagicMock(return_value="TOK_MON")  # type: ignore[method-assign]
    orch.step_scheduler(now_ist=_ist_dt(2026, 11, 23, 8, 48, 0))
    assert orch._milestone_done.get("auth") == mon_day

    # 2. At 08:56 IST: cache returning 0 contracts does NOT mark cache DONE; retry > 0 marks DONE
    orch.run_instrument_cache = MagicMock(return_value=0)  # type: ignore[method-assign]
    orch.step_scheduler(now_ist=_ist_dt(2026, 11, 23, 8, 56, 0))
    assert "cache" not in orch._milestone_done
    orch._last_cache_attempt = 0.0
    orch.run_instrument_cache = MagicMock(return_value=1500)  # type: ignore[method-assign]
    orch.step_scheduler(now_ist=_ist_dt(2026, 11, 23, 8, 58, 0))
    assert orch._milestone_done.get("cache") == mon_day

    # 3. At 09:15 IST: WS pending handshake does NOT mark ws DONE; once connected it marks DONE
    orch.run_ws_connect_and_backfill = MagicMock()  # type: ignore[method-assign]
    orch.client.ws_connected = False
    orch.step_scheduler(now_ist=_ist_dt(2026, 11, 23, 9, 15, 0))
    assert "ws" not in orch._milestone_done
    orch.client.ws_connected = True
    orch.step_scheduler(now_ist=_ist_dt(2026, 11, 23, 9, 15, 5))
    assert orch._milestone_done.get("ws") == mon_day
    assert orch.run_ws_connect_and_backfill.call_count == 1

    # 4. Verify _backfill_all_indices uses lookback_days=7 and warms HTF/indicators
    for b in orch.candle_builders.values():
        b.backfill_from_api = MagicMock(return_value=0)  # type: ignore[method-assign]
    orch._backfill_all_indices()
    for b in orch.candle_builders.values():
        assert b.backfill_from_api.call_args.kwargs["lookback_days"] == 7


def test_telegram_startup_and_test_endpoint(tmp_path: Path) -> None:
    """Verify /api/telegram/test and 10:00 scanner_alert single-fire notification."""
    settings = _make_settings(tmp_path, mode="PAPER")
    orch = TradingSystemOrchestrator(settings=settings)
    app = create_app(orch)
    handlers = app.state.handlers

    # 1. Disabled by default without bot token / chat id
    ok, msg = orch.send_test_telegram()
    assert ok is False and "not configured" in msg.lower()
    resp = handlers["api_telegram_test"]()
    data = json.loads(resp.body.decode("utf-8"))
    assert resp.status_code == 200 and data["ok"] is False and data["enabled"] is False

    # 2. Enabled with mocked send_sync -> succeeds
    orch.notifier.bot_token = "123456:TESTTOKEN"
    orch.notifier.chat_id = "987654321"
    orch.notifier.send_sync = MagicMock(return_value=True)  # type: ignore[method-assign]
    resp2 = handlers["api_telegram_test"]()
    data2 = json.loads(resp2.body.decode("utf-8"))
    assert data2["ok"] is True and data2["enabled"] is True
    assert orch.notifier.send_sync.call_count == 1

    # 3. scanner_alert fires once at 10:00 IST and does not duplicate
    sent: List[str] = []
    orch.notifier.send_async = MagicMock(side_effect=lambda m, prefix="": sent.append(m))  # type: ignore[method-assign]
    orch._mark_milestone("reset", date(2026, 11, 23))
    orch._mark_milestone("auth", date(2026, 11, 23))
    orch._mark_milestone("cache", date(2026, 11, 23))
    orch._mark_milestone("ws", date(2026, 11, 23))
    orch._mark_milestone("orb_lock", date(2026, 11, 23))
    orch.step_scheduler(now_ist=_ist_dt(2026, 11, 23, 10, 0, 0))
    orch.step_scheduler(now_ist=_ist_dt(2026, 11, 23, 10, 1, 0))
    scanner_msgs = [m for m in sent if "Scanner LIVE" in m]
    assert len(scanner_msgs) == 1


def test_positionbook_404_no_reauth_and_reconcile_skip(tmp_path: Path) -> None:
    """PositionBook 404 returns [] without triggering reauth; Bearer 401 tries jKey fallback first."""
    settings = _make_settings(tmp_path, mode="LIVE", shoonya_user_id="USR404")
    client = ShoonyaClient(settings)
    reauth_cb = MagicMock(return_value=True)
    client.register_reauth_callback(reauth_cb)

    # 1. HTTP 404 JSON "no data" on PositionBook -> returns [] and no reauth
    resp_404_json = MagicMock(status_code=404)
    resp_404_json.json.return_value = {"stat": "Not_Ok", "emsg": "no data"}
    client.api._session.post = MagicMock(return_value=resp_404_json)  # type: ignore[method-assign]
    assert client.get_positions() == []
    assert reauth_cb.call_count == 0

    # 2. HTTP 404 non-JSON empty body on PositionBook -> returns [] without raise_for_status/reauth
    resp_404_raw = MagicMock(status_code=404)
    resp_404_raw.json.side_effect = ValueError("No JSON")
    resp_404_raw.raise_for_status.side_effect = RuntimeError("Should not raise 404")
    client.api._session.post = MagicMock(return_value=resp_404_raw)  # type: ignore[method-assign]
    assert client.get_positions() == []
    assert reauth_cb.call_count == 0

    # 2b. HTTP 401 with JSON "no data" on OrderBook/PositionBook -> returns [] without reauth
    resp_401_nodata = MagicMock(status_code=401)
    resp_401_nodata.json.return_value = {"stat": "Not_Ok", "emsg": 'Error Occurred : 5 "no data"'}
    client.api._session.post = MagicMock(return_value=resp_401_nodata)  # type: ignore[method-assign]
    assert client.api.get_order_book() == []
    assert client.api.get_positions() == []
    assert reauth_cb.call_count == 0

    # 3. UserDetails 401 must NOT trigger reauth or strip Bearer header
    client.api.injectOAuthHeader("OAUTH_TOK", "USR404", "USR404", susertoken="QUICK_WS_TOK", use_bearer=True)
    resp_401_ud = MagicMock(status_code=401)
    resp_401_ud.json.side_effect = ValueError("No JSON")
    resp_401_ud.raise_for_status.side_effect = RuntimeError("401 on UserDetails")
    client.api._session.post = MagicMock(return_value=resp_401_ud)  # type: ignore[method-assign]
    try:
        client.api.get_user_details()
    except RuntimeError:
        pass
    assert reauth_cb.call_count == 0
    assert client.api._use_bearer is True
    assert client.api._session.headers.get("Authorization") == "Bearer OAUTH_TOK"

    # 4. Bearer 401 on Limits falls back cleanly to jKey before re-auth & preserves _quick_susertoken for WS
    client.api._quick_susertoken = "QUICK_WS_TOK"
    client.api.injectOAuthHeader("OAUTH_TOK", "USR404", "USR404", susertoken="QUICK_WS_TOK", use_bearer=True)
    assert client.api._quick_susertoken == "QUICK_WS_TOK"
    resp_401 = MagicMock(status_code=401)
    resp_ok = MagicMock(status_code=200)
    resp_ok.json.return_value = {"stat": "Ok", "cash": "50000"}
    client.api._session.post = MagicMock(side_effect=[resp_401, resp_ok])  # type: ignore[method-assign]
    lim = client.api.get_limits()
    assert lim["stat"] == "Ok"
    assert client.api._use_bearer is False
    assert client.api._quick_susertoken == "QUICK_WS_TOK"
    assert reauth_cb.call_count == 0

    # 5. RiskManager.reconcile_positions skips PositionBook API call when no internal positions exist
    rm = RiskManager(settings, client, InstrumentMapper(client))
    client.get_positions = MagicMock()  # type: ignore[method-assign]
    assert rm.reconcile_positions() == []
    assert client.get_positions.call_count == 0

    # 6. NSE holiday check (Nov 24, 2026 Guru Nanak Jayanti) & WS watchdog grace period
    assert is_nse_trading_day(date(2026, 11, 24)) is False
    assert is_nse_trading_day(date(2026, 11, 25)) is True
    client.subscribe(["NSE|26000"])
    client.ws_connected = True
    client.last_tick_monotonic = 0.0
    assert client.check_ws_watchdog(now_ist=_ist_dt(2026, 11, 24, 9, 15, 0)) is False
    assert client.check_ws_watchdog(now_ist=_ist_dt(2026, 11, 25, 9, 15, 0)) is False


def test_runtime_state_persistence_across_restart(tmp_path: Path) -> None:
    """Mode, capital, lots, enabled indices, loss limits, timings & token persist across restarts."""
    s1 = _make_settings(tmp_path, mode="LIVE", capital=100000.0, shoonya_user_id="USR_P")
    orch1 = TradingSystemOrchestrator(settings=s1)
    assert orch1.toggle_mode()[0] is True  # LIVE -> PAPER
    assert orch1.add_or_update_funds(250000.0, action="set")[0] is True
    assert orch1.set_index_lots("NIFTY", 4)[0] is True
    assert orch1.set_index_product_type("BANKNIFTY", "I")[0] is True
    assert orch1.set_max_daily_loss(7500.0)[0] is True
    assert orch1.set_enabled_indices(nifty=True, banknifty=False)[0] is True
    assert orch1.set_orb_anchor("09:15", 15)[0] is True
    assert orch1.set_entry_cutoff_time("14:30")[0] is True
    orch1.auth.save_token("PERSIST_TOK_99", use_bearer=False)

    state_file = Path(s1.data_dir) / "runtime_state.json"
    assert state_file.exists()
    # Delete local disk token file to verify external runtime_state token restoration works too
    token_file = Path(s1.token_file_path)
    if token_file.exists():
        token_file.unlink()

    s2 = _make_settings(tmp_path, mode="LIVE", capital=100000.0, shoonya_user_id="USR_P")
    orch2 = TradingSystemOrchestrator(settings=s2)
    assert orch2.settings.mode == "PAPER"
    assert orch2.settings.capital == 250000.0
    assert orch2.custom_lots["NIFTY"] == 4
    assert orch2.custom_product_types["BANKNIFTY"] == "I"
    assert orch2.risk_manager.custom_max_daily_loss == 7500.0
    assert orch2.risk_manager.get_enabled_indices() == {"NIFTY": True, "BANKNIFTY": False}
    assert orch2.settings.orb_window_minutes == 15
    assert orch2.settings.entry_cutoff_time == time(14, 30, 0)

    loaded_state = orch2.state_store.load()
    assert loaded_state["shoonya_token"]["access_token"] == "PERSIST_TOK_99"
    orch2.auth.api.get_limits = MagicMock(return_value={"stat": "Ok"})  # type: ignore[method-assign]
    assert orch2.auth.load_cached_token(external_payload=loaded_state["shoonya_token"]) == "PERSIST_TOK_99"


def test_one_minute_signal_check_and_no_double_entry(tmp_path: Path) -> None:
    """1-minute signal check evaluates live forming candle and avoids double entry on 5m close."""
    settings = _make_settings(
        tmp_path,
        mode="PAPER",
        orb_window_minutes=15,
        orb_end_time=time(9, 30, 0),
        daily_bias_filter=False,
        use_ema_filter=False,
        min_candle_body_ratio=0.0,
        use_supertrend_filter=False,
        enable_nifty_trading=False,
        enable_banknifty_trading=True,
    )
    orch = TradingSystemOrchestrator(settings=settings)
    orch.client.ws_connected = True
    orch.client.last_tick_monotonic = 9999999999.0
    orch.client.update_ltp("NSE|26017", 15.0)

    # Seed 16 prior closed candles (5m + warmup from previous day) + today's rising candles -> Wilder RSI(14) > 60 & 15m ORB 51400..51550
    bnf_builder = orch.candle_builders["BANKNIFTY"]
    base_candles: List[Candle] = []
    prev_day = _ist_dt(2026, 11, 23, 14, 30, 0)
    for k in range(10):
        c_ts = prev_day + timedelta(minutes=k * 5)
        px = 51200.0 + (k * 10.0)
        base_candles.append(
            Candle(timestamp=c_ts, open=px - 4.0, high=px + 8.0, low=px - 8.0, close=px + 4.0, volume=1000, is_closed=True)
        )
    start_dt = _ist_dt(2026, 11, 25, 9, 15, 0)
    for i in range(10):
        c_ts = start_dt + timedelta(minutes=i * 5)
        px = 51400.0 + (i * 15.0)
        hi = 51550.0 if i < 3 else (px + 10.0)
        lo = 51400.0 if i < 3 else (px - 10.0)
        base_candles.append(
            Candle(timestamp=c_ts, open=px - 5.0, high=hi, low=lo, close=px + 5.0, volume=1000, is_closed=True)
        )
    bnf_builder.seed_candles(base_candles)
    orch.strategy.lock_opening_range("BANKNIFTY", bnf_builder.get_candles(), session_date=date(2026, 11, 25))
    assert orch.strategy.get_state("BANKNIFTY").is_locked is True

    # Inject forming 1m breakout tick at 10:06:00 above ORH=51550 & VWAP
    bnf_builder.on_tick(51680.0, tick_time=_ist_dt(2026, 11, 25, 10, 6, 0), volume=1500)
    orch.client.update_ltp("NSE|26009", 51680.0)

    res = orch.run_minute_signal_check(now_ist=_ist_dt(2026, 11, 25, 10, 6, 0))
    assert "BANKNIFTY" in res["triggered"]
    assert orch.last_signal_check_time == "10:06:00"
    assert orch.risk_manager.trades_taken_today == 1

    # Second 1m check inside same 10:05 bucket & subsequent 10:10 close must NOT double-enter
    res2 = orch.run_minute_signal_check(now_ist=_ist_dt(2026, 11, 25, 10, 7, 0))
    assert res2["triggered"] == []
    bnf_builder.flush_due_candles(now_ist=_ist_dt(2026, 11, 25, 10, 10, 0))
    assert orch.risk_manager.trades_taken_today == 1

    snap = orch.get_status_snapshot()
    assert snap["schedule"]["signal_check_interval_sec"] == 60
    assert snap["schedule"]["last_signal_check_time"] == "10:07:00"


def test_paper_position_and_completed_trades_persist_across_restart(tmp_path: Path) -> None:
    """Active PAPER position, resting SL-LMT order, and completed trades survive restarts."""
    s1 = _make_settings(tmp_path, mode="PAPER", shoonya_user_id="USR_PAPER")
    orch1 = TradingSystemOrchestrator(settings=s1)
    orch1.client.update_ltp("NSE|26000", 25300.0)
    ok, msg, _ = orch1.trigger_auto_order("NIFTY", signal_type="CE")
    assert ok is True, msg
    assert "NIFTY" in orch1.risk_manager.positions

    pos1 = orch1.risk_manager.positions["NIFTY"]
    custom_sl = round(max(1.0, pos1.entry_avg_price - 15.0), 1)
    ok_sl, msg_sl = orch1.modify_position_sl("NIFTY", custom_sl)
    assert ok_sl is True, msg_sl

    # Restart orchestrator (defaulting to LIVE) sharing the same data_dir
    s2 = _make_settings(tmp_path, mode="LIVE", shoonya_user_id="USR_PAPER")
    orch2 = TradingSystemOrchestrator(settings=s2)
    assert orch2.settings.mode == "PAPER"
    assert "NIFTY" in orch2.risk_manager.positions
    pos2 = orch2.risk_manager.positions["NIFTY"]
    assert pos2.trade_id == pos1.trade_id
    assert pos2.contract.tradingsymbol == pos1.contract.tradingsymbol
    assert pos2.entry_avg_price == pos1.entry_avg_price
    assert pos2.sl_trigger_price == custom_sl
    assert pos2.sl_order_id == pos1.sl_order_id
    assert pos2.sl_order_id in orch2.client._paper_orders
    opt_key = f"{pos2.contract.exchange}|{pos2.contract.token}"
    assert opt_key in orch2.client.subscribed_tokens

    # Simulate option drop hitting SL after restart -> SL fills & closes cleanly
    orch2.client.update_ltp(opt_key, max(0.1, custom_sl - 2.0))
    orch2.risk_manager.on_option_tick("NIFTY", max(0.1, custom_sl - 2.0))
    assert "NIFTY" not in orch2.risk_manager.positions
    assert len(orch2.risk_manager.completed_trades) == 1
    assert orch2.risk_manager.completed_trades[0].trade_id == pos1.trade_id

    # Restart a third time -> completed trade stays intact and scheduler reset does not wipe it
    s3 = _make_settings(tmp_path, mode="PAPER", shoonya_user_id="USR_PAPER")
    orch3 = TradingSystemOrchestrator(settings=s3)
    assert len(orch3.risk_manager.completed_trades) == 1
    today_dt = IST.localize(datetime.combine(datetime.now(IST).date(), time(10, 15, 0)))
    orch3.step_scheduler(now_ist=today_dt)
    assert len(orch3.risk_manager.completed_trades) == 1


def test_cold_start_remote_load_failure_does_not_clobber_state(tmp_path: Path) -> None:
    """Failed Neon cold-start load must block background overwrites until explicit/synced save."""
    from unittest.mock import patch
    from services.runtime_state_store import RuntimeStateStore

    store = RuntimeStateStore(tmp_path / "data", enable_remote_db=False)
    store._write_local(json.dumps({"mode": "PAPER", "capital": 500000.0}))

    store._remote_enabled = True
    store._loop = MagicMock()
    store._retry_delay_sec = 0.0
    store._load_remote_async = MagicMock(return_value="CORO")  # type: ignore[method-assign]
    store._save_remote_async = MagicMock(return_value="CORO")  # type: ignore[method-assign]

    failing_fut = MagicMock()
    failing_fut.result.side_effect = TimeoutError()
    ok_fut = MagicMock()
    ok_fut.result.return_value = None

    with patch("services.runtime_state_store.asyncio.run_coroutine_threadsafe", return_value=failing_fut) as mock_rcts:
        loaded = store.load()
        assert loaded["mode"] == "PAPER"
        assert store.remote_load_ok is False
        assert store._remote_writes_blocked is True
        assert mock_rcts.call_count == 2  # Retried twice before falling back

    with patch("services.runtime_state_store.asyncio.run_coroutine_threadsafe", return_value=ok_fut) as mock_rcts_save:
        # Automatic non-forced saves must NOT overwrite remote DB while blocked
        store.save({"mode": "LIVE", "capital": 100000.0}, force_remote=False)
        assert store.save_sync({"mode": "LIVE", "capital": 100000.0}, force_remote=False) is False
        assert mock_rcts_save.call_count == 0

        # Explicit user action (force_remote=True) unblocks & commits remotely
        assert store.save_sync({"mode": "PAPER", "capital": 600000.0}, force_remote=True) is True
        assert mock_rcts_save.call_count == 1
        assert store._remote_writes_blocked is False


def test_0840_auth_milestone_sends_telegram_on_cached_token(tmp_path: Path) -> None:
    """08:40 IST scheduler emits Telegram alert even when reusing a cached daily token."""
    from unittest.mock import patch

    sent_msgs: List[str] = []
    settings = _make_settings(
        tmp_path,
        mode="LIVE",
        shoonya_user_id="FA_TEST",
        telegram_bot_token="123456:TESTTOKEN",
        telegram_chat_id="999888777",
    )
    # Prevent Telegram worker from making real HTTP calls
    with patch("utils.notifier.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        orch = TradingSystemOrchestrator(settings=settings)
        orch.notifier.send_async = lambda m, prefix="": sent_msgs.append(f"{prefix}{m}")  # type: ignore[method-assign]

        trading_day = date(2026, 11, 26)
        assert is_nse_trading_day(trading_day) is True
        orch.auth.last_token_date = trading_day.strftime("%Y-%m-%d")
        orch.auth.last_login_status = "CACHED_TOKEN_ACTIVE"
        orch.auth.load_cached_token = MagicMock(return_value="CACHED_TOK_OK")  # type: ignore[method-assign]
        orch.client.get_live_broker_snapshot = MagicMock(return_value={"connected": True})  # type: ignore[method-assign]
        orch._milestone_done["reset"] = trading_day

        orch.step_scheduler(now_ist=_ist_dt(2026, 11, 26, 8, 40, 5))
        assert orch._milestone_done.get("auth") == trading_day
        assert any("08:40 IST Auto OAuth Ready" in m for m in sent_msgs)
        assert orch.get_status_snapshot()["telegram_enabled"] is True


def test_live_broker_data_archived_to_db_for_analysis(tmp_path: Path) -> None:
    """LIVE mode captures limits, orders, trades, positions, and holdings into DB without duplicates."""
    settings = _make_settings(tmp_path, mode="LIVE", shoonya_user_id="FA53975", shoonya_account_id="FA53975")
    orch = TradingSystemOrchestrator(settings=settings)
    app = create_app(orch)

    orch.client.api.access_token = "LIVE_TOK"
    orch.client.api.get_limits = MagicMock(  # type: ignore[method-assign]
        return_value={
            "stat": "Ok",
            "prfname": "SHOONYA",
            "cash": "125000.00",
            "payin": "0.00",
            "payout": "0.00",
            "brkcollamt": "25000.00",
            "marginused": "30000.00",
            "request_time": "10:30:00 26-11-2026",
        }
    )
    orch.client.api.get_user_details = MagicMock(  # type: ignore[method-assign]
        return_value={"stat": "Ok", "uid": "FA53975", "actid": "FA53975", "uname": "BALAJI TEST"}
    )
    orch.client.api.get_order_book = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "norenordno": "ORD1001",
                "exchordid": "EX9001",
                "norentm": "10:15:02",
                "exch": "NFO",
                "tsym": "NIFTY01DEC26C25750",
                "trantype": "B",
                "s_prdt_ali": "NRML",
                "prctyp": "LMT",
                "qty": "65",
                "fillshares": "0",
                "prc": "210.50",
                "trgprc": "0",
                "status": "OPEN",
                "ordersource": "API",
                "remarks": "ALGO_TEST_2026",
            }
        ]
    )
    orch.client.api.get_trade_book = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "flid": "FL5001",
                "norenordno": "ORD1001",
                "exchordid": "EX9001",
                "fltm": "10:15:03",
                "exch": "NFO",
                "tsym": "NIFTY01DEC26C25750",
                "trantype": "B",
                "s_prdt_ali": "NRML",
                "flqty": "65",
                "flprc": "210.00",
                "ordersource": "API",
                "remarks": "ALGO_TEST_2026",
            }
        ]
    )
    orch.client.api.get_positions = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "exch": "NFO",
                "tsym": "NIFTY01DEC26C25750",
                "s_prdt_ali": "NRML",
                "netqty": "65",
                "daybuyqty": "65",
                "daysellqty": "0",
                "netavgprc": "210.00",
                "daybuyavgprc": "210.00",
                "daysellavgprc": "0.00",
                "lp": "225.00",
                "rpnl": "0.00",
                "urmtom": "975.00",
            }
        ]
    )
    orch.client.api.get_holdings = MagicMock(  # type: ignore[method-assign]
        return_value=[{"exch_tsym": [{"exch": "NSE", "tsym": "INFY-EQ"}], "holdqty": "10", "upldprc": "1500.00"}]
    )

    res1 = orch.sync_live_broker_archive(trigger_event="TEST_SYNC_1", force_refresh=True, sync=True)
    assert res1["stored"] is True
    assert res1["orders"] == 1
    assert res1["trades"] == 1
    assert res1["positions"] == 1
    assert res1["holdings"] == 1

    # Second sync with updated order status -> upserts without creating duplicate order row
    orch.client.api.get_order_book.return_value[0]["status"] = "COMPLETE"
    orch.client.api.get_order_book.return_value[0]["fillshares"] = "65"
    res2 = orch.sync_live_broker_archive(trigger_event="TEST_SYNC_2", force_refresh=True, sync=True)
    assert res2["stored"] is True

    archive = orch.broker_archive.query_archive()
    assert archive["ok"] is True
    assert archive["summary"]["total_snapshots"] == 2
    assert archive["summary"]["total_orders"] == 1
    assert archive["summary"]["total_trades"] == 1
    assert archive["summary"]["total_positions"] == 1
    assert archive["summary"]["total_unrealized_pnl"] == 975.0
    assert archive["orders"][0]["status"] == "COMPLETE"
    assert archive["orders"][0]["fillshares"] == 65
    assert "NIFTY01DEC26C25750" in archive["summary"]["distinct_symbols"]
    assert "125000.00" in archive["snapshots"][0]["raw_limits_json"]

    # Query via HTTP route handler
    api_archive_resp = app.state.handlers["api_broker_archive"]()
    assert api_archive_resp.status_code == 200
    api_data = json.loads(api_archive_resp.body.decode("utf-8"))
    assert api_data["summary"]["total_orders"] == 1

    # Verify PAPER mode does NOT trigger LIVE broker archival
    assert orch.toggle_mode(force_clear_paper=True)[0] is True
    res_paper = orch.sync_live_broker_archive(trigger_event="SHOULD_SKIP", force_refresh=False, sync=True)
    assert res_paper["stored"] is False
    assert res_paper["reason"] == "not_live_mode"







def test_strategy_gates_and_timings_update_and_persist(tmp_path: Path) -> None:
    import json as _json
    from ui.dashboard import StrategyConfigRequest

    s1 = _make_settings(tmp_path, use_supertrend_filter=True)
    orch1 = TradingSystemOrchestrator(settings=s1)
    app = create_app(orch1)
    handlers = app.state.handlers

    res_get = handlers["api_get_strategy_config"]()
    cfg0 = _json.loads(res_get.body.decode("utf-8"))["strategy_config"]
    assert cfg0["orb_window_minutes"] == 30
    assert cfg0["rsi_bull_threshold"] == 60.0
    assert cfg0["use_supertrend_filter"] is True

    # Invalid orb_window_minutes must still be rejected cleanly
    bad_res = handlers["api_set_strategy_config"](StrategyConfigRequest(orb_window_minutes=20))
    bad_body = _json.loads(bad_res.body.decode("utf-8"))
    assert bad_body["ok"] is False

    # Update full set of strategy gates and timings
    req = StrategyConfigRequest(
        orb_start_time="09:15",
        orb_window_minutes=15,
        entry_start_time="09:30",
        entry_cutoff_time="15:00",
        nifty_htf_entry_start_time="09:30",
        auto_squareoff_time="15:20",
        min_india_vix=11.0,
        min_orb_range_pct=0.15,
        max_orb_range_pct=1.10,
        rsi_bull_threshold=55.0,
        rsi_bear_threshold=45.0,
        daily_bias_filter=True,
        use_ema_filter=False,
        min_candle_body_ratio=0.30,
        require_vwap_slope=True,
        min_vwap_slope_pct=0.02,
        use_supertrend_filter=False,
        supertrend_period=7,
        supertrend_multiplier=2.5,
        nifty_use_htf_swing_hybrid=True,
        nifty_use_ema_body_filter=True,
        nifty_htf_swing_len=3,
        nifty_htf_break_buf_atr=0.15,
        nifty_htf_stop_atr_mult=0.6,
        nifty_htf_opt_sl_cap=40.0,
        nifty_htf_max_trades_per_day=3,
        max_trades_per_day=6,
        max_weekly_losses=3,
        tuesday_expiry_boost=False,
        banknifty_be_pyramiding=False,
    )
    res_set = handlers["api_set_strategy_config"](req)
    body = _json.loads(res_set.body.decode("utf-8"))
    assert body["ok"] is True
    cfg1 = body["strategy_config"]
    assert cfg1["orb_window_minutes"] == 15
    assert cfg1["entry_start_time"] == "09:30"
    assert cfg1["entry_cutoff_time"] == "15:00"
    assert cfg1["rsi_bull_threshold"] == 55.0
    assert cfg1["rsi_bear_threshold"] == 45.0
    assert cfg1["use_supertrend_filter"] is False
    assert cfg1["supertrend_period"] == 7
    assert cfg1["supertrend_multiplier"] == 2.5
    assert cfg1["max_trades_per_day"] == 6
    assert body["snapshot"]["strategy_config"]["entry_cutoff_time"] == "15:00"

    # Restart orchestrator on same data dir and verify strategy gates & timings persisted
    s2 = _make_settings(tmp_path, use_supertrend_filter=True)
    orch2 = TradingSystemOrchestrator(settings=s2)
    restored = orch2.get_strategy_config()
    assert restored["orb_window_minutes"] == 15
    assert restored["entry_start_time"] == "09:30"
    assert restored["entry_cutoff_time"] == "15:00"
    assert restored["rsi_bull_threshold"] == 55.0
    assert restored["use_supertrend_filter"] is False
    assert restored["supertrend_period"] == 7
    assert restored["max_trades_per_day"] == 6
    assert restored["tuesday_expiry_boost"] is False


def test_backtest_0930_to_1500_runner_and_api(tmp_path: Path, monkeypatch: Any) -> None:
    import json as _json
    from engine.candle_builder import Candle
    from scripts.update_backtest_data import run_nf_bnf_sequential_backtest
    from ui.dashboard import BacktestRunRequest

    # Build deterministic 2-day 5m synthetic candles (Day 1 warmup + Day 2 breakout in 09:30..15:00)
    nf_candles: list[Candle] = []
    bnf_candles: list[Candle] = []
    d1 = date(2026, 6, 15)
    d2 = date(2026, 6, 16)
    for day_idx, d in enumerate((d1, d2)):
        t_cur = _ist_dt(d.year, d.month, d.day, 9, 15)
        t_end = _ist_dt(d.year, d.month, d.day, 15, 25)
        bar_n = 0
        while t_cur <= t_end:
            nf_px = 25000.0 + bar_n * (0.5 if day_idx == 0 else (3.5 if bar_n >= 3 else 0.5))
            bnf_px = 60000.0 + bar_n * (1.5 if day_idx == 0 else (18.0 if bar_n >= 3 else 2.0))
            nf_candles.append(
                Candle(
                    timestamp=t_cur,
                    open=nf_px - 4.0,
                    high=nf_px + 25.0,
                    low=nf_px - 25.0,
                    close=nf_px + (12.0 if (day_idx == 1 and bar_n >= 3) else 0.0),
                    volume=100000.0,
                    is_closed=True,
                )
            )
            bnf_candles.append(
                Candle(
                    timestamp=t_cur,
                    open=bnf_px - 10.0,
                    high=bnf_px + 80.0,
                    low=bnf_px - 80.0,
                    close=bnf_px + (35.0 if (day_idx == 1 and bar_n >= 3) else 0.0),
                    volume=100000.0,
                    is_closed=True,
                )
            )
            t_cur += timedelta(minutes=5)
            bar_n += 1

    # Engine defaults to 09:30 - 15:00 IST with 15m ORB
    trades = run_nf_bnf_sequential_backtest(
        {"NIFTY": nf_candles, "BANKNIFTY": bnf_candles},
        entry_start_time="09:30",
        entry_cutoff_time="15:00",
        orb_window_minutes=15,
        min_orb_range_pct=0.05,
        max_orb_range_pct=2.0,
        rsi_bull_threshold=45.0,
        use_supertrend_filter=False,
        use_ema_filter=False,
        min_candle_body_ratio=0.0,
    )
    assert isinstance(trades, list)
    for t in trades:
        assert "09:30" <= t["in_t"] <= "15:00"

    # API runner + trades endpoint + synced mobile UI checks
    orch = TradingSystemOrchestrator(settings=_make_settings(tmp_path))
    app = create_app(orch)
    handlers = app.state.handlers

    import scripts.update_backtest_data as ubd
    monkeypatch.setattr(ubd, "load_candles_from_cache", lambda idx: nf_candles if idx == "NIFTY" else bnf_candles)
    monkeypatch.setattr(ubd, "load_vix_map", lambda: {})

    run_resp = handlers["api_run_backtest"](
        BacktestRunRequest(
            entry_start_time="09:30",
            entry_cutoff_time="15:00",
            orb_window_minutes=15,
            use_live_gates=False,
            save=False,
            min_orb_range_pct=0.05,
            max_orb_range_pct=2.0,
            rsi_bull_threshold=45.0,
            use_supertrend_filter=False,
            use_ema_filter=False,
            min_candle_body_ratio=0.0,
        )
    )
    run_body = _json.loads(run_resp.body.decode("utf-8"))
    assert run_body["ok"] is True
    assert run_body["meta"]["entry_start_time"] == "09:30"
    assert run_body["meta"]["entry_cutoff_time"] == "15:00"
    assert run_body["meta"]["orb_window_minutes"] == 15

    bt_get = _json.loads(handlers["api_backtest_trades"]().body.decode("utf-8"))
    assert bt_get["ok"] is True
    assert bt_get["meta"].get("entry_start_time") == "09:30"
    assert bt_get["meta"].get("entry_cutoff_time") == "15:00"

    mob_html = (Path(__file__).resolve().parent.parent / "mobile" / "www" / "index.html").read_text(encoding="utf-8")
    assert "sheet-backtest" in mob_html
    assert "m-pill-backtest" in mob_html
    assert "ms-rsi-bull" in mob_html


def test_gann_box_5m_strategy_atm_protection_and_persist(tmp_path: Path) -> None:
    import json as _json
    from engine.candle_builder import Candle
    from ui.dashboard import StrategyConfigRequest

    s1 = _make_settings(
        tmp_path,
        strategy_profile="GANN_BOX_5M",
        gann_box_mode="BREAKOUT",
        gann_confirm_candles=1,
        gann_warmup_minutes=15,
        gann_use_htf_view_protection=True,
        use_supertrend_filter=False,
        entry_start_time=time(9, 30, 0),
        entry_cutoff_time=time(15, 0, 0),
    )
    orch1 = TradingSystemOrchestrator(settings=s1)
    app = create_app(orch1)
    handlers = app.state.handlers

    # Build Day 1 bars forming a 15m swing high near 25080 and swing low near 24920
    d1 = date(2026, 6, 15)
    d2 = date(2026, 6, 16)
    history: list[Candle] = []
    # 21 bars (5 x 15m bars -> clearly establishes a 15m pivot high > 25080 and pivot low ~24920)
    pattern = [
        25000, 25010, 25020,   # 09:15 15m: C=25020
        24990, 24950, 24930,   # 09:30 15m: low 24920
        25010, 25050, 25080,   # 09:45 15m: high 25090 (swing high)
        25040, 25010, 24990,   # 10:00 15m
        24980, 24960, 24940,   # 10:15 15m: C=24940 (< 24990, keeps bearish/neutral bias)
        24970, 24965, 24950,   # 10:30 15m
        24990, 25000, 25005,   # 10:45 15m: inside range [24920, 25090]
    ]
    t_cur = _ist_dt(d1.year, d1.month, d1.day, 9, 15)
    for px in pattern:
        history.append(
            Candle(
                timestamp=t_cur,
                open=px - 5.0,
                high=px + 10.0,
                low=px - 10.0,
                close=float(px),
                volume=100000,
                is_closed=True,
            )
        )
        t_cur += timedelta(minutes=5)

    # Day 2 1st 5-minute candle (09:15): VAH=25020, VAL=24980, MID=25000
    c_open = Candle(timestamp=_ist_dt(2026, 6, 16, 9, 15), open=24995.0, high=25020.0, low=24980.0, close=25005.0, volume=120000, is_closed=True)
    sig0 = orch1.strategy.evaluate_candle_close("NIFTY", c_open, history, india_vix=15.0)
    assert sig0 is None
    st_nf = orch1.strategy.get_state("NIFTY")
    assert st_nf.gann_vah == 25020.0
    assert st_nf.gann_val == 24980.0
    assert st_nf.gann_mid == 25000.0
    history.append(c_open)

    # Warmup (< 09:30): 09:20 and 09:25 closes above VAH are still skipped during warm-up (< 09:30)
    c_920 = Candle(timestamp=_ist_dt(2026, 6, 16, 9, 20), open=25005.0, high=25030.0, low=25000.0, close=25025.0, volume=100000, is_closed=True)
    assert orch1.strategy.evaluate_candle_close("NIFTY", c_920, history, india_vix=15.0) is None
    history.append(c_920)

    # Pull back inside box at 09:25 so run resets
    c_925 = Candle(timestamp=_ist_dt(2026, 6, 16, 9, 25), open=25015.0, high=25020.0, low=25000.0, close=25010.0, volume=100000, is_closed=True)
    assert orch1.strategy.evaluate_candle_close("NIFTY", c_925, history, india_vix=15.0) is None
    history.append(c_925)

    # 09:30 bar closes above VAH (25035) AND below HTF red (25090), so 1st close does not fire (confirmN=1)
    c_930 = Candle(timestamp=_ist_dt(2026, 6, 16, 9, 30), open=25010.0, high=25040.0, low=25010.0, close=25035.0, volume=100000, is_closed=True)
    assert orch1.strategy.evaluate_candle_close("NIFTY", c_930, history, india_vix=15.0) is None
    assert orch1.strategy.get_state("NIFTY").gann_up_run == 1
    history.append(c_930)

    # 09:35 bar closes at 25045 (> VAH 25020 -> up_run == 2, EXCEPT still < HTF red 25090) -> BLOCKED by ATM HTF View Protection!
    c_935 = Candle(timestamp=_ist_dt(2026, 6, 16, 9, 35), open=25035.0, high=25050.0, low=25030.0, close=25045.0, volume=100000, is_closed=True)
    sig_blocked = orch1.strategy.evaluate_candle_close("NIFTY", c_935, history, india_vix=15.0)
    assert sig_blocked is None
    history.append(c_935)

    # Pull back inside box at 09:40, then break cleanly above BOTH VAH (25020) and HTF Red (25090) on 2 consecutive bars (09:45 & 09:50)
    c_940 = Candle(timestamp=_ist_dt(2026, 6, 16, 9, 40), open=25020.0, high=25022.0, low=25005.0, close=25012.0, volume=100000, is_closed=True)
    orch1.strategy.evaluate_candle_close("NIFTY", c_940, history, india_vix=15.0)
    history.append(c_940)

    c_945 = Candle(timestamp=_ist_dt(2026, 6, 16, 9, 45), open=25015.0, high=25105.0, low=25015.0, close=25100.0, volume=120000, is_closed=True)
    assert orch1.strategy.evaluate_candle_close("NIFTY", c_945, history, india_vix=15.0) is None  # 1st close (confirmN=1 awaits 2nd)
    history.append(c_945)

    c_950 = Candle(timestamp=_ist_dt(2026, 6, 16, 9, 50), open=25100.0, high=25120.0, low=25095.0, close=25115.0, volume=130000, is_closed=True)
    sig_ok = orch1.strategy.evaluate_candle_close("NIFTY", c_950, history, india_vix=15.0)
    assert sig_ok is not None
    assert sig_ok.strategy_mode == "GANN_BOX_5M"
    assert sig_ok.signal_type == "CE"
    assert sig_ok.spot_sl_price is not None and sig_ok.spot_sl_price <= 25000.0

    # Verify ATM Protection Panel in snapshot
    orch1.client.update_ltp("NSE|26000", 25115.0)
    orch1.client.update_ltp("NSE|26009", 60120.0)
    snap = _json.loads(handlers["api_status"]().body.decode("utf-8"))
    assert snap["strategy_profile"] == "GANN_BOX_5M"
    ap = snap["atm_protection_panel"]
    assert ap["protection_enabled"] is True
    assert ap["indices"]["NIFTY"]["atm_strike"] == 25100
    assert ap["indices"]["NIFTY"]["htf_view"] == 1
    assert "ATM 25100 CE" in ap["indices"]["NIFTY"]["recommended_label"]
    assert ap["indices"]["BANKNIFTY"]["atm_strike"] == 60100

    # Switch profile & persist across restart
    set_res = handlers["api_set_strategy_config"](
        StrategyConfigRequest(
            strategy_profile="GANN_BOX_5M",
            gann_box_mode="BOUNCE",
            gann_confirm_candles=2,
            gann_hold_candles=3,
            gann_warmup_minutes=20,
            gann_use_htf_view_protection=False,
            gann_stop_atr_mult=0.75,
        )
    )
    set_body = _json.loads(set_res.body.decode("utf-8"))
    assert set_body["ok"] is True
    assert set_body["strategy_config"]["gann_box_mode"] == "BOUNCE"
    assert set_body["strategy_config"]["gann_use_htf_view_protection"] is False

    orch2 = TradingSystemOrchestrator(settings=_make_settings(tmp_path))
    restored = orch2.get_strategy_config()
    assert restored["strategy_profile"] == "GANN_BOX_5M"
    assert restored["gann_box_mode"] == "BOUNCE"
    assert restored["gann_confirm_candles"] == 2
    assert restored["gann_hold_candles"] == 3
    assert restored["gann_warmup_minutes"] == 20
    assert restored["gann_use_htf_view_protection"] is False
    assert restored["gann_stop_atr_mult"] == 0.75


def test_gann_box_5m_backtest_runner_and_pine_script(tmp_path: Path, monkeypatch: Any) -> None:
    import json as _json
    from engine.candle_builder import Candle
    from scripts.update_backtest_data import run_nf_bnf_sequential_backtest
    from ui.dashboard import BacktestRunRequest

    nf_candles: list[Candle] = []
    bnf_candles: list[Candle] = []
    for day_idx, d in enumerate((date(2026, 6, 15), date(2026, 6, 16))):
        t_cur = _ist_dt(d.year, d.month, d.day, 9, 15)
        t_end = _ist_dt(d.year, d.month, d.day, 15, 25)
        bar_n = 0
        while t_cur <= t_end:
            nf_px = 25000.0 + bar_n * (0.2 if day_idx == 0 else (3.0 if bar_n >= 3 else 0.2))
            bnf_px = 60000.0 + bar_n * (0.5 if day_idx == 0 else (12.0 if bar_n >= 3 else 0.5))
            nf_candles.append(Candle(timestamp=t_cur, open=nf_px - 3.0, high=nf_px + 12.0, low=nf_px - 12.0, close=nf_px + 4.0, volume=100000, is_closed=True))
            bnf_candles.append(Candle(timestamp=t_cur, open=bnf_px - 6.0, high=bnf_px + 30.0, low=bnf_px - 30.0, close=bnf_px + 10.0, volume=100000, is_closed=True))
            t_cur += timedelta(minutes=5)
            bar_n += 1

    trades = run_nf_bnf_sequential_backtest(
        {"NIFTY": nf_candles, "BANKNIFTY": bnf_candles},
        entry_start_time="09:30",
        entry_cutoff_time="15:00",
        strategy_profile="GANN_BOX_5M",
        gann_box_mode="BREAKOUT",
        gann_confirm_candles=1,
        gann_warmup_minutes=15,
        gann_use_htf_view_protection=True,
        use_supertrend_filter=False,
    )
    assert isinstance(trades, list)
    for t in trades:
        assert "09:30" <= t["in_t"] <= "15:00"

    orch = TradingSystemOrchestrator(settings=_make_settings(tmp_path))
    app = create_app(orch)
    import scripts.update_backtest_data as ubd
    monkeypatch.setattr(ubd, "load_candles_from_cache", lambda idx: nf_candles if idx == "NIFTY" else bnf_candles)
    monkeypatch.setattr(ubd, "load_vix_map", lambda: {})

    resp = app.state.handlers["api_run_backtest"](
        BacktestRunRequest(
            strategy_profile="GANN_BOX_5M",
            entry_start_time="09:30",
            entry_cutoff_time="15:00",
            orb_window_minutes=5,
            use_live_gates=False,
            save=False,
        )
    )
    body = _json.loads(resp.body.decode("utf-8"))
    assert body["ok"] is True
    assert body["meta"]["strategy_profile"] == "GANN_BOX_5M"

    root = Path(__file__).resolve().parent.parent
    pine_src = (root / "scripts" / "nifty_banknifty_gann_box_v6.pine").read_text(encoding="utf-8")
    assert "//@version=6" in pine_src
    assert "NSE:NIFTY" in pine_src and "NSE:BANKNIFTY" in pine_src
    assert "idxAgree" in pine_src and "useViewProtect" in pine_src and "ATM PROTECTION" in pine_src

    mob_html = (root / "mobile" / "www" / "index.html").read_text(encoding="utf-8")
    assert "m-atm-panel" in mob_html
    assert "ms-strat-profile" in mob_html
    assert "m-bt-strategy" in mob_html
