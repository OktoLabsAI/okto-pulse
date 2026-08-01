"""R01B REPLAN-IMP1 (AC2) — Community UnitOfWork parity (commit/rollback/close).

Proves the Community ``CommunityUnitOfWork`` + factory mirror the core
``SQLAlchemyUnitOfWork`` semantics against a REAL SQLite database:

  - commit persists; read-after-write inside the same transaction (autoflush);
  - rollback discards a flushed-but-uncommitted row (the teardown invariant);
  - explicit rollback discards; the session always closes (one teardown path);
  - the unit of work satisfies the core ports (PulseUnitOfWork + the three
    repository Protocols), and realm_id/actor are carried-not-enforced.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from okto_pulse.community.adapters.sqlalchemy_database import (
    CommunityDatabaseRuntime,
    build_community_engine,
    build_community_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board as BoardRow,
    Card as CardRow,
)
from okto_pulse.community.adapters import sqlalchemy_unit_of_work as uow_mod
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
    build_community_unit_of_work_factory,
)
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.knowledge_propagation import (
    KnowledgeCreationRaceError,
)
from okto_pulse.core.domain.entities import Board
from okto_pulse.core.domain.knowledge_selection import KnowledgeTargetType
from okto_pulse.core.ports.knowledge_propagation import KnowledgeTargetKey


@pytest.fixture
def _temp_session_factory(tmp_path):
    """Temp SQLite DB built entirely by Community relational adapters."""
    engine = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'r01b_uow.db'}")
    session_factory = build_community_session_factory(engine)

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    try:
        yield session_factory
    finally:
        asyncio.run(engine.dispose())


def _board(board_id: str) -> Board:
    return Board(id=board_id, name="R01B", owner_id="r01b-user", settings={})


class _TeardownSession:
    def __init__(self, *, fail_rollback: bool = False) -> None:
        self.active = True
        self.fail_rollback = fail_rollback
        self.rollback_calls = 0
        self.close_calls = 0
        self.rollback_started = asyncio.Event()
        self.rollback_release = asyncio.Event()

    def in_transaction(self) -> bool:
        return self.active

    async def rollback(self) -> None:
        self.rollback_calls += 1
        self.rollback_started.set()
        await self.rollback_release.wait()
        self.active = False
        if self.fail_rollback:
            raise RuntimeError("modeled UoW rollback cleanup failure")

    async def close(self) -> None:
        self.close_calls += 1


def _teardown_uow(session: _TeardownSession) -> CommunityUnitOfWork:
    """Build only the lifecycle surface needed by cancellation regressions."""

    uow = object.__new__(CommunityUnitOfWork)
    uow._session = session
    uow.rollback = session.rollback
    uow.close = session.close
    return uow


def test_ac2_commit_persists_and_read_after_write(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-commit"

    async def drive():
        async with factory() as uow:
            await uow.boards.add(_board(board_id))
            # read-after-write inside the same transaction (autoflush=True)
            seen = await uow.boards.get(board_id)
            await uow.commit()
        # a FRESH session confirms the row was committed
        async with sf() as s:
            row = (
                await s.execute(select(BoardRow).where(BoardRow.id == board_id))
            ).scalar_one_or_none()
        return seen, row

    seen, row = asyncio.run(drive())
    assert seen is not None and seen.id == board_id  # read-after-write
    assert row is not None and row.name == "R01B"  # committed


def test_ac2_rollback_on_error_discards_flushed_row(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-rollback"

    async def drive():
        # An exception inside the context triggers __aexit__(exc) -> rollback+close;
        # __aexit__ returns None so the exception is NOT suppressed.
        with pytest.raises(RuntimeError):
            async with factory() as uow:
                await uow.boards.add(_board(board_id))
                await uow.boards.get(board_id)  # autoflush -> INSERT in the txn
                raise RuntimeError("boom")
        async with sf() as s:
            row = (
                await s.execute(select(BoardRow).where(BoardRow.id == board_id))
            ).scalar_one_or_none()
        return row

    assert asyncio.run(drive()) is None  # rolled back, never committed


def test_ac2_explicit_rollback_discards(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-explicit-rb"

    async def drive():
        async with factory() as uow:
            await uow.boards.add(_board(board_id))
            await uow.rollback()
        async with sf() as s:
            row = (
                await s.execute(select(BoardRow).where(BoardRow.id == board_id))
            ).scalar_one_or_none()
        return row

    assert asyncio.run(drive()) is None


def test_synchronize_preserves_integrity_error_by_default(
    _temp_session_factory,
):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-sync-default-conflict"

    async def drive() -> None:
        async with factory() as uow:
            await uow.boards.add(_board(board_id))
            await uow.commit()
        with pytest.raises(IntegrityError):
            async with factory() as uow:
                await uow.boards.add(_board(board_id))
                await uow.synchronize()

    asyncio.run(drive())


def test_synchronize_maps_only_target_integrity_error_to_supplied_conflict(
    _temp_session_factory,
):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-sync-mapped-conflict"
    target = KnowledgeTargetKey(
        board_id=board_id,
        target_type=KnowledgeTargetType.CARD,
        target_id="r01b-deterministic-card",
    )

    async def drive() -> None:
        async with factory() as uow:
            await uow.boards.add(_board(board_id))
            await uow.commit()
        async with sf() as session:
            session.add(
                CardRow(
                    id=target.target_id,
                    board_id=board_id,
                    title="winner",
                    created_by="r01b-user",
                )
            )
            await session.commit()
        modeled = KnowledgeCreationRaceError(target)
        with pytest.raises(KnowledgeCreationRaceError) as raised:
            async with factory() as uow:
                uow._session.add(
                    CardRow(
                        id=target.target_id,
                        board_id=board_id,
                        title="loser",
                        created_by="r01b-user",
                    )
                )
                await uow.synchronize(conflict_error=modeled)
        assert raised.value is modeled
        assert isinstance(raised.value.__cause__, IntegrityError)

    asyncio.run(drive())


def test_synchronize_preserves_non_target_integrity_error_with_race_envelope(
    _temp_session_factory,
):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    board_id = "r01b-sync-unrelated-conflict"
    target = KnowledgeTargetKey(
        board_id=board_id,
        target_type=KnowledgeTargetType.CARD,
        target_id="r01b-unrelated-card",
    )

    async def drive() -> None:
        async with factory() as uow:
            await uow.boards.add(_board(board_id))
            await uow.commit()
        modeled = KnowledgeCreationRaceError(target)
        with pytest.raises(IntegrityError) as raised:
            async with factory() as uow:
                await uow.boards.add(_board(board_id))
                await uow.synchronize(conflict_error=modeled)
        assert raised.value is not modeled

    asyncio.run(drive())


def test_ac2_unit_of_work_satisfies_ports(_temp_session_factory):
    from okto_pulse.core.repositories.interfaces.repositories import (
        BoardRepository,
        IdeationRepository,
        SpecRepository,
    )
    from okto_pulse.core.repositories.interfaces.unit_of_work import PulseUnitOfWork

    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)

    async def drive():
        async with factory() as uow:
            checks = (
                isinstance(uow, PulseUnitOfWork),
                isinstance(uow.boards, BoardRepository),
                isinstance(uow.ideations, IdeationRepository),
                isinstance(uow.specs, SpecRepository),
                not hasattr(uow, "session"),
                uow.services is not None,
                uow.realm_id,
                uow.actor,
            )
            await uow.rollback()
        return checks

    is_uow, is_b, is_i, is_s, no_session, has_services, realm, actor = asyncio.run(
        drive()
    )
    assert is_uow and is_b and is_i and is_s and no_session and has_services
    # Community composition binds the legacy data set to RealmScope.local().
    assert realm == "local" and actor is None


def test_ac2_factory_carries_realm_and_actor(_temp_session_factory):
    sf = _temp_session_factory
    factory = build_community_unit_of_work_factory(sf)
    expected_actor = ActorContext("actor-x", "system", realm_id="realm-1")

    async def drive():
        async with factory(realm_id="realm-1", actor=expected_actor) as uow:
            carried = (uow.realm_id, uow.actor)
            await uow.rollback()
        return carried

    realm, actor = asyncio.run(drive())
    assert realm == "realm-1" and actor is expected_actor


def test_f02_exactly_one_commit_on_success(_temp_session_factory):
    async def drive():
        async with _temp_session_factory() as session:
            uow = CommunityUnitOfWork(session)
            calls = {"commit": 0, "rollback": 0}
            original_commit = uow.commit
            original_rollback = uow.rollback

            async def counted_commit():
                calls["commit"] += 1
                await original_commit()

            async def counted_rollback():
                calls["rollback"] += 1
                await original_rollback()

            uow.commit = counted_commit
            uow.rollback = counted_rollback
            async with uow:
                await uow.boards.add(_board("f02-one-commit"))
                await uow.commit()
            return calls

    assert asyncio.run(drive()) == {"commit": 1, "rollback": 0}


def test_f02_exactly_one_rollback_on_failure(_temp_session_factory):
    async def drive():
        async with _temp_session_factory() as session:
            uow = CommunityUnitOfWork(session)
            calls = {"commit": 0, "rollback": 0}
            original_commit = uow.commit
            original_rollback = uow.rollback

            async def counted_commit():
                calls["commit"] += 1
                await original_commit()

            async def counted_rollback():
                calls["rollback"] += 1
                await original_rollback()

            uow.commit = counted_commit
            uow.rollback = counted_rollback
            with pytest.raises(RuntimeError, match="modeled failure"):
                async with uow:
                    await uow.boards.add(_board("f02-one-rollback"))
                    raise RuntimeError("modeled failure")
            return calls

    assert asyncio.run(drive()) == {"commit": 0, "rollback": 1}


def test_f02_caught_error_still_rolls_back_active_transaction(
    _temp_session_factory,
):
    async def drive():
        async with _temp_session_factory() as session:
            uow = CommunityUnitOfWork(session)
            calls = {"rollback": 0}
            original_rollback = uow.rollback

            async def counted_rollback():
                calls["rollback"] += 1
                await original_rollback()

            uow.rollback = counted_rollback
            async with uow:
                await uow.boards.add(_board("f02-caught-error"))
                # Force a real transaction before the handler catches its own
                # exception and exits the UoW with exc=None.
                await uow.boards.get("f02-caught-error")
                try:
                    raise RuntimeError("caught by handler")
                except RuntimeError:
                    pass

            async with _temp_session_factory() as check:
                row = (
                    await check.execute(
                        select(BoardRow).where(BoardRow.id == "f02-caught-error")
                    )
                ).scalar_one_or_none()
            return calls, row

    calls, row = asyncio.run(drive())
    assert calls == {"rollback": 1}
    assert row is None


@pytest.mark.asyncio(loop_scope="function")
async def test_mcp_uow_hard_cancel_returns_connection_to_pool(tmp_path) -> None:
    engine = build_community_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'r01b-uow-cancel.db'}"
    )
    factory = build_community_unit_of_work_factory(
        build_community_session_factory(engine)
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    entered = asyncio.Event()

    async def victim() -> None:
        async with factory() as uow:
            assert await uow.boards.get("missing-board") is None
            entered.set()
            await asyncio.sleep(30)

    try:
        task = asyncio.create_task(victim(), name="test.cancelled-mcp-uow")
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert engine.sync_engine.pool.checkedout() == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            if engine.sync_engine.pool.checkedout() == 0:
                break
            await asyncio.sleep(0.05)
        assert engine.sync_engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_uow_second_cancel_logs_and_consumes_cleanup_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _TeardownSession(fail_rollback=True)
    uow = _teardown_uow(session)
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    caplog.set_level(logging.ERROR, logger=uow_mod.__name__)

    async def victim() -> None:
        async with uow:
            entered.set()
            await asyncio.sleep(30)

    try:
        task = asyncio.create_task(victim())
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        await asyncio.wait_for(session.rollback_started.wait(), timeout=2)
        assert not task.done()

        # A second cancellation while shielded teardown is in flight detaches
        # that teardown from the request task. Its terminal failure must still
        # be observed and logged after the caller sees cancellation.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        session.rollback_release.set()
        for _ in range(100):
            if not uow_mod._pending_uow_cleanups:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_exception_handler)

    assert session.close_calls == 1
    assert uow_mod._pending_uow_cleanups == set()
    assert loop_errors == []
    assert any(
        getattr(record, "event", None) == "db.uow.background_cleanup_failed"
        and getattr(record, "error_type", None) == "RuntimeError"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_database_close_drains_detached_uow_before_engine_dispose() -> None:
    class _Engine:
        def __init__(self) -> None:
            self.dispose_calls = 0

        async def dispose(self) -> None:
            self.dispose_calls += 1

    session = _TeardownSession()
    uow = _teardown_uow(session)
    engine = _Engine()
    runtime = CommunityDatabaseRuntime(
        engine=cast(Any, engine),
        session_factory=cast(Any, lambda: None),
    )
    entered = asyncio.Event()

    async def victim() -> None:
        async with uow:
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(victim())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
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
    assert uow_mod._pending_uow_cleanups == set()
    assert engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_database_close_bounds_stuck_uow_cleanup_drain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Engine:
        def __init__(self) -> None:
            self.dispose_calls = 0

        async def dispose(self) -> None:
            self.dispose_calls += 1

    session = _TeardownSession()
    uow = _teardown_uow(session)
    engine = _Engine()
    runtime = CommunityDatabaseRuntime(
        engine=cast(Any, engine),
        session_factory=cast(Any, lambda: None),
    )
    entered = asyncio.Event()
    monkeypatch.setattr(uow_mod, "_UOW_CLEANUP_DRAIN_TIMEOUT_S", 0.01)
    caplog.set_level(logging.WARNING, logger=uow_mod.__name__)

    async def victim() -> None:
        async with uow:
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(victim())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    await asyncio.wait_for(session.rollback_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.wait_for(runtime.close(), timeout=1)

    assert engine.dispose_calls == 1
    assert len(uow_mod._pending_uow_cleanups) == 1
    assert any(
        getattr(record, "event", None) == "db.uow.cleanup_drain_timeout"
        and getattr(record, "pending_count", None) == 1
        for record in caplog.records
    )

    session.rollback_release.set()
    for _ in range(100):
        if not uow_mod._pending_uow_cleanups:
            break
        await asyncio.sleep(0.01)
    assert session.close_calls == 1
    assert uow_mod._pending_uow_cleanups == set()
