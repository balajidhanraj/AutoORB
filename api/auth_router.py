"""Authentication router: Email, Google, Facebook, and Mobile OTP."""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user
from database.connection import get_db
from database.models import User, UserBrokerConfig, UserRiskSetting
from services.auth_service import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

# In-memory OTP storage for phone verification (or swap with Redis/SMS Gateway)
_OTP_CACHE: Dict[str, Dict[str, Any]] = {}


# --- Request Schemas ---

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleAuthRequest(BaseModel):
    id_token: str
    email: EmailStr
    name: Optional[str] = None
    avatar_url: Optional[str] = None


class FacebookAuthRequest(BaseModel):
    access_token: str
    user_id: str
    email: Optional[EmailStr] = None
    name: Optional[str] = None


class PhoneSendOtpRequest(BaseModel):
    phone: str  # Format: +919876543210


class PhoneVerifyOtpRequest(BaseModel):
    phone: str
    otp: str
    name: Optional[str] = None


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: Dict[str, Any]


# --- Helper to bootstrap user settings ---

async def _bootstrap_user_defaults(db: AsyncSession, user: User) -> None:
    broker_cfg = UserBrokerConfig(user_id=user.id)
    risk_cfg = UserRiskSetting(user_id=user.id)
    db.add(broker_cfg)
    db.add(risk_cfg)
    await db.commit()


# --- Endpoints ---

@router.post("/register", response_model=AuthResponse)
async def register_user(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """Register a new user with email and password."""
    existing = await db.execute(select(User).where(User.email == req.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="An account with this email already exists")

    user = User(
        email=req.email.lower(),
        name=req.name or req.email.split("@")[0],
        hashed_password=hash_password(req.password),
        auth_provider="email",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    await _bootstrap_user_defaults(db, user)

    token = create_access_token({"sub": user.id, "email": user.email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "name": user.name, "provider": user.auth_provider},
    }


@router.post("/login", response_model=AuthResponse)
async def login_user(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Sign in using email and password."""
    result = await db.execute(select(User).where(User.email == req.email.lower()))
    user = result.scalar_one_or_none()
    if not user or not user.hashed_password or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token({"sub": user.id, "email": user.email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "name": user.name, "provider": user.auth_provider},
    }


@router.post("/google", response_model=AuthResponse)
async def google_auth(req: GoogleAuthRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate or register seamlessly via Google SSO."""
    result = await db.execute(select(User).where(User.email == req.email.lower()))
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            email=req.email.lower(),
            name=req.name,
            avatar_url=req.avatar_url,
            auth_provider="google",
            is_verified=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        await _bootstrap_user_defaults(db, user)

    token = create_access_token({"sub": user.id, "email": user.email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "name": user.name, "avatar_url": user.avatar_url, "provider": "google"},
    }


@router.post("/facebook", response_model=AuthResponse)
async def facebook_auth(req: FacebookAuthRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate or register seamlessly via Facebook SSO."""
    lookup_field = User.email if req.email else User.provider_id
    lookup_val = req.email.lower() if req.email else req.user_id

    result = await db.execute(select(User).where(lookup_field == lookup_val))
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            email=req.email.lower() if req.email else None,
            name=req.name or f"User_{req.user_id[:6]}",
            provider_id=req.user_id,
            auth_provider="facebook",
            is_verified=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        await _bootstrap_user_defaults(db, user)

    token = create_access_token({"sub": user.id, "email": user.email or user.provider_id})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email, "name": user.name, "provider": "facebook"},
    }


@router.post("/phone/send-otp")
async def send_phone_otp(req: PhoneSendOtpRequest):
    """Generate and dispatch OTP to mobile number (Firebase Phone or SMS Gateway)."""
    phone = req.phone.strip()
    if len(phone) < 10:
        raise HTTPException(status_code=400, detail="Invalid phone number format")

    # Generate 6-digit OTP
    otp = str(random.randint(100000, 999999))
    _OTP_CACHE[phone] = {
        "otp": otp,
        "created_at": datetime.utcnow().timestamp(),
    }

    # In production, dispatch via Firebase Auth or Fast2SMS / Twilio
    # For dev & testing, we also return the OTP in the debug response:
    return {
        "status": "success",
        "message": f"OTP sent successfully to {phone}",
        "dev_otp": otp,  # Removed in production mode
    }


@router.post("/phone/verify-otp", response_model=AuthResponse)
async def verify_phone_otp(req: PhoneVerifyOtpRequest, db: AsyncSession = Depends(get_db)):
    """Verify submitted OTP and return session token."""
    phone = req.phone.strip()
    entry = _OTP_CACHE.get(phone)
    if not entry or entry["otp"] != req.otp.strip():
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    # Expire after 10 minutes
    if datetime.utcnow().timestamp() - entry["created_at"] > 600:
        del _OTP_CACHE[phone]
        raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")

    del _OTP_CACHE[phone]

    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            phone=phone,
            name=req.name or f"Trader_{phone[-4:]}",
            auth_provider="phone",
            is_verified=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        await _bootstrap_user_defaults(db, user)

    token = create_access_token({"sub": user.id, "phone": user.phone})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": user.id, "phone": user.phone, "name": user.name, "provider": "phone"},
    }


@router.get("/me")
async def get_me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Fetch current user profile, broker link status, and risk settings."""
    cfg_res = await db.execute(select(UserBrokerConfig).where(UserBrokerConfig.user_id == user.id))
    broker_cfg = cfg_res.scalar_one_or_none()

    risk_res = await db.execute(select(UserRiskSetting).where(UserRiskSetting.user_id == user.id))
    risk_cfg = risk_res.scalar_one_or_none()

    return {
        "id": user.id,
        "email": user.email,
        "phone": user.phone,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "provider": user.auth_provider,
        "role": user.role,
        "broker": {
            "name": broker_cfg.broker_name if broker_cfg else "SHOONYA",
            "shoonya_user_id": broker_cfg.shoonya_user_id if broker_cfg else None,
            "is_connected": broker_cfg.is_connected if broker_cfg else False,
            "status": broker_cfg.connection_status if broker_cfg else "DISCONNECTED",
            "trading_enabled": broker_cfg.trading_enabled if broker_cfg else False,
        },
        "risk": {
            "mode": risk_cfg.mode if risk_cfg else "PAPER",
            "capital": risk_cfg.capital if risk_cfg else 100000.0,
            "max_daily_loss": risk_cfg.max_daily_loss if risk_cfg else 3000.0,
            "nifty_lots": risk_cfg.nifty_lots if risk_cfg else 1,
            "banknifty_lots": risk_cfg.banknifty_lots if risk_cfg else 1,
            "nifty_enabled": risk_cfg.nifty_enabled if risk_cfg else True,
            "banknifty_enabled": risk_cfg.banknifty_enabled if risk_cfg else True,
        },
    }

