"""Composition contracts for the routed Community Board provider bundle."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from okto_grafx.errors import GrafxSchemaVersionMismatch, GrafxUnsupportedOperation
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
)
from okto_pulse.core.kg.safe_write_lifecycle import STEP_CHECKPOINT, STEP_FLUSH, STEP_FSYNC

import okto_pulse.community.adapters.routed_board_graph_composition as composition
import okto_pulse.community.adapters.routed_graph_transaction as routed_transaction
from okto_pulse.community.adapters.grafx_database_pool import (
    CommunityGrafxDatabasePool,
    GrafxDatabasePoolError,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBindingStore,
)

PAGE_SIZE = 8192


def _coverage_access(tmp_path, monkeypatch):
    connector = _GrafxConnector()
    snapshot = SimpleNamespace(scope_id='r7-readers', backend='grafx',
        active_path=tmp_path / 'generation-1', page_size=PAGE_SIZE)
    fences, admissions = [], []
    resolver = SimpleNamespace(
        current_board_snapshot=lambda *args, **kwargs: snapshot,
        revalidate_snapshot=lambda *args, **kwargs: None,
        admit_grafx_route=lambda snap, db, operation: admissions.append((snap.active_path, db.path)),
    )
    monkeypatch.setattr(composition, 'revalidate_board_graph_write_lease',
        lambda board_id, failure_phase: fences.append(failure_phase))
    pools = tuple(CommunityGrafxDatabasePool(tmp_path, connect=connector, read_only=True)
                  for _ in range(2))
    access = composition._GrafxBoardAccess(resolver, SimpleNamespace(), SimpleNamespace(),
        configured_page_size=PAGE_SIZE, connect=None, read_pools=pools, query_pools=pools)
    return access, snapshot, pools, connector, fences, admissions


def test_coverage_cold_readers_never_open_checkpoint_or_prepare(tmp_path, monkeypatch):
    access, snapshot, _, connector, fences, _ = _coverage_access(tmp_path, monkeypatch)
    board = snapshot.scope_id
    with access.query_execution.scope(board, timeout_ms=500, existing_only=True):
        with pytest.raises(GraphCapabilityUnavailable) as refused:
            with access.read_database_scope(board):
                pytest.fail('cold reader admitted')
        assert refused.value.details == {'reason': 'prepared_reader_unavailable'}
        with pytest.raises(GraphCapabilityUnavailable):
            access.read_database(board)
        with pytest.raises(GraphCapabilityUnavailable):
            access.prepare_query_readers(board)
        with pytest.raises(GraphCapabilityUnavailable):
            access.database(board)
        with pytest.raises(GraphCapabilityUnavailable):
            access.write_fence(board, 'checkpoint')
    assert connector.calls == [] and fences == []


def test_coverage_unconfigured_readers_report_configuration_without_opening(tmp_path, monkeypatch):
    access, snapshot, _, connector, fences, _ = _coverage_access(tmp_path, monkeypatch)
    access.query_pools = ()
    with access.query_execution.scope(snapshot.scope_id, timeout_ms=500, existing_only=True):
        with pytest.raises(GraphCapabilityUnavailable) as refused:
            with access.read_database_scope(snapshot.scope_id):
                pytest.fail('unconfigured reader admitted')
    assert refused.value.details == {'reason': 'query_readers_not_configured'}
    assert connector.calls == [] and fences == []


def test_prepared_readers_pin_exact_generation_and_release_on_failure(tmp_path, monkeypatch):
    access, snapshot, pools, connector, fences, admissions = _coverage_access(tmp_path, monkeypatch)
    board = snapshot.scope_id
    assert access.prepare_query_readers(board) is True
    assert len(connector.calls) == 2 and len(fences) == 3
    with pytest.raises(RuntimeError, match='native failure'):
        with access.query_execution.scope(board, timeout_ms=500, existing_only=True):
            with access.read_database_scope(board) as reader:
                assert reader.path == str(snapshot.active_path)
                assert sum(pool.pin_count(snapshot.active_path) for pool in pools) == 1
                raise RuntimeError('native failure')
    assert sum(pool.pin_count(snapshot.active_path) for pool in pools) == 0
    assert len(connector.calls) == 2
    snapshot.active_path = tmp_path / 'generation-2'
    with access.query_execution.scope(board, timeout_ms=500, existing_only=True):
        with pytest.raises(GraphCapabilityUnavailable):
            with access.read_database_scope(board):
                pytest.fail('old generation was reused')
    assert len(connector.calls) == 2
    access.prepare_query_readers(board)
    with access.query_execution.scope(board, timeout_ms=500, existing_only=True):
        with access.read_database_scope(board) as reader:
            assert reader.path == str(snapshot.active_path)
    assert all(str(path) == observed for path, observed in admissions)
    for pool in pools:
        pool.close_all()


@pytest.mark.parametrize(('step', 'expected'), [
    (STEP_CHECKPOINT, ['checkpoint', 'prepare']),
    (STEP_FLUSH, ['flush']),
    (STEP_FSYNC, ['flush', 'checkpoint', 'prepare']),
])
def test_lifecycle_prepares_after_durability_without_removing_any_step(tmp_path, step, expected):
    from okto_pulse.community.adapters.grafx_graph_lifecycle import CommunityGrafxGraphLifecycle
    events = []
    database = SimpleNamespace(identity=SimpleNamespace(page_size=PAGE_SIZE),
        checkpoint=lambda: events.append('checkpoint'), flush=lambda: events.append('flush'))
    lifecycle = CommunityGrafxGraphLifecycle(lambda _: database, lambda _: tmp_path,
        lambda _: None, lambda *args: None,
        prepare_query_readers=lambda _: events.append('prepare'))
    result = lifecycle.apply_step('r7-durability', 'board_graph', step)
    assert result.ok, result.detail
    assert events == expected


def test_optional_reader_preparation_failure_does_not_rewrite_durability(tmp_path, monkeypatch):
    access, snapshot, pools, connector, fences, _ = _coverage_access(tmp_path, monkeypatch)
    def busy(*args, **kwargs):
        raise GrafxDatabasePoolError('busy reader', reason='pool_open_failed')
    for pool in pools:
        monkeypatch.setattr(pool, 'get', busy)
    assert access.prepare_query_readers(snapshot.scope_id) is False
    assert connector.calls == [] and len(fences) == 3
    with access.query_execution.scope(snapshot.scope_id, timeout_ms=500, existing_only=True):
        with pytest.raises(GraphCapabilityUnavailable):
            with access.read_database_scope(snapshot.scope_id):
                pytest.fail('failed preparation manufactured a reader')


class _FakeGrafxTransaction:
    def __init__(self) -> None:
        self.active = True
        self.report = None

    def commit(self) -> None:
        self.active = False

    def rollback(self) -> None:
        self.active = False


class _FakeGrafxDatabase:
    def __init__(
        self, path: Path, page_size: int, descriptor_revalidation: str = "strict"
    ) -> None:
        self.path = str(path)
        self.identity = SimpleNamespace(page_size=page_size)
        self.descriptor_revalidation = descriptor_revalidation
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
        self.descriptor_revalidation_calls: list[str] = []
        self.read_only_calls: list[bool] = []
        self.constructor_options_calls: list[dict[str, Any]] = []
        self.databases: list[_FakeGrafxDatabase] = []

    def __call__(
        self,
        path: Path,
        *,
        page_size: int,
        descriptor_revalidation: str = "strict",
        read_only: bool = False,
        **constructor_options: Any,
    ) -> _FakeGrafxDatabase:
        path = Path(path)
        self.calls.append((path, page_size))
        self.descriptor_revalidation_calls.append(descriptor_revalidation)
        self.read_only_calls.append(read_only)
        self.constructor_options_calls.append(constructor_options)
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
        database = _FakeGrafxDatabase(path, observed, descriptor_revalidation)
        self.databases.append(database)
        return database


def _settings(
    root: Path,
    *,
    board_backend: str = "grafx",
    global_backend: str = "grafx",
    page_size: int = PAGE_SIZE,
    descriptor_revalidation: str = "strict",
) -> SimpleNamespace:
    return SimpleNamespace(
        kg_base_dir=str(root),
        kg_graph_backend=board_backend,
        kg_global_graph_backend=global_backend,
        kg_grafx_page_size=page_size,
        kg_grafx_descriptor_revalidation=descriptor_revalidation,
    )


def test_runtime_budget_uses_composed_constructor_values_without_opening_graphs(tmp_path):
    settings = _settings(tmp_path)
    settings.kg_grafx_buffer_pool_mb = 96
    settings.kg_grafx_read_participants = 3
    connector = _GrafxConnector()
    bundle = composition.build_community_routed_board_graph_composition(settings=settings, grafx_connect=connector)
    settings.kg_grafx_buffer_pool_mb = 128
    budget = bundle.graph_runtime_store.budget_snapshot()
    assert budget.status == "available"
    assert budget.effective["board_buffer_pool_mb"] == 96
    assert budget.effective["read_participants"] == 3
    assert budget.process_envelope["buffer_pool_per_board_mb"] == 384
    assert budget.process_envelope["total_process_bound_available"] is False
    assert connector.calls == []


def _build(
    root: Path,
    connector: _GrafxConnector,
    *,
    board_backend: str = "grafx",
    page_size: int = PAGE_SIZE,
    descriptor_revalidation: str = "strict",
) -> composition.CommunityRoutedBoardGraphComposition:
    return composition.build_community_routed_board_graph_composition(
        settings=_settings(
            root,
            board_backend=board_backend,
            page_size=page_size,
            descriptor_revalidation=descriptor_revalidation,
        ),
        grafx_connect=connector,
    )


def test_schema_manager_receives_the_same_scoped_reader_scheduler(
    tmp_path, monkeypatch
):
    original = composition.CommunityGrafxGraphSchemaManager
    captured = []

    def schema_factory(*args, **kwargs):
        captured.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(composition, "CommunityGrafxGraphSchemaManager", schema_factory)
    _build(tmp_path / "kg", _GrafxConnector())
    assert len(captured) == 1
    scope = captured[0]["read_database_scope"]
    resolver = captured[0]["read_database_resolver"]
    assert scope.__self__ is resolver.__self__
    assert scope.__func__ is composition._GrafxBoardAccess.read_database_scope


def _make_windows_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction semantics are required")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip(f"junction creation unavailable: {completed.stderr.strip()}")


def test_grafx_common_write_fence_revalidates_lease_and_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    snapshot = SimpleNamespace(
        backend="grafx",
        page_size=PAGE_SIZE,
        binding_sha256="a" * 64,
    )
    resolver = SimpleNamespace(
        current_board_snapshot=lambda board_id, require_physical: (
            events.append(("resolve", board_id, require_physical)) or snapshot
        ),
        revalidate_snapshot=lambda observed, require_physical: events.append(
            ("revalidate", observed, require_physical)
        ),
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda board_id, failure_phase: events.append(
            ("lease", board_id, failure_phase)
        ),
    )
    access = composition._GrafxBoardAccess(
        resolver,
        SimpleNamespace(),
        SimpleNamespace(),
        configured_page_size=PAGE_SIZE,
        connect=None,
    )

    access.write_fence("board-1", "schema_write")

    assert events == [
        ("lease", "board-1", "schema_write"),
        ("resolve", "board-1", True),
        ("revalidate", snapshot, True),
    ]


def test_grafx_common_write_fence_rejects_stale_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = SimpleNamespace(
        backend="grafx",
        page_size=PAGE_SIZE,
        binding_sha256="b" * 64,
    )

    def reject_stale_route(_snapshot: object, *, require_physical: bool) -> None:
        assert require_physical is True
        raise GraphCapabilityUnavailable(
            "stale route",
            details={"operation": "test", "reason": "binding_changed"},
        )

    resolver = SimpleNamespace(
        current_board_snapshot=lambda _board_id, require_physical: snapshot,
        revalidate_snapshot=reject_stale_route,
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, failure_phase: None,
    )
    access = composition._GrafxBoardAccess(
        resolver,
        SimpleNamespace(),
        SimpleNamespace(),
        configured_page_size=PAGE_SIZE,
        connect=None,
    )

    with pytest.raises(GraphCapabilityUnavailable):
        access.write_fence("board-1", "schema_write")


def test_first_reader_join_checkpoints_transparently_when_wal_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "grafx" / "generation-1"
    snapshot = SimpleNamespace(
        scope_id="board-reader-join",
        backend="grafx",
        active_path=path,
        page_size=PAGE_SIZE,
        binding_sha256="c" * 64,
    )
    checkpoints: list[str] = []
    admissions: list[tuple[object, str]] = []
    revalidations: list[object] = []

    class Writer:
        def checkpoint(self) -> None:
            checkpoints.append("checkpoint")

    class WriterPool:
        def get(self, _path: Path, *, page_size: int) -> Writer:
            assert page_size == PAGE_SIZE
            return Writer()

        def pooled_paths(self) -> tuple[str, ...]:
            return ()

    class ReadPool:
        read_only = True

        def __init__(self) -> None:
            self.calls = 0

        def get(self, _path: Path, *, page_size: int) -> object:
            assert page_size == PAGE_SIZE
            self.calls += 1
            if self.calls <= 2:
                cause = GrafxUnsupportedOperation(
                    "checkpoint is required",
                    field="read_only_consistency",
                )
                raise GrafxDatabasePoolError(
                    "read-only open refused",
                    reason="pool_open_failed",
                ) from cause
            return SimpleNamespace(path=str(path))

        def pooled_paths(self) -> tuple[str, ...]:
            return ()

    resolver = SimpleNamespace(
        current_board_snapshot=lambda _board_id, require_physical: snapshot,
        admit_grafx_route=lambda _snapshot, database, operation: admissions.append(
            (database, operation)
        ),
        revalidate_snapshot=lambda observed, require_physical: revalidations.append(
            (observed, require_physical)
        ),
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, failure_phase: revalidations.append(failure_phase),
    )
    read_pool = ReadPool()
    access = composition._GrafxBoardAccess(
        resolver,
        WriterPool(),
        SimpleNamespace(),
        configured_page_size=PAGE_SIZE,
        connect=None,
        read_pools=(read_pool,),
    )

    opened = access.read_database(snapshot.scope_id)

    assert opened.path == str(path)
    assert read_pool.calls == 3
    assert checkpoints == ["checkpoint"]
    assert admissions[-1][1] == "resolve_routed_board_grafx_read_database"
    assert "grafx_read_join_checkpoint" in revalidations


def test_build_is_read_only_and_every_board_port_shares_one_route_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "kg"
    connector = _GrafxConnector()

    bundle = _build(root, connector)

    assert not root.exists()
    assert connector.calls == []
    assert bundle.grafx_pool._max_entries is None
    assert len(bundle.grafx_read_pools) == 2
    assert all(pool.read_only for pool in bundle.grafx_read_pools)
    assert bundle.graph_transaction._grafx_pool is bundle.grafx_pool
    assert not hasattr(bundle, "graph_rollout_coordinator")
    assert "graph_rollout_coordinator" not in bundle.registry_providers()
    observation = bundle.registry_providers()["graph_health_observation"]
    assert observation is not None
    with observation.scope("missing"), pytest.raises(GraphCapabilityUnavailable):
        bundle.cypher_executor.execute_read_only("missing", "RETURN 1")
    assert connector.calls == []
    assert not root.exists()
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


def test_health_proves_unbound_empty_board_absent_without_opening_or_writing(tmp_path):
    from okto_pulse.core.kg.interfaces.graph_runtime_store import GraphRuntimeObservationState

    connector = _GrafxConnector()
    bundle = _build(tmp_path, connector)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*'))
    with bundle.graph_health_observation.scope('e2e-board-0001'):
        state = bundle.graph_runtime_store.graph_state('e2e-board-0001', generation='unmaterialized-v1')
    assert state.normalized_state is GraphRuntimeObservationState.CONFIRMED_ABSENT, state
    assert connector.calls == []
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob('*')) == before


def test_board_reader_lanes_are_distinct_from_the_writer_participant(
    tmp_path: Path,
) -> None:
    connector = _GrafxConnector()
    bundle = _build(tmp_path / "kg", connector)
    snapshot = bundle.initialize_board_route("board-reader-lanes")

    writer = bundle.grafx_pool.get(snapshot.active_path, page_size=PAGE_SIZE)
    readers = tuple(
        pool.get(snapshot.active_path, page_size=PAGE_SIZE)
        for pool in bundle.grafx_read_pools
    )

    assert readers[0] is not readers[1]
    assert all(reader is not writer for reader in readers)
    assert connector.read_only_calls == [False, True, True]


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

    with pytest.raises(ValueError, match="descriptor revalidation policy"):
        composition.build_community_routed_board_graph_composition(
            settings=_settings(root, descriptor_revalidation="generation"),
            binding_store=first.binding_store,
            resolver=first.resolver,
            grafx_pool=first.grafx_pool,
        )


def test_builder_applies_one_generation_policy_to_the_shared_pool(
    tmp_path: Path,
) -> None:
    connector = _GrafxConnector()
    bundle = _build(
        tmp_path / "kg",
        connector,
        descriptor_revalidation="generation",
    )

    snapshot = bundle.initialize_board_route("board-generation")

    assert bundle.grafx_pool.descriptor_revalidation == "generation"
    assert connector.descriptor_revalidation_calls == ["generation"]
    assert snapshot.backend == "grafx"


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


def test_adopt_existing_board_route_is_noncreating_and_publishes_only_storage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "kg"
    connector = _GrafxConnector()
    bundle = _build(root, connector)

    assert bundle.adopt_existing_board_route("board-absent") is None
    assert connector.calls == []
    assert not root.exists()

    existing = bundle.binding_store.board_grafx_path(
        "board-existing",
        "generation-existing",
    )
    existing.mkdir(parents=True)
    (existing / "grafx.meta").write_bytes(b"grafx")

    adopted = bundle.adopt_existing_board_route("board-existing")

    assert adopted is not None
    assert adopted.backend == "grafx"
    assert adopted.active_path == existing
    assert bundle.resolver.acquire_board_route("board-existing") == adopted
    assert connector.calls == [(existing, PAGE_SIZE)]


def test_adopt_returns_an_existing_binding_without_recreating_missing_physical(
    tmp_path: Path,
) -> None:
    connector = _GrafxConnector()
    bundle = _build(tmp_path / "kg", connector)
    initial = bundle.initialize_board_route("board-purged")
    assert bundle.grafx_pool.close(initial.active_path) is True
    (initial.active_path / "grafx.meta").unlink()
    initial.active_path.rmdir()

    adopted = bundle.adopt_existing_board_route("board-purged")

    assert adopted == initial
    assert not initial.active_path.exists()
    assert connector.calls == [(initial.active_path, PAGE_SIZE)]


def test_adopt_fails_closed_on_ambiguous_existing_storage(tmp_path: Path) -> None:
    root = tmp_path / "kg"
    bundle = _build(root, _GrafxConnector())
    legacy = bundle.binding_store.board_ladybug_path("board-ambiguous")
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    grafx = bundle.binding_store.board_grafx_path(
        "board-ambiguous",
        "generation-existing",
    )
    grafx.mkdir(parents=True)
    (grafx / "grafx.meta").write_bytes(b"grafx")

    with pytest.raises(GraphCapabilityUnavailable) as ambiguous:
        bundle.adopt_existing_board_route("board-ambiguous")

    assert ambiguous.value.details["reason"] == "graph_route_storage_ambiguous"
    with pytest.raises(GraphCapabilityUnavailable) as missing:
        bundle.binding_store.inspect_board_binding("board-ambiguous")
    assert missing.value.details["reason"] == "binding_missing"


def test_adoption_retries_only_a_typed_persisted_grafx_geometry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "kg"
    store = CommunityGraphBackendBindingStore(root)
    existing = store.board_grafx_path("board-adopt", "generation-crash")
    existing.mkdir(parents=True)
    (existing / "grafx.meta").write_bytes(b"grafx")
    connector = _GrafxConnector(persisted_page_size=4096)
    bundle = _build(root, connector, board_backend="grafx", page_size=PAGE_SIZE)

    snapshot = bundle.initialize_board_route("board-adopt")

    assert snapshot.backend == "grafx"
    assert snapshot.active_path == existing
    assert snapshot.page_size == 4096
    assert connector.calls == [(existing, PAGE_SIZE), (existing, 4096)]
    assert not bundle.binding_store.board_ladybug_path("board-adopt").exists()


@pytest.mark.asyncio
async def test_routed_grafx_open_validates_without_schema_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = _GrafxConnector()
    bundle = _build(tmp_path / "kg", connector)
    snapshot = bundle.initialize_board_route("board-open")
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
    assert handle.readers_prepared is True
    assert validations == ["schema", "board-meta"]
    # Production routed open must make the exact generation available to
    # existing-only Coverage reads, without relying on a later writer commit.
    opened_count = len(connector.calls)
    assert sum(connector.read_only_calls) == len(bundle.grafx_query_pools)
    assert bundle.grafx_query_pools
    from okto_pulse.community.adapters.grafx_foreground_query_limits import (
        FOREGROUND_QUERY_LIMITS, FOREGROUND_READER_BUFFER_MB,
    )
    for options, read_only in zip(connector.constructor_options_calls, connector.read_only_calls):
        if read_only:
            assert options == dict(FOREGROUND_QUERY_LIMITS, buffer_budget_bytes=FOREGROUND_READER_BUFFER_MB * 1024 * 1024)
    for pool in bundle.grafx_query_pools:
        with pool.acquire_existing(snapshot.active_path, page_size=PAGE_SIZE) as lease:
            assert lease.database.path == str(snapshot.active_path)
            assert pool.pin_count(snapshot.active_path) == 1
        assert pool.pin_count(snapshot.active_path) == 0
    assert len(connector.calls) == opened_count


@pytest.mark.parametrize('available_readers', [0, 1, 2])
def test_reader_preparation_reports_actual_available_participants(tmp_path, monkeypatch, available_readers):
    access, snapshot, pools, _, _, _ = _coverage_access(tmp_path, monkeypatch)

    def unavailable(*args, **kwargs):
        raise GrafxDatabasePoolError('reader unavailable', reason='pool_open_failed')

    for pool in pools[available_readers:]:
        monkeypatch.setattr(pool, 'get', unavailable)
    assert access.prepare_query_readers(snapshot.scope_id) is (available_readers > 0)
    with access.query_execution.scope(snapshot.scope_id, timeout_ms=500, existing_only=True):
        if available_readers:
            with access.read_database_scope(snapshot.scope_id) as reader:
                assert reader.path == str(snapshot.active_path)
        else:
            with pytest.raises(GraphCapabilityUnavailable):
                with access.read_database_scope(snapshot.scope_id):
                    pytest.fail('unavailable reader admitted')
    assert all(pool.pin_count(snapshot.active_path) == 0 for pool in pools)
    for pool in pools:
        pool.close_all()


def test_no_configured_readers_cannot_report_prepared(tmp_path, monkeypatch):
    access, snapshot, _, connector, _, _ = _coverage_access(tmp_path, monkeypatch)
    access.query_pools = ()
    assert access.prepare_query_readers(snapshot.scope_id) is False
    assert connector.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('readiness', [None, False, True])
async def test_direct_lifecycle_requires_explicit_reader_readiness(tmp_path, monkeypatch, readiness):
    import okto_pulse.community.adapters.grafx_graph_lifecycle as lifecycle_module
    database = SimpleNamespace(closed=False, identity=SimpleNamespace(page_size=PAGE_SIZE))
    monkeypatch.setattr(lifecycle_module, 'ensure_current_grafx_board_schema', lambda *args, **kwargs: None)
    lifecycle = lifecycle_module.CommunityGrafxGraphLifecycle(
        lambda _: database, lambda _: tmp_path, lambda _: None, lambda *args: None,
        prepare_query_readers=lambda _: readiness)
    handle = await lifecycle.open('board-readiness')
    assert handle.opened is True
    assert handle.readers_prepared is (readiness is True)


@pytest.mark.asyncio
async def test_routed_open_writer_success_does_not_imply_reader_success(tmp_path, monkeypatch):
    connector = _GrafxConnector()
    bundle = _build(tmp_path / 'kg', connector)
    snapshot = bundle.initialize_board_route('board-open-readers-unavailable')
    monkeypatch.setattr(composition, 'validate_current_grafx_schema', lambda _: 'fingerprint')
    monkeypatch.setattr(composition, 'read_current_grafx_schema_version', lambda _: '1')

    def unavailable(*args, **kwargs):
        raise GrafxDatabasePoolError('reader unavailable', reason='pool_open_failed')

    for pool in bundle.grafx_query_pools:
        monkeypatch.setattr(pool, 'get', unavailable)
    handle = await bundle.graph_lifecycle.open(snapshot.scope_id)
    assert handle.opened is True
    assert handle.readers_prepared is False
    assert not any(connector.read_only_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["prepare_query_readers", "prepare_query_reader"])
async def test_routed_open_refuses_lost_ownership_before_preparing_readers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_phase: str,
) -> None:
    from okto_pulse.core.kg.guarded_write import GuardedWriteError

    connector = _GrafxConnector()
    bundle = _build(tmp_path / "kg", connector)
    snapshot = bundle.initialize_board_route("board-open-lost")
    monkeypatch.setattr(composition, "validate_current_grafx_schema", lambda _: "fingerprint")
    monkeypatch.setattr(composition, "read_current_grafx_schema_version", lambda _: "1")

    def lost_owner(board_id: str, *, failure_phase: str) -> None:
        assert board_id == snapshot.scope_id
        if failure_phase == refused_phase:
            raise GuardedWriteError("writer_lease_lost", "ownership lost", retryable=False)

    refused_phase = failure_phase
    monkeypatch.setattr(composition, "revalidate_board_graph_write_lease", lost_owner)
    with pytest.raises(GuardedWriteError, match="ownership lost"):
        await bundle.graph_lifecycle.open(snapshot.scope_id)
    assert not any(connector.read_only_calls)
    assert all(pool.pin_count(snapshot.active_path) == 0 for pool in bundle.grafx_query_pools)


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
async def test_transaction_terminal_close_is_safe_in_copied_worker_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocking graph I/O may finish outside the begin ContextVar context."""

    connector = _GrafxConnector()
    bundle = _build(tmp_path / "kg", connector)
    snapshot = bundle.initialize_board_route("board-cross-context")
    monkeypatch.setattr(
        routed_transaction,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )

    scope = await bundle.graph_transaction.begin("board-cross-context")
    assert bundle.grafx_pool.pin_count(snapshot.active_path) == 1

    await asyncio.to_thread(lambda: asyncio.run(scope.rollback()))

    assert bundle.grafx_pool.pin_count(snapshot.active_path) == 0
    assert bundle.grafx_pool.close(snapshot.active_path) is True


