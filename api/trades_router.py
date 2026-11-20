"""Trades router: Trade history, open positions, and analytics."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user
from database.connection import DATABASE_URL, get_db
from database.models import User, UserTrade
from services.neon_service import seal_neon_realtime_query

router = APIRouter(prefix="/api/v1/trades", tags=["Trades & Positions"])


class RecordTradeRequest(BaseModel):
    symbol: str
    side: str = "BUY"
    quantity: int = 65
    entry_price: float
    exit_price: Optional[float] = None
    sl_price: Optional[float] = None
    target1_price: Optional[float] = None
    pnl: float = 0.0
    status: str = "OPEN"
    exit_reason: Optional[str] = None
    trade_ref: Optional[str] = None


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


@router.post("/live-query")
async def get_sealed_trades_live_query(user: User = Depends(get_current_user)):
    """Seal a per-user read-only SQL query for Neon Realtime live subscriptions."""
    sql_query = (
        "SELECT id, trade_ref, user_id, symbol, side, quantity, entry_price, exit_price, "
        "sl_price, target1_price, pnl, status, exit_reason, opened_at, closed_at "
        "FROM user_trades WHERE user_id = $1 ORDER BY opened_at DESC LIMIT 50"
    )
    try:
        return seal_neon_realtime_query(sql_query, [user.id])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/record")
async def record_user_trade(
    req: RecordTradeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Write a user trade to Lakebase Postgres and return the transaction txid for Neon Realtime awaitTxId()."""
    trade = UserTrade(
        id=str(uuid.uuid4()),
        user_id=user.id,
        trade_ref=req.trade_ref or f"ORB-{uuid.uuid4().hex[:8].upper()}",
        symbol=req.symbol.strip().upper(),
        side=req.side.strip().upper(),
        quantity=req.quantity,
        entry_price=req.entry_price,
        exit_price=req.exit_price,
        sl_price=req.sl_price,
        target1_price=req.target1_price,
        pnl=req.pnl,
        status=req.status.strip().upper(),
        exit_reason=req.exit_reason,
    )
    db.add(trade)
    await db.flush()

    txid: Optional[str] = None
    if "sqlite" not in DATABASE_URL:
        try:
            tx_res = await db.execute(text("SELECT pg_current_xact_id()::text"))
            txid = tx_res.scalar_one_or_none()
        except Exception:
            txid = None

    await db.commit()
    await db.refresh(trade)
    return {
        "kind": "committed",
        "txid": txid,
        "trade": {
            "id": trade.id,
            "trade_ref": trade.trade_ref,
            "user_id": trade.user_id,
            "symbol": trade.symbol,
            "side": trade.side,
            "quantity": trade.quantity,
            "entry_price": trade.entry_price,
            "pnl": trade.pnl,
            "status": trade.status,
        },
    }

