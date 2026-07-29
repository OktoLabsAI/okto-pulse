"""Community SQLAlchemy engine/session adapters.

This module is the local-first relational adapter for the Community edition. It
owns engine construction, session-factory construction, SQLite hardening and
pool observability, then injects the resulting runtime into the core facade.

Pool config is preserved EXACTLY for the Local First SQLite runtime (deadlock
fix report 2026-04-29 / bug d0f6bab2): ``pool_size=20/max_overflow=30/
pool_timeout=10/pool_recycle=1800/pool_pre_ping=True``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filelock import AsyncFileLock
from filelock import Timeout as FileLockTimeout
from okto_pulse.core.domain.realm import RealmScope
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)
_SESSION_CLEANUP_DRAIN_TIMEOUT_S = 5.0
_SCHEMA_PROCESS_LOCK_TIMEOUT_S = 300.0
_checkout_owner: ContextVar[str] = ContextVar(
    "community_database_checkout_owner",
    default="unattributed",
)


class CommunityDatabasePathUnavailable(RuntimeError):
    """The active Community database has no local SQLite file path."""


class CommunitySchemaLifecycleLockTimeout(RuntimeError):
    """The bounded cross-process SQLite lifecycle lock could not be acquired."""


def _schema_process_lock_path(runtime: CommunityDatabaseRuntime) -> Path | None:
    database_path = runtime.local_database_path()
    if database_path is None:
        return None
    return database_path.with_name(
        f"{database_path.name}.schema-lifecycle.lock"
    )


@asynccontextmanager
async def _serialized_schema_lifecycle(
    runtime: CommunityDatabaseRuntime,
) -> AsyncIterator[None]:
    lock_path = _schema_process_lock_path(runtime)
    if lock_path is None:
        # In-memory SQLite cannot be shared by independent processes.
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = AsyncFileLock(
        str(lock_path),
        timeout=_SCHEMA_PROCESS_LOCK_TIMEOUT_S,
    )
    try:
        await process_lock.acquire()
    except FileLockTimeout as exc:
        raise CommunitySchemaLifecycleLockTimeout(
            "community SQLite schema lifecycle lock timed out "
            f"after {_SCHEMA_PROCESS_LOCK_TIMEOUT_S:.1f}s: {lock_path}"
        ) from exc
    try:
        yield
    finally:
        await asyncio.shield(process_lock.release())


@dataclass(frozen=True)
class CommunityDatabaseRuntime:
    """Local-first implementation of Core's relational runtime port."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    _pending_session_closes: set[asyncio.Task[Any]] = field(
        default_factory=set,
        init=False,
        repr=False,
        compare=False,
    )

    async def _await_cleanup(self, awaitable: Awaitable[None]) -> None:
        task = asyncio.ensure_future(awaitable)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_consume_cleanup_exception)
            raise

    async def _quiet_cleanup(self, awaitable: Awaitable[None]) -> None:
        with contextlib.suppress(BaseException):
            await self._await_cleanup(awaitable)

    async def _cancel_safe_close(self, awaitable: Awaitable[None]) -> None:
        task = asyncio.create_task(awaitable)
        self._pending_session_closes.add(task)
        task.add_done_callback(self._pending_session_closes.discard)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # The caller must observe cancellation immediately, while the
            # teardown remains alive long enough to finish rollback + close.
            # Once detached, explicitly observe and log its terminal failure;
            # otherwise asyncio reports an unstructured "Task exception was
            # never retrieved" after the connection cleanup has left scope.
            task.add_done_callback(_log_background_session_cleanup_failure)
            raise
        except Exception:
            logger.exception("db.session.cancel_safe_close_failed")

    async def _drain_pending_session_closes(
        self,
        *,
        timeout_s: float = _SESSION_CLEANUP_DRAIN_TIMEOUT_S,
    ) -> None:
        """Wait boundedly for detached rollback/close tasks before dispose.

        Shutdown normally starts only after request/worker cancellation, so
        detached session teardowns should be given a short opportunity to
        return their checked-out connections. The deadline is process-wide for
        this runtime close, including tasks that appear while an earlier batch
        is draining. A stuck driver never blocks shutdown indefinitely.
        """

        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        current = asyncio.current_task()
        while True:
            tasks = {
                task
                for task in self._pending_session_closes
                if not task.done() and task is not current
            }
            if not tasks:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning(
                    "db.session.cleanup_drain_timeout pending=%d timeout_s=%.3f",
                    len(tasks),
                    timeout_s,
                    extra={
                        "event": "db.session.cleanup_drain_timeout",
                        "pending_count": len(tasks),
                        "timeout_s": timeout_s,
                    },
                )
                return
            _done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                logger.warning(
                    "db.session.cleanup_drain_timeout pending=%d timeout_s=%.3f",
                    len(pending),
                    timeout_s,
                    extra={
                        "event": "db.session.cleanup_drain_timeout",
                        "pending_count": len(pending),
                        "timeout_s": timeout_s,
                    },
                )
                return

    async def _session_scope_teardown(
        self,
        session: Any,
        exit_: Callable[..., Awaitable[None]] | None,
        exc_info: tuple[type[BaseException] | None, BaseException | None, Any],
    ) -> None:
        """Rollback an active transaction and always release its session.

        MCP handlers may catch a database exception inside the scope, so the
        context manager cannot rely on ``exc_info`` alone to decide whether a
        rollback is required.  Conversely, an explicitly committed happy path
        has no active transaction and must not receive a redundant rollback.
        The caller shields this complete teardown as one task so cancellation
        cannot strand the connection between rollback and close.
        """

        try:
            in_transaction = getattr(session, "in_transaction", None)
            if callable(in_transaction) and in_transaction():
                await session.rollback()
        finally:
            if exit_ is not None:
                await exit_(*exc_info)
            else:
                await session.close()

    @asynccontextmanager
    async def transactional_session(self) -> AsyncIterator[AsyncSession]:
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except BaseException:
            await self._quiet_cleanup(session.rollback())
            raise
        finally:
            await self._quiet_cleanup(session.close())

    @asynccontextmanager
    async def cancel_safe_session_scope(
        self,
        session_factory: Callable[[], Any] | None = None,
        *,
        owner: str = "community.session_scope",
    ) -> AsyncIterator[Any]:
        with community_database_checkout_owner(owner):
            scope = (session_factory or self.session_factory)()
            enter = getattr(scope, "__aenter__", None)
            exit_ = getattr(scope, "__aexit__", None)
            if callable(enter) and callable(exit_):
                session = await enter()
            else:
                session = scope
                exit_ = None
            exc_info: tuple[type[BaseException] | None, BaseException | None, Any] = (
                None,
                None,
                None,
            )
            try:
                yield session
            except BaseException as exc:
                exc_info = (type(exc), exc, exc.__traceback__)
                raise
            finally:
                await self._cancel_safe_close(
                    self._session_scope_teardown(session, exit_, exc_info)
                )

    async def close(self) -> None:
        try:
            from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
                drain_pending_uow_cleanups,
            )

            await drain_pending_uow_cleanups()
        finally:
            try:
                await self._drain_pending_session_closes(
                    timeout_s=_SESSION_CLEANUP_DRAIN_TIMEOUT_S,
                )
            finally:
                await self._await_cleanup(self.engine.dispose())

    def pool_status(self) -> str:
        return community_pool_status(self.engine)

    def local_database_path(self) -> Path | None:
        url = self.engine.url
        if url.get_backend_name() != "sqlite":
            return None
        database = url.database
        if not database or str(database) in {":memory:", "file::memory:"}:
            return None
        return Path(str(database))


