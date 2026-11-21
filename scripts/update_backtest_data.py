"""Fetch 5-minute historical spot candles from Shoonya and run the Master Alpha ORB backtest."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time as time_mod
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from broker.auth import ShoonyaAuthenticator
from broker.shoonya_client import ShoonyaClient
from config.settings import INDEX_SPECS, IST, IndexSpec, load_settings
from scripts.backtest_orb import (
    Candle,
    calculate_intraday_vwap,
    calculate_transaction_charges,
    calculate_wilder_rsi,
    parse_shoonya_candle_row,
)


def fetch_candles_from_shoonya(
    client: ShoonyaClient,
    index_name: str,
    start_date: date,
    end_date: date,
    warmup_days: int = 14,
    chunk_days: int = 25,
) -> List[Candle]:
    """Fetch 5-minute spot candles from Shoonya TPSeries in chunked date windows."""
    spec = INDEX_SPECS[index_name]
    fetch_start = start_date - timedelta(days=warmup_days)
    all_by_ts: Dict[datetime, Candle] = {}

    cur = fetch_start
    chunk_idx = 0
    while cur <= end_date:
        chunk_end = min(end_date, cur + timedelta(days=chunk_days - 1))
        st_dt = IST.localize(datetime.combine(cur, time(9, 0, 0)))
        et_dt = IST.localize(datetime.combine(chunk_end, time(15, 35, 0)))
        chunk_idx += 1

        rows = client.get_time_price_series(
            exchange=spec.spot_exchange,
            token=spec.spot_token,
            starttime=int(st_dt.timestamp()),
            endtime=int(et_dt.timestamp()),
            intrv="5",
        )
        if not rows and cur >= date(2026, 3, 1):
            time_mod.sleep(1.5)
            rows = client.get_time_price_series(
                exchange=spec.spot_exchange,
                token=spec.spot_token,
                starttime=int(st_dt.timestamp()),
                endtime=int(et_dt.timestamp()),
                intrv="5",
            )
        added = 0
        for row in rows:
            c = parse_shoonya_candle_row(row)
            if c is None:
                continue
            if time(9, 15) <= c.timestamp.time() <= time(15, 30):
                if c.timestamp not in all_by_ts:
                    added += 1
                all_by_ts[c.timestamp] = c

        print(
            f"  [{index_name}] Chunk {chunk_idx:02d} ({cur} -> {chunk_end}): "
            f"raw={len(rows)}, added={added}",
            flush=True,
        )
        cur = chunk_end + timedelta(days=1)
        time_mod.sleep(0.8)

    candles = sorted(all_by_ts.values(), key=lambda x: x.timestamp)
    cache_dir = ROOT_DIR / "data" / "historical"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{index_name}_5m_365d.json"
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "timestamp": c.timestamp.isoformat(),
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in candles
            ],
            f,
        )
    print(f"  [{index_name}] Cached {len(candles)} unique 5m candles -> {cache_path}", flush=True)
    return candles


def load_candles_from_cache(index_name: str) -> List[Candle]:
    """Load 5m candles from 365d cache (or 180d fallback)."""
    for suffix in ("365d", "180d"):
        cache_path = ROOT_DIR / "data" / "historical" / f"{index_name}_5m_{suffix}.json"
        if cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            candles = []
            for r in raw:
                ts_str = r.get("timestamp")
                if not ts_str:
                    continue
                ts = datetime.fromisoformat(ts_str)
                candles.append(
                    Candle(
                        timestamp=ts,
                        open=float(r.get("open", 0.0)),
                        high=float(r.get("high", 0.0)),
                        low=float(r.get("low", 0.0)),
                        close=float(r.get("close", 0.0)),
                        volume=int(r.get("volume", 0)),
                        is_closed=True,
                    )
                )
            candles.sort(key=lambda x: x.timestamp)
            return candles
    raise FileNotFoundError(f"No historical cache found for {index_name}; run with --fetch")


def run_master_alpha_backtest(
    index_name: str,
    candles: List[Candle],
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Run Master Alpha 5-gate ORB + VWAP + Wilder RSI(14) backtest."""
    spec: IndexSpec = INDEX_SPECS[index_name]
    active_sl_pts = spec.sl_pts
    active_be_pts = spec.breakeven_trigger_pts
    active_t1_pts = spec.target1_pts
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
            all_seen_candles.extend(day_candles)
            if day_candles:
                prev_day_close = day_candles[-1].close
            continue

        # Warmup-only dates before start_date still feed RSI & prev_day_close
        in_window = (
            (start_date is None or sess_date >= start_date)
            and (end_date is None or sess_date <= end_date)
        )
        if not in_window:
            all_seen_candles.extend(day_candles)
            prev_day_close = day_candles[-1].close
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

        is_tuesday = sess_date.weekday() == 1
        day_lots = 3 if is_tuesday else 2
        total_qty = spec.lot_size * day_lots

        for candle in day_candles:
            all_seen_candles.append(candle)
            c_time = candle.timestamp.time()

            if c_time < time(10, 0) or c_time > time(13, 15):
                continue

            if in_trade or trades_today >= max_trades_per_day or day_lost:
                continue

            day_so_far = [c for c in day_candles if c.timestamp <= candle.timestamp]
            vwap = calculate_intraday_vwap(day_so_far, session_date=sess_date)
            rsi = calculate_wilder_rsi([c.close for c in all_seen_candles[-120:]], period=14)

            trend_ce_ok = prev_day_close is None or candle.close >= prev_day_close
            trend_pe_ok = prev_day_close is None or candle.close <= prev_day_close

            ce_signal = (
                (candle.close > orh)
                and (vwap is None or candle.close > vwap)
                and (rsi is not None and rsi > 60.0)
                and trend_ce_ok
            )
            pe_signal = (
                (candle.close < orl)
                and (vwap is None or candle.close < vwap)
                and (rsi is not None and rsi < 40.0)
                and trend_pe_ok
            )

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
                approx_opt_close = max(
                    0.5,
                    p0 + (((bar.close - entry_spot) if sig_type == "CE" else (entry_spot - bar.close)) * delta),
                )

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
            trade_dicts.append(
                {
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
                    "charges": round(charges, 2),
                    "net": net_pnl,
                    "reason": exit_reason,
                    "be": breakeven_reached,
                    "partial": partial_booked,
                    "mode": "BACKTEST",
                }
            )

        prev_day_close = day_candles[-1].close

    return trade_dicts


