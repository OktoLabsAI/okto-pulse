"""Composition contracts for the routed Community Board provider bundle."""

from __future__ import annotations

import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from okto_grafx.errors import GrafxSchemaVersionMismatch
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphUnavailable,
)
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
        self.databases: list[_FakeGrafxDatabase] = []

    def __call__(
        self,
        path: Path,
        *,
        page_size: int,
        descriptor_revalidation: str = "strict",
    ) -> _FakeGrafxDatabase:
        path = Path(path)
        self.calls.append((path, page_size))
        self.descriptor_revalidation_calls.append(descriptor_revalidation)
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


def test_ladybug_administrative_write_advances_rollout_high_water(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "kg"
    bundle = _build(root, _GrafxConnector(), board_backend="ladybug")
    source_path = _publish_ladybug_binding(bundle, "board-admin")
    source = bundle.binding_store.acquire_board_binding("board-admin")
    journal = CommunityGraphRolloutJournal(root, "board-admin")
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
                "board-admin", "rollout-candidate-1"
            ),
            page_size=PAGE_SIZE,
        ),
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, failure_phase: None,
    )

    with bundle.resolver.board_route_session("board-admin"):
        bundle.graph_schema_manager._revalidate_write_fence(
            "board-admin", "graph_schema_migrate"
        )
        bundle.graph_lifecycle._revalidate_write_fence(
            "board-admin", "graph_lifecycle_close"
        )

    assert journal.capture_high_water() == 1


def test_composed_rollout_privacy_keeps_tombstone_until_finalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "kg"
    bundle = _build(root, _GrafxConnector(), board_backend="ladybug")
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


@pytest.mark.parametrize(
    "route_door",
    (
        "initialize_board_route",
        "adopt_existing_board_route",
        "rematerialize_board_route",
    ),
)
def test_erased_rollout_refuses_every_route_creation_door_without_a_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route_door: str,
) -> None:
    board_id = f"board-erased-{route_door}"
    connector = _GrafxConnector()
    bundle = _build(
        tmp_path / route_door / "kg",
        connector,
        board_backend="ladybug",
    )
    journal, source_path, _candidate_path = (
        _leave_erased_rollout_with_unbound_ladybug_residue(bundle, board_id)
    )
    monkeypatch.setattr(
        kg_runtime,
        "board_kuzu_path",
        lambda observed_board_id: bundle.binding_store.board_ladybug_path(
            observed_board_id
        ),
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )

    with pytest.raises(GraphCapabilityUnavailable) as refused:
        getattr(bundle, route_door)(board_id)

    assert refused.value.details["reason"] == ("graph_rollout_privacy_tombstone_active")
    assert journal.read().state == "erased"
    assert source_path.read_bytes() == b"ladybug"
    assert not (source_path.parent / BOARD_BINDING_FILENAME).exists()
    assert connector.calls == []


def test_empty_board_can_be_initialized_after_privacy_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = "board-erased-finalized"
    connector = _GrafxConnector()
    bundle = _build(tmp_path / "kg", connector, board_backend="grafx")
    journal, source_path, candidate_path = (
        _leave_erased_rollout_with_unbound_ladybug_residue(bundle, board_id)
    )
    source_path.unlink()
    assert not source_path.exists()
    assert not candidate_path.exists()
    proof = journal.erase_privacy_storage()
    assert proof.storage_absent is True
    assert journal.privacy_storage_present() is False
    monkeypatch.setattr(
        kg_runtime,
        "board_kuzu_path",
        lambda observed_board_id: bundle.binding_store.board_ladybug_path(
            observed_board_id
        ),
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )

    snapshot = bundle.initialize_board_route(board_id)

    assert snapshot.backend == "grafx"
    assert snapshot.active_path.exists()
    assert connector.calls == [(snapshot.active_path, PAGE_SIZE)]


