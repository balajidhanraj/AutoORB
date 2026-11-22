"""15-minute Opening Range Breakout (09:15-09:30) + VWAP + Wilder RSI(14) strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Dict, Optional, Sequence

from config.settings import INDEX_SPECS, IST, AppSettings
from engine.candle_builder import Candle
from engine.indicators import (
    build_htf_15m_series,
    calculate_ema,
    calculate_intraday_vwap,
    calculate_supertrend,
    calculate_wilder_rsi,
)
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
    last_supertrend: Optional[float] = None
    last_supertrend_dir: int = 0
    strategy_mode: str = "ORB_5GATE"
    htf_swing_high: Optional[float] = None
    htf_swing_low: Optional[float] = None
    htf_atr: Optional[float] = None
    htf_bias: int = 0
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
    strategy_mode: str = "ORB_5GATE"
    spot_sl_price: Optional[float] = None
    opt_sl_cap_pts: Optional[float] = None
    pure_supertrend_trail: bool = False


class ORBStrategy:
    """Implements configurable-duration Opening Range Breakout (09:15-ORB) + VWAP + RSI + VIX rules."""

    ORB_CANDLE_TIMES = (time(9, 15, 0), time(9, 20, 0), time(9, 25, 0))

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.states: Dict[str, ORBState] = {
            name: ORBState(index_name=name) for name in INDEX_SPECS
        }

    def get_orb_candle_times(self) -> Tuple[time, ...]:
        """Return all 5-minute candle start times covering the configured ORB window."""
        mins = getattr(self.settings, "orb_window_minutes", 15)
        start_t = getattr(self.settings, "orb_start_time", time(9, 15, 0))
        candle_count = max(1, mins // 5)
        start_dt = datetime.combine(date(2000, 1, 1), start_t)
        return tuple((start_dt + timedelta(minutes=i * 5)).time() for i in range(candle_count))

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
        """Compute ORH/ORL from opening 5-min candles and enforce <= 0.75% filter."""
        state = self.states[index_name]
        target_date = session_date
        if target_date is None and candles:
            ts = candles[-1].timestamp
            ts_ist = ts.astimezone(IST) if ts.tzinfo else IST.localize(ts)
            target_date = ts_ist.date()
        if target_date is None:
            target_date = datetime.now(IST).date()

        target_candle_times = self.get_orb_candle_times()
        expected_count = len(target_candle_times)
        orb_candles = []
        for c in candles:
            ts_ist = c.timestamp.astimezone(IST) if c.timestamp.tzinfo else IST.localize(c.timestamp)
            if ts_ist.date() == target_date and ts_ist.time() in target_candle_times:
                orb_candles.append(c)

        if len(orb_candles) < expected_count:
            logger.debug(
                "Cannot lock ORB for %s yet: only %d/%d opening candles present",
                index_name,
                len(orb_candles),
                expected_count,
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

        if candles:
            candles_seq = list(candles)
            state.last_spot = candles_seq[-1].close
            state.last_vwap = calculate_intraday_vwap(candles_seq, session_date=target_date)
            state.last_rsi = calculate_wilder_rsi([c.close for c in candles_seq], period=14)

        max_range_limit = self.settings.max_orb_range_pct
        min_range_limit = getattr(self.settings, "min_orb_range_pct", 0.20)
        if range_pct > max_range_limit:
            state.no_trade_reason = (
                f"Range {range_pct:.2f}% > {max_range_limit:.2f}%"
            )
            logger.warning(
                "%s marked NO-TRADE for %s: ORH=%.2f ORL=%.2f (%s)",
                index_name,
                target_date,
                orh,
                orl,
                state.no_trade_reason,
            )
        elif min_range_limit > 0 and range_pct < min_range_limit:
            state.no_trade_reason = (
                f"Range {range_pct:.2f}% < {min_range_limit:.2f}% (Chop filter)"
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
        st_period = int(getattr(self.settings, "supertrend_period", 10) or 10)
        st_mult = float(getattr(self.settings, "supertrend_multiplier", 3.0) or 3.0)
        st_val, st_dir = calculate_supertrend(candles_seq[-60:], period=st_period, multiplier=st_mult)

        state.last_spot = closed_candle.close
        state.last_vwap = vwap
        state.last_rsi = rsi
        state.last_supertrend = st_val
        state.last_supertrend_dir = st_dir
        state.last_eval_time = eval_dt.strftime("%H:%M:%S")

        if not state.is_locked and eval_time >= self.settings.orb_end_time:
            self.lock_opening_range(index_name, candles_seq, session_date=candle_ist.date())

        close_px = float(closed_candle.close)
        vix_val = float(india_vix) if india_vix is not None else 15.0

        # Dedicated H1 Hybrid NIFTY Engine:
        # 15m HTF Swing Break (swing_len=2, buf=0.1*ATR) + India VIX >= 12 + Supertrend(10,3) Confirm & Pure Trail
        if index_name == "NIFTY" and getattr(self.settings, "nifty_use_htf_swing_hybrid", False):
            return self._evaluate_nifty_htf_hybrid(
                state=state,
                closed_candle=closed_candle,
                candles_seq=candles_seq,
                candle_ist=candle_ist,
                current_time_ist=current_time_ist,
                vwap=vwap,
                rsi=rsi,
                st_val=st_val,
                st_dir=st_dir,
                vix_val=vix_val,
            )

        state.strategy_mode = "ORB_5GATE"
        if not state.is_locked or state.orh is None or state.orl is None:
            return None

        if state.no_trade_reason is not None:
            return None

        # Institutional Entry Window: 10:00 to 13:15 IST
        entry_start = getattr(self.settings, "entry_start_time", time(10, 0, 0))
        entry_end = getattr(self.settings, "entry_cutoff_time", time(13, 15, 0))
        if not (entry_start <= eval_time <= entry_end):
            return None

        # Volatility Filter: Skip if India VIX < 12.0
        if vix_val < self.settings.min_india_vix:
            logger.info(
                "%s signal skipped: India VIX %.2f < %.2f",
                index_name,
                vix_val,
                self.settings.min_india_vix,
            )
            return None

        rsi_bull = 60.0
        rsi_bear = 40.0

        ce_signal = (close_px > state.orh) and (close_px > vwap) and (rsi > rsi_bull)
        pe_signal = (close_px < state.orl) and (close_px < vwap) and (rsi < rsi_bear)

        # 0. Sloping VWAP Filter (skip flat sideways VWAP chop)
        if getattr(self.settings, "require_vwap_slope", False) and (ce_signal or pe_signal):
            day_candles = [
                c for c in candles_seq
                if (c.timestamp.astimezone(IST) if c.timestamp.tzinfo else IST.localize(c.timestamp)).date() == candle_ist.date()
            ]
            if len(day_candles) >= 4:
                prev_vwap = calculate_intraday_vwap(day_candles[:-3], session_date=candle_ist.date())
                min_slope = float(getattr(self.settings, "min_vwap_slope_pct", 0.02) or 0.02)
                if prev_vwap and prev_vwap > 0:
                    slope_pct = ((vwap - prev_vwap) / prev_vwap) * 100.0
                    if ce_signal and slope_pct < min_slope:
                        logger.info("%s CE skipped: flat VWAP slope %.4f%% < %.4f%%", index_name, slope_pct, min_slope)
                        ce_signal = False
                    if pe_signal and slope_pct > -min_slope:
                        logger.info("%s PE skipped: flat VWAP slope %.4f%% > -%.4f%%", index_name, slope_pct, min_slope)
                        pe_signal = False

        # 1. Day Open Bias Filter (CE requires Spot > Open; PE requires Spot < Open)
        if getattr(self.settings, "daily_bias_filter", False) and candles_seq:
            day_open = candles_seq[0].open
            if ce_signal and close_px <= day_open:
                logger.info("%s CE skipped: Spot %.2f <= Day Open %.2f", index_name, close_px, day_open)
                ce_signal = False
            if pe_signal and close_px >= day_open:
                logger.info("%s PE skipped: Spot %.2f >= Day Open %.2f", index_name, close_px, day_open)
                pe_signal = False

        # 2. 20 EMA Filter (Option H for BANKNIFTY; optional for NIFTY Option A)
        apply_ema_body = (index_name != "NIFTY") or getattr(self.settings, "nifty_use_ema_body_filter", False)
        if apply_ema_body and getattr(self.settings, "use_ema_filter", False):
            ema20 = calculate_ema([c.close for c in candles_seq], period=20)
            if ema20 is not None:
                if ce_signal and close_px <= ema20:
                    logger.info("%s CE skipped: Spot %.2f <= 20 EMA %.2f", index_name, close_px, ema20)
                    ce_signal = False
                if pe_signal and close_px >= ema20:
                    logger.info("%s PE skipped: Spot %.2f >= 20 EMA %.2f", index_name, close_px, ema20)
                    pe_signal = False

        # 3. Decisive Candle Body Ratio Filter (Option H for BANKNIFTY; optional for NIFTY Option A)
        min_body_ratio = getattr(self.settings, "min_candle_body_ratio", 0.0)
        if apply_ema_body and min_body_ratio > 0 and (ce_signal or pe_signal):
            c_range = closed_candle.high - closed_candle.low
            c_body = abs(closed_candle.close - closed_candle.open)
            if c_range > 0 and (c_body / c_range) < min_body_ratio:
                logger.info(
                    "%s signal skipped: Candle body ratio %.2f < %.2f",
                    index_name,
                    c_body / c_range,
                    min_body_ratio,
                )
                ce_signal = False
                pe_signal = False

        # 4. Supertrend(10, 3) Confirmation Filter (CE requires Bullish +1; PE requires Bearish -1)
        if getattr(self.settings, "use_supertrend_filter", False) and (ce_signal or pe_signal):
            st_period = int(getattr(self.settings, "supertrend_period", 10) or 10)
            st_mult = float(getattr(self.settings, "supertrend_multiplier", 3.0) or 3.0)
            st_val, st_dir = calculate_supertrend(candles_seq[-60:], period=st_period, multiplier=st_mult)
            state.last_supertrend = st_val
            state.last_supertrend_dir = st_dir
            if st_val is not None and st_dir != 0:
                if ce_signal and (st_dir != 1 or close_px <= st_val):
                    logger.info(
                        "%s CE skipped: Supertrend(%d,%.1f) dir=%d val=%.2f vs spot %.2f",
                        index_name, st_period, st_mult, st_dir, st_val, close_px,
                    )
                    ce_signal = False
                if pe_signal and (st_dir != -1 or close_px >= st_val):
                    logger.info(
                        "%s PE skipped: Supertrend(%d,%.1f) dir=%d val=%.2f vs spot %.2f",
                        index_name, st_period, st_mult, st_dir, st_val, close_px,
                    )
                    pe_signal = False

        if ce_signal:
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

        if pe_signal:
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

    def _evaluate_nifty_htf_hybrid(
        self,
        state: ORBState,
        closed_candle: Candle,
        candles_seq: Sequence[Candle],
        candle_ist: datetime,
        current_time_ist: Optional[datetime],
        vwap: float,
        rsi: float,
        st_val: Optional[float],
        st_dir: int,
        vix_val: float,
    ) -> Optional[ORBSignal]:
        """H1 Hybrid NIFTY: 15m HTF Swing Break + VIX >= 12 + Supertrend(10,3) Confirm & Pure Trail."""
        state.strategy_mode = "HTF_SWING_HYBRID"
        swing_len = int(getattr(self.settings, "nifty_htf_swing_len", 2) or 2)
        buf_mult = float(getattr(self.settings, "nifty_htf_break_buf_atr", 0.1) or 0.1)
        stop_mult = float(getattr(self.settings, "nifty_htf_stop_atr_mult", 0.5) or 0.5)
        opt_sl_cap = float(getattr(self.settings, "nifty_htf_opt_sl_cap", 35.0) or 35.0)

        series = build_htf_15m_series(candles_seq, swing_len=swing_len, atr_period=14)
        if not series:
            return None

        htf_c, htf_f, htf_a, htf_b = series[-1]
        state.htf_swing_high = round(htf_c, 2) if htf_c is not None else None
        state.htf_swing_low = round(htf_f, 2) if htf_f is not None else None
        state.htf_atr = round(htf_a, 2) if htf_a is not None else None
        state.htf_bias = int(htf_b)

        entry_start = getattr(self.settings, "nifty_htf_entry_start_time", time(9, 30, 0))
        entry_end = getattr(self.settings, "entry_cutoff_time", time(13, 15, 0))
        check_t = current_time_ist.time() if current_time_ist is not None else candle_ist.time()
        if not (entry_start <= check_t <= entry_end):
            return None

        if vix_val < self.settings.min_india_vix:
            logger.info("NIFTY Hybrid skipped: India VIX %.2f < %.2f", vix_val, self.settings.min_india_vix)
            return None

        if htf_a is None or st_val is None or st_dir == 0 or len(candles_seq) < 2 or len(series) < 2:
            return None

        prev_close = float(candles_seq[-2].close)
        prev_c, prev_f, prev_a, _ = series[-2]
        buf_pts = buf_mult * htf_a
        prev_buf = (buf_mult * prev_a) if prev_a is not None else buf_pts
        close_px = float(closed_candle.close)

        ref_c = prev_c if prev_c is not None else htf_c
        ref_bc = prev_buf if prev_c is not None else buf_pts
        brk_up = (
            htf_c is not None
            and close_px > (htf_c + buf_pts)
            and prev_close <= (ref_c + ref_bc)
        )

        ref_f = prev_f if prev_f is not None else htf_f
        ref_bf = prev_buf if prev_f is not None else buf_pts
        brk_dn = (
            htf_f is not None
            and close_px < (htf_f - buf_pts)
            and prev_close >= (ref_f - ref_bf)
        )

        ce_signal = brk_up and (st_dir == 1 and close_px > st_val)
        pe_signal = brk_dn and (st_dir == -1 and close_px < st_val)

        if ce_signal and pe_signal:
            if close_px >= float(closed_candle.open):
                pe_signal = False
            else:
                ce_signal = False

        if ce_signal and htf_c is not None:
            spot_sl = round(max(0.0, min(float(closed_candle.low), htf_c) - stop_mult * htf_a), 2)
            state.last_signal = f"CE HTF @ {close_px:.2f} (> {htf_c:.1f} | ST {st_val:.1f})"
            return ORBSignal(
                index_name="NIFTY",
                signal_type="CE",
                candle_time=candle_ist,
                spot_close=close_px,
                orh=round(htf_c, 2),
                orl=round(htf_f, 2) if htf_f is not None else (state.orl or close_px),
                vwap=vwap,
                rsi=rsi,
                india_vix=vix_val,
                strategy_mode="HTF_SWING_HYBRID",
                spot_sl_price=spot_sl,
                opt_sl_cap_pts=opt_sl_cap,
                pure_supertrend_trail=True,
            )

        if pe_signal and htf_f is not None:
            spot_sl = round(max(float(closed_candle.high), htf_f) + stop_mult * htf_a, 2)
            state.last_signal = f"PE HTF @ {close_px:.2f} (< {htf_f:.1f} | ST {st_val:.1f})"
            return ORBSignal(
                index_name="NIFTY",
                signal_type="PE",
                candle_time=candle_ist,
                spot_close=close_px,
                orh=round(htf_c, 2) if htf_c is not None else (state.orh or close_px),
                orl=round(htf_f, 2),
                vwap=vwap,
                rsi=rsi,
                india_vix=vix_val,
                strategy_mode="HTF_SWING_HYBRID",
                spot_sl_price=spot_sl,
                opt_sl_cap_pts=opt_sl_cap,
                pure_supertrend_trail=True,
            )

        return None

