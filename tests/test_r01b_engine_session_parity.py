"""R01B REPLAN-IMP1 (AC1) — Community engine/session/PRAGMA/pool parity.

Proves the Community-owned relational adapters mirror the core
``okto_pulse.core.infra.database`` configuration EXACTLY:

  - pool sizing per dialect (parity oracle vs a core engine built side-by-side);
  - session factory kwargs (``class_=AsyncSession`` + ``expire_on_commit=False``);
  - the SINGLE-OWNER SQLite PRAGMA union — WAL + busy_timeout=30000 +
    synchronous=NORMAL + foreign_keys=ON — proven against a REAL connection;
  - pool observability listeners installed (status snapshot).

Additive/DORMANT: these builders never replace the core module globals; the
fixtures that touch ``create_database`` save/restore them.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Importing the core app registers every ORM model on Base.metadata (parity
# oracle uses create_database which the import keeps consistent).
import okto_pulse.core.app as _core_app  # noqa: F401
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_engine,
    build_community_session_factory,
    community_pool_status,
    install_community_pool_observability,
    install_community_sqlite_pragmas,
)


def test_ac1_sqlite_pool_config_matches_core(tmp_path):
    """The Community sqlite engine carries the EXACT core pool config (a parity
    oracle against a core engine + a literal anchor for the documented kwargs)."""
    import okto_pulse.core.infra.database as _db

    ce = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'community.db'}")
    saved_e, saved_f = _db._engine, _db._session_factory
    try:
        _db.create_database(f"sqlite+aiosqlite:///{tmp_path / 'core.db'}")
        core_e = _db.get_engine()

        assert ce.echo is False and core_e.echo is False
        # parity vs core (catches future core drift)
        assert ce.pool.size() == core_e.pool.size() == 20
        for priv in ("_max_overflow", "_timeout", "_recycle", "_pre_ping"):
            assert getattr(ce.pool, priv) == getattr(core_e.pool, priv), priv
        # literal anchor — the documented sqlite kwargs are preserved verbatim
        assert ce.pool._max_overflow == 30
        assert ce.pool._timeout == 10
        assert ce.pool._recycle == 1800
        assert ce.pool._pre_ping is True

        async def _cleanup() -> None:
            await ce.dispose()
            await core_e.dispose()

        asyncio.run(_cleanup())
    finally:
        _db._engine, _db._session_factory = saved_e, saved_f


def test_ac1_postgresql_pool_config():
    """The postgresql branch carries pool_size=10/max_overflow=20/pre_ping
    (skipped if the asyncpg dbapi is not installed in this env)."""
    try:
        pe = build_community_engine("postgresql+asyncpg://u:p@localhost/db")
    except Exception:  # pragma: no cover - depends on optional dbapi
        pytest.skip("asyncpg dbapi not available")
    try:
        assert pe.pool.size() == 10
        assert pe.pool._max_overflow == 20
        assert pe.pool._pre_ping is True
    finally:
        asyncio.run(pe.dispose())


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
    assert str(jm).lower() == "wal"        # journal_mode=WAL
    assert int(bt) == 30000                # busy_timeout=30000
    assert int(sy) == 1                    # synchronous=NORMAL (1)
    assert int(fk) == 1                    # foreign_keys=ON (1)


def test_ac1_pragma_listener_noop_for_non_sqlite():
    """install_community_sqlite_pragmas is a no-op for non-sqlite engines."""
    try:
        pe = build_community_engine("postgresql+asyncpg://u:p@localhost/db")
    except Exception:  # pragma: no cover - depends on optional dbapi
        pytest.skip("asyncpg dbapi not available")
    try:
        # Must not raise / must not register a sqlite connect listener.
        install_community_sqlite_pragmas(pe)
    finally:
        asyncio.run(pe.dispose())


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
