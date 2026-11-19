"""Trades router: Trade history, open positions, and analytics."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user
from database.connection import get_db
from database.models import User, UserTrade

router = APIRouter(prefix="/api/v1/trades", tags=["Trades & Positions"])


@router.get("/my-trades")
async def get_my_trades(
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fetch user's trade history."""
    result = await db.execute(
        select(UserTrade)
        .where(UserTrade.user_id == user.id)
        .order_by(desc(UserTrade.opened_at))
        .limit(limit)
    )
    trades = result.scalars().all()

    return [
        {
            "id": t.id,
            "trade_ref": t.trade_ref,
            "symbol": t.symbol,
            "side": t.side,
            "quantity": t.quantity,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "sl_price": t.sl_price,
            "target1_price": t.target1_price,
            "pnl": t.pnl,
            "status": t.status,
            "exit_reason": t.exit_reason,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        }
        for t in trades
    ]


@router.get("/stats")
async def get_trade_stats(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Calculate win-rate, total PnL, and trade performance metrics."""
    result = await db.execute(select(UserTrade).where(UserTrade.user_id == user.id))
    trades = result.scalars().all()

    closed_trades = [t for t in trades if t.status == "CLOSED"]
    winning_trades = [t for t in closed_trades if (t.pnl or 0.0) > 0]
    losing_trades = [t for t in closed_trades if (t.pnl or 0.0) < 0]

    total_pnl = sum((t.pnl or 0.0) for t in closed_trades)
    win_rate = (len(winning_trades) / len(closed_trades) * 100.0) if closed_trades else 0.0

    return {
        "total_trades": len(closed_trades),
        "winning_trades": len(winning_trades),
        "losing_trades": len(losing_trades),
        "win_rate_pct": round(win_rate, 2),
        "total_realized_pnl": round(total_pnl, 2),
        "open_positions_count": len([t for t in trades if t.status == "OPEN"]),
    }

