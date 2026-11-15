"""15-minute Opening Range Breakout (09:15-09:30) + VWAP + Wilder RSI(14) strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional, Sequence

from config.settings import INDEX_SPECS, IST, AppSettings
from engine.candle_builder import Candle
from engine.indicators import calculate_intraday_vwap, calculate_wilder_rsi
from utils.logger import get_logger

logger = get_logger("engine.strategy_orb")


@dataclass
class ORBState:
    """Intraday ORB & indicator state for a single index."""

    index_name: str
    session_date: Optional[date] = None
    orh: Optional[float] = None
    orl: Optional[float] = None
    range_pct: Optional[float] = None
    is_locked: bool = False
    no_trade_reason: Optional[str] = None
    last_spot: Optional[float] = None
    last_vwap: Optional[float] = None
    last_rsi: Optional[float] = None
    last_signal: Optional[str] = None
    last_eval_time: Optional[str] = None


@dataclass(frozen=True)
class ORBSignal:
    """Validated breakout signal ready for option selection and execution."""

    index_name: str
    signal_type: str  # "CE" or "PE"
    candle_time: datetime
    spot_close: float
    orh: float
    orl: float
    vwap: float
    rsi: float
    india_vix: float


class ORBStrategy:
    """Implements the 15-min Opening Range Breakout + VWAP + RSI + VIX rules."""

    ORB_CANDLE_TIMES = (time(9, 15, 0), time(9, 20, 0), time(9, 25, 0))

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.states: Dict[str, ORBState] = {
            name: ORBState(index_name=name) for name in INDEX_SPECS
        }

    def reset_daily(self, session_date: Optional[date] = None) -> None:
        target = session_date or datetime.now(IST).date()
        self.states = {
            name: ORBState(index_name=name, session_date=target)
            for name in INDEX_SPECS
        }

    def get_state(self, index_name: str) -> ORBState:
        return self.states[index_name]

    def lock_opening_range(
        self,
        index_name: str,
        candles: Sequence[Candle],
        session_date: Optional[date] = None,
    ) -> ORBState:
        """Compute ORH/ORL from 09:15, 09:20, 09:25 5-min candles and enforce <= 0.75% filter."""
        state = self.states[index_name]
        target_date = session_date
        if target_date is None and candles:
            ts = candles[-1].timestamp
            ts_ist = ts.astimezone(IST) if ts.tzinfo else IST.localize(ts)
            target_date = ts_ist.date()
        if target_date is None:
            target_date = datetime.now(IST).date()

        orb_candles = []
        for c in candles:
            ts_ist = c.timestamp.astimezone(IST) if c.timestamp.tzinfo else IST.localize(c.timestamp)
            if ts_ist.date() == target_date and ts_ist.time() in self.ORB_CANDLE_TIMES:
                orb_candles.append(c)

        if len(orb_candles) < 3:
            logger.warning(
                "Cannot lock ORB for %s yet: only %d/3 opening candles present",
                index_name,
                len(orb_candles),
            )
            return state

        orh = max(c.high for c in orb_candles)
        orl = min(c.low for c in orb_candles)
        range_pct = ((orh - orl) / orl) * 100.0 if orl > 0 else 999.0

        state.session_date = target_date
        state.orh = round(orh, 2)
        state.orl = round(orl, 2)
        state.range_pct = round(range_pct, 4)
        state.is_locked = True
        state.no_trade_reason = None

        if range_pct > self.settings.max_orb_range_pct:
            state.no_trade_reason = (
                f"Range {range_pct:.2f}% > {self.settings.max_orb_range_pct:.2f}%"
            )
            logger.warning(
                "%s marked NO-TRADE for %s: ORH=%.2f ORL=%.2f (%s)",
                index_name,
                target_date,
                orh,
                orl,
                state.no_trade_reason,
            )
        else:
            logger.info(
                "%s ORB locked: ORH=%.2f ORL=%.2f Range=%.2f%%",
                index_name,
                orh,
                orl,
                range_pct,
            )
        return state

    def evaluate_candle_close(
        self,
        index_name: str,
        closed_candle: Candle,
        all_candles: Sequence[Candle],
        india_vix: Optional[float] = None,
        current_time_ist: Optional[datetime] = None,
    ) -> Optional[ORBSignal]:
        """Evaluate a closed 5-min spot candle against ORH/ORL, VWAP, RSI(14), and India VIX."""
        state = self.states[index_name]
        candle_ist = (
            closed_candle.timestamp.astimezone(IST)
            if closed_candle.timestamp.tzinfo
            else IST.localize(closed_candle.timestamp)
        )
        eval_dt = current_time_ist or (candle_ist + timedelta(minutes=5))
        eval_time = eval_dt.time()

        # Ensure candle list includes the closed candle chronologically
        candles_seq = list(all_candles)
        if not candles_seq or candles_seq[-1].timestamp < closed_candle.timestamp:
            candles_seq.append(closed_candle)

        vwap = calculate_intraday_vwap(candles_seq, session_date=candle_ist.date())
        rsi = calculate_wilder_rsi([c.close for c in candles_seq], period=14)

        state.last_spot = closed_candle.close
        state.last_vwap = vwap
        state.last_rsi = rsi
        state.last_eval_time = eval_dt.strftime("%H:%M:%S")

        if not state.is_locked and eval_time >= self.settings.orb_end_time:
            self.lock_opening_range(index_name, candles_seq, session_date=candle_ist.date())

        if not state.is_locked or state.orh is None or state.orl is None:
            return None

        if state.no_trade_reason is not None:
            return None

        # Entry Window: 09:30:01 to 14:15:00 IST
        if not (time(9, 30, 1) <= eval_time <= self.settings.entry_cutoff_time):
            return None

        # Volatility Filter: Skip if India VIX < 12.0
        vix_val = float(india_vix) if india_vix is not None else 15.0
        if vix_val < self.settings.min_india_vix:
            logger.info(
                "%s signal skipped: India VIX %.2f < %.2f",
                index_name,
                vix_val,
                self.settings.min_india_vix,
            )
            return None

        close_px = closed_candle.close
        if close_px > state.orh and close_px > vwap and rsi > 60.0:
            state.last_signal = f"CE @ {close_px:.2f} (RSI {rsi:.1f})"
            return ORBSignal(
                index_name=index_name,
                signal_type="CE",
                candle_time=candle_ist,
                spot_close=close_px,
                orh=state.orh,
                orl=state.orl,
                vwap=vwap,
                rsi=rsi,
                india_vix=vix_val,
            )

        if close_px < state.orl and close_px < vwap and rsi < 40.0:
            state.last_signal = f"PE @ {close_px:.2f} (RSI {rsi:.1f})"
            return ORBSignal(
                index_name=index_name,
                signal_type="PE",
                candle_time=candle_ist,
                spot_close=close_px,
                orh=state.orh,
                orl=state.orl,
                vwap=vwap,
                rsi=rsi,
                india_vix=vix_val,
            )

        return None
