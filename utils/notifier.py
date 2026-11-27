"""Thread-safe non-blocking Telegram notification dispatcher."""

from __future__ import annotations

import mimetypes
import queue
import threading
from pathlib import Path
from typing import Optional, Tuple, Union

import requests

from utils.logger import get_logger

logger = get_logger("notifier")


class TelegramNotifier:
    """Dispatches Telegram alerts via a background worker thread."""

    def __init__(self, bot_token: str = "", chat_id: str = "", timeout_sec: float = 5.0) -> None:
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.timeout_sec = timeout_sec
        self._queue: queue.Queue[
            Optional[Union[str, Tuple[str, Path, str, Optional[str]]]]
        ] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None

        if self.enabled:
            masked_chat = f"...{self.chat_id[-4:]}" if len(self.chat_id) > 4 else self.chat_id
            logger.info("Telegram notifications ENABLED (chat_id=%s)", masked_chat)
            self._worker = threading.Thread(
                target=self._run_worker,
                name="telegram-notifier",
                daemon=True,
            )
            self._worker.start()
        else:
            logger.info("Telegram notifications DISABLED (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set)")

    @property
    def enabled(self) -> bool:
        """Return True when both bot token and chat ID are configured."""
        return bool(self.bot_token and self.chat_id)

    def send_async(self, message: str, prefix: str = "") -> None:
        """Enqueue a Telegram notification without blocking the caller."""
        text = f"{prefix}{message}".strip()
        if not text:
            return
        if not self.enabled:
            logger.debug("Telegram disabled; message skipped: %s", text)
            return
        self._queue.put(text)

    def send_sync(self, message: str) -> bool:
        """Send a Telegram message synchronously and return success status."""
        if not self.enabled:
            logger.debug("Telegram disabled; sync message skipped: %s", message)
            return False

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout_sec)
            if resp.status_code == 200:
                preview = " ".join(message.split())[:60]
                logger.info("Telegram alert delivered (%s)", preview)
                return True
            logger.warning("Telegram API returned %s: %s", resp.status_code, resp.text)
            return False
        except Exception as exc:
            logger.warning("Telegram send error: %s", exc)
            return False

    def send_document_async(
        self,
        file_path: str | Path,
        caption: str = "",
        filename: Optional[str] = None,
    ) -> None:
        """Enqueue a file attachment upload to Telegram without blocking the caller."""
        if not self.enabled:
            logger.debug("Telegram disabled; document skipped: %s", file_path)
            return
        self._queue.put(("document", Path(file_path), caption.strip(), filename))

    def send_document_sync(
        self,
        file_path: str | Path,
        caption: str = "",
        filename: Optional[str] = None,
        fallback_to_text: bool = True,
    ) -> bool:
        """Upload a local file to Telegram via sendDocument and return success status."""
        if not self.enabled:
            logger.debug("Telegram disabled; sync document skipped: %s", file_path)
            return False

        path = Path(file_path)
        clean_caption = caption.strip()
        if not path.is_file():
            logger.warning("Telegram document missing: %s", path)
            if fallback_to_text and clean_caption:
                self.send_sync(clean_caption)
            return False

        doc_name = (filename or path.name).strip() or path.name
        mime_type = (
            "text/csv"
            if doc_name.lower().endswith(".csv")
            else (mimetypes.guess_type(doc_name)[0] or "application/octet-stream")
        )
        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        data = {"chat_id": self.chat_id}
        if clean_caption:
            data["caption"] = clean_caption[:1024]
            data["parse_mode"] = "HTML"

        try:
            with path.open("rb") as fh:
                resp = requests.post(
                    url,
                    data=data,
                    files={"document": (doc_name, fh, mime_type)},
                    timeout=max(self.timeout_sec, 15.0),
                )
            if resp.status_code == 200:
                logger.info("Telegram document delivered (%s)", doc_name)
                return True
            logger.warning("Telegram sendDocument returned %s: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("Telegram sendDocument error: %s", exc)

        if fallback_to_text and clean_caption:
            self.send_sync(clean_caption)
        return False

    def _run_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            if isinstance(item, tuple) and len(item) == 4 and item[0] == "document":
                self.send_document_sync(item[1], caption=item[2], filename=item[3])
            elif isinstance(item, str):
                self.send_sync(item)
            self._queue.task_done()

    def stop(self) -> None:
        """Stop background dispatcher cleanly."""
        self._stop_event.set()
        if self._worker is not None and self._worker.is_alive():
            self._queue.put(None)
            self._worker.join(timeout=2.0)
