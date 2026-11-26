"""Persistent runtime state store backed by Neon Postgres and atomic local JSON mirror."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from utils.logger import get_logger

logger = get_logger("services.runtime_state_store")


class RuntimeStateStore:
    """Thread-safe sync facade over local JSON + Neon Postgres `system_runtime_state`."""

    def __init__(
        self,
        data_dir: str | Path,
        state_key: str = "default",
        enable_remote_db: Optional[bool] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.local_path = self.data_dir / "runtime_state.json"
        self.state_key = (state_key or "default").strip() or "default"

        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._pg_engine: Any = None
        self._table_ready = False

        raw_db_url = (os.getenv("DATABASE_URL") or "").strip()
        is_pytest = bool(os.getenv("PYTEST_CURRENT_TEST"))
        is_pg = ("postgresql" in raw_db_url) and ("sqlite" not in raw_db_url)
        if enable_remote_db is not None:
            self._remote_enabled = bool(enable_remote_db and is_pg)
        else:
            self._remote_enabled = bool(is_pg and not is_pytest)

        self._db_url = raw_db_url
        self.remote_load_ok: bool = not self._remote_enabled
        self._remote_writes_blocked: bool = False
        self._retry_delay_sec: float = 2.0
        if self._remote_enabled:
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
            name="runtime-state-pg-loop",
            daemon=True,
        )
        self._loop_thread.start()
        self._loop_ready.wait(timeout=3.0)

    def _ensure_engine(self) -> Any:
        if self._pg_engine is None:
            from database.connection import _build_engine_kwargs, normalize_database_url

            norm_url = normalize_database_url(self._db_url)
            kwargs = _build_engine_kwargs(norm_url)
            if "pool_size" in kwargs:
                kwargs["pool_size"] = 2
                kwargs["max_overflow"] = 2
            conn_args = dict(kwargs.get("connect_args") or {})
            conn_args.setdefault("timeout", 15)
            kwargs["connect_args"] = conn_args
            self._pg_engine = create_async_engine(norm_url, **kwargs)
        return self._pg_engine

    async def _ensure_table_async(self) -> None:
        if self._table_ready:
            return
        eng = self._ensure_engine()
        ddl = text(
            """
            CREATE TABLE IF NOT EXISTS system_runtime_state (
                key VARCHAR(64) PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        async with eng.begin() as conn:
            await conn.execute(ddl)
        self._table_ready = True

    async def _load_remote_async(self) -> Dict[str, Any]:
        eng = self._ensure_engine()
        stmt = text("SELECT state_json FROM system_runtime_state WHERE key = :k")
        try:
            async with eng.connect() as conn:
                res = await conn.execute(stmt, {"k": self.state_key})
                row = res.first()
            self._table_ready = True
        except Exception as exc:
            err_lower = str(exc).lower()
            if (
                "system_runtime_state" in err_lower
                or "does not exist" in err_lower
                or "undefinedtable" in type(exc).__name__.lower()
            ):
                await self._ensure_table_async()
                return {}
            raise
        if not row or not row[0]:
            return {}
        parsed = json.loads(str(row[0]))
        return parsed if isinstance(parsed, dict) else {}

    async def _save_remote_async(self, serialized: str) -> None:
        await self._ensure_table_async()
        eng = self._ensure_engine()
        stmt = text(
            """
            INSERT INTO system_runtime_state (key, state_json, updated_at)
            VALUES (:k, :s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE
            SET state_json = EXCLUDED.state_json,
                updated_at = CURRENT_TIMESTAMP
            """
        )
        async with eng.begin() as conn:
            await conn.execute(stmt, {"k": self.state_key, "s": serialized})

    def _write_local(self, serialized: str) -> None:
        with self._lock:
            self.local_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.local_path.with_suffix(".tmp")
            tmp_path.write_text(serialized, encoding="utf-8")
            tmp_path.replace(self.local_path)

    def _read_local(self) -> Dict[str, Any]:
        with self._lock:
            if not self.local_path.exists():
                return {}
            try:
                raw = self.local_path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except Exception as exc:
                logger.warning("Failed reading local runtime state %s: %s", self.local_path, exc)
                return {}

    def load(self) -> Dict[str, Any]:
        """Load persisted runtime state from Neon Postgres first, falling back to local JSON."""
        if self._remote_enabled and self._loop is not None:
            last_exc: Optional[Exception] = None
            for attempt in range(1, 3):
                try:
                    fut = asyncio.run_coroutine_threadsafe(self._load_remote_async(), self._loop)
                    remote_data = fut.result(timeout=15.0)
                    self.remote_load_ok = True
                    self._remote_writes_blocked = False
                    if remote_data:
                        try:
                            self._write_local(json.dumps(remote_data, indent=2, sort_keys=True))
                        except Exception:
                            pass
                        return remote_data
                    return self._read_local()
                except Exception as exc:
                    last_exc = exc
                    err_detail = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
                    logger.warning(
                        "Remote Postgres runtime state load attempt %d/2 failed: %s",
                        attempt,
                        err_detail,
                    )
                    if attempt < 2 and self._retry_delay_sec > 0:
                        time.sleep(self._retry_delay_sec)
            self.remote_load_ok = False
            self._remote_writes_blocked = True
            logger.warning(
                "Remote Postgres unavailable at startup (blocking background overwrites until synced; using local fallback): %s",
                f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown error",
            )
        return self._read_local()

    def save(self, payload: Dict[str, Any], force_remote: bool = False) -> None:
        """Persist runtime state locally now and dispatch async Neon Postgres upsert."""
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        self._write_local(serialized)
        if self._remote_enabled and self._loop is not None:
            if self._remote_writes_blocked and not force_remote:
                logger.debug("Skipping remote Postgres overwrite until initial cold-start restore succeeds")
                return
            if force_remote:
                self._remote_writes_blocked = False
            try:
                asyncio.run_coroutine_threadsafe(self._save_remote_async(serialized), self._loop)
            except Exception as exc:
                logger.warning("Failed scheduling remote Postgres state upsert: %s", exc)

    def save_sync(self, payload: Dict[str, Any], timeout: float = 10.0, force_remote: bool = False) -> bool:
        """Persist runtime state locally and wait up to `timeout` seconds for Postgres commit."""
        serialized = json.dumps(payload, indent=2, sort_keys=True)
        self._write_local(serialized)
        if not self._remote_enabled or self._loop is None:
            return True
        if self._remote_writes_blocked and not force_remote:
            logger.debug("Skipping remote Postgres sync overwrite until initial cold-start restore succeeds")
            return False
        if force_remote:
            self._remote_writes_blocked = False
        try:
            fut = asyncio.run_coroutine_threadsafe(self._save_remote_async(serialized), self._loop)
            fut.result(timeout=timeout)
            self.remote_load_ok = True
            return True
        except Exception as exc:
            logger.warning(
                "Remote Postgres state sync timed out/failed (local JSON saved): %s",
                f"{type(exc).__name__}: {exc}",
            )
            return False
