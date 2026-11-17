"""Strategy Optimizer & Monthly Return Analyzer for NIFTY and BANKNIFTY.
Precomputes indicators once and executes ultra-fast grid search.
"""

from __future__ import annotations
import json
import sys
from dataclasses import dataclass
from datetime import datetime, time, date
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pytz

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from engine.candle_builder import Candle
from engine.indicators import calculate_intraday_vwap, calculate_wilder_rsi

IST = pytz.timezone("Asia/Kolkata")

@dataclass
class PrecomputedCandle:
    c: Candle
    vwap: float
    rsi: float

def load_and_precompute(filepath: str) -> List[PrecomputedCandle]:
    with open(filepath, "r", encoding="utf-8") as f:
        raw = json.load(f)
    
    candles: List[Candle] = []
    for r in raw:
        ts = datetime.fromisoformat(r["timestamp"])
        if ts.tzinfo is None:
            ts = IST.localize(ts)
        candles.append(Candle(ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume", 0))))
    candles.sort(key=lambda c: c.timestamp)

    # Precalculate Wilder RSI across the series
    closes = [c.close for c in candles]
    # To be exactly like the running calculation:
    rsis: List[float] = []
    for i in range(len(candles)):
        if i < 14:
            rsis.append(50.0)
        else:
            # Wilder RSI on closes up to i
            # To make it fast, we can calculate incrementally
            pass

    # Fast incremental RSI
    period = 14
    rsis = [50.0] * len(candles)
    if len(candles) > period:
        deltas = [closes[k] - closes[k - 1] for k in range(1, len(closes))]
        gains = [max(0.0, d) for d in deltas]
        losses = [max(0.0, -d) for d in deltas]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        rsis[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

        for k in range(period, len(deltas)):
            avg_gain = ((avg_gain * (period - 1)) + gains[k]) / period
            avg_loss = ((avg_loss * (period - 1)) + losses[k]) / period
            if avg_loss == 0:
                rsis[k + 1] = 100.0 if avg_gain > 0 else 50.0
            else:
                rs = avg_gain / avg_loss
                rsis[k + 1] = round(100.0 - (100.0 / (1.0 + rs)), 2)

    # Precalculate VWAP per day
    days_map: Dict[date, List[int]] = {}
    for idx, c in enumerate(candles):
        days_map.setdefault(c.timestamp.date(), []).append(idx)

    vwaps = [0.0] * len(candles)
    for sess_date, indices in days_map.items():
        cum_tp_vol = 0.0
        cum_vol = 0.0
        cum_tp = 0.0
        count = 0
        for idx in indices:
            c = candles[idx]
            tp = (c.high + c.low + c.close) / 3.0
            vol = max(0.0, c.volume)
            count += 1
            cum_tp += tp
            cum_tp_vol += tp * vol
            cum_vol += vol
            if cum_vol > 0:
                vwaps[idx] = round(cum_tp_vol / cum_vol, 2)
            else:
                vwaps[idx] = round(cum_tp / count, 2)

    return [PrecomputedCandle(c=candles[i], vwap=vwaps[i], rsi=rsis[i]) for i in range(len(candles))]

def calc_charges(buy_to: float, sell_to: float, orders: int = 2) -> float:
    brokerage = orders * 20.0
    stt = sell_to * 0.001
    txn = (buy_to + sell_to) * 0.0005
    gst = (brokerage + txn) * 0.18
    sebi = (buy_to + sell_to) * 0.000001
    stamp = buy_to * 0.00003
    return round(brokerage + stt + txn + gst + sebi + stamp, 2)

def simulate_strategy(
    nifty_pc: List[PrecomputedCandle],
    bnf_pc: List[PrecomputedCandle],
    start_time: time = time(10, 0),
    end_time: time = time(13, 15),
    nifty_sl: float = 25.0,
    nifty_be: float = 20.0,
    nifty_t1: float = 35.0,
    nifty_trail: float = 15.0,
    bnf_sl: float = 45.0,
    bnf_be: float = 45.0,
    bnf_t1: float = 70.0,
    bnf_trail: float = 30.0,
    daily_bias: bool = True,       # CE requires Close > Day Open; PE requires Close < Day Open
    bnf_ce_only: bool = False,     # Only trade CE on BankNifty due to long-term drift
    min_body_ratio: float = 0.40,  # Breakout candle body / total range >= min_body_ratio
    tuesday_multiplier: float = 1.5, # Boost size on Tuesdays
    base_lots_nifty: int = 2,      # 2 lots = 130 qty
    base_lots_bnf: int = 2,        # 2 lots = 60 qty
    orb_window_mins: int = 30,     # 09:15 - 09:45 ORB
):
    datasets = [
        ("NIFTY", nifty_pc, 65, 125.0, nifty_sl, nifty_be, nifty_t1, nifty_trail, base_lots_nifty),
        ("BANKNIFTY", bnf_pc, 30, 260.0, bnf_sl, bnf_be, bnf_t1, bnf_trail, base_lots_bnf),
    ]

    all_trades = []

    for index_name, pcs, lot_size, baseline_prem, sl_pts, be_pts, t1_pts, trail_pts, base_lots in datasets:
        days_map: Dict[date, List[PrecomputedCandle]] = {}
        for item in pcs:
            days_map.setdefault(item.c.timestamp.date(), []).append(item)

        sorted_dates = sorted(days_map.keys())

        for sess_date in sorted_dates:
            day_pcs = days_map[sess_date]
            if len(day_pcs) < 8:
                continue

            day_open = day_pcs[0].c.open

            # Determine ORB window
            orb_end = time(9, 45) if orb_window_mins == 30 else time(9, 30)
            scan_start_default = orb_end

            actual_scan_start = max(start_time, scan_start_default)

            orb_candles = [p.c for p in day_pcs if p.c.timestamp.time() < orb_end]
            if len(orb_candles) < (orb_window_mins // 5):
                continue

            orh = max(c.high for c in orb_candles)
            orl = min(c.low for c in orb_candles)
            range_pct = ((orh - orl) / orl) * 100.0 if orl > 0 else 999.0

            if range_pct > 0.85:
                continue

            dow = sess_date.weekday() # 1 is Tuesday
            lots = int(round(base_lots * tuesday_multiplier)) if dow == 1 else base_lots
            total_qty = lot_size * lots

            trades_today = 0
            for i, pc in enumerate(day_pcs):
                c = pc.c
                c_time = c.timestamp.time()
                if c_time < actual_scan_start or c_time > end_time:
                    continue
                if trades_today >= 1:
                    continue

                vwap = pc.vwap
                rsi = pc.rsi

                # Gate checks
                ce_signal = (c.close > orh) and (vwap is None or c.close > vwap) and (rsi > 58.0)
                pe_signal = (c.close < orl) and (vwap is None or c.close < vwap) and (rsi < 42.0)

                # Daily Bias filter: CE only if close > day_open, PE only if close < day_open
                if daily_bias:
                    if ce_signal and c.close <= day_open:
                        ce_signal = False
                    if pe_signal and c.close >= day_open:
                        pe_signal = False

                # BNF CE only filter
                if bnf_ce_only and index_name == "BANKNIFTY" and pe_signal:
                    pe_signal = False

                # Body to range confirmation
                if min_body_ratio > 0 and (ce_signal or pe_signal):
                    candle_range = c.high - c.low
                    candle_body = abs(c.close - c.open)
                    if candle_range > 0 and (candle_body / candle_range) < min_body_ratio:
                        continue

                if not ce_signal and not pe_signal:
                    continue

                sig_type = "CE" if ce_signal else "PE"
                entry_spot = c.close
                p0 = baseline_prem + 1.0  # 1 pt entry slippage
                trades_today += 1

                sl_price = max(1.0, p0 - sl_pts)
                breakeven_reached = False
                partial_booked = False
                partial_pnl = 0.0
                remaining_qty = total_qty
                delta = 0.58

                exit_option_px = p0
                exit_reason = "EOD_SQUAREOFF"
                exit_time_str = "15:15"

                for next_pc in day_pcs[i + 1:]:
                    fc = next_pc.c
                    if sig_type == "CE":
                        fav_move = fc.high - entry_spot
                        adv_move = entry_spot - fc.low
                        close_move = fc.close - entry_spot
                    else:
                        fav_move = entry_spot - fc.low
                        adv_move = fc.high - entry_spot
                        close_move = entry_spot - fc.close

                    opt_high = p0 + (delta * fav_move)
                    opt_low = p0 - (delta * adv_move)
                    opt_close = p0 + (delta * close_move)

                    if opt_low <= sl_price:
                        exit_time_str = fc.timestamp.strftime("%H:%M")
                        exit_option_px = sl_price - 1.0 # 1 pt exit slippage
                        exit_reason = "BREAKEVEN_SL" if breakeven_reached else "STOP_LOSS"
                        break

                    if not breakeven_reached and opt_high >= (p0 + be_pts):
                        breakeven_reached = True
                        sl_price = p0

                    if not partial_booked and opt_high >= (p0 + t1_pts):
                        partial_booked = True
                        half_qty = lot_size * max(1, lots // 2)
                        target_fill = (p0 + t1_pts) - 1.0
                        partial_pnl = (target_fill - p0) * half_qty
                        remaining_qty = total_qty - half_qty
                        sl_price = max(sl_price, p0 + be_pts)

                    if partial_booked:
                        steps = int((opt_high - (p0 + t1_pts)) // trail_pts)
                        if steps > 0:
                            new_trail = (p0 + t1_pts) + (steps - 1) * trail_pts
                            if new_trail > sl_price:
                                sl_price = new_trail

                    if fc.timestamp.time() >= time(15, 15):
                        exit_time_str = "15:15"
                        exit_option_px = opt_close - 1.0
                        exit_reason = "EOD_SQUAREOFF"
                        break

                if partial_booked:
                    rem_pnl = (exit_option_px - p0) * remaining_qty
                    gross_pnl = round(partial_pnl + rem_pnl, 2)
                else:
                    gross_pnl = round((exit_option_px - p0) * total_qty, 2)

                buy_to = p0 * total_qty
                sell_to = exit_option_px * total_qty
                charges = calc_charges(buy_to, sell_to, orders=3 if partial_booked else 2)
                net_pnl = round(gross_pnl - charges, 2)

                all_trades.append({
                    "date": sess_date.strftime("%Y-%m-%d"),
                    "month": sess_date.strftime("%Y-%m"),
                    "index": index_name,
                    "signal": sig_type,
                    "entry_time": c.timestamp.strftime("%H:%M"),
                    "exit_time": exit_time_str,
                    "exit_reason": exit_reason,
                    "lots": lots,
                    "qty": total_qty,
                    "gross_pnl": gross_pnl,
                    "charges": charges,
                    "net_pnl": net_pnl,
                    "is_win": net_pnl > 0,
                    "margin_used": buy_to,
                })

    all_trades.sort(key=lambda t: (t["date"], t["entry_time"]))

    total_net = sum(t["net_pnl"] for t in all_trades)
    total_trades = len(all_trades)
    wins = [t for t in all_trades if t["net_pnl"] > 0]
    win_rate = (len(wins) / total_trades * 100.0) if total_trades > 0 else 0.0

    gross_wins = sum(t["net_pnl"] for t in wins)
    losses = [t for t in all_trades if t["net_pnl"] <= 0]
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    pf = (gross_wins / gross_loss) if gross_loss > 0 else 99.0

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in all_trades:
        cum += t["net_pnl"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    months = sorted(list(set(t["month"] for t in all_trades)))
    monthly_pnl = {}
    for m in months:
        m_trades = [t for t in all_trades if t["month"] == m]
        m_net = sum(t["net_pnl"] for t in m_trades)
        m_trades_cnt = len(m_trades)
        m_wins = len([t for t in m_trades if t["net_pnl"] > 0])
        monthly_pnl[m] = {
            "net_pnl": round(m_net, 2),
            "trades": m_trades_cnt,
            "win_rate": round(m_wins / m_trades_cnt * 100.0, 1) if m_trades_cnt > 0 else 0,
        }

    return {
        "total_net": round(total_net, 2),
        "total_trades": total_trades,
        "win_rate": round(win_rate, 1),
        "profit_factor": round(pf, 2),
        "max_drawdown": round(max_dd, 2),
        "monthly": monthly_pnl,
        "trades": all_trades,
    }

if __name__ == "__main__":
    print("Loading and precomputing indicators...")
    n_pc = load_and_precompute("data/historical/NIFTY_5m_180d.json")
    b_pc = load_and_precompute("data/historical/BANKNIFTY_5m_180d.json")
    print(f"Precomputed {len(n_pc)} NIFTY and {len(b_pc)} BANKNIFTY candles.")

    configs = [
        {"name": "1. Baseline (Unoptimized)", "start": time(9, 30), "end": time(14, 15), "daily_bias": False, "body": 0.0, "tues_mult": 1.0, "n_sl": 20.0, "orb": 15},
        {"name": "2. Window 10:00-13:15 Only", "start": time(10, 0), "end": time(13, 15), "daily_bias": False, "body": 0.0, "tues_mult": 1.0, "n_sl": 20.0, "orb": 15},
        {"name": "3. Window 10:15-13:15 Only", "start": time(10, 15), "end": time(13, 15), "daily_bias": False, "body": 0.0, "tues_mult": 1.0, "n_sl": 20.0, "orb": 15},
        {"name": "4. 10:00-13:15 + Nifty SL 25", "start": time(10, 0), "end": time(13, 15), "daily_bias": False, "body": 0.0, "tues_mult": 1.0, "n_sl": 25.0, "orb": 15},
        {"name": "5. 10:00-13:15 + Bias (Day Open)", "start": time(10, 0), "end": time(13, 15), "daily_bias": True, "body": 0.0, "tues_mult": 1.0, "n_sl": 25.0, "orb": 15},
        {"name": "6. 10:00-13:15 + Bias + Body 0.40", "start": time(10, 0), "end": time(13, 15), "daily_bias": True, "body": 0.40, "tues_mult": 1.0, "n_sl": 25.0, "orb": 15},
        {"name": "7. 10:00-13:15 + Bias + Body + Tues 1.5x", "start": time(10, 0), "end": time(13, 15), "daily_bias": True, "body": 0.40, "tues_mult": 1.5, "n_sl": 25.0, "orb": 15},
        {"name": "8. 10:15-13:15 + Bias + Body + Tues 1.5x", "start": time(10, 15), "end": time(13, 15), "daily_bias": True, "body": 0.40, "tues_mult": 1.5, "n_sl": 25.0, "orb": 15},
        {"name": "9. 30m ORB (09:45-13:15) + Bias + Tues 1.5x", "start": time(9, 45), "end": time(13, 15), "daily_bias": True, "body": 0.40, "tues_mult": 1.5, "n_sl": 25.0, "orb": 30},
        {"name": "10. Institutional Master: 10:00-13:15 + Bias + Body + Tues 1.5x + BNF CE Only", "start": time(10, 0), "end": time(13, 15), "daily_bias": True, "body": 0.40, "tues_mult": 1.5, "n_sl": 25.0, "orb": 15, "bnf_ce_only": True},
    ]

    print("\n" + "="*95)
    print(f"{'Config Name':<52} | {'Trades':<6} | {'Win %':<6} | {'PF':<5} | {'Net P&L (₹)':<12} | {'Max DD':<8}")
    print("="*95)

    results = []
    for cfg in configs:
        res = simulate_strategy(
            n_pc,
            b_pc,
            start_time=cfg["start"],
            end_time=cfg["end"],
            nifty_sl=cfg["n_sl"],
            daily_bias=cfg["daily_bias"],
            min_body_ratio=cfg["body"],
            tuesday_multiplier=cfg["tues_mult"],
            orb_window_mins=cfg["orb"],
            bnf_ce_only=cfg.get("bnf_ce_only", False),
        )
        results.append((cfg, res))
        print(f"{cfg['name']:<52} | {res['total_trades']:<6} | {res['win_rate']:<6.1f} | {res['profit_factor']:<5.2f} | {res['total_net']:>12,.2f} | {res['max_drawdown']:>8,.2f}")

    print("\n" + "="*95)
    print("MONTHLY BREAKDOWN FOR TOP CANDIDATES")
    print("="*95)
    for idx in [0, 6, 7, 9]:
        cfg, res = results[idx]
        print(f"\n--- {cfg['name']} ---")
        for m, stats in sorted(res['monthly'].items()):
            print(f"  {m}: Net P&L = ₹{stats['net_pnl']:>9,.2f} | Trades = {stats['trades']:>2} | Win Rate = {stats['win_rate']:>5.1f}%")