def _consume_cleanup_exception(task: asyncio.Future[None]) -> None:
    if task.cancelled():
        return
    with contextlib.suppress(BaseException):
        task.exception()


def _log_background_session_cleanup_failure(
    task: asyncio.Future[None],
) -> None:
    """Consume a detached session teardown failure with structured evidence."""

    if task.cancelled():
        logger.error(
            "db.session.background_cleanup_cancelled",
            extra={"event": "db.session.background_cleanup_cancelled"},
        )
        return
    try:
        task.result()
    except BaseException as exc:  # cleanup failure must never escape callback
        logger.error(
            "db.session.background_cleanup_failed error_type=%s",
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "event": "db.session.background_cleanup_failed",
                "error_type": type(exc).__name__,
            },
        )


@contextmanager
def community_database_checkout_owner(owner: str):
    token = _checkout_owner.set(owner)
    try:
        yield
    finally:
        _checkout_owner.reset(token)


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


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _task_diagnostic(task: asyncio.Task[Any] | None, task_name: str) -> str:
    if task is None:
        return f"{task_name}:unavailable"
    state = "cancelled" if task.cancelled() else "done" if task.done() else "active"
    frames = task.get_stack(limit=12) if not task.done() else []
    application_frames = [
        frame
        for frame in frames
        if "okto_pulse" in frame.f_code.co_filename.replace("\\", "/")
    ]
    selected = application_frames[-4:] or frames[-2:]
    location = ">".join(
        f"{Path(frame.f_code.co_filename).name}:{frame.f_lineno}:{frame.f_code.co_name}"
        for frame in selected
    )
    return f"{task_name}:{state}:{location or 'no-stack'}"


