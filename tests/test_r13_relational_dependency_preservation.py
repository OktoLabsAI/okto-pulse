"""R13 - relational dependency preservation without asyncpg in Community smoke."""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession

import okto_pulse.community.adapters.sqlalchemy_database as _db


def test_r13_sqlalchemy_adapter_import_does_not_import_asyncpg() -> None:
    assert "asyncpg" not in sys.modules


def test_r13_sqlite_engine_preserves_pool_kwargs_and_future(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel_engine = object()

    def fake_create_async_engine(url: str, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return sentinel_engine

    monkeypatch.setattr(_db, "create_async_engine", fake_create_async_engine)

    engine = _db.build_community_engine("sqlite+aiosqlite:///pulse.db", echo=True)

    assert engine is sentinel_engine
    assert captured["url"] == "sqlite+aiosqlite:///pulse.db"
    assert captured["kwargs"] == {
        "echo": True,
        "future": True,
        "pool_size": 20,
        "max_overflow": 30,
        "pool_timeout": 10,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    }


def test_r13_session_factory_preserves_async_session_kwargs(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel_engine = object()
    sentinel_factory = object()

    def fake_async_sessionmaker(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return sentinel_factory

    monkeypatch.setattr(_db, "async_sessionmaker", fake_async_sessionmaker)

    factory = _db.build_community_session_factory(sentinel_engine)  # type: ignore[arg-type]

    assert factory is sentinel_factory
    assert captured["args"] == (sentinel_engine,)
    assert captured["kwargs"] == {
        "class_": AsyncSession,
        "expire_on_commit": False,
    }


def test_r13_sqlite_pragma_union_on_real_connection(tmp_path) -> None:
    engine = _db.build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'pragma.db'}")
    _db.install_community_sqlite_pragmas(engine)

    async def read_pragmas() -> tuple[str, int, int, int]:
        async with engine.connect() as conn:
            journal_mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
            busy_timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
            synchronous = (await conn.exec_driver_sql("PRAGMA synchronous")).scalar()
            foreign_keys = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        await engine.dispose()
        return str(journal_mode), int(busy_timeout), int(synchronous), int(foreign_keys)

    journal_mode, busy_timeout, synchronous, foreign_keys = asyncio.run(read_pragmas())

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 30000
    assert synchronous == 1
    assert foreign_keys == 1
