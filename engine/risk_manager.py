"""Position sizing, SL-LMT bracket lifecycle, partial profit, trailing SL, and MTM kill-switch."""

from __future__ import annotations

import csv
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from broker.instrument_mapper import InstrumentContract, InstrumentMapper
from broker.shoonya_client import ShoonyaClient
from config.settings import INDEX_SPECS, IST, AppSettings, IndexSpec
from engine.strategy_orb import ORBSignal
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger("engine.risk_manager")


class PositionState(str, Enum):
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    OPEN_FULL = "OPEN_FULL"
    OPEN_FULL_BREAKEVEN = "OPEN_FULL_BREAKEVEN"
    OPEN_PARTIAL_TRAILING = "OPEN_PARTIAL_TRAILING"
    EXITING = "EXITING"
    HALTED_DAILY_LOSS = "HALTED_DAILY_LOSS"
    COMPLETED_FOR_DAY = "COMPLETED_FOR_DAY"


@dataclass
class ActivePosition:
    """Live state of an open or entering option position."""

    trade_id: str
    index_name: str
    contract: InstrumentContract
    signal_type: str
    state: PositionState
    initial_qty: int
    remaining_qty: int
    entry_order_id: str
    entry_limit_price: float
    entry_submitted_monotonic: float
    entry_modified_once: bool = False
    entry_avg_price: float = 0.0
    entry_time: Optional[datetime] = None
    sl_order_id: Optional[str] = None
    sl_trigger_price: float = 0.0
    sl_limit_price: float = 0.0
    target1_order_id: Optional[str] = None
    last_ltp: float = 0.0
    highest_ltp: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    trail_steps_applied: int = 0


@dataclass
class TradeRecord:
    """Completed trade log row for UI display and EOD CSV export."""

    trade_id: str
    date: str
    index_name: str
    tradingsymbol: str
    signal_type: str
    quantity: int
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    gross_pnl: float
    charges_est: float
    net_pnl: float
    exit_reason: str
    mode: str


