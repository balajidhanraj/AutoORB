"""Position sizing, SL-LMT bracket lifecycle, partial profit, trailing SL, and MTM kill-switch."""

from __future__ import annotations

import csv
import json
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
    # Aliases for backward compatibility
    ENTERED = "OPEN_FULL"
    PARTIAL_EXIT = "OPEN_PARTIAL_TRAILING"


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
    product_type: str = "M"
    position_key: str = ""


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
        self.trades_taken_by_index: Dict[str, int] = {}
        self.sl_hit_today_by_index: Dict[str, bool] = {}
        self.realized_pnl_today: float = 0.0
        self.halted: bool = False
        self.halt_reason: Optional[str] = None
        self.custom_lots: Dict[str, int] = {}
        self.custom_product_types: Dict[str, str] = {}
        self.custom_max_daily_loss: Optional[float] = None
        self._default_max_daily_loss: float = float(settings.max_daily_loss or 10000.0)
        self.weekly_losses: Dict[Tuple[int, int], int] = {}

    def load_backtest_trades(self, limit: int = 50) -> None:
        """Load recent Master Alpha 15% backtest trades into completed trades ledger."""
        json_path = Path(__file__).resolve().parent.parent / "data" / "backtest" / "all_trades_180d.json"
        if not json_path.exists():
            return
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            recent = data[-limit:] if limit > 0 else data
            with self._lock:
                for t in recent:
                    rec = TradeRecord(
                        trade_id=str(t.get("id")),
                        date=str(t.get("date")),
                        index_name=str(t.get("idx")),
                        tradingsymbol=str(t.get("tradingsymbol") or f"{t.get('idx')} {t.get('strike')} {t.get('side')}"),
                        signal_type=str(t.get("side")),
                        quantity=int(t.get("qty", 0)),
                        entry_time=str(t.get("in_t")),
                        entry_price=float(t.get("in_px", 0.0)),
                        exit_time=str(t.get("out_t")),
                        exit_price=float(t.get("out_px", 0.0)),
                        gross_pnl=float(t.get("gross", 0.0)),
                        charges_est=float(t.get("charges", 0.0)),
                        net_pnl=float(t.get("net", 0.0)),
                        exit_reason=str(t.get("reason", "BACKTEST_EXIT")),
                        mode=str(t.get("mode", "BACKTEST")),
                    )
                    if not any(x.trade_id == rec.trade_id for x in self.completed_trades):
                        self.completed_trades.append(rec)
        except Exception as e:
            logger.warning("Could not load backtest trades into risk manager: %s", e)

    def set_index_lots(self, index_name: str, lots: int) -> None:
        self.custom_lots[index_name] = max(1, int(lots))
        self.get_dynamic_max_daily_loss()

    def set_index_product_type(self, index_name: str, product_type: str) -> None:
        norm = product_type.strip().upper()
        self.custom_product_types[index_name] = "M" if norm in {"M", "NRML", "NORMAL"} else "I"

    def get_dynamic_max_daily_loss(self, now_dt: Optional[datetime] = None) -> float:
        """Return user-configured Daily Max Loss Guardrail if set, else dynamic lot-based limit:
        - 1 lot base = ₹2,500
        - 2 lots base (Mon-Fri) = ₹5,000
        - 4 lots boosted (Tuesday / custom) = ₹10,000
        Formula: max(active_lots) * 2,500.0
        """
        if self.custom_max_daily_loss is not None and self.custom_max_daily_loss > 0:
            self.settings.max_daily_loss = float(self.custom_max_daily_loss)
            return float(self.custom_max_daily_loss)
        now = now_dt or datetime.now(IST)
        nf_lots = self.custom_lots.get("NIFTY") or self.calculate_dynamic_lots("NIFTY", trade_time=now)
        bnf_lots = self.custom_lots.get("BANKNIFTY") or self.calculate_dynamic_lots("BANKNIFTY", trade_time=now)
        active_lots = max(1, int(nf_lots), int(bnf_lots))
        base_limit = float(self.settings.max_daily_loss or 10000.0)
        limit = max(base_limit, float(active_lots * 2500.0))
        self.settings.max_daily_loss = limit
        return limit

    def set_max_daily_loss(self, amount: Optional[float]) -> Tuple[bool, str]:
        """Allow user to set a custom Daily Max Loss Guardrail (₹) or pass None/0 to restore AUTO dynamic sizing."""
        with self._lock:
            if amount is None or float(amount) <= 0:
                self.custom_max_daily_loss = None
                self.settings.max_daily_loss = self._default_max_daily_loss
                auto_limit = self.get_dynamic_max_daily_loss()
                msg = f"Daily Max Loss Guardrail restored to AUTO (₹{auto_limit:,.0f})."
                logger.info(msg)
                return True, msg
            val = round(float(amount), 2)
            if val < 500.0:
                return False, "Daily Max Loss Guardrail must be at least ₹500."
            self.custom_max_daily_loss = val
            self.settings.max_daily_loss = val
            msg = f"Daily Max Loss Guardrail updated to ₹{val:,.0f} (CUSTOM)."
            logger.info(msg)
            return True, msg

    def reset_halt_and_limits(self, reset_type: str = "all", now_dt: Optional[datetime] = None) -> Tuple[bool, str]:
        """Reset system halts and/or daily order loss, weekly loss circuit breaker, and daily MTM loss."""
        norm = (reset_type or "all").strip().lower()
        now = now_dt or datetime.now(IST)
        cal_week = (now.year, now.isocalendar()[1])

        with self._lock:
            if norm in {"all", "full"}:
                self.halted = False
                self.halt_reason = None
                self.trades_taken_today = 0
                self.trades_taken_by_index.clear()
                self.sl_hit_today_by_index.clear()
                self.realized_pnl_today = 0.0
                self.weekly_losses[cal_week] = 0
                msg = "All system halts cleared: Daily orders (0), Weekly loss guard (0), and Daily MTM loss (₹0) reset."
            elif norm in {"daily_orders", "orders", "trades"}:
                self.trades_taken_today = 0
                self.trades_taken_by_index.clear()
                self.sl_hit_today_by_index.clear()
                self.halted = False
                self.halt_reason = None
                msg = "Daily order counter reset to 0 and system halt cleared."
            elif norm in {"weekly_loss", "weekly"}:
                self.weekly_losses[cal_week] = 0
                self.halted = False
                self.halt_reason = None
                msg = f"Weekly loss circuit breaker reset to 0 for {cal_week[0]}-W{cal_week[1]:02d} and halt cleared."
            elif norm in {"daily_mtm", "mtm"}:
                self.realized_pnl_today = 0.0
                self.halted = False
                self.halt_reason = None
                msg = "Daily MTM realized loss baseline reset to ₹0 and MTM halt cleared."
            elif norm == "halt":
                self.halted = False
                self.halt_reason = None
                msg = "System halt cleared; trading engine resumed."
            else:
                return False, f"Unknown reset_type '{reset_type}'. Use all, daily_orders, weekly_loss, daily_mtm, or halt."

        logger.warning("Risk guardrail reset (%s): %s", norm, msg)
        return True, msg

    def get_available_fund(self) -> float:
        """Return available fund for sizing: live broker terminal net/cash in LIVE mode, or local capital + realized PnL in PAPER mode."""
        if self.settings.mode == "LIVE":
            try:
                snap = self.client.get_live_broker_snapshot(force=False)
                if isinstance(snap, dict):
                    net_avail = float(snap.get("net_available") or 0.0)
                    if net_avail > 0:
                        return net_avail
                    cash = float(snap.get("cash") or 0.0)
                    if cash > 0:
                        return cash
            except Exception as exc:
                logger.warning("Error fetching broker snapshot for available funds: %s", exc)
        # In PAPER mode or fallback
        return max(0.0, float(self.settings.capital) + float(self.realized_pnl_today))

    def calculate_dynamic_lots(
        self,
        index_name: str,
        option_ltp: float = 0.0,
        trade_time: Optional[datetime] = None,
    ) -> int:
        """Position sizing based on institutional day rule:
        - Tuesday (expiry alpha booster): 4 lots for both NIFTY and BANKNIFTY
        - Mon / Wed / Thu / Fri: 2 lots (BNF - 2 lots, NF - 2 lots)
        """
        spec = INDEX_SPECS.get(index_name)
        if not spec:
            return 1
        now_dt = trade_time or datetime.now(IST)
        is_tuesday = (now_dt.weekday() == 1)
        if is_tuesday:
            return 4
        return spec.default_lots

    def get_weekly_guard_status(self, check_time: Optional[datetime] = None) -> Dict[str, Any]:
        """Return current calendar week loss count and circuit breaker status."""
        now_dt = check_time or datetime.now(IST)
        cal_week = (now_dt.year, now_dt.isocalendar()[1])
        w_losses = self.weekly_losses.get(cal_week, 0)
        max_w = getattr(self.settings, "max_weekly_losses", 2)
        is_halted = (max_w > 0 and w_losses >= max_w)
        if is_halted:
            status_text = f"{w_losses}/{max_w} Losses (HALTED until Monday)"
            state = "HALTED"
        elif w_losses == 1:
            status_text = f"1/{max_w} Losses (CAUTION)"
            state = "CAUTION"
        else:
            status_text = f"{w_losses}/{max_w} Losses (ARMED)"
            state = "ARMED"
        return {
            "cal_week": f"{cal_week[0]}-W{cal_week[1]:02d}",
            "losses": w_losses,
            "max_losses": max_w,
            "is_halted": is_halted,
            "status_text": status_text,
            "state": state,
        }

    def get_enabled_indices(self) -> Dict[str, bool]:
        with self._lock:
            return {
                "NIFTY": bool(getattr(self.settings, "enable_nifty_trading", True)),
                "BANKNIFTY": bool(getattr(self.settings, "enable_banknifty_trading", True)),
            }

    def set_enabled_indices(
        self,
        nifty: Optional[bool] = None,
        banknifty: Optional[bool] = None,
    ) -> Tuple[bool, str]:
        with self._lock:
            if nifty is not None:
                self.settings.enable_nifty_trading = bool(nifty)
            if banknifty is not None:
                self.settings.enable_banknifty_trading = bool(banknifty)
            active = [k for k, v in self.get_enabled_indices().items() if v]
            label = " + ".join(active) if active else "NONE (Paused)"
            msg = f"Active strategy indices updated: {label}"
            logger.info(msg)
            return True, msg

    def can_take_new_trade(self, index_name: str, check_time: Optional[datetime] = None) -> bool:
        with self._lock:
            if self.halted:
                return False
            if index_name == "NIFTY" and not getattr(self.settings, "enable_nifty_trading", True):
                return False
            if index_name == "BANKNIFTY" and not getattr(self.settings, "enable_banknifty_trading", True):
                return False
            if self.sl_hit_today_by_index.get(index_name, False):
                return False
            allow_bnf_pyramid = (
                index_name == "BANKNIFTY"
                and getattr(self.settings, "banknifty_be_pyramiding", False)
            )
            if not allow_bnf_pyramid:
                # Default ₹1L capital-safe mode: strictly 1 active order across ALL indices at a time,
                # and global max_trades_per_day (4) across NIFTY + BANKNIFTY combined.
                if self.trades_taken_today >= self.settings.max_trades_per_day:
                    return False
                if len(self.positions) > 0:
                    return False
            else:
                idx_count = self.trades_taken_by_index.get(index_name, 0)
                if idx_count >= self.settings.max_trades_per_day:
                    return False
                active_for_idx = [p for p in self.positions.values() if p.index_name == index_name]
                if active_for_idx:
                    if len(active_for_idx) >= self.settings.max_trades_per_day:
                        return False
                    if any(
                        leg.state not in {
                            PositionState.OPEN_FULL_BREAKEVEN,
                            PositionState.OPEN_PARTIAL_TRAILING,
                        }
                        for leg in active_for_idx
                    ):
                        return False
            max_w_losses = getattr(self.settings, "max_weekly_losses", 0)
            if max_w_losses > 0:
                now_dt = check_time or datetime.now(IST)
                cal_week = (now_dt.year, now_dt.isocalendar()[1])
                w_losses = self.weekly_losses.get(cal_week, 0)
                if w_losses >= max_w_losses:
                    logger.warning(
                        "Trade rejected for %s: Weekly Circuit Breaker active (%d losses in week %s >= %d)",
                        index_name, w_losses, cal_week, max_w_losses
                    )
                    return False
            return True

    def total_mtm(self) -> float:
        with self._lock:
            unrealized = sum(p.unrealized_pnl for p in self.positions.values())
            partial_realized = sum(p.realized_pnl for p in self.positions.values())
            return round(self.realized_pnl_today + partial_realized + unrealized, 2)

    def execute_entry(
        self,
        signal: ORBSignal,
        option_ltp: Optional[float] = None,
        lots: Optional[int] = None,
    ) -> Optional[ActivePosition]:
        """Select option strike, subscribe WebSocket, and place entry LMT order."""
        with self._lock:
            if not self.can_take_new_trade(signal.index_name, check_time=signal.candle_time):
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
            if lots is not None:
                active_lots = max(1, int(lots))
            elif signal.index_name in self.custom_lots:
                active_lots = max(1, int(self.custom_lots[signal.index_name]))
            else:
                active_lots = self.calculate_dynamic_lots(
                    signal.index_name,
                    option_ltp=float(ltp),
                    trade_time=signal.candle_time,
                )
            qty = contract.lot_size * active_lots
            limit_px = round(float(ltp) + self.settings.entry_slippage_buffer, 1)

            prd_type = self.custom_product_types.get(signal.index_name) or getattr(spec, "product_type", "M")
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
                product_type=prd_type,
                position_key=self._next_position_key_locked(signal.index_name),
            )
            self.positions[pos.position_key] = pos
            logger.info(
                "Entry order submitted: %s %s qty=%d LMT=%.2f (order=%s)",
                pos.position_key,
                contract.tradingsymbol,
                qty,
                limit_px,
                order_id,
            )

            # Check immediate fill (standard in PAPER mode when limit >= LTP)
            self._check_entry_fill_locked(pos)
            return self.positions.get(pos.position_key)

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
            self.trades_taken_by_index[pos.index_name] = self.trades_taken_by_index.get(pos.index_name, 0) + 1
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
            self.positions.pop(pos.position_key or pos.index_name, None)

    def _next_position_key_locked(self, index_name: str) -> str:
        if index_name not in self.positions:
            return index_name
        leg = 2
        while f"{index_name}#{leg}" in self.positions:
            leg += 1
        return f"{index_name}#{leg}"

    def _place_initial_sl_locked(self, pos: ActivePosition) -> None:
        spec = INDEX_SPECS[pos.index_name]
        dyn_pct = float(getattr(self.settings, "dynamic_sl_pct", 0.0) or 0.0)
        sl_pts = spec.sl_pts
        if dyn_pct > 0 and pos.entry_avg_price > 0:
            cap_pts = 140.0 if pos.index_name == "BANKNIFTY" else 70.0
            sl_pts = max(spec.sl_pts, min(cap_pts, pos.entry_avg_price * dyn_pct))
            max_loss_cap = self.get_dynamic_max_daily_loss()
            if pos.initial_qty > 0 and max_loss_cap > 0:
                sl_pts = min(sl_pts, max(spec.sl_pts, (max_loss_cap * 0.90) / pos.initial_qty))
        trig_px = round(max(0.1, pos.entry_avg_price - sl_pts), 1)
        limit_px = round(max(0.05, trig_px - spec.sl_buffer_pts), 1)
        prd_type = getattr(pos, "product_type", getattr(INDEX_SPECS.get(pos.index_name), "product_type", "M"))
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
            matching = [
                p for k, p in list(self.positions.items())
                if k == index_name or p.index_name == index_name
            ]
            if not matching:
                return
            for pos in matching:
                self._process_position_tick_locked(pos, float(ltp))

    def _process_position_tick_locked(self, pos: ActivePosition, ltp: float) -> None:
        index_name = pos.index_name
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

        # 1. Target 1 (+40 Nifty / +80 BNF): reduce SL-LMT qty to 50%, exit 50% via LMT
        if gain >= spec.target1_pts and pos.state in {
            PositionState.OPEN_FULL,
            PositionState.OPEN_FULL_BREAKEVEN,
        }:
            self._execute_partial_target1_locked(pos, ltp, spec)

        # 2. Breakeven (+20 Nifty / +45 BNF): move SL to cost (Entry_Avg_Price)
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
        prd_type = getattr(pos, "product_type", getattr(INDEX_SPECS.get(pos.index_name), "product_type", "M"))
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
        self.positions.pop(pos.position_key or pos.index_name, None)
        if exit_reason in {"INITIAL_SL", "STOP_LOSS"}:
            self.sl_hit_today_by_index[pos.index_name] = True

        # Track weekly losses for Weekly Circuit Breaker
        if net_pnl < 0:
            cal_week = (now_ist.year, now_ist.isocalendar()[1])
            self.weekly_losses[cal_week] = self.weekly_losses.get(cal_week, 0) + 1
            max_w = getattr(self.settings, "max_weekly_losses", 0)
            if max_w > 0 and self.weekly_losses[cal_week] >= max_w:
                logger.warning(
                    "WEEKLY CIRCUIT BREAKER REACHED: %d losses in week %s. Halting new entries for the rest of this week.",
                    self.weekly_losses[cal_week],
                    cal_week,
                )

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
        """Monitor live MTM every 1s; liquidate and halt if MTM <= -dynamic_max_daily_loss."""
        with self._lock:
            if self.halted:
                return True
            max_limit = self.get_dynamic_max_daily_loss()
            mtm = self.total_mtm()
            if mtm > -abs(max_limit):
                return False

            reason = f"DAILY_LOSS_LIMIT_BREACHED (MTM ₹{mtm:.2f} <= -₹{max_limit:.2f})"
            logger.critical("KILL SWITCH TRIGGERED: %s", reason)
            self._liquidate_all_locked(exit_reason="KILL_SWITCH_MTM")
            self.halted = True
            self.halt_reason = reason
            if self.notifier:
                self.notifier.send_async(f"🚨 CRITICAL KILL SWITCH: {reason}")
            return True

    def _exit_position_locked(self, pos: ActivePosition, reason: str) -> None:
        """Cancel working SL order and execute market exit for remaining quantity."""
        if pos.state == PositionState.ENTERING:
            self.client.cancel_order(pos.entry_order_id)
            self.positions.pop(pos.position_key or pos.index_name, None)
            return

        if pos.sl_order_id:
            try:
                self.client.cancel_order(pos.sl_order_id)
            except Exception as e:
                logger.warning("Error cancelling SL order %s: %s", pos.sl_order_id, e)

        token_key = f"{pos.contract.exchange}|{pos.contract.token}"
        ltp = self.client.get_ltp(token_key) or pos.last_ltp or pos.entry_avg_price
        aggr_limit = round(max(0.05, float(ltp) - 5.0), 1)
        prd_type = getattr(pos, "product_type", getattr(INDEX_SPECS.get(pos.index_name), "product_type", "M"))
        resp = self.client.place_order(
            buy_or_sell="S",
            exchange=pos.contract.exchange,
            tradingsymbol=pos.contract.tradingsymbol,
            quantity=pos.remaining_qty,
            price_type="LMT",
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
        self._close_position_record_locked(pos, exit_px, reason)

    _exit_position_market = _exit_position_locked

    def exit_position(self, index_name: str, reason: str = "MANUAL_EXIT") -> bool:
        """Exit active position(s) for a specific index or position key."""
        with self._lock:
            matching = [
                p for k, p in list(self.positions.items())
                if k == index_name or p.index_name == index_name
            ]
            if not matching:
                return False
            for pos in matching:
                self._exit_position_locked(pos, reason)
            logger.info("Manually exited %d position(s) for %s (%s)", len(matching), index_name, reason)
            return True

    def check_supertrend_trailing_exit(
        self,
        index_name: str,
        spot_close: float,
        supertrend_line: Optional[float],
        supertrend_dir: int,
    ) -> bool:
        """Exit open CE if 5m spot candle closes below Supertrend(10,3) line, or PE if it closes above."""
        if not getattr(self.settings, "use_supertrend_filter", False):
            return False
        if supertrend_line is None or supertrend_dir == 0:
            return False
        with self._lock:
            matching = [
                p for k, p in list(self.positions.items())
                if (k == index_name or p.index_name == index_name)
                and p.state in {
                    PositionState.OPEN_FULL,
                    PositionState.OPEN_FULL_BREAKEVEN,
                    PositionState.OPEN_PARTIAL_TRAILING,
                }
            ]
            if not matching:
                return False
            exited_any = False
            for pos in matching:
                should_exit = (
                    (pos.signal_type == "CE" and (spot_close < supertrend_line or supertrend_dir == -1))
                    or (pos.signal_type == "PE" and (spot_close > supertrend_line or supertrend_dir == 1))
                )
                if should_exit:
                    logger.info(
                        "Supertrend trailing exit triggered for %s (%s): spot=%.2f vs ST=%.2f (dir=%d)",
                        pos.contract.tradingsymbol,
                        pos.signal_type,
                        spot_close,
                        supertrend_line,
                        supertrend_dir,
                    )
                    self._exit_position_locked(pos, "SUPERTREND_TRAIL")
                    exited_any = True
            return exited_any

    def modify_position_sl(self, index_name: str, new_sl_price: float) -> Tuple[bool, str]:
        """Manually adjust the active SL-LMT trigger price for an open position."""
        idx = index_name.strip().upper()
        if new_sl_price <= 0:
            return False, "SL trigger price must be greater than zero."

        with self._lock:
            pos = self.positions.get(idx)
            if pos is None:
                return False, f"No active position found for '{idx}'."

            spec = INDEX_SPECS[pos.index_name]
            new_trig = round(float(new_sl_price), 1)
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
                if (
                    self.settings.mode == "LIVE"
                    and (not isinstance(resp, dict) or resp.get("stat") != "Ok")
                ):
                    return False, f"Broker SL modify failed: {resp}"

            pos.sl_trigger_price = new_trig
            pos.sl_limit_price = new_lim
            msg = f"Updated SL for {pos.contract.tradingsymbol} to ₹{new_trig:.2f}"
            logger.info(msg)
            return True, msg

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
        """Mandatory square-off of open positions and pending orders at 15:15 IST."""
        with self._lock:
            for index_name, pos in list(self.positions.items()):
                self._exit_position_locked(pos, reason)

            self.halted = True
            self.halt_reason = PositionState.COMPLETED_FOR_DAY.value
            logger.info("Auto square-off completed (%s)", reason)

    def _liquidate_all_locked(self, exit_reason: str) -> None:
        for index_name, pos in list(self.positions.items()):
            self._exit_position_locked(pos, exit_reason)

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
            self.trades_taken_by_index.clear()
            self.sl_hit_today_by_index.clear()
            self.halted = False
            self.halt_reason = None
            logger.info("Risk manager trades counter reset to 0; halt cleared.")

    def reset_daily(self) -> None:
        with self._lock:
            self.positions.clear()
            self.completed_trades.clear()
            self.trades_taken_today = 0
            self.trades_taken_by_index.clear()
            self.sl_hit_today_by_index.clear()
            self.realized_pnl_today = 0.0
            self.halted = False
            self.halt_reason = None
