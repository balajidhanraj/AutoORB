"""Technical indicators: Intraday 09:15-anchored VWAP and 14-period Wilder RSI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Dict, List, Optional, Sequence, Tuple

from config.settings import IST
from engine.candle_builder import Candle


def calculate_intraday_vwap(
    candles: Sequence[Candle],
    session_date: date | None = None,
) -> float:
    """Compute intraday VWAP anchored to 09:15 IST of the active trading day.

    Falls back to cumulative typical price (TWAP proxy) when spot index volume is 0.
    """
    if not candles:
        return 0.0

    target_date = session_date
    if target_date is None:
        last_ts = candles[-1].timestamp
        last_ist = last_ts.astimezone(IST) if last_ts.tzinfo else IST.localize(last_ts)
        target_date = last_ist.date()

    session_candles = []
    for c in candles:
        ts_ist = c.timestamp.astimezone(IST) if c.timestamp.tzinfo else IST.localize(c.timestamp)
        if ts_ist.date() == target_date and ts_ist.time() >= time(9, 15, 0):
            session_candles.append(c)

    if not session_candles:
        session_candles = list(candles)

    total_vol = sum(max(0, c.volume) for c in session_candles)
    if total_vol > 0:
        weighted_sum = sum(
            ((c.high + c.low + c.close) / 3.0) * max(0, c.volume)
            for c in session_candles
        )
        return round(weighted_sum / total_vol, 4)

    # Zero-volume Spot index fallback: cumulative typical price average
    tp_sum = sum((c.high + c.low + c.close) / 3.0 for c in session_candles)
    return round(tp_sum / len(session_candles), 4)


def calculate_wilder_rsi(closes: Sequence[float], period: int = 14) -> float:
    """Calculate Wilder's smoothed RSI (matching TradingView / standard RMA)."""
    if period <= 0:
        raise ValueError("RSI period must be positive")
    if len(closes) < period + 1:
        return 50.0

    deltas = [float(closes[i]) - float(closes[i - 1]) for i in range(1, len(closes))]
    gains = [max(0.0, d) for d in deltas]
    losses = [max(0.0, -d) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(deltas)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)


def calculate_ema(closes: Sequence[float], period: int = 20) -> Optional[float]:
    """Calculate Exponential Moving Average (EMA)."""
    if period <= 0:
        raise ValueError("EMA period must be positive")
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = float(closes[0])
    for px in closes[1:]:
        ema = (float(px) * k) + (ema * (1.0 - k))
    return round(ema, 2)


def calculate_supertrend(
    candles: Sequence[Candle],
    period: int = 10,
    multiplier: float = 3.0,
) -> Tuple[Optional[float], int]:
    """Calculate TradingView-compatible Supertrend(period=10, multiplier=3.0).

    Returns (supertrend_line_value, direction) where direction is:
      +1 = Bullish (price above Supertrend -> CE)
      -1 = Bearish (price below Supertrend -> PE)
       0 = Insufficient bars (< period + 1)
    """
    if len(candles) < period + 1:
        return None, 0

    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i].high)
        l = float(candles[i].low)
        pc = float(candles[i - 1].close)
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)

    atr = sum(trs[:period]) / period
    final_upper = 0.0
    final_lower = 0.0
    direction = 1
    st_val = 0.0

    for idx in range(period - 1, len(trs)):
        c_bar = candles[idx + 1]
        prev_bar = candles[idx]
        if idx > period - 1:
            atr = ((atr * (period - 1)) + trs[idx]) / period

        hl2 = (float(c_bar.high) + float(c_bar.low)) / 2.0
        basic_upper = hl2 + (multiplier * atr)
        basic_lower = hl2 - (multiplier * atr)

        if idx == period - 1:
            final_upper = basic_upper
            final_lower = basic_lower
            direction = 1 if float(c_bar.close) >= final_lower else -1
            st_val = final_lower if direction == 1 else final_upper
            continue

        prev_close = float(prev_bar.close)
        if basic_upper < final_upper or prev_close > final_upper:
            final_upper = basic_upper
        if basic_lower > final_lower or prev_close < final_lower:
            final_lower = basic_lower

        cur_close = float(c_bar.close)
        if direction == -1 and cur_close > final_upper:
            direction = 1
        elif direction == 1 and cur_close < final_lower:
            direction = -1

        st_val = final_lower if direction == 1 else final_upper

    return round(st_val, 2), direction


