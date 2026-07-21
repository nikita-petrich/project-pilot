"""Tests for the async session_scope unit-of-work helper (skipped without Postgres)."""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from project_pilot.db import create_engine, create_session_factory, session_scope

_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pilot:pilot@localhost:5432/project_pilot",
)


async def _factory_or_skip() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_engine(_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as err:
        await engine.dispose()
        pytest.skip(f"Postgres not available: {err}")
    return engine, create_session_factory(engine)


async def test_session_scope_commits() -> None:
    engine, factory = await _factory_or_skip()
    try:
        async with session_scope(factory) as session:
            assert await session.scalar(text("SELECT 42")) == 42
    finally:
        await engine.dispose()


async def test_session_scope_rolls_back_on_error() -> None:
    engine, factory = await _factory_or_skip()
    try:
        with pytest.raises(RuntimeError):
            async with session_scope(factory) as session:
                await session.execute(text("SELECT 1"))
                raise RuntimeError("boom")
    finally:
        await engine.dispose()
