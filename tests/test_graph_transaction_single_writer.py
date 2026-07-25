"""Community Ladybug single-writer transaction regression coverage."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
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


@pytest.mark.asyncio
async def test_source_deleted_replacement_removes_indexed_semantic_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"source-delete-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        with kg_runtime.open_board_connection(board_id) as (_db, conn):
            created = conn.execute(
                "CREATE (:Requirement {"
                "id: $id, title: $title, content: $content, "
                "context: $context, justification: $justification, "
                "source_artifact_ref: $source_ref, "
                "source_session_id: $session_id, "
                "graph_layer: $graph_layer, maturity_status: $maturity, "
                "created_by_agent: $agent, relevance_score: $relevance, "
                "source_span_quote: $quote, embedding: $embedding, "
                "source_content_hash: $source_hash})",
                {
                    "id": "requirement-to-erase",
                    "title": "Readable private title",
                    "content": "Readable private body",
                    "context": "Readable private context",
                    "justification": "Readable private justification",
                    "source_ref": "spec:deleted-spec:fr:fr_private",
                    "session_id": "source-session",
                    "graph_layer": "working",
                    "maturity": "working_immature",
                    "agent": "system:deterministic",
                    "relevance": 0.8,
                    "quote": "Readable source quote",
                    "embedding": [0.01] * 384,
                    "source_hash": "legacy-private-content-hash",
                },
            )
            created.close()

        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        assert scope.replace_with_source_deleted_tombstone(
            "Requirement",
            "requirement-to-erase",
            graph_layer="working",
            maturity_status="working_stale",
            revocation_reason="source_deleted",
            relevance_score=0.0,
        )
        await scope.commit()

        with kg_runtime.open_board_connection(board_id) as (_db, conn):
            result = conn.execute(
                "MATCH (n:Requirement {id: $id}) "
                "RETURN n.title, n.content, n.context, n.justification, "
                "n.source_span_quote, n.embedding IS NULL, "
                "n.source_content_hash IS NULL, "
                "n.source_artifact_ref, n.graph_layer, n.maturity_status, "
                "n.revocation_reason, n.relevance_score",
                {"id": "requirement-to-erase"},
            )
            assert result.get_next() == [
                "",
                "",
                "",
                "",
                "",
                True,
                True,
                "spec:deleted-spec:fr:fr_private",
                "working",
                "working_stale",
                "source_deleted",
                0.0,
            ]
            result.close()

        retry_scope = await CommunityKuzuGraphTransaction().begin(board_id)
        assert retry_scope.replace_with_source_deleted_tombstone(
            "Requirement",
            "requirement-to-erase",
            graph_layer="working",
            maturity_status="working_stale",
            revocation_reason="source_deleted",
            relevance_score=0.0,
        )
        await retry_scope.commit()
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_payload_empty_node_is_not_converged_until_state_and_edges_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"source-delete-false-convergence-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        async with await CommunityKuzuGraphTransaction().begin(board_id) as scope:
            scope.create_node(
                "Requirement",
                "empty-but-canonical",
                {
                    "title": "",
                    "content": "",
                    "context": "",
                    "justification": "",
                    "source_span_quote": "",
                    "source_content_hash": "",
                    "source_artifact_ref": "spec:deleted:fr:empty",
                    "graph_layer": "canonical",
                    "maturity_status": "canonical_mature",
                    "revocation_reason": "",
                    "relevance_score": 0.8,
                },
                source_session_id="source-session",
            )
            scope.create_node(
                "Entity",
                "former-parent",
                {"title": "Former parent"},
                source_session_id="parent-session",
            )
            assert scope.create_edge(
                "belongs_to",
                "Requirement",
                "Entity",
                "empty-but-canonical",
                "former-parent",
                {
                    "confidence": 1.0,
                    "created_by_session_id": "edge-session",
                    "layer": "deterministic",
                    "rule_id": "test/source-deleted",
                    "created_by": "system:test",
                    "fallback_reason": "",
                },
            )

            assert scope.replace_with_source_deleted_tombstone(
                "Requirement",
                "empty-but-canonical",
                graph_layer="working",
                maturity_status="working_stale",
                revocation_reason="source_deleted",
                relevance_score=0.0,
            )
            after = scope._snapshot_node_before_image(
                "Requirement",
                "empty-but-canonical",
                include_incident_edges=True,
            )
            assert after is not None
            assert after.attrs["graph_layer"] == "working"
            assert after.attrs["maturity_status"] == "working_stale"
            assert after.attrs["revocation_reason"] == "source_deleted"
            assert after.attrs["relevance_score"] == 0.0
            assert after.incident_edges == ()
    finally:
        kg_runtime.close_all_connections(board_id)


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
async def test_open_failure_releases_but_transaction_close_failure_poison_holds_writer(
    monkeypatch,
) -> None:
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

    try:
        assert close_fails._finished is True
        assert close_fails._writer_lease._released is False
        with pytest.raises(RuntimeError, match="scope_close_failed"):
            await close_fails.commit()
        with pytest.raises(GraphLockContention):
            blocked = await tx.begin(board_id)
            await blocked.commit()
    finally:
        # A poisoned transactional close intentionally requires explicit
        # operator/process cleanup. Do not strand the process-global test gate.
        close_fails._writer_lease.release()

    final = await tx.begin(board_id)
    await final.commit()
    assert attempts == 3


def test_strict_close_surfaces_native_failure_without_reader_exit() -> None:
    class _NativeConnection:
        close_count = 0

        def close(self) -> None:
            self.close_count += 1
            raise RuntimeError("native close unconfirmed")

    class _CloseGuard:
        exit_count = 0

        def reader_exit(self) -> None:
            self.exit_count += 1

    connection = object.__new__(kg_runtime.BoardConnection)
    connection._board_id = "strict-close-board"
    connection._closed = False
    connection.conn = _NativeConnection()
    connection._close_guard = _CloseGuard()

    with pytest.raises(RuntimeError, match="native close unconfirmed"):
        connection.close_strict()

    assert connection._closed is False
    assert connection.conn.close_count == 1
    assert connection._close_guard.exit_count == 0

    # Ordinary context-manager cleanup remains best-effort and releases its
    # reader registration even when the same native close keeps failing.
    connection.close()
    assert connection._closed is True
    assert connection._close_guard.exit_count == 1


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
