"""Persistent archive of LIVE Shoonya broker data (balances, orders, trades, positions, holdings)."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config.settings import IST
from utils.logger import get_logger

logger = get_logger("services.broker_archive_store")


class BrokerArchiveStore:
    """Thread-safe async Postgres / SQLite archive for LIVE Shoonya broker telemetry & analysis."""

    def __init__(self, data_dir: str | Path, db_url: Optional[str] = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._engine: Any = None
        self._tables_ready = False

        self.last_archive_time_ist: Optional[str] = None
        self.last_archive_counts: Dict[str, int] = {}

        if db_url:
            self._db_url = db_url.strip()
        else:
            raw_env_url = (os.getenv("DATABASE_URL") or "").strip()
            is_pytest = bool(os.getenv("PYTEST_CURRENT_TEST"))
            is_pg = ("postgresql" in raw_env_url) and ("sqlite" not in raw_env_url)
            if is_pg and not is_pytest:
                self._db_url = raw_env_url
            else:
                sqlite_path = (self.data_dir / "broker_archive.db").resolve().as_posix()
                self._db_url = f"sqlite+aiosqlite:///{sqlite_path}"

        self._is_postgres = ("postgresql" in self._db_url) and ("sqlite" not in self._db_url)
        self._start_bg_loop()

    def _start_bg_loop(self) -> None:
        if self._loop_thread is not None and self._loop_thread.is_alive():
            return

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._loop_ready.set()
            loop.run_forever()

        self._loop_thread = threading.Thread(
            target=_runner,
            name="broker-archive-db-loop",
            daemon=True,
        )
        self._loop_thread.start()
        self._loop_ready.wait(timeout=3.0)

    def _ensure_engine(self) -> Any:
        if self._engine is None:
            from database.connection import _build_engine_kwargs, normalize_database_url

            norm_url = normalize_database_url(self._db_url)
            kwargs = _build_engine_kwargs(norm_url)
            if self._is_postgres:
                if "pool_size" in kwargs:
                    kwargs["pool_size"] = 2
                    kwargs["max_overflow"] = 2
                conn_args = dict(kwargs.get("connect_args") or {})
                conn_args.setdefault("timeout", 15)
                kwargs["connect_args"] = conn_args
            self._engine = create_async_engine(norm_url, **kwargs)
        return self._engine

    async def _ensure_tables_async(self) -> None:
        if self._tables_ready:
            return
        from database.models import (
            BrokerAccountSnapshot,
            BrokerOrderRecord,
            BrokerPositionRecord,
            BrokerTradeRecord,
        )

        tables = [
            BrokerAccountSnapshot.__table__,
            BrokerOrderRecord.__table__,
            BrokerTradeRecord.__table__,
            BrokerPositionRecord.__table__,
        ]
        eng = self._ensure_engine()
        async with eng.begin() as conn:
            await conn.run_sync(lambda sync_conn: [t.create(sync_conn, checkfirst=True) for t in tables])
        self._tables_ready = True

    @staticmethod
    def _to_float(val: Any, default: float = 0.0) -> float:
        try:
            if val is None or val == "":
                return default
            return float(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_int(val: Any, default: int = 0) -> int:
        try:
            if val is None or val == "":
                return default
            return int(float(val))
        except (TypeError, ValueError):
            return default

    async def _archive_async(self, bundle: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_tables_async()
        eng = self._ensure_engine()
        snap_row = bundle["snapshot"]
        orders_rows: List[Dict[str, Any]] = bundle["orders"]
        trades_rows: List[Dict[str, Any]] = bundle["trades"]
        positions_rows: List[Dict[str, Any]] = bundle["positions"]

        snap_stmt = text(
            """
            INSERT INTO broker_account_snapshots (
                id, session_date, captured_at_ist, trigger_event, user_id, account_id, mode,
                cash, payin, payout, collateral, margin_used, net_available,
                realized_pnl, unrealized_pnl, total_pnl,
                orders_count, trades_count, open_positions_count, holdings_count,
                raw_limits_json, raw_holdings_json, created_at
            ) VALUES (
                :id, :session_date, :captured_at_ist, :trigger_event, :user_id, :account_id, :mode,
                :cash, :payin, :payout, :collateral, :margin_used, :net_available,
                :realized_pnl, :unrealized_pnl, :total_pnl,
                :orders_count, :trades_count, :open_positions_count, :holdings_count,
                :raw_limits_json, :raw_holdings_json, CURRENT_TIMESTAMP
            )
            """
        )

        order_stmt = text(
            """
            INSERT INTO broker_orders (
                order_key, session_date, user_id, norenordno, exchordid, norentm,
                exch, tsym, trantype, prd, prctyp, qty, fillshares,
                prc, avgprc, trgprc, status, ordersource, remarks, rejreason, raw_json, updated_at
            ) VALUES (
                :order_key, :session_date, :user_id, :norenordno, :exchordid, :norentm,
                :exch, :tsym, :trantype, :prd, :prctyp, :qty, :fillshares,
                :prc, :avgprc, :trgprc, :status, :ordersource, :remarks, :rejreason, :raw_json, CURRENT_TIMESTAMP
            )
            ON CONFLICT (order_key) DO UPDATE SET
                exchordid = EXCLUDED.exchordid,
                norentm = EXCLUDED.norentm,
                exch = EXCLUDED.exch,
                tsym = EXCLUDED.tsym,
                trantype = EXCLUDED.trantype,
                prd = EXCLUDED.prd,
                prctyp = EXCLUDED.prctyp,
                qty = EXCLUDED.qty,
                fillshares = EXCLUDED.fillshares,
                prc = EXCLUDED.prc,
                avgprc = EXCLUDED.avgprc,
                trgprc = EXCLUDED.trgprc,
                status = EXCLUDED.status,
                ordersource = EXCLUDED.ordersource,
                remarks = EXCLUDED.remarks,
                rejreason = EXCLUDED.rejreason,
                raw_json = EXCLUDED.raw_json,
                updated_at = CURRENT_TIMESTAMP
            """
        )

        trade_stmt = text(
            """
            INSERT INTO broker_trades (
                trade_key, session_date, user_id, flid, norenordno, exchordid, fltm,
                exch, tsym, trantype, prd, flqty, flprc, ordersource, remarks, raw_json, updated_at
            ) VALUES (
                :trade_key, :session_date, :user_id, :flid, :norenordno, :exchordid, :fltm,
                :exch, :tsym, :trantype, :prd, :flqty, :flprc, :ordersource, :remarks, :raw_json, CURRENT_TIMESTAMP
            )
            ON CONFLICT (trade_key) DO UPDATE SET
                exchordid = EXCLUDED.exchordid,
                fltm = EXCLUDED.fltm,
                exch = EXCLUDED.exch,
                tsym = EXCLUDED.tsym,
                trantype = EXCLUDED.trantype,
                prd = EXCLUDED.prd,
                flqty = EXCLUDED.flqty,
                flprc = EXCLUDED.flprc,
                ordersource = EXCLUDED.ordersource,
                remarks = EXCLUDED.remarks,
                raw_json = EXCLUDED.raw_json,
                updated_at = CURRENT_TIMESTAMP
            """
        )

        pos_stmt = text(
            """
            INSERT INTO broker_positions (
                position_key, session_date, user_id, exch, tsym, prd,
                netqty, daybuyqty, daysellqty,
                netavgprc, daybuyavgprc, daysellavgprc, lp,
                rpnl, urmtom, total_pnl, raw_json, updated_at
            ) VALUES (
                :position_key, :session_date, :user_id, :exch, :tsym, :prd,
                :netqty, :daybuyqty, :daysellqty,
                :netavgprc, :daybuyavgprc, :daysellavgprc, :lp,
                :rpnl, :urmtom, :total_pnl, :raw_json, CURRENT_TIMESTAMP
            )
            ON CONFLICT (position_key) DO UPDATE SET
                netqty = EXCLUDED.netqty,
                daybuyqty = EXCLUDED.daybuyqty,
                daysellqty = EXCLUDED.daysellqty,
                netavgprc = EXCLUDED.netavgprc,
                daybuyavgprc = EXCLUDED.daybuyavgprc,
                daysellavgprc = EXCLUDED.daysellavgprc,
                lp = EXCLUDED.lp,
                rpnl = EXCLUDED.rpnl,
                urmtom = EXCLUDED.urmtom,
                total_pnl = EXCLUDED.total_pnl,
                raw_json = EXCLUDED.raw_json,
                updated_at = CURRENT_TIMESTAMP
            """
        )

        async with eng.begin() as conn:
            await conn.execute(snap_stmt, snap_row)
            if orders_rows:
                await conn.execute(order_stmt, orders_rows)
            if trades_rows:
                await conn.execute(trade_stmt, trades_rows)
            if positions_rows:
                await conn.execute(pos_stmt, positions_rows)

        return {
            "stored": True,
            "snapshot_id": snap_row["id"],
            "session_date": snap_row["session_date"],
            "captured_at_ist": snap_row["captured_at_ist"],
            "orders": len(orders_rows),
            "trades": len(trades_rows),
            "positions": len(positions_rows),
            "holdings": snap_row["holdings_count"],
        }

    def archive_live_snapshot(
        self,
        snapshot: Dict[str, Any],
        raw_payload: Optional[Dict[str, Any]] = None,
        trigger_event: str = "PERIODIC",
        session_date: Optional[str] = None,
        sync: bool = False,
    ) -> Dict[str, Any]:
        """Persist a connected LIVE Shoonya broker snapshot and all orders/trades/positions/holdings."""
        if not isinstance(snapshot, dict) or not snapshot.get("connected"):
            return {"stored": False, "reason": "not_connected"}
        if self._loop is None:
            return {"stored": False, "reason": "loop_unavailable"}

        now_ist = datetime.now(IST)
        sess_date = session_date or now_ist.strftime("%Y-%m-%d")
        captured_at = now_ist.isoformat()
        user_id = str(snapshot.get("user_id") or "").strip().upper()
        account_id = str(snapshot.get("account_id") or user_id).strip().upper()
        raw = raw_payload if isinstance(raw_payload, dict) else {}

        raw_orders = raw.get("orders") if isinstance(raw.get("orders"), list) else snapshot.get("orders") or []
        raw_trades = raw.get("trades") if isinstance(raw.get("trades"), list) else snapshot.get("trades") or []
        raw_positions = (
            raw.get("positions") if isinstance(raw.get("positions"), list) else snapshot.get("positions") or []
        )
        raw_holdings = (
            raw.get("holdings") if isinstance(raw.get("holdings"), list) else snapshot.get("holdings") or []
        )
        raw_limits = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}

        orders_rows: List[Dict[str, Any]] = []
        seen_okeys: set[str] = set()
        for o in raw_orders:
            if not isinstance(o, dict):
                continue
            ord_no = str(o.get("norenordno") or "").strip()
            if not ord_no:
                continue
            okey = f"{sess_date}:{user_id}:{ord_no}"[:128]
            if okey in seen_okeys:
                continue
            seen_okeys.add(okey)
            orders_rows.append(
                {
                    "order_key": okey,
                    "session_date": sess_date,
                    "user_id": user_id,
                    "norenordno": ord_no,
                    "exchordid": str(o.get("exchordid") or ""),
                    "norentm": str(o.get("norentm") or ""),
                    "exch": str(o.get("exch") or ""),
                    "tsym": str(o.get("tsym") or ""),
                    "trantype": str(o.get("trantype") or ""),
                    "prd": str(o.get("s_prdt_ali") or o.get("prd") or ""),
                    "prctyp": str(o.get("prctyp") or ""),
                    "qty": self._to_int(o.get("qty")),
                    "fillshares": self._to_int(o.get("fillshares")),
                    "prc": self._to_float(o.get("prc")),
                    "avgprc": self._to_float(o.get("avgprc") or o.get("prc")),
                    "trgprc": self._to_float(o.get("trgprc")),
                    "status": str(o.get("status") or ""),
                    "ordersource": str(o.get("ordersource") or ""),
                    "remarks": str(o.get("remarks") or o.get("reason") or ""),
                    "rejreason": str(o.get("rejreason") or ""),
                    "raw_json": json.dumps(o, default=str),
                }
            )

        trades_rows: List[Dict[str, Any]] = []
        seen_tkeys: set[str] = set()
        for idx, t in enumerate(raw_trades):
            if not isinstance(t, dict):
                continue
            flid = str(t.get("flid") or "").strip()
            ord_no = str(t.get("norenordno") or "").strip()
            fltm = str(t.get("fltm") or t.get("norentm") or "").strip()
            if not (flid or ord_no):
                continue
            suffix = flid or f"{ord_no}:{fltm or idx}"
            tkey = f"{sess_date}:{user_id}:{suffix}"[:128]
            if tkey in seen_tkeys:
                continue
            seen_tkeys.add(tkey)
            trades_rows.append(
                {
                    "trade_key": tkey,
                    "session_date": sess_date,
                    "user_id": user_id,
                    "flid": flid,
                    "norenordno": ord_no,
                    "exchordid": str(t.get("exchordid") or ""),
                    "fltm": fltm,
                    "exch": str(t.get("exch") or ""),
                    "tsym": str(t.get("tsym") or ""),
                    "trantype": str(t.get("trantype") or ""),
                    "prd": str(t.get("s_prdt_ali") or t.get("prd") or ""),
                    "flqty": self._to_int(t.get("flqty") or t.get("fillshares")),
                    "flprc": self._to_float(t.get("flprc") or t.get("prc")),
                    "ordersource": str(t.get("ordersource") or ""),
                    "remarks": str(t.get("remarks") or ""),
                    "raw_json": json.dumps(t, default=str),
                }
            )

        positions_rows: List[Dict[str, Any]] = []
        seen_pkeys: set[str] = set()
        total_rpnl = 0.0
        total_urmtom = 0.0
        open_pos_count = 0
        for p in raw_positions:
            if not isinstance(p, dict):
                continue
            exch = str(p.get("exch") or "")
            tsym = str(p.get("tsym") or "")
            prd = str(p.get("s_prdt_ali") or p.get("prd") or "")
            if not tsym:
                continue
            netqty = self._to_int(p.get("netqty"))
            if netqty != 0:
                open_pos_count += 1
            rp = self._to_float(p.get("rpnl"))
            ur = self._to_float(p.get("urmtom"))
            total_rpnl += rp
            total_urmtom += ur
            pkey = f"{sess_date}:{user_id}:{exch}:{tsym}:{prd}"[:160]
            if pkey in seen_pkeys:
                continue
            seen_pkeys.add(pkey)
            positions_rows.append(
                {
                    "position_key": pkey,
                    "session_date": sess_date,
                    "user_id": user_id,
                    "exch": exch,
                    "tsym": tsym,
                    "prd": prd,
                    "netqty": netqty,
                    "daybuyqty": self._to_int(p.get("daybuyqty")),
                    "daysellqty": self._to_int(p.get("daysellqty")),
                    "netavgprc": self._to_float(p.get("netavgprc")),
                    "daybuyavgprc": self._to_float(p.get("daybuyavgprc")),
                    "daysellavgprc": self._to_float(p.get("daysellavgprc")),
                    "lp": self._to_float(p.get("lp")),
                    "rpnl": round(rp, 2),
                    "urmtom": round(ur, 2),
                    "total_pnl": round(rp + ur, 2),
                    "raw_json": json.dumps(p, default=str),
                }
            )

        snap_rpnl = round(total_rpnl, 2) if positions_rows else round(self._to_float(snapshot.get("realized_pnl")), 2)
        snap_urmtom = round(total_urmtom, 2) if positions_rows else round(self._to_float(snapshot.get("unrealized_pnl")), 2)
        snap_row = {
            "id": str(uuid.uuid4()),
            "session_date": sess_date,
            "captured_at_ist": captured_at,
            "trigger_event": str(trigger_event or "PERIODIC")[:32],
            "user_id": user_id,
            "account_id": account_id,
            "mode": "LIVE",
            "cash": round(self._to_float(snapshot.get("cash")), 2),
            "payin": round(self._to_float(snapshot.get("payin")), 2),
            "payout": round(self._to_float(snapshot.get("payout")), 2),
            "collateral": round(self._to_float(snapshot.get("collateral")), 2),
            "margin_used": round(self._to_float(snapshot.get("margin_used")), 2),
            "net_available": round(self._to_float(snapshot.get("net_available")), 2),
            "realized_pnl": snap_rpnl,
            "unrealized_pnl": snap_urmtom,
            "total_pnl": round(snap_rpnl + snap_urmtom, 2),
            "orders_count": len(orders_rows),
            "trades_count": len(trades_rows),
            "open_positions_count": open_pos_count,
            "holdings_count": len([h for h in raw_holdings if isinstance(h, dict)]),
            "raw_limits_json": json.dumps(raw_limits, default=str),
            "raw_holdings_json": json.dumps(raw_holdings, default=str),
        }

        bundle = {
            "snapshot": snap_row,
            "orders": orders_rows,
            "trades": trades_rows,
            "positions": positions_rows,
        }

        def _record_stats(res: Dict[str, Any]) -> None:
            with self._lock:
                self.last_archive_time_ist = captured_at
                self.last_archive_counts = {
                    "orders": len(orders_rows),
                    "trades": len(trades_rows),
                    "positions": len(positions_rows),
                    "holdings": snap_row["holdings_count"],
                }
            if orders_rows or trades_rows or positions_rows:
                logger.info(
                    "Archived LIVE broker data [%s] (%s): orders=%d trades=%d positions=%d net_avail=₹%.2f",
                    trigger_event,
                    user_id or "SHOONYA",
                    len(orders_rows),
                    len(trades_rows),
                    len(positions_rows),
                    snap_row["net_available"],
                )
            else:
                logger.debug(
                    "Archived LIVE broker balance snapshot [%s] (%s): net_avail=₹%.2f",
                    trigger_event,
                    user_id or "SHOONYA",
                    snap_row["net_available"],
                )

        try:
            fut = asyncio.run_coroutine_threadsafe(self._archive_async(bundle), self._loop)
            if sync:
                res = fut.result(timeout=12.0)
                _record_stats(res)
                return res

            def _on_done(done_fut: Any) -> None:
                try:
                    _record_stats(done_fut.result())
                except Exception as exc:
                    logger.warning("Async LIVE broker archive failed: %s", f"{type(exc).__name__}: {exc}")

            fut.add_done_callback(_on_done)
            return {
                "stored": True,
                "scheduled": True,
                "snapshot_id": snap_row["id"],
                "session_date": sess_date,
                "captured_at_ist": captured_at,
                "orders": len(orders_rows),
                "trades": len(trades_rows),
                "positions": len(positions_rows),
                "holdings": snap_row["holdings_count"],
            }
        except Exception as exc:
            logger.warning("Failed persisting LIVE broker archive: %s", f"{type(exc).__name__}: {exc}")
            return {"stored": False, "error": f"{type(exc).__name__}: {exc}"}

    async def _query_archive_async(
        self,
        session_date: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        await self._ensure_tables_async()
        eng = self._ensure_engine()
        clauses = ["1=1"]
        params: Dict[str, Any] = {"lim": max(1, min(500, int(limit)))}
        if session_date:
            clauses.append("session_date = :sd")
            params["sd"] = session_date.strip()
        if user_id:
            clauses.append("user_id = :uid")
            params["uid"] = user_id.strip().upper()
        where_sql = " AND ".join(clauses)

        async with eng.connect() as conn:
            s_res = await conn.execute(
                text(
                    f"""
                    SELECT id, session_date, captured_at_ist, trigger_event, user_id, account_id, mode,
                           cash, payin, payout, collateral, margin_used, net_available,
                           realized_pnl, unrealized_pnl, total_pnl,
                           orders_count, trades_count, open_positions_count, holdings_count,
                           raw_limits_json, raw_holdings_json
                    FROM broker_account_snapshots
                    WHERE {where_sql}
                    ORDER BY captured_at_ist DESC
                    LIMIT :lim
                    """
                ),
                params,
            )
            snapshots = [dict(r) for r in s_res.mappings().all()]

            o_res = await conn.execute(
                text(
                    f"""
                    SELECT order_key, session_date, user_id, norenordno, exchordid, norentm,
                           exch, tsym, trantype, prd, prctyp, qty, fillshares,
                           prc, avgprc, trgprc, status, ordersource, remarks, rejreason, raw_json
                    FROM broker_orders
                    WHERE {where_sql}
                    ORDER BY updated_at DESC
                    LIMIT :lim
                    """
                ),
                params,
            )
            orders = [dict(r) for r in o_res.mappings().all()]

            t_res = await conn.execute(
                text(
                    f"""
                    SELECT trade_key, session_date, user_id, flid, norenordno, exchordid, fltm,
                           exch, tsym, trantype, prd, flqty, flprc, ordersource, remarks, raw_json
                    FROM broker_trades
                    WHERE {where_sql}
                    ORDER BY updated_at DESC
                    LIMIT :lim
                    """
                ),
                params,
            )
            trades = [dict(r) for r in t_res.mappings().all()]

            p_res = await conn.execute(
                text(
                    f"""
                    SELECT position_key, session_date, user_id, exch, tsym, prd,
                           netqty, daybuyqty, daysellqty,
                           netavgprc, daybuyavgprc, daysellavgprc, lp,
                           rpnl, urmtom, total_pnl, raw_json
                    FROM broker_positions
                    WHERE {where_sql}
                    ORDER BY updated_at DESC
                    LIMIT :lim
                    """
                ),
                params,
            )
            positions = [dict(r) for r in p_res.mappings().all()]

        total_rpnl = round(sum(float(p.get("rpnl") or 0.0) for p in positions), 2)
        total_upnl = round(sum(float(p.get("urmtom") or 0.0) for p in positions), 2)
        distinct_syms = sorted(
            {
                str(row.get("tsym") or "")
                for row in (orders + trades + positions)
                if row.get("tsym")
            }
        )
        return {
            "ok": True,
            "summary": {
                "total_snapshots": len(snapshots),
                "total_orders": len(orders),
                "total_trades": len(trades),
                "total_positions": len(positions),
                "total_realized_pnl": total_rpnl,
                "total_unrealized_pnl": total_upnl,
                "total_pnl": round(total_rpnl + total_upnl, 2),
                "distinct_symbols": distinct_syms,
                "latest_snapshot": snapshots[0] if snapshots else None,
            },
            "snapshots": snapshots,
            "orders": orders,
            "trades": trades,
            "positions": positions,
        }

    def query_archive(
        self,
        session_date: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """Query archived LIVE broker data and aggregate P&L/activity summary for analysis."""
        if self._loop is None:
            return {"ok": False, "error": "Archive DB loop unavailable", "summary": {}}
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._query_archive_async(session_date=session_date, user_id=user_id, limit=limit),
                self._loop,
            )
            return fut.result(timeout=12.0)
        except Exception as exc:
            logger.warning("Failed querying broker archive: %s", f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "summary": {}}
