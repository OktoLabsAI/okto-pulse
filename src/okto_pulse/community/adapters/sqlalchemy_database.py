"""Community SQLAlchemy engine/session adapters (R01B REPLAN-IMP1).

Register-before-remove half of the relational ownership inversion: this module
mirrors the core ``okto_pulse.core.infra.database`` engine + session-factory +
PRAGMA + pool configuration so the Community edition can OWN the relational
storage seam (TR4/TR5) — WITHOUT removing the core concretes (that strangle is
IMP2) and WITHOUT re-pointing any consumer (FR3 is IMP2).

DORMANT this phase (DEC dec_ba1450dd): the live boot still runs through the core
``create_database`` and ``community/main.py``; these builders are registered and
observable via the Community composition root (``app.state.runtime_composition``)
but do NOT replace the core module globals. The cutover is IMP2.

PRAGMA single-owner (TR5): production today runs TWO connect listeners against
SQLite — the core ``create_database`` sets ``journal_mode=WAL`` +
``busy_timeout=30000`` + ``synchronous=NORMAL`` and
``community/main.py:_configure_sqlite_pragmas`` adds ``journal_mode=WAL`` +
``foreign_keys=ON`` on top. ``install_community_sqlite_pragmas`` reconciles BOTH
into a single connect listener — the effective UNION — so the Community-owned
engine carries identical session semantics. The live reconciliation that removes
the two duplicate points is part of the IMP2 cutover; here the union is only
proven on the Community-built engine.

Pool config is preserved EXACTLY (deadlock fix report 2026-04-29 / bug d0f6bab2):
postgresql ``pool_size=10/max_overflow=20/pool_pre_ping=True``; sqlite
``pool_size=20/max_overflow=30/pool_timeout=10/pool_recycle=1800/pool_pre_ping=True``.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)


def build_community_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the async engine with the EXACT core pool configuration (TR4).

    Mirrors ``okto_pulse.core.infra.database.create_database`` byte-for-byte:
    base ``echo``/``future=True`` kwargs plus the dialect-specific pool sizing.
    Unlike the core function this returns the engine instead of assigning a
    module global — DORMANT, the Community composition owns the lifecycle.
    """
    engine_kwargs: dict = {
        "echo": echo,
        "future": True,
    }
    if url.startswith("postgresql"):
        engine_kwargs.update(
            {
                "pool_size": 10,
                "max_overflow": 20,
                "pool_pre_ping": True,
            }
        )
    elif url.startswith("sqlite"):
        engine_kwargs.update(
            {
                "pool_size": 20,
                "max_overflow": 30,
                "pool_timeout": 10,
                "pool_recycle": 1800,
                "pool_pre_ping": True,
            }
        )

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
    )


def install_community_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Single-owner SQLite PRAGMA listener (TR5 reconciliation).

    Fires per pooled connection (``@event.listens_for(sync_engine, "connect")``)
    so EVERY connection — not just the first — carries WAL + busy_timeout +
    synchronous + foreign_keys. This is the UNION of the two production listeners
    (core ``create_database`` and ``community/main.py:_configure_sqlite_pragmas``).
    No-op for non-SQLite engines.
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


__all__ = [
    "build_community_engine",
    "build_community_session_factory",
    "install_community_sqlite_pragmas",
    "install_community_pool_observability",
    "community_pool_status",
]
