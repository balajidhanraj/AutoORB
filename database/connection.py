"""Async SQLAlchemy database connection and session lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.models import Base

# Supports SQLite by default or PostgreSQL (Supabase / Neon / RDS) via DATABASE_URL
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/autoorb.db")

# Ensure sqlite data directory exists
if "sqlite" in DATABASE_URL:
    Path("data").mkdir(exist_ok=True)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all database tables if they do not exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