def test_initialize_and_privacy_invalidation_share_one_writer_serialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = "board-route-privacy-race"
    bundle = _build(tmp_path / "kg", _GrafxConnector(), board_backend="ladybug")
    source_path = _publish_ladybug_binding(bundle, board_id)
    source = bundle.binding_store.acquire_board_binding(board_id)
    journal = CommunityGraphRolloutJournal(bundle.binding_store.root, board_id)
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
                board_id,
                "rollout-candidate-1",
            ),
            page_size=PAGE_SIZE,
        ),
    )
    monkeypatch.setattr(
        kg_runtime,
        "board_kuzu_path",
        lambda observed_board_id: bundle.binding_store.board_ladybug_path(
            observed_board_id
        ),
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )

    guard_read = threading.Event()
    release_guard = threading.Event()
    privacy_writer_attempted = threading.Event()
    privacy_closed = threading.Event()
    events: list[str] = []
    failures: list[BaseException] = []
    initialized: list[object] = []

    real_read_if_exists = CommunityGraphRolloutJournal.read_if_exists

    def blocking_route_guard(
        observed_journal: CommunityGraphRolloutJournal,
    ):
        observed = real_read_if_exists(observed_journal)
        if threading.current_thread().name == "route-initialize":
            events.append("route_guard_read")
            guard_read.set()
            assert release_guard.wait(timeout=5.0)
        return observed

    monkeypatch.setattr(
        CommunityGraphRolloutJournal,
        "read_if_exists",
        blocking_route_guard,
    )

    real_initialize = bundle.resolver.initialize_board_route

    def observed_initialize(*args: Any, **kwargs: Any):
        snapshot = real_initialize(*args, **kwargs)
        events.append("route_resolved")
        return snapshot

    monkeypatch.setattr(
        bundle.resolver,
        "initialize_board_route",
        observed_initialize,
    )

    real_close_for_privacy = CommunityGraphRolloutJournal.close_for_privacy

    def observed_close_for_privacy(
        observed_journal: CommunityGraphRolloutJournal,
        *,
        expected_version: int,
    ):
        events.append("privacy_close")
        privacy_closed.set()
        return real_close_for_privacy(
            observed_journal,
            expected_version=expected_version,
        )

    monkeypatch.setattr(
        CommunityGraphRolloutJournal,
        "close_for_privacy",
        observed_close_for_privacy,
    )

    real_writer_scope = ladybug_writer.ladybug_writer_scope

    @contextmanager
    def tracked_writer_scope(*, scope: str, phase: str, **kwargs: Any):
        if phase == "privacy_invalidation_race":
            privacy_writer_attempted.set()
        with real_writer_scope(scope=scope, phase=phase, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(ladybug_writer, "ladybug_writer_scope", tracked_writer_scope)

    def initialize_route() -> None:
        try:
            initialized.append(bundle.initialize_board_route(board_id))
        except BaseException as failure:  # noqa: BLE001 - surface thread failure
            failures.append(failure)

    def invalidate_privacy() -> None:
        try:
            with kg_runtime.board_storage_mutation_window(
                board_id,
                phase="privacy_invalidation_race",
            ):
                invalidate = bundle.graph_runtime_store._rollout_erase_unguarded
                assert invalidate is not None
                invalidate(board_id, reason="privacy_race")
        except BaseException as failure:  # noqa: BLE001 - surface thread failure
            failures.append(failure)

    initialize_thread = threading.Thread(
        target=initialize_route,
        name="route-initialize",
    )
    privacy_thread = threading.Thread(
        target=invalidate_privacy,
        name="privacy-invalidate",
    )
    initialize_thread.start()
    assert guard_read.wait(timeout=5.0)
    privacy_thread.start()
    assert privacy_writer_attempted.wait(timeout=5.0)
    assert privacy_closed.is_set() is False
    release_guard.set()
    initialize_thread.join(timeout=5.0)
    privacy_thread.join(timeout=5.0)

    assert initialize_thread.is_alive() is False
    assert privacy_thread.is_alive() is False
    assert failures == []
    assert len(initialized) == 1
    assert journal.read().state == "erased"
    assert events == ["route_guard_read", "route_resolved", "privacy_close"]


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
    assert bundle.graph_rollout_coordinator._bindings is bundle.binding_store
    assert bundle.graph_rollout_coordinator._shadow._connector is connector
    assert "graph_rollout_coordinator" not in bundle.registry_providers()
    assert bundle.graph_rollout_coordinator not in bundle.registry_providers().values()
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


def test_rematerialize_is_the_only_door_after_purge_and_preserves_route_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector = _GrafxConnector()
    bundle = _build(tmp_path / "kg", connector)
    monkeypatch.setattr(
        kg_runtime,
        "board_kuzu_path",
        lambda board_id: bundle.binding_store.board_ladybug_path(board_id),
    )
    initial = bundle.initialize_board_route("board-rematerialize")
    assert bundle.grafx_pool.close(initial.active_path) is True
    (initial.active_path / "grafx.meta").unlink()
    initial.active_path.rmdir()

    with pytest.raises(GraphUnavailable) as ordinary_initialize:
        bundle.initialize_board_route("board-rematerialize")
    assert ordinary_initialize.value.details["reason"] == "physical_database_missing"
    assert connector.calls == [(initial.active_path, PAGE_SIZE)]

    restored = bundle.rematerialize_board_route("board-rematerialize")

    assert restored == initial
    assert restored.binding_sha256 == initial.binding_sha256
    assert restored.route_sha256 == initial.route_sha256
    assert restored.active_path.exists()
    assert connector.calls == [
        (initial.active_path, PAGE_SIZE),
        (initial.active_path, PAGE_SIZE),
    ]


def test_ladybug_rematerialization_uses_only_the_owned_window_primitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _build(
        tmp_path / "kg",
        _GrafxConnector(),
        board_backend="ladybug",
    )
    path = _publish_ladybug_binding(bundle, "board-rematerialize-ladybug")
    initial = bundle.resolver.acquire_board_route("board-rematerialize-ladybug")
    path.unlink()
    calls: list[str] = []

    @contextmanager
    def owned_window(board_id: str, *, phase: str):
        calls.append(f"window:{board_id}:{phase}")
        yield

    def rematerialize_unguarded(board_id: str):
        calls.append(f"physical:{board_id}")
        path.write_bytes(b"rematerialized-ladybug")
        return SimpleNamespace(path=path)

    monkeypatch.setattr(kg_runtime, "board_kuzu_path", lambda _board_id: path)
    monkeypatch.setattr(
        kg_runtime,
        "board_storage_mutation_window",
        owned_window,
    )
    monkeypatch.setattr(
        kg_runtime,
        "rematerialize_board_graph_unguarded",
        rematerialize_unguarded,
    )
    monkeypatch.setattr(
        kg_runtime,
        "bootstrap_board_graph",
        lambda _board_id: (_ for _ in ()).throw(
            AssertionError("normal bootstrap used during rematerialization")
        ),
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )

    restored = bundle.rematerialize_board_route("board-rematerialize-ladybug")

    assert restored == initial
    assert calls == [
        "window:board-rematerialize-ladybug:rematerialize_board_route_after_purge",
        "physical:board-rematerialize-ladybug",
    ]


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


def test_unbound_adopt_and_ladybug_privacy_erase_refuse_a_board_root_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "kg"
    bundle = _build(root, _GrafxConnector(), board_backend="ladybug")
    external = tmp_path / "outside-board"
    external.mkdir()
    sentinel = external / "graph.lbug"
    sentinel.write_bytes(b"private-external-data")
    boards_root = root / "boards"
    boards_root.mkdir(parents=True)
    junction = boards_root / "board-unbound"
    _make_windows_junction(junction, external)
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: root)
    monkeypatch.setattr(
        kg_runtime,
        "board_kuzu_path",
        lambda _board_id: junction / "graph.lbug",
    )

    try:
        with pytest.raises(GraphCorruption) as alias:
            bundle.adopt_existing_board_route("board-unbound")
        assert alias.value.details["reason"] == "graph_route_filesystem_alias_refused"

        with pytest.raises(ValueError, match="filesystem alias"):
            kg_runtime.erase_board_graph_storage_for_privacy_unguarded(
                "board-unbound",
                reason="privacy",
            )

        assert sentinel.read_bytes() == b"private-external-data"
        with pytest.raises(GraphCapabilityUnavailable) as missing:
            bundle.binding_store.inspect_board_binding("board-unbound")
        assert missing.value.details["reason"] == "binding_missing"
    finally:
        junction.rmdir()


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
async def test_routed_ladybug_rebuild_owns_a_logically_reentrant_native_writer(
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
    def unguarded_window(
        board_id: str,
        *,
        phase: str,
        drain_timeout: float = 30.0,
    ):
        assert drain_timeout == 30.0
        assert ladybug_writer.writer_lease_is_active() is True
        windows.append(f"{board_id}:{phase}")
        yield

    def ensure_under_writer(board_id: str) -> None:
        assert ladybug_writer.writer_lease_is_active() is True
        ensured.append(board_id)

    monkeypatch.setattr(kg_runtime, "board_kuzu_path", lambda _board_id: path)
    monkeypatch.setattr(
        kg_runtime,
        "board_storage_mutation_window_unguarded",
        unguarded_window,
    )
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )
    monkeypatch.setattr(
        kg_runtime,
        "ensure_board_graph_bootstrapped_unguarded",
        ensure_under_writer,
    )

    report = await bundle.graph_lifecycle.rebuild("board-rebuild")
    with ladybug_writer.ladybug_writer_scope(
        scope="outer-global-writer",
        phase="test-reentrant",
    ):
        repeated = await bundle.graph_lifecycle.rebuild("board-rebuild")

    assert report.status == "rebuilt"
    assert report.steps == (
        "close_all_connections",
        "ensure_board_graph_bootstrapped",
    )
    assert repeated.status == "rebuilt"
    assert ensured == ["board-rebuild", "board-rebuild"]
    assert windows == [
        "board-rebuild:graph_lifecycle_rebuild",
        "board-rebuild:graph_lifecycle_rebuild",
    ]


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
    fences: list[str] = []

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
        composition,
        "revalidate_board_graph_write_lease",
        lambda board_id, *, failure_phase: fences.append(f"{board_id}:{failure_phase}"),
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
    assert fences == [
        "board-purge:purge_board_graph",
        "board-purge:purge_board_graph",
        "board-purge:runtime_purge_ladybug",
        "board-recover:recover_wal_only",
        "board-recover:graph_recovery_ladybug",
    ]