def _checkout_diagnostic() -> str:
    frames = traceback.extract_stack(limit=50)
    application_frames = [
        frame
        for frame in frames
        if "/okto_pulse/" in frame.filename.replace("\\", "/")
        and Path(frame.filename).name != Path(__file__).name
    ]
    return ">".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in application_frames[-8:]
    )


def install_community_pool_observability(engine: AsyncEngine) -> None:
    """Install checkout/checkin/close listeners that warn on stale checkouts
    BEFORE pool exhaustion, mirroring the core observability hooks (TR4)."""
    sync_engine = engine.sync_engine
    capture_checkout_sites = os.environ.get(
        "OKTO_PULSE_DB_CHECKOUT_TRACE",
        "",
    ).strip().lower() in {"1", "true", "yes"}
    checked_out: dict[
        int,
        tuple[float, str, asyncio.Task[Any] | None, str, str, str],
    ] = {}
    last_stale_warn_at = 0.0

    @event.listens_for(sync_engine, "checkout")
    def _on_checkout(_dbapi_conn, conn_record, _conn_proxy):  # noqa: ANN001
        nonlocal last_stale_warn_at
        now = time.monotonic()
        task = _current_task()
        task_name = task.get_name() if task is not None else "no-async-task"
        checkout_site = _checkout_diagnostic() if capture_checkout_sites else ""
        owner = _checkout_owner.get()
        record_id = id(conn_record)
        conn_record.info["pulse_checkout_record_id"] = record_id
        checked_out[record_id] = (
            now,
            task_name,
            task,
            checkout_site,
            owner,
            "",
        )
        stale = [
            (
                now - started_at,
                task_name,
                checkout_task,
                checkout_site,
                owner,
                statement,
            )
            for (
                started_at,
                task_name,
                checkout_task,
                checkout_site,
                owner,
                statement,
            ) in checked_out.values()
            if now - started_at > _POOL_STALE_CHECKOUT_WARN_SECONDS
        ]
        if (
            stale
            and now - last_stale_warn_at > _POOL_STALE_WARN_INTERVAL_SECONDS
        ):
            last_stale_warn_at = now
            oldest = max(
                age
                for age, _task_name, _task, _site, _owner, _statement in stale
            )
            stale_tasks = sorted(
                {
                    task_name
                    for _age, task_name, _task, _site, _owner, _statement in stale
                }
            )
            task_diagnostics = sorted(
                {
                    _task_diagnostic(checkout_task, task_name)
                    for (
                        _age,
                        task_name,
                        checkout_task,
                        _site,
                        _owner,
                        _statement,
                    ) in stale
                }
            )
            checkout_sites = sorted(
                {
                    site
                    for _age, _task_name, _task, site, _owner, _statement in stale
                    if site
                }
            )
            checkout_owners = sorted(
                {
                    owner
                    for _age, _task_name, _task, _site, owner, _statement in stale
                }
            )
            last_statements = sorted(
                {
                    statement
                    for _age, _task_name, _task, _site, _owner, statement in stale
                    if statement
                }
            )
            logger.warning(
                "db.pool.stale_checkouts count=%d oldest_s=%.0f tasks=%s "
                "task_diagnostics=%s checkout_sites=%s checkout_owners=%s "
                "last_statements=%s pool=%s",
                len(stale),
                oldest,
                stale_tasks,
                task_diagnostics,
                checkout_sites,
                checkout_owners,
                last_statements,
                sync_engine.pool.status(),
                extra={
                    "event": "db.pool.stale_checkouts",
                    "count": len(stale),
                    "oldest_s": round(oldest),
                    "tasks": stale_tasks,
                    "task_diagnostics": task_diagnostics,
                    "checkout_sites": checkout_sites,
                    "checkout_owners": checkout_owners,
                    "last_statements": last_statements,
                    "pool_status": sync_engine.pool.status(),
                },
            )

    @event.listens_for(sync_engine, "before_cursor_execute")
    def _on_cursor_execute(conn, _cursor, statement, _parameters, _context, _many):  # noqa: ANN001
        record_id = conn.info.get("pulse_checkout_record_id")
        observation = checked_out.get(record_id)
        if observation is None:
            return
        normalized_statement = " ".join(str(statement).split())[:500]
        checked_out[record_id] = (
            observation[0],
            observation[1],
            observation[2],
            observation[3],
            _checkout_owner.get(),
            normalized_statement,
        )

    @event.listens_for(sync_engine, "checkin")
    def _on_checkin(_dbapi_conn, conn_record):  # noqa: ANN001
        checked_out.pop(id(conn_record), None)

    @event.listens_for(sync_engine, "close")
    def _on_close(_dbapi_conn, conn_record):  # noqa: ANN001
        checked_out.pop(id(conn_record), None)


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
    runtime = CommunityDatabaseRuntime(
        engine=engine,
        session_factory=session_factory,
    )
    configure_database_runtime(runtime=runtime)
    return runtime


