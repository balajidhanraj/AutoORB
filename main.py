"""Entry point: IST daily scheduler, WebSocket event loop, and FastAPI Web UI server."""

from __future__ import annotations

import argparse
import threading
import time
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

import uvicorn

from broker.auth import ShoonyaAuthenticator
from broker.instrument_mapper import InstrumentMapper
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

        self.candle_builders: Dict[str, CandleBuilder] = {
            name: CandleBuilder(name, spec.spot_exchange, spec.spot_token)
            for name, spec in INDEX_SPECS.items()
        }
        for name, builder in self.candle_builders.items():
            builder.register_close_callback(self._on_candle_closed)

        self.client.register_tick_listener(self._on_raw_tick)
        self.client.register_reconnect_callback(self._backfill_all_indices)

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._last_reconcile_monotonic: float = 0.0

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

    def run_lock_orb(self, now_ist: Optional[datetime] = None) -> None:
        dt = now_ist or datetime.now(IST)
        lines = [f"<b>ORB Locked ({dt.strftime('%Y-%m-%d')}) [{self.settings.mode}]</b>"]
        for name, builder in self.candle_builders.items():
            state = self.strategy.lock_opening_range(name, builder.get_candles(), session_date=dt.date())
            if state.is_locked:
                status = state.no_trade_reason or "ELIGIBLE"
                lines.append(
                    f"• {name}: ORH={state.orh}, ORL={state.orl}, Range={state.range_pct:.2f}% ({status})"
                )
            else:
                lines.append(f"• {name}: Pending opening candles")
        self.notifier.send_async("\n".join(lines))

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

    def trigger_kill_switch(self, reason: str = "MANUAL_UI_KILL_SWITCH") -> None:
        self.risk_manager.emergency_kill_switch(reason=reason)

    def toggle_mode(self) -> Tuple[bool, str]:
        with self._lock:
            if self.risk_manager.positions:
                return False, "Cannot toggle MODE while positions are active."
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
                for builder in self.candle_builders.values():
                    builder.flush_due_candles(dt)
                self.run_lock_orb(dt)
                self._mark_milestone("orb_lock", today)

            if current_t >= self.settings.auto_squareoff_time and self._milestone_ready("squareoff", today):
                self.run_eod_squareoff()
                self._mark_milestone("squareoff", today)

            if current_t >= self.settings.eod_report_time and self._milestone_ready("eod_report", today):
                self.run_eod_report()
                self._mark_milestone("eod_report", today)

            # Continuous intraday checks
            if self.settings.market_open_time <= current_t <= self.settings.market_close_time:
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

    def get_status_snapshot(self) -> Dict[str, Any]:
        """Return thread-safe JSON snapshot for the live FastAPI Web UI."""
        now_ist = datetime.now(IST)
        indices_data: Dict[str, Any] = {}
        for name, spec in INDEX_SPECS.items():
            st = self.strategy.get_state(name)
            indices_data[name] = {
                "spot_ltp": self.client.get_ltp(spec.spot_key) or st.last_spot,
                "orh": st.orh,
                "orl": st.orl,
                "range_pct": st.range_pct,
                "is_locked": st.is_locked,
                "no_trade_reason": st.no_trade_reason,
                "vwap": st.last_vwap,
                "rsi": st.last_rsi,
                "last_signal": st.last_signal,
                "last_eval_time": st.last_eval_time,
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
            "oauth_status": self.auth.last_login_status,
            "token_date": self.auth.last_token_date,
            "sebi_algo_id": self.settings.sebi_algo_id,
            "ist_time": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "ws_connected": self.client.ws_connected,
            "public_ip": self.public_ip,
            "india_vix": self.client.get_ltp(INDIA_VIX_KEY),
            "mtm_pnl": self.risk_manager.total_mtm(),
            "realized_pnl": self.risk_manager.realized_pnl_today,
            "trades_taken": self.risk_manager.trades_taken_today,
            "max_trades": self.settings.max_trades_per_day,
            "max_daily_loss": self.settings.max_daily_loss,
            "halted": self.risk_manager.halted,
            "halt_reason": self.risk_manager.halt_reason,
            "cached_contracts": self.mapper.cached_count,
            "indices": indices_data,
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