@pytest.mark.parametrize("kind", ["directory", "file"])
@pytest.mark.parametrize("operation", ["inspect", "initialize"])
def test_retired_rollout_storage_is_refused_without_writes(tmp_path: Path, kind: str, operation: str) -> None:
    root = tmp_path / "kg"
    connector = _GrafxConnector()
    bundle = _build(root, connector, board_backend="grafx")
    retired = root / "boards" / "board-old" / "rollout"
    retired.parent.mkdir(parents=True)
    if kind == "directory":
        retired.mkdir()
        (retired / "opaque.bin").write_bytes(b"untrusted retired storage")
    else:
        retired.write_bytes(b"not a journal")

    def inventory():
        return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
                for p in root.rglob("*")}

    before = inventory()
    with pytest.raises(GraphCapabilityUnavailable) as raised:
        if operation == "inspect":
            bundle.binding_store.inspect_board_binding("board-old")
        else:
            bundle.binding_store.initialize_board_binding(
                board_id="board-old", backend="grafx", generation="generation-1",
                physical_path=bundle.binding_store.board_grafx_path("board-old", "generation-1"),
                page_size=PAGE_SIZE,
            )
    assert raised.value.details["reason"] == "graph_storage_format_incompatible"
    assert inventory() == before
    assert connector.calls == []
