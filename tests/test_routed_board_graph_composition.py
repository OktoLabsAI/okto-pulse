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

import okto_pulse.community.adapters.routed_board_graph_composition as composition
import okto_pulse.community.adapters.routed_graph_transaction as routed_transaction
from okto_pulse.community.adapters.grafx_database_pool import (
    CommunityGrafxDatabasePool,
    GrafxDatabasePoolError,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    BOARD_BINDING_FILENAME,
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.graph_rollout_journal import (
    CommunityGraphRolloutJournal,
    GraphRolloutJournalConflict,
    RolloutEndpointIdentity,
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
        self.databases: list[_FakeGrafxDatabase] = []

    def __call__(
        self,
        path: Path,
        *,
        page_size: int,
        descriptor_revalidation: str = "strict",
        read_only: bool = False,
    ) -> _FakeGrafxDatabase:
        path = Path(path)
        self.calls.append((path, page_size))
        self.descriptor_revalidation_calls.append(descriptor_revalidation)
        self.read_only_calls.append(read_only)
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
        kg_ladybug_max_db_size_gb=2,
    )


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


def _leave_erased_rollout_with_unbound_ladybug_residue(
    bundle: composition.CommunityRoutedBoardGraphComposition,
    board_id: str,
) -> tuple[CommunityGraphRolloutJournal, Path, Path]:
    source_path = _publish_ladybug_binding(bundle, board_id)
    source = bundle.binding_store.acquire_board_binding(board_id)
    candidate_path = bundle.binding_store.board_grafx_path(
        board_id,
        "rollout-candidate-1",
    )
    journal = CommunityGraphRolloutJournal(bundle.binding_store.root, board_id)
    rollout = journal.start(
        source=RolloutEndpointIdentity(
            backend="ladybug",
            binding_sha256=source.binding_sha256,
            generation=source.generation,
            physical_path=source_path,
        ),
        candidate=RolloutEndpointIdentity(
            backend="grafx",
            binding_sha256=None,
            generation="rollout-candidate-1",
            physical_path=candidate_path,
            page_size=PAGE_SIZE,
        ),
    )
    journal.close_for_privacy(expected_version=rollout.state_version)
    binding_path = source_path.parent / BOARD_BINDING_FILENAME
    binding_path.unlink()
    binding_lock = Path(f"{binding_path}.lock")
    if binding_lock.exists():
        binding_lock.unlink()
    return journal, source_path, candidate_path


def test_grafx_common_write_fence_closes_rollout_rollback_after_route_revalidation(
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
    recorder = SimpleNamespace(
        close_rollback_before_write_if_active=lambda *args: events.append(
            ("close_rollback", *args)
        )
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
        recorder,
        configured_page_size=PAGE_SIZE,
        connect=None,
    )

    access.write_fence("board-1", "schema_write")

    assert events == [
        ("lease", "board-1", "schema_write"),
        ("resolve", "board-1", True),
        ("revalidate", snapshot, True),
        ("close_rollback", "board-1", "a" * 64, "grafx"),
    ]


def test_grafx_common_write_fence_never_closes_rollback_for_a_stale_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = SimpleNamespace(
        backend="grafx",
        page_size=PAGE_SIZE,
        binding_sha256="b" * 64,
    )
    recorder_calls: list[object] = []

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
    recorder = SimpleNamespace(
        close_rollback_before_write_if_active=lambda *args: recorder_calls.append(args)
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
        recorder,
        configured_page_size=PAGE_SIZE,
        connect=None,
    )

    with pytest.raises(GraphCapabilityUnavailable):
        access.write_fence("board-1", "schema_write")

    assert recorder_calls == []


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
    recorder = SimpleNamespace(
        close_rollback_before_write_if_active=lambda *_args: None
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
        recorder,
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


def test_composed_rollout_privacy_keeps_tombstone_until_finalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "kg"
    bundle = _build(root, _GrafxConnector(), board_backend="grafx")
    source_path = _publish_ladybug_binding(bundle, "board-erase")
    source = bundle.binding_store.acquire_board_binding("board-erase")
    journal = CommunityGraphRolloutJournal(root, "board-erase")
    journal.start(
        source=RolloutEndpointIdentity(
            backend="ladybug",
            binding_sha256=source.binding_sha256,
            generation=source.generation,
            physical_path=source_path,
        ),
        candidate=RolloutEndpointIdentity(
            backend="grafx",
            binding_sha256=None,
            generation="rollout-candidate-1",
            physical_path=bundle.binding_store.board_grafx_path(
                "board-erase", "rollout-candidate-1"
            ),
            page_size=PAGE_SIZE,
        ),
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, failure_phase: None,
    )
    invalidate_rollout = bundle.graph_runtime_store._rollout_erase_unguarded
    finalize_rollout = bundle.graph_runtime_store._rollout_finalize_erase_unguarded
    assert invalidate_rollout is not None
    assert finalize_rollout is not None

    first = invalidate_rollout("board-erase", reason="privacy")
    retry = invalidate_rollout("board-erase", reason="privacy_retry")

    assert first.status == "erased"
    assert first.removed is True
    assert retry.status == "not_found"
    assert retry.not_found is True
    assert journal.read().state == "erased"
    assert journal.privacy_storage_present() is True
    with pytest.raises(GraphRolloutJournalConflict) as refused:
        journal.prepare_if_active(
            family="upsert_node",
            payload={"payload": "redacted"},
            expected_binding_sha256=source.binding_sha256,
            backend="ladybug",
        )
    assert refused.value.details["reason"] == "rollout_not_writable"

    finalized = finalize_rollout("board-erase", reason="privacy")

    assert finalized.status == "erased"
    assert finalized.removed is True
    assert journal.privacy_storage_present() is False


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
    ladybug = bundle.binding_store.board_ladybug_path("board-ambiguous")
    ladybug.parent.mkdir(parents=True)
    ladybug.write_bytes(b"ladybug")
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
