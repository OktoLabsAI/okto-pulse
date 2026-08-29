"""Focused contracts for immutable Board graph transaction routing."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest
from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption, GraphError
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult

from okto_pulse.community.adapters import routed_graph_transaction as routed
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteSnapshot,
)

BOARD_ID = "board-routed-transaction"
PAGE_SIZE = 8192


def _snapshot(
    tmp_path: Path,
    *,
    backend: str,
    scope: str = "board",
    scope_id: str = BOARD_ID,
    page_size: int | None = None,
) -> CommunityGraphRouteSnapshot:
    anchor = tmp_path / "boards" / BOARD_ID
    active = anchor / "database" if backend == "grafx" else anchor / "graph.lbug"
    return CommunityGraphRouteSnapshot(
        scope=scope,  # type: ignore[arg-type]
        scope_id=scope_id,
        backend=backend,  # type: ignore[arg-type]
        generation="generation-7",
        binding_path=anchor / "backend-binding.json",
        anchor_path=anchor,
        active_path=active,
        page_size=page_size,
        binding_sha256="b" * 64,
        route_sha256="r" * 64,
    )


class _WindowProbe:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = False
        self.exits = 0

    def __call__(self, board_id: str) -> _WindowProbe:
        assert board_id == BOARD_ID
        return self

    def __enter__(self) -> None:
        assert not self.active
        self.active = True
        self.events.append("window_enter")

    def __exit__(self, *_exc: object) -> None:
        assert self.active
        self.active = False
        self.exits += 1
        self.events.append("window_exit")


class _ResolverProbe:
    def __init__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        events: list[str],
    ) -> None:
        self.snapshot = snapshot
        self.events = events
        self.acquire_calls = 0
        self.revalidate_calls = 0
        self.revalidate_require_physical: list[bool] = []
        self.admission_error: BaseException | None = None
        self.revalidate_error_at: int | None = None

    def acquire_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        assert board_id == BOARD_ID
        self.acquire_calls += 1
        self.events.append("route_acquire")
        return self.snapshot

    def admit_grafx_route(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        database: object,
        *,
        operation: str,
    ) -> object:
        assert snapshot is self.snapshot
        assert database is not None
        assert operation == "begin_routed_graph_transaction"
        self.events.append("route_admit")
        if self.admission_error is not None:
            raise self.admission_error
        return object()

    def revalidate_snapshot(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        require_physical: bool = False,
    ) -> CommunityGraphRouteSnapshot:
        assert snapshot is self.snapshot
        self.revalidate_calls += 1
        self.revalidate_require_physical.append(require_physical)
        self.events.append(f"route_revalidate:{self.revalidate_calls}")
        if self.revalidate_calls == self.revalidate_error_at:
            raise GraphCorruption(
                "injected route cutover",
                details={"reason": "graph_route_snapshot_mismatch"},
            )
        return snapshot


class _LadybugScope:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = True
        self.rollback_failure: BaseException | None = None

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        assert self.active
        self.events.append(f"ladybug_execute:{statement}")
        return GraphStatementResult.from_rows((((params or {}).get("value", 1),),))

    async def commit(self) -> None:
        self.events.append("ladybug_commit")
        self.active = False

    async def rollback(self) -> None:
        self.events.append("ladybug_rollback")
        if self.rollback_failure is not None:
            raise self.rollback_failure
        self.active = False

    async def __aenter__(self) -> Self:
        self.events.append("ladybug_aenter")
        return self

    async def __aexit__(self, *exc: object) -> None:
        if exc and exc[0] is not None:
            await self.rollback()
        else:
            await self.commit()


class _LadybugProvider:
    def __init__(self, events: list[str], scope: _LadybugScope) -> None:
        self.events = events
        self.scope = scope
        self.begin_failure: BaseException | None = None
        self.begin_calls = 0

    async def begin(self, board_id: str) -> _LadybugScope:
        assert board_id == BOARD_ID
        self.begin_calls += 1
        self.events.append("ladybug_begin")
        if self.begin_failure is not None:
            raise self.begin_failure
        return self.scope


class _EngineTransaction:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = True
        self.report: object | None = None
        self.rollback_failure: BaseException | None = None
        self.rollback_failure_leaves_active = True

    def commit(self) -> None:
        self.events.append("engine_commit")
        self.active = False
        self.report = SimpleNamespace(durable=True, wrote=True, csn=7)

    def execute(self, statement: str, params: dict[str, Any]) -> object:
        assert self.active
        assert statement.startswith("CREATE")
        assert params == {"id": "node-1"}
        self.events.append("engine_execute")
        return SimpleNamespace(columns=(), rows=())

    def rollback(self) -> None:
        self.events.append("engine_rollback")
        if self.rollback_failure is not None:
            self.active = self.rollback_failure_leaves_active
            raise self.rollback_failure
        self.active = False


class _DatabaseProbe:
    def __init__(self, events: list[str], transaction: _EngineTransaction) -> None:
        self.events = events
        self.transaction = transaction
        self.begin_failure: BaseException | None = None

    def begin(self, mode: str) -> _EngineTransaction:
        assert mode == "write"
        self.events.append("engine_begin")
        if self.begin_failure is not None:
            raise self.begin_failure
        return self.transaction


class _LeaseProbe:
    def __init__(
        self,
        events: list[str],
        database: _DatabaseProbe,
    ) -> None:
        self.events = events
        self.database = database
        self.released = False
        self.release_calls = 0

    def release(self) -> bool:
        self.release_calls += 1
        if self.released:
            return False
        assert not self.database.transaction.active
        self.released = True
        self.events.append("pool_release")
        return True


class _PoolProbe:
    def __init__(self, events: list[str], lease: _LeaseProbe) -> None:
        self.events = events
        self.lease = lease
        self.acquire_calls = 0

    def acquire(self, path: Path, *, page_size: int) -> _LeaseProbe:
        self.acquire_calls += 1
        assert path.name in {"database", "graph.lbug"}
        assert page_size == PAGE_SIZE
        self.events.append("pool_acquire")
        return self.lease


class _MutationRecorderProbe:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.prepared: list[dict[str, object]] = []

    def prepare_mutation(self, **record: object) -> object:
        token = f"mutation-{len(self.prepared) + 1}"
        self.prepared.append(dict(record))
        self.events.append(f"capture_prepare:{record['backend']}")
        return token

    def mark_source_committed(self, token: object) -> None:
        self.events.append(f"capture_committed:{token}")

    def mark_source_abandoned(self, token: object) -> None:
        self.events.append(f"capture_abandoned:{token}")

    def mark_source_ambiguous(self, token: object, *, error_type: str) -> None:
        self.events.append(f"capture_ambiguous:{token}:{error_type}")


def _assembly(
    tmp_path: Path,
    *,
    backend: str,
    mutation_recorder: _MutationRecorderProbe | None = None,
) -> tuple[
    routed.CommunityRoutedGraphTransaction,
    list[str],
    _WindowProbe,
    _ResolverProbe,
    _LadybugProvider,
    _EngineTransaction,
    _DatabaseProbe,
    _LeaseProbe,
    _PoolProbe,
]:
    events: list[str] = []
    window = _WindowProbe(events)
    resolver = _ResolverProbe(
        _snapshot(
            tmp_path,
            backend=backend,
            page_size=PAGE_SIZE if backend == "grafx" else None,
        ),
        events,
    )
    ladybug_scope = _LadybugScope(events)
    ladybug = _LadybugProvider(events, ladybug_scope)
    transaction = _EngineTransaction(events)
    database = _DatabaseProbe(events, transaction)
    lease = _LeaseProbe(events, database)
    pool = _PoolProbe(events, lease)
    facade = routed.CommunityRoutedGraphTransaction(
        resolver,  # type: ignore[arg-type]
        ladybug=ladybug,  # type: ignore[arg-type]
        grafx_pool=pool,  # type: ignore[arg-type]
        operation_window=window,
        mutation_recorder=mutation_recorder,
    )
    return (
        facade,
        events,
        window,
        resolver,
        ladybug,
        transaction,
        database,
        lease,
        pool,
    )


@pytest.mark.asyncio
async def test_ladybug_capture_prepares_before_auto_commit_write(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    recorder = _MutationRecorderProbe(events)
    facade, routed_events, window, resolver, *_rest = _assembly(
        tmp_path,
        backend="ladybug",
        mutation_recorder=recorder,
    )
    recorder.events = routed_events

    scope = await facade.begin(BOARD_ID)
    result = scope.execute("CREATE (n {id: $value})", {"value": "node-1"})

    assert result.rows == (("node-1",),)
    assert routed_events[-3:] == [
        "capture_prepare:ladybug",
        "ladybug_execute:CREATE (n {id: $value})",
        "capture_committed:mutation-1",
    ]
    assert recorder.prepared == [
        {
            "board_id": BOARD_ID,
            "binding_sha256": resolver.snapshot.binding_sha256,
            "backend": "ladybug",
            "transaction_id": recorder.prepared[0]["transaction_id"],
            "family": "execute",
            "payload": recorder.prepared[0]["payload"],
        }
    ]
    payload = recorder.prepared[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["parameter_names"] == ["value"]
    assert "CREATE" not in repr(payload)
    assert window.active
    await scope.commit()


@pytest.mark.asyncio
async def test_capture_ignores_read_only_statement(tmp_path: Path) -> None:
    events: list[str] = []
    recorder = _MutationRecorderProbe(events)
    facade, routed_events, *_rest = _assembly(
        tmp_path,
        backend="ladybug",
        mutation_recorder=recorder,
    )
    recorder.events = routed_events

    scope = await facade.begin(BOARD_ID)
    result = scope.execute("RETURN $value", {"value": 17})
    await scope.commit()

    assert result.rows == ((17,),)
    assert recorder.prepared == []
    assert all(not event.startswith("capture_") for event in routed_events)


@pytest.mark.asyncio
async def test_grafx_capture_confirms_only_after_durable_engine_commit(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    recorder = _MutationRecorderProbe(events)
    (
        facade,
        routed_events,
        _window,
        resolver,
        _ladybug,
        transaction,
        _database,
        _lease,
        _pool,
    ) = _assembly(
        tmp_path,
        backend="grafx",
        mutation_recorder=recorder,
    )
    recorder.events = routed_events

    scope = await facade.begin(BOARD_ID)
    scope.execute("CREATE (n {id: $id})", {"id": "node-1"})

    assert routed_events[-3:] == [
        "capture_prepare:grafx",
        "route_revalidate:2",
        "engine_execute",
    ]
    assert recorder.prepared[0]["binding_sha256"] == resolver.snapshot.binding_sha256
    assert recorder.prepared[0]["backend"] == "grafx"

    await scope.commit()

    assert transaction.report is not None
    assert routed_events[-5:] == [
        "route_revalidate:3",
        "engine_commit",
        "pool_release",
        "window_exit",
        "capture_committed:mutation-1",
    ]


@pytest.mark.asyncio
async def test_ladybug_route_holds_one_window_until_commit_and_forwards_scope(
    tmp_path: Path,
) -> None:
    facade, events, window, resolver, ladybug, *_rest = _assembly(
        tmp_path,
        backend="ladybug",
    )

    scope = await facade.begin(BOARD_ID)
    result = scope.execute("RETURN $value", {"value": 11})

    assert result.rows == ((11,),)
    assert window.active
    assert resolver.acquire_calls == 1
    assert ladybug.begin_calls == 1
    assert events == [
        "window_enter",
        "route_acquire",
        "ladybug_begin",
        "ladybug_execute:RETURN $value",
    ]

    await scope.commit()
    await scope.commit()
    await scope.rollback()

    assert events[-2:] == ["ladybug_commit", "window_exit"]
    assert window.exits == 1


@pytest.mark.asyncio
async def test_ladybug_context_rollback_releases_after_delegate_terminal(
    tmp_path: Path,
) -> None:
    facade, events, window, *_rest = _assembly(tmp_path, backend="ladybug")

    with pytest.raises(RuntimeError, match="body failure"):
        async with await facade.begin(BOARD_ID):
            assert window.active
            raise RuntimeError("body failure")

    assert events[-3:] == ["ladybug_aenter", "ladybug_rollback", "window_exit"]


@pytest.mark.asyncio
async def test_ladybug_failed_terminal_retains_window_until_retry_succeeds(
    tmp_path: Path,
) -> None:
    facade, events, window, _resolver, ladybug, *_rest = _assembly(
        tmp_path,
        backend="ladybug",
    )
    failure = OSError("connection is still open")
    ladybug.scope.rollback_failure = failure
    scope = await facade.begin(BOARD_ID)

    with pytest.raises(OSError) as raised:
        await scope.rollback()

    assert raised.value is failure
    assert window.active
    assert "window_exit" not in events

    ladybug.scope.rollback_failure = None
    await scope.rollback()
    assert events[-2:] == ["ladybug_rollback", "window_exit"]


@pytest.mark.asyncio
async def test_begin_cancellation_is_preserved_and_releases_unowned_window(
    tmp_path: Path,
) -> None:
    facade, events, window, _resolver, ladybug, *_rest = _assembly(
        tmp_path,
        backend="ladybug",
    )
    cancellation = asyncio.CancelledError("cancel begin")
    ladybug.begin_failure = cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        await facade.begin(BOARD_ID)

    assert raised.value is cancellation
    assert events[-2:] == ["ladybug_begin", "window_exit"]
    assert not window.active


@pytest.mark.asyncio
async def test_invalid_board_snapshot_fails_closed_without_provider_fallback(
    tmp_path: Path,
) -> None:
    facade, events, window, resolver, ladybug, *_rest = _assembly(
        tmp_path,
        backend="ladybug",
    )
    resolver.snapshot = _snapshot(
        tmp_path,
        backend="ladybug",
        scope_id="a-different-board",
    )

    with pytest.raises(GraphCorruption) as raised:
        await facade.begin(BOARD_ID)

    assert raised.value.details["reason"] == "graph_route_snapshot_scope_invalid"
    assert ladybug.begin_calls == 0
    assert events == ["window_enter", "route_acquire", "window_exit"]
    assert not window.active


@pytest.mark.asyncio
async def test_grafx_order_is_window_route_pin_admit_begin_engine_pin_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade, events, window, resolver, ladybug, transaction, _database, lease, pool = (
        _assembly(tmp_path, backend="grafx")
    )

    def writer_fence(board_id: str, *, failure_phase: str) -> None:
        assert board_id == BOARD_ID
        events.append(f"writer_fence:{failure_phase}")

    monkeypatch.setattr(routed, "revalidate_board_graph_write_lease", writer_fence)

    scope = await facade.begin(BOARD_ID)

    assert transaction.active
    assert window.active
    assert not lease.released
    assert resolver.acquire_calls == 1
    assert resolver.revalidate_calls == 1
    assert resolver.revalidate_require_physical == [True]
    assert pool.acquire_calls == 1
    assert ladybug.begin_calls == 0
    assert events == [
        "window_enter",
        "route_acquire",
        "pool_acquire",
        "route_admit",
        "writer_fence:begin",
        "route_revalidate:1",
        "engine_begin",
    ]

    scope.execute("CREATE (n:Entity {id: $id})", {"id": "node-1"})
    assert resolver.revalidate_require_physical == [True, True]
    assert events[-3:] == [
        "writer_fence:graph_statement_precommit",
        "route_revalidate:2",
        "engine_execute",
    ]

    await scope.commit()

    assert resolver.revalidate_require_physical == [True, True, True]
    assert events[-5:] == [
        "writer_fence:commit",
        "route_revalidate:3",
        "engine_commit",
        "pool_release",
        "window_exit",
    ]
    assert lease.release_calls == 1
    assert window.exits == 1

    await scope.commit()
    await scope.rollback()
    assert lease.release_calls == 1
    assert window.exits == 1


@pytest.mark.asyncio
async def test_grafx_admission_failure_releases_pin_then_window_without_begin(
    tmp_path: Path,
) -> None:
    facade, events, window, resolver, _ladybug, transaction, _database, lease, _pool = (
        _assembly(tmp_path, backend="grafx")
    )
    failure = GraphCorruption("admission refused", details={"reason": "mismatch"})
    resolver.admission_error = failure
    # No engine scope exists on this path, so the focused lease probe may be
    # released even though its reusable transaction stub starts marked active.
    transaction.active = False

    with pytest.raises(GraphCorruption) as raised:
        await facade.begin(BOARD_ID)

    assert raised.value is failure
    assert events == [
        "window_enter",
        "route_acquire",
        "pool_acquire",
        "route_admit",
        "pool_release",
        "window_exit",
    ]
    assert lease.release_calls == 1
    assert not window.active


@pytest.mark.asyncio
async def test_grafx_begin_failure_releases_transferred_resources_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade, events, window, _resolver, _ladybug, transaction, database, lease, _pool = (
        _assembly(tmp_path, backend="grafx")
    )
    database.begin_failure = OSError("begin refused")
    transaction.active = False
    monkeypatch.setattr(
        routed,
        "revalidate_board_graph_write_lease",
        lambda _board, *, failure_phase: events.append(f"writer_fence:{failure_phase}"),
    )

    with pytest.raises(GraphError):
        await facade.begin(BOARD_ID)

    assert events[-3:] == ["engine_begin", "pool_release", "window_exit"]
    assert lease.release_calls == 1
    assert not window.active


@pytest.mark.asyncio
async def test_route_cutover_at_commit_retains_resources_until_engine_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facade, events, window, resolver, _ladybug, transaction, _database, lease, _pool = (
        _assembly(tmp_path, backend="grafx")
    )
    monkeypatch.setattr(
        routed,
        "revalidate_board_graph_write_lease",
        lambda _board, *, failure_phase: events.append(f"writer_fence:{failure_phase}"),
    )
    scope = await facade.begin(BOARD_ID)
    resolver.revalidate_error_at = 2

    with pytest.raises(GraphCorruption, match="injected route cutover"):
        await scope.commit()

    assert transaction.active
    assert not lease.released
    assert window.active
    assert "engine_commit" not in events

    resolver.revalidate_error_at = None
    await scope.rollback()
    assert events[-3:] == ["engine_rollback", "pool_release", "window_exit"]


@pytest.mark.asyncio
async def test_active_grafx_rollback_failure_retains_pin_and_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        facade,
        events,
        window,
        _resolver,
        _ladybug,
        transaction,
        _database,
        lease,
        _pool,
    ) = _assembly(tmp_path, backend="grafx")
    monkeypatch.setattr(
        routed,
        "revalidate_board_graph_write_lease",
        lambda _board, *, failure_phase: events.append(f"writer_fence:{failure_phase}"),
    )
    scope = await facade.begin(BOARD_ID)
    transaction.rollback_failure = OSError("rollback blocked")
    transaction.rollback_failure_leaves_active = True

    with pytest.raises(GraphError):
        await scope.rollback()

    assert transaction.active
    assert not lease.released
    assert window.active
    assert events[-1] == "engine_rollback"

    transaction.rollback_failure = None
    await scope.rollback()
    assert events[-3:] == ["engine_rollback", "pool_release", "window_exit"]


@pytest.mark.asyncio
async def test_inactive_grafx_rollback_failure_still_releases_terminal_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        facade,
        events,
        window,
        _resolver,
        _ladybug,
        transaction,
        _database,
        lease,
        _pool,
    ) = _assembly(tmp_path, backend="grafx")
    monkeypatch.setattr(
        routed,
        "revalidate_board_graph_write_lease",
        lambda _board, *, failure_phase: events.append(f"writer_fence:{failure_phase}"),
    )
    scope = await facade.begin(BOARD_ID)
    transaction.rollback_failure = OSError("rollback reported after close")
    transaction.rollback_failure_leaves_active = False

    with pytest.raises(GraphError):
        await scope.rollback()

    assert not transaction.active
    assert lease.released
    assert not window.active
    assert events[-3:] == ["engine_rollback", "pool_release", "window_exit"]


@pytest.mark.asyncio
async def test_invalid_board_id_is_refused_before_opening_window(
    tmp_path: Path,
) -> None:
    facade, events, *_rest = _assembly(tmp_path, backend="ladybug")

    for invalid in ("", None, 7):
        with pytest.raises(ValueError, match="board_id"):
            await facade.begin(invalid)  # type: ignore[arg-type]

    assert events == []


def test_source_has_no_settings_or_initialization_fallback() -> None:
    source_path = Path(routed.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    called_attributes = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    assert not any(name.endswith(".config") for name in imports)
    assert "initialize_board_route" not in called_attributes
    assert called_attributes.count("acquire_board_route") == 1
