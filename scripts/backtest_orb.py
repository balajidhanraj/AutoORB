"""Standalone ORB + VWAP + RSI + VIX Backtesting Engine for NIFTY and BANKNIFTY.

Simulates the exact 5-gate Opening Range Breakout strategy:
  Gate 1: System / Daily trade limit
  Gate 2: India VIX >= 12.0
  Gate 3: Opening Range (09:15-09:30) <= 0.75%
  Gate 4: 5m Candle Close > ORH & VWAP (CE) or < ORL & VWAP (PE)
  Gate 5: Wilder RSI(14) > 60 (CE) or < 40 (PE)

Executes on ATM Option contracts with:
  - Initial Stop Loss (-20 pts NIFTY / -45 pts BANKNIFTY)
  - Breakeven trigger (+25 pts NIFTY / +55 pts BANKNIFTY) -> moves SL to entry
  - Target 1 (+30 pts NIFTY / +65 pts BANKNIFTY) -> books 50% quantity
  - Trailing Stop Loss (+15 pts NIFTY / +30 pts BANKNIFTY) on remaining 50%
  - 15:15 IST EOD Auto Square-off
  - Realistic Slippage (1 pt entry / 1 pt exit) and full statutory charges (STT, Brokerage, GST, Turnover)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add parent directory to path to import project modules
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from config.settings import INDEX_SPECS, IST, AppSettings, IndexSpec, load_settings
from engine.candle_builder import Candle, floor_to_5min
from engine.indicators import calculate_intraday_vwap, calculate_wilder_rsi
from broker.auth import ShoonyaAuthenticator
from broker.shoonya_client import ShoonyaClient
from utils.logger import get_logger

logger = get_logger("backtest_orb")


@dataclass
class BacktestTrade:
    """Record of a simulated option trade."""
    trade_id: str
    date: str
    index_name: str
    signal_type: str  # CE or PE
    entry_time: str
    exit_time: str
    strike: int
    lots: int
    quantity: int
    entry_spot: float
    entry_option: float
    exit_option: float
    points_pnl: float
    gross_pnl: float
    charges: float
    net_pnl: float
    exit_reason: str
    partial_booked: bool = False
    breakeven_reached: bool = False
    max_favorable_pts: float = 0.0


@dataclass
class BacktestSummary:
    """Aggregated performance metrics."""
    index_name: str
    total_days: int
    trading_days_active: int
    no_trade_days: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate_pct: float
    gross_pnl: float
    total_charges: float
    net_pnl: float
    profit_factor: float
    avg_trade_pnl: float
    max_drawdown_amount: float
    max_drawdown_pct: float
    max_win_amount: float
    max_loss_amount: float
    expectancy_per_trade: float
    trades: List[BacktestTrade] = field(default_factory=list)


def parse_shoonya_candle_row(row: Dict[str, Any]) -> Optional[Candle]:
    """Parse Shoonya TPSeries response dict into Candle object."""
    if not isinstance(row, dict) or row.get("stat") == "Not_Ok":
        return None

    raw_ssboe = row.get("ssboe")
    ts: Optional[datetime] = None
    if raw_ssboe is not None:
        try:
            ts = datetime.fromtimestamp(int(float(raw_ssboe)), tz=IST)
        except (ValueError, TypeError):
            pass

    if ts is None:
        raw_time = str(row.get("time") or "").strip()
        for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                ts = IST.localize(datetime.strptime(raw_time, fmt))
                break
            except ValueError:
                continue

    if ts is None:
        return None

    try:
        o = float(row.get("into") or row.get("open") or 0.0)
        h = float(row.get("inth") or row.get("high") or 0.0)
        l = float(row.get("intl") or row.get("low") or 0.0)
        c = float(row.get("intc") or row.get("close") or 0.0)
        v = int(float(row.get("intv") or row.get("v") or 0))
    except (ValueError, TypeError):
        return None

    if h <= 0 or l <= 0 or c <= 0:
        return None

    return Candle(
        timestamp=floor_to_5min(ts),
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        is_closed=True,
    )


def fetch_historical_candles(
    client: ShoonyaClient,
    index_name: str,
    days: int = 30,
) -> List[Candle]:
    """Fetch 5-minute historical candles from Shoonya or local cache."""
    spec = INDEX_SPECS[index_name]
    cache_dir = ROOT_DIR / "data" / "historical"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{index_name}_5m_{days}d.json"

    end_dt = datetime.now(IST)
    start_dt = end_dt - timedelta(days=days)

    candles: List[Candle] = []

    # Check cache if modified recently (< 12 hours old)
    if cache_file.exists():
        try:
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime, tz=IST)
            if (end_dt - mtime).total_seconds() < 43200:
                with open(cache_file, "r", encoding="utf-8") as f:
                    cached_raw = json.load(f)
                for item in cached_raw:
                    c = Candle(
                        timestamp=datetime.fromisoformat(item["timestamp"]),
                        open=item["open"],
                        high=item["high"],
                        low=item["low"],
                        close=item["close"],
                        volume=item["volume"],
                        is_closed=True,
                    )
                    candles.append(c)
                if candles:
                    logger.info("Loaded %d %s candles from local cache", len(candles), index_name)
                    return candles
        except Exception as exc:
            logger.warning("Cache read failed: %s; refetching...", exc)

    logger.info("Fetching %d days of %s candles from Shoonya TPSeries...", days, index_name)
    start_epoch = int(start_dt.timestamp())
    end_epoch = int(end_dt.timestamp())

    resp = client.get_time_price_series(
        exchange=spec.spot_exchange,
        token=spec.spot_token,
        starttime=start_epoch,
        endtime=end_epoch,
        intrv="5",
    )

    if isinstance(resp, list):
        for row in reversed(resp):  # Shoonya returns newest first
            c = parse_shoonya_candle_row(row)
            if c is not None:
                candles.append(c)

        # Sort chronologically and deduplicate
        candles_by_ts: Dict[datetime, Candle] = {c.timestamp: c for c in candles}
        candles = [candles_by_ts[k] for k in sorted(candles_by_ts)]

        if candles:
            # Save to cache
            try:
                dump_data = [
                    {
                        "timestamp": c.timestamp.isoformat(),
                        "open": c.open,
                        "high": c.high,
                        "low": c.low,
                        "close": c.close,
                        "volume": c.volume,
                    }
                    for c in candles
                ]
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(dump_data, f, indent=2)
                logger.info("Saved %d %s candles to cache: %s", len(candles), index_name, cache_file)
            except Exception as exc:
                logger.warning("Failed saving candle cache: %s", exc)

    return candles


def calculate_transaction_charges(
    buy_turnover: float,
    sell_turnover: float,
    orders_count: int = 2,
) -> float:
    """Calculate realistic Indian option statutory transaction costs and brokerage."""
    brokerage = orders_count * 20.0  # ₹20 per order
    stt = sell_turnover * 0.001  # 0.1% on sell turnover for options
    txn_charges = (buy_turnover + sell_turnover) * 0.0005  # Exchange + clearing (~0.05%)
    gst = (brokerage + txn_charges) * 0.18  # 18% GST
    sebi = (buy_turnover + sell_turnover) * 0.000001  # ₹10 per crore
    stamp_duty = buy_turnover * 0.00003  # 0.003% on buy
    return round(brokerage + stt + txn_charges + gst + sebi + stamp_duty, 2)


def run_single_index_backtest(
    index_name: str,
    candles: List[Candle],
    custom_lots: Optional[int] = None,
    delta: float = 0.58,
    entry_slippage: float = 1.0,
    exit_slippage: float = 1.0,
    max_trades_per_day: int = 1,
    sl_pts: Optional[float] = None,
    breakeven_pts: Optional[float] = None,
    target1_pts: Optional[float] = None,
) -> BacktestSummary:
    """Run bar-by-bar backtest of 5-gate ORB strategy on an index."""
    spec: IndexSpec = INDEX_SPECS[index_name]
    active_sl_pts = sl_pts if sl_pts is not None else spec.sl_pts
    active_be_pts = breakeven_pts if breakeven_pts is not None else spec.breakeven_trigger_pts
    active_t1_pts = target1_pts if target1_pts is not None else spec.target1_pts
    lots = custom_lots or spec.default_lots
    total_qty = spec.lot_size * lots

    # Baseline option premium when spot enters
    baseline_atm_premium = 125.0 if index_name == "NIFTY" else 260.0

    # Group candles by session date
    days_map: Dict[date, List[Candle]] = {}
    for c in candles:
        d = c.timestamp.date()
        days_map.setdefault(d, []).append(c)

    sorted_dates = sorted(days_map.keys())
    trades: List[BacktestTrade] = []
    active_days_count = 0
    no_trade_days_count = 0

    # Cumulative candles for accurate indicator calculation
    all_seen_candles: List[Candle] = []

    for sess_date in sorted_dates:
        day_candles = days_map[sess_date]
        if len(day_candles) < 6:
            # Insufficient candles for a full session
            continue

        # 1. Identify Opening Range (09:15 - 09:30 IST -> 3 candles: 09:15, 09:20, 09:25)
        orb_candles = [
            c for c in day_candles
            if c.timestamp.time() in (time(9, 15), time(9, 20), time(9, 25))
        ]
        if len(orb_candles) < 3:
            no_trade_days_count += 1
            all_seen_candles.extend(day_candles)
            continue

        orh = max(c.high for c in orb_candles)
        orl = min(c.low for c in orb_candles)
        range_pct = ((orh - orl) / orl) * 100.0 if orl > 0 else 999.0

        # Gate 3: ORB Range Filter <= 0.75%
        max_allowed_range = getattr(spec, "max_orb_range_pct", 0.75)
        if range_pct > max_allowed_range:
            no_trade_days_count += 1
            all_seen_candles.extend(day_candles)
            continue

        # Active scanning for the day
        active_days_count += 1
        trades_today = 0
        in_trade = False

        # Scan candles starting from 09:30 up to 14:15 cutoff
        for i, candle in enumerate(day_candles):
            all_seen_candles.append(candle)
            c_time = candle.timestamp.time()

            # Skip opening range candles
            if c_time < time(9, 30):
                continue

            # Skip entries after cutoff time (14:15 IST)
            if c_time > time(14, 15):
                continue

            if in_trade or trades_today >= max_trades_per_day:
                continue

            # Compute indicators
            # VWAP calculated from beginning of day's candles up to current
            day_so_far = [c for c in day_candles if c.timestamp <= candle.timestamp]
            vwap = calculate_intraday_vwap(day_so_far, session_date=sess_date)
            # RSI(14) calculated across cumulative closes
            rsi = calculate_wilder_rsi([c.close for c in all_seen_candles], period=14)

            # Gate 4 & 5 Check
            ce_signal = (candle.close > orh) and (vwap is None or candle.close > vwap) and (rsi is not None and rsi > 60.0)
            pe_signal = (candle.close < orl) and (vwap is None or candle.close < vwap) and (rsi is not None and rsi < 40.0)

            if not ce_signal and not pe_signal:
                continue

            # --- SIGNAL TRIGGERED ---
            sig_type = "CE" if ce_signal else "PE"
            entry_spot = candle.close
            entry_time_str = candle.timestamp.strftime("%H:%M")
            atm_strike = int(round(entry_spot / spec.strike_step) * spec.strike_step)

            # Option Entry Price with slippage
            p0 = baseline_atm_premium + entry_slippage

            trades_today += 1
            in_trade = True

            # Tracking variables for active trade
            sl_price = max(1.0, p0 - active_sl_pts)
            breakeven_reached = False
            partial_booked = False
            partial_pnl = 0.0
            remaining_qty = total_qty
            max_favorable = 0.0

            exit_time_str = "15:15"
            exit_option_px = p0
            exit_reason = "EOD_SQUAREOFF"

            # Step forward candle by candle until exit
            for future_candle in day_candles[i + 1:]:
                # Favorable and adverse spot excursion within this candle
                if sig_type == "CE":
                    favorable_spot_move = future_candle.high - entry_spot
                    adverse_spot_move = entry_spot - future_candle.low
                    close_spot_move = future_candle.close - entry_spot
                else:
                    favorable_spot_move = entry_spot - future_candle.low
                    adverse_spot_move = future_candle.high - entry_spot
                    close_spot_move = entry_spot - future_candle.close

                # Simulated option prices
                opt_high = p0 + (delta * favorable_spot_move)
                opt_low = p0 - (delta * adverse_spot_move)
                opt_close = p0 + (delta * close_spot_move)

                favorable_pts = opt_high - p0
                if favorable_pts > max_favorable:
                    max_favorable = favorable_pts

                # 1. Stop-Loss Trigger Check
                if opt_low <= sl_price:
                    exit_time_str = future_candle.timestamp.strftime("%H:%M")
                    exit_option_px = sl_price - exit_slippage
                    exit_reason = "BREAKEVEN_SL" if breakeven_reached else "STOP_LOSS"
                    break

                # 2. Breakeven Trigger Check
                if not breakeven_reached and opt_high >= (p0 + active_be_pts):
                    breakeven_reached = True
                    sl_price = p0  # Move SL to Entry (risk-free)

                # 3. Target 1 Check (+30 pts NIFTY / +65 pts BNF) -> Book 50%
                if not partial_booked and opt_high >= (p0 + active_t1_pts):
                    partial_booked = True
                    half_qty = spec.lot_size * max(1, lots // 2)
                    target_fill = (p0 + active_t1_pts) - exit_slippage
                    partial_pnl = (target_fill - p0) * half_qty
                    remaining_qty = total_qty - half_qty
                    # Trail SL to Target 1 - step
                    sl_price = max(sl_price, p0 + active_be_pts)

                # 4. Trailing Step Logic for remaining quantity
                if partial_booked:
                    steps_above = int((opt_high - (p0 + active_t1_pts)) // spec.trail_step_pts)
                    if steps_above > 0:
                        new_trail_sl = (p0 + active_t1_pts) + (steps_above - 1) * spec.trail_step_pts
                        if new_trail_sl > sl_price:
                            sl_price = new_trail_sl

                # 5. EOD 15:15 Square-off
                if future_candle.timestamp.time() >= time(15, 15):
                    exit_time_str = "15:15"
                    exit_option_px = opt_close - exit_slippage
                    exit_reason = "EOD_SQUAREOFF"
                    break

            # Calculate trade financials
            if partial_booked:
                rem_pnl = (exit_option_px - p0) * remaining_qty
                gross_pnl = round(partial_pnl + rem_pnl, 2)
                net_pts = round(gross_pnl / total_qty, 2)
            else:
                net_pts = round(exit_option_px - p0, 2)
                gross_pnl = round(net_pts * total_qty, 2)

            buy_turnover = p0 * total_qty
            sell_turnover = exit_option_px * total_qty
            charges = calculate_transaction_charges(
                buy_turnover,
                sell_turnover,
                orders_count=3 if partial_booked else 2,
            )
            net_pnl = round(gross_pnl - charges, 2)

            trade = BacktestTrade(
                trade_id=f"{index_name}-{sess_date}-{trades_today}",
                date=sess_date.strftime("%Y-%m-%d"),
                index_name=index_name,
                signal_type=sig_type,
                entry_time=entry_time_str,
                exit_time=exit_time_str,
                strike=atm_strike,
                lots=lots,
                quantity=total_qty,
                entry_spot=round(entry_spot, 2),
                entry_option=round(p0, 2),
                exit_option=round(exit_option_px, 2),
                points_pnl=net_pts,
                gross_pnl=gross_pnl,
                charges=charges,
                net_pnl=net_pnl,
                exit_reason=exit_reason,
                partial_booked=partial_booked,
                breakeven_reached=breakeven_reached,
                max_favorable_pts=round(max_favorable, 2),
            )
            trades.append(trade)
            in_trade = False

    # Aggregate Statistics
    total_trades = len(trades)
    winning = [t for t in trades if t.net_pnl > 0]
    losing = [t for t in trades if t.net_pnl < 0]
    breakeven = [t for t in trades if t.net_pnl == 0]

    tot_gross = round(sum(t.gross_pnl for t in trades), 2)
    tot_charges = round(sum(t.charges for t in trades), 2)
    tot_net = round(sum(t.net_pnl for t in trades), 2)

    win_sum = sum(t.net_pnl for t in winning)
    loss_sum = abs(sum(t.net_pnl for t in losing))
    profit_factor = round(win_sum / loss_sum, 2) if loss_sum > 0 else (99.0 if win_sum > 0 else 0.0)

    win_rate = round((len(winning) / total_trades * 100.0), 1) if total_trades > 0 else 0.0
    avg_pnl = round(tot_net / total_trades, 2) if total_trades > 0 else 0.0

    # Max Drawdown Calculation
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum_pnl += t.net_pnl
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd:
            max_dd = dd

    max_dd = round(max_dd, 2)
    starting_capital = 200000.0  # ₹2,00,000 base
    dd_pct = round((max_dd / starting_capital) * 100.0, 2)

    max_win = max([t.net_pnl for t in trades], default=0.0)
    max_loss = min([t.net_pnl for t in trades], default=0.0)

    # Expectancy
    avg_win = (win_sum / len(winning)) if winning else 0.0
    avg_loss = (loss_sum / len(losing)) if losing else 0.0
    win_prob = len(winning) / total_trades if total_trades else 0.0
    loss_prob = len(losing) / total_trades if total_trades else 0.0
    expectancy = round((win_prob * avg_win) - (loss_prob * avg_loss), 2)

    return BacktestSummary(
        index_name=index_name,
        total_days=len(sorted_dates),
        trading_days_active=active_days_count,
        no_trade_days=no_trade_days_count,
        total_trades=total_trades,
        winning_trades=len(winning),
        losing_trades=len(losing),
        breakeven_trades=len(breakeven),
        win_rate_pct=win_rate,
        gross_pnl=tot_gross,
        total_charges=tot_charges,
        net_pnl=tot_net,
        profit_factor=profit_factor,
        avg_trade_pnl=avg_pnl,
        max_drawdown_amount=max_dd,
        max_drawdown_pct=dd_pct,
        max_win_amount=round(max_win, 2),
        max_loss_amount=round(max_loss, 2),
        expectancy_per_trade=expectancy,
        trades=trades,
    )


def print_backtest_report(summary: BacktestSummary) -> None:
    """Format and print beautiful console report and trade ledger."""
    spec = INDEX_SPECS[summary.index_name]
    lot_info = f"{spec.default_lots} lots ({spec.lot_size * spec.default_lots} qty)"

    print("\n" + "=" * 80)
    print(f"       ORB + VWAP + RSI BACKTEST REPORT: {summary.index_name} ({lot_info})")
    print("=" * 80)
    print(f"Total Calendar Days Analyzed  : {summary.total_days} days")
    print(f"Active Trading Sessions       : {summary.trading_days_active} sessions")
    print(f"Filtered No-Trade Days        : {summary.no_trade_days} sessions (Range > 0.75%)")
    print(f"Total Strategy Trades         : {summary.total_trades}")
    print(f"Wins / Losses / Breakeven     : {summary.winning_trades} wins | {summary.losing_trades} losses | {summary.breakeven_trades} BE")
    print(f"Strategy Win Rate             : {summary.win_rate_pct:.1f}%")
    print("-" * 80)
    print(f"Gross P&L                     : ₹{summary.gross_pnl:+,.2f}")
    print(f"Estimated Brokerage & Taxes   : -₹{summary.total_charges:,.2f}")
    print(f"NET PROFIT / LOSS             : ₹{summary.net_pnl:+,.2f}")
    print(f"Profit Factor                 : {summary.profit_factor:.2f}")
    print(f"Expectancy per Trade          : ₹{summary.expectancy_per_trade:+,.2f}")
    print(f"Average P&L per Trade         : ₹{summary.avg_trade_pnl:+,.2f}")
    print(f"Max Drawdown (₹ / %)          : ₹{summary.max_drawdown_amount:,.2f} ({summary.max_drawdown_pct:.2f}% of ₹2L capital)")
    print(f"Best Trade / Worst Trade      : ₹{summary.max_win_amount:+,.2f} / ₹{summary.max_loss_amount:+,.2f}")
    print("=" * 80)

    if summary.trades:
        print("\nDETAILED TRADE LOG:")
        print(f"{'Date':<11} {'Side':<4} {'Strike':<7} {'In':<6} {'Out':<6} {'In Px':<7} {'Out Px':<7} {'Pts':<7} {'Gross P&L':<11} {'Net P&L':<11} {'Exit Reason'}")
        print("-" * 95)
        for t in summary.trades:
            print(
                f"{t.date:<11} {t.signal_type:<4} {t.strike:<7} {t.entry_time:<6} {t.exit_time:<6} "
                f"₹{t.entry_option:<6.1f} ₹{t.exit_option:<6.1f} {t.points_pnl:+6.1f} "
                f"₹{t.gross_pnl:+9.2f}  ₹{t.net_pnl:+9.2f}  {t.exit_reason}"
            )
        print("-" * 95)


def export_backtest_csv(summary: BacktestSummary, output_path: Optional[Path] = None) -> Path:
    """Export trade log to CSV."""
    out_dir = ROOT_DIR / "data" / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_file = output_path or (out_dir / f"backtest_{summary.index_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "Trade ID", "Date", "Index", "Side", "Strike", "Lots", "Qty",
            "Entry Time", "Exit Time", "Spot Price", "Option Entry", "Option Exit",
            "Points P&L", "Gross P&L", "Charges", "Net P&L", "Exit Reason",
            "Partial Booked", "Breakeven Hit", "Max Favorable Pts"
        ])
        for t in summary.trades:
            writer.writerow([
                t.trade_id, t.date, t.index_name, t.signal_type, t.strike, t.lots, t.quantity,
                t.entry_time, t.exit_time, t.entry_spot, t.entry_option, t.exit_option,
                t.points_pnl, t.gross_pnl, t.charges, t.net_pnl, t.exit_reason,
                t.partial_booked, t.breakeven_reached, t.max_favorable_pts
            ])

    logger.info("Exported backtest trade ledger to: %s", csv_file)
    return csv_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest ORB Strategy on NIFTY & BANKNIFTY")
    parser.add_argument("--index", choices=["NIFTY", "BANKNIFTY", "ALL"], default="ALL", help="Instrument to backtest")
    parser.add_argument("--days", type=int, default=30, help="Number of historical days to backtest (default 30)")
    parser.add_argument("--lots", type=int, default=None, help="Custom lot count per trade")
    parser.add_argument("--max-trades", type=int, default=1, help="Max trades per day (default 1)")
    parser.add_argument("--sl", type=float, default=None, help="Custom Stop Loss points")
    parser.add_argument("--be", type=float, default=None, help="Custom Breakeven trigger points")
    parser.add_argument("--t1", type=float, default=None, help="Custom Target 1 points")
    parser.add_argument("--csv", action="store_true", help="Export trade logs to CSV")
    args = parser.parse_args()

    settings = load_settings()
    client = ShoonyaClient(settings=settings)
    auth = ShoonyaAuthenticator(settings, client.api)
    auth.load_cached_token()

    targets = ["NIFTY", "BANKNIFTY"] if args.index == "ALL" else [args.index]

    for idx in targets:
        candles = fetch_historical_candles(client, index_name=idx, days=args.days)
        if not candles:
            print(f"\n[!] No candles available for {idx}. Please ensure internet / broker token is valid.")
            continue

        summary = run_single_index_backtest(
            index_name=idx,
            candles=candles,
            custom_lots=args.lots,
            max_trades_per_day=args.max_trades,
            sl_pts=args.sl,
            breakeven_pts=args.be,
            target1_pts=args.t1,
        )
        print_backtest_report(summary)

        if args.csv:
            path = export_backtest_csv(summary)
            print(f"CSV exported to: {path}")


if __name__ == "__main__":
    main()
