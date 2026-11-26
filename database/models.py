"""SQLAlchemy database models for multi-tenant AutoORB platform."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def generate_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    """User account model supporting Google, Facebook, Phone OTP, and Email auth."""

    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    email = Column(String(255), unique=True, nullable=True, index=True)
    phone = Column(String(32), unique=True, nullable=True, index=True)
    name = Column(String(128), nullable=True)
    avatar_url = Column(String(512), nullable=True)
    auth_provider = Column(String(32), default="email")  # google, facebook, phone, email
    provider_id = Column(String(255), nullable=True, index=True)
    hashed_password = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    role = Column(String(32), default="user")  # user, admin
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    broker_config = relationship("UserBrokerConfig", back_populates="user", uselist=False, cascade="all, delete-orphan")
    risk_settings = relationship("UserRiskSetting", back_populates="user", uselist=False, cascade="all, delete-orphan")
    trades = relationship("UserTrade", back_populates="user", cascade="all, delete-orphan")


class UserBrokerConfig(Base):
    """Encrypted broker credentials and per-user session tokens."""

    __tablename__ = "user_broker_configs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    broker_name = Column(String(32), default="SHOONYA")
    shoonya_user_id = Column(String(64), nullable=True)
    encrypted_password = Column(Text, nullable=True)
    encrypted_totp_secret = Column(Text, nullable=True)
    encrypted_api_key = Column(Text, nullable=True)
    encrypted_secret_key = Column(Text, nullable=True)
    access_token = Column(Text, nullable=True)
    token_date = Column(String(16), nullable=True)  # YYYY-MM-DD
    is_connected = Column(Boolean, default=False)
    connection_status = Column(String(64), default="DISCONNECTED")
    last_error = Column(Text, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    trading_enabled = Column(Boolean, default=False)  # Master toggle per user

    user = relationship("User", back_populates="broker_config")


class UserRiskSetting(Base):
    """Personalized trading risk parameters per user."""

    __tablename__ = "user_risk_settings"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    mode = Column(String(16), default="PAPER")  # PAPER or LIVE
    capital = Column(Float, default=100000.0)
    max_daily_loss = Column(Float, default=3000.0)
    max_trades_per_day = Column(Integer, default=2)
    nifty_lots = Column(Integer, default=1)
    banknifty_lots = Column(Integer, default=1)
    nifty_enabled = Column(Boolean, default=True)
    banknifty_enabled = Column(Boolean, default=True)
    tuesday_expiry_boost = Column(Boolean, default=True)
    telegram_chat_id = Column(String(64), nullable=True)
    push_notifications_enabled = Column(Boolean, default=True)

    user = relationship("User", back_populates="risk_settings")


class UserTrade(Base):
    """Individual trade execution record per user."""

    __tablename__ = "user_trades"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    trade_ref = Column(String(64), nullable=True)  # Signal ID or order reference
    symbol = Column(String(64), nullable=False)
    side = Column(String(16), nullable=False)  # BUY or SELL
    order_type = Column(String(16), default="MKT")
    quantity = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    sl_price = Column(Float, nullable=True)
    target1_price = Column(Float, nullable=True)
    pnl = Column(Float, default=0.0)
    status = Column(String(32), default="OPEN")  # OPEN, CLOSED, CANCELLED, REJECTED
    exit_reason = Column(String(64), nullable=True)
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="trades")


class SystemRuntimeState(Base):
    """Persistent engine runtime state (mode, capital, settings, daily token & counters)."""

    __tablename__ = "system_runtime_state"

    key = Column(String(64), primary_key=True)
    state_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BrokerAccountSnapshot(Base):
    """Time-series LIVE Shoonya account margin, balance, P&L, and holdings snapshot."""

    __tablename__ = "broker_account_snapshots"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    session_date = Column(String(10), nullable=False, index=True)
    captured_at_ist = Column(String(32), nullable=False, index=True)
    trigger_event = Column(String(32), default="PERIODIC")
    user_id = Column(String(64), nullable=False, default="", index=True)
    account_id = Column(String(64), nullable=False, default="")
    mode = Column(String(16), default="LIVE")
    cash = Column(Float, default=0.0)
    payin = Column(Float, default=0.0)
    payout = Column(Float, default=0.0)
    collateral = Column(Float, default=0.0)
    margin_used = Column(Float, default=0.0)
    net_available = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)
    orders_count = Column(Integer, default=0)
    trades_count = Column(Integer, default=0)
    open_positions_count = Column(Integer, default=0)
    holdings_count = Column(Integer, default=0)
    raw_limits_json = Column(Text, nullable=True)
    raw_holdings_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BrokerOrderRecord(Base):
    """Deduplicated Shoonya OrderBook record archived for live tracking & analysis."""

    __tablename__ = "broker_orders"

    order_key = Column(String(128), primary_key=True)
    session_date = Column(String(10), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, default="", index=True)
    norenordno = Column(String(64), nullable=False, index=True)
    exchordid = Column(String(64), nullable=True)
    norentm = Column(String(32), nullable=True)
    exch = Column(String(16), default="")
    tsym = Column(String(64), default="", index=True)
    trantype = Column(String(8), default="")
    prd = Column(String(16), default="")
    prctyp = Column(String(16), default="")
    qty = Column(Integer, default=0)
    fillshares = Column(Integer, default=0)
    prc = Column(Float, default=0.0)
    avgprc = Column(Float, default=0.0)
    trgprc = Column(Float, default=0.0)
    status = Column(String(32), default="", index=True)
    ordersource = Column(String(32), nullable=True)
    remarks = Column(Text, nullable=True)
    rejreason = Column(Text, nullable=True)
    raw_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BrokerTradeRecord(Base):
    """Deduplicated Shoonya TradeBook fill record archived for post-trade analysis."""

    __tablename__ = "broker_trades"

    trade_key = Column(String(128), primary_key=True)
    session_date = Column(String(10), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, default="", index=True)
    flid = Column(String(64), nullable=False, default="", index=True)
    norenordno = Column(String(64), nullable=False, default="", index=True)
    exchordid = Column(String(64), nullable=True)
    fltm = Column(String(32), nullable=True)
    exch = Column(String(16), default="")
    tsym = Column(String(64), default="", index=True)
    trantype = Column(String(8), default="")
    prd = Column(String(16), default="")
    flqty = Column(Integer, default=0)
    flprc = Column(Float, default=0.0)
    ordersource = Column(String(32), nullable=True)
    remarks = Column(Text, nullable=True)
    raw_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BrokerPositionRecord(Base):
    """Deduplicated Shoonya PositionBook daily summary archived for P&L attribution."""

    __tablename__ = "broker_positions"

    position_key = Column(String(160), primary_key=True)
    session_date = Column(String(10), nullable=False, index=True)
    user_id = Column(String(64), nullable=False, default="", index=True)
    exch = Column(String(16), default="")
    tsym = Column(String(64), default="", index=True)
    prd = Column(String(16), default="")
    netqty = Column(Integer, default=0)
    daybuyqty = Column(Integer, default=0)
    daysellqty = Column(Integer, default=0)
    netavgprc = Column(Float, default=0.0)
    daybuyavgprc = Column(Float, default=0.0)
    daysellavgprc = Column(Float, default=0.0)
    lp = Column(Float, default=0.0)
    rpnl = Column(Float, default=0.0)
    urmtom = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)
    raw_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