@pytest.mark.asyncio
async def test_ladybug_recovery_advances_rollout_high_water_before_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "kg"
    bundle = _build(root, _GrafxConnector(), board_backend="ladybug")
    source_path = _publish_ladybug_binding(bundle, "board-recover-shadow")
    source = bundle.binding_store.acquire_board_binding("board-recover-shadow")
    journal = CommunityGraphRolloutJournal(root, "board-recover-shadow")
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
                "board-recover-shadow", "rollout-candidate-1"
            ),
            page_size=PAGE_SIZE,
        ),
    )

    @contextmanager
    def mutation_window(_board_id: str, *, phase: str):
        assert phase == "recover_wal_only"
        yield

    async def recover_unguarded(board_id: str) -> WalRecoveryReport:
        assert journal.capture_high_water() == 1
        return WalRecoveryReport(
            board_id=board_id,
            status="recovered",
            main_untouched=True,
        )

    monkeypatch.setattr(kg_runtime, "board_storage_mutation_window", mutation_window)
    monkeypatch.setattr(kg_runtime, "board_kuzu_path", lambda _board_id: source_path)
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )
    monkeypatch.setattr(
        wal_recovery.CommunityGraphRecovery,
        "recover_wal_only_unguarded",
        lambda _self, board_id: recover_unguarded(board_id),
    )

    recovered = await bundle.graph_recovery.recover_wal_only("board-recover-shadow")

    assert recovered.status == "recovered"
    assert journal.capture_high_water() == 1


