"""Entry point: IST daily scheduler, WebSocket event loop, and FastAPI Web UI server."""

from __future__ import annotations

import argparse
import threading
import time
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Dict, Optional, Tuple

import uvicorn

from broker.auth import ShoonyaAuthenticator
from broker.instrument_mapper import InstrumentMapper, round_to_strike
from broker.shoonya_client import ShoonyaClient
from config.settings import INDEX_SPECS, INDIA_VIX_KEY, IST, AppSettings, load_settings
from engine.candle_builder import Candle, CandleBuilder
from engine.risk_manager import RiskManager
from engine.strategy_orb import ORBStrategy
from ui.dashboard import create_app
from utils.logger import get_logger, get_recent_logs, setup_logging
from utils.notifier import TelegramNotifier

logger = get_logger("main")


class TradingSystemOrchestrator:
    """Coordinates auth, market data streaming, ORB strategy, risk manager, and IST schedule."""

    def __init__(self, settings: Optional[AppSettings] = None) -> None:
        self.settings = settings or load_settings()
        setup_logging(self.settings.log_dir)

        self.notifier = TelegramNotifier(
            bot_token=self.settings.telegram_bot_token,
            chat_id=self.settings.telegram_chat_id,
        )
        self.client = ShoonyaClient(self.settings)
        self.auth = ShoonyaAuthenticator(self.settings, self.client.api, notifier=self.notifier)
        self.mapper = InstrumentMapper(self.client)
        self.strategy = ORBStrategy(self.settings)
        self.risk_manager = RiskManager(
            self.settings,
            self.client,
            self.mapper,
            notifier=self.notifier,
        )
        self.risk_manager.load_backtest_trades(limit=50)

        self.candle_builders: Dict[str, CandleBuilder] = {
            name: CandleBuilder(name, spec.spot_exchange, spec.spot_token)
            for name, spec in INDEX_SPECS.items()
        }
        for name, builder in self.candle_builders.items():
            builder.register_close_callback(self._on_candle_closed)

        self.client.register_tick_listener(self._on_raw_tick)
        self.client.register_reconnect_callback(self._backfill_all_indices)
        self._reauth_lock = threading.Lock()
        self.client.register_reauth_callback(self._handle_reauth)

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._last_reconcile_monotonic: float = 0.0

        self.custom_lots: Dict[str, int] = {
            name: spec.default_lots for name, spec in INDEX_SPECS.items()
        }
        self.custom_product_types: Dict[str, str] = {
            name: spec.product_type for name, spec in INDEX_SPECS.items()
        }

        # Track daily schedule milestones by IST date
        self._milestone_done: Dict[str, date] = {}
        self.public_ip: str = "detecting..."
        threading.Thread(
            target=self._detect_public_ip,
            name="public-ip-detect",
            daemon=True,
        ).start()
        if self.settings.shoonya_user_id:
            try:
                self.auth.load_cached_token()
            except Exception as exc:
                logger.debug("Startup cached token check skipped: %s", exc)

    def _detect_public_ip(self) -> None:
        """Best-effort lookup of the host outbound IPv4 address for Shoonya IP whitelisting."""
        try:
            with urllib.request.urlopen("https://api.ipify.org?format=text", timeout=3.0) as resp:
                ip_text = resp.read().decode("utf-8", errors="ignore").strip()
                self.public_ip = ip_text or "unknown"
        except Exception:
            self.public_ip = "unknown"

    def _milestone_ready(self, key: str, today: date) -> bool:
        return self._milestone_done.get(key) != today

    def _mark_milestone(self, key: str, today: date) -> None:
        self._milestone_done[key] = today

    def _on_raw_tick(self, tick: Dict[str, Any]) -> None:
        """Route incoming Shoonya WebSocket ticks to Spot CandleBuilders or active options."""
        exch = str(tick.get("e") or "")
        tok = str(tick.get("tk") or "")
        key = f"{exch}|{tok}"
        raw_lp = tick.get("lp")
        if raw_lp is None:
            return
        try:
            ltp = float(raw_lp)
        except (TypeError, ValueError):
            return

        now_ist = datetime.now(IST)
        vol = 0
        if tick.get("v") is not None:
            try:
                vol = int(float(tick["v"]))
            except (TypeError, ValueError):
                vol = 0

        for name, spec in INDEX_SPECS.items():
            if key == spec.spot_key:
                self.candle_builders[name].on_tick(
                    price=ltp,
                    tick_time=now_ist,
                    volume=vol,
                    is_cumulative_volume=True,
                )
                return

        # Check active option positions
        for name, pos in list(self.risk_manager.positions.items()):
            opt_key = f"{pos.contract.exchange}|{pos.contract.token}"
            if key == opt_key:
                self.risk_manager.on_option_tick(name, ltp)

    def _on_candle_closed(
        self,
        index_name: str,
        closed_candle: Candle,
        all_closed_candles: list[Candle],
    ) -> None:
        """Evaluate closed 5-minute spot candles for ORB + VWAP + RSI breakout."""
        vix = self.client.get_ltp(INDIA_VIX_KEY)
        signal = self.strategy.evaluate_candle_close(
            index_name=index_name,
            closed_candle=closed_candle,
            all_candles=all_closed_candles,
            india_vix=vix,
        )
        if signal is None:
            return

        logger.info(
            "BREAKOUT SIGNAL: %s %s spot=%.2f ORH=%.2f ORL=%.2f VWAP=%.2f RSI=%.1f VIX=%.2f",
            signal.index_name,
            signal.signal_type,
            signal.spot_close,
            signal.orh,
            signal.orl,
            signal.vwap,
            signal.rsi,
            signal.india_vix,
        )
        self.risk_manager.execute_entry(signal)

    def _backfill_all_indices(self) -> None:
        now_ist = datetime.now(IST)
        for name, builder in self.candle_builders.items():
            builder.backfill_from_api(self.client, lookback_days=3, now_ist=now_ist)
        if now_ist.weekday() < 5 and now_ist.time() >= self.settings.orb_end_time:
            if not all(self.strategy.get_state(n).is_locked for n in INDEX_SPECS):
                self.run_lock_orb(now_ist)

    def run_daily_login(self, force_refresh: bool = False) -> Optional[str]:
        token = self.auth.ensure_authenticated(force_refresh=force_refresh)
        if token:
            self.client.get_live_broker_snapshot(force=True)
        return token

    def connect_shoonya_account(
        self,
        user_id: str,
        password: str,
        totp_secret: str,
        secret_key: str,
        account_id: str = "",
        api_key: str = "",
        sebi_algo_id: str = "",
    ) -> Tuple[bool, str]:
        """Configure or switch Shoonya credentials at runtime and authenticate immediately."""
        uid = user_id.strip().upper()
        pwd = password.strip()
        totp = totp_secret.strip().replace(" ", "")
        skey = secret_key.strip()
        if not all([uid, pwd, totp, skey]):
            return False, "User ID, Password, TOTP Secret, and Secret Key are required."

        with self._lock:
            if self.risk_manager.positions:
                return False, "Cannot switch Shoonya account while strategy positions are active."
            actid = (account_id.strip() or uid).upper()
            cid = api_key.strip() or f"{uid}_U"
            self.settings.shoonya_user_id = uid
            self.settings.shoonya_account_id = actid
            self.settings.shoonya_password = pwd
            self.settings.shoonya_totp_secret = totp
            self.settings.shoonya_api_key = cid
            self.settings.shoonya_secret_key = skey
            self.settings.shoonya_access_token = ""
            if sebi_algo_id.strip():
                self.settings.sebi_algo_id = sebi_algo_id.strip()
            self.client.update_credentials(self.settings)

        ok, msg = self.auth.authenticate_once()
        if ok:
            self.client.get_live_broker_snapshot(force=True)
        return ok, msg

    def add_or_update_funds(self, amount: float, action: str = "add") -> Tuple[bool, str]:
        """Add to or set strategy capital allocation and sync live broker margin."""
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return False, "Invalid fund amount."

        norm_action = action.strip().lower()
        if norm_action == "set":
            if amt < 0:
                return False, "Capital amount must be non-negative."
            self.settings.capital = round(amt, 2)
        else:
            if amt <= 0:
                return False, "Added fund amount must be greater than zero."
            self.settings.capital = round(self.settings.capital + amt, 2)

        self.client.get_live_broker_snapshot(force=True)
        msg = f"Strategy capital updated to ₹{self.settings.capital:,.2f}; broker balances synced."
        logger.info(msg)
        return True, msg

    def refresh_broker_data(self) -> Dict[str, Any]:
        """Force refresh of live Shoonya limits, orders, and positions."""
        return self.client.get_live_broker_snapshot(force=True)

    def place_manual_broker_order(
        self,
        exchange: str,
        tradingsymbol: str,
        buy_or_sell: str,
        quantity: int,
        price_type: str = "LMT",
        price: float = 0.0,
        product_type: str = "C",
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Place a live broker order directly through Shoonya API and return status."""
        if not tradingsymbol.strip():
            return False, "Trading symbol is required.", {}
        if int(quantity) <= 0:
            return False, "Order quantity must be positive.", {}

        resp = self.client.place_live_broker_order(
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            buy_or_sell=buy_or_sell,
            quantity=quantity,
            price_type=price_type,
            price=price,
            product_type=product_type,
        )
        ok = isinstance(resp, dict) and resp.get("stat") == "Ok"
        msg = (
            f"Broker order accepted: {resp.get('norenordno')}"
            if ok
            else str(resp.get("emsg") or resp)
        )
        logger.info("Manual broker order (%s %s qty=%s): ok=%s msg=%s", buy_or_sell, tradingsymbol, quantity, ok, msg)
        return ok, msg, resp

    def run_instrument_cache(self) -> int:
        return self.mapper.refresh_instrument_cache()

    def run_ws_connect_and_backfill(self) -> None:
        tokens = [spec.spot_key for spec in INDEX_SPECS.values()] + [INDIA_VIX_KEY]
        self.client.subscribe(tokens)
        if not self.client.ws_connected and (self.settings.mode == "LIVE" or self.auth.last_token_date):
            self.client.start_websocket()
            self._backfill_all_indices()

    def run_lock_orb(self, now_ist: Optional[datetime] = None) -> bool:
        dt = now_ist or datetime.now(IST)
        lines = [f"<b>ORB Locked ({dt.strftime('%Y-%m-%d')}) [{self.settings.mode}]</b>"]
        all_locked = True
        for name, builder in self.candle_builders.items():
            state = self.strategy.lock_opening_range(name, builder.get_candles(), session_date=dt.date())
            if state.is_locked:
                status = state.no_trade_reason or "ELIGIBLE"
                lines.append(
                    f"• {name}: ORH={state.orh}, ORL={state.orl}, Range={state.range_pct:.2f}% ({status})"
                )
            else:
                all_locked = False
                lines.append(f"• {name}: Pending opening candles")
        self.notifier.send_async("\n".join(lines))
        return all_locked

    def run_eod_squareoff(self) -> None:
        self.risk_manager.auto_square_off_all()

    def run_eod_report(self) -> None:
        csv_path = self.risk_manager.export_daily_trades_csv()
        mtm = self.risk_manager.total_mtm()
        count = len(self.risk_manager.completed_trades)
        msg = (
            f"<b>EOD Report [{self.settings.mode}]</b>\n"
            f"Trades: {count} | Total Gross P&L: ₹{mtm:+.2f}\n"
            f"CSV: {csv_path}"
        )
        logger.info("EOD Report: trades=%d mtm=%.2f csv=%s", count, mtm, csv_path)
        self.notifier.send_async(msg)

    def _handle_reauth(self) -> bool:
        """Triggered automatically when a 401 Unauthorized or WebSocket auth rejection occurs."""
        now_m = time.monotonic()
        if hasattr(self, "_last_reauth_time") and (now_m - self._last_reauth_time) < 15.0:
            logger.debug("Shoonya re-auth triggered within 15s cooldown; skipping duplicate")
            return True
        if not self._reauth_lock.acquire(blocking=False):
            logger.debug("Shoonya re-auth already in progress; skipping duplicate trigger")
            return False
        try:
            self._last_reauth_time = now_m
            logger.warning("Shoonya session invalid or expired; executing automatic token renewal...")
            token = self.auth.ensure_authenticated(force_refresh=True)
            if token:
                logger.info("Shoonya OAuth session successfully renewed (%s)", self.settings.shoonya_user_id)
                self.client.update_credentials(self.settings)
                susertoken = getattr(self.client.api, "susertoken", "") or getattr(self.auth.api, "susertoken", "") or token
                if hasattr(self.client.api, "injectOAuthHeader"):
                    self.client.api.injectOAuthHeader(
                        token,
                        self.settings.shoonya_user_id,
                        self.settings.shoonya_account_id,
                        susertoken=susertoken,
                    )
                if not self.client.ws_connected:
                    self.client.reconnect_websocket()
                    self._backfill_all_indices()
                return True
            else:
                logger.error("Automated Shoonya re-login failed to return a valid access token")
                return False
        except Exception as exc:
            logger.error("Exception during automated Shoonya re-authentication: %s", exc)
            return False
        finally:
            self._reauth_lock.release()

    def set_orb_anchor(self, start_time: str, minutes: int) -> Tuple[bool, str]:
        """Configure custom ORB anchor start time (e.g. '09:15', '13:00', or 'recent') and duration."""
        try:
            mins = int(minutes)
        except (ValueError, TypeError):
            return False, "Invalid ORB window duration."

        if mins not in {5, 15, 30, 45, 60}:
            return False, "Invalid ORB window duration. Choose 5, 15, 30, 45, or 60 minutes."

        now_ist = datetime.now(IST)
        start_t: dt_time
        norm_start = str(start_time).strip().lower()

        if norm_start in {"recent", "current", "auto", "last"}:
            # Find the most recently completed window before now_ist
            current_total_min = now_ist.hour * 60 + now_ist.minute
            last_end_min = (current_total_min // mins) * mins
            last_start_min = max(9 * 60 + 15, last_end_min - mins)
            start_h, start_m = divmod(last_start_min, 60)
            start_t = dt_time(start_h, start_m, 0)
        else:
            try:
                parts = norm_start.split(":")
                start_h = int(parts[0])
                start_m = int(parts[1]) if len(parts) > 1 else 0
                start_t = dt_time(start_h, start_m, 0)
            except Exception:
                return False, f"Invalid start time format: '{start_time}'. Use 'HH:MM' (e.g. '13:00') or 'recent'."

        with self._lock:
            self.settings.orb_start_time = start_t
            self.settings.orb_window_minutes = mins
            end_dt = datetime.combine(now_ist.date(), start_t) + timedelta(minutes=mins)
            self.settings.orb_end_time = end_dt.time()

            # If afternoon range pushes past default 14:15 cutoff, extend cutoff safely up to 15:15
            if self.settings.orb_end_time >= self.settings.entry_cutoff_time and self.settings.orb_end_time < self.settings.auto_squareoff_time:
                extended = (datetime.combine(now_ist.date(), self.settings.orb_end_time) + timedelta(minutes=60)).time()
                self.settings.entry_cutoff_time = min(self.settings.auto_squareoff_time, extended)
                logger.info("Extended entry cutoff time to %s to accommodate %s range", self.settings.entry_cutoff_time, self.settings.orb_end_time)

            self.strategy.reset_daily()
            if now_ist.weekday() < 5 and now_ist.time() >= self.settings.orb_end_time:
                self.run_lock_orb(now_ist)

        start_str = self.settings.orb_start_time.strftime("%H:%M")
        end_str = self.settings.orb_end_time.strftime("%H:%M")
        msg = f"ORB window set to {mins}m ({start_str}–{end_str} IST); opening range recalculated."
        logger.info(msg)
        return True, msg

    def set_orb_window(self, minutes: int) -> Tuple[bool, str]:
        """Set Opening Range duration (5, 15, 30, 45, or 60 min) keeping current start time."""
        current_start = getattr(self.settings, "orb_start_time", dt_time(9, 15, 0)).strftime("%H:%M")
        return self.set_orb_anchor(start_time=current_start, minutes=minutes)

    def reset_daily_trades(self) -> Tuple[bool, str]:
        """Reset daily trades count to 0 and clear any risk management halt."""
        self.risk_manager.reset_trades_counter()
        msg = f"Daily trades counter reset to 0/{self.settings.max_trades_per_day}; strategy unhalted."
        logger.info(msg)
        return True, msg

    def set_entry_cutoff_time(self, cutoff_time_str: str) -> Tuple[bool, str]:
        """Configure entry cutoff time between ORB end time (e.g. 09:30) and Auto Squareoff (15:15)."""
        clean_str = str(cutoff_time_str).strip()
        try:
            parts = clean_str.split(":")
            ch = int(parts[0])
            cm = int(parts[1]) if len(parts) > 1 else 0
            cutoff_t = dt_time(ch, cm, 0)
        except Exception:
            return False, f"Invalid time format: '{cutoff_time_str}'. Use 'HH:MM' (e.g. '14:30' or '15:15')."

        min_allowed = dt_time(9, 30, 0)
        max_allowed = self.settings.auto_squareoff_time  # 15:15:00
        if cutoff_t < min_allowed:
            return False, f"Entry cutoff time cannot be before {min_allowed.strftime('%H:%M')} IST."
        if cutoff_t > max_allowed:
            return False, f"Entry cutoff time cannot be after auto square-off at {max_allowed.strftime('%H:%M')} IST."

        with self._lock:
            self.settings.entry_cutoff_time = cutoff_t

        msg = f"Auto breakout entry window updated to {self.settings.orb_end_time.strftime('%H:%M')}–{cutoff_t.strftime('%H:%M')} IST."
        logger.info(msg)
        return True, msg

    def set_index_lots(self, index_name: str, lots: int) -> Tuple[bool, str]:
        """Configure user-selected trade lots per instrument (1 to 50 lots)."""
        idx = index_name.strip().upper()
        if idx not in INDEX_SPECS:
            return False, f"Unknown instrument '{index_name}'. Valid: {list(INDEX_SPECS.keys())}"
        try:
            val = int(lots)
        except (ValueError, TypeError):
            return False, "Lot count must be an integer."
        if val < 1 or val > 50:
            return False, "Lot count must be between 1 and 50."

        with self._lock:
            self.custom_lots[idx] = val
            self.risk_manager.set_index_lots(idx, val)

        spec = INDEX_SPECS[idx]
        tot_qty = spec.lot_size * val
        msg = f"Updated {idx} to {val} lot(s) ({tot_qty} quantity)."
        logger.info(msg)
        return True, msg

    def set_index_product_type(self, index_name: str, product_type: str) -> Tuple[bool, str]:
        """Configure order product type (NRML or MIS) per instrument."""
        idx = index_name.strip().upper()
        if idx not in INDEX_SPECS:
            return False, f"Unknown instrument '{index_name}'."
        norm = product_type.strip().upper()
        if norm in {"M", "NRML", "NORMAL"}:
            val = "M"
            lbl = "NRML (Normal)"
        elif norm in {"I", "MIS", "INTRADAY"}:
            val = "I"
            lbl = "MIS (Intraday)"
        else:
            return False, f"Invalid product type '{product_type}'. Choose NRML or MIS."

        with self._lock:
            self.custom_product_types[idx] = val
            self.risk_manager.set_index_product_type(idx, val)

        msg = f"Updated {idx} order product type to {lbl}."
        logger.info(msg)
        return True, msg

    def trigger_auto_order(
        self,
        index_name: str = "BANKNIFTY",
        signal_type: Optional[str] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Trigger strategy automated entry order immediately into broker terminal (LIVE) or paper (PAPER)."""
        idx = index_name.strip().upper()
        if idx not in INDEX_SPECS:
            valid_names = ", ".join(f"'{k}'" for k in INDEX_SPECS)
            return False, f"Invalid index: '{index_name}'. Use one of: {valid_names}.", {}

        st = self.strategy.get_state(idx)
        spec = INDEX_SPECS[idx]
        now_ist = datetime.now(IST)
        spot_ltp = self.client.get_ltp(spec.spot_key) or st.last_spot or 0.0

        if spot_ltp <= 0:
            try:
                q = self.client.get_quotes(spec.spot_exchange, spec.spot_token)
                if isinstance(q, dict):
                    raw_lp = q.get("lp") or q.get("c") or q.get("bp1") or q.get("sp1")
                    if raw_lp and float(raw_lp) > 0:
                        spot_ltp = float(raw_lp)
                        self.client.update_ltp(spec.spot_key, spot_ltp)
                        logger.info("Fetched live quote for %s (%s): %.2f", idx, spec.spot_key, spot_ltp)
            except Exception as exc:
                logger.debug("Failed fetching REST quote for %s: %s", idx, exc)

        if spot_ltp <= 0:
            return False, f"Cannot trigger auto-order: No live spot LTP available for {idx}.", {}

        # Allow testing on-demand even if market day was marked completed
        if self.risk_manager.halted and self.risk_manager.halt_reason in {"COMPLETED_FOR_DAY", "EOD_SQUAREOFF"}:
            self.risk_manager.halted = False
            self.risk_manager.halt_reason = None
            logger.info("Cleared COMPLETED_FOR_DAY halt for on-demand %s trigger", idx)

        sig_type = signal_type.strip().upper() if signal_type else "AUTO"
        if sig_type not in {"CE", "PE"}:
            vwap = st.last_vwap or spot_ltp
            sig_type = "CE" if spot_ltp >= vwap else "PE"

        orh = st.orh or round(spot_ltp + 10.0, 2)
        orl = st.orl or round(spot_ltp - 10.0, 2)
        vwap = st.last_vwap or spot_ltp
        rsi = st.last_rsi or (65.0 if sig_type == "CE" else 35.0)
        vix = self.client.get_ltp(INDIA_VIX_KEY) or 15.0

        from engine.strategy_orb import ORBSignal
        signal = ORBSignal(
            index_name=idx,
            signal_type=sig_type,
            candle_time=now_ist,
            spot_close=spot_ltp,
            orh=orh,
            orl=orl,
            vwap=vwap,
            rsi=rsi,
            india_vix=vix,
        )

        active_lots = self.custom_lots.get(idx, spec.default_lots)
        logger.info(
            "Triggering automated strategy entry: %s %s spot=%.2f VWAP=%.2f RSI=%.1f lots=%d (MODE=%s)",
            idx, sig_type, spot_ltp, vwap, rsi, active_lots, self.settings.mode
        )
        pos = self.risk_manager.execute_entry(signal, lots=active_lots)
        if pos is None:
            return False, "Strategy entry execution skipped or rejected by risk manager.", {}

        self.client.get_live_broker_snapshot(force=True)
        order_id = pos.entry_order_id or "SUBMITTED"
        msg = f"Auto-order triggered successfully: {idx} {pos.contract.tradingsymbol} qty={pos.initial_qty} (Order ID: {order_id}) in {self.settings.mode} mode."
        logger.info(msg)
        return True, msg, {
            "order_id": order_id,
            "tradingsymbol": pos.contract.tradingsymbol,
            "quantity": pos.initial_qty,
            "price": pos.entry_limit_price,
            "mode": self.settings.mode,
        }

    def trigger_kill_switch(self, reason: str = "MANUAL_UI_KILL_SWITCH") -> None:
        self.risk_manager.emergency_kill_switch(reason=reason)

    def exit_position(self, index_name: str) -> Tuple[bool, str]:
        """Manually exit / square off an active position for an index or commodity."""
        idx = index_name.strip().upper()
        ok = self.risk_manager.exit_position(idx, reason="UI_MANUAL_EXIT")
        if ok:
            msg = f"Successfully squared off {idx} position."
            logger.info(msg)
            return True, msg
        return False, f"No active position found for '{idx}'."

    def toggle_mode(self, force_clear_paper: bool = True) -> Tuple[bool, str]:
        with self._lock:
            if self.risk_manager.positions:
                if self.settings.mode == "PAPER" or force_clear_paper:
                    logger.info("Auto-clearing %d simulated PAPER position(s) to switch MODE", len(self.risk_manager.positions))
                    for idx_name in list(self.risk_manager.positions.keys()):
                        self.risk_manager.exit_position(idx_name, reason="MODE_SWITCH_CLEANUP")
                    self.risk_manager.halted = False
                    self.risk_manager.halt_reason = None
                else:
                    return False, "Cannot toggle MODE while real LIVE broker positions are active. Square off live positions first."
            new_mode = "LIVE" if self.settings.mode == "PAPER" else "PAPER"
            self.settings.mode = new_mode
            logger.warning("Trading MODE switched to %s via UI", new_mode)
            return True, f"Switched mode to {new_mode}"

    def step_scheduler(self, now_ist: Optional[datetime] = None) -> None:
        """Advance scheduled milestones and 1s/10s/15s operational checks."""
        dt = now_ist or datetime.now(IST)
        today = dt.date()
        current_t = dt.time()

        # Skip trading schedule on weekends (Mon=0..Fri=4)
        if dt.weekday() < 5:
            if current_t < self.settings.auth_time and self._milestone_ready("reset", today):
                self.strategy.reset_daily(today)
                self.risk_manager.reset_daily()
                self._mark_milestone("reset", today)

            if current_t >= self.settings.auth_time and self._milestone_ready("auth", today):
                self.run_daily_login()
                self._mark_milestone("auth", today)

            if current_t >= self.settings.instrument_cache_time and self._milestone_ready("cache", today):
                self.run_instrument_cache()
                self._mark_milestone("cache", today)

            if current_t >= self.settings.ws_connect_time and self._milestone_ready("ws", today):
                self.run_ws_connect_and_backfill()
                self._mark_milestone("ws", today)

            if current_t >= self.settings.orb_end_time and self._milestone_ready("orb_lock", today):
                if current_t >= self.settings.market_close_time:
                    # After market hours: mark milestone complete so scheduler does not repeatedly poll
                    self._mark_milestone("orb_lock", today)
                else:
                    now_ts = time.time()
                    if now_ts - getattr(self, "_last_orb_lock_attempt", 0.0) >= 30.0:
                        self._last_orb_lock_attempt = now_ts
                        for builder in self.candle_builders.values():
                            builder.flush_due_candles(dt)
                        locked = self.run_lock_orb(dt)
                        if locked:
                            self._mark_milestone("orb_lock", today)

            if current_t >= self.settings.auto_squareoff_time and self._milestone_ready("squareoff", today):
                self.run_eod_squareoff()
                self._mark_milestone("squareoff", today)

            if current_t >= self.settings.eod_report_time and self._milestone_ready("eod_report", today):
                self.run_eod_report()
                self._mark_milestone("eod_report", today)

            # Continuous intraday checks (NSE equity 09:15-15:30; MCX commodity 09:00-23:30)
            has_mcx = any(getattr(s, "spot_exchange", "") == "MCX" for s in INDEX_SPECS.values())
            mkt_open = dt_time(9, 0, 0) if has_mcx else self.settings.market_open_time
            mkt_close = dt_time(23, 30, 0) if has_mcx else self.settings.market_close_time
            if mkt_open <= current_t <= mkt_close:
                for builder in self.candle_builders.values():
                    builder.flush_due_candles(dt)
                self.risk_manager.check_pending_entries()
                self.risk_manager.check_mtm_kill_switch()
                if self.settings.mode == "LIVE" or self.client.ws_connected:
                    self.client.check_ws_watchdog(dt)

                now_m = time.monotonic()
                if now_m - self._last_reconcile_monotonic >= self.settings.reconcile_interval_sec:
                    self.risk_manager.reconcile_positions()
                    self._last_reconcile_monotonic = now_m

    def start_background_loop(self) -> None:
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return

        def _loop() -> None:
            logger.info("TradingSystemOrchestrator loop started (MODE=%s)", self.settings.mode)
            while not self._stop_event.is_set():
                try:
                    self.step_scheduler()
                except Exception as exc:
                    logger.error("Scheduler iteration error: %s", exc)
                self._stop_event.wait(self.settings.mtm_check_interval_sec)

        self._loop_thread = threading.Thread(
            target=_loop,
            name="orb-orchestrator",
            daemon=True,
        )
        self._loop_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop_thread is not None and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=3.0)
        self.notifier.stop()

    def _build_index_radar(
        self,
        index_name: str,
        spot_ltp: Optional[float],
        vix: Optional[float],
        now_ist: datetime,
    ) -> Dict[str, Any]:
        """Compute live 5-gate auto-order readiness metrics for an index."""
        spec = INDEX_SPECS[index_name]
        st = self.strategy.get_state(index_name)

        system_ready = (
            not self.risk_manager.halted
            and self.risk_manager.trades_taken_today < self.settings.max_trades_per_day
            and index_name not in self.risk_manager.positions
        )
        vix_ready = (vix is not None and vix >= self.settings.min_india_vix)
        orb_ready = bool(st.is_locked and not st.no_trade_reason)
        entry_start = getattr(self.settings, "entry_start_time", dt_time(10, 0, 0))
        entry_end = getattr(self.settings, "entry_cutoff_time", dt_time(13, 15, 0))
        in_window = (
            now_ist.weekday() < 5
            and (entry_start <= now_ist.time() <= entry_end)
        )

        ce_price_ok = bool(
            spot_ltp is not None
            and st.orh is not None
            and spot_ltp > st.orh
            and (st.last_vwap is None or spot_ltp > st.last_vwap)
        )
        pe_price_ok = bool(
            spot_ltp is not None
            and st.orl is not None
            and spot_ltp < st.orl
            and (st.last_vwap is None or spot_ltp < st.last_vwap)
        )
        ce_rsi_ok = bool(st.last_rsi is not None and (st.last_rsi > 60.0))
        pe_rsi_ok = bool(st.last_rsi is not None and (st.last_rsi < 40.0))

        dist_to_orh = round(st.orh - spot_ltp, 2) if (spot_ltp is not None and st.orh is not None) else None
        dist_to_orl = round(spot_ltp - st.orl, 2) if (spot_ltp is not None and st.orl is not None) else None

        # Dynamic Position Sizing based on available funds:
        # Tuesday: 75% allocation; Mon/Wed/Thu/Fri: 50% allocation
        avail_fund = self.risk_manager.get_available_fund()
        is_tuesday = (now_ist.weekday() == 1)
        alloc_pct = 75 if is_tuesday else 50
        alloc_fund = round(avail_fund * (alloc_pct / 100.0), 2)

        atm_premium = 120.0 if index_name == "NIFTY" else 260.0
        if spot_ltp is not None and spot_ltp > 0:
            try:
                c = self.mapper.select_option_contract(index_name, spot_ltp, "CE", vix=vix or 15.0)
                token_key = f"{c.exchange}|{c.token}"
                cached_ltp = self.client.get_ltp(token_key)
                if cached_ltp is not None and cached_ltp > 0:
                    atm_premium = float(cached_ltp)
            except Exception:
                pass

        dynamic_lots = self.risk_manager.calculate_dynamic_lots(index_name, atm_premium, now_ist)
        if index_name in self.custom_lots:
            lots = self.custom_lots[index_name]
            is_custom = True
        else:
            lots = dynamic_lots
            is_custom = False

        product_type = self.custom_product_types.get(index_name, spec.product_type)
        product_type_label = "NRML" if product_type == "M" else "MIS"
        total_qty = spec.lot_size * lots
        est_margin = round(total_qty * atm_premium, 2)

        candidate_ce = "--"
        candidate_pe = "--"
        if spot_ltp is not None and spot_ltp > 0:
            strike = round_to_strike(spot_ltp, spec.strike_step)
            candidate_ce = f"{index_name} {strike} CE ({total_qty} qty / {lots} lot [{product_type_label}])"
            candidate_pe = f"{index_name} {strike} PE ({total_qty} qty / {lots} lot [{product_type_label}])"

        ce_score = sum([system_ready, vix_ready, orb_ready, ce_price_ok, ce_rsi_ok])
        pe_score = sum([system_ready, vix_ready, orb_ready, pe_price_ok, pe_rsi_ok])
        readiness_pct = int(round((max(ce_score, pe_score) / 5.0) * 100))

        if index_name in self.risk_manager.positions:
            radar_state = "ACTIVE_POSITION"
            radar_summary = "Auto position active — SL-LMT, Breakeven (+25/+55) & Trailing active."
        elif self.risk_manager.halted or self.risk_manager.trades_taken_today >= self.settings.max_trades_per_day:
            radar_state = "HALTED_OR_MAX_TRADES"
            radar_summary = f"Auto entries paused: {self.risk_manager.halt_reason or 'Max daily trades reached'}."
        elif st.no_trade_reason:
            radar_state = "NO_TRADE_DAY"
            radar_summary = f"No auto-trade today: {st.no_trade_reason}."
        elif not st.is_locked:
            radar_state = "WAITING_ORB_LOCK"
            start_str = self.settings.orb_start_time.strftime('%H:%M')
            end_str = self.settings.orb_end_time.strftime('%H:%M')
            radar_summary = f"Collecting {start_str}-{end_str} IST opening candles; ORH/ORL locks at {end_str}."
        elif orb_ready and ce_price_ok:
            radar_state = "IMMINENT_CE_BREAKOUT"
            if ce_rsi_ok and vix_ready and in_window:
                radar_summary = f"ALL 5 GATES MET! Auto-BUY {candidate_ce} arms on 5m close."
            else:
                rsi_txt = f"{st.last_rsi:.1f}" if st.last_rsi is not None else "--"
                radar_summary = f"Above ORH & VWAP! Need RSI > 60 (now {rsi_txt}) & 5m close -> {candidate_ce}."
        elif orb_ready and pe_price_ok:
            radar_state = "IMMINENT_PE_BREAKOUT"
            if pe_rsi_ok and vix_ready and in_window:
                radar_summary = f"ALL 5 GATES MET! Auto-BUY {candidate_pe} arms on 5m close."
            else:
                rsi_txt = f"{st.last_rsi:.1f}" if st.last_rsi is not None else "--"
                radar_summary = f"Below ORL & VWAP! Need RSI < 40 (now {rsi_txt}) & 5m close -> {candidate_pe}."
        else:
            radar_state = "SCANNING_RANGE"
            orh_txt = f"{dist_to_orh:+.2f}" if dist_to_orh is not None else "--"
            orl_txt = f"{dist_to_orl:+.2f}" if dist_to_orl is not None else "--"
            radar_summary = f"Inside range: {orh_txt} pts to ORH (CE) | {orl_txt} pts above ORL (PE)."

        return {
            "system_ready": system_ready,
            "vix_ready": vix_ready,
            "orb_ready": orb_ready,
            "in_window": in_window,
            "ce_price_ok": ce_price_ok,
            "pe_price_ok": pe_price_ok,
            "ce_rsi_ok": ce_rsi_ok,
            "pe_rsi_ok": pe_rsi_ok,
            "dist_to_orh": dist_to_orh,
            "dist_to_orl": dist_to_orl,
            "candidate_ce": candidate_ce,
            "candidate_pe": candidate_pe,
            "readiness_pct": readiness_pct,
            "radar_state": radar_state,
            "radar_summary": radar_summary,
            "lots": lots,
            "dynamic_lots": dynamic_lots,
            "is_custom_lots": is_custom,
            "avail_fund": round(avail_fund, 2),
            "alloc_pct": alloc_pct,
            "alloc_fund": alloc_fund,
            "product_type": product_type,
            "product_type_label": product_type_label,
            "lot_size": spec.lot_size,
            "total_qty": total_qty,
            "atm_premium": round(atm_premium, 2),
            "est_margin": est_margin,
        }

    def _build_schedule_status(self, now_ist: datetime) -> Dict[str, Any]:
        """Return automated daily IST schedule phase, candle countdown, and milestones."""
        today = now_ist.date()
        t = now_ist.time()
        is_weekday = now_ist.weekday() < 5
        entry_start = getattr(self.settings, "entry_start_time", dt_time(10, 0, 0))
        entry_cutoff = getattr(self.settings, "entry_cutoff_time", dt_time(13, 15, 0))

        if not is_weekday:
            phase = "WEEKEND_STANDBY"
        elif t < self.settings.market_open_time:
            phase = "PRE_MARKET"
        elif t < self.settings.orb_end_time:
            phase = f"ORB_FORMATION ({self.settings.orb_start_time.strftime('%H:%M')}-{self.settings.orb_end_time.strftime('%H:%M')})"
        elif t < entry_start:
            phase = f"ORB_LOCKED_COOLOFF ({self.settings.orb_end_time.strftime('%H:%M')}-{entry_start.strftime('%H:%M')})"
        elif t <= entry_cutoff:
            phase = f"INSTITUTIONAL_SCANNER ({entry_start.strftime('%H:%M')}-{entry_cutoff.strftime('%H:%M')})"
        elif t < self.settings.auto_squareoff_time:
            phase = f"POSITION_MANAGEMENT ({entry_cutoff.strftime('%H:%M')}-{self.settings.auto_squareoff_time.strftime('%H:%M')})"
        elif t <= self.settings.market_close_time:
            phase = "POST_SQUARE_OFF (15:15-15:30)"
        else:
            phase = "MARKET_CLOSED"

        next_candle_close_sec: Optional[int] = None
        is_trading_hour = (self.settings.market_open_time <= t < self.settings.market_close_time)
        if is_weekday and is_trading_hour:
            sec_into_5m = (now_ist.minute % 5) * 60 + now_ist.second
            next_candle_close_sec = max(1, 300 - sec_into_5m)

        all_locked = all(self.strategy.get_state(name).is_locked for name in INDEX_SPECS)

        def _ms_status(done_cond: bool, active_cond: bool = False) -> str:
            if done_cond:
                return "DONE"
            if active_cond:
                return "ACTIVE"
            return "PENDING"

        milestones = [
            {
                "time": "08:40 IST",
                "label": "Auto OAuth Login",
                "status": _ms_status(
                    bool(self.auth.last_token_date == today.strftime("%Y-%m-%d"))
                    or self._milestone_done.get("auth") == today
                ),
            },
            {
                "time": "08:55 IST",
                "label": "NFO Contract Cache",
                "status": _ms_status(
                    self.mapper.cached_count > 0 or self._milestone_done.get("cache") == today
                ),
            },
            {
                "time": "09:14 IST",
                "label": "WS & 3D Backfill",
                "status": _ms_status(
                    self.client.ws_connected or self._milestone_done.get("ws") == today
                ),
            },
            {
                "time": f"{self.settings.orb_end_time.strftime('%H:%M')} IST",
                "label": f"{self.settings.orb_window_minutes}-Min ORB Lock",
                "status": _ms_status(all_locked or self._milestone_done.get("orb_lock") == today),
            },
            {
                "time": f"{entry_start.strftime('%H:%M')}-{entry_cutoff.strftime('%H:%M')}",
                "label": "Institutional Breakout Scan",
                "status": _ms_status(
                    is_weekday and t > entry_cutoff,
                    active_cond=is_weekday
                    and entry_start <= t <= entry_cutoff,
                ),
            },
            {
                "time": "15:15 IST",
                "label": "Auto EOD Square-Off",
                "status": _ms_status(
                    self._milestone_done.get("squareoff") == today
                    or (is_weekday and t >= self.settings.auto_squareoff_time)
                ),
            },
        ]

        return {
            "phase": phase,
            "is_market_day": is_weekday,
            "next_candle_close_sec": next_candle_close_sec,
            "milestones": milestones,
            "auto_mode_note": (
                f"Auto orders trigger automatically on 5m candle closes "
                f"({entry_start.strftime('%H:%M')}-{entry_cutoff.strftime('%H:%M')} IST) "
                f"in {self.settings.mode} mode when all 5 gates align."
            ),
        }

    def get_status_snapshot(self) -> Dict[str, Any]:
        """Return thread-safe JSON snapshot for the live FastAPI Web UI."""
        now_ist = datetime.now(IST)
        vix = self.client.get_ltp(INDIA_VIX_KEY)
        indices_data: Dict[str, Any] = {}
        for name, spec in INDEX_SPECS.items():
            st = self.strategy.get_state(name)
            spot_ltp = self.client.get_ltp(spec.spot_key) or st.last_spot
            indices_data[name] = {
                "spot_ltp": spot_ltp,
                "orh": st.orh,
                "orl": st.orl,
                "range_pct": st.range_pct,
                "is_locked": st.is_locked,
                "no_trade_reason": st.no_trade_reason,
                "vwap": st.last_vwap,
                "rsi": st.last_rsi,
                "last_signal": st.last_signal,
                "last_eval_time": st.last_eval_time,
                "radar": self._build_index_radar(name, spot_ltp, vix, now_ist),
            }

        positions_data = []
        for pos in list(self.risk_manager.positions.values()):
            spec = INDEX_SPECS[pos.index_name]
            positions_data.append(
                {
                    "trade_id": pos.trade_id,
                    "index_name": pos.index_name,
                    "tradingsymbol": pos.contract.tradingsymbol,
                    "signal_type": pos.signal_type,
                    "state": pos.state.value,
                    "initial_qty": pos.initial_qty,
                    "remaining_qty": pos.remaining_qty,
                    "entry_avg_price": pos.entry_avg_price,
                    "last_ltp": pos.last_ltp,
                    "sl_trigger_price": pos.sl_trigger_price,
                    "breakeven_target": round(pos.entry_avg_price + spec.breakeven_trigger_pts, 2),
                    "target1_price": round(pos.entry_avg_price + spec.target1_pts, 2),
                    "unrealized_pnl": pos.unrealized_pnl,
                    "realized_pnl": pos.realized_pnl,
                }
            )

        return {
            "mode": self.settings.mode,
            "capital": self.settings.capital,
            "user_id": self.settings.shoonya_user_id,
            "orb_window_minutes": self.settings.orb_window_minutes,
            "orb_start_time": self.settings.orb_start_time.strftime("%H:%M"),
            "orb_end_time": self.settings.orb_end_time.strftime("%H:%M"),
            "entry_start_time": getattr(self.settings, "entry_start_time", dt_time(10, 0, 0)).strftime("%H:%M"),
            "entry_cutoff_time": self.settings.entry_cutoff_time.strftime("%H:%M"),
            "oauth_status": self.auth.last_login_status,
            "token_date": self.auth.last_token_date,
            "sebi_algo_id": self.settings.sebi_algo_id,
            "ist_time": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "ws_connected": self.client.ws_connected,
            "public_ip": self.public_ip,
            "india_vix": vix,
            "avail_fund": round(self.risk_manager.get_available_fund(), 2),
            "weekly_guard": self.risk_manager.get_weekly_guard_status(now_ist),
            "mtm_pnl": self.risk_manager.total_mtm(),
            "realized_pnl": self.risk_manager.realized_pnl_today,
            "trades_taken": self.risk_manager.trades_taken_today,
            "max_trades": self.settings.max_trades_per_day,
            "max_daily_loss": self.settings.max_daily_loss,
            "halted": self.risk_manager.halted,
            "halt_reason": self.risk_manager.halt_reason,
            "cached_contracts": self.mapper.cached_count,
            "indices": indices_data,
            "schedule": self._build_schedule_status(now_ist),
            "positions": positions_data,
            "completed_trades": [t.__dict__ for t in self.risk_manager.completed_trades],
            "broker": self.client.get_live_broker_snapshot(force=False),
            "logs": get_recent_logs(100),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Shoonya ORB Automated Options Trading System")
    parser.add_argument("--cli-only", action="store_true", help="Run trading scheduler without FastAPI Web UI")
    args = parser.parse_args()

    settings = load_settings()
    orchestrator = TradingSystemOrchestrator(settings=settings)
    orchestrator.start_background_loop()

    try:
        if args.cli_only:
            while True:
                time.sleep(1.0)
        else:
            app = create_app(orchestrator)
            uvicorn.run(
                app,
                host=settings.ui_host,
                port=settings.ui_port,
                log_level="info",
            )
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user")
    finally:
        orchestrator.stop()


if __name__ == "__main__":
    main()
