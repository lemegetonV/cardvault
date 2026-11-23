"""Run a service's Alembic migrations programmatically (called by the entrypoint)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

log = logging.getLogger(__name__)


def config_for(migrations_dir: Path, database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade(migrations_dir: Path, database_url: str) -> None:
    command.upgrade(config_for(migrations_dir, database_url), "head")


def run_env(context, target_metadata=None) -> None:
    """Body of every service's ``migrations/env.py`` (async engine, online mode only)."""
    url = context.config.get_main_option("sqlalchemy.url")

    def do_run(connection) -> None:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

    async def run_async() -> None:
        engine = create_async_engine(url, poolclass=pool.NullPool)
        async with engine.connect() as connection:
            await connection.run_sync(do_run)
        await engine.dispose()

    asyncio.run(run_async())
