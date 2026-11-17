"""Real-time tick-to-5m candle aggregator and historical TPSeries backfill."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from config.settings import IST
from utils.logger import get_logger

logger = get_logger("engine.candle_builder")


@dataclass
class Candle:
    """5-minute OHLCV bar aligned to IST exchange clock."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    is_closed: bool = False


def floor_to_5min(dt: datetime) -> datetime:
    """Floor an IST datetime to its 5-minute candle bucket start."""
    ist_dt = dt.astimezone(IST) if dt.tzinfo else IST.localize(dt)
    floored_minute = (ist_dt.minute // 5) * 5
    return ist_dt.replace(minute=floored_minute, second=0, microsecond=0)


class CandleBuilder:
    """Thread-safe tick-to-5-minute OHLCV builder with TPSeries historical backfill."""

    def __init__(self, symbol: str, exchange: str, token: str) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.token = token
        self._lock = threading.RLock()
        self._candles: Dict[datetime, Candle] = {}
        self._active_bucket: Optional[datetime] = None
        self._last_cum_volume: Optional[int] = None
        self._last_cum_date: Optional[str] = None
        self._close_callbacks: List[Callable[[str, Candle, List[Candle]], None]] = []

    def register_close_callback(
        self, callback: Callable[[str, Candle, List[Candle]], None]
    ) -> None:
        with self._lock:
            self._close_callbacks.append(callback)

    def get_candles(self, include_open: bool = False) -> List[Candle]:
        with self._lock:
            ordered = [self._candles[k] for k in sorted(self._candles)]
            if include_open:
                return ordered
            return [c for c in ordered if c.is_closed]

    def seed_candles(self, candles: List[Candle]) -> None:
        """Load pre-built or backfilled candles into history."""
        with self._lock:
            for c in candles:
                bucket = floor_to_5min(c.timestamp)
                existing = self._candles.get(bucket)
                if existing is not None and existing.is_closed and not c.is_closed:
                    continue
                self._candles[bucket] = Candle(
                    timestamp=bucket,
                    open=float(c.open),
                    high=float(c.high),
                    low=float(c.low),
                    close=float(c.close),
                    volume=int(c.volume),
                    is_closed=bool(c.is_closed),
                )

    def on_tick(
        self,
        price: float,
        tick_time: Optional[datetime] = None,
        volume: int = 0,
        is_cumulative_volume: bool = False,
    ) -> Optional[Candle]:
        """Process a live price tick and return a newly closed candle if a 5m boundary crossed."""
        now_ist = tick_time or datetime.now(IST)
        bucket = floor_to_5min(now_ist)
        closed_candle: Optional[Candle] = None

        with self._lock:
            day_str = bucket.strftime("%Y-%m-%d")
            if is_cumulative_volume:
                if self._last_cum_date != day_str or self._last_cum_volume is None:
                    vol_delta = max(0, int(volume))
                else:
                    vol_delta = max(0, int(volume) - self._last_cum_volume)
                self._last_cum_date = day_str
                self._last_cum_volume = int(volume)
            else:
                vol_delta = max(0, int(volume))

            if self._active_bucket is not None and bucket > self._active_bucket:
                prev = self._candles.get(self._active_bucket)
                if prev is not None and not prev.is_closed:
                    prev.is_closed = True
                    closed_candle = prev

            current = self._candles.get(bucket)
            if current is None:
                self._candles[bucket] = Candle(
                    timestamp=bucket,
                    open=float(price),
                    high=float(price),
                    low=float(price),
                    close=float(price),
                    volume=vol_delta,
                    is_closed=False,
                )
            else:
                current.high = max(current.high, float(price))
                current.low = min(current.low, float(price))
                current.close = float(price)
                current.volume += vol_delta

            self._active_bucket = bucket
            snapshot = [self._candles[k] for k in sorted(self._candles) if self._candles[k].is_closed]
            callbacks = list(self._close_callbacks)

        if closed_candle is not None:
            for cb in callbacks:
                try:
                    cb(self.symbol, closed_candle, snapshot)
                except Exception as exc:
                    logger.error("Candle close callback error for %s: %s", self.symbol, exc)

        return closed_candle

    def flush_due_candles(self, now_ist: Optional[datetime] = None) -> Optional[Candle]:
        """Close the active 5m candle if the wall clock has moved past its end time."""
        current_dt = now_ist or datetime.now(IST)
        current_bucket = floor_to_5min(current_dt)
        closed_candle: Optional[Candle] = None

        with self._lock:
            if self._active_bucket is not None and current_bucket > self._active_bucket:
                prev = self._candles.get(self._active_bucket)
                if prev is not None and not prev.is_closed:
                    prev.is_closed = True
                    closed_candle = prev
                    self._active_bucket = None
            snapshot = [self._candles[k] for k in sorted(self._candles) if self._candles[k].is_closed]
            callbacks = list(self._close_callbacks)

        if closed_candle is not None:
            for cb in callbacks:
                try:
                    cb(self.symbol, closed_candle, snapshot)
                except Exception as exc:
                    logger.error("Flush candle callback error for %s: %s", self.symbol, exc)

        return closed_candle

    def backfill_from_api(
        self,
        api_client: Any,
        lookback_days: int = 3,
        now_ist: Optional[datetime] = None,
    ) -> int:
        """Backfill 5-minute historical candles using Shoonya get_time_price_series."""
        end_dt = now_ist or datetime.now(IST)
        start_dt = (end_dt - timedelta(days=lookback_days)).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
        start_epoch = int(start_dt.timestamp())
        end_epoch = int(end_dt.timestamp())

        try:
            series = api_client.get_time_price_series(
                exchange=self.exchange,
                token=self.token,
                starttime=start_epoch,
                endtime=end_epoch,
                intrv="5",
            )
        except Exception as exc:
            logger.warning("TPSeries backfill failed for %s: %s", self.symbol, exc)
            return 0

        if not isinstance(series, list):
            return 0

        current_bucket = floor_to_5min(end_dt)
        parsed: List[Candle] = []
        for row in series:
            if not isinstance(row, dict) or row.get("stat") == "Not_Ok":
                continue
            ts = self._parse_series_time(row)
            if ts is None:
                continue
            try:
                c = Candle(
                    timestamp=floor_to_5min(ts),
                    open=float(row.get("into") or row.get("open") or 0.0),
                    high=float(row.get("inth") or row.get("high") or 0.0),
                    low=float(row.get("intl") or row.get("low") or 0.0),
                    close=float(row.get("intc") or row.get("close") or 0.0),
                    volume=int(float(row.get("intv") or row.get("v") or 0)),
                    is_closed=floor_to_5min(ts) < current_bucket,
                )
                if c.high > 0 and c.low > 0:
                    parsed.append(c)
            except (TypeError, ValueError):
                continue

        self.seed_candles(parsed)
        logger.info("Backfilled %d 5m candles for %s", len(parsed), self.symbol)
        return len(parsed)

    @staticmethod
    def _parse_series_time(row: Dict[str, Any]) -> Optional[datetime]:
        raw_ssboe = row.get("ssboe")
        if raw_ssboe is not None:
            try:
                return datetime.fromtimestamp(int(float(raw_ssboe)), tz=IST)
            except (TypeError, ValueError):
                pass
        raw_time = str(row.get("time") or "").strip()
        for fmt in ("%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return IST.localize(datetime.strptime(raw_time, fmt))
            except ValueError:
                continue
        return None
