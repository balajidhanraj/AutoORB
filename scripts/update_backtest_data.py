import json
import sys
from pathlib import Path
from datetime import datetime, date, time
from typing import List, Dict, Any, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.backtest_orb import (
    Candle, BacktestTrade, BacktestSummary,
    calculate_intraday_vwap, calculate_wilder_rsi,
    calculate_transaction_charges
)
from config.settings import INDEX_SPECS, IST, IndexSpec

def load_candles_from_cache(index_name: str) -> List[Candle]:
    cache_path = ROOT_DIR / "data" / "historical" / f"{index_name}_5m_180d.json"
    with open(cache_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    
    candles = []
    for r in raw:
        ts_str = r.get("timestamp")
        if not ts_str:
            continue
        ts = datetime.fromisoformat(ts_str)
        o = float(r.get("open", 0.0))
        h = float(r.get("high", 0.0))
        l = float(r.get("low", 0.0))
        c = float(r.get("close", 0.0))
        v = int(r.get("volume", 0))
        candles.append(Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v, is_closed=True))
    
    candles.sort(key=lambda x: x.timestamp)
    return candles

def run_master_alpha_backtest(index_name: str, candles: List[Candle]) -> List[Dict[str, Any]]:
    spec: IndexSpec = INDEX_SPECS[index_name]
    active_sl_pts = 25.0 if index_name == "NIFTY" else 50.0
    active_be_pts = 20.0 if index_name == "NIFTY" else 40.0
    active_t1_pts = 40.0 if index_name == "NIFTY" else 80.0
    delta = 0.58
    entry_slippage = 1.0
    exit_slippage = 1.0
    max_trades_per_day = 2
    baseline_atm_premium = 125.0 if index_name == "NIFTY" else 260.0

    days_map: Dict[date, List[Candle]] = {}
    for c in candles:
        days_map.setdefault(c.timestamp.date(), []).append(c)

    sorted_dates = sorted(days_map.keys())
    trade_dicts: List[Dict[str, Any]] = []
    all_seen_candles: List[Candle] = []
    prev_day_close: Optional[float] = None

    for sess_date in sorted_dates:
        day_candles = days_map[sess_date]
        if len(day_candles) < 6:
            continue

        orb_candles = [c for c in day_candles if time(9, 15) <= c.timestamp.time() <= time(9, 40)]
        if len(orb_candles) < 5:
            all_seen_candles.extend(day_candles)
            prev_day_close = day_candles[-1].close
            continue

        orh = max(c.high for c in orb_candles)
        orl = min(c.low for c in orb_candles)
        range_pct = ((orh - orl) / orl) * 100.0 if orl > 0 else 999.0

        if range_pct < 0.20 or range_pct > 0.85:
            all_seen_candles.extend(day_candles)
            prev_day_close = day_candles[-1].close
            continue

        trades_today = 0
        in_trade = False
        day_lost = False

        is_tuesday = (sess_date.weekday() == 1)
        day_lots = 3 if is_tuesday else 2
        total_qty = spec.lot_size * day_lots

        for i, candle in enumerate(day_candles):
            all_seen_candles.append(candle)
            c_time = candle.timestamp.time()

            if c_time < time(10, 0) or c_time > time(13, 15):
                continue

            if in_trade or trades_today >= max_trades_per_day or day_lost:
                continue

            day_so_far = [c for c in day_candles if c.timestamp <= candle.timestamp]
            vwap = calculate_intraday_vwap(day_so_far, session_date=sess_date)
            rsi = calculate_wilder_rsi([c.close for c in all_seen_candles], period=14)

            trend_ce_ok = (prev_day_close is None or candle.close >= prev_day_close)
            trend_pe_ok = (prev_day_close is None or candle.close <= prev_day_close)

            ce_signal = (candle.close > orh) and (vwap is None or candle.close > vwap) and (rsi is not None and rsi > 60.0) and trend_ce_ok
            pe_signal = (candle.close < orl) and (vwap is None or candle.close < vwap) and (rsi is not None and rsi < 40.0) and trend_pe_ok

            if not ce_signal and not pe_signal:
                continue

            sig_type = "CE" if ce_signal else "PE"
            entry_spot = candle.close
            entry_time_str = candle.timestamp.strftime("%H:%M")
            atm_strike = int(round(entry_spot / spec.strike_step) * spec.strike_step)
            p0 = baseline_atm_premium + entry_slippage

            trades_today += 1
            in_trade = True
            sl_price = max(1.0, p0 - active_sl_pts)
            breakeven_reached = False
            partial_booked = False
            partial_pnl = 0.0
            remaining_qty = total_qty

            exit_time_str = "15:15"
            exit_px = p0
            exit_reason = "EOD_SQUAREOFF"
            max_fav_pts = 0.0

            subsequent = [c for c in day_candles if c.timestamp > candle.timestamp]
            for bar in subsequent:
                b_time = bar.timestamp.time()
                spot_move = (bar.high - entry_spot) if sig_type == "CE" else (entry_spot - bar.low)
                spot_worst = (bar.low - entry_spot) if sig_type == "CE" else (entry_spot - bar.high)

                approx_opt_high = p0 + (spot_move * delta)
                approx_opt_low = max(0.5, p0 + (spot_worst * delta))
                approx_opt_close = max(0.5, p0 + (((bar.close - entry_spot) if sig_type == "CE" else (entry_spot - bar.close)) * delta))

                fav_pts = approx_opt_high - p0
                if fav_pts > max_fav_pts:
                    max_fav_pts = fav_pts

                if approx_opt_low <= sl_price:
                    exit_px = max(0.5, sl_price - exit_slippage)
                    exit_time_str = bar.timestamp.strftime("%H:%M")
                    exit_reason = "BREAKEVEN_SL" if breakeven_reached else "STOP_LOSS"
                    if exit_reason == "STOP_LOSS":
                        day_lost = True
                    break

                if not partial_booked and fav_pts >= active_t1_pts:
                    partial_booked = True
                    breakeven_reached = True
                    sl_price = p0
                    t1_exit_px = p0 + active_t1_pts
                    booked_qty = total_qty // 2
                    partial_pnl = booked_qty * (t1_exit_px - p0)
                    remaining_qty = total_qty - booked_qty

                if not breakeven_reached and fav_pts >= active_be_pts:
                    breakeven_reached = True
                    sl_price = p0

                if b_time >= time(15, 15):
                    exit_px = approx_opt_close
                    exit_time_str = "15:15"
                    exit_reason = "EOD_SQUAREOFF"
                    break

            in_trade = False
            pts_gain = exit_px - p0
            total_gross = partial_pnl + (remaining_qty * pts_gain)
            buy_turnover = total_qty * p0
            sell_turnover = total_qty * exit_px
            charges = calculate_transaction_charges(buy_turnover, sell_turnover)
            net_pnl = round(total_gross - charges, 2)

            contract_symbol = f"{index_name} {atm_strike} {sig_type}"
            trade_dicts.append({
                "id": f"{index_name}-{sess_date.strftime('%Y-%m-%d')}-{trades_today}",
                "date": sess_date.strftime("%Y-%m-%d"),
                "idx": index_name,
                "side": sig_type,
                "strike": atm_strike,
                "tradingsymbol": contract_symbol,
                "qty": total_qty,
                "in_t": entry_time_str,
                "out_t": exit_time_str,
                "in_px": p0,
                "out_px": round(exit_px, 2),
                "pts": round(pts_gain, 2),
                "gross": round(total_gross, 2),
                "charges": charges,
                "net": net_pnl,
                "reason": exit_reason,
                "be": breakeven_reached,
                "partial": partial_booked,
                "mode": "BACKTEST",
            })

        prev_day_close = day_candles[-1].close

    return trade_dicts

def main():
    all_trades = []
    for idx in ["NIFTY", "BANKNIFTY"]:
        candles = load_candles_from_cache(idx)
        idx_trades = run_master_alpha_backtest(idx, candles)
        print(f"Generated {len(idx_trades)} Master Alpha trades for {idx}")
        all_trades.extend(idx_trades)
    
    # Sort all trades by date and time
    all_trades.sort(key=lambda t: (t["date"], t["in_t"]))
    
    out_dir = ROOT_DIR / "data" / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Save full 180d trades
    full_path = out_dir / "all_trades_180d.json"
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, indent=2)
    print(f"Saved {len(all_trades)} trades to {full_path}")
    
    # Save minified trades
    min_path = out_dir / "trades_minified.json"
    with open(min_path, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, separators=(',', ':'))
    print(f"Saved minified trades to {min_path}")

    # Re-generate backtest_dashboard.html
    import subprocess
    cmd = [sys.executable, str(ROOT_DIR / "scripts" / "generate_dashboard_html.py")]
    subprocess.run(cmd, check=True)
    print("Regenerated backtest_dashboard.html successfully!")

if __name__ == "__main__":
    main()
