"""Risk settings router: Configure personal risk limits and lot sizes."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user
from database.connection import get_db
from database.models import User, UserRiskSetting

router = APIRouter(prefix="/api/v1/risk", tags=["Risk Management"])


class UpdateRiskRequest(BaseModel):
    mode: Optional[str] = Field(None, description="PAPER or LIVE")
    capital: Optional[float] = Field(None, ge=10000.0)
    max_daily_loss: Optional[float] = Field(None, ge=500.0)
    max_trades_per_day: Optional[int] = Field(None, ge=1, le=10)
    nifty_lots: Optional[int] = Field(None, ge=1, le=20)
    banknifty_lots: Optional[int] = Field(None, ge=1, le=20)
    nifty_enabled: Optional[bool] = None
    banknifty_enabled: Optional[bool] = None
    tuesday_expiry_boost: Optional[bool] = None
    telegram_chat_id: Optional[str] = None
    push_notifications_enabled: Optional[bool] = None


@router.get("/settings")
async def get_risk_settings(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Retrieve current user risk settings."""
    result = await db.execute(select(UserRiskSetting).where(UserRiskSetting.user_id == user.id))
    risk = result.scalar_one_or_none()
    if not risk:
        risk = UserRiskSetting(user_id=user.id)
        db.add(risk)
        await db.commit()
        await db.refresh(risk)

    return {
        "mode": risk.mode,
        "capital": risk.capital,
        "max_daily_loss": risk.max_daily_loss,
        "max_trades_per_day": risk.max_trades_per_day,
        "nifty_lots": risk.nifty_lots,
        "banknifty_lots": risk.banknifty_lots,
        "nifty_enabled": risk.nifty_enabled,
        "banknifty_enabled": risk.banknifty_enabled,
        "tuesday_expiry_boost": risk.tuesday_expiry_boost,
        "telegram_chat_id": risk.telegram_chat_id,
        "push_notifications_enabled": risk.push_notifications_enabled,
    }


@router.put("/settings")
async def update_risk_settings(
    req: UpdateRiskRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update risk limits and lot sizes."""
    result = await db.execute(select(UserRiskSetting).where(UserRiskSetting.user_id == user.id))
    risk = result.scalar_one_or_none()
    if not risk:
        risk = UserRiskSetting(user_id=user.id)
        db.add(risk)

    for field, val in req.model_dump(exclude_unset=True).items():
        setattr(risk, field, val)

    await db.commit()
    await db.refresh(risk)

    return {"status": "success", "message": "Risk settings updated successfully"}

