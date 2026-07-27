from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import PendingRollbackError

from okto_pulse.community.adapters.sqlalchemy_database import (
    CommunityDatabaseRuntime,
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters import sqlalchemy_database as database_mod
from okto_pulse.core.kg import canonical_partition_integrity
from okto_pulse.core.ports.relational_runtime import (
    RelationalRuntime,
    configure_database_runtime,
)
from okto_pulse.core.runtime_context import (
    capture_runtime_values_for_tests,
    restore_runtime_values_for_tests,
)
from okto_pulse.core.services import kg_health_service


class _Session:
    def __init__(self, *, active: bool = False, block_rollback: bool = False) -> None:
        self.active = active
        self.rollback_calls = 0
        self.close_calls = 0
        self.rollback_started = asyncio.Event()
        self.rollback_release = asyncio.Event()
        if not block_rollback:
            self.rollback_release.set()

    def in_transaction(self) -> bool:
        return self.active

    async def rollback(self) -> None:
        self.rollback_calls += 1
        self.rollback_started.set()
        await self.rollback_release.wait()
        self.active = False

    async def close(self) -> None:
        self.close_calls += 1


def _runtime(session: _Session) -> CommunityDatabaseRuntime:
    return CommunityDatabaseRuntime(
        engine=cast(Any, object()),
        session_factory=cast(Any, lambda: session),
    )


def test_community_runtime_satisfies_cancel_safe_relational_port() -> None:
    runtime = _runtime(_Session())

    assert isinstance(runtime, RelationalRuntime)


@pytest.mark.asyncio
async def test_scope_rolls_back_transaction_after_internally_caught_error() -> None:
    session = _Session()
    runtime = _runtime(session)

    async with runtime.cancel_safe_session_scope() as scoped:
        scoped.active = True
        try:
            raise RuntimeError("handler caught this database failure")
        except RuntimeError:
            pass

    assert session.rollback_calls == 1
    assert session.close_calls == 1
    assert session.active is False


@pytest.mark.asyncio
async def test_scope_does_not_rollback_after_explicit_commit() -> None:
    session = _Session()
    runtime = _runtime(session)

    async with runtime.cancel_safe_session_scope() as scoped:
        scoped.active = True
        # Model an explicit successful commit: SQLAlchemy has no active
        # transaction by the time the scope's teardown begins.
        scoped.active = False

    assert session.rollback_calls == 0
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_scope_cancellation_cannot_interrupt_rollback_then_close() -> None:
    session = _Session(active=True, block_rollback=True)
    runtime = _runtime(session)

    async def victim() -> None:
        async with runtime.cancel_safe_session_scope():
            pass

    task = asyncio.create_task(victim())
    await asyncio.wait_for(session.rollback_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    session.rollback_release.set()
    for _ in range(100):
        if session.close_calls == 1:
            break
        await asyncio.sleep(0.01)

    assert session.rollback_calls == 1
    assert session.close_calls == 1
    assert session.active is False


@pytest.mark.asyncio
async def test_background_cleanup_failure_is_logged_and_consumed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _FailingRollbackSession(_Session):
        async def rollback(self) -> None:
            await super().rollback()
            raise RuntimeError("modeled rollback cleanup failure")

    session = _FailingRollbackSession(active=True, block_rollback=True)
    runtime = _runtime(session)
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    caplog.set_level(logging.ERROR, logger=database_mod.__name__)

    async def victim() -> None:
        async with runtime.cancel_safe_session_scope():
            pass

    try:
        task = asyncio.create_task(victim())
        await asyncio.wait_for(session.rollback_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        session.rollback_release.set()
        for _ in range(100):
            if not runtime._pending_session_closes:
                break
            await asyncio.sleep(0.01)
        # Let both done callbacks (set discard + structured observer) run.
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_exception_handler)

    assert session.close_calls == 1
    assert runtime._pending_session_closes == set()
    assert loop_errors == []
    assert any(
        getattr(record, "event", None)
        == "db.session.background_cleanup_failed"
        and getattr(record, "error_type", None) == "RuntimeError"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_runtime_close_drains_pending_session_teardown_before_dispose() -> None:
    class _Engine:
        def __init__(self) -> None:
            self.dispose_calls = 0

        async def dispose(self) -> None:
            self.dispose_calls += 1

    session = _Session(active=True, block_rollback=True)
    engine = _Engine()
    runtime = CommunityDatabaseRuntime(
        engine=cast(Any, engine),
        session_factory=cast(Any, lambda: session),
    )

    async def victim() -> None:
        async with runtime.cancel_safe_session_scope():
            pass

    task = asyncio.create_task(victim())
    await asyncio.wait_for(session.rollback_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    close_task = asyncio.create_task(runtime.close())
    await asyncio.sleep(0.01)
    assert not close_task.done()
    assert engine.dispose_calls == 0

    session.rollback_release.set()
    await asyncio.wait_for(close_task, timeout=2)

    assert session.close_calls == 1
    assert runtime._pending_session_closes == set()
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_runtime_close_bounds_stuck_session_cleanup_drain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Engine:
        def __init__(self) -> None:
            self.dispose_calls = 0

        async def dispose(self) -> None:
            self.dispose_calls += 1

    session = _Session(active=True, block_rollback=True)
    engine = _Engine()
    runtime = CommunityDatabaseRuntime(
        engine=cast(Any, engine),
        session_factory=cast(Any, lambda: session),
    )
    monkeypatch.setattr(database_mod, "_SESSION_CLEANUP_DRAIN_TIMEOUT_S", 0.01)
    caplog.set_level(logging.WARNING, logger=database_mod.__name__)

    async def victim() -> None:
        async with runtime.cancel_safe_session_scope():
            pass

    task = asyncio.create_task(victim())
    await asyncio.wait_for(session.rollback_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(runtime.close(), timeout=1)

    assert engine.dispose_calls == 1
    assert len(runtime._pending_session_closes) == 1
    assert any(
        getattr(record, "event", None) == "db.session.cleanup_drain_timeout"
        and getattr(record, "pending_count", None) == 1
        for record in caplog.records
    )

    # Release the modeled stuck driver so this test leaves no task behind.
    session.rollback_release.set()
    for _ in range(100):
        if not runtime._pending_session_closes:
            break
        await asyncio.sleep(0.01)
    assert session.close_calls == 1
    assert runtime._pending_session_closes == set()


@pytest.mark.asyncio
async def test_digest_overlay_sql_timeout_does_not_poison_caller_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cancelled aiosqlite query is rolled back in its isolated scope.

    This reproduces the production failure mode: cancelling SQL with
    ``asyncio.wait_for`` invalidates that connection and the next statement
    raises ``PendingRollbackError`` unless teardown performs a rollback.  The
    caller keeps an independent active transaction and remains usable.
    """

    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'digest-overlay-timeout.db'}"
    )

    @event.listens_for(engine.sync_engine, "connect")
    def install_sleep(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.create_function(
            "sleep_ms",
            1,
            lambda milliseconds: time.sleep(float(milliseconds) / 1000.0),
        )

    session_factory = build_community_session_factory(engine)
    runtime = CommunityDatabaseRuntime(
        engine=engine,
        session_factory=session_factory,
    )
    previous_runtime = capture_runtime_values_for_tests()
    configure_database_runtime(runtime=runtime)

    async def slow_overlay(
        context: Any, *, board_id: str
    ) -> dict[str, str]:
        assert board_id == "board-real-timeout"
        await context.execute(text("SELECT sleep_ms(200)"))
        return {}

    monkeypatch.setattr(
        canonical_partition_integrity,
        "pending_or_debt_exclusions",
        slow_overlay,
    )
    monkeypatch.setattr(
        kg_health_service,
        "_DIGEST_OVERLAY_TIMEOUT_S",
        0.01,
    )

    try:
        # Control: the old same-session pattern reproduces the exact production
        # failure before the fixed isolated path is exercised below.
        async with session_factory() as poisoned:
            assert await poisoned.scalar(text("SELECT 1")) == 1
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    slow_overlay(poisoned, board_id="board-real-timeout"),
                    timeout=0.01,
                )
            with pytest.raises(PendingRollbackError, match="invalid transaction"):
                await poisoned.scalar(text("SELECT 1"))
            await poisoned.rollback()

        async with session_factory() as caller:
            assert await caller.scalar(text("SELECT 1")) == 1
            assert caller.in_transaction()

            with pytest.raises(TimeoutError):
                await kg_health_service._load_digest_partition_overlay(
                    board_id="board-real-timeout"
                )

            # The isolated cancelled query has already rolled back and returned
            # its connection; only the caller's connection remains checked out.
            assert engine.sync_engine.pool.checkedout() == 1
            assert caller.in_transaction()
            assert await caller.scalar(text("SELECT 1")) == 1
    finally:
        restore_runtime_values_for_tests(previous_runtime)
        await engine.dispose()