class RiskManager:
    """Manages order execution, bracket SL/Target/Trailing, MTM kill-switch, and reconciliation."""

    def __init__(
        self,
        settings: AppSettings,
        client: ShoonyaClient,
        mapper: InstrumentMapper,
        notifier: Optional[TelegramNotifier] = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.mapper = mapper
        self.notifier = notifier
        self._lock = threading.RLock()

        self.positions: Dict[str, ActivePosition] = {}
        self.completed_trades: List[TradeRecord] = []
        self.trades_taken_today: int = 0
        self.realized_pnl_today: float = 0.0
        self.halted: bool = False
        self.halt_reason: Optional[str] = None

    def can_take_new_trade(self, index_name: str) -> bool:
        with self._lock:
            if self.halted:
                return False
            if self.trades_taken_today >= self.settings.max_trades_per_day:
                return False
            if index_name in self.positions:
                return False
            return True

    def total_mtm(self) -> float:
        with self._lock:
            unrealized = sum(p.unrealized_pnl for p in self.positions.values())
            partial_realized = sum(p.realized_pnl for p in self.positions.values())
            return round(self.realized_pnl_today + partial_realized + unrealized, 2)

    def execute_entry(self, signal: ORBSignal, option_ltp: Optional[float] = None) -> Optional[ActivePosition]:
        """Select option strike, subscribe WebSocket, and place entry LMT order."""
        with self._lock:
            if not self.can_take_new_trade(signal.index_name):
                logger.info("Trade rejected for %s (halted=%s, count=%d/%d)",
                            signal.index_name, self.halted, self.trades_taken_today, self.settings.max_trades_per_day)
                return None

            spec: IndexSpec = INDEX_SPECS[signal.index_name]
            contract = self.mapper.select_option_contract(
                index_name=signal.index_name,
                spot_price=signal.spot_close,
                option_type=signal.signal_type,
                vix=signal.india_vix,
                ref_date=signal.candle_time.date(),
            )
            opt_key = f"{contract.exchange}|{contract.tradingsymbol}"
            token_key = f"{contract.exchange}|{contract.token}"
            self.client.register_symbol_token(contract.tradingsymbol, contract.exchange, contract.token)
            self.client.subscribe([token_key])

            ltp = option_ltp if option_ltp is not None else self.client.get_ltp(token_key)
            if ltp is None or ltp <= 0:
                try:
                    quote = self.client.get_quotes(contract.exchange, contract.token)
                    if isinstance(quote, dict):
                        raw_lp = quote.get("lp") or quote.get("sp1") or quote.get("bp1") or quote.get("c")
                        if raw_lp is not None and float(raw_lp) > 0:
                            ltp = float(raw_lp)
                            self.client.update_ltp(token_key, ltp)
                            logger.info("Fetched live REST quote for %s: %.2f", contract.tradingsymbol, ltp)
                except Exception as exc:
                    logger.debug("REST get_quotes failed for %s: %s", contract.tradingsymbol, exc)

            if ltp is None or ltp <= 0:
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                    ltp = self.client.get_ltp(token_key)
                    if ltp is not None and ltp > 0:
                        break

            if (ltp is None or ltp <= 0) and contract.token.startswith("SIM_"):
                try:
                    logger.info("Attempting live searchscrip resolution for %s %d %s...", signal.index_name, contract.strike, signal.signal_type)
                    resp = self.client.searchscrip(contract.exchange, f"{signal.index_name} {contract.strike}")
                    values = resp.get("values", []) if isinstance(resp, dict) else []
                    for item in values:
                        if item.get("instname") in {"OPTIDX", "OPTFUT"} and (item.get("optt") or "").strip().upper() == signal.signal_type:
                            raw_stk = item.get("strike") or item.get("strprc")
                            item_tsym = str(item.get("tsym") or "").strip()
                            if raw_stk is not None and str(raw_stk).strip() != "":
                                stk_val = int(float(raw_stk))
                            else:
                                m = re.search(r'[CP](\d+)$', item_tsym)
                                stk_val = int(m.group(1)) if m else 0
                            if stk_val == contract.strike:
                                real_token = str(item.get("token") or "")
                                real_tsym = item_tsym
                                if real_token:
                                    contract.token = real_token
                                    contract.tradingsymbol = real_tsym
                                    token_key = f"{contract.exchange}|{contract.token}"
                                    self.client.subscribe([token_key])
                                    q = self.client.get_quotes(contract.exchange, real_token)
                                    raw_lp = q.get("lp") or q.get("sp1") or q.get("bp1") or q.get("c")
                                    if raw_lp is not None and float(raw_lp) > 0:
                                        ltp = float(raw_lp)
                                        self.client.update_ltp(token_key, ltp)
                                        logger.info("Resolved real live option LTP via searchscrip for %s: %.2f", real_tsym, ltp)
                                        break
                except Exception as exc:
                    logger.warning("Live searchscrip resolution failed: %s", exc)

            if ltp is None or ltp <= 0:
                if self.settings.mode == "PAPER":
                    intrinsic = max(
                        0.0,
                        signal.spot_close - contract.strike
                        if contract.option_type == "CE"
                        else contract.strike - signal.spot_close,
                    )
                    ltp = round(max(30.0, intrinsic + 80.0), 1)
                    self.client.update_ltp(token_key, ltp)
                    logger.info("Synthesized paper option LTP for %s: %.2f", contract.tradingsymbol, ltp)
                else:
                    logger.warning("No option LTP available for %s (%s); skipping entry", contract.tradingsymbol, opt_key)
                    return None

            self.client.update_ltp(token_key, float(ltp))
            qty = contract.lot_size * spec.default_lots
            limit_px = round(float(ltp) + self.settings.entry_slippage_buffer, 1)

            prd_type = getattr(spec, "product_type", "I")
            resp = self.client.place_order(
                buy_or_sell="B",
                exchange=contract.exchange,
                tradingsymbol=contract.tradingsymbol,
                quantity=qty,
                price_type="LMT",
                price=limit_px,
                product_type=prd_type,
                retention="DAY",
                discloseqty=0,
                remarks=self.settings.sebi_algo_id,
            )
            if not isinstance(resp, dict) or resp.get("stat") != "Ok":
                logger.error("Entry order placement failed for %s: %s", contract.tradingsymbol, resp)
                return None

            order_id = str(resp.get("norenordno"))
            pos = ActivePosition(
                trade_id=f"TRD-{uuid.uuid4().hex[:8].upper()}",
                index_name=signal.index_name,
                contract=contract,
                signal_type=signal.signal_type,
                state=PositionState.ENTERING,
                initial_qty=qty,
                remaining_qty=qty,
                entry_order_id=order_id,
                entry_limit_price=limit_px,
                entry_submitted_monotonic=time.monotonic(),
                last_ltp=float(ltp),
                highest_ltp=float(ltp),
            )
            self.positions[signal.index_name] = pos
            logger.info(
                "Entry order submitted: %s %s qty=%d LMT=%.2f (order=%s)",
                signal.index_name,
                contract.tradingsymbol,
                qty,
                limit_px,
                order_id,
            )

            # Check immediate fill (standard in PAPER mode when limit >= LTP)
            self._check_entry_fill_locked(pos)
            return self.positions.get(signal.index_name)

    def _check_entry_fill_locked(self, pos: ActivePosition) -> None:
        if pos.state != PositionState.ENTERING:
            return
        order_info = self.client.get_order_status(pos.entry_order_id)
        if not order_info:
            return
        status = str(order_info.get("status", "")).upper()
        if status == "COMPLETE":
            avg_px = float(order_info.get("avgprc") or pos.entry_limit_price)
            pos.entry_avg_price = round(avg_px, 2)
            pos.entry_time = datetime.now(IST)
            pos.highest_ltp = max(pos.last_ltp, pos.entry_avg_price)
            pos.state = PositionState.OPEN_FULL
            self.trades_taken_today += 1
            self._place_initial_sl_locked(pos)
            msg = (
                f"[{self.settings.mode}] ENTRY FILLED {pos.contract.tradingsymbol} "
                f"qty={pos.initial_qty} @ ₹{pos.entry_avg_price:.2f} | SL=₹{pos.sl_trigger_price:.2f}"
            )
            logger.info(msg)
            if self.notifier:
                self.notifier.send_async(msg)
        elif status in {"REJECTED", "CANCELED"}:
            logger.warning("Entry order %s ended in status %s; resetting FLAT", pos.entry_order_id, status)
            self.positions.pop(pos.index_name, None)

    def _place_initial_sl_locked(self, pos: ActivePosition) -> None:
        spec = INDEX_SPECS[pos.index_name]
        trig_px = round(max(0.1, pos.entry_avg_price - spec.sl_pts), 1)
        limit_px = round(max(0.05, trig_px - spec.sl_buffer_pts), 1)
        prd_type = getattr(INDEX_SPECS.get(pos.index_name), "product_type", "I") if pos.index_name in INDEX_SPECS else "I"
        resp = self.client.place_order(
            buy_or_sell="S",
            exchange=pos.contract.exchange,
            tradingsymbol=pos.contract.tradingsymbol,
            quantity=pos.remaining_qty,
            price_type="SL-LMT",
            price=limit_px,
            trigger_price=trig_px,
            product_type=prd_type,
            retention="DAY",
            discloseqty=0,
            remarks=self.settings.sebi_algo_id,
        )
        if isinstance(resp, dict) and resp.get("stat") == "Ok":
            pos.sl_order_id = str(resp.get("norenordno"))
            pos.sl_trigger_price = trig_px
            pos.sl_limit_price = limit_px
            logger.info(
                "Initial SL-LMT placed for %s: order=%s trig=%.2f prc=%.2f",
                pos.contract.tradingsymbol,
                pos.sl_order_id,
                trig_px,
                limit_px,
            )

    def check_pending_entries(self, now_monotonic: Optional[float] = None) -> None:
        """Modify unfilled entry once after 10s; cancel if still unfilled after 20s."""
        now_m = now_monotonic if now_monotonic is not None else time.monotonic()
        with self._lock:
            for index_name, pos in list(self.positions.items()):
                if pos.state != PositionState.ENTERING:
                    continue
                self._check_entry_fill_locked(pos)
                if index_name not in self.positions or pos.state != PositionState.ENTERING:
                    continue

                elapsed = now_m - pos.entry_submitted_monotonic
                if elapsed >= self.settings.entry_cancel_timeout_sec:
                    logger.warning("Entry order %s unfilled after %.1fs; canceling", pos.entry_order_id, elapsed)
                    self.client.cancel_order(pos.entry_order_id)
                    self.positions.pop(index_name, None)
                elif elapsed >= self.settings.entry_modify_timeout_sec and not pos.entry_modified_once:
                    token_key = f"{pos.contract.exchange}|{pos.contract.token}"
                    latest_ltp = self.client.get_ltp(token_key) or pos.last_ltp
                    new_limit = round(float(latest_ltp) + self.settings.entry_slippage_buffer, 1)
                    resp = self.client.modify_order(
                        orderno=pos.entry_order_id,
                        exchange=pos.contract.exchange,
                        tradingsymbol=pos.contract.tradingsymbol,
                        newquantity=pos.initial_qty,
                        newprice_type="LMT",
                        newprice=new_limit,
                    )
                    pos.entry_modified_once = True
                    if isinstance(resp, dict) and resp.get("stat") == "Ok":
                        pos.entry_limit_price = new_limit
                        logger.info("Modified unfilled entry order %s once to LMT=%.2f", pos.entry_order_id, new_limit)

    def on_option_tick(self, index_name: str, ltp: float) -> None:
        """Update live MTM, detect SL fills, and drive Breakeven -> Target 1 -> Step Trailing."""
        with self._lock:
            pos = self.positions.get(index_name)
            if pos is None:
                return

            pos.last_ltp = float(ltp)
            pos.highest_ltp = max(pos.highest_ltp, float(ltp))

            if pos.state == PositionState.ENTERING:
                self._check_entry_fill_locked(pos)
                return

            # Check if SL-LMT order executed
            if pos.sl_order_id:
                sl_status = self.client.get_order_status(pos.sl_order_id)
                if sl_status and str(sl_status.get("status", "")).upper() == "COMPLETE":
                    exit_px = float(sl_status.get("avgprc") or pos.sl_trigger_price)
                    reason = "TRAILING_SL" if pos.state == PositionState.OPEN_PARTIAL_TRAILING else (
                        "BREAKEVEN_SL" if pos.state == PositionState.OPEN_FULL_BREAKEVEN else "INITIAL_SL"
                    )
                    self._close_position_record_locked(pos, exit_px, reason)
                    return

            if pos.state not in {
                PositionState.OPEN_FULL,
                PositionState.OPEN_FULL_BREAKEVEN,
                PositionState.OPEN_PARTIAL_TRAILING,
            }:
                return

            pos.unrealized_pnl = round((ltp - pos.entry_avg_price) * pos.remaining_qty, 2)
            spec = INDEX_SPECS[index_name]
            gain = ltp - pos.entry_avg_price

            # 1. Target 1 (+30 Nifty / +65 BNF): reduce SL-LMT qty to 50%, exit 50% via LMT
            if gain >= spec.target1_pts and pos.state in {
                PositionState.OPEN_FULL,
                PositionState.OPEN_FULL_BREAKEVEN,
            }:
                self._execute_partial_target1_locked(pos, ltp, spec)

            # 2. Breakeven (+25 Nifty / +55 BNF): move SL to cost (Entry_Avg_Price)
            elif gain >= spec.breakeven_trigger_pts and pos.state == PositionState.OPEN_FULL:
                self._move_sl_to_breakeven_locked(pos, spec)

            # 3. Step Trailing on remaining 50% after Target 1
            if pos.state == PositionState.OPEN_PARTIAL_TRAILING:
                self._apply_step_trailing_locked(pos, ltp, spec)

    def _move_sl_to_breakeven_locked(self, pos: ActivePosition, spec: IndexSpec) -> None:
        new_trig = round(pos.entry_avg_price, 1)
        new_lim = round(max(0.05, new_trig - spec.sl_buffer_pts), 1)
        if pos.sl_order_id and new_trig > pos.sl_trigger_price:
            resp = self.client.modify_order(
                orderno=pos.sl_order_id,
                exchange=pos.contract.exchange,
                tradingsymbol=pos.contract.tradingsymbol,
                newquantity=pos.remaining_qty,
                newprice_type="SL-LMT",
                newprice=new_lim,
                newtrigger_price=new_trig,
            )
            if isinstance(resp, dict) and resp.get("stat") == "Ok":
                pos.sl_trigger_price = new_trig
                pos.sl_limit_price = new_lim
        pos.state = PositionState.OPEN_FULL_BREAKEVEN
        logger.info("SL moved to BREAKEVEN for %s: trig=%.2f", pos.contract.tradingsymbol, pos.sl_trigger_price)

    def _execute_partial_target1_locked(self, pos: ActivePosition, ltp: float, spec: IndexSpec) -> None:
        partial_qty = min(pos.remaining_qty, pos.contract.lot_size * max(1, spec.default_lots // 2))
        rem_qty = pos.remaining_qty - partial_qty

        # Step A: Reduce resting SL-LMT quantity FIRST (and lift SL at least to breakeven) to avoid margin lock
        new_trig = round(max(pos.sl_trigger_price, pos.entry_avg_price), 1)
        new_lim = round(max(0.05, new_trig - spec.sl_buffer_pts), 1)
        if pos.sl_order_id and rem_qty > 0:
            self.client.modify_order(
                orderno=pos.sl_order_id,
                exchange=pos.contract.exchange,
                tradingsymbol=pos.contract.tradingsymbol,
                newquantity=rem_qty,
                newprice_type="SL-LMT",
                newprice=new_lim,
                newtrigger_price=new_trig,
            )
            pos.sl_trigger_price = new_trig
            pos.sl_limit_price = new_lim

        # Step B: Book 50% Target 1 via marketable LMT sell order
        exit_lmt = round(max(0.05, ltp - 1.0), 1)
        prd_type = getattr(INDEX_SPECS.get(pos.index_name), "product_type", "I") if pos.index_name in INDEX_SPECS else "I"
        resp = self.client.place_order(
            buy_or_sell="S",
            exchange=pos.contract.exchange,
            tradingsymbol=pos.contract.tradingsymbol,
            quantity=partial_qty,
            price_type="LMT",
            price=exit_lmt,
            product_type=prd_type,
            retention="DAY",
            discloseqty=0,
            remarks=self.settings.sebi_algo_id,
        )
        fill_px = ltp
        if isinstance(resp, dict) and resp.get("stat") == "Ok":
            pos.target1_order_id = str(resp.get("norenordno"))
            status_info = self.client.get_order_status(pos.target1_order_id)
            if status_info and float(status_info.get("avgprc") or 0) > 0:
                fill_px = float(status_info["avgprc"])

        booked_pnl = round((fill_px - pos.entry_avg_price) * partial_qty, 2)
        pos.realized_pnl = round(pos.realized_pnl + booked_pnl, 2)
        pos.remaining_qty = rem_qty
        pos.unrealized_pnl = round((ltp - pos.entry_avg_price) * pos.remaining_qty, 2)
        pos.state = PositionState.OPEN_PARTIAL_TRAILING

        msg = (
            f"[{self.settings.mode}] TARGET 1 BOOKED (50%) {pos.contract.tradingsymbol} "
            f"qty={partial_qty} @ ₹{fill_px:.2f} (+₹{booked_pnl:.2f}); trailing {rem_qty} qty"
        )
        logger.info(msg)
        if self.notifier:
            self.notifier.send_async(msg)

    def _apply_step_trailing_locked(self, pos: ActivePosition, ltp: float, spec: IndexSpec) -> None:
        """Trail remaining 50% SL upward by trail_step_pts for every trail_step_pts gain above Target 1."""
        gain_above_t1 = ltp - (pos.entry_avg_price + spec.target1_pts)
        if gain_above_t1 < spec.trail_step_pts:
            return
        steps = int(math.floor(gain_above_t1 / spec.trail_step_pts))
        if steps <= pos.trail_steps_applied:
            return

        new_trig = round(pos.entry_avg_price + (steps * spec.trail_step_pts), 1)
        if new_trig <= pos.sl_trigger_price:
            return

        new_lim = round(max(0.05, new_trig - spec.sl_buffer_pts), 1)
        if pos.sl_order_id:
            resp = self.client.modify_order(
                orderno=pos.sl_order_id,
                exchange=pos.contract.exchange,
                tradingsymbol=pos.contract.tradingsymbol,
                newquantity=pos.remaining_qty,
                newprice_type="SL-LMT",
                newprice=new_lim,
                newtrigger_price=new_trig,
            )
            if isinstance(resp, dict) and resp.get("stat") == "Ok":
                pos.sl_trigger_price = new_trig
                pos.sl_limit_price = new_lim
                pos.trail_steps_applied = steps
                logger.info(
                    "Trailed SL step %d for %s -> trig=%.2f prc=%.2f",
                    steps,
                    pos.contract.tradingsymbol,
                    new_trig,
                    new_lim,
                )

    def _close_position_record_locked(
        self, pos: ActivePosition, exit_price: float, exit_reason: str
    ) -> TradeRecord:
        rem_pnl = (exit_price - pos.entry_avg_price) * pos.remaining_qty
        gross_pnl = round(pos.realized_pnl + rem_pnl, 2)
        charges_est = self._estimate_charges(pos.entry_avg_price, exit_price, pos.initial_qty)
        net_pnl = round(gross_pnl - charges_est, 2)
        self.realized_pnl_today = round(self.realized_pnl_today + gross_pnl, 2)

        now_ist = datetime.now(IST)
        weighted_exit = round(
            pos.entry_avg_price + (gross_pnl / pos.initial_qty) if pos.initial_qty > 0 else exit_price,
            2,
        )
        record = TradeRecord(
            trade_id=pos.trade_id,
            date=now_ist.strftime("%Y-%m-%d"),
            index_name=pos.index_name,
            tradingsymbol=pos.contract.tradingsymbol,
            signal_type=pos.signal_type,
            quantity=pos.initial_qty,
            entry_time=(pos.entry_time or now_ist).strftime("%H:%M:%S"),
            entry_price=pos.entry_avg_price,
            exit_time=now_ist.strftime("%H:%M:%S"),
            exit_price=weighted_exit,
            gross_pnl=gross_pnl,
            charges_est=charges_est,
            net_pnl=net_pnl,
            exit_reason=exit_reason,
            mode=self.settings.mode,
        )
        self.completed_trades.append(record)
        self.positions.pop(pos.index_name, None)

        msg = (
            f"[{self.settings.mode}] CLOSED {pos.contract.tradingsymbol} ({exit_reason}) "
            f"| Gross: ₹{gross_pnl:+.2f} | Net: ₹{net_pnl:+.2f}"
        )
        logger.info(msg)
        if self.notifier:
            self.notifier.send_async(msg)
        return record

    @staticmethod
    def _estimate_charges(entry_px: float, exit_px: float, qty: int) -> float:
        turnover_sell = exit_px * qty
        turnover_total = (entry_px + exit_px) * qty
        stt = turnover_sell * 0.001
        exch_txn = turnover_total * 0.00053
        sebi_stamp = turnover_total * 0.00003
        gst = (exch_txn) * 0.18
        return round(stt + exch_txn + sebi_stamp + gst, 2)

    def check_mtm_kill_switch(self) -> bool:
        """Monitor live MTM every 1s; liquidate and halt if MTM <= -MAX_DAILY_LOSS."""
        with self._lock:
            if self.halted:
                return True
            mtm = self.total_mtm()
            if mtm > -abs(self.settings.max_daily_loss):
                return False

            reason = f"DAILY_LOSS_LIMIT_BREACHED (MTM ₹{mtm:.2f} <= -₹{self.settings.max_daily_loss:.2f})"
            logger.critical("KILL SWITCH TRIGGERED: %s", reason)
            self._liquidate_all_locked(exit_reason="KILL_SWITCH_MTM")
            self.halted = True
            self.halt_reason = reason
            if self.notifier:
                self.notifier.send_async(f"🚨 CRITICAL KILL SWITCH: {reason}")
            return True

    def exit_position(self, index_name: str, reason: str = "MANUAL_EXIT") -> bool:
        """Exit an active position for a specific index or commodity."""
        with self._lock:
            pos = self.positions.get(index_name)
            if pos is None:
                return False
            if pos.state == PositionState.ENTERING:
                self.client.cancel_order(pos.entry_order_id)
                self.positions.pop(index_name, None)
            elif pos.state in (PositionState.ENTERED, PositionState.PARTIAL_EXIT):
                self._exit_position_market(pos, reason)
            logger.info("Manually exited position for %s (%s)", index_name, reason)
            return True

    def emergency_kill_switch(self, reason: str = "MANUAL_UI_KILL_SWITCH") -> None:
        """Immediately cancel pending orders, exit all positions, and halt trading."""
        with self._lock:
            self._liquidate_all_locked(exit_reason=reason)
            self.halted = True
            self.halt_reason = reason
            logger.critical("MANUAL KILL SWITCH EXECUTED: %s", reason)
            if self.notifier:
                self.notifier.send_async(f"🛑 EMERGENCY KILL SWITCH: {reason}")

    def auto_square_off_all(self, reason: str = "EOD_1515_SQUAREOFF") -> None:
        """Mandatory square-off of open positions and pending orders."""
        with self._lock:
            # If MCX commodity is traded, only square-off NSE positions at 15:15; MCX squares off at 23:15
            has_mcx = any(getattr(s, "spot_exchange", "") == "MCX" for s in INDEX_SPECS.values())
            is_1515 = "1515" in reason
            for index_name, pos in list(self.positions.items()):
                spec = INDEX_SPECS.get(index_name)
                if is_1515 and spec and spec.spot_exchange == "MCX":
                    continue  # Keep MCX position open until evening square-off
                if pos.state == PositionState.ENTERING:
                    self.client.cancel_order(pos.entry_order_id)
                    self.positions.pop(index_name, None)
                elif pos.state in (PositionState.ENTERED, PositionState.PARTIAL_EXIT):
                    self._exit_position_market(pos, reason)

            if not has_mcx or not is_1515:
                self.halted = True
                self.halt_reason = PositionState.COMPLETED_FOR_DAY.value
            logger.info("Auto square-off completed (%s)", reason)

    def _liquidate_all_locked(self, exit_reason: str) -> None:
        for index_name, pos in list(self.positions.items()):
            if pos.state == PositionState.ENTERING:
                self.client.cancel_order(pos.entry_order_id)
                self.positions.pop(index_name, None)
                continue

            if pos.sl_order_id:
                self.client.cancel_order(pos.sl_order_id)

            token_key = f"{pos.contract.exchange}|{pos.contract.token}"
            ltp = self.client.get_ltp(token_key) or pos.last_ltp or pos.entry_avg_price
            aggr_limit = round(max(0.05, float(ltp) - 5.0), 1)
            prd_type = getattr(INDEX_SPECS.get(pos.index_name), "product_type", "I") if pos.index_name in INDEX_SPECS else "I"
            resp = self.client.place_order(
                buy_or_sell="S",
                exchange=pos.contract.exchange,
                tradingsymbol=pos.contract.tradingsymbol,
                quantity=pos.remaining_qty,
                price_type="MKT",
                price=aggr_limit,
                product_type=prd_type,
                retention="DAY",
                discloseqty=0,
                remarks=self.settings.sebi_algo_id,
            )
            exit_px = float(ltp)
            if isinstance(resp, dict) and resp.get("stat") == "Ok":
                oid = str(resp.get("norenordno"))
                status_info = self.client.get_order_status(oid)
                if status_info and float(status_info.get("avgprc") or 0) > 0:
                    exit_px = float(status_info["avgprc"])
            self._close_position_record_locked(pos, exit_px, exit_reason)

    def reconcile_positions(self) -> List[str]:
        """Reconcile broker netqty against internal positions every 10 seconds."""
        discrepancies: List[str] = []
        try:
            broker_positions = self.client.get_positions()
        except Exception as exc:
            logger.warning("Position reconciliation API call failed: %s", exc)
            return discrepancies

        broker_qty_map: Dict[str, int] = {}
        for row in broker_positions or []:
            if not isinstance(row, dict):
                continue
            tsym = str(row.get("tsym") or "").strip()
            if not tsym:
                continue
            try:
                broker_qty_map[tsym] = int(float(row.get("netqty") or 0))
            except ValueError:
                continue

        with self._lock:
            for pos in self.positions.values():
                if pos.state == PositionState.ENTERING:
                    continue
                actual = broker_qty_map.get(pos.contract.tradingsymbol, 0)
                if actual != pos.remaining_qty:
                    msg = (
                        f"Reconcile mismatch for {pos.contract.tradingsymbol}: "
                        f"internal={pos.remaining_qty}, broker={actual}"
                    )
                    logger.warning(msg)
                    discrepancies.append(msg)
        return discrepancies

    def export_daily_trades_csv(self, output_dir: Optional[str | Path] = None) -> Path:
        """Export daily completed trades to data/trades_YYYY-MM-DD.csv."""
        target_dir = Path(output_dir or self.settings.data_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        csv_path = target_dir / f"trades_{today_str}.csv"

        with self._lock:
            records = list(self.completed_trades)

        fieldnames = [
            "trade_id",
            "date",
            "index_name",
            "tradingsymbol",
            "signal_type",
            "quantity",
            "entry_time",
            "entry_price",
            "exit_time",
            "exit_price",
            "gross_pnl",
            "charges_est",
            "net_pnl",
            "exit_reason",
            "mode",
        ]
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for r in records:
                writer.writerow(r.__dict__)

        logger.info("Exported %d trades to %s", len(records), csv_path)
        return csv_path

    def reset_trades_counter(self) -> None:
        """Reset daily trades count and clear halt flag for live trigger testing."""
        with self._lock:
            self.trades_taken_today = 0
            self.halted = False
            self.halt_reason = None
            logger.info("Risk manager trades counter reset to 0; halt cleared.")

    def reset_daily(self) -> None:
        with self._lock:
            self.positions.clear()
            self.completed_trades.clear()
            self.trades_taken_today = 0
            self.realized_pnl_today = 0.0
            self.halted = False
            self.halt_reason = None