def resolve_community_database_runtime() -> CommunityDatabaseRuntime:
    """Resolve the active runtime and enforce Community adapter ownership."""

    from okto_pulse.core.ports.relational_runtime import resolve_database_runtime

    runtime = resolve_database_runtime()
    if not isinstance(runtime, CommunityDatabaseRuntime):
        raise RuntimeError("active relational runtime is not Community-owned")
    return runtime


def get_engine() -> AsyncEngine:
    """Return the Community-owned SQLAlchemy engine."""

    return resolve_community_database_runtime().engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the Community-owned SQLAlchemy session factory."""

    return resolve_community_database_runtime().session_factory


async def init_db() -> None:
    """Run the composed schema lifecycle through the Core lifecycle port."""

    from okto_pulse.core.ports.relational_runtime import init_db as initialize_schema

    runtime = resolve_community_database_runtime()
    async with _serialized_schema_lifecycle(runtime):
        await initialize_schema()


async def close_db() -> None:
    """Close the Community-owned runtime."""

    await resolve_community_database_runtime().close()


def is_database_runtime_configured() -> bool:
    from okto_pulse.core.ports.relational_runtime import (
        is_database_runtime_configured as is_configured,
    )

    return is_configured()


def resolve_sqlite_database_path() -> Path:
    try:
        path = resolve_community_database_runtime().local_database_path()
    except RuntimeError as exc:
        raise CommunityDatabasePathUnavailable(
            "Community database runtime has no local SQLite path"
        ) from exc
    if path is None:
        raise CommunityDatabasePathUnavailable(
            "Community database runtime has no local SQLite path"
        )
    return path


__all__ = [
    "build_community_engine",
    "build_community_session_factory",
    "configure_community_database",
    "install_community_sqlite_pragmas",
    "install_community_pool_observability",
    "community_pool_status",
    "community_database_checkout_owner",
    "CommunityDatabaseRuntime",
    "CommunityDatabasePathUnavailable",
    "CommunitySchemaLifecycleLockTimeout",
    "close_db",
    "get_engine",
    "get_session_factory",
    "init_db",
    "is_database_runtime_configured",
    "resolve_community_database_runtime",
    "resolve_sqlite_database_path",
]
