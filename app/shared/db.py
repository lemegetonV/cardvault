"""Async SQLAlchemy engine and session helpers. One database per service."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    def __init__(self, url: str, pool_size: int = 5) -> None:
        self.engine: AsyncEngine = create_async_engine(
            url, pool_size=pool_size, max_overflow=5, pool_pre_ping=True
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """A session inside one transaction: commits on success, rolls back on error."""
        async with self.sessions() as session, session.begin():
            yield session

    async def ping(self) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def truncate(self, *tables: str) -> None:
        if not tables:
            return
        names = ", ".join(f'"{t}"' for t in tables)
        async with self.engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

    async def dispose(self) -> None:
        await self.engine.dispose()
