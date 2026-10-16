"""Opt-in PostgreSQL proof of the current consistent-read unit of work.

The Community product runtime stays SQLite-only.  This test deliberately uses
an externally managed PostgreSQL instance only as an isolation proof and
never starts a container or an Okto Pulse process.
"""

from __future__ import annotations

import os
from importlib import import_module

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
)

pytestmark = pytest.mark.e2e

_POSTGRES_DSN_ENV = "OKTO_PULSE_TEST_POSTGRES_DSN"
_POSTGRES_DSN = os.environ.get(_POSTGRES_DSN_ENV)
asyncpg = import_module("asyncpg") if _POSTGRES_DSN else None


def _schema_qualified_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+asyncpg://", 1)
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not _POSTGRES_DSN,
    reason=f"set {_POSTGRES_DSN_ENV} to run the SK-M PostgreSQL proof",
)
async def test_skm_postgresql_uow_starts_repeatable_read_before_query() -> None:
    """The SaaS-target adapter configures isolation before its first SELECT."""

    assert _POSTGRES_DSN is not None
    engine = create_async_engine(_schema_qualified_dsn(_POSTGRES_DSN))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            uow = CommunityUnitOfWork(session)
            await uow.begin_consistent_read()
            isolation = await session.scalar(text("SHOW transaction_isolation"))
            assert str(isolation).replace("_", " ").upper() == "REPEATABLE READ"
            await session.rollback()
    finally:
        await engine.dispose()
