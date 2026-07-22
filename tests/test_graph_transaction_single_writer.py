"""Community Ladybug single-writer transaction regression coverage."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from uuid import uuid4

import pytest

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.graph_error_mapping import map_graph_error
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    CommunityKuzuGraphTransaction,
    _KuzuTransactionScope,
    _statement_kind,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention


class _FakeConnection:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, statement, params=None):
        self.executed.append((statement, params))
        if self.error is not None:
            raise self.error
        return {"ok": True}


class _FakeBoardConnection:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.db = object()
        self.conn = _FakeConnection(error)
        self.close_error = close_error
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error


def test_statement_observability_is_low_cardinality() -> None:
    assert _statement_kind("MATCH (n) RETURN n") == "MATCH_READ"
    assert _statement_kind("MATCH (n) SET n.value = $value") == "MATCH_SET"
    assert _statement_kind("PROFILE MATCH (n) DELETE n") == "MATCH_DELETE"
    assert _statement_kind("CREATE (n:Decision)") == "CREATE"
    assert _statement_kind("CALL SHOW_TABLES() RETURN name") == "CALL"
    assert _statement_kind("unrecognized private payload") == "OTHER"


def test_native_single_writer_error_maps_to_retryable_lock_contention() -> None:
    error = RuntimeError(
        "Cannot start a new write transaction in the system. "
        "Only one write transaction at a time is allowed in the system."
    )
    mapped = map_graph_error(error, operation="graph_statement")
    assert isinstance(mapped, GraphLockContention)
    assert mapped.retryable is True


@pytest.mark.asyncio
async def test_transaction_scope_serializes_process_wide_and_releases_on_commit(
    monkeypatch,
) -> None:
    opened: list[_FakeBoardConnection] = []

    def fake_open(_board_id: str) -> _FakeBoardConnection:
        connection = _FakeBoardConnection()
        opened.append(connection)
        return connection

    monkeypatch.setattr(kg_runtime, "open_board_connection", fake_open)
    board_id = f"single-writer-{uuid4().hex}"

    first = await CommunityKuzuGraphTransaction().begin(board_id)
    with pytest.raises(GraphLockContention):
        await CommunityKuzuGraphTransaction(writer_lock_timeout_s=0.02).begin(board_id)
    assert len(opened) == 1

    # Ladybug's limit is process-wide, including a different Database file.
    with pytest.raises(GraphLockContention):
        await CommunityKuzuGraphTransaction(writer_lock_timeout_s=0.02).begin(
            f"other-{board_id}"
        )

    await first.commit()
    other = await CommunityKuzuGraphTransaction(writer_lock_timeout_s=0.1).begin(
        f"other-{board_id}"
    )
    await other.commit()
    second = await CommunityKuzuGraphTransaction(writer_lock_timeout_s=0.1).begin(
        board_id
    )
    await second.commit()

    assert len(opened) == 3
    assert [connection.close_count for connection in opened] == [1, 1, 1]


@pytest.mark.asyncio
async def test_direct_scope_construction_acquires_and_releases_writer_lease(
    monkeypatch,
) -> None:
    """Keep the worker-thread compatibility path on the same global gate."""

    opened: list[_FakeBoardConnection] = []

    def fake_open(_board_id: str) -> _FakeBoardConnection:
        connection = _FakeBoardConnection()
        opened.append(connection)
        return connection

    monkeypatch.setattr(kg_runtime, "open_board_connection", fake_open)
    board_id = f"direct-scope-{uuid4().hex}"

    direct = await asyncio.to_thread(_KuzuTransactionScope, board_id)
    with pytest.raises(GraphLockContention):
        await CommunityKuzuGraphTransaction(writer_lock_timeout_s=0.02).begin(
            f"other-{board_id}"
        )

    await direct.commit()
    after_release = await CommunityKuzuGraphTransaction(
        writer_lock_timeout_s=0.1
    ).begin(board_id)
    await after_release.commit()

    assert len(opened) == 2
    assert [connection.close_count for connection in opened] == [1, 1]


@pytest.mark.asyncio
async def test_open_and_close_failures_cannot_strand_writer_lease(monkeypatch) -> None:
    attempts = 0

    def fake_open(_board_id: str) -> _FakeBoardConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("open failed")
        return _FakeBoardConnection(
            close_error=RuntimeError("close failed") if attempts == 2 else None
        )

    monkeypatch.setattr(kg_runtime, "open_board_connection", fake_open)
    board_id = f"lease-cleanup-{uuid4().hex}"
    tx = CommunityKuzuGraphTransaction(writer_lock_timeout_s=0.1)

    with pytest.raises(RuntimeError, match="open failed"):
        await tx.begin(board_id)

    close_fails = await tx.begin(board_id)
    with pytest.raises(RuntimeError, match="close failed"):
        await close_fails.commit()

    final = await tx.begin(board_id)
    await final.commit()
    assert attempts == 3


@pytest.mark.asyncio
async def test_cancelled_begin_drains_and_releases_background_acquire(
    monkeypatch,
) -> None:
    from okto_pulse.community.adapters import ladybug_writer as writer

    started = threading.Event()
    allow_acquire = threading.Event()
    released = threading.Event()

    class ControlledLock:
        def __init__(self) -> None:
            self.release_count = 0

        def acquire(self, *, timeout: float) -> bool:
            started.set()
            return allow_acquire.wait(timeout)

        def release(self) -> None:
            self.release_count += 1
            released.set()

    lock = ControlledLock()
    monkeypatch.setattr(writer, "_writer_lock", lock)
    monkeypatch.setattr(
        kg_runtime,
        "open_board_connection",
        lambda _board_id: pytest.fail("cancelled begin must not open the graph"),
    )

    begin = asyncio.create_task(
        CommunityKuzuGraphTransaction(writer_lock_timeout_s=1).begin("cancelled-board")
    )
    assert await asyncio.to_thread(started.wait, 1)
    begin.cancel()
    allow_acquire.set()
    with pytest.raises(asyncio.CancelledError):
        await begin
    assert await asyncio.to_thread(released.wait, 1)
    assert lock.release_count == 1


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_eventual_acquire_cleanup(
    monkeypatch,
) -> None:
    from okto_pulse.community.adapters import ladybug_writer as writer

    started = threading.Event()
    allow_acquire = threading.Event()
    released = threading.Event()

    class ControlledLock:
        def __init__(self) -> None:
            self.release_count = 0

        def acquire(self, *, timeout: float) -> bool:
            started.set()
            return allow_acquire.wait(timeout)

        def release(self) -> None:
            self.release_count += 1
            released.set()

    lock = ControlledLock()
    monkeypatch.setattr(writer, "_writer_lock", lock)

    acquire = asyncio.create_task(
        writer.acquire_ladybug_writer_async(
            scope="repeated-cancel",
            phase="waiting-for-native-lease",
            timeout_s=1,
        )
    )
    assert await asyncio.to_thread(started.wait, 1)

    assert acquire.cancel() is True
    # Let the first cancellation enter the cleanup path.  The former
    # implementation was now suspended in ``await acquire_task``, where the
    # second cancellation could cancel the only future carrying the result.
    await asyncio.sleep(0)
    acquire.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquire

    # The caller is already cancelled, but the native acquisition remains
    # owned by its cleanup callback until it can return and release exactly once.
    assert lock.release_count == 0
    allow_acquire.set()
    assert await asyncio.to_thread(released.wait, 1)
    assert lock.release_count == 1


@pytest.mark.asyncio
async def test_sync_writer_contention_fails_fast_on_event_loop_with_owner(
) -> None:
    from okto_pulse.community.adapters.ladybug_writer import ladybug_writer_scope

    entered = threading.Event()
    release = threading.Event()

    def hold_writer() -> None:
        with ladybug_writer_scope(scope="holder", phase="long_graph_write"):
            entered.set()
            assert release.wait(timeout=5)

    holder = asyncio.create_task(asyncio.to_thread(hold_writer))
    assert await asyncio.to_thread(entered.wait, 1)
    started = time.monotonic()
    try:
        with pytest.raises(GraphLockContention) as exc_info:
            with ladybug_writer_scope(
                scope="event-loop",
                phase="contending_sync_adapter",
                timeout_s=1,
            ):
                pytest.fail("the contending event-loop call must not enter")
        assert time.monotonic() - started < 0.1
        assert exc_info.value.details["timeout_ms"] == 0
        assert exc_info.value.details["owner_scope"] == "holder"
        assert exc_info.value.details["owner_phase"] == "long_graph_write"
        assert exc_info.value.details["held_ms"] >= 0
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
async def test_cancelled_tracked_writer_stays_joinable_without_loop_stall() -> None:
    from okto_pulse.community.adapters.ladybug_writer import ladybug_writer_scope
    from okto_pulse.community.adapters.worker_runners import TrackedBlockingExecution

    entered = threading.Event()
    release = threading.Event()
    executor = TrackedBlockingExecution()

    def native_commit() -> None:
        with ladybug_writer_scope(scope="tracked", phase="native_commit"):
            entered.set()
            assert release.wait(timeout=5)

    parent = asyncio.create_task(executor.run(native_commit))
    assert await asyncio.to_thread(entered.wait, 1)
    parent.cancel()
    await asyncio.sleep(0)
    assert not parent.done()
    assert await executor.join(0.01) == 1

    heartbeat = asyncio.create_task(asyncio.sleep(0))
    with pytest.raises(GraphLockContention):
        with ladybug_writer_scope(
            scope="event-loop",
            phase="while_native_commit",
            timeout_s=1,
        ):
            pytest.fail("event-loop contender must fail fast")
    await asyncio.wait_for(heartbeat, timeout=0.1)

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await parent
    assert await executor.join(1) == 0


@pytest.mark.asyncio
async def test_vector_extension_hot_open_shares_process_writer_gate(
    monkeypatch,
) -> None:
    opened: list[_FakeBoardConnection] = []

    def fake_open(_board_id: str) -> _FakeBoardConnection:
        connection = _FakeBoardConnection()
        opened.append(connection)
        return connection

    class Result:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class ExtensionConnection:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.results: list[Result] = []

        def execute(self, statement: str) -> Result:
            self.statements.append(statement)
            result = Result()
            self.results.append(result)
            return result

    monkeypatch.setattr(kg_runtime, "open_board_connection", fake_open)
    scope = await CommunityKuzuGraphTransaction().begin(f"extension-gate-{uuid4().hex}")
    extension = ExtensionConnection()
    try:
        with pytest.raises(GraphLockContention):
            await asyncio.to_thread(
                kg_runtime.load_vector_extension,
                extension,
                install=False,
                writer_timeout_s=0.02,
            )
        assert extension.statements == []
    finally:
        await scope.commit()

    kg_runtime.load_vector_extension(
        extension,
        install=False,
        writer_timeout_s=0.1,
    )
    assert extension.statements == ["LOAD VECTOR"]
    assert extension.results[0].closed is True


@pytest.mark.asyncio
async def test_failure_log_exposes_kind_but_not_statement_or_params(
    monkeypatch,
    caplog,
) -> None:
    secret = "never-log-this-value"
    native = RuntimeError("Only one write transaction at a time is allowed; " + secret)
    fake = _FakeBoardConnection(error=native)
    monkeypatch.setattr(
        kg_runtime,
        "open_board_connection",
        lambda _board_id: fake,
    )
    board_id = f"redacted-{uuid4().hex}"
    scope = await CommunityKuzuGraphTransaction().begin(board_id)

    with caplog.at_level(
        logging.WARNING,
        logger="okto_pulse.community.adapters.kuzu_graph_transaction",
    ):
        with pytest.raises(GraphLockContention):
            scope.execute(
                "MATCH (n) WHERE n.private = $private SET n.token = $token",
                {"private": secret, "token": secret},
            )
    await scope.rollback()

    log_text = caplog.text
    assert "phase=execute" in log_text
    assert "statement_kind=MATCH_SET" in log_text
    assert "error_code=graph_lock_contention" in log_text
    assert secret not in log_text
    assert "n.private" not in log_text
