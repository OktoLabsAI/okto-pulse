"""Community engine/session/PRAGMA/pool parity.

Proves the Community-owned relational adapter owns the local-first SQLAlchemy
runtime:

  - Local First pool sizing and fail-closed URL validation;
  - session factory kwargs (``class_=AsyncSession`` + ``expire_on_commit=False``);
  - the SQLite PRAGMA union — WAL + busy_timeout=30000 +
    synchronous=NORMAL + foreign_keys=ON — proven against a REAL connection;
  - pool observability listeners installed (status snapshot);
  - configure_community_database injects the live runtime into the core facade.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import okto_pulse.community.app as _core_app  # noqa: F401
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
    community_pool_status,
    configure_community_database,
    install_community_pool_observability,
    install_community_sqlite_pragmas,
)


def test_ac1_sqlite_pool_config_matches_core(tmp_path):
    """The Community sqlite engine carries the documented local-first pool config."""
    ce = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'community.db'}")
    try:
        assert ce.echo is False
        assert ce.pool.size() == 20
        assert ce.pool._max_overflow == 30
        assert ce.pool._timeout == 10
        assert ce.pool._recycle == 1800
        assert ce.pool._pre_ping is True

        async def _cleanup() -> None:
            await ce.dispose()

        asyncio.run(_cleanup())
    except Exception:
        asyncio.run(ce.dispose())
        raise


def test_ac1_non_local_database_url_is_rejected_before_driver_loading():
    with pytest.raises(ValueError, match="community_database_requires_sqlite"):
        build_community_engine("serverdb+missingdriver://u:p@localhost/db")


def test_ac1_session_factory_kwargs(tmp_path):
    """expire_on_commit=False + class_=AsyncSession preserved (read-after-write)."""
    eng = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'sf.db'}")
    sf = build_community_session_factory(eng)

    async def drive():
        session = sf()
        try:
            is_async = isinstance(session, AsyncSession)
            expire = session.sync_session.expire_on_commit
        finally:
            await session.close()
            await eng.dispose()
        return is_async, expire

    is_async, expire = asyncio.run(drive())
    assert is_async is True
    assert expire is False


def test_ac1_sqlite_pragma_union_on_real_connection(tmp_path):
    """The single-owner listener sets WAL + busy_timeout=30000 +
    synchronous=NORMAL + foreign_keys=ON on a REAL pooled connection (TR5)."""
    eng = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'pragma.db'}")
    install_community_sqlite_pragmas(eng)

    async def read():
        async with eng.connect() as conn:
            jm = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
            bt = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
            sy = (await conn.exec_driver_sql("PRAGMA synchronous")).scalar()
            fk = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        await eng.dispose()
        return jm, bt, sy, fk

    jm, bt, sy, fk = asyncio.run(read())
    assert str(jm).lower() == "wal"  # journal_mode=WAL
    assert int(bt) == 30000  # busy_timeout=30000
    assert int(sy) == 1  # synchronous=NORMAL (1)
    assert int(fk) == 1  # foreign_keys=ON (1)


def test_ac1_pool_observability_installed(tmp_path):
    """Observability listeners install and the pool exposes a status snapshot."""
    eng = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'obs.db'}")
    install_community_pool_observability(eng)
    install_community_sqlite_pragmas(eng)

    async def cycle():
        async with eng.connect() as conn:
            await conn.exec_driver_sql("SELECT 1")
        status = community_pool_status(eng)
        await eng.dispose()
        return status

    status = asyncio.run(cycle())
    assert isinstance(status, str)
    assert "Pool size" in status


def test_configure_community_database_injects_core_runtime(tmp_path):
    import okto_pulse.core.infra.database as _db

    runtime = None
    try:
        runtime = configure_community_database(
            f"sqlite+aiosqlite:///{tmp_path / 'configured.db'}"
        )
        assert _db.get_engine() is runtime.engine
        assert _db.get_session_factory() is runtime.session_factory
        assert runtime.engine.pool.size() == 20
    finally:
        if runtime is not None:
            asyncio.run(runtime.engine.dispose())
