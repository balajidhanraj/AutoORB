"""Async SQLAlchemy database connection and session lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncGenerator

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from database.models import Base

load_dotenv()

def normalize_database_url(raw_url: str) -> str:
    """Normalize PostgreSQL URLs (VPS / Neon / Supabase / GCP Cloud SQL) for asyncpg."""
    cleaned = (raw_url or "").strip()
    if cleaned.startswith("postgres://"):
        cleaned = "postgresql+asyncpg://" + cleaned[len("postgres://") :]
    elif cleaned.startswith("postgresql://"):
        cleaned = "postgresql+asyncpg://" + cleaned[len("postgresql://") :]

    if cleaned.startswith("postgresql+asyncpg://") and "?" in cleaned:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(cleaned)
        filtered_params = []
        for k, v in parse_qsl(parts.query, keep_blank_values=True):
            if k == "sslmode":
                filtered_params.append(("ssl", v))
            elif k == "channel_binding":
                continue
            else:
                filtered_params.append((k, v))
        cleaned = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(filtered_params), parts.fragment)
        )
    return cleaned


# Supports SQLite by default or Neon Lakebase Postgres via DATABASE_URL (pooled) and DATABASE_URL_UNPOOLED (direct)
DATABASE_URL = normalize_database_url(
    os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/autoorb.db")
)
DATABASE_URL_UNPOOLED = normalize_database_url(
    os.getenv("DATABASE_URL_UNPOOLED", "")
) or DATABASE_URL

# Ensure sqlite data directory exists
if "sqlite" in DATABASE_URL:
    Path("data").mkdir(exist_ok=True)


def _build_engine_kwargs(url: str) -> dict:
    kwargs: dict = {"echo": False, "future": True}
    if "sqlite" not in url:
        kwargs.update(
            {
                "pool_pre_ping": True,
                "pool_size": 5,
                "max_overflow": 10,
            }
        )
        # Neon pooled endpoints (-pooler) run PgBouncer in transaction mode; disable asyncpg statement cache
        if "-pooler." in url:
            kwargs["connect_args"] = {"statement_cache_size": 0}
    return kwargs


engine = create_async_engine(DATABASE_URL, **_build_engine_kwargs(DATABASE_URL))

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all database tables using the direct (unpooled) connection string per Neon best practices."""
    if "sqlite" not in DATABASE_URL_UNPOOLED and DATABASE_URL_UNPOOLED != DATABASE_URL:
        migration_engine = create_async_engine(
            DATABASE_URL_UNPOOLED, **_build_engine_kwargs(DATABASE_URL_UNPOOLED)
        )
        try:
            async with migration_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await migration_engine.dispose()
    else:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

