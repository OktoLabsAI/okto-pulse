"""Composition contracts for the routed Community Board provider bundle."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from okto_grafx.errors import GrafxSchemaVersionMismatch
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.interfaces.graph_recovery import WalRecoveryReport

import okto_pulse.community.adapters.graph_connection_pool as connection_pool
import okto_pulse.community.adapters.kg_wal_recovery as wal_recovery
import okto_pulse.community.adapters.routed_board_graph_composition as composition
import okto_pulse.community.adapters.routed_graph_transaction as routed_transaction
from okto_pulse.community.adapters import kg_runtime, ladybug_writer
from okto_pulse.community.adapters.grafx_database_pool import (
    CommunityGrafxDatabasePool,
    GrafxDatabasePoolError,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)

PAGE_SIZE = 8192


class _FakeGrafxTransaction:
    def __init__(self) -> None:
        self.active = True
        self.report = None

    def commit(self) -> None:
        self.active = False

    def rollback(self) -> None:
        self.active = False


class _FakeGrafxDatabase:
    def __init__(self, path: Path, page_size: int) -> None:
        self.path = str(path)
        self.identity = SimpleNamespace(page_size=page_size)
        self.closed = False
        self.close_calls = 0
        self.begin_modes: list[str] = []

    def begin(self, mode: str) -> _FakeGrafxTransaction:
        self.begin_modes.append(mode)
        return _FakeGrafxTransaction()

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _GrafxConnector:
    def __init__(self, *, persisted_page_size: int | None = None) -> None:
        self.persisted_page_size = persisted_page_size
        self.calls: list[tuple[Path, int]] = []
        self.databases: list[_FakeGrafxDatabase] = []

    def __call__(self, path: Path, *, page_size: int) -> _FakeGrafxDatabase:
        path = Path(path)
        self.calls.append((path, page_size))
        if (
            self.persisted_page_size is not None
            and path.exists()
            and page_size != self.persisted_page_size
        ):
            raise GrafxSchemaVersionMismatch(
                "The requested page geometry differs from persisted storage.",
                field="page_size",
                stored=self.persisted_page_size,
            )
        observed = self.persisted_page_size or page_size
        path.mkdir(parents=True, exist_ok=True)
        (path / "grafx.meta").write_bytes(b"grafx")
        database = _FakeGrafxDatabase(path, observed)
        self.databases.append(database)
        return database


def _settings(
    root: Path,
    *,
    board_backend: str = "grafx",
    global_backend: str = "grafx",
    page_size: int = PAGE_SIZE,
) -> SimpleNamespace:
    return SimpleNamespace(
        kg_base_dir=str(root),
        kg_graph_backend=board_backend,
        kg_global_graph_backend=global_backend,
        kg_grafx_page_size=page_size,
        kg_ladybug_max_db_size_gb=2,
    )


def _build(
    root: Path,
    connector: _GrafxConnector,
    *,
    board_backend: str = "grafx",
    page_size: int = PAGE_SIZE,
) -> composition.CommunityRoutedBoardGraphComposition:
    return composition.build_community_routed_board_graph_composition(
        settings=_settings(
            root,
            board_backend=board_backend,
            page_size=page_size,
        ),
        grafx_connect=connector,
    )


def _publish_ladybug_binding(
    bundle: composition.CommunityRoutedBoardGraphComposition,
    board_id: str,
) -> Path:
    path = bundle.binding_store.board_ladybug_path(board_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ladybug")
    bundle.binding_store.initialize_board_binding(
        board_id=board_id,
        backend="ladybug",
        generation="ladybug-1",
        physical_path=path,
    )
    return path


def test_build_is_read_only_and_every_board_port_shares_one_route_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "kg"
    connector = _GrafxConnector()

    bundle = _build(root, connector)

    assert not root.exists()
    assert connector.calls == []
    assert bundle.grafx_pool._max_entries is None
    assert bundle.graph_transaction._grafx_pool is bundle.grafx_pool
    for port in (
        bundle.graph_store,
        bundle.cypher_executor,
        bundle.graph_transaction,
        bundle.graph_schema_manager,
        bundle.graph_lifecycle,
        bundle.graph_runtime_store,
        bundle.graph_recovery,
    ):
        assert port._resolver is bundle.resolver

    assert bundle.graph_runtime_store.exists("missing") is False
    with pytest.raises(GraphCapabilityUnavailable) as missing:
        bundle.graph_store.get_schema_version("missing")
    assert missing.value.details["reason"] == "binding_missing"
    assert connector.calls == []
    assert not root.exists()


def test_builder_accepts_and_validates_exact_prebuilt_shared_components(
    tmp_path: Path,
) -> None:
    root = tmp_path / "kg"
    connector = _GrafxConnector()
    first = _build(root, connector)

    second = composition.build_community_routed_board_graph_composition(
        settings=_settings(root),
        binding_store=first.binding_store,
        resolver=first.resolver,
        grafx_pool=first.grafx_pool,
    )

    assert second.binding_store is first.binding_store
    assert second.resolver is first.resolver
    assert second.grafx_pool is first.grafx_pool
    assert connector.calls == []

    bounded = CommunityGrafxDatabasePool(root, connect=connector, max_entries=1)
    with pytest.raises(ValueError, match="must be unbounded"):
        composition.build_community_routed_board_graph_composition(
            settings=_settings(root),
            binding_store=first.binding_store,
            resolver=first.resolver,
            grafx_pool=bounded,
        )


def test_explicit_initialization_is_the_only_grafx_first_boot_door(
    tmp_path: Path,
) -> None:
    root = tmp_path / "kg"
    connector = _GrafxConnector()
    bundle = _build(root, connector)

    snapshot = bundle.initialize_board_route("board-a")
    repeated = bundle.initialize_board_route("board-a")

    assert repeated == snapshot
    assert snapshot.backend == "grafx"
    assert snapshot.page_size == PAGE_SIZE
    assert snapshot.active_path == bundle.binding_store.board_grafx_path(
        "board-a", "generation-1"
    )
    assert connector.calls == [(snapshot.active_path, PAGE_SIZE)]
    assert bundle.resolver.acquire_board_route("board-a") == snapshot

    with bundle.resolver.board_route_session("board-a"):
        first = bundle.resolver.inspect_board_route("board-a")
        second = bundle.resolver.acquire_board_route("board-a")
        assert second is first


def test_adoption_retries_only_a_typed_persisted_grafx_geometry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "kg"
    store = CommunityGraphBackendBindingStore(root)
    existing = store.board_grafx_path("board-adopt", "generation-crash")
    existing.mkdir(parents=True)
    (existing / "grafx.meta").write_bytes(b"grafx")
    connector = _GrafxConnector(persisted_page_size=4096)
    bundle = _build(root, connector, board_backend="ladybug", page_size=PAGE_SIZE)

    snapshot = bundle.initialize_board_route("board-adopt")

    assert snapshot.backend == "grafx"
    assert snapshot.active_path == existing
    assert snapshot.page_size == 4096
    assert connector.calls == [(existing, PAGE_SIZE), (existing, 4096)]
    assert not bundle.binding_store.board_ladybug_path("board-adopt").exists()


def test_ladybug_route_refuses_a_different_process_runtime_root_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build(
        tmp_path / "kg",
        _GrafxConnector(),
        board_backend="ladybug",
    )
    outside = tmp_path / "other-kg" / "boards" / "board-a" / "graph.lbug"
    bootstrap_calls: list[str] = []
    monkeypatch.setattr(kg_runtime, "board_kuzu_path", lambda _board_id: outside)
    monkeypatch.setattr(
        kg_runtime,
        "bootstrap_board_graph",
        lambda board_id: bootstrap_calls.append(board_id),
    )

    with pytest.raises(GraphCapabilityUnavailable) as mismatch:
        bundle.initialize_board_route("board-a")

    assert mismatch.value.details["reason"] == "ladybug_runtime_path_mismatch"
    assert bootstrap_calls == []
    assert not outside.exists()

    _publish_ladybug_binding(bundle, "board-bound")
    with pytest.raises(GraphCapabilityUnavailable) as routed:
        bundle.graph_runtime_store.exists("board-bound")
    assert routed.value.details["reason"] == "ladybug_runtime_path_mismatch"


@pytest.mark.asyncio
async def test_routed_grafx_open_validates_without_schema_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = _GrafxConnector()
    bundle = _build(tmp_path / "kg", connector)
    bundle.initialize_board_route("board-open")
    validations: list[str] = []

    async def creating_open_must_not_run(*_args: Any, **_kwargs: Any):
        raise AssertionError("schema-initializing Grafx lifecycle open")

    monkeypatch.setattr(
        composition.CommunityGrafxGraphLifecycle,
        "open",
        creating_open_must_not_run,
    )
    monkeypatch.setattr(
        composition,
        "validate_current_grafx_schema",
        lambda _database: validations.append("schema") or "fingerprint",
    )
    monkeypatch.setattr(
        composition,
        "read_current_grafx_schema_version",
        lambda _database: validations.append("board-meta") or "1",
    )

    handle = await bundle.graph_lifecycle.open("board-open")

    assert handle.opened is True
    assert validations == ["schema", "board-meta"]


@pytest.mark.asyncio
async def test_transaction_pins_shared_grafx_handle_until_terminal_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = _GrafxConnector()
    bundle = _build(tmp_path / "kg", connector)
    snapshot = bundle.initialize_board_route("board-pin")
    monkeypatch.setattr(
        routed_transaction,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )

    scope = await bundle.graph_transaction.begin("board-pin")

    assert bundle.grafx_pool.pin_count(snapshot.active_path) == 1
    with pytest.raises(GrafxDatabasePoolError) as pinned:
        bundle.grafx_pool.close(snapshot.active_path)
    assert pinned.value.reason == "pool_close_refused_pinned"

    await scope.rollback()
    assert bundle.grafx_pool.pin_count(snapshot.active_path) == 0
    assert bundle.grafx_pool.close(snapshot.active_path) is True
    assert connector.databases[0].closed is True


@pytest.mark.asyncio
async def test_close_all_closes_board_grafx_only_and_preserves_global(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "kg"
    connector = _GrafxConnector()
    bundle = _build(root, connector)
    board_path = root / "boards" / "board-a" / "grafx" / "generation-1"
    global_path = root / "global" / "grafx" / "generation-1"
    board_database = bundle.grafx_pool.get(board_path, page_size=PAGE_SIZE)
    global_database = bundle.grafx_pool.get(global_path, page_size=PAGE_SIZE)
    monkeypatch.setattr(connection_pool, "close_all_board_connections", lambda: None)
    monkeypatch.setattr(kg_runtime, "close_board_db_cache", lambda board_id=None: None)

    await bundle.graph_lifecycle.close(None)

    assert board_database.closed is True
    assert global_database.closed is False
    assert tuple(Path(path) for path in bundle.grafx_pool.pooled_paths()) == (
        global_path,
    )


def test_unguarded_storage_window_never_enters_the_native_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Guard:
        @contextmanager
        def closing(self, *, timeout: float):
            assert timeout == 7.0
            events.append("guard-enter")
            try:
                yield True, 0
            finally:
                events.append("guard-exit")

    def writer_must_not_run(*_args: Any, **_kwargs: Any):
        raise AssertionError("nested Ladybug writer")

    monkeypatch.setattr(
        connection_pool,
        "close_board_connection",
        lambda board_id: events.append(f"pool-close:{board_id}"),
    )
    monkeypatch.setattr(kg_runtime, "_get_close_guard", lambda _board_id: _Guard())
    monkeypatch.setattr(
        kg_runtime,
        "_close_cached_db_unguarded",
        lambda board_id: events.append(f"cache-close:{board_id}"),
    )
    monkeypatch.setattr(ladybug_writer, "ladybug_writer_scope", writer_must_not_run)

    with kg_runtime.board_storage_mutation_window_unguarded(
        "board-a",
        phase="test",
        drain_timeout=7.0,
    ):
        events.append("body")

    assert events == [
        "pool-close:board-a",
        "guard-enter",
        "cache-close:board-a",
        "body",
        "guard-exit",
    ]


@pytest.mark.asyncio
async def test_routed_ladybug_rebuild_ensures_schema_without_nested_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build(
        tmp_path / "kg",
        _GrafxConnector(),
        board_backend="ladybug",
    )
    path = _publish_ladybug_binding(bundle, "board-rebuild")
    windows: list[str] = []
    ensured: list[str] = []

    @contextmanager
    def unguarded_window(board_id: str, *, phase: str):
        windows.append(f"{board_id}:{phase}")
        yield

    def guarded_window_must_not_run(*_args: Any, **_kwargs: Any):
        raise AssertionError("nested writer/close guard")

    monkeypatch.setattr(kg_runtime, "board_kuzu_path", lambda _board_id: path)
    monkeypatch.setattr(
        kg_runtime,
        "board_storage_mutation_window_unguarded",
        unguarded_window,
    )
    monkeypatch.setattr(
        kg_runtime,
        "board_storage_mutation_window",
        guarded_window_must_not_run,
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )
    monkeypatch.setattr(
        kg_runtime,
        "ensure_board_graph_bootstrapped_unguarded",
        lambda board_id: ensured.append(board_id),
    )

    report = await bundle.graph_lifecycle.rebuild("board-rebuild")

    assert report.status == "rebuilt"
    assert report.steps == (
        "close_all_connections",
        "ensure_board_graph_bootstrapped",
    )
    assert ensured == ["board-rebuild"]
    assert windows == ["board-rebuild:graph_lifecycle_rebuild"]


def test_ladybug_schema_ensure_uses_the_owned_window_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "graph.lbug"
    path.write_bytes(b"ladybug")
    events: list[str] = []
    connection = object()

    @contextmanager
    def registered(board_id: str, *, within_close_window: bool = False):
        assert board_id == "board-owned"
        assert within_close_window is True
        events.append("connection-enter")
        try:
            yield object(), connection
        finally:
            events.append("connection-exit")

    monkeypatch.setattr(kg_runtime, "board_kuzu_path", lambda _board_id: path)
    monkeypatch.setattr(kg_runtime, "registered_raw_connection", registered)
    monkeypatch.setattr(
        kg_runtime,
        "_apply_board_schema_and_stamp_meta",
        lambda observed, board_id: events.append(
            f"schema:{observed is connection}:{board_id}"
        ),
    )
    monkeypatch.setattr(
        kg_runtime,
        "_enforce_embedding_guard_on_connection",
        lambda observed, board_id: events.append(
            f"embedding:{observed is connection}:{board_id}"
        ),
    )

    handle = kg_runtime.ensure_board_graph_bootstrapped_unguarded("board-owned")

    assert handle.path == path
    assert events == [
        "connection-enter",
        "schema:True:board-owned",
        "embedding:True:board-owned",
        "connection-exit",
    ]


@pytest.mark.asyncio
async def test_runtime_and_recovery_callbacks_do_not_reenter_the_outer_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build(
        tmp_path / "kg",
        _GrafxConnector(),
        board_backend="ladybug",
    )
    purge_path = _publish_ladybug_binding(bundle, "board-purge")
    recovery_path = _publish_ladybug_binding(bundle, "board-recover")
    paths = {
        "board-purge": purge_path,
        "board-recover": recovery_path,
    }
    phases: list[str] = []

    @contextmanager
    def one_outer_window(board_id: str, *, phase: str):
        phases.append(f"{board_id}:{phase}")
        yield

    def purge_unguarded(board_id: str, *, reason: str):
        path = paths[board_id]
        path.unlink()
        return [str(path)], f"quarantine:{reason}"

    async def recover_unguarded(board_id: str) -> WalRecoveryReport:
        return WalRecoveryReport(
            board_id=board_id,
            status="recovered",
            main_untouched=True,
        )

    monkeypatch.setattr(kg_runtime, "board_kuzu_path", lambda board_id: paths[board_id])
    monkeypatch.setattr(
        kg_runtime,
        "board_storage_mutation_window",
        one_outer_window,
    )
    monkeypatch.setattr(
        kg_runtime,
        "purge_board_graph_storage_with_receipt_unguarded",
        purge_unguarded,
    )
    monkeypatch.setattr(
        kg_runtime,
        "purge_board_graph_storage_with_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("guarded purge callback")
        ),
    )
    monkeypatch.setattr(
        wal_recovery.CommunityGraphRecovery,
        "recover_wal_only_unguarded",
        lambda _self, board_id: recover_unguarded(board_id),
    )
    monkeypatch.setattr(
        wal_recovery.CommunityGraphRecovery,
        "recover_wal_only",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("guarded recovery callback")
        ),
    )

    purged = bundle.graph_runtime_store.purge_board_graph(
        "board-purge",
        reason="test",
    )
    recovered = await bundle.graph_recovery.recover_wal_only("board-recover")

    assert purged.status == "purged"
    assert recovered.status == "recovered"
    assert phases == [
        "board-purge:purge_board_graph",
        "board-recover:recover_wal_only",
    ]
