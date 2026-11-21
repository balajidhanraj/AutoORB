"""Application configuration, 2026 NSE contract rules, and risk thresholds."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Dict

import pytz
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

IST = pytz.timezone("Asia/Kolkata")


@dataclass(frozen=True)
class IndexSpec:
    """Static NSE 2026 contract & risk parameters for an underlying index."""

    name: str
    spot_exchange: str
    spot_token: str
    opt_exchange: str
    lot_size: int
    default_lots: int
    strike_step: int
    expiry_type: str  # "WEEKLY_TUESDAY" or "MONTHLY_LAST_TUESDAY"
    sl_pts: float
    sl_buffer_pts: float
    breakeven_trigger_pts: float
    target1_pts: float
    trail_step_pts: float
    target_delta_min: float = 0.55
    target_delta_max: float = 0.65
    product_type: str = "M"

    @property
    def default_quantity(self) -> int:
        """Return total option quantity for the default lot count."""
        return self.lot_size * self.default_lots

    @property
    def partial_exit_quantity(self) -> int:
        """Return 50% quantity rounded to an integer number of lots."""
        half_lots = max(1, self.default_lots // 2)
        return self.lot_size * half_lots

    @property
    def spot_key(self) -> str:
        """Return Shoonya subscription key for the spot index."""
        return f"{self.spot_exchange}|{self.spot_token}"


INDEX_SPECS: Dict[str, IndexSpec] = {
    "NIFTY": IndexSpec(
        name="NIFTY",
        spot_exchange="NSE",
        spot_token="26000",
        opt_exchange="NFO",
        lot_size=65,
        default_lots=2,
        strike_step=50,
        expiry_type="WEEKLY_TUESDAY",
        sl_pts=25.0,
        sl_buffer_pts=3.0,
        breakeven_trigger_pts=20.0,
        target1_pts=40.0,
        trail_step_pts=15.0,
        product_type="M",
    ),
    "BANKNIFTY": IndexSpec(
        name="BANKNIFTY",
        spot_exchange="NSE",
        spot_token="26009",
        opt_exchange="NFO",
        lot_size=30,
        default_lots=2,
        strike_step=100,
        expiry_type="MONTHLY_LAST_TUESDAY",
        sl_pts=45.0,
        sl_buffer_pts=5.0,
        breakeven_trigger_pts=45.0,
        target1_pts=80.0,
        trail_step_pts=30.0,
        product_type="M",
    ),
}

INDIA_VIX_EXCHANGE = "NSE"
INDIA_VIX_TOKEN = "26017"
INDIA_VIX_KEY = f"{INDIA_VIX_EXCHANGE}|{INDIA_VIX_TOKEN}"


class AppSettings(BaseModel):
    """Runtime settings loaded from environment variables and .env."""

    mode: str = Field(default="LIVE")
    shoonya_host: str = Field(default="https://api.shoonya.com/NorenWClientAPI/")
    shoonya_ws_host: str = Field(default="wss://api.shoonya.com/NorenWSAPI/")
    shoonya_user_id: str = Field(default="")
    shoonya_account_id: str = Field(default="")
    shoonya_password: str = Field(default="")
    shoonya_totp_secret: str = Field(default="")
    shoonya_api_key: str = Field(default="")
    shoonya_secret_key: str = Field(default="")
    shoonya_redirect_uri: str = Field(default="http://127.0.0.1:8080/callback")
    shoonya_access_token: str = Field(default="")
    token_file_path: str = Field(default="data/daily_token.json")
    sebi_algo_id: str = Field(default="ORB_VWAP_RSI_2026")

    headless_browser: bool = Field(default=True)
    capital: float = Field(default=100000.0)
    max_daily_loss: float = Field(default=10000.0)
    max_trades_per_day: int = Field(default=4)
    enable_nifty_trading: bool = Field(default=False)

    min_india_vix: float = Field(default=12.0)
    min_orb_range_pct: float = Field(default=0.20)
    max_orb_range_pct: float = Field(default=0.85)
    daily_bias_filter: bool = Field(default=True)
    use_ema_filter: bool = Field(default=True)
    min_candle_body_ratio: float = Field(default=0.40)
    require_vwap_slope: bool = Field(default=False)
    min_vwap_slope_pct: float = Field(default=0.02)
    dynamic_sl_pct: float = Field(default=0.0)
    nifty_use_ema_body_filter: bool = Field(default=False)
    banknifty_be_pyramiding: bool = Field(default=False)
    tuesday_expiry_boost: bool = Field(default=True)
    max_weekly_losses: int = Field(default=0)
    entry_slippage_buffer: float = Field(default=2.5)
    paper_slippage_pts: float = Field(default=1.0)
    entry_modify_timeout_sec: float = Field(default=10.0)
    entry_cancel_timeout_sec: float = Field(default=20.0)
    ws_watchdog_timeout_sec: float = Field(default=15.0)
    reconcile_interval_sec: float = Field(default=10.0)
    mtm_check_interval_sec: float = Field(default=1.0)

    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    ui_host: str = Field(default="0.0.0.0")
    ui_port: int = Field(default=8000)
    ui_pin: str = Field(default="1234")

    data_dir: str = Field(default="data")
    log_dir: str = Field(default="logs")

    # Market session timings (IST) — 30-min ORB (09:15-09:45), Institutional Entry Window (10:00-13:15)
    orb_window_minutes: int = Field(default=30)
    orb_start_time: time = Field(default=time(9, 15, 0))
    auth_time: time = Field(default=time(8, 40, 0))
    instrument_cache_time: time = Field(default=time(8, 55, 0))
    ws_connect_time: time = Field(default=time(9, 14, 0))
    market_open_time: time = Field(default=time(9, 15, 0))
    orb_end_time: time = Field(default=time(9, 45, 0))
    entry_start_time: time = Field(default=time(10, 0, 0))
    entry_cutoff_time: time = Field(default=time(13, 15, 0))
    auto_squareoff_time: time = Field(default=time(15, 15, 0))
    market_close_time: time = Field(default=time(15, 30, 0))
    eod_report_time: time = Field(default=time(15, 35, 0))

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"PAPER", "LIVE"}:
            raise ValueError("MODE must be either 'PAPER' or 'LIVE'")
        return normalized


def _parse_bool(raw: str | None, default: bool = True) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings(env_path: str | Path | None = None) -> AppSettings:
    """Load settings from optional .env file and OS environment variables."""
    if env_path is not None:
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)

    user_id = os.getenv("SHOONYA_USER_ID", "").strip()
    account_id = os.getenv("SHOONYA_ACCOUNT_ID", "").strip() or user_id

    settings = AppSettings(
        mode=os.getenv("MODE", "LIVE"),
        shoonya_host=os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientAPI/").strip(),
        shoonya_ws_host=os.getenv("SHOONYA_WS_HOST", "wss://api.shoonya.com/NorenWSAPI/").strip(),
        shoonya_user_id=user_id,
        shoonya_account_id=account_id,
        shoonya_password=os.getenv("SHOONYA_PASSWORD", "").strip(),
        shoonya_totp_secret=os.getenv("SHOONYA_TOTP_SECRET", "").strip(),
        shoonya_api_key=os.getenv("SHOONYA_API_KEY", "").strip(),
        shoonya_secret_key=os.getenv("SHOONYA_SECRET_KEY", "").strip(),
        shoonya_redirect_uri=os.getenv("SHOONYA_REDIRECT_URI", "http://127.0.0.1:8080/callback").strip(),
        shoonya_access_token=os.getenv("SHOONYA_ACCESS_TOKEN", "").strip(),
        token_file_path=os.getenv("TOKEN_FILE_PATH", "data/daily_token.json").strip(),
        sebi_algo_id=os.getenv("SEBI_ALGO_ID", "ORB_VWAP_RSI_2026").strip() or "ORB_VWAP_RSI_2026",
        headless_browser=_parse_bool(os.getenv("HEADLESS_BROWSER"), default=True),
        capital=float(os.getenv("CAPITAL", "100000") or 100000),
        max_daily_loss=float(os.getenv("MAX_DAILY_LOSS", "10000") or 10000),
        max_trades_per_day=int(os.getenv("MAX_TRADES_PER_DAY", "4") or 4),
        enable_nifty_trading=_parse_bool(os.getenv("ENABLE_NIFTY_TRADING"), default=False),
        min_india_vix=float(os.getenv("MIN_INDIA_VIX", "12.0") or 12.0),
        min_orb_range_pct=float(os.getenv("MIN_ORB_RANGE_PCT", "0.20") or 0.20),
        max_orb_range_pct=float(os.getenv("MAX_ORB_RANGE_PCT", "0.85") or 0.85),
        daily_bias_filter=_parse_bool(os.getenv("DAILY_BIAS_FILTER"), default=True),
        use_ema_filter=_parse_bool(os.getenv("USE_EMA_FILTER"), default=True),
        min_candle_body_ratio=float(os.getenv("MIN_CANDLE_BODY_RATIO", "0.40") or 0.40),
        require_vwap_slope=_parse_bool(os.getenv("REQUIRE_VWAP_SLOPE"), default=False),
        min_vwap_slope_pct=float(os.getenv("MIN_VWAP_SLOPE_PCT", "0.02") or 0.02),
        dynamic_sl_pct=float(os.getenv("DYNAMIC_SL_PCT", "0.0") or 0.0),
        nifty_use_ema_body_filter=_parse_bool(os.getenv("NIFTY_USE_EMA_BODY_FILTER"), default=False),
        banknifty_be_pyramiding=_parse_bool(os.getenv("BANKNIFTY_BE_PYRAMIDING"), default=False),
        tuesday_expiry_boost=_parse_bool(os.getenv("TUESDAY_EXPIRY_BOOST"), default=True),
        max_weekly_losses=int(os.getenv("MAX_WEEKLY_LOSSES", "0") or 0),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        ui_host=os.getenv("UI_HOST", "0.0.0.0").strip(),
        ui_port=int(os.getenv("PORT") or os.getenv("UI_PORT") or 8000),
        ui_pin=os.getenv("UI_PIN", "1234").strip(),
    )
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.token_file_path).parent.mkdir(parents=True, exist_ok=True)
    return settings
