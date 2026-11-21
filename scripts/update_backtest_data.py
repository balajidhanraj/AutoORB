"""Fetch 5-minute historical spot candles from Shoonya and run the Hybrid Live-Parity Master Alpha ORB backtest.

Hybrid Strategy (matches live RiskManager & ORBStrategy 1-to-1):
  - NIFTY     (Option A): Max 2 trades/day, strict sequential no-overlap, ORB + VWAP + RSI(14) + Daily Bias.
  - BANKNIFTY (Option H): Max 2 trades/day, Breakeven-Locked Pyramiding (Leg #2 allowed while Leg #1 is alive
                          ONLY after Leg #1 has already locked SL at Breakeven +45 pts) + 20-EMA + Body >= 0.40.
  - Daily Risk Guardrail: Rs 10,000 shared daily max loss cap across both indices.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time as time_mod
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from broker.auth import ShoonyaAuthenticator
from broker.instrument_mapper import get_target_expiry
from broker.shoonya_client import ShoonyaClient
from config.settings import INDIA_VIX_EXCHANGE, INDIA_VIX_TOKEN, INDEX_SPECS, IST, IndexSpec, load_settings
from engine.indicators import calculate_ema
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


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_option_price(
    spot: float,
    strike: float,
    t_years: float,
    sigma: float,
    opt_type: str,
    r: float = 0.065,
) -> float:
    """Black-Scholes European option price driven by real Shoonya Spot, DTE, and India VIX."""
    intrinsic = max(0.0, spot - strike) if opt_type == "CE" else max(0.0, strike - spot)
    if t_years <= 1e-6 or sigma <= 1e-4 or spot <= 0 or strike <= 0:
        return max(0.5, intrinsic)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if opt_type == "CE":
        px = spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    else:
        px = strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return max(0.5, px)


def load_vix_map() -> Dict[datetime, float]:
    """Load cached Shoonya India VIX (NSE|26017) 5m series into a timestamp -> VIX map."""
    vix_path = ROOT_DIR / "data" / "historical" / "INDIAVIX_5m_365d.json"
    if not vix_path.exists():
        return {}
    with open(vix_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    out: Dict[datetime, float] = {}
    for r in raw:
        ts_str = r.get("timestamp")
        c_val = r.get("close")
        if ts_str and c_val:
            try:
                out[datetime.fromisoformat(ts_str)] = float(c_val)
            except Exception:
                continue
    return out


def fetch_vix_from_shoonya(
    client: ShoonyaClient,
    start_date: date,
    end_date: date,
    chunk_days: int = 29,
) -> Dict[datetime, float]:
    """Fetch 5-minute India VIX (NSE|26017) candles from Shoonya TPSeries."""
    by_ts: Dict[datetime, Candle] = {}
    cur = start_date
    while cur <= end_date:
        ce = min(end_date, cur + timedelta(days=chunk_days))
        st_dt = IST.localize(datetime.combine(cur, time(9, 0, 0)))
        et_dt = IST.localize(datetime.combine(ce, time(15, 35, 0)))
        rows = client.get_time_price_series(
            exchange=INDIA_VIX_EXCHANGE,
            token=INDIA_VIX_TOKEN,
            starttime=int(st_dt.timestamp()),
            endtime=int(et_dt.timestamp()),
            intrv="5",
        )
        for row in rows:
            bar = parse_shoonya_candle_row(row)
            if bar and time(9, 15) <= bar.timestamp.time() <= time(15, 30):
                by_ts[bar.timestamp] = bar
        cur = ce + timedelta(days=1)
        time_mod.sleep(0.4)

    candles = sorted(by_ts.values(), key=lambda x: x.timestamp)
    cache_dir = ROOT_DIR / "data" / "historical"
    cache_dir.mkdir(parents=True, exist_ok=True)
    vix_path = cache_dir / "INDIAVIX_5m_365d.json"
    with open(vix_path, "w", encoding="utf-8") as f:
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
    print(f"  [INDIAVIX] Cached {len(candles)} unique 5m candles -> {vix_path}", flush=True)
    return {c.timestamp: c.close for c in candles}


def _time_to_expiry_years(bar_ts: datetime, expiry_date: date) -> float:
    exp_dt = IST.localize(datetime.combine(expiry_date, time(15, 30, 0)))
    bar_ist = bar_ts.astimezone(IST) if bar_ts.tzinfo else IST.localize(bar_ts)
    sec = max(300.0, (exp_dt - bar_ist).total_seconds())
    return sec / (365.0 * 86400.0)


def _simulate_single_trade_exit(
    index_name: str,
    spec: IndexSpec,
    sig_type: str,
    strike: int,
    expiry_date: date,
    entry_candle: Candle,
    day_candles: List[Candle],
    vix_map: Dict[datetime, float],
    total_qty: int,
    p0: float,
    exit_slippage: float = 1.0,
) -> Dict[str, Any]:
    """Simulate intraday SL / Breakeven / Target-1 / 15:15 EOD using Black-Scholes driven by real Shoonya Spot & VIX."""
    iv_mult = 1.12 if index_name == "BANKNIFTY" else 1.0
    sl_price = max(1.0, p0 - spec.sl_pts)
    be_ts: Optional[datetime] = None
    t1_ts: Optional[datetime] = None
    partial_pnl = 0.0
    remaining_qty = total_qty

    exit_ts = entry_candle.timestamp
    exit_time_str = "15:15"
    exit_px = p0
    exit_reason = "EOD_SQUAREOFF"
    last_vix = vix_map.get(entry_candle.timestamp, 14.5)

    subsequent = [c for c in day_candles if c.timestamp > entry_candle.timestamp]
    for bar in subsequent:
        b_time = bar.timestamp.time()
        vix_val = vix_map.get(bar.timestamp, last_vix)
        last_vix = vix_val
        sigma = max(0.09, (vix_val / 100.0) * iv_mult)
        t_yr = _time_to_expiry_years(bar.timestamp, expiry_date)

        spot_best = bar.high if sig_type == "CE" else bar.low
        spot_worst = bar.low if sig_type == "CE" else bar.high

        approx_opt_high = bs_option_price(spot_best, strike, t_yr, sigma, sig_type)
        approx_opt_low = bs_option_price(spot_worst, strike, t_yr, sigma, sig_type)
        approx_opt_close = bs_option_price(bar.close, strike, t_yr, sigma, sig_type)

        fav_pts = approx_opt_high - p0

        if approx_opt_low <= sl_price:
            exit_px = max(0.5, sl_price - exit_slippage)
            exit_ts = bar.timestamp
            exit_time_str = bar.timestamp.strftime("%H:%M")
            exit_reason = "BREAKEVEN_SL" if be_ts is not None else "STOP_LOSS"
            break

        if t1_ts is None and fav_pts >= spec.target1_pts:
            t1_ts = bar.timestamp
            if be_ts is None:
                be_ts = bar.timestamp
            sl_price = p0
            booked_qty = total_qty // 2
            partial_pnl = booked_qty * spec.target1_pts
            remaining_qty = total_qty - booked_qty

        if be_ts is None and fav_pts >= spec.breakeven_trigger_pts:
            be_ts = bar.timestamp
            sl_price = p0

        if b_time >= time(15, 15):
            exit_px = approx_opt_close
            exit_ts = bar.timestamp
            exit_time_str = bar.timestamp.strftime("%H:%M")
            exit_reason = "EOD_SQUAREOFF"
            break
    else:
        if subsequent:
            last_bar = subsequent[-1]
            exit_ts = last_bar.timestamp
            exit_time_str = last_bar.timestamp.strftime("%H:%M")
            vix_val = vix_map.get(last_bar.timestamp, last_vix)
            sigma = max(0.09, (vix_val / 100.0) * iv_mult)
            t_yr = _time_to_expiry_years(last_bar.timestamp, expiry_date)
            exit_px = bs_option_price(last_bar.close, strike, t_yr, sigma, sig_type)

    pts_gain = exit_px - p0
    total_gross = partial_pnl + (remaining_qty * pts_gain)
    buy_turnover = total_qty * p0
    sell_turnover = total_qty * exit_px
    charges = calculate_transaction_charges(buy_turnover, sell_turnover)
    net_pnl = round(total_gross - charges, 2)

    return {
        "exit_ts": exit_ts,
        "be_ts": be_ts,
        "t1_ts": t1_ts,
        "exit_time_str": exit_time_str,
        "exit_px": round(exit_px, 2),
        "pts": round(pts_gain, 2),
        "gross": round(total_gross, 2),
        "charges": round(charges, 2),
        "net": net_pnl,
        "reason": exit_reason,
        "be": be_ts is not None,
        "partial": t1_ts is not None,
    }


def run_hybrid_live_parity_backtest(
    candles_by_index: Dict[str, List[Candle]],
    vix_map: Optional[Dict[datetime, float]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    max_trades_per_index: int = 2,
    daily_loss_limit: float = 10000.0,
) -> List[Dict[str, Any]]:
    vix_lookup = vix_map or {}
    """
    Chronological Hybrid Live-Parity Backtest (zero lookahead):
      - NIFTY     (Option A): Sequential no-overlap, max 2/day, ORB + VWAP + RSI(14) + Daily Bias.
      - BANKNIFTY (Option H): BE-Locked Pyramiding (Leg #2 allowed while Leg #1 is open ONLY if be_ts <= ts),
                              max 2/day, ORB + VWAP + RSI(14) + Daily Bias + 20-EMA + Body >= 0.40.
      - Shared daily risk guardrail: Rs 10,000.
    """
    indices = ("NIFTY", "BANKNIFTY")
    days_by_idx: Dict[str, Dict[date, List[Candle]]] = {idx: {} for idx in indices}
    all_dates_set = set()
    for idx in indices:
        for c in candles_by_index.get(idx, []):
            d = c.timestamp.date()
            days_by_idx[idx].setdefault(d, []).append(c)
            all_dates_set.add(d)

    sorted_dates = sorted(all_dates_set)
    seen_by_idx: Dict[str, List[Candle]] = {idx: [] for idx in indices}
    prev_close_by_idx: Dict[str, Optional[float]] = {idx: None for idx in indices}
    trade_dicts: List[Dict[str, Any]] = []

    for sess_date in sorted_dates:
        in_window = (
            (start_date is None or sess_date >= start_date)
            and (end_date is None or sess_date <= end_date)
        )
        if not in_window:
            for idx in indices:
                dc = days_by_idx[idx].get(sess_date, [])
                seen_by_idx[idx].extend(dc)
                if dc:
                    prev_close_by_idx[idx] = dc[-1].close
            continue

        orb_info: Dict[str, Optional[Tuple[float, float]]] = {}
        day_bars_by_idx_ts: Dict[str, Dict[datetime, Candle]] = {idx: {} for idx in indices}
        all_timestamps_set = set()

        for idx in indices:
            dc = days_by_idx[idx].get(sess_date, [])
            for c in dc:
                day_bars_by_idx_ts[idx][c.timestamp] = c
                all_timestamps_set.add(c.timestamp)

            orb_candles = [c for c in dc if time(9, 15) <= c.timestamp.time() <= time(9, 40)]
            if len(dc) < 6 or len(orb_candles) < 5:
                orb_info[idx] = None
                continue
            orh = max(c.high for c in orb_candles)
            orl = min(c.low for c in orb_candles)
            range_pct = ((orh - orl) / orl) * 100.0 if orl > 0 else 999.0
            if range_pct < 0.20 or range_pct > 0.85:
                orb_info[idx] = None
            else:
                orb_info[idx] = (orh, orl)

        is_tuesday = sess_date.weekday() == 1
        day_lots = 3 if is_tuesday else 2

        idx_cnt = {idx: 0 for idx in indices}
        idx_sl_hit = {idx: False for idx in indices}
        idx_open: Dict[str, List[Dict[str, Any]]] = {idx: [] for idx in indices}
        pending_exits: List[Tuple[datetime, str, float, str]] = []
        realized_pnl_today = 0.0
        day_halted = False

        for ts in sorted(all_timestamps_set):
            still_open: List[Tuple[datetime, str, float, str]] = []
            for ex_ts, ex_idx, ex_net, ex_reason in pending_exits:
                if ex_ts <= ts:
                    realized_pnl_today += ex_net
                    if ex_reason == "STOP_LOSS":
                        idx_sl_hit[ex_idx] = True
                    if realized_pnl_today <= -daily_loss_limit:
                        day_halted = True
                else:
                    still_open.append((ex_ts, ex_idx, ex_net, ex_reason))
            pending_exits = still_open

            for idx in indices:
                candle = day_bars_by_idx_ts[idx].get(ts)
                if candle is None:
                    continue
                seen_by_idx[idx].append(candle)
                idx_open[idx] = [o for o in idx_open[idx] if o["exit_ts"] >= ts]

                c_time = candle.timestamp.time()
                if c_time < time(10, 0) or c_time > time(13, 15):
                    continue

                if day_halted or idx_cnt[idx] >= max_trades_per_index or idx_sl_hit[idx]:
                    continue

                # Overlap / Pyramiding Rule:
                # - NIFTY (Option A): strictly sequential no-overlap.
                # - BANKNIFTY (Option H): allow Leg #2 only after Leg #1 has locked Breakeven (be_ts <= ts).
                if idx_open[idx]:
                    if idx == "NIFTY":
                        continue
                    first_leg = idx_open[idx][0]
                    if first_leg["be_ts"] is None or first_leg["be_ts"] > ts:
                        continue

                bounds = orb_info.get(idx)
                if bounds is None:
                    continue
                orh, orl = bounds

                dc = days_by_idx[idx][sess_date]
                day_so_far = [c for c in dc if c.timestamp <= candle.timestamp]
                vwap = calculate_intraday_vwap(day_so_far, session_date=sess_date)
                closes_win = [c.close for c in seen_by_idx[idx][-120:]]
                rsi = calculate_wilder_rsi(closes_win, period=14)
                prev_c = prev_close_by_idx[idx]

                trend_ce_ok = prev_c is None or candle.close >= prev_c
                trend_pe_ok = prev_c is None or candle.close <= prev_c

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

                # BANKNIFTY Option H institutional filters: 20-EMA + Candle Body >= 0.40
                if idx == "BANKNIFTY" and (ce_signal or pe_signal):
                    ema20 = calculate_ema(closes_win, period=20)
                    if ema20 is not None:
                        if ce_signal and candle.close <= ema20:
                            ce_signal = False
                        if pe_signal and candle.close >= ema20:
                            pe_signal = False
                    c_range = candle.high - candle.low
                    c_body = abs(candle.close - candle.open)
                    if c_range > 0 and (c_body / c_range) < 0.40:
                        ce_signal = False
                        pe_signal = False

                if not ce_signal and not pe_signal:
                    continue

                spec = INDEX_SPECS[idx]
                sig_type = "CE" if ce_signal else "PE"
                entry_spot = candle.close
                atm_strike = int(round(entry_spot / spec.strike_step) * spec.strike_step)
                expiry_d = get_target_expiry(idx, sess_date)
                iv_mult = 1.12 if idx == "BANKNIFTY" else 1.0
                vix_now = vix_lookup.get(candle.timestamp, 14.5)
                sigma_now = max(0.09, (vix_now / 100.0) * iv_mult)
                t_entry = _time_to_expiry_years(candle.timestamp, expiry_d)
                bs_entry = bs_option_price(entry_spot, atm_strike, t_entry, sigma_now, sig_type)
                p0 = round(bs_entry + 1.0, 2)  # Real Black-Scholes ATM premium + ₹1 entry slippage
                total_qty = spec.lot_size * day_lots

                res = _simulate_single_trade_exit(
                    index_name=idx,
                    spec=spec,
                    sig_type=sig_type,
                    strike=atm_strike,
                    expiry_date=expiry_d,
                    entry_candle=candle,
                    day_candles=dc,
                    vix_map=vix_lookup,
                    total_qty=total_qty,
                    p0=p0,
                )

                idx_cnt[idx] += 1
                idx_open[idx].append(res)
                pending_exits.append((res["exit_ts"], idx, res["net"], res["reason"]))

                trade_dicts.append(
                    {
                        "id": f"{idx}-{sess_date.strftime('%Y-%m-%d')}-{idx_cnt[idx]}",
                        "date": sess_date.strftime("%Y-%m-%d"),
                        "idx": idx,
                        "side": sig_type,
                        "strike": atm_strike,
                        "tradingsymbol": f"{idx} {atm_strike} {sig_type}",
                        "qty": total_qty,
                        "in_t": candle.timestamp.strftime("%H:%M"),
                        "out_t": res["exit_time_str"],
                        "in_px": p0,
                        "out_px": res["exit_px"],
                        "pts": res["pts"],
                        "gross": res["gross"],
                        "charges": res["charges"],
                        "net": res["net"],
                        "reason": res["reason"],
                        "be": res["be"],
                        "partial": res["partial"],
                        "mode": "BACKTEST",
                    }
                )

        for idx in indices:
            dc = days_by_idx[idx].get(sess_date, [])
            if dc:
                prev_close_by_idx[idx] = dc[-1].close

    return trade_dicts


def verify_hybrid_rules(trades: List[Dict[str, Any]], max_per_index: int = 2) -> None:
    """Verify <= max_per_index/day per index and zero overlapping trades on NIFTY."""
    by_day_idx: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for t in trades:
        by_day_idx.setdefault((t["date"], t["idx"]), []).append(t)

    for (d, idx), items in by_day_idx.items():
        if len(items) > max_per_index:
            raise AssertionError(f"{d} {idx} exceeded cap {max_per_index}: {len(items)}")
        if idx == "NIFTY":
            items_sorted = sorted(items, key=lambda x: x["in_t"])
            for a, b in zip(items_sorted, items_sorted[1:]):
                if b["in_t"] <= a["out_t"]:
                    raise AssertionError(f"NIFTY overlap on {d}: {a['in_t']}-{a['out_t']} vs {b['in_t']}-{b['out_t']}")


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


def print_report(
    title: str,
    all_trades: List[Dict[str, Any]],
    date_ranges: Dict[str, Tuple[str, str]],
) -> None:
    print(f"\n================ {title} ================")
    for idx, (d0, d1) in date_ranges.items():
        print(f"  {idx:10s} candle coverage: {d0} -> {d1}")
    print("---------------------------------------------------------------------")
    for label, subset in [
        ("NIFTY (A)", [t for t in all_trades if t["idx"] == "NIFTY"]),
        ("BNF   (H)", [t for t in all_trades if t["idx"] == "BANKNIFTY"]),
        ("COMBINED", all_trades),
    ]:
        m = compute_metrics(subset)
        print(
            f"  {label:10s} | Trades: {m['trades']:3d} (W:{m['wins']:3d}/L:{m['losses']:3d}) "
            f"| WR: {m['win_rate']:5.1f}% | PF: {m['pf']:4.2f} "
            f"| Gross: Rs {m['gross']:+10,.2f} | Charges: Rs {m['charges']:8,.2f} "
            f"| Net: Rs {m['net']:+10,.2f} | MaxDD: Rs {m['max_dd']:9,.2f} | Avg: Rs {m['avg_pnl']:+8,.2f}"
        )

    by_day: Dict[str, float] = {}
    for t in all_trades:
        by_day[t["date"]] = by_day.get(t["date"], 0.0) + t["net"]
    if by_day:
        wd = min(by_day.items(), key=lambda x: x[1])
        bd = max(by_day.items(), key=lambda x: x[1])
        print(f"  Best Day: {bd[0]} (Rs {bd[1]:+,.2f}) | Worst Day: {wd[0]} (Rs {wd[1]:+,.2f})")

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
    parser = argparse.ArgumentParser(description="Fetch 5m candles and run Hybrid Live-Parity ORB backtest.")
    parser.add_argument("--start", type=str, default="2025-11-20", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, default="2026-11-20", help="End date YYYY-MM-DD")
    parser.add_argument("--fetch", action="store_true", help="Fetch fresh 5m candles from Shoonya TPSeries")
    parser.add_argument("--force", action="store_true", help="Force re-fetch even if 365d cache is non-empty")
    parser.add_argument("--max-per-index", type=int, default=2, help="Max trades per day per index (default 2)")
    parser.add_argument("--daily-risk", type=float, default=10000.0, help="Shared daily max loss cap (Rs)")
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

    candles_by_index: Dict[str, List[Candle]] = {}
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

        candles_by_index[idx] = candles
        date_ranges[idx] = (
            (candles[0].timestamp.strftime("%Y-%m-%d"), candles[-1].timestamp.strftime("%Y-%m-%d"))
            if candles
            else ("N/A", "N/A")
        )

    vix_map = load_vix_map()
    if (not vix_map or args.force) and client is not None:
        vix_map = fetch_vix_from_shoonya(client, max(start_d, date(2026, 3, 10)), end_d)
    print(f"  [INDIAVIX] Using {len(vix_map)} Shoonya 5m VIX bars for Black-Scholes option pricing", flush=True)

    all_trades = run_hybrid_live_parity_backtest(
        candles_by_index,
        vix_map=vix_map,
        start_date=start_d,
        end_date=end_d,
        max_trades_per_index=args.max_per_index,
        daily_loss_limit=args.daily_risk,
    )
    verify_hybrid_rules(all_trades, max_per_index=args.max_per_index)
    all_trades.sort(key=lambda t: (t["date"], t["in_t"]))
    print_report(
        f"HYBRID LIVE-PARITY BACKTEST (NIFTY=OptA, BNF=OptH, {args.max_per_index}/idx, Rs {args.daily_risk:,.0f} Risk)",
        all_trades,
        date_ranges,
    )

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

    print(f"Saved {len(all_trades)} verified Hybrid trades -> {full_path}, {min_path}, {csv_path}", flush=True)


if __name__ == "__main__":
    main()