def compute_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(trades)
    wins = sum(1 for t in trades if t["net"] > 0)
    losses = sum(1 for t in trades if t["net"] < 0)
    gross = round(sum(t["gross"] for t in trades), 2)
    charges = round(sum(t["charges"] for t in trades), 2)
    net = round(sum(t["net"] for t in trades), 2)
    g_win = sum(t["net"] for t in trades if t["net"] > 0)
    g_loss = abs(sum(t["net"] for t in trades if t["net"] < 0))
    pf = round(g_win / g_loss, 2) if g_loss > 0 else (99.0 if g_win > 0 else 0.0)
    wr = round((wins / total) * 100.0, 2) if total > 0 else 0.0

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t["net"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    avg_pnl = round(net / total, 2) if total > 0 else 0.0
    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": wr,
        "gross": gross,
        "charges": charges,
        "net": net,
        "pf": pf,
        "max_dd": round(max_dd, 2),
        "avg_pnl": avg_pnl,
    }


def print_report(all_trades: List[Dict[str, Any]], date_ranges: Dict[str, Tuple[str, str]]) -> None:
    print("\n================ 1-YEAR MASTER ALPHA BACKTEST REPORT ================")
    for idx, (d0, d1) in date_ranges.items():
        print(f"  {idx:10s} candle coverage: {d0} -> {d1}")
    print("---------------------------------------------------------------------")
    for label, subset in [
        ("NIFTY", [t for t in all_trades if t["idx"] == "NIFTY"]),
        ("BANKNIFTY", [t for t in all_trades if t["idx"] == "BANKNIFTY"]),
        ("COMBINED", all_trades),
    ]:
        m = compute_metrics(subset)
        print(
            f"  {label:10s} | Trades: {m['trades']:3d} (W:{m['wins']:3d}/L:{m['losses']:3d}) "
            f"| WR: {m['win_rate']:5.1f}% | PF: {m['pf']:4.2f} "
            f"| Gross: Rs {m['gross']:+10,.2f} | Charges: Rs {m['charges']:8,.2f} "
            f"| Net: Rs {m['net']:+10,.2f} | MaxDD: Rs {m['max_dd']:9,.2f} | Avg: Rs {m['avg_pnl']:+8,.2f}"
        )

    print("\n--------------------- MONTHLY BREAKDOWN -----------------------------")
    months: Dict[str, List[Dict[str, Any]]] = {}
    for t in all_trades:
        months.setdefault(t["date"][:7], []).append(t)
    for ym in sorted(months.keys()):
        m = compute_metrics(months[ym])
        print(
            f"  {ym} | Trades: {m['trades']:2d} (W:{m['wins']:2d}/L:{m['losses']:2d}) "
            f"| WR: {m['win_rate']:5.1f}% | PF: {m['pf']:4.2f} | Net: Rs {m['net']:+10,.2f}"
        )
    print("=====================================================================\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch 5m candles and run Master Alpha ORB backtest.")
    parser.add_argument("--start", type=str, default="2025-11-20", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, default="2026-11-20", help="End date YYYY-MM-DD")
    parser.add_argument("--fetch", action="store_true", help="Fetch fresh 5m candles from Shoonya TPSeries")
    parser.add_argument("--force", action="store_true", help="Force re-fetch even if 365d cache is non-empty")
    args = parser.parse_args()

    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)

    client: Optional[ShoonyaClient] = None
    if args.fetch:
        settings = load_settings()
        client = ShoonyaClient(settings=settings)
        auth = ShoonyaAuthenticator(settings, client.api)
        if not auth.load_cached_token():
            auth.login_via_selenium()
        print(f"Authenticated with Shoonya ({settings.shoonya_user_id}); fetching {start_d} -> {end_d}...", flush=True)

    all_trades: List[Dict[str, Any]] = []
    date_ranges: Dict[str, Tuple[str, str]] = {}
    for idx in ("NIFTY", "BANKNIFTY"):
        cache_365 = ROOT_DIR / "data" / "historical" / f"{idx}_5m_365d.json"
        cached_ok = False
        if not args.force and cache_365.exists() and cache_365.stat().st_size > 1000:
            candles = load_candles_from_cache(idx)
            if candles:
                cached_ok = True
                print(f"  [{idx}] Loaded {len(candles)} cached 5m candles from {cache_365}", flush=True)

        if not cached_ok:
            if client is not None:
                candles = fetch_candles_from_shoonya(client, idx, start_d, end_d)
            else:
                candles = load_candles_from_cache(idx)

        if candles:
            date_ranges[idx] = (
                candles[0].timestamp.strftime("%Y-%m-%d"),
                candles[-1].timestamp.strftime("%Y-%m-%d"),
            )
        else:
            date_ranges[idx] = ("N/A", "N/A")

        idx_trades = run_master_alpha_backtest(idx, candles, start_date=start_d, end_date=end_d)
        print(f"Generated {len(idx_trades)} Master Alpha trades for {idx} ({start_d} -> {end_d})", flush=True)
        all_trades.extend(idx_trades)

    all_trades.sort(key=lambda t: (t["date"], t["in_t"]))
    print_report(all_trades, date_ranges)

    out_dir = ROOT_DIR / "data" / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)

    full_path = out_dir / "all_trades_180d.json"
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, indent=2)

    min_path = out_dir / "trades_minified.json"
    with open(min_path, "w", encoding="utf-8") as f:
        json.dump(all_trades, f, separators=(",", ":"))

    csv_path = out_dir / f"backtest_1y_{start_d.strftime('%Y%m%d')}_to_{end_d.strftime('%Y%m%d')}.csv"
    if all_trades:
        fieldnames = list(all_trades[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_trades)

    print(f"Saved {len(all_trades)} trades -> {full_path}, {min_path}, {csv_path}", flush=True)


if __name__ == "__main__":
    main()
