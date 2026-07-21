"""Shared pytest fixtures, including a Postgres-backed async session.

The ``session`` fixture builds the schema with ``create_all`` (allowed in tests
only) and yields a session. If Postgres is not reachable, DB tests skip rather
than fail, so the suite still runs in a database-less environment.
"""

import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from project_pilot.models import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://pilot:pilot@localhost:5432/project_pilot",
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except (SQLAlchemyError, OSError) as err:
        await engine.dispose()
        pytest.skip(f"Postgres not available for tests: {err}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db_session:
            yield db_session
    finally:
        await engine.dispose()
