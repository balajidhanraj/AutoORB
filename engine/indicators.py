"""Technical indicators: Intraday 09:15-anchored VWAP and 14-period Wilder RSI."""

from __future__ import annotations

from datetime import date, time
from typing import Sequence

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