@dataclass
class _Htf15mBar:
    """Internal 15-minute bar container for non-repainting HTF swing detection."""

    open: float
    high: float
    low: float
    close: float
    atr14: Optional[float] = None
    ceil: Optional[float] = None
    floor: Optional[float] = None
    bias_b: int = 0


def build_htf_15m_series(
    candles_5m: Sequence[Candle],
    swing_len: int = 2,
    atr_period: int = 14,
) -> List[Tuple[Optional[float], Optional[float], Optional[float], int]]:
    """Resample 5m bars to 15m (IST 09:15-aligned) and return non-repainting [1] HTF levels per 5m bar.

    Each 5m bar receives (htf_swing_high, htf_swing_low, htf_atr, htf_bias) from the last
    completely closed 15-minute bar ([1] inside 15m timeframe), matching zero-repaint Pine logic.
    """
    if not candles_5m:
        return []

    htf_bars: List[_Htf15mBar] = []
    bar_to_htf_idx: List[int] = []
    seen_buckets: Dict[Tuple[date, int], int] = {}

    for c in candles_5m:
        ts_ist = c.timestamp.astimezone(IST) if c.timestamp.tzinfo else IST.localize(c.timestamp)
        minutes = ts_ist.hour * 60 + ts_ist.minute
        bucket = max(0, (minutes - (9 * 60 + 15)) // 15)
        bkey = (ts_ist.date(), bucket)
        idx = seen_buckets.get(bkey)
        if idx is None:
            idx = len(htf_bars)
            seen_buckets[bkey] = idx
            htf_bars.append(
                _Htf15mBar(
                    open=float(c.open),
                    high=float(c.high),
                    low=float(c.low),
                    close=float(c.close),
                )
            )
        else:
            hb = htf_bars[idx]
            if float(c.high) > hb.high:
                hb.high = float(c.high)
            if float(c.low) < hb.low:
                hb.low = float(c.low)
            hb.close = float(c.close)
        bar_to_htf_idx.append(idx)

    tr_list: List[float] = []
    rma: Optional[float] = None
    ceil_val: Optional[float] = None
    floor_val: Optional[float] = None
    b_val = 0
    prev_ceil: Optional[float] = None
    prev_floor: Optional[float] = None

    for j, hb in enumerate(htf_bars):
        tr = (
            hb.high - hb.low
            if j == 0
            else max(
                hb.high - hb.low,
                abs(hb.high - htf_bars[j - 1].close),
                abs(hb.low - htf_bars[j - 1].close),
            )
        )
        tr_list.append(tr)
        if j == atr_period - 1:
            rma = sum(tr_list) / float(atr_period)
        elif j >= atr_period and rma is not None:
            rma = ((rma * (atr_period - 1)) + tr) / float(atr_period)
        hb.atr14 = rma

        if j >= 2 * swing_len:
            p = j - swing_len
            cand_h = htf_bars[p].high
            is_ph = all(htf_bars[k].high <= cand_h for k in range(p - swing_len, p)) and all(
                htf_bars[k].high < cand_h for k in range(p + 1, p + swing_len + 1)
            )
            if is_ph:
                ceil_val = cand_h

            cand_l = htf_bars[p].low
            is_pl = all(htf_bars[k].low >= cand_l for k in range(p - swing_len, p)) and all(
                htf_bars[k].low > cand_l for k in range(p + 1, p + swing_len + 1)
            )
            if is_pl:
                floor_val = cand_l

        if prev_ceil is not None and hb.close > prev_ceil:
            b_val = 1
        elif prev_floor is not None and hb.close < prev_floor:
            b_val = -1

        hb.ceil = ceil_val
        hb.floor = floor_val
        hb.bias_b = b_val
        prev_ceil = ceil_val
        prev_floor = floor_val

    mapped: List[Tuple[Optional[float], Optional[float], Optional[float], int]] = []
    for h_idx in bar_to_htf_idx:
        if h_idx == 0:
            mapped.append((None, None, None, 0))
        else:
            prev_hb = htf_bars[h_idx - 1]
            mapped.append((prev_hb.ceil, prev_hb.floor, prev_hb.atr14, prev_hb.bias_b))
    return mapped


def calculate_htf_15m_swing_levels(
    candles_5m: Sequence[Candle],
    swing_len: int = 2,
    atr_period: int = 14,
) -> Tuple[Optional[float], Optional[float], Optional[float], int]:
    """Return latest non-repainting (15m_swing_high, 15m_swing_low, 15m_atr, htf_bias) for a 5m sequence."""
    series = build_htf_15m_series(candles_5m, swing_len=swing_len, atr_period=atr_period)
    if not series:
        return None, None, None, 0
    return series[-1]


