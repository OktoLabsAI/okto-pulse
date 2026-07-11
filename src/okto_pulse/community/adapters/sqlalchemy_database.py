"""Community SQLAlchemy engine/session adapters.

This module is the local-first relational adapter for the Community edition. It
owns engine construction, session-factory construction, SQLite hardening and
pool observability, then injects the resulting runtime into the core facade.

Pool config is preserved EXACTLY for the Local First SQLite runtime (deadlock
fix report 2026-04-29 / bug d0f6bab2): ``pool_size=20/max_overflow=30/
pool_timeout=10/pool_recycle=1800/pool_pre_ping=True``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from okto_pulse.core.domain.realm import RealmScope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommunityDatabaseRuntime:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def build_community_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the async engine with Community local-first pool configuration."""
    if not url.startswith("sqlite"):
        raise ValueError("community_database_requires_sqlite")
    engine_kwargs: dict = {
        "echo": echo,
        "future": True,
        "pool_size": 20,
        "max_overflow": 30,
        "pool_timeout": 10,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    }

    return create_async_engine(url, **engine_kwargs)


def build_community_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Async session factory with the EXACT core kwargs.

    ``class_=AsyncSession`` and ``expire_on_commit=False`` are preserved so the
    repositories keep read-after-write semantics on committed instances.
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        info={"realm_scope": RealmScope.local()},
    )


def install_community_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Single-owner SQLite PRAGMA listener.

    Fires per pooled connection (``@event.listens_for(sync_engine, "connect")``)
    so EVERY connection — not just the first — carries WAL + busy_timeout +
    synchronous + foreign_keys. No-op for non-SQLite engines.
    """
    if engine.url.get_backend_name() != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_community_sqlite_pragmas(dbapi_conn, _conn_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


# ---------------------------------------------------------------------------
# Pool observability — leak detection (mirrors core _install_pool_observability)
# ---------------------------------------------------------------------------

_POOL_STALE_CHECKOUT_WARN_SECONDS = 30.0
_POOL_STALE_WARN_INTERVAL_SECONDS = 60.0
_community_checked_out_since: dict[int, float] = {}
_community_last_stale_warn_at: float = 0.0


def install_community_pool_observability(engine: AsyncEngine) -> None:
    """Install checkout/checkin/close listeners that warn on stale checkouts
    BEFORE pool exhaustion, mirroring the core observability hooks (TR4)."""
    sync_engine = engine.sync_engine

    @event.listens_for(sync_engine, "checkout")
    def _on_checkout(_dbapi_conn, conn_record, _conn_proxy):  # noqa: ANN001
        global _community_last_stale_warn_at
        now = time.monotonic()
        _community_checked_out_since[id(conn_record)] = now
        stale_ages = [
            now - ts
            for ts in _community_checked_out_since.values()
            if now - ts > _POOL_STALE_CHECKOUT_WARN_SECONDS
        ]
        if (
            stale_ages
            and now - _community_last_stale_warn_at
            > _POOL_STALE_WARN_INTERVAL_SECONDS
        ):
            _community_last_stale_warn_at = now
            logger.warning(
                "db.pool.stale_checkouts count=%d oldest_s=%.0f pool=%s",
                len(stale_ages),
                max(stale_ages),
                sync_engine.pool.status(),
                extra={
                    "event": "db.pool.stale_checkouts",
                    "count": len(stale_ages),
                    "oldest_s": round(max(stale_ages)),
                    "pool_status": sync_engine.pool.status(),
                },
            )

    @event.listens_for(sync_engine, "checkin")
    def _on_checkin(_dbapi_conn, conn_record):  # noqa: ANN001
        _community_checked_out_since.pop(id(conn_record), None)

    @event.listens_for(sync_engine, "close")
    def _on_close(_dbapi_conn, conn_record):  # noqa: ANN001
        _community_checked_out_since.pop(id(conn_record), None)


def community_pool_status(engine: AsyncEngine) -> str:
    """Readable pool snapshot (size/checked-out/overflow) for diagnostics."""
    return engine.sync_engine.pool.status()


def configure_community_database(
    url: str,
    *,
    echo: bool = False,
) -> CommunityDatabaseRuntime:
    """Build and inject the Community relational runtime into core."""
    from okto_pulse.core.ports.relational_runtime import configure_database_runtime

    engine = build_community_engine(url, echo=echo)
    install_community_sqlite_pragmas(engine)
    install_community_pool_observability(engine)
    session_factory = build_community_session_factory(engine)
    configure_database_runtime(engine=engine, session_factory=session_factory)
    return CommunityDatabaseRuntime(engine=engine, session_factory=session_factory)


__all__ = [
    "build_community_engine",
    "build_community_session_factory",
    "configure_community_database",
    "install_community_sqlite_pragmas",
    "install_community_pool_observability",
    "community_pool_status",
    "CommunityDatabaseRuntime",
]
