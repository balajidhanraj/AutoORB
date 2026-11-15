"""Automated Selenium + TOTP OAuth 2.0 login and daily token manager for Shoonya."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import pyotp

from config.settings import IST, AppSettings
from utils.logger import get_logger
from utils.notifier import TelegramNotifier

logger = get_logger("broker.auth")


def parse_oauth_code_from_url(url: str) -> Optional[str]:
    """Extract the OAuth authorization `code` query parameter from a redirect URL."""
    if not url:
        return None
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    codes = params.get("code")
    if not codes:
        return None
    code = codes[0].strip()
    return code or None


def should_wait_for_totp_window(
    current_epoch: Optional[float] = None,
    threshold: float = 26.0,
) -> Tuple[bool, float]:
    """Check if the current time is too close to a 30-second TOTP rollover boundary."""
    now = time.time() if current_epoch is None else current_epoch
    remainder = now % 30.0
    if remainder > threshold:
        wait_seconds = max(5.0, (30.0 - remainder) + 0.5)
        return True, round(wait_seconds, 2)
    return False, 0.0


class _OAuthCallbackServer:
    """Ephemeral local HTTP listener to capture the OAuth redirect code cleanly."""

    def __init__(self, redirect_uri: str) -> None:
        parsed = urlparse(redirect_uri)
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 8080
        self.captured_code: Optional[str] = None
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                code = parse_oauth_code_from_url(self.path)
                if code:
                    parent.captured_code = code
                body = b"<html><body><h3>Shoonya OAuth code captured. You may close this tab.</h3></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        try:
            self._server = HTTPServer((self.host, self.port), _Handler)
            self._server.timeout = 0.5
            self._thread = threading.Thread(
                target=self._serve,
                name="oauth-callback-server",
                daemon=True,
            )
            self._thread.start()
            return True
        except OSError as exc:
            logger.debug("Ephemeral OAuth callback server unavailable on %s:%s (%s)", self.host, self.port, exc)
            return False

    def _serve(self) -> None:
        if self._server is None:
            return
        while self.captured_code is None:
            try:
                self._server.handle_request()
            except Exception:
                break

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None


class ShoonyaAuthenticator:
    """Manages daily Shoonya OAuth 2.0 session caching and headless Selenium + TOTP login."""

    RETRY_BACKOFFS_SEC = (10, 25, 45)

    def __init__(
        self,
        settings: AppSettings,
        api_client: Any,
        notifier: Optional[TelegramNotifier] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.api = api_client
        self.notifier = notifier
        self._sleep = sleep_fn
        self.last_login_status: str = "UNAUTHENTICATED"
        self.last_token_date: Optional[str] = None

    @staticmethod
    def _today_ist_str() -> str:
        return datetime.now(IST).strftime("%Y-%m-%d")

    def load_cached_token(self) -> Optional[str]:
        """Load today's cached OAuth access token and verify it with get_limits()."""
        token_path = Path(self.settings.token_file_path)
        today_str = self._today_ist_str()
        candidate_token = ""

        if token_path.exists():
            try:
                payload = json.loads(token_path.read_text(encoding="utf-8"))
                cached_uid = str(payload.get("user_id") or "").strip().upper()
                expected_uid = self.settings.shoonya_user_id.strip().upper()
                if (
                    payload.get("date") == today_str
                    and payload.get("access_token")
                    and (not cached_uid or not expected_uid or cached_uid == expected_uid)
                ):
                    candidate_token = str(payload["access_token"]).strip()
            except Exception as exc:
                logger.warning("Failed reading token cache %s: %s", token_path, exc)

        if not candidate_token and self.settings.shoonya_access_token:
            candidate_token = self.settings.shoonya_access_token

        if not candidate_token:
            return None

        try:
            self.api.injectOAuthHeader(
                candidate_token,
                self.settings.shoonya_user_id,
                self.settings.shoonya_account_id,
            )
            limits = self.api.get_limits()
            if isinstance(limits, dict) and limits.get("stat") == "Ok":
                self.last_login_status = "AUTHENTICATED (CACHED)"
                self.last_token_date = today_str
                logger.info("Loaded and verified cached Shoonya access token for %s", today_str)
                return candidate_token
        except Exception as exc:
            logger.warning("Cached token validation failed: %s", exc)

        return None

    def save_token(self, access_token: str) -> None:
        """Persist today's validated OAuth token to disk."""
        today_str = self._today_ist_str()
        token_path = Path(self.settings.token_file_path)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date": today_str,
            "access_token": access_token,
            "user_id": self.settings.shoonya_user_id,
            "created_at": datetime.now(IST).isoformat(),
        }
        token_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.settings.shoonya_access_token = access_token
        self.last_token_date = today_str
        self.last_login_status = "AUTHENTICATED"

    def generate_totp(self, current_epoch: Optional[float] = None) -> str:
        """Generate a valid 6-digit TOTP, sleeping past 30-second boundary rollovers."""
        if not self.settings.shoonya_totp_secret:
            raise ValueError("SHOONYA_TOTP_SECRET is not configured")

        should_wait, wait_sec = should_wait_for_totp_window(current_epoch=current_epoch)
        if should_wait:
            logger.info("Near 30s TOTP boundary; sleeping %.2fs for fresh OTP window", wait_sec)
            self._sleep(wait_sec)

        return pyotp.TOTP(self.settings.shoonya_totp_secret).now()

    def _create_webdriver(self) -> Any:
        """Create a Chromium WebDriver configured for headless Linux VPS or local desktop."""
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        options = Options()
        if self.settings.headless_browser:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.page_load_strategy = "eager"

        # Prefer system chromedriver if installed (e.g., Docker / Linux VPS), else webdriver-manager
        for driver_path in ("/usr/bin/chromedriver", "/usr/local/bin/chromedriver"):
            if Path(driver_path).exists():
                return webdriver.Chrome(service=Service(driver_path), options=options)

        try:
            from webdriver_manager.chrome import ChromeDriverManager

            service = Service(ChromeDriverManager().install())
            return webdriver.Chrome(service=service, options=options)
        except Exception:
            return webdriver.Chrome(options=options)

    def _fill_otp_inputs(self, driver: Any, otp_code: str) -> None:
        """Populate either split 6-box OTP inputs or a single OTP input element."""
        from selenium.webdriver.common.by import By

        split_selectors = (
            "input[maxlength='1']",
            "input.otp-input",
            "input[data-testid*='otp']",
        )
        for selector in split_selectors:
            boxes = [el for el in driver.find_elements(By.CSS_SELECTOR, selector) if el.is_displayed()]
            if len(boxes) >= 6:
                for idx, digit in enumerate(otp_code[:6]):
                    boxes[idx].clear()
                    boxes[idx].send_keys(digit)
                return

        single_selectors = (
            "input[name*='otp' i]",
            "input[id*='otp' i]",
            "input[placeholder*='otp' i]",
            "input[placeholder*='totp' i]",
            "input[type='tel']",
            "input[type='number']",
            "input[type='text']",
        )
        for selector in single_selectors:
            elements = [el for el in driver.find_elements(By.CSS_SELECTOR, selector) if el.is_displayed()]
            if elements:
                elements[0].clear()
                elements[0].send_keys(otp_code)
                return

        raise RuntimeError("Could not locate TOTP input element on Shoonya OAuth page")

    def _fetch_code_via_selenium(self) -> str:
        """Use headless Chromium Selenium to authenticate and capture the OAuth redirect code."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        oauth_url = self.api.getOAuthURL()
        callback_server = _OAuthCallbackServer(self.settings.shoonya_redirect_uri)
        callback_server.start()
        driver = None

        try:
            driver = self._create_webdriver()
            wait = WebDriverWait(driver, 20)
            driver.get(oauth_url)

            # Step 1: User ID & Password
            user_input = wait.until(
                EC.visibility_of_element_located(
                    (By.CSS_SELECTOR, "input[name*='user' i], input[id*='user' i], input[type='text']")
                )
            )
            pwd_input = wait.until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
            )
            user_input.clear()
            user_input.send_keys(self.settings.shoonya_user_id)
            pwd_input.clear()
            pwd_input.send_keys(self.settings.shoonya_password)

            # Shoonya OAuth page places #lgnotp on the same card; handle both single-step & 2-step forms
            inline_otp = [
                el
                for el in driver.find_elements(By.CSS_SELECTOR, "input#lgnotp, input[autocomplete='one-time-code']")
                if el.is_displayed()
            ]
            if inline_otp:
                otp_code = self.generate_totp()
                inline_otp[0].clear()
                inline_otp[0].send_keys(otp_code)

            submit_buttons = driver.find_elements(By.CSS_SELECTOR, "button[type='submit'], button.lgnBtnClss, input[type='submit']")
            if submit_buttons:
                submit_buttons[0].click()
            else:
                pwd_input.send_keys(Keys.RETURN)

            if not inline_otp:
                wait.until(
                    EC.presence_of_element_located(
                        (
                            By.CSS_SELECTOR,
                            "input#lgnotp, input[maxlength='1'], input[name*='otp' i], input[id*='otp' i], input[type='tel']",
                        )
                    )
                )
                otp_code = self.generate_totp()
                self._fill_otp_inputs(driver, otp_code)

                otp_buttons = driver.find_elements(By.CSS_SELECTOR, "button[type='submit'], button.lgnBtnClss, input[type='submit']")
                if otp_buttons:
                    otp_buttons[0].click()

            # Step 3: Capture redirect code
            redirect_prefix = self.settings.shoonya_redirect_uri.split("?")[0]
            deadline = time.monotonic() + 25.0
            auth_code: Optional[str] = None
            while time.monotonic() < deadline:
                if callback_server.captured_code:
                    auth_code = callback_server.captured_code
                    break
                current_url = driver.current_url or ""
                if current_url.startswith(redirect_prefix):
                    auth_code = parse_oauth_code_from_url(current_url)
                    if auth_code:
                        break
                self._sleep(0.25)

            if not auth_code:
                raise RuntimeError("Timed out waiting for Shoonya OAuth redirect code")
            return auth_code
        finally:
            callback_server.stop()
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

    def login_via_selenium(self) -> str:
        """Run one OAuth 2.0 + TOTP login flow (fast API handshake with Selenium fallback) and return access_token."""
        if not all([
            self.settings.shoonya_user_id,
            self.settings.shoonya_password,
            self.settings.shoonya_totp_secret,
            self.settings.shoonya_secret_key,
        ]):
            raise ValueError("Missing Shoonya OAuth/TOTP credentials in configuration")

        auth_code: Optional[str] = None
        if hasattr(self.api, "fetch_oauth_code_direct"):
            try:
                otp_code = self.generate_totp()
                auth_code = self.api.fetch_oauth_code_direct(
                    self.settings.shoonya_user_id,
                    self.settings.shoonya_password,
                    otp_code,
                    client_id=self.settings.shoonya_api_key,
                )
                logger.info("Obtained Shoonya OAuth authorization code via direct handshake")
            except Exception as exc:
                logger.warning("Direct OAuth code handshake failed (%s); falling back to Selenium", exc)

        if not auth_code:
            auth_code = self._fetch_code_via_selenium()

        token_resp = self.api.getAccessToken(auth_code)
        if not isinstance(token_resp, dict):
            raise RuntimeError(f"Invalid access token response: {token_resp}")

        if token_resp.get("stat") == "Not_Ok":
            emsg = str(token_resp.get("emsg") or "")
            if "INVALID_IP" in emsg:
                raise RuntimeError(
                    "Shoonya GenAcsTok rejected request: INVALID_IP — whitelist your current public IP "
                    "in Shoonya Profile -> API Key"
                )
            raise RuntimeError(f"GenAcsTok error: {token_resp}")

        access_token = str(token_resp.get("access_token") or token_resp.get("susertoken") or "").strip()
        if not access_token:
            raise RuntimeError(f"Invalid access token response: {token_resp}")

        self.api.injectOAuthHeader(
            access_token,
            self.settings.shoonya_user_id,
            self.settings.shoonya_account_id,
        )

        limits = self.api.get_limits()
        if not isinstance(limits, dict) or limits.get("stat") != "Ok":
            raise RuntimeError(f"get_limits validation failed after OAuth login: {limits}")

        self.save_token(access_token)
        return access_token

    def authenticate_once(self) -> Tuple[bool, str]:
        """Run one immediate OAuth login attempt for interactive UI account connection."""
        try:
            self.login_via_selenium()
            self.last_login_status = "AUTHENTICATED"
            logger.info("Shoonya OAuth Login Successful (%s)", self.settings.shoonya_user_id)
            if self.notifier:
                self.notifier.send_async(
                    f"Shoonya OAuth Login Successful ({self.settings.shoonya_user_id})"
                )
            return True, f"Authenticated Shoonya account {self.settings.shoonya_user_id}."
        except Exception as exc:
            self.last_login_status = f"LOGIN_FAILED: {exc}"
            logger.error("Interactive OAuth login failed (%s): %s", self.settings.shoonya_user_id, exc)
            return False, str(exc)

    def ensure_authenticated(self, force_refresh: bool = False) -> Optional[str]:
        """Ensure a valid OAuth session exists, using cache or 3-attempt Selenium retry."""
        if not force_refresh:
            cached = self.load_cached_token()
            if cached:
                return cached

        if self.settings.mode == "PAPER" and not self.settings.shoonya_user_id:
            self.last_login_status = "PAPER_SANDBOX (NO BROKER CREDS)"
            logger.info("PAPER mode without Shoonya credentials; skipping browser OAuth login")
            return None

        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                logger.info("Starting Shoonya OAuth 2.0 login attempt %d/3", attempt)
                token = self.login_via_selenium()
                logger.info("Shoonya OAuth Login Successful")
                if self.notifier:
                    self.notifier.send_async("Shoonya OAuth Login Successful")
                return token
            except Exception as exc:
                last_error = exc
                logger.error("OAuth login attempt %d/3 failed: %s", attempt, exc)
                if attempt < 3:
                    backoff = self.RETRY_BACKOFFS_SEC[attempt - 1]
                    logger.info("Retrying OAuth login in %ds...", backoff)
                    self._sleep(backoff)

        self.last_login_status = f"LOGIN_FAILED: {last_error}"
        alert_msg = f"CRITICAL: Morning Login Failed after 3 attempts: {last_error}"
        logger.critical(alert_msg)
        if self.notifier:
            self.notifier.send_async(alert_msg)
        return None
