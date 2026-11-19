"""Broker configuration router: Connect, Authenticate, and Toggle Trading."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pyotp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user
from broker.shoonya_client import ShoonyaClient
from config.settings import AppSettings
from database.connection import get_db
from database.models import User, UserBrokerConfig
from services.encryption import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/api/v1/broker", tags=["Broker Integration"])


class ConnectShoonyaRequest(BaseModel):
    user_id: str
    password: str
    totp_secret: str
    api_key: Optional[str] = None
    secret_key: str


class ToggleTradingRequest(BaseModel):
    enabled: bool


@router.get("/status")
async def get_broker_status(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Get the user's broker connection status."""
    result = await db.execute(select(UserBrokerConfig).where(UserBrokerConfig.user_id == user.id))
    cfg = result.scalar_one_or_none()
    if not cfg:
        return {"is_connected": False, "status": "NOT_CONFIGURED", "trading_enabled": False}

    return {
        "broker_name": cfg.broker_name,
        "shoonya_user_id": cfg.shoonya_user_id,
        "is_connected": cfg.is_connected,
        "status": cfg.connection_status,
        "trading_enabled": cfg.trading_enabled,
        "last_login_at": cfg.last_login_at.isoformat() if cfg.last_login_at else None,
        "last_error": cfg.last_error,
    }


@router.post("/connect")
async def connect_shoonya(
    req: ConnectShoonyaRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Authenticate with Shoonya, validate credentials, and store encrypted keys."""
    uid = req.user_id.strip().upper()
    api_key = req.api_key.strip() if req.api_key else f"{uid}_U"
    if not api_key.endswith("_U"):
        api_key = f"{api_key}_U"

    # Step 1: Validate TOTP generation
    try:
        totp_code = pyotp.TOTP(req.totp_secret.strip()).now()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid TOTP Secret format: {exc}")

    # Step 2: Test live handshake with Shoonya
    temp_settings = AppSettings(
        shoonya_user_id=uid,
        shoonya_password=req.password.strip(),
        shoonya_totp_secret=req.totp_secret.strip(),
        shoonya_api_key=api_key,
        shoonya_secret_key=req.secret_key.strip(),
    )
    client = ShoonyaClient(temp_settings)

    try:
        # Handshake 1: QuickAuth to get OAuth authorization code
        auth_code = client.api.fetch_oauth_code_direct(
            uid,
            req.password.strip(),
            totp_code,
            client_id=api_key,
        )
        # Handshake 2: Exchange for Access Token
        token_resp = client.api.getAccessToken(auth_code)
        if not isinstance(token_resp, dict) or token_resp.get("stat") != "Ok":
            raise RuntimeError(f"Shoonya Token generation failed: {token_resp}")

        access_token = str(token_resp.get("access_token") or token_resp.get("susertoken") or "").strip()
        client.api.injectOAuthHeader(access_token, uid, uid)

        # Handshake 3: Validate limits
        limits = client.api.get_limits()
        if not isinstance(limits, dict) or limits.get("stat") != "Ok":
            raise RuntimeError(f"Shoonya profile limits check failed: {limits}")

    except Exception as exc:
        # Record error
        result = await db.execute(select(UserBrokerConfig).where(UserBrokerConfig.user_id == user.id))
        cfg = result.scalar_one_or_none()
        if cfg:
            cfg.is_connected = False
            cfg.connection_status = "LOGIN_FAILED"
            cfg.last_error = str(exc)
            await db.commit()
        raise HTTPException(status_code=400, detail=f"Shoonya authentication failed: {exc}")

    # Step 3: Persist encrypted credentials to database
    result = await db.execute(select(UserBrokerConfig).where(UserBrokerConfig.user_id == user.id))
    cfg = result.scalar_one_or_none()
    if not cfg:
        cfg = UserBrokerConfig(user_id=user.id)
        db.add(cfg)

    cfg.shoonya_user_id = uid
    cfg.encrypted_password = encrypt_secret(req.password.strip())
    cfg.encrypted_totp_secret = encrypt_secret(req.totp_secret.strip())
    cfg.encrypted_api_key = encrypt_secret(api_key)
    cfg.encrypted_secret_key = encrypt_secret(req.secret_key.strip())
    cfg.access_token = access_token
    cfg.token_date = datetime.utcnow().strftime("%Y-%m-%d")
    cfg.is_connected = True
    cfg.connection_status = "CONNECTED"
    cfg.last_error = None
    cfg.last_login_at = datetime.utcnow()
    cfg.trading_enabled = True

    await db.commit()

    return {
        "status": "success",
        "message": f"Successfully connected Shoonya account {uid}",
        "user_name": token_resp.get("uname", uid),
        "available_cash": limits.get("cash", "0.00"),
    }


@router.post("/toggle-trading")
async def toggle_trading(
    req: ToggleTradingRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable automated trade execution for this user."""
    result = await db.execute(select(UserBrokerConfig).where(UserBrokerConfig.user_id == user.id))
    cfg = result.scalar_one_or_none()
    if not cfg or not cfg.is_connected:
        raise HTTPException(status_code=400, detail="Cannot toggle trading: No broker account connected")

    cfg.trading_enabled = req.enabled
    await db.commit()
    return {"status": "success", "trading_enabled": cfg.trading_enabled}


@router.post("/disconnect")
async def disconnect_broker(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Disconnect broker account and clear stored session tokens."""
    result = await db.execute(select(UserBrokerConfig).where(UserBrokerConfig.user_id == user.id))
    cfg = result.scalar_one_or_none()
    if cfg:
        cfg.is_connected = False
        cfg.connection_status = "DISCONNECTED"
        cfg.access_token = None
        cfg.trading_enabled = False
        await db.commit()

    return {"status": "success", "message": "Broker disconnected successfully"}