@pytest.mark.asyncio
async def test_grafx_recovery_closes_rollback_fence_before_writable_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "kg"
    connector = _GrafxConnector()
    bundle = _build(root, connector, board_backend="grafx")
    board_id = "board-recover-grafx"
    path = bundle.binding_store.board_grafx_path(board_id, "grafx-1")
    database = connector(path, page_size=PAGE_SIZE)
    binding = bundle.binding_store.initialize_board_binding(
        board_id=board_id,
        backend="grafx",
        generation="grafx-1",
        physical_path=path,
        page_size=PAGE_SIZE,
        database=database,
    )
    events: list[str] = []

    @contextmanager
    def mutation_window(_board_id: str, *, phase: str):
        assert phase == "recover_wal_only"
        yield

    def close_rollback(
        _self: object,
        observed_board_id: str,
        binding_sha256: str,
        backend: str,
    ) -> None:
        assert observed_board_id == board_id
        assert binding_sha256 == binding.binding_sha256
        assert backend == "grafx"
        events.append("close_rollback")

    async def recover_grafx(_self: object, observed_board_id: str) -> WalRecoveryReport:
        assert events == ["close_rollback"]
        events.append("recover")
        return WalRecoveryReport(
            board_id=observed_board_id,
            status="recovered",
            main_untouched=False,
        )

    monkeypatch.setattr(kg_runtime, "board_storage_mutation_window", mutation_window)
    monkeypatch.setattr(
        composition,
        "revalidate_board_graph_write_lease",
        lambda _board_id, *, failure_phase: None,
    )
    monkeypatch.setattr(
        composition.CommunityGraphRolloutMutationRecorder,
        "close_rollback_before_write_if_active",
        close_rollback,
    )
    monkeypatch.setattr(
        composition.CommunityGrafxGraphRecovery,
        "recover_wal_only",
        recover_grafx,
    )

    recovered = await bundle.graph_recovery.recover_wal_only(board_id)

    assert recovered.status == "recovered"
    assert events == ["close_rollback", "recover"]
