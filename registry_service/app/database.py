"""
registry_service/app/database.py

Async SQLAlchemy engine and session factory.

DATABASE_URL must be set to a postgresql+asyncpg:// connection string, e.g.:
  postgresql+asyncpg://user:pass@postgres.ping-devops-cprice.svc.cluster.local:5432/registry
"""
from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DATABASE_URL: str = os.environ["DATABASE_URL"]  # must use asyncpg driver

engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
