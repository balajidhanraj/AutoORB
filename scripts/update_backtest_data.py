"""Fetch 5-minute historical spot & India VIX candles from Shoonya and run Dual-Engine Option 2 backtest.

Live-Parity Strategy (matches config/settings.py, engine/strategy_orb.py & engine/risk_manager.py 1-to-1):
  - Global Lock : Strict single-active-order lock (Rs 1,00,000 capital safe, Max 4 trades/day, Rs 10k daily risk cap).
  - Entry Window: 10:00 - 13:15 IST with India VIX >= 12.0 applied identically to NIFTY & BANKNIFTY.
  - NIFTY (H1)  : 15m HTF Swing Break (swing_len=2, 0.1*ATR buffer) + Supertrend(10,3) Confirm & Pure Close Trail,
                  Spot 0.5*15m ATR SL (capped at 35pt option SL), Max 2 trades/day.
  - BANKNIFTY   : 5-Gate 30m ORB (09:15-09:45, range 0.20%-0.85%) + Daily Bias + 20-EMA + Body >= 0.40 +
                  Supertrend(10,3) Confirm & Line Close Trail, Fixed 45pt SL / +45pt BE / +80pt T1 (50%), Stop-on-SL.
  - Pricing     : Black-Scholes driven by real Shoonya Spot, exact NSE Tuesday Expiry DTE, and Shoonya India VIX.
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
from engine.indicators import build_htf_15m_series, calculate_ema, calculate_supertrend
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


def _vwap_is_sloping(
    day_candles: List[Candle],
    pos_i: int,
    sess_date: date,
    direction: str,
    min_slope_pct: float = 0.02,
) -> bool:
    if pos_i < 3:
        return False
    v_now = calculate_intraday_vwap(day_candles[: pos_i + 1], session_date=sess_date)
    v_prev = calculate_intraday_vwap(day_candles[: pos_i - 2], session_date=sess_date)
    if v_now is None or v_prev is None or v_prev <= 0:
        return False
    pct = ((v_now - v_prev) / v_prev) * 100.0
    return pct >= min_slope_pct if direction == "CE" else pct <= -min_slope_pct


def _simulate_fixed_sl_trade(
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
    st_by_ts: Optional[Dict[datetime, Tuple[Optional[float], int]]] = None,
    exit_slippage: float = 1.0,
) -> Dict[str, Any]:
    """Simulate Fixed SL (25pt NF / 45pt BNF), BE (+20/+45), T1 50% (+40/+80), Supertrend(10,3) line trail, and 15:15 EOD."""
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

        o_high = bs_option_price(spot_best, strike, t_yr, sigma, sig_type)
        o_low = bs_option_price(spot_worst, strike, t_yr, sigma, sig_type)
        o_close = bs_option_price(bar.close, strike, t_yr, sigma, sig_type)

        fav_pts = o_high - p0

        if o_low <= sl_price:
            exit_px = max(0.5, sl_price - exit_slippage)
            exit_ts = bar.timestamp
            exit_time_str = bar.timestamp.strftime("%H:%M")
            exit_reason = "BREAKEVEN_SL" if be_ts is not None else "STOP_LOSS"
            break

        if t1_ts is None and fav_pts >= spec.target1_pts:
            t1_ts = bar.timestamp
            if be_ts is None:
                be_ts = bar.timestamp
            sl_price = max(sl_price, p0)
            booked_qty = total_qty // 2
            partial_pnl = booked_qty * spec.target1_pts
            remaining_qty = total_qty - booked_qty

        if be_ts is None and fav_pts >= spec.breakeven_trigger_pts:
            be_ts = bar.timestamp
            sl_price = max(sl_price, p0)

        # Supertrend(10, 3) Trailing Exit on 5-minute candle close across the Supertrend line
        if st_by_ts is not None:
            st_line, st_dir = st_by_ts.get(bar.timestamp, (None, 0))
            if st_line is not None and st_dir != 0:
                st_crossed = (
                    (sig_type == "CE" and (bar.close < st_line or st_dir == -1))
                    or (sig_type == "PE" and (bar.close > st_line or st_dir == 1))
                )
                if st_crossed:
                    exit_px = max(0.5, o_close - exit_slippage)
                    exit_ts = bar.timestamp
                    exit_time_str = bar.timestamp.strftime("%H:%M")
                    exit_reason = "SUPERTREND_TRAIL"
                    break

        if b_time >= time(15, 15):
            exit_px = o_close
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


def _simulate_nifty_htf_hybrid_trade(
    spec: IndexSpec,
    sig_type: str,
    strike: int,
    expiry_date: date,
    entry_candle: Candle,
    day_candles: List[Candle],
    vix_map: Dict[datetime, float],
    total_qty: int,
    p0: float,
    spot_sl: Optional[float],
    st_by_ts: Dict[datetime, Tuple[Optional[float], int]],
    opt_sl_cap: float = 35.0,
    exit_slippage: float = 1.0,
) -> Dict[str, Any]:
    """Simulate H1 Hybrid NIFTY trade: Spot ATR SL + 35pt Option SL Cap + Pure Supertrend(10,3) Trail + 15:15 EOD."""
    opt_sl = max(1.0, p0 - opt_sl_cap)
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
        sigma = max(0.09, vix_val / 100.0)
        t_yr = _time_to_expiry_years(bar.timestamp, expiry_date)

        spot_worst = bar.low if sig_type == "CE" else bar.high
        o_low = bs_option_price(spot_worst, strike, t_yr, sigma, sig_type)
        o_close = bs_option_price(bar.close, strike, t_yr, sigma, sig_type)

        spot_sl_hit = False
        if spot_sl is not None:
            spot_sl_hit = (bar.low <= spot_sl) if sig_type == "CE" else (bar.high >= spot_sl)
        opt_sl_hit = o_low <= opt_sl

        if spot_sl_hit and spot_sl is not None:
            px_at_spot = max(0.5, bs_option_price(spot_sl, strike, t_yr, sigma, sig_type) - exit_slippage)
            exit_px = max(0.5, opt_sl - exit_slippage) if opt_sl_hit else max(px_at_spot, opt_sl - exit_slippage)
            exit_ts = bar.timestamp
            exit_time_str = bar.timestamp.strftime("%H:%M")
            exit_reason = "SPOT_ATR_SL"
            break
        if opt_sl_hit:
            exit_px = max(0.5, opt_sl - exit_slippage)
            exit_ts = bar.timestamp
            exit_time_str = bar.timestamp.strftime("%H:%M")
            exit_reason = "STOP_LOSS"
            break

        st_line, st_dir = st_by_ts.get(bar.timestamp, (None, 0))
        if st_line is not None and st_dir != 0:
            st_crossed = (
                (sig_type == "CE" and (bar.close < st_line or st_dir == -1))
                or (sig_type == "PE" and (bar.close > st_line or st_dir == 1))
            )
            if st_crossed:
                exit_px = max(0.5, o_close - exit_slippage)
                exit_ts = bar.timestamp
                exit_time_str = bar.timestamp.strftime("%H:%M")
                exit_reason = "SUPERTREND_TRAIL"
                break

        if b_time >= time(15, 15):
            exit_px = max(0.5, o_close - exit_slippage)
            exit_ts = bar.timestamp
            exit_time_str = bar.timestamp.strftime("%H:%M")
            exit_reason = "EOD_SQUAREOFF"
            break
    else:
        if subsequent:
            last_bar = subsequent[-1]
            exit_ts = last_bar.timestamp
            exit_time_str = last_bar.timestamp.strftime("%H:%M")
            sigma = max(0.09, vix_map.get(last_bar.timestamp, last_vix) / 100.0)
            t_yr = _time_to_expiry_years(last_bar.timestamp, expiry_date)
            exit_px = max(0.5, bs_option_price(last_bar.close, strike, t_yr, sigma, sig_type) - exit_slippage)

    pts_gain = exit_px - p0
    total_gross = total_qty * pts_gain
    charges = calculate_transaction_charges(total_qty * p0, total_qty * exit_px)
    net_pnl = round(total_gross - charges, 2)
    return {
        "exit_ts": exit_ts,
        "be_ts": None,
        "t1_ts": None,
        "exit_time_str": exit_time_str,
        "exit_px": round(exit_px, 2),
        "pts": round(pts_gain, 2),
        "gross": round(total_gross, 2),
        "charges": round(charges, 2),
        "net": net_pnl,
        "reason": exit_reason,
        "be": False,
        "partial": False,
    }


def run_nf_bnf_sequential_backtest(
    candles_by_index: Dict[str, List[Candle]],
    vix_map: Optional[Dict[datetime, float]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    max_trades_per_day: int = 4,
    daily_loss_limit: float = 10000.0,
) -> List[Dict[str, Any]]:
    """Run Hybrid NIFTY (H1: 15m HTF Swing Break + VIX >= 12 + ST(10,3)) + BANKNIFTY (5-Gate ORB + ST(10,3))
    with strict GLOBAL single-active-order lock (Rs 1L capital safe)."""
    vix_lookup = vix_map or {}
    # Priority order when both trigger on the exact same candle: BANKNIFTY first, then NIFTY
    indices = ("BANKNIFTY", "NIFTY")

    days_by_idx: Dict[str, Dict[date, List[Candle]]] = {idx: {} for idx in indices}
    all_dates_set = set()
    for idx in indices:
        for c in candles_by_index.get(idx, []):
            d = c.timestamp.date()
            days_by_idx[idx].setdefault(d, []).append(c)
            all_dates_set.add(d)

    # Precompute non-repainting 15m HTF Swing levels for NIFTY
    nf_candles = candles_by_index.get("NIFTY", [])
    nf_htf_series = build_htf_15m_series(nf_candles, swing_len=2, atr_period=14)
    nf_htf_by_ts: Dict[datetime, Tuple[Optional[float], Optional[float], Optional[float], int, Optional[Tuple[Optional[float], Optional[float], Optional[float], int]], Optional[float]]] = {}
    for pos_k, c in enumerate(nf_candles):
        htf_c, htf_f, htf_a, htf_b = nf_htf_series[pos_k]
        prev_tuple = nf_htf_series[pos_k - 1] if pos_k >= 1 else None
        prev_cls = nf_candles[pos_k - 1].close if pos_k >= 1 else None
        nf_htf_by_ts[c.timestamp] = (htf_c, htf_f, htf_a, htf_b, prev_tuple, prev_cls)

    seen_by_idx: Dict[str, List[Candle]] = {idx: [] for idx in indices}
    prev_close_by_idx: Dict[str, Optional[float]] = {idx: None for idx in indices}
    trade_dicts: List[Dict[str, Any]] = []

    for sess_date in sorted(all_dates_set):
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
        bars_ts_idx: Dict[str, Dict[datetime, int]] = {idx: {} for idx in indices}
        day_st_map: Dict[str, Dict[datetime, Tuple[Optional[float], int]]] = {idx: {} for idx in indices}
        all_ts_set = set()

        for idx in indices:
            dc = days_by_idx[idx].get(sess_date, [])
            prior_tail = seen_by_idx[idx][-60:]
            for i, c in enumerate(dc):
                bars_ts_idx[idx][c.timestamp] = i
                all_ts_set.add(c.timestamp)
                day_st_map[idx][c.timestamp] = calculate_supertrend(
                    (prior_tail + dc[: i + 1])[-60:],
                    period=10,
                    multiplier=3.0,
                )
            oc = [c for c in dc if time(9, 15) <= c.timestamp.time() <= time(9, 40)]
            if len(dc) >= 6 and len(oc) >= 5:
                orh = max(c.high for c in oc)
                orl = min(c.low for c in oc)
                rp = ((orh - orl) / orl) * 100.0 if orl > 0 else 999.0
                orb_info[idx] = (orh, orl) if (0.20 <= rp <= 0.85) else None
            else:
                orb_info[idx] = None

        day_lots = 3 if sess_date.weekday() == 1 else 2
        trades_today_global = 0
        trades_by_idx = {idx: 0 for idx in indices}
        sl_hit_by_idx = {idx: False for idx in indices}
        day_halted = False
        realized_today = 0.0
        global_active_exit_ts: Optional[datetime] = None
        pending_exits: List[Tuple[datetime, str, float, str]] = []

        for ts in sorted(all_ts_set):
            still_open: List[Tuple[datetime, str, float, str]] = []
            for ex_ts, ex_idx, ex_net, ex_r in pending_exits:
                if ex_ts <= ts:
                    realized_today += ex_net
                    if ex_r == "STOP_LOSS":
                        sl_hit_by_idx[ex_idx] = True
                    if realized_today <= -daily_loss_limit:
                        day_halted = True
                else:
                    still_open.append((ex_ts, ex_idx, ex_net, ex_r))
            pending_exits = still_open

            if global_active_exit_ts is not None and ts > global_active_exit_ts:
                global_active_exit_ts = None

            # Always update seen candle history for both indices first
            for idx in indices:
                pos_i = bars_ts_idx[idx].get(ts)
                if pos_i is not None:
                    seen_by_idx[idx].append(days_by_idx[idx][sess_date][pos_i])

            if day_halted or trades_today_global >= max_trades_per_day:
                continue
            if not (time(10, 0) <= ts.time() <= time(13, 15)):
                continue
            # Strict GLOBAL single-active-order lock: hold ALL new signals while ANY order is running
            if global_active_exit_ts is not None:
                continue

            for idx in indices:
                if global_active_exit_ts is not None:
                    break
                pos_i = bars_ts_idx[idx].get(ts)
                if pos_i is None:
                    continue
                dc = days_by_idx[idx][sess_date]
                candle = dc[pos_i]
                spec = INDEX_SPECS[idx]

                if idx == "NIFTY":
                    # Hybrid H1 NIFTY (15m HTF Swing Break + VIX >= 12 + ST(10,3), max 2 trades/day)
                    if trades_by_idx["NIFTY"] >= 2:
                        continue
                    vix_now = vix_lookup.get(ts, 14.5)
                    if vix_now < 12.0:
                        continue
                    htf_info = nf_htf_by_ts.get(ts)
                    if not htf_info:
                        continue
                    htf_c, htf_f, htf_a, _htf_b, prev_htf_tup, prev_cls = htf_info
                    if htf_a is None or prev_cls is None or prev_htf_tup is None:
                        continue
                    st_val, st_dir = day_st_map["NIFTY"].get(ts, (None, 0))
                    if st_val is None or st_dir == 0:
                        continue
                    buf_pts = 0.1 * htf_a
                    prev_c_lv, prev_f_lv, prev_a, _ = prev_htf_tup
                    prev_buf = (0.1 * prev_a) if prev_a is not None else buf_pts
                    brk_up = (
                        htf_c is not None
                        and candle.close > (htf_c + buf_pts)
                        and prev_cls <= ((prev_c_lv if prev_c_lv is not None else htf_c) + (prev_buf if prev_c_lv is not None else buf_pts))
                    )
                    brk_dn = (
                        htf_f is not None
                        and candle.close < (htf_f - buf_pts)
                        and prev_cls >= ((prev_f_lv if prev_f_lv is not None else htf_f) - (prev_buf if prev_f_lv is not None else buf_pts))
                    )
                    ce_signal = brk_up and (st_dir == 1 and candle.close > st_val)
                    pe_signal = brk_dn and (st_dir == -1 and candle.close < st_val)
                    if not ce_signal and not pe_signal:
                        continue

                    sig_type = "CE" if ce_signal else "PE"
                    spot_sl = (
                        max(0.0, min(candle.low, htf_c) - 0.5 * htf_a)
                        if ce_signal and htf_c is not None
                        else (max(candle.high, htf_f) + 0.5 * htf_a if htf_f is not None else None)
                    )
                    atm_strike = int(round(candle.close / spec.strike_step) * spec.strike_step)
                    expiry_d = get_target_expiry(idx, sess_date)
                    sigma_now = max(0.09, vix_now / 100.0)
                    t_entry = _time_to_expiry_years(ts, expiry_d)
                    p0 = round(bs_option_price(candle.close, atm_strike, t_entry, sigma_now, sig_type) + 0.5, 2)
                    total_qty = spec.lot_size * day_lots
                    res = _simulate_nifty_htf_hybrid_trade(
                        spec=spec,
                        sig_type=sig_type,
                        strike=atm_strike,
                        expiry_date=expiry_d,
                        entry_candle=candle,
                        day_candles=dc,
                        vix_map=vix_lookup,
                        total_qty=total_qty,
                        p0=p0,
                        spot_sl=spot_sl,
                        st_by_ts=day_st_map["NIFTY"],
                        opt_sl_cap=35.0,
                    )
                else:
                    # BANKNIFTY: 5-Gate 30m ORB (10:00-13:15) + India VIX >= 12 + Supertrend(10,3)
                    if not (time(10, 0) <= ts.time() <= time(13, 15)):
                        continue
                    vix_now = vix_lookup.get(ts, 14.5)
                    if vix_now < 12.0:
                        continue
                    if sl_hit_by_idx[idx] or orb_info.get(idx) is None:
                        continue
                    orh, orl = orb_info[idx]  # type: ignore[misc]
                    vwap = calculate_intraday_vwap(dc[: pos_i + 1], session_date=sess_date)
                    closes_win = [c.close for c in seen_by_idx[idx][-120:]]
                    rsi = calculate_wilder_rsi(closes_win, period=14)
                    prev_c = prev_close_by_idx[idx]

                    ce_signal = (
                        (candle.close > orh)
                        and (vwap is None or candle.close > vwap)
                        and (rsi is not None and rsi > 60.0)
                        and (prev_c is None or candle.close >= prev_c)
                    )
                    pe_signal = (
                        (candle.close < orl)
                        and (vwap is None or candle.close < vwap)
                        and (rsi is not None and rsi < 40.0)
                        and (prev_c is None or candle.close <= prev_c)
                    )
                    if ce_signal or pe_signal:
                        st_val, st_dir = calculate_supertrend(seen_by_idx[idx][-60:], period=10, multiplier=3.0)
                        if st_val is not None and st_dir != 0:
                            if ce_signal and (st_dir != 1 or candle.close <= st_val):
                                ce_signal = False
                            if pe_signal and (st_dir != -1 or candle.close >= st_val):
                                pe_signal = False
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

                    sig_type = "CE" if ce_signal else "PE"
                    atm_strike = int(round(candle.close / spec.strike_step) * spec.strike_step)
                    expiry_d = get_target_expiry(idx, sess_date)
                    sigma_now = max(0.09, (vix_now / 100.0) * 1.12)
                    t_entry = _time_to_expiry_years(ts, expiry_d)
                    p0 = round(bs_option_price(candle.close, atm_strike, t_entry, sigma_now, sig_type) + 1.0, 2)
                    total_qty = spec.lot_size * day_lots

                    res = _simulate_fixed_sl_trade(
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
                        st_by_ts=day_st_map[idx],
                    )

                trades_today_global += 1
                trades_by_idx[idx] += 1
                global_active_exit_ts = res["exit_ts"]
                pending_exits.append((res["exit_ts"], idx, res["net"], res["reason"]))

                trade_dicts.append(
                    {
                        "id": f"{idx}-{sess_date.strftime('%Y-%m-%d')}-{trades_by_idx[idx]}",
                        "date": sess_date.strftime("%Y-%m-%d"),
                        "idx": idx,
                        "side": sig_type,
                        "strike": atm_strike,
                        "tradingsymbol": f"{idx} {atm_strike} {sig_type}",
                        "qty": total_qty,
                        "in_t": ts.strftime("%H:%M"),
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
        mm = compute_metrics(months[ym])
        print(
            f"  {ym} | Trades: {mm['trades']:2d} (W:{mm['wins']:2d}/L:{mm['losses']:2d}) "
            f"| WR: {mm['win_rate']:5.1f}% | PF: {mm['pf']:4.2f} | Net: Rs {mm['net']:+10,.2f}"
        )
    print("=====================================================================\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch 5m candles and run BANKNIFTY-ONLY 15m ORB backtest.")
    parser.add_argument("--start", type=str, default="2025-11-20", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, default="2026-11-20", help="End date YYYY-MM-DD")
    parser.add_argument("--fetch", action="store_true", help="Fetch fresh 5m candles from Shoonya TPSeries")
    parser.add_argument("--force", action="store_true", help="Force re-fetch even if 365d cache is non-empty")
    parser.add_argument("--max-trades", type=int, default=4, help="Max trades per day (default 4)")
    parser.add_argument("--daily-risk", type=float, default=10000.0, help="Daily max loss cap (Rs)")
    parser.add_argument("--bnf-only", action="store_true", help="Run BANKNIFTY only (default: NIFTY + BANKNIFTY)")
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

    active_indices = ("BANKNIFTY",) if args.bnf_only else ("NIFTY", "BANKNIFTY")
    candles_by_index: Dict[str, List[Candle]] = {}
    date_ranges: Dict[str, Tuple[str, str]] = {}
    for idx in active_indices:
        cache_365 = ROOT_DIR / "data" / "historical" / f"{idx}_5m_365d.json"
        if not args.force and cache_365.exists() and cache_365.stat().st_size > 1000:
            candles = load_candles_from_cache(idx)
            print(f"  [{idx}] Loaded {len(candles)} cached 5m candles from {cache_365}", flush=True)
        elif client is not None:
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

    all_trades = run_nf_bnf_sequential_backtest(
        candles_by_index,
        vix_map=vix_map,
        start_date=start_d,
        end_date=end_d,
        max_trades_per_day=args.max_trades,
        daily_loss_limit=args.daily_risk,
    )
    all_trades.sort(key=lambda t: (t["date"], t["in_t"]))
    # Verify strict GLOBAL zero-overlap across NIFTY + BANKNIFTY combined
    by_day: Dict[str, List[Dict[str, Any]]] = {}
    for t in all_trades:
        by_day.setdefault(t["date"], []).append(t)
    for d_str, d_items in by_day.items():
        for a, b in zip(d_items, d_items[1:]):
            if b["in_t"] <= a["out_t"]:
                raise AssertionError(
                    f"Global overlap on {d_str}: {a['idx']} {a['in_t']}-{a['out_t']} vs {b['idx']} {b['in_t']}-{b['out_t']}"
                )

    print_report(
        f"OPTION 2 DUAL-ENGINE BACKTEST (10:00-13:15, VIX>=12, ST(10,3), Max {args.max_trades}/day, Rs 1L Lock)",
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

    print(f"Saved {len(all_trades)} verified Option 2 dual-engine trades -> {full_path}, {min_path}, {csv_path}", flush=True)


if __name__ == "__main__":
    main()
