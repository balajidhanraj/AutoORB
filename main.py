"""Entry point: IST daily scheduler, WebSocket event loop, and FastAPI Web UI server."""

from __future__ import annotations

import argparse
import os
import re
import threading
import time
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Dict, Optional, Tuple

import uvicorn

from broker.auth import ShoonyaAuthenticator
from broker.instrument_mapper import InstrumentMapper, round_to_strike
from broker.shoonya_client import ShoonyaClient
from config.settings import (
    INDEX_SPECS,
    INDIA_VIX_EXCHANGE,
    INDIA_VIX_KEY,
    INDIA_VIX_TOKEN,
    IST,
    NSE_HOLIDAYS_2026,
    AppSettings,
    is_nse_trading_day,
    load_settings,
)
from engine.candle_builder import Candle, CandleBuilder
from engine.risk_manager import RiskManager
from engine.strategy_orb import ORBStrategy
from services.broker_archive_store import BrokerArchiveStore
from services.runtime_state_store import RuntimeStateStore
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
        self._reauth_lock = threading.Lock()
        self.client.register_reauth_callback(self._handle_reauth)
        self.client.subscribe([spec.spot_key for spec in INDEX_SPECS.values()] + [INDIA_VIX_KEY])

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._loop_thread: Optional[threading.Thread] = None
        self._last_reconcile_monotonic: float = 0.0
        self._last_signal_check_monotonic: float = 0.0
        self.last_signal_check_time: Optional[str] = None
        self._last_triggered_bucket_by_index: Dict[str, datetime] = {}

        self.custom_lots: Dict[str, int] = {}
        self.custom_product_types: Dict[str, str] = {
            name: spec.product_type for name, spec in INDEX_SPECS.items()
        }

        # Track daily schedule milestones by IST date and manual user overrides
        self._milestone_done: Dict[str, date] = {}
        self._milestone_overrides: Dict[str, Tuple[date, str, dt_time]] = {}
        self._last_auth_attempt: float = 0.0
        self._last_cache_attempt: float = 0.0
        self._last_ws_attempt: float = 0.0
        self._last_ws_attempt_date: Optional[date] = None
        self.public_ip: str = "detecting..."
        self.previous_public_ip: str = ""
        self._last_ip_check_monotonic: float = time.monotonic()
        self._last_self_ping_monotonic: float = 0.0
        self._last_broker_archive_monotonic: float = 0.0
        self._ip_sync_lock = threading.Lock()
        self._user_mutated_state: bool = False

        self.broker_archive = BrokerArchiveStore(self.settings.data_dir)
        self.state_store = RuntimeStateStore(self.settings.data_dir)
        self.auth.register_token_persist_callback(self._on_token_persisted)
        self.risk_manager.register_state_change_callback(self._on_risk_state_changed)
        restored_token_payload = self._restore_runtime_state()

        if getattr(self.state_store, "_remote_enabled", False) and not getattr(
            self.state_store, "remote_load_ok", True
        ):
            self._start_delayed_remote_restore()

        threading.Thread(
            target=self._detect_public_ip,
            name="public-ip-detect",
            daemon=True,
        ).start()
        if self.settings.shoonya_user_id:
            try:
                self.auth.load_cached_token(external_payload=restored_token_payload)
            except Exception as exc:
                logger.debug("Startup cached token check skipped: %s", exc)

    def _on_token_persisted(self, _payload: Dict[str, Any]) -> None:
        self._persist_runtime_state(sync=False, force_remote=False)

    def _on_risk_state_changed(self) -> None:
        self._persist_runtime_state(sync=False, force_remote=False)
        if self.settings.mode == "LIVE":
            threading.Thread(
                target=self.sync_live_broker_archive,
                kwargs={"trigger_event": "ORDER_EVENT", "force_refresh": True, "sync": False},
                name="broker-archive-event-sync",
                daemon=True,
            ).start()

    def sync_live_broker_archive(
        self,
        trigger_event: str = "PERIODIC",
        force_refresh: bool = False,
        sync: bool = False,
    ) -> Dict[str, Any]:
        """In LIVE mode, capture all Shoonya broker limits, orders, trades, positions, and holdings to DB."""
        if self.settings.mode != "LIVE":
            return {"stored": False, "reason": "not_live_mode"}
        try:
            snap = self.client.get_live_broker_snapshot(force=force_refresh)
            raw = self.client.get_last_raw_broker_payload()
            return self.broker_archive.archive_live_snapshot(
                snap,
                raw_payload=raw,
                trigger_event=trigger_event,
                sync=sync,
            )
        except Exception as exc:
            logger.warning("LIVE broker archive sync error (%s): %s", trigger_event, exc)
            return {"stored": False, "error": str(exc)}

    def _start_delayed_remote_restore(self) -> None:
        """Retry loading state from Neon Postgres when cold start timed out."""

        def _worker() -> None:
            for _attempt in range(6):
                if self._stop_event.wait(10.0):
                    return
                if self.state_store.remote_load_ok or self._user_mutated_state:
                    return
                try:
                    remote_state = self.state_store.load()
                    if (
                        self.state_store.remote_load_ok
                        and remote_state
                        and not self._user_mutated_state
                    ):
                        tok = self._restore_runtime_state(state=remote_state)
                        if tok and not self.auth.last_token_date:
                            self.auth.load_cached_token(external_payload=tok)
                        logger.info("Delayed Neon Postgres state recovery applied after cold start.")
                        return
                except Exception as exc:
                    logger.debug("Delayed remote state retry failed: %s", exc)

        threading.Thread(
            target=_worker,
            name="delayed-remote-state-restore",
            daemon=True,
        ).start()

    def _build_runtime_state_payload(self) -> Dict[str, Any]:
        today = datetime.now(IST).date()
        today_str = today.strftime("%Y-%m-%d")
        with self._lock:
            token_payload = self.auth.export_token_payload()
            durable_milestones = ("reset", "auth", "scanner_alert", "squareoff", "eod_report")
            done_today = [k for k in durable_milestones if self._milestone_done.get(k) == today]
            overrides_today = {
                k: [v[0].strftime("%Y-%m-%d"), str(v[1]), v[2].strftime("%H:%M:%S")]
                for k, v in self._milestone_overrides.items()
                if v[0] == today and k not in ("cache", "ws")
            }
            return {
                "version": 1,
                "mode": self.settings.mode,
                "capital": float(self.settings.capital),
                "custom_max_daily_loss": self.risk_manager.custom_max_daily_loss,
                "enabled_indices": self.risk_manager.get_enabled_indices(),
                "custom_lots": {str(k): int(v) for k, v in self.custom_lots.items()},
                "custom_product_types": {str(k): str(v) for k, v in self.custom_product_types.items()},
                "orb_start_time": self.settings.orb_start_time.strftime("%H:%M"),
                "orb_window_minutes": int(self.settings.orb_window_minutes),
                "entry_cutoff_time": self.settings.entry_cutoff_time.strftime("%H:%M"),
                "strategy_config": self.get_strategy_config(),
                "shoonya_user_id": self.settings.shoonya_user_id,
                "session_date": today_str,
                "daily_risk": {
                    "trades_taken_today": int(self.risk_manager.trades_taken_today),
                    "trades_taken_by_index": {
                        str(k): int(v) for k, v in self.risk_manager.trades_taken_by_index.items()
                    },
                    "sl_hit_today_by_index": {
                        str(k): bool(v) for k, v in self.risk_manager.sl_hit_today_by_index.items()
                    },
                    "realized_pnl_today": float(self.risk_manager.realized_pnl_today),
                    "halted": bool(self.risk_manager.halted),
                    "halt_reason": self.risk_manager.halt_reason,
                },
                "risk_session": self.risk_manager.export_session_state(),
                "paper_broker": self.client.export_paper_state(),
                "milestones_done": done_today,
                "milestone_overrides": overrides_today,
                "shoonya_token": token_payload,
                "updated_at_ist": datetime.now(IST).isoformat(),
            }

    def _persist_runtime_state(self, sync: bool = False, force_remote: bool = False) -> None:
        if force_remote:
            self._user_mutated_state = True
        try:
            payload = self._build_runtime_state_payload()
            if sync:
                self.state_store.save_sync(payload, timeout=10.0, force_remote=force_remote)
            else:
                self.state_store.save(payload, force_remote=force_remote)
        except Exception as exc:
            logger.warning("Failed persisting runtime state: %s", exc)

    def _restore_runtime_state(self, state: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        if state is None:
            try:
                state = self.state_store.load()
            except Exception as exc:
                logger.warning("Runtime state restore failed: %s", exc)
                return None
        if not isinstance(state, dict) or not state:
            return None

        now_ist = datetime.now(IST)
        today = now_ist.date()
        today_str = today.strftime("%Y-%m-%d")

        def _parse_hhmm_local(raw: Any, fallback: dt_time) -> dt_time:
            try:
                parts = str(raw).strip().split(":")
                return dt_time(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0, int(parts[2]) if len(parts) > 2 else 0)
            except Exception:
                return fallback

        with self._lock:
            saved_mode = str(state.get("mode") or "").strip().upper()
            if saved_mode in {"PAPER", "LIVE"}:
                self.settings.mode = saved_mode

            if state.get("capital") is not None:
                try:
                    cap_val = float(state["capital"])
                    if cap_val >= 0:
                        self.settings.capital = round(cap_val, 2)
                except (TypeError, ValueError):
                    pass

            if "custom_max_daily_loss" in state:
                raw_mdl = state.get("custom_max_daily_loss")
                if raw_mdl is None:
                    self.risk_manager.custom_max_daily_loss = None
                else:
                    try:
                        mdl_val = float(raw_mdl)
                        if mdl_val >= 500.0:
                            self.risk_manager.custom_max_daily_loss = mdl_val
                            self.settings.max_daily_loss = mdl_val
                    except (TypeError, ValueError):
                        pass

            en_map = state.get("enabled_indices")
            if isinstance(en_map, dict):
                self.risk_manager.set_enabled_indices(
                    nifty=en_map.get("NIFTY"),
                    banknifty=en_map.get("BANKNIFTY"),
                )

            lots_map = state.get("custom_lots")
            if isinstance(lots_map, dict):
                for k, v in lots_map.items():
                    idx = str(k).strip().upper()
                    if idx in INDEX_SPECS:
                        try:
                            lval = max(1, min(50, int(v)))
                            self.custom_lots[idx] = lval
                            self.risk_manager.set_index_lots(idx, lval)
                        except (TypeError, ValueError):
                            pass

            prd_map = state.get("custom_product_types")
            if isinstance(prd_map, dict):
                for k, v in prd_map.items():
                    idx = str(k).strip().upper()
                    norm_p = str(v).strip().upper()
                    if idx in INDEX_SPECS and norm_p in {"M", "I"}:
                        self.custom_product_types[idx] = norm_p
                        self.risk_manager.set_index_product_type(idx, norm_p)

            if state.get("orb_start_time"):
                self.settings.orb_start_time = _parse_hhmm_local(state["orb_start_time"], self.settings.orb_start_time)
            if state.get("orb_window_minutes") is not None:
                try:
                    mins = int(state["orb_window_minutes"])
                    if mins in {5, 15, 30, 45, 60}:
                        self.settings.orb_window_minutes = mins
                except (TypeError, ValueError):
                    pass
            if state.get("entry_cutoff_time"):
                self.settings.entry_cutoff_time = _parse_hhmm_local(state["entry_cutoff_time"], self.settings.entry_cutoff_time)

            strat_cfg = state.get("strategy_config")
            if isinstance(strat_cfg, dict) and strat_cfg:
                try:
                    self._apply_strategy_config_locked(strat_cfg, today=today)
                except ValueError:
                    pass

            end_dt = datetime.combine(today, self.settings.orb_start_time) + timedelta(minutes=self.settings.orb_window_minutes)
            self.settings.orb_end_time = end_dt.time()

            self.settings.max_daily_loss = self.risk_manager.get_dynamic_max_daily_loss(now_ist)

            if state.get("session_date") == today_str:
                dr = state.get("daily_risk") or {}
                if isinstance(dr, dict):
                    self.risk_manager.trades_taken_today = int(dr.get("trades_taken_today") or 0)
                    tbi = dr.get("trades_taken_by_index") or {}
                    if isinstance(tbi, dict):
                        self.risk_manager.trades_taken_by_index = {str(k): int(v) for k, v in tbi.items()}
                    slbi = dr.get("sl_hit_today_by_index") or {}
                    if isinstance(slbi, dict):
                        self.risk_manager.sl_hit_today_by_index = {str(k): bool(v) for k, v in slbi.items()}
                    self.risk_manager.realized_pnl_today = float(dr.get("realized_pnl_today") or 0.0)
                    self.risk_manager.halted = bool(dr.get("halted", False))
                    self.risk_manager.halt_reason = dr.get("halt_reason")

                for mk in state.get("milestones_done") or []:
                    if mk in ("reset", "auth", "scanner_alert", "squareoff", "eod_report"):
                        self._milestone_done[str(mk)] = today

                ov_map = state.get("milestone_overrides") or {}
                if isinstance(ov_map, dict):
                    for ok_key, ov_vals in ov_map.items():
                        if isinstance(ov_vals, (list, tuple)) and len(ov_vals) >= 3 and str(ov_vals[0]) == today_str:
                            self._milestone_overrides[str(ok_key)] = (
                                today,
                                str(ov_vals[1]),
                                _parse_hhmm_local(ov_vals[2], dt_time(23, 59, 59)),
                            )

                tok = state.get("shoonya_token")
                paper_raw = state.get("paper_broker")
                if isinstance(paper_raw, dict):
                    self.client.restore_paper_state(paper_raw)
                risk_raw = state.get("risk_session")
                restored_pos = 0
                if isinstance(risk_raw, dict):
                    restored_pos = self.risk_manager.restore_session_state(
                        risk_raw, session_date=today_str
                    )
                restored_trades = len(
                    [
                        t
                        for t in self.risk_manager.completed_trades
                        if str(t.mode or "").upper() != "BACKTEST"
                    ]
                )
                if restored_pos > 0 or restored_trades > 0:
                    self._milestone_done["reset"] = today

                logger.info(
                    "Restored persistent runtime state: MODE=%s CAP=₹%.0f IDX=%s LOTS=%s POS=%d TRADES=%d",
                    self.settings.mode,
                    self.settings.capital,
                    self.risk_manager.get_enabled_indices(),
                    self.custom_lots,
                    restored_pos,
                    restored_trades,
                )
                return tok if isinstance(tok, dict) else None

        logger.info(
            "Restored sticky settings from previous session: MODE=%s CAP=₹%.0f IDX=%s",
            self.settings.mode,
            self.settings.capital,
            self.risk_manager.get_enabled_indices(),
        )
        return None

    def _fetch_outbound_ip(self) -> str:
        """Query external IP echo services for the host's current outbound IPv4 address."""
        for url in (
            "https://api.ipify.org?format=text",
            "https://icanhazip.com",
            "https://ifconfig.me/ip",
        ):
            try:
                with urllib.request.urlopen(url, timeout=3.0) as resp:
                    ip_text = resp.read().decode("utf-8", errors="ignore").strip()
                    if ip_text and "." in ip_text and len(ip_text) <= 45:
                        return ip_text
            except Exception:
                continue
        return ""

    def _detect_public_ip(self, auto_sync_on_change: bool = False) -> None:
        """Best-effort lookup of the host outbound IPv4 address; auto-syncs Shoonya Backup IP when changed."""
        new_ip = self._fetch_outbound_ip()
        if not new_ip:
            if self.public_ip == "detecting...":
                self.public_ip = "unknown"
            return

        old_ip = self.public_ip
        self.public_ip = new_ip
        if not self.auth.last_synced_backup_ip:
            self.auth.last_synced_backup_ip = new_ip
            self.auth.last_ip_sync_status = f"ACTIVE ({new_ip})"

        if auto_sync_on_change and old_ip not in ("", "detecting...", "unknown") and new_ip != old_ip:
            self.previous_public_ip = old_ip
            logger.warning(
                "Outbound Public IP changed (%s -> %s); triggering automatic Shoonya Backup IP sync...",
                old_ip,
                new_ip,
            )
            self.sync_shoonya_backup_ip(target_ip=new_ip)

    def sync_shoonya_backup_ip(self, target_ip: str = "") -> Tuple[bool, str]:
        """Detect or use target_ip and sync it as the Shoonya Backup IP + refresh session."""
        if not self._ip_sync_lock.acquire(blocking=False):
            return False, "Backup IP sync is already in progress."
        try:
            ip_to_use = (target_ip or "").strip()
            if not ip_to_use:
                detected = self._fetch_outbound_ip()
                if detected:
                    if self.public_ip not in ("", "detecting...", "unknown") and detected != self.public_ip:
                        self.previous_public_ip = self.public_ip
                    self.public_ip = detected
                ip_to_use = self.public_ip
            else:
                if self.public_ip not in ("", "detecting...", "unknown") and ip_to_use != self.public_ip:
                    self.previous_public_ip = self.public_ip
                self.public_ip = ip_to_use

            ok, msg = self.auth.sync_backup_ip_in_shoonya(ip_to_use)
            if ok:
                self.client.get_live_broker_snapshot(force=True)
            return ok, msg
        finally:
            self._ip_sync_lock.release()

    def _milestone_scheduled_time(self, key: str) -> Optional[dt_time]:
        entry_start = getattr(self.settings, "entry_start_time", dt_time(10, 0, 0))
        mapping: Dict[str, dt_time] = {
            "auth": self.settings.auth_time,
            "cache": self.settings.instrument_cache_time,
            "ws": self.settings.ws_connect_time,
            "orb_lock": self.settings.orb_end_time,
            "scanner": entry_start,
            "squareoff": self.settings.auto_squareoff_time,
            "eod_report": self.settings.eod_report_time,
        }
        return mapping.get(key)

    def _milestone_ready(self, key: str, today: date, current_t: Optional[dt_time] = None) -> bool:
        if self._milestone_done.get(key) == today:
            return False
        override = self._milestone_overrides.get(key)
        if override and override[0] == today:
            ov_status = override[1]
            ov_t = override[2] if len(override) > 2 else dt_time(23, 59, 59)
            if ov_status in ("DONE", "ACTIVE"):
                return False
            if ov_status == "PENDING":
                sched_t = self._milestone_scheduled_time(key)
                if sched_t is not None and ov_t < sched_t:
                    # User reset this milestone to PENDING BEFORE its scheduled trigger time today:
                    # allow the scheduler to execute it automatically once current_t >= sched_t.
                    return True
                return False
        return True

    def _mark_milestone(self, key: str, today: date) -> None:
        self._milestone_done[key] = today
        self._milestone_overrides.pop(key, None)
        if key in ("reset", "auth", "scanner_alert", "squareoff", "eod_report"):
            self._persist_runtime_state(sync=False)

    def control_schedule_milestone(
        self,
        milestone_key: str,
        action: str = "restart",
        now_ist: Optional[datetime] = None,
    ) -> Tuple[bool, str]:
        """Allow user to manually run/restart, reset to PENDING, or mark DONE any daily schedule milestone."""
        now_ist = now_ist or datetime.now(IST)
        today = now_ist.date()
        now_t = now_ist.time()
        key = (milestone_key or "all").strip().lower()
        act = (action or "restart").strip().lower()

        labels = {
            "auth": "Auto OAuth Login",
            "cache": "NFO Contract Cache",
            "ws": "WS & 7D Backfill",
            "orb_lock": f"{self.settings.orb_window_minutes}-Min ORB Lock",
            "scanner": "Institutional Breakout Scan",
            "squareoff": "Auto EOD Square-Off",
        }
        all_keys = list(labels.keys())

        if key == "all":
            if act == "reset":
                with self._lock:
                    self._milestone_done.clear()
                    self._milestone_done["reset"] = today
                    for k in all_keys:
                        self._milestone_overrides[k] = (today, "PENDING", now_t)
                    self.strategy.reset_daily(today)
                logger.info("All daily schedule milestones reset to PENDING by user at %s", now_t.strftime("%H:%M:%S"))
                self._persist_runtime_state(sync=True, force_remote=True)
                return True, "All daily schedule milestones reset to PENDING."

            # Restart / re-run the morning pipeline
            with self._lock:
                self._milestone_done.clear()
                self._milestone_done["reset"] = today
                self._milestone_overrides.clear()
                self.strategy.reset_daily(today)

            self.run_daily_login(force_refresh=True)
            self._mark_milestone("auth", today)
            cnt = self.run_instrument_cache()
            self._mark_milestone("cache", today)
            self.run_ws_connect_and_backfill()
            self._mark_milestone("ws", today)
            for k in ("orb_lock", "scanner", "squareoff"):
                sched_t = self._milestone_scheduled_time(k)
                if sched_t is not None and now_t >= sched_t:
                    self._milestone_overrides[k] = (today, "PENDING", now_t)
            logger.info("Daily startup milestones (OAuth, NFO Cache=%d, WS/7D Backfill) restarted by user", cnt)
            self._persist_runtime_state(sync=True, force_remote=True)
            return True, f"Restarted morning milestones (OAuth, {cnt} NFO contracts, WS & 7D backfill)."

        if key not in labels:
            return False, f"Unknown milestone '{milestone_key}'."

        label = labels[key]
        if act == "reset":
            with self._lock:
                self._milestone_done.pop(key, None)
                self._milestone_overrides[key] = (today, "PENDING", now_t)
                if key == "orb_lock":
                    self.strategy.reset_daily(today)
            logger.info("Milestone '%s' (%s) reset to PENDING by user at %s", label, key, now_t.strftime("%H:%M:%S"))
            self._persist_runtime_state(sync=True, force_remote=True)
            return True, f"{label} reset to PENDING."

        if act == "done":
            with self._lock:
                self._mark_milestone(key, today)
                self._milestone_overrides[key] = (today, "DONE", now_t)
            logger.info("Milestone '%s' (%s) marked DONE by user", label, key)
            self._persist_runtime_state(sync=True, force_remote=True)
            return True, f"{label} marked DONE."

        # act in ("restart", "run")
        with self._lock:
            self._milestone_overrides.pop(key, None)

        if key == "auth":
            token = self.run_daily_login(force_refresh=True)
            self._mark_milestone("auth", today)
            self._milestone_overrides["auth"] = (today, "DONE", now_t)
            status_txt = self.auth.last_login_status or ("OK" if token else "COMPLETED")
            return True, f"Auto OAuth Login restarted ({status_txt})."

        if key == "cache":
            cnt = self.run_instrument_cache()
            self._mark_milestone("cache", today)
            self._milestone_overrides["cache"] = (today, "DONE", now_t)
            return True, f"NFO Contract Cache restarted ({cnt} contracts loaded)."

        if key == "ws":
            if self.client.ws_connected:
                try:
                    self.client.reconnect_websocket()
                except Exception:
                    pass
            self.run_ws_connect_and_backfill()
            self._mark_milestone("ws", today)
            self._milestone_overrides["ws"] = (today, "DONE", now_t)
            return True, "WS & 7D Backfill restarted."

        if key == "orb_lock":
            for builder in self.candle_builders.values():
                builder.flush_due_candles(now_ist)
            locked = self.run_lock_orb(now_ist)
            self._mark_milestone("orb_lock", today)
            self._milestone_overrides["orb_lock"] = (today, "DONE", now_t)
            return True, (
                "Opening Range locked across indices."
                if locked
                else "ORB lock executed (waiting for opening candles)."
            )

        if key == "scanner":
            with self._lock:
                self.risk_manager.halted = False
                self.risk_manager.halt_reason = ""
                self._milestone_overrides["scanner"] = (today, "ACTIVE", now_t)
            return True, "Institutional Breakout Scanner armed (ACTIVE)."

        if key == "squareoff":
            self.run_eod_squareoff()
            self._mark_milestone("squareoff", today)
            self._milestone_overrides["squareoff"] = (today, "DONE", now_t)
            return True, "Auto EOD Square-Off executed."

        return False, f"Unsupported action '{action}'."

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
        if vix is None:
            try:
                q = self.client.get_quotes(INDIA_VIX_EXCHANGE, INDIA_VIX_TOKEN)
                if isinstance(q, dict):
                    raw_lp = q.get("lp") or q.get("c")
                    if raw_lp is not None and float(raw_lp) > 0:
                        vix = float(raw_lp)
                        self.client.update_ltp(INDIA_VIX_KEY, vix)
            except Exception as exc:
                logger.debug("Fallback REST VIX lookup failed: %s", exc)
        signal = self.strategy.evaluate_candle_close(
            index_name=index_name,
            closed_candle=closed_candle,
            all_candles=all_closed_candles,
            india_vix=vix,
        )
        st_state = self.strategy.get_state(index_name)
        self.risk_manager.check_spot_atr_stop_exit(
            index_name=index_name,
            spot_low=float(closed_candle.low),
            spot_high=float(closed_candle.high),
        )
        self.risk_manager.check_supertrend_trailing_exit(
            index_name=index_name,
            spot_close=closed_candle.close,
            supertrend_line=st_state.last_supertrend,
            supertrend_dir=st_state.last_supertrend_dir,
        )
        if signal is None:
            return

        if self._last_triggered_bucket_by_index.get(index_name) == signal.candle_time:
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
        pos = self.risk_manager.execute_entry(signal)
        if pos is not None:
            self._last_triggered_bucket_by_index[index_name] = signal.candle_time
            self._persist_runtime_state(sync=False)

    def _backfill_all_indices(self) -> None:
        now_ist = datetime.now(IST)
        for name, builder in self.candle_builders.items():
            builder.backfill_from_api(self.client, lookback_days=7, now_ist=now_ist)
            self.strategy.warm_indicators_from_candles(name, builder.get_candles())
        if self.client.get_ltp(INDIA_VIX_KEY) is None:
            try:
                q = self.client.get_quotes(INDIA_VIX_EXCHANGE, INDIA_VIX_TOKEN)
                if isinstance(q, dict):
                    raw_lp = q.get("lp") or q.get("c")
                    if raw_lp is not None and float(raw_lp) > 0:
                        self.client.update_ltp(INDIA_VIX_KEY, float(raw_lp))
            except Exception as exc:
                logger.debug("REST VIX quote backfill skipped: %s", exc)
        if now_ist.weekday() < 5 and now_ist.time() >= self.settings.orb_end_time:
            if not all(self.strategy.get_state(n).is_locked for n in INDEX_SPECS):
                self.run_lock_orb(now_ist)

    def run_daily_login(self, force_refresh: bool = False) -> Optional[str]:
        token = self.auth.ensure_authenticated(force_refresh=force_refresh)
        if token:
            self.client.get_live_broker_snapshot(force=True)
            if self.settings.mode == "LIVE":
                self.sync_live_broker_archive(trigger_event="LOGIN", force_refresh=False, sync=False)
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
            self._persist_runtime_state(sync=True, force_remote=True)
            if self.settings.mode == "LIVE":
                self.sync_live_broker_archive(trigger_event="ACCOUNT_CONNECT", force_refresh=False, sync=False)
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

        self._persist_runtime_state(sync=True, force_remote=True)
        self.client.get_live_broker_snapshot(force=True)
        msg = f"Strategy capital updated to ₹{self.settings.capital:,.2f}; broker balances synced."
        logger.info(msg)
        return True, msg

    def refresh_broker_data(self) -> Dict[str, Any]:
        """Force refresh of live Shoonya limits, orders, and positions."""
        snap = self.client.get_live_broker_snapshot(force=True)
        if self.settings.mode == "LIVE":
            self.sync_live_broker_archive(trigger_event="MANUAL_REFRESH", force_refresh=False, sync=False)
        return snap

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
        tsym = tradingsymbol.strip().upper()
        if not tsym:
            return False, "Trading symbol is required.", {}
        if int(quantity) <= 0:
            return False, "Order quantity must be positive.", {}

        # Auto-resolve / sanitize symbol if formatted like NIFTY26NOV24500CE or NIFTY 24500 CE
        m = re.search(r"(NIFTY|BANKNIFTY)[\s\w]*?(\d{4,5})\s*(CE|PE|C|P)", tsym)
        if m and (exchange == "NFO" or "NIFTY" in tsym):
            idx_name = m.group(1)
            strike = int(m.group(2))
            opt_type = "CE" if "C" in m.group(3) else "PE"
            try:
                if self.mapper.cached_count == 0:
                    self.run_instrument_cache()
                resolved_c = self.mapper.select_option_contract(idx_name, float(strike), opt_type)
                if resolved_c and resolved_c.tradingsymbol:
                    tsym = resolved_c.tradingsymbol
            except Exception as e:
                logger.warning("Could not auto-resolve contract for %s: %s", tsym, e)

        prd = str(product_type).strip().upper()
        if prd in ("NRML", "NORMAL", "C", "CNC"):
            prd = "M"
        elif prd in ("MIS", "INTRADAY"):
            prd = "I"

        resp = self.client.place_live_broker_order(
            exchange=exchange,
            tradingsymbol=tsym,
            buy_or_sell=buy_or_sell,
            quantity=quantity,
            price_type=price_type,
            price=price,
            product_type=prd,
        )
        ok = isinstance(resp, dict) and resp.get("stat") == "Ok"
        msg = (
            f"Broker order accepted: {resp.get('norenordno')}"
            if ok
            else str(resp.get("emsg") or resp)
        )
        logger.info("Manual broker order (%s %s qty=%s): ok=%s msg=%s", buy_or_sell, tsym, quantity, ok, msg)
        if self.settings.mode == "LIVE":
            self.sync_live_broker_archive(trigger_event="MANUAL_ORDER", force_refresh=False, sync=False)
        return ok, msg, resp

    def run_instrument_cache(self) -> int:
        return self.mapper.refresh_instrument_cache()

    def run_ws_connect_and_backfill(self) -> None:
        pos_tokens = [
            f"{p.contract.exchange}|{p.contract.token}"
            for p in list(self.risk_manager.positions.values())
            if getattr(p.contract, "token", "")
        ]
        tokens = [spec.spot_key for spec in INDEX_SPECS.values()] + [INDIA_VIX_KEY] + pos_tokens
        self.client.subscribe(tokens)
        if self.settings.mode == "LIVE" or self.auth.last_token_date:
            if not self.client.ws_connected:
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
        self._persist_runtime_state(sync=False)
        if self.settings.mode == "LIVE":
            self.sync_live_broker_archive(trigger_event="EOD_SQUAREOFF", force_refresh=True, sync=True)

    def run_eod_report(self) -> None:
        csv_path = self.risk_manager.export_daily_trades_csv()
        mtm = self.risk_manager.total_mtm()
        count = len(self.risk_manager.completed_trades)
        if self.settings.mode == "LIVE":
            self.sync_live_broker_archive(trigger_event="EOD_REPORT", force_refresh=True, sync=True)
        msg = (
            f"<b>EOD Report [{self.settings.mode}]</b>\n"
            f"Trades: {count} | Total Gross P&L: ₹{mtm:+.2f}\n"
            f"CSV: {csv_path}"
        )
        logger.info("EOD Report: trades=%d mtm=%.2f csv=%s", count, mtm, csv_path)
        self.notifier.send_async(msg)

    def send_test_telegram(self) -> Tuple[bool, str]:
        """Send a synchronous test alert via Telegram to verify bot token and chat ID."""
        if not self.notifier.enabled:
            return False, "Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)."
        now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        entry_start = getattr(self.settings, "entry_start_time", dt_time(10, 0, 0)).strftime("%H:%M")
        entry_end = getattr(self.settings, "entry_cutoff_time", dt_time(13, 15, 0)).strftime("%H:%M")
        msg = (
            f"<b>✅ AutoORB Telegram Test [{self.settings.mode}]</b>\n"
            f"Account: <code>{self.settings.shoonya_user_id or 'UNLINKED'}</code> | Time: {now_str}\n"
            f"Window: {entry_start}–{entry_end} IST | Public IP: <code>{self.public_ip}</code>"
        )
        ok = self.notifier.send_sync(msg)
        if ok:
            return True, "Test Telegram alert delivered!"
        return False, "Telegram API rejected message — verify Bot Token, Chat ID, and tap Start on your bot."

    def _notify_ws_ready_async(self) -> None:
        vix = self.client.get_ltp(INDIA_VIX_KEY)
        vix_txt = f"{vix:.2f}" if vix is not None else "--"
        nf_st = self.strategy.get_state("NIFTY")
        htf_txt = (
            f" | 15m Swing RED={nf_st.htf_swing_high or '--'} GRN={nf_st.htf_swing_low or '--'}"
            if getattr(self.settings, "nifty_use_htf_swing_hybrid", False)
            else ""
        )
        self.notifier.send_async(
            f"<b>📡 WS Feed &amp; 7D Backfill Ready</b>\nVIX: {vix_txt}{htf_txt}"
        )

    def _handle_reauth(self) -> bool:
        """Triggered automatically when a 401 Unauthorized or WebSocket auth rejection occurs."""
        now_m = time.monotonic()
        last_t = getattr(self, "_last_reauth_time", 0.0)
        last_ok = getattr(self, "_last_reauth_ok", False)
        is_invalid_ip = "INVALID_IP" in str(getattr(self.auth, "last_login_status", "") or "")
        cooldown = 120.0 if last_ok else (180.0 if is_invalid_ip else 90.0)
        if last_t > 0.0 and (now_m - last_t) < cooldown:
            logger.debug("Shoonya re-auth skipped within %.0fs cooldown (last_ok=%s)", cooldown, last_ok)
            return last_ok
        if not self._reauth_lock.acquire(blocking=False):
            logger.debug("Shoonya re-auth already in progress; skipping duplicate trigger")
            return False
        try:
            self._last_reauth_time = now_m
            logger.warning("Shoonya session invalid or expired; executing automatic token renewal...")
            token = self.auth.ensure_authenticated(force_refresh=True)
            if token:
                self._last_reauth_ok = True
                logger.info("Shoonya OAuth session successfully renewed (%s)", self.settings.shoonya_user_id)
                self.client.update_credentials(self.settings)
                raw_ub = getattr(self.client.api, "_use_bearer", True)
                use_bearer = (raw_ub if isinstance(raw_ub, bool) else True) and (
                    "DYNAMIC_IP_FALLBACK" not in str(self.auth.last_login_status or "")
                )
                susertoken = getattr(self.client.api, "susertoken", "") or getattr(self.auth.api, "susertoken", "") or token
                if hasattr(self.client.api, "injectOAuthHeader"):
                    try:
                        self.client.api.injectOAuthHeader(
                            token,
                            self.settings.shoonya_user_id,
                            self.settings.shoonya_account_id,
                            susertoken=susertoken,
                            use_bearer=use_bearer,
                        )
                    except TypeError:
                        self.client.api.injectOAuthHeader(
                            token,
                            self.settings.shoonya_user_id,
                            self.settings.shoonya_account_id,
                            susertoken=susertoken,
                        )
                if not self.client.ws_connected:
                    self.client.reconnect_websocket()
                    self._backfill_all_indices()
                self._persist_runtime_state(sync=False)
                return True
            else:
                self._last_reauth_ok = False
                logger.error("Automated Shoonya re-login failed to return a valid access token")
                return False
        except Exception as exc:
            self._last_reauth_ok = False
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
        self._persist_runtime_state(sync=True, force_remote=True)
        return True, msg

    def set_orb_window(self, minutes: int) -> Tuple[bool, str]:
        """Set Opening Range duration (5, 15, 30, 45, or 60 min) keeping current start time."""
        current_start = getattr(self.settings, "orb_start_time", dt_time(9, 15, 0)).strftime("%H:%M")
        return self.set_orb_anchor(start_time=current_start, minutes=minutes)

    def get_strategy_config(self) -> Dict[str, Any]:
        """Return all user-editable strategy gates, indicators, risk caps, and session timings."""
        s = self.settings
        with self._lock:
            return {
                "orb_start_time": s.orb_start_time.strftime("%H:%M"),
                "orb_window_minutes": int(s.orb_window_minutes),
                "orb_end_time": s.orb_end_time.strftime("%H:%M"),
                "entry_start_time": s.entry_start_time.strftime("%H:%M"),
                "entry_cutoff_time": s.entry_cutoff_time.strftime("%H:%M"),
                "nifty_htf_entry_start_time": s.nifty_htf_entry_start_time.strftime("%H:%M"),
                "auto_squareoff_time": s.auto_squareoff_time.strftime("%H:%M"),
                "min_india_vix": float(s.min_india_vix),
                "min_orb_range_pct": float(s.min_orb_range_pct),
                "max_orb_range_pct": float(s.max_orb_range_pct),
                "rsi_bull_threshold": float(getattr(s, "rsi_bull_threshold", 60.0)),
                "rsi_bear_threshold": float(getattr(s, "rsi_bear_threshold", 40.0)),
                "daily_bias_filter": bool(s.daily_bias_filter),
                "use_ema_filter": bool(s.use_ema_filter),
                "min_candle_body_ratio": float(s.min_candle_body_ratio),
                "require_vwap_slope": bool(s.require_vwap_slope),
                "min_vwap_slope_pct": float(s.min_vwap_slope_pct),
                "use_supertrend_filter": bool(s.use_supertrend_filter),
                "supertrend_period": int(s.supertrend_period),
                "supertrend_multiplier": float(s.supertrend_multiplier),
                "nifty_use_htf_swing_hybrid": bool(s.nifty_use_htf_swing_hybrid),
                "nifty_use_ema_body_filter": bool(s.nifty_use_ema_body_filter),
                "nifty_htf_swing_len": int(s.nifty_htf_swing_len),
                "nifty_htf_break_buf_atr": float(s.nifty_htf_break_buf_atr),
                "nifty_htf_stop_atr_mult": float(s.nifty_htf_stop_atr_mult),
                "nifty_htf_opt_sl_cap": float(s.nifty_htf_opt_sl_cap),
                "nifty_htf_max_trades_per_day": int(s.nifty_htf_max_trades_per_day),
                "strategy_profile": str(getattr(s, "strategy_profile", "DUAL_ORB_HYBRID") or "DUAL_ORB_HYBRID"),
                "gann_box_mode": str(getattr(s, "gann_box_mode", "BREAKOUT") or "BREAKOUT"),
                "gann_confirm_candles": int(getattr(s, "gann_confirm_candles", 1)),
                "gann_hold_candles": int(getattr(s, "gann_hold_candles", 2)),
                "gann_warmup_minutes": int(getattr(s, "gann_warmup_minutes", 15)),
                "gann_use_htf_view_protection": bool(getattr(s, "gann_use_htf_view_protection", True)),
                "gann_stop_atr_mult": float(getattr(s, "gann_stop_atr_mult", 0.5)),
                "max_trades_per_day": int(s.max_trades_per_day),
                "max_weekly_losses": int(s.max_weekly_losses),
                "tuesday_expiry_boost": bool(s.tuesday_expiry_boost),
                "banknifty_be_pyramiding": bool(s.banknifty_be_pyramiding),
            }

    @staticmethod
    def _parse_strict_hhmm(raw: Any, field_name: str, lo: dt_time, hi: dt_time) -> dt_time:
        txt = str(raw or "").strip()
        try:
            parts = txt.split(":")
            if len(parts) < 2:
                raise ValueError("missing colon")
            t = dt_time(int(parts[0]), int(parts[1]), 0)
        except Exception as exc:
            raise ValueError(f"Invalid time format for {field_name}: '{txt}'. Use HH:MM.") from exc
        if t < lo or t > hi:
            raise ValueError(
                f"{field_name} ({t.strftime('%H:%M')}) must be between {lo.strftime('%H:%M')} and {hi.strftime('%H:%M')} IST."
            )
        return t

    @staticmethod
    def _coerce_bool(val: Any) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        return str(val).strip().lower() in {"1", "true", "yes", "y", "on"}

    def _apply_strategy_config_locked(self, updates: Dict[str, Any], today: date) -> bool:
        """Validate and apply strategy timings and gate overrides under self._lock. Returns True if ORB/indicators need refresh."""
        s = self.settings
        clean = {k: v for k, v in updates.items() if v is not None}

        new_orb_start = (
            self._parse_strict_hhmm(clean["orb_start_time"], "orb_start_time", dt_time(9, 15), dt_time(14, 30))
            if "orb_start_time" in clean
            else s.orb_start_time
        )
        new_orb_min = int(clean["orb_window_minutes"]) if "orb_window_minutes" in clean else int(s.orb_window_minutes)
        if new_orb_min not in {5, 15, 30, 45, 60}:
            raise ValueError("orb_window_minutes must be 5, 15, 30, 45, or 60.")

        new_entry_start = (
            self._parse_strict_hhmm(clean["entry_start_time"], "entry_start_time", dt_time(9, 15), dt_time(15, 10))
            if "entry_start_time" in clean
            else s.entry_start_time
        )
        new_cutoff = (
            self._parse_strict_hhmm(clean["entry_cutoff_time"], "entry_cutoff_time", dt_time(9, 20), dt_time(15, 20))
            if "entry_cutoff_time" in clean
            else s.entry_cutoff_time
        )
        if new_cutoff < new_entry_start:
            raise ValueError(
                f"Entry cutoff ({new_cutoff.strftime('%H:%M')}) cannot be before entry start ({new_entry_start.strftime('%H:%M')})."
            )

        if "nifty_htf_entry_start_time" in clean:
            new_nf_start = self._parse_strict_hhmm(
                clean["nifty_htf_entry_start_time"], "nifty_htf_entry_start_time", dt_time(9, 15), dt_time(15, 10)
            )
        elif "entry_start_time" in clean:
            new_nf_start = new_entry_start
        else:
            new_nf_start = s.nifty_htf_entry_start_time

        new_sqoff = (
            self._parse_strict_hhmm(clean["auto_squareoff_time"], "auto_squareoff_time", dt_time(14, 0), dt_time(15, 25))
            if "auto_squareoff_time" in clean
            else s.auto_squareoff_time
        )

        new_profile = str(clean.get("strategy_profile", getattr(s, "strategy_profile", "DUAL_ORB_HYBRID")) or "DUAL_ORB_HYBRID").strip().upper()
        if new_profile not in {"DUAL_ORB_HYBRID", "GANN_BOX_5M"}:
            raise ValueError(f"Unsupported strategy_profile '{new_profile}'. Choose DUAL_ORB_HYBRID or GANN_BOX_5M.")

        new_gann_mode = str(clean.get("gann_box_mode", getattr(s, "gann_box_mode", "BREAKOUT")) or "BREAKOUT").strip().upper()
        if new_gann_mode not in {"BREAKOUT", "BOUNCE"}:
            raise ValueError(f"Unsupported gann_box_mode '{new_gann_mode}'. Choose BREAKOUT or BOUNCE.")

        num_specs = {
            "min_india_vix": (0.0, 40.0, float),
            "min_orb_range_pct": (0.0, 5.0, float),
            "max_orb_range_pct": (0.1, 10.0, float),
            "rsi_bull_threshold": (40.0, 90.0, float),
            "rsi_bear_threshold": (10.0, 60.0, float),
            "min_candle_body_ratio": (0.0, 0.95, float),
            "min_vwap_slope_pct": (0.0, 1.0, float),
            "supertrend_period": (3, 30, int),
            "supertrend_multiplier": (1.0, 8.0, float),
            "nifty_htf_swing_len": (1, 6, int),
            "nifty_htf_break_buf_atr": (0.0, 1.0, float),
            "nifty_htf_stop_atr_mult": (0.1, 3.0, float),
            "nifty_htf_opt_sl_cap": (10.0, 150.0, float),
            "nifty_htf_max_trades_per_day": (1, 10, int),
            "gann_confirm_candles": (0, 5, int),
            "gann_hold_candles": (1, 10, int),
            "gann_warmup_minutes": (0, 240, int),
            "gann_stop_atr_mult": (0.05, 3.0, float),
            "max_trades_per_day": (1, 20, int),
            "max_weekly_losses": (0, 10, int),
        }
        parsed_nums: Dict[str, Any] = {}
        for k, (lo, hi, caster) in num_specs.items():
            if k in clean:
                try:
                    val = caster(clean[k])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid numeric value for {k}: {clean[k]}") from exc
                if val < lo or val > hi:
                    raise ValueError(f"{k} must be between {lo} and {hi} (got {val}).")
                parsed_nums[k] = val

        eff_min_orb = parsed_nums.get("min_orb_range_pct", float(s.min_orb_range_pct))
        eff_max_orb = parsed_nums.get("max_orb_range_pct", float(s.max_orb_range_pct))
        if eff_max_orb < eff_min_orb:
            raise ValueError("max_orb_range_pct cannot be less than min_orb_range_pct.")

        eff_bull = parsed_nums.get("rsi_bull_threshold", float(getattr(s, "rsi_bull_threshold", 60.0)))
        eff_bear = parsed_nums.get("rsi_bear_threshold", float(getattr(s, "rsi_bear_threshold", 40.0)))
        if eff_bull <= eff_bear:
            raise ValueError("rsi_bull_threshold must be greater than rsi_bear_threshold.")

        bool_keys = (
            "daily_bias_filter",
            "use_ema_filter",
            "require_vwap_slope",
            "use_supertrend_filter",
            "nifty_use_htf_swing_hybrid",
            "nifty_use_ema_body_filter",
            "gann_use_htf_view_protection",
            "tuesday_expiry_boost",
            "banknifty_be_pyramiding",
        )
        parsed_bools = {k: self._coerce_bool(clean[k]) for k in bool_keys if k in clean}

        orb_or_ind_changed = (
            new_orb_start != s.orb_start_time
            or new_orb_min != int(s.orb_window_minutes)
            or new_profile != str(getattr(s, "strategy_profile", "DUAL_ORB_HYBRID")).upper()
            or new_gann_mode != str(getattr(s, "gann_box_mode", "BREAKOUT")).upper()
            or any(
                k in clean
                for k in (
                    "min_orb_range_pct",
                    "max_orb_range_pct",
                    "supertrend_period",
                    "supertrend_multiplier",
                    "nifty_use_htf_swing_hybrid",
                    "nifty_htf_swing_len",
                    "gann_confirm_candles",
                    "gann_hold_candles",
                    "gann_use_htf_view_protection",
                )
            )
        )

        # Commit validated settings atomically
        s.orb_start_time = new_orb_start
        s.orb_window_minutes = new_orb_min
        end_dt = datetime.combine(today, new_orb_start) + timedelta(minutes=new_orb_min)
        s.orb_end_time = end_dt.time()
        s.entry_start_time = new_entry_start
        s.entry_cutoff_time = new_cutoff
        s.nifty_htf_entry_start_time = new_nf_start
        s.auto_squareoff_time = new_sqoff
        s.strategy_profile = new_profile
        s.gann_box_mode = new_gann_mode

        for k, val in parsed_nums.items():
            setattr(s, k, val)
        for k, val in parsed_bools.items():
            setattr(s, k, val)

        return orb_or_ind_changed

    def update_strategy_config(self, updates: Dict[str, Any]) -> Tuple[bool, str]:
        """Validate, apply, recalculate ORB/indicators, and persist all strategy gates & timings."""
        now_ist = datetime.now(IST)
        today = now_ist.date()
        try:
            with self._lock:
                refresh_needed = self._apply_strategy_config_locked(updates, today=today)
                if refresh_needed:
                    self.strategy.reset_daily(today)
                    for name, builder in self.candle_builders.items():
                        candles = builder.get_candles()
                        if candles:
                            self.strategy.warm_indicators_from_candles(name, candles)
                            if now_ist.weekday() < 5 and now_ist.time() >= self.settings.orb_end_time:
                                self.strategy.lock_opening_range(name, candles, session_date=today)
        except ValueError as exc:
            return False, str(exc)

        self._persist_runtime_state(sync=True, force_remote=True)
        cfg = self.get_strategy_config()
        msg = (
            f"Strategy ({cfg['strategy_profile']}) updated: ORB {cfg['orb_start_time']}–{cfg['orb_end_time']} ({cfg['orb_window_minutes']}m) | "
            f"Entry {cfg['entry_start_time']}–{cfg['entry_cutoff_time']} IST | "
            f"VIX≥{cfg['min_india_vix']:g} | RSI>{cfg['rsi_bull_threshold']:g}/<{cfg['rsi_bear_threshold']:g}."
        )
        logger.info(msg)
        return True, msg

    def reset_daily_trades(self) -> Tuple[bool, str]:
        """Reset daily trades count to 0 and clear any risk management halt."""
        self.risk_manager.reset_trades_counter()
        self._persist_runtime_state(sync=True, force_remote=True)
        msg = f"Daily trades counter reset to 0/{self.settings.max_trades_per_day}; strategy unhalted."
        logger.info(msg)
        return True, msg

    def set_max_daily_loss(self, amount: Optional[float]) -> Tuple[bool, str]:
        """Modify the Daily Max Loss Guardrail (₹) or pass None/0 for dynamic AUTO sizing."""
        ok, msg = self.risk_manager.set_max_daily_loss(amount)
        if ok:
            self._persist_runtime_state(sync=True, force_remote=True)
        return ok, msg

    def set_enabled_indices(
        self,
        nifty: Optional[bool] = None,
        banknifty: Optional[bool] = None,
    ) -> Tuple[bool, str]:
        """Toggle whether NIFTY and/or BANKNIFTY are enabled for live strategy execution."""
        ok, msg = self.risk_manager.set_enabled_indices(nifty=nifty, banknifty=banknifty)
        if ok:
            self._persist_runtime_state(sync=True, force_remote=True)
        return ok, msg

    def reset_risk_guardrails(self, reset_type: str = "all") -> Tuple[bool, str]:
        """Reset system halts, daily order count, weekly loss guard, and/or daily MTM loss."""
        ok, msg = self.risk_manager.reset_halt_and_limits(reset_type=reset_type)
        if ok:
            self._persist_runtime_state(sync=True, force_remote=True)
        return ok, msg

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

        self._persist_runtime_state(sync=True, force_remote=True)
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
            dynamic_max_loss = self.risk_manager.get_dynamic_max_daily_loss()
            self.settings.max_daily_loss = dynamic_max_loss

        spec = INDEX_SPECS[idx]
        tot_qty = spec.lot_size * val
        msg = f"Updated {idx} to {val} lot(s) ({tot_qty} quantity). Dynamic Max Daily Loss set to ₹{dynamic_max_loss:,.0f}."
        logger.info(msg)
        self._persist_runtime_state(sync=True, force_remote=True)
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
        self._persist_runtime_state(sync=True, force_remote=True)
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
        self._persist_runtime_state(sync=True, force_remote=True)
        if self.settings.mode == "LIVE":
            self.sync_live_broker_archive(trigger_event="AUTO_ENTRY", force_refresh=False, sync=False)
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
        self._persist_runtime_state(sync=True, force_remote=True)
        if self.settings.mode == "LIVE":
            self.sync_live_broker_archive(trigger_event="KILL_SWITCH", force_refresh=True, sync=False)

    def exit_position(self, index_name: str) -> Tuple[bool, str]:
        """Manually exit / square off an active position for an index or commodity."""
        idx = index_name.strip().upper()
        ok = self.risk_manager.exit_position(idx, reason="UI_MANUAL_EXIT")
        if ok:
            self._persist_runtime_state(sync=True, force_remote=True)
            if self.settings.mode == "LIVE":
                self.sync_live_broker_archive(trigger_event="MANUAL_EXIT", force_refresh=True, sync=False)
            msg = f"Successfully squared off {idx} position."
            logger.info(msg)
            return True, msg
        return False, f"No active position found for '{idx}'."

    def modify_position_sl(self, index_name: str, new_sl_price: float) -> Tuple[bool, str]:
        """Manually modify the stop-loss trigger price for an active position."""
        ok, msg = self.risk_manager.modify_position_sl(index_name, new_sl_price)
        if ok:
            self._persist_runtime_state(sync=True, force_remote=True)
            if self.settings.mode == "LIVE":
                self.sync_live_broker_archive(trigger_event="SL_MODIFY", force_refresh=True, sync=False)
        return ok, msg

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
        self._persist_runtime_state(sync=True, force_remote=True)
        if new_mode == "LIVE":
            self.sync_live_broker_archive(trigger_event="MODE_SWITCH_LIVE", force_refresh=True, sync=False)
        return True, f"Switched mode to {new_mode}"

    def run_minute_signal_check(self, now_ist: Optional[datetime] = None) -> Dict[str, Any]:
        """Evaluate live 1-minute spot/indicator state and trigger qualified entries."""
        dt = now_ist or datetime.now(IST)
        tick_stale = (
            not self.client.ws_connected
            or self.client.last_tick_monotonic <= 0
            or (time.monotonic() - self.client.last_tick_monotonic) > 15.0
        )

        vix = self.client.get_ltp(INDIA_VIX_KEY)
        if vix is None or tick_stale:
            try:
                q_vix = self.client.get_quotes(INDIA_VIX_EXCHANGE, INDIA_VIX_TOKEN)
                if isinstance(q_vix, dict):
                    raw_vix = q_vix.get("lp") or q_vix.get("c")
                    if raw_vix is not None and float(raw_vix) > 0:
                        vix = float(raw_vix)
                        self.client.update_ltp(INDIA_VIX_KEY, vix)
            except Exception as exc:
                logger.debug("1m VIX REST refresh skipped: %s", exc)

        for name, spec in INDEX_SPECS.items():
            if tick_stale or self.client.get_ltp(spec.spot_key) is None:
                try:
                    q_spot = self.client.get_quotes(spec.spot_exchange, spec.spot_token)
                    if isinstance(q_spot, dict):
                        raw_lp = q_spot.get("lp") or q_spot.get("c")
                        if raw_lp is not None and float(raw_lp) > 0:
                            lp = float(raw_lp)
                            vol = 0
                            try:
                                if q_spot.get("v") is not None:
                                    vol = int(float(q_spot["v"]))
                            except (TypeError, ValueError):
                                vol = 0
                            self.client.update_ltp(spec.spot_key, lp, volume=vol if vol > 0 else None)
                            self.candle_builders[name].on_tick(
                                price=lp,
                                tick_time=dt,
                                volume=vol,
                                is_cumulative_volume=True,
                            )
                except Exception as exc:
                    logger.debug("1m spot REST refresh skipped for %s: %s", name, exc)

        for builder in self.candle_builders.values():
            builder.flush_due_candles(dt)

        if dt.weekday() < 5 and self.settings.orb_end_time <= dt.time() < self.settings.market_close_time:
            for name, builder in self.candle_builders.items():
                st_cur = self.strategy.get_state(name)
                if not st_cur.is_locked and builder.get_candles():
                    self.strategy.lock_opening_range(name, builder.get_candles(), session_date=dt.date())

        # Refresh active option LTP via REST when WS ticks are stale so PAPER/LIVE MTM & SL stay live
        for pos_key, pos in list(self.risk_manager.positions.items()):
            c = pos.contract
            if not c.token or str(c.token).startswith("SIM_"):
                continue
            opt_key = f"{c.exchange}|{c.token}"
            if tick_stale or self.client.get_ltp(opt_key) is None:
                try:
                    q_opt = self.client.get_quotes(c.exchange, c.token)
                    if isinstance(q_opt, dict):
                        raw_opt = (
                            q_opt.get("lp")
                            or q_opt.get("bp1")
                            or q_opt.get("sp1")
                            or q_opt.get("c")
                        )
                        if raw_opt is not None and float(raw_opt) > 0:
                            opt_lp = float(raw_opt)
                            self.client.update_ltp(opt_key, opt_lp)
                            self.risk_manager.on_option_tick(pos_key, opt_lp)
                except Exception as exc:
                    logger.debug("1m option REST refresh skipped for %s: %s", c.tradingsymbol, exc)

        vix = self.client.get_ltp(INDIA_VIX_KEY)
        summary_parts = []
        triggered = []
        for name, builder in self.candle_builders.items():
            candles_open = builder.get_candles(include_open=True)
            if not candles_open:
                continue
            signal = self.strategy.evaluate_live_1m_check(
                index_name=name,
                candles_with_open=candles_open,
                india_vix=vix,
                current_time_ist=dt,
            )
            st_state = self.strategy.get_state(name)
            latest_c = candles_open[-1]
            self.risk_manager.check_spot_atr_stop_exit(
                index_name=name,
                spot_low=float(latest_c.low),
                spot_high=float(latest_c.high),
            )
            self.risk_manager.check_supertrend_trailing_exit(
                index_name=name,
                spot_close=float(latest_c.close),
                supertrend_line=st_state.last_supertrend,
                supertrend_dir=st_state.last_supertrend_dir,
            )
            spot_txt = f"{st_state.last_spot:.1f}" if st_state.last_spot is not None else "--"
            rsi_txt = f"{st_state.last_rsi:.1f}" if st_state.last_rsi is not None else "--"
            st_txt = f"{st_state.last_supertrend:.1f}" if st_state.last_supertrend is not None else "--"
            summary_parts.append(f"{name}={spot_txt}(RSI={rsi_txt},ST={st_txt})")

            if (
                signal is not None
                and self._last_triggered_bucket_by_index.get(name) != signal.candle_time
                and name not in self.risk_manager.positions
            ):
                logger.info(
                    "[1M SIGNAL] %s %s spot=%.2f ORH=%.2f ORL=%.2f VWAP=%.2f RSI=%.1f VIX=%.2f",
                    signal.index_name,
                    signal.signal_type,
                    signal.spot_close,
                    signal.orh,
                    signal.orl,
                    signal.vwap,
                    signal.rsi,
                    signal.india_vix,
                )
                pos = self.risk_manager.execute_entry(signal)
                if pos is not None:
                    self._last_triggered_bucket_by_index[name] = signal.candle_time
                    triggered.append(signal.index_name)
                    self._persist_runtime_state(sync=False)

        self.last_signal_check_time = dt.strftime("%H:%M:%S")
        vix_txt = f"{vix:.2f}" if vix is not None else "--"
        logger.info(
            "[1M SIGNAL CHECK] %s | VIX=%s | %s%s",
            self.last_signal_check_time,
            vix_txt,
            " | ".join(summary_parts) if summary_parts else "Waiting for candles",
            f" | TRIGGERED={','.join(triggered)}" if triggered else "",
        )
        return {"time": self.last_signal_check_time, "vix": vix, "triggered": triggered}

    def step_scheduler(self, now_ist: Optional[datetime] = None) -> None:
        """Advance scheduled milestones and 1s/10s/15s/60s operational checks."""
        dt = now_ist or datetime.now(IST)
        today = dt.date()
        current_t = dt.time()

        # Check outbound public IP every 300s (5 min) and auto-sync Shoonya Backup IP if IP changed
        now_mono = time.monotonic()
        if now_mono - self._last_ip_check_monotonic >= 300.0:
            self._last_ip_check_monotonic = now_mono
            threading.Thread(
                target=self._detect_public_ip,
                kwargs={"auto_sync_on_change": True},
                name="periodic-ip-watcher",
                daemon=True,
            ).start()

        # Self keep-alive ping during market prep/hours or while positions are open
        if not os.getenv("PYTEST_CURRENT_TEST"):
            ping_base = (
                os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
            ).strip().rstrip("/")
            if ping_base:
                in_keepalive_window = (
                    is_nse_trading_day(today)
                    and dt_time(7, 30, 0) <= current_t <= dt_time(16, 0, 0)
                ) or bool(self.risk_manager.positions)
                if in_keepalive_window and (now_mono - self._last_self_ping_monotonic >= 480.0):
                    self._last_self_ping_monotonic = now_mono
                    threading.Thread(
                        target=self._send_keepalive_ping,
                        args=(f"{ping_base}/health",),
                        name="self-keepalive-ping",
                        daemon=True,
                    ).start()

        # Skip trading schedule on weekends and official NSE holidays
        if is_nse_trading_day(today):
            if self._milestone_ready("reset", today, current_t):
                if not self.risk_manager.positions:
                    self._milestone_done = {k: v for k, v in self._milestone_done.items() if v == today}
                    self._milestone_overrides = {
                        k: v for k, v in self._milestone_overrides.items() if v[0] == today
                    }
                    self._last_triggered_bucket_by_index.clear()
                    self.strategy.reset_daily(today)
                    self.risk_manager.reset_daily()
                    self._mark_milestone("reset", today)

            if current_t >= self.settings.auth_time and self._milestone_ready("auth", today, current_t):
                now_ts = time.time()
                if now_ts - getattr(self, "_last_auth_attempt", 0.0) >= 120.0:
                    self._last_auth_attempt = now_ts
                    logger.info("Scheduled 08:40 IST Auto OAuth Login triggered")
                    need_refresh = self.auth.last_token_date != today.strftime("%Y-%m-%d")
                    token = self.run_daily_login(force_refresh=need_refresh)
                    paper_no_creds = self.settings.mode == "PAPER" and not self.settings.shoonya_user_id
                    if token or paper_no_creds or current_t >= self.settings.market_close_time:
                        self._mark_milestone("auth", today)
                        if not need_refresh or paper_no_creds:
                            acct = self.settings.shoonya_user_id or "PAPER_SANDBOX"
                            status_txt = self.auth.last_login_status or "ACTIVE"
                            self.notifier.send_async(
                                f"<b>🔑 08:40 IST Auto OAuth Ready [{self.settings.mode}]</b>\n"
                                f"Account: <code>{acct}</code> | Status: {status_txt}"
                            )
                    else:
                        logger.warning("Auto OAuth Login incomplete; scheduler will retry in 120s")

            if current_t >= self.settings.instrument_cache_time and self._milestone_ready("cache", today, current_t):
                now_ts = time.time()
                if now_ts - getattr(self, "_last_cache_attempt", 0.0) >= 60.0:
                    self._last_cache_attempt = now_ts
                    logger.info("Scheduled 08:55 IST NFO Contract Cache triggered")
                    cnt = self.run_instrument_cache()
                    if cnt > 0 or current_t >= self.settings.market_close_time:
                        self._mark_milestone("cache", today)
                        if cnt > 0:
                            self.notifier.send_async(
                                f"<b>📦 NFO Contract Cache Ready</b>\nLoaded {cnt:,} contracts for {today}."
                            )
                    else:
                        logger.warning("NFO Contract Cache returned 0 contracts; scheduler will retry in 60s")

            if current_t >= self.settings.ws_connect_time and self._milestone_ready("ws", today, current_t):
                if getattr(self, "_last_ws_attempt_date", None) == today and self.client.ws_connected:
                    self._mark_milestone("ws", today)
                    self._notify_ws_ready_async()
                else:
                    auth_ready = (
                        self.settings.mode == "PAPER"
                        or self._milestone_done.get("auth") == today
                        or self.auth.last_token_date == today.strftime("%Y-%m-%d")
                        or bool(self.settings.shoonya_access_token)
                    )
                    now_ts = time.time()
                    if (auth_ready or current_t >= self.settings.market_close_time) and (
                        now_ts - getattr(self, "_last_ws_attempt", 0.0) >= 30.0
                    ):
                        self._last_ws_attempt = now_ts
                        self._last_ws_attempt_date = today
                        logger.info("Scheduled 09:14 IST WS & 7D Backfill triggered")
                        self.run_ws_connect_and_backfill()
                        if (
                            self.client.ws_connected
                            or self.settings.mode == "PAPER"
                            or current_t >= self.settings.market_close_time
                        ):
                            self._mark_milestone("ws", today)
                            if current_t < self.settings.market_close_time:
                                self._notify_ws_ready_async()
                        else:
                            logger.warning("WS connection pending/retrying; scheduler will recheck in 30s")

            if current_t >= self.settings.orb_end_time and self._milestone_ready("orb_lock", today, current_t):
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

            entry_start = getattr(self.settings, "entry_start_time", dt_time(10, 0, 0))
            if (
                entry_start <= current_t <= self.settings.entry_cutoff_time
                and self._milestone_ready("scanner_alert", today, current_t)
            ):
                self._mark_milestone("scanner_alert", today)
                vix_now = self.client.get_ltp(INDIA_VIX_KEY)
                vix_txt = f"{vix_now:.2f}" if vix_now is not None else "--"
                self.notifier.send_async(
                    f"<b>🟢 AutoORB Scanner LIVE ({entry_start.strftime('%H:%M')}–{self.settings.entry_cutoff_time.strftime('%H:%M')} IST)</b>\n"
                    f"Mode: {self.settings.mode} | VIX: {vix_txt} | NIFTY Hybrid H1 &amp; BANKNIFTY 5-Gate ORB armed."
                )

            if current_t >= self.settings.auto_squareoff_time and self._milestone_ready("squareoff", today, current_t):
                logger.info("Scheduled 15:15 IST Auto EOD Square-Off triggered")
                self.run_eod_squareoff()
                self._mark_milestone("squareoff", today)

            if current_t >= self.settings.eod_report_time and self._milestone_ready("eod_report", today, current_t):
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
                if now_m - self._last_signal_check_monotonic >= self.settings.signal_check_interval_sec:
                    self._last_signal_check_monotonic = now_m
                    self.run_minute_signal_check(dt)

                if now_m - self._last_reconcile_monotonic >= self.settings.reconcile_interval_sec:
                    self.risk_manager.reconcile_positions()
                    self._last_reconcile_monotonic = now_m

                if (
                    self.settings.mode == "LIVE"
                    and (now_m - self._last_broker_archive_monotonic >= 300.0)
                ):
                    self._last_broker_archive_monotonic = now_m
                    threading.Thread(
                        target=self.sync_live_broker_archive,
                        kwargs={"trigger_event": "PERIODIC", "force_refresh": True, "sync": False},
                        name="broker-archive-sync",
                        daemon=True,
                    ).start()

    def start_background_loop(self) -> None:
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return

        def _loop() -> None:
            logger.info("TradingSystemOrchestrator loop started (MODE=%s)", self.settings.mode)
            self.notifier.send_async(
                f"<b>🚀 AutoORB Engine Started [{self.settings.mode}]</b>\n"
                f"Account: <code>{self.settings.shoonya_user_id or 'UNLINKED'}</code> | Capital: ₹{self.settings.capital:,.0f}\n"
                f"Strategy: Option 2 Dual Engine (10:00–13:15 IST, VIX ≥ {self.settings.min_india_vix:.0f}, ST 10,3)\n"
                f"Schedule: 08:40 Auth → 08:55 Cache → 09:14 WS/7D → 09:45 ORB → 10:00 Scan"
            )
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

    @staticmethod
    def _send_keepalive_ping(url: str) -> None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AutoORB-KeepAlive/1.0"})
            with urllib.request.urlopen(req, timeout=8.0):
                pass
        except Exception as exc:
            logger.debug("Self keep-alive ping failed: %s", exc)

    def _build_index_radar(
        self,
        index_name: str,
        spot_ltp: Optional[float],
        vix: Optional[float],
        now_ist: datetime,
    ) -> Dict[str, Any]:
        """Compute live 5-gate auto-order readiness metrics and ATM option protection state."""
        spec = INDEX_SPECS[index_name]
        st = self.strategy.get_state(index_name)
        is_gann = (str(getattr(self.settings, "strategy_profile", "DUAL_ORB_HYBRID") or "DUAL_ORB_HYBRID").upper() == "GANN_BOX_5M")
        is_hybrid = (
            not is_gann
            and index_name == "NIFTY"
            and bool(getattr(self.settings, "nifty_use_htf_swing_hybrid", False))
        )
        strategy_mode = "GANN_BOX_5M" if is_gann else ("HTF_SWING_HYBRID" if is_hybrid else "ORB_5GATE")

        htf_view = int(getattr(st, "htf_view", 0) or st.htf_bias or 0)
        if spot_ltp is not None:
            if st.htf_swing_high is not None and spot_ltp > st.htf_swing_high:
                htf_view = 1
            elif st.htf_swing_low is not None and spot_ltp < st.htf_swing_low:
                htf_view = -1
        htf_view_label = "BULLISH" if htf_view == 1 else ("BEARISH" if htf_view == -1 else "NEUTRAL")
        use_gann_prot = bool(getattr(self.settings, "gann_use_htf_view_protection", True))

        system_ready = (
            not self.risk_manager.halted
            and self.risk_manager.trades_taken_today < self.settings.max_trades_per_day
            and index_name not in self.risk_manager.positions
        )
        vix_ready = (vix is not None and vix >= self.settings.min_india_vix)
        if is_gann:
            orb_ready = bool(st.gann_vah is not None and st.gann_val is not None and st.gann_vah > st.gann_val)
            entry_start = getattr(self.settings, "entry_start_time", dt_time(9, 30, 0))
            entry_end = getattr(self.settings, "entry_cutoff_time", dt_time(15, 0, 0))
            in_window = is_nse_trading_day(now_ist.date()) and (entry_start <= now_ist.time() <= entry_end)
            ce_price_ok = bool(spot_ltp is not None and st.gann_vah is not None and spot_ltp > st.gann_vah)
            pe_price_ok = bool(spot_ltp is not None and st.gann_val is not None and spot_ltp < st.gann_val)
            ce_rsi_ok = (htf_view == 1) if use_gann_prot else True
            pe_rsi_ok = (htf_view == -1) if use_gann_prot else True
            dist_to_orh = round(st.gann_vah - spot_ltp, 2) if (spot_ltp is not None and st.gann_vah is not None) else None
            dist_to_orl = round(spot_ltp - st.gann_val, 2) if (spot_ltp is not None and st.gann_val is not None) else None
        elif is_hybrid:
            orb_ready = bool(st.htf_swing_high is not None and st.htf_swing_low is not None and st.htf_atr is not None)
            entry_start = getattr(self.settings, "nifty_htf_entry_start_time", dt_time(10, 0, 0))
            entry_end = getattr(self.settings, "entry_cutoff_time", dt_time(13, 15, 0))
            in_window = is_nse_trading_day(now_ist.date()) and (entry_start <= now_ist.time() <= entry_end)
            buf_pts = (float(getattr(self.settings, "nifty_htf_break_buf_atr", 0.1) or 0.1) * st.htf_atr) if st.htf_atr else 0.0
            ce_price_ok = bool(spot_ltp is not None and st.htf_swing_high is not None and spot_ltp > (st.htf_swing_high + buf_pts))
            pe_price_ok = bool(spot_ltp is not None and st.htf_swing_low is not None and spot_ltp < (st.htf_swing_low - buf_pts))
            ce_rsi_ok = bool(st.last_supertrend_dir == 1) if getattr(self.settings, "use_supertrend_filter", True) else True
            pe_rsi_ok = bool(st.last_supertrend_dir == -1) if getattr(self.settings, "use_supertrend_filter", True) else True
            dist_to_orh = round(st.htf_swing_high - spot_ltp, 2) if (spot_ltp is not None and st.htf_swing_high is not None) else None
            dist_to_orl = round(spot_ltp - st.htf_swing_low, 2) if (spot_ltp is not None and st.htf_swing_low is not None) else None
        else:
            orb_ready = bool(st.is_locked and not st.no_trade_reason)
            entry_start = getattr(self.settings, "entry_start_time", dt_time(10, 0, 0))
            entry_end = getattr(self.settings, "entry_cutoff_time", dt_time(13, 15, 0))
            in_window = (
                is_nse_trading_day(now_ist.date())
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
            rsi_bull = float(getattr(self.settings, "rsi_bull_threshold", 60.0) or 60.0)
            rsi_bear = float(getattr(self.settings, "rsi_bear_threshold", 40.0) or 40.0)
            ce_rsi_ok = bool(st.last_rsi is not None and (st.last_rsi > rsi_bull))
            pe_rsi_ok = bool(st.last_rsi is not None and (st.last_rsi < rsi_bear))
            dist_to_orh = round(st.orh - spot_ltp, 2) if (spot_ltp is not None and st.orh is not None) else None
            dist_to_orl = round(spot_ltp - st.orl, 2) if (spot_ltp is not None and st.orl is not None) else None

        # Dynamic Position Sizing based on available funds:
        # Tuesday: 75% allocation; Mon/Wed/Thu/Fri: 50% allocation
        avail_fund = self.risk_manager.get_available_fund()
        is_tuesday = (now_ist.weekday() == 1)
        alloc_pct = 75 if is_tuesday else 50
        alloc_fund = round(avail_fund * (alloc_pct / 100.0), 2)

        actual_symbol_ce = ""
        actual_symbol_pe = ""
        atm_premium = 120.0 if index_name == "NIFTY" else 260.0
        if spot_ltp is not None and spot_ltp > 0:
            try:
                c = self.mapper.select_option_contract(index_name, spot_ltp, "CE", vix=vix or 15.0)
                actual_symbol_ce = c.tradingsymbol
                token_key = f"{c.exchange}|{c.token}"
                cached_ltp = self.client.get_ltp(token_key)
                if cached_ltp is not None and cached_ltp > 0:
                    atm_premium = float(cached_ltp)
            except Exception:
                pass
            try:
                c_pe = self.mapper.select_option_contract(index_name, spot_ltp, "PE", vix=vix or 15.0)
                actual_symbol_pe = c_pe.tradingsymbol
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

        atm_strike = (
            round_to_strike(spot_ltp, spec.strike_step)
            if (spot_ltp is not None and spot_ltp > 0)
            else None
        )
        candidate_ce = "--"
        candidate_pe = "--"
        if atm_strike is not None:
            lots_lbl = f"{lots} lot{'s' if lots > 1 else ''}"
            candidate_ce = f"{index_name} {atm_strike} CE ({total_qty} qty / {lots_lbl} [{product_type_label}])"
            candidate_pe = f"{index_name} {atm_strike} PE ({total_qty} qty / {lots_lbl} [{product_type_label}])"

        rec_side = "CE" if htf_view == 1 else ("PE" if htf_view == -1 else None)
        rec_symbol = actual_symbol_ce if rec_side == "CE" else (actual_symbol_pe if rec_side == "PE" else "")
        rec_label = (
            f"BUY ATM {atm_strike} {rec_side}"
            if (atm_strike is not None and rec_side is not None)
            else (f"ATM {atm_strike} — WAIT (NO VIEW)" if atm_strike is not None else "WAIT")
        )
        atm_prot_ok = (ce_rsi_ok if ce_price_ok else (pe_rsi_ok if pe_price_ok else (htf_view != 0 if use_gann_prot else True)))

        ce_score = sum([system_ready, vix_ready, orb_ready, ce_price_ok, ce_rsi_ok])
        pe_score = sum([system_ready, vix_ready, orb_ready, pe_price_ok, pe_rsi_ok])
        readiness_pct = int(round((max(ce_score, pe_score) / 5.0) * 100))

        if index_name in self.risk_manager.positions:
            radar_state = "ACTIVE_POSITION"
            radar_summary = (
                "1st 5m Gann Box active — Spot Mid-Box ATR SL & Supertrend Trail armed."
                if is_gann
                else (
                    "Hybrid H1 active — Spot ATR SL, 35pt Cap & Supertrend(10,3) Close Trail armed."
                    if is_hybrid
                    else "5-Gate ORB active — 45pt SL, BE (+45), T1 50% (+80) & Supertrend(10,3) Line Trail armed."
                )
            )
        elif self.risk_manager.halted or self.risk_manager.trades_taken_today >= self.settings.max_trades_per_day:
            radar_state = "HALTED_OR_MAX_TRADES"
            radar_summary = f"Auto entries paused: {self.risk_manager.halt_reason or 'Max daily trades reached'}."
        elif is_gann:
            prot_note = f"HTF View {htf_view_label} ({rec_label})" if use_gann_prot else "Protection OFF"
            if not orb_ready:
                radar_state = "WAITING_5M_BOX"
                radar_summary = "Waiting for 1st 5-minute opening candle (09:15-09:20 IST) to lock VAH/VAL box."
            elif ce_price_ok:
                radar_state = "IMMINENT_CE_BREAKOUT"
                radar_summary = (
                    f"1st 5m VAH break ({st.gann_vah}) + {prot_note}! Run {st.gann_up_run}."
                    if (ce_rsi_ok and vix_ready and in_window)
                    else f"Above 1st 5m VAH ({st.gann_vah}) — {prot_note} (run {st.gann_up_run})."
                )
            elif pe_price_ok:
                radar_state = "IMMINENT_PE_BREAKOUT"
                radar_summary = (
                    f"1st 5m VAL break ({st.gann_val}) + {prot_note}! Run {st.gann_dn_run}."
                    if (pe_rsi_ok and vix_ready and in_window)
                    else f"Below 1st 5m VAL ({st.gann_val}) — {prot_note} (run {st.gann_dn_run})."
                )
            else:
                radar_state = "SCANNING_RANGE"
                radar_summary = f"1st 5m Gann Box [{st.gann_val}–{st.gann_vah}] | {prot_note}."
        elif is_hybrid:
            if not orb_ready:
                radar_state = "WAITING_15M_SWINGS"
                radar_summary = "Warming 15m HTF swing high/low & ATR(14) levels for Hybrid H1 breakout scan."
            elif ce_price_ok:
                radar_state = "IMMINENT_CE_BREAKOUT"
                radar_summary = (
                    f"15m Red break + ST(10,3) Bull + VIX >= 12! Auto-BUY {candidate_ce} arms on 5m close."
                    if (ce_rsi_ok and vix_ready and in_window)
                    else f"Above 15m Red ({st.htf_swing_high:.1f}) — awaiting ST(10,3) Bull & VIX >= 12."
                )
            elif pe_price_ok:
                radar_state = "IMMINENT_PE_BREAKOUT"
                radar_summary = (
                    f"15m Green break + ST(10,3) Bear + VIX >= 12! Auto-BUY {candidate_pe} arms on 5m close."
                    if (pe_rsi_ok and vix_ready and in_window)
                    else f"Below 15m Green ({st.htf_swing_low:.1f}) — awaiting ST(10,3) Bear & VIX >= 12."
                )
            else:
                radar_state = "SCANNING_RANGE"
                orh_txt = f"{dist_to_orh:+.2f}" if dist_to_orh is not None else "--"
                orl_txt = f"{dist_to_orl:+.2f}" if dist_to_orl is not None else "--"
                radar_summary = f"15m Swing Band: {orh_txt} pts to Red ({st.htf_swing_high}) | {orl_txt} pts above Green ({st.htf_swing_low})."
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
                radar_summary = f"ALL GATES MET! Auto-BUY {candidate_ce} arms on 5m close."
            else:
                rsi_txt = f"{st.last_rsi:.1f}" if st.last_rsi is not None else "--"
                rsi_b = float(getattr(self.settings, "rsi_bull_threshold", 60.0) or 60.0)
                radar_summary = f"Above ORH & VWAP! Need RSI > {rsi_b:g} (now {rsi_txt}) & 5m close -> {candidate_ce}."
        elif orb_ready and pe_price_ok:
            radar_state = "IMMINENT_PE_BREAKOUT"
            if pe_rsi_ok and vix_ready and in_window:
                radar_summary = f"ALL GATES MET! Auto-BUY {candidate_pe} arms on 5m close."
            else:
                rsi_txt = f"{st.last_rsi:.1f}" if st.last_rsi is not None else "--"
                rsi_br = float(getattr(self.settings, "rsi_bear_threshold", 40.0) or 40.0)
                radar_summary = f"Below ORL & VWAP! Need RSI < {rsi_br:g} (now {rsi_txt}) & 5m close -> {candidate_pe}."
        else:
            radar_state = "SCANNING_RANGE"
            orh_txt = f"{dist_to_orh:+.2f}" if dist_to_orh is not None else "--"
            orl_txt = f"{dist_to_orl:+.2f}" if dist_to_orl is not None else "--"
            radar_summary = f"Inside range: {orh_txt} pts to ORH (CE) | {orl_txt} pts above ORL (PE)."

        return {
            "strategy_mode": strategy_mode,
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
            "htf_swing_high": st.htf_swing_high,
            "htf_swing_low": st.htf_swing_low,
            "htf_atr": st.htf_atr,
            "htf_view": htf_view,
            "htf_view_label": htf_view_label,
            "atm_strike": atm_strike,
            "recommended_option_side": rec_side,
            "recommended_symbol": rec_symbol,
            "recommended_label": rec_label,
            "atm_protection_ok": atm_prot_ok,
            "gann_vah": st.gann_vah,
            "gann_val": st.gann_val,
            "gann_mid": st.gann_mid,
            "gann_up_run": st.gann_up_run,
            "gann_dn_run": st.gann_dn_run,
            "gann_t1_spot": st.gann_t1_spot,
            "gann_t2_spot": st.gann_t2_spot,
            "supertrend_value": st.last_supertrend,
            "supertrend_dir": st.last_supertrend_dir,
            "candidate_ce": candidate_ce,
            "candidate_pe": candidate_pe,
            "actual_symbol_ce": actual_symbol_ce,
            "actual_symbol_pe": actual_symbol_pe,
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
        is_holiday = is_weekday and (today in NSE_HOLIDAYS_2026)
        is_market_day = is_nse_trading_day(today)
        entry_start = getattr(self.settings, "entry_start_time", dt_time(10, 0, 0))
        entry_cutoff = getattr(self.settings, "entry_cutoff_time", dt_time(13, 15, 0))

        if not is_weekday:
            phase = "WEEKEND_STANDBY"
        elif is_holiday:
            phase = "NSE_HOLIDAY (MARKET CLOSED)"
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
        if is_market_day and is_trading_hour:
            sec_into_5m = (now_ist.minute % 5) * 60 + now_ist.second
            next_candle_close_sec = max(1, 300 - sec_into_5m)

        all_locked = all(self.strategy.get_state(name).is_locked for name in INDEX_SPECS)

        def _ms_status(key: str, done_cond: bool, active_cond: bool = False) -> str:
            override = self._milestone_overrides.get(key)
            if override and override[0] == today:
                ov_status = override[1]
                ov_t = override[2] if len(override) > 2 else dt_time(23, 59, 59)
                sched_t = self._milestone_scheduled_time(key)
                if ov_status == "PENDING" and sched_t is not None and ov_t < sched_t and t >= sched_t:
                    # Pre-schedule PENDING override has now been reached by the IST clock
                    pass
                else:
                    return ov_status
            if done_cond:
                return "DONE"
            if active_cond:
                return "ACTIVE"
            return "PENDING"

        milestones = [
            {
                "key": "auth",
                "time": "08:40 IST",
                "label": "Auto OAuth Login",
                "status": _ms_status(
                    "auth",
                    self._milestone_done.get("auth") == today
                    or (
                        t >= self.settings.auth_time
                        and bool(self.auth.last_token_date == today.strftime("%Y-%m-%d"))
                    ),
                ),
            },
            {
                "key": "cache",
                "time": "08:55 IST",
                "label": "NFO Contract Cache",
                "status": _ms_status(
                    "cache",
                    self._milestone_done.get("cache") == today
                    or (t >= self.settings.instrument_cache_time and self.mapper.cached_count > 0),
                ),
            },
            {
                "key": "ws",
                "time": "09:14 IST",
                "label": "WS & 7D Backfill",
                "status": _ms_status(
                    "ws",
                    self._milestone_done.get("ws") == today
                    or (t >= self.settings.ws_connect_time and self.client.ws_connected),
                ),
            },
            {
                "key": "orb_lock",
                "time": f"{self.settings.orb_end_time.strftime('%H:%M')} IST",
                "label": f"{self.settings.orb_window_minutes}-Min ORB Lock",
                "status": _ms_status(
                    "orb_lock",
                    all_locked or self._milestone_done.get("orb_lock") == today,
                ),
            },
            {
                "key": "scanner",
                "time": f"{entry_start.strftime('%H:%M')}-{entry_cutoff.strftime('%H:%M')}",
                "label": "Institutional Breakout Scan",
                "status": _ms_status(
                    "scanner",
                    is_market_day and t > entry_cutoff,
                    active_cond=is_market_day
                    and entry_start <= t <= entry_cutoff,
                ),
            },
            {
                "key": "squareoff",
                "time": "15:15 IST",
                "label": "Auto EOD Square-Off",
                "status": _ms_status(
                    "squareoff",
                    self._milestone_done.get("squareoff") == today
                    or (is_market_day and t >= self.settings.auto_squareoff_time),
                ),
            },
        ]

        next_signal_check_sec: Optional[int] = None
        if is_market_day and is_trading_hour:
            if self._last_signal_check_monotonic > 0:
                next_signal_check_sec = max(
                    0,
                    int(round(self.settings.signal_check_interval_sec - (time.monotonic() - self._last_signal_check_monotonic))),
                )
            else:
                next_signal_check_sec = 0

        return {
            "phase": phase,
            "is_market_day": is_market_day,
            "next_candle_close_sec": next_candle_close_sec,
            "signal_check_interval_sec": int(self.settings.signal_check_interval_sec),
            "last_signal_check_time": self.last_signal_check_time,
            "next_signal_check_sec": next_signal_check_sec,
            "milestones": milestones,
            "auto_mode_note": (
                f"Option 2 Dual Engine scans every 60s + 5m candle closes "
                f"({entry_start.strftime('%H:%M')}-{entry_cutoff.strftime('%H:%M')} IST, VIX >= {self.settings.min_india_vix:.0f}, ST 10,3) "
                f"in {self.settings.mode} mode: NIFTY 15m HTF Swing Hybrid & BANKNIFTY 5-Gate ORB."
            ),
        }

    def get_status_snapshot(self) -> Dict[str, Any]:
        """Return thread-safe JSON snapshot for the live FastAPI Web UI."""
        now_ist = datetime.now(IST)
        vix = self.client.get_ltp(INDIA_VIX_KEY)
        active_profile = str(getattr(self.settings, "strategy_profile", "DUAL_ORB_HYBRID") or "DUAL_ORB_HYBRID").upper()
        indices_data: Dict[str, Any] = {}
        atm_panel_indices: Dict[str, Any] = {}
        for name, spec in INDEX_SPECS.items():
            st = self.strategy.get_state(name)
            spot_ltp = self.client.get_ltp(spec.spot_key) or st.last_spot
            is_hybrid = (
                active_profile != "GANN_BOX_5M"
                and name == "NIFTY"
                and bool(getattr(self.settings, "nifty_use_htf_swing_hybrid", False))
            )
            idx_mode = "GANN_BOX_5M" if active_profile == "GANN_BOX_5M" else ("HTF_SWING_HYBRID" if is_hybrid else "ORB_5GATE")
            radar = self._build_index_radar(name, spot_ltp, vix, now_ist)
            indices_data[name] = {
                "strategy_mode": idx_mode,
                "spot_ltp": spot_ltp,
                "orh": st.orh,
                "orl": st.orl,
                "htf_swing_high": st.htf_swing_high,
                "htf_swing_low": st.htf_swing_low,
                "htf_atr": st.htf_atr,
                "htf_bias": st.htf_bias,
                "htf_view": radar.get("htf_view", 0),
                "gann_vah": st.gann_vah,
                "gann_val": st.gann_val,
                "gann_mid": st.gann_mid,
                "gann_up_run": st.gann_up_run,
                "gann_dn_run": st.gann_dn_run,
                "gann_t1_spot": st.gann_t1_spot,
                "gann_t2_spot": st.gann_t2_spot,
                "supertrend_value": st.last_supertrend,
                "supertrend_dir": st.last_supertrend_dir,
                "range_pct": st.range_pct,
                "is_locked": st.is_locked,
                "no_trade_reason": st.no_trade_reason,
                "vwap": st.last_vwap,
                "rsi": st.last_rsi,
                "last_signal": st.last_signal,
                "last_eval_time": st.last_eval_time,
                "radar": radar,
            }
            atm_panel_indices[name] = {
                "spot_ltp": spot_ltp,
                "atm_strike": radar.get("atm_strike"),
                "htf_view": radar.get("htf_view", 0),
                "htf_view_label": radar.get("htf_view_label", "NEUTRAL"),
                "htf_swing_high": st.htf_swing_high,
                "htf_swing_low": st.htf_swing_low,
                "gann_vah": st.gann_vah,
                "gann_mid": st.gann_mid,
                "gann_val": st.gann_val,
                "gann_up_run": st.gann_up_run,
                "gann_dn_run": st.gann_dn_run,
                "recommended_side": radar.get("recommended_option_side"),
                "recommended_symbol": radar.get("recommended_symbol", ""),
                "recommended_label": radar.get("recommended_label", "WAIT"),
                "atm_protection_ok": radar.get("atm_protection_ok", False),
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
            "telegram_enabled": self.notifier.enabled,
            "remote_state_synced": bool(getattr(self.state_store, "remote_load_ok", False)),
            "broker_archive_last_sync": self.broker_archive.last_archive_time_ist,
            "public_ip": self.public_ip,
            "previous_public_ip": self.previous_public_ip,
            "last_synced_backup_ip": self.auth.last_synced_backup_ip or self.public_ip,
            "backup_ip_status": self.auth.last_ip_sync_status,
            "india_vix": vix,
            "avail_fund": round(self.risk_manager.get_available_fund(), 2),
            "weekly_guard": self.risk_manager.get_weekly_guard_status(now_ist),
            "mtm_pnl": self.risk_manager.total_mtm(),
            "realized_pnl": self.risk_manager.realized_pnl_today,
            "trades_taken": self.risk_manager.trades_taken_today,
            "max_trades": self.settings.max_trades_per_day,
            "max_daily_loss": self.risk_manager.get_dynamic_max_daily_loss(now_ist),
            "is_custom_max_daily_loss": self.risk_manager.custom_max_daily_loss is not None,
            "enabled_indices": self.risk_manager.get_enabled_indices(),
            "strategy_profile": active_profile,
            "strategy_config": self.get_strategy_config(),
            "atm_protection_panel": {
                "strategy_profile": active_profile,
                "protection_enabled": bool(getattr(self.settings, "gann_use_htf_view_protection", True)),
                "gann_box_mode": str(getattr(self.settings, "gann_box_mode", "BREAKOUT") or "BREAKOUT"),
                "indices": atm_panel_indices,
            },
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
