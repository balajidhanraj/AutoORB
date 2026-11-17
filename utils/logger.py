"""Structured rotating file, console, and in-memory UI ring-buffer logger."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Deque, Dict, List

from config.settings import IST

_LOG_BUFFER_LOCK = threading.RLock()
_LOG_BUFFER: Deque[Dict[str, str]] = deque(maxlen=250)
_INITIALIZED = False


class _ISTFormatter(logging.Formatter):
    """Format log timestamps consistently in Asia/Kolkata (IST)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


class _UIRingBufferHandler(logging.Handler):
    """Stores recent log lines in memory for live Web UI inspection."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            dt = datetime.fromtimestamp(record.created, tz=IST).strftime("%H:%M:%S")
            entry = {
                "timestamp": dt,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            with _LOG_BUFFER_LOCK:
                _LOG_BUFFER.append(entry)
        except Exception:
            self.handleError(record)


def setup_logging(log_dir: str | Path = "logs", level: int = logging.INFO) -> logging.Logger:
    """Configure root application logging once and return the root logger."""
    global _INITIALIZED
    root_logger = logging.getLogger("shoonya_orb")
    root_logger.setLevel(level)

    if _INITIALIZED and root_logger.handlers:
        return root_logger

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "shoonya_orb.log"

    formatter = _ISTFormatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    file_handler = TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)

    ui_handler = _UIRingBufferHandler()
    ui_handler.setLevel(level)

    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(ui_handler)
    root_logger.propagate = False

    _INITIALIZED = True
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the shoonya_orb namespace."""
    if not _INITIALIZED:
        setup_logging()
    return logging.getLogger(f"shoonya_orb.{name}")


def get_recent_logs(limit: int = 100) -> List[Dict[str, str]]:
    """Return the most recent log records for the Web UI dashboard."""
    with _LOG_BUFFER_LOCK:
        items = list(_LOG_BUFFER)
    if limit <= 0:
        return []
    return items[-limit:]
