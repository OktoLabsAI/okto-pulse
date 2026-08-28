from __future__ import annotations

import json
import shutil
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
    GraphRuntimeState,
)
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef
from okto_pulse.core.ports.global_discovery_recovery_control import (
    recovery_attempt_id,
)

from okto_pulse.community.adapters.global_discovery_layout import (
    canonical_sha256,
    generation_graph_path,
)
from okto_pulse.community.adapters.global_discovery_recovery import (
    _physical_generation_id as _ladybug_recovery_generation_id,
)
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.grafx_database_pool import (
    CommunityGrafxDatabasePool,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    _adoption_generation_id as _grafx_adoption_generation_id,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    _generation_id as _grafx_recovery_generation_id,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    GLOBAL_BINDING_FILENAME,
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteResolver,
    CommunityGraphRouteSnapshot,
)
from okto_pulse.community.adapters.routed_global_graph_composition import (
    CommunityGlobalGraphShutdown,
    CommunityGlobalGraphShutdownError,
    _GlobalPrivacyCoordinator,
    _GlobalPurgeCoordinator,
    _RotatingGrafxLease,
    _validate_authenticated_recovery_transition,
    build_community_routed_global_graph_composition,
)
from okto_pulse.community.config import PULSE_GRAFX_DEFAULT_PAGE_SIZE


def _binding_manifest(root: Path) -> Path:
    return root / "global" / GLOBAL_BINDING_FILENAME


def _snapshot(
    root: Path,
    *,
    backend: str = "ladybug",
    active_generation: str | None = None,
) -> CommunityGraphRouteSnapshot:
    anchor = (
        root / "global" / "discovery.lbug"
        if backend == "ladybug"
        else root / "global" / "grafx" / "generation-1"
    )
    active = (
        anchor
        if active_generation is None
        else anchor.parent / "discovery.generations" / active_generation / anchor.name
    )
    return CommunityGraphRouteSnapshot(
        scope="global",
        scope_id="global",
        backend=backend,  # type: ignore[arg-type]
        generation="generation-1",
        binding_path=anchor,
        anchor_path=anchor,
        active_path=active,
        page_size=4096 if backend == "grafx" else None,
        binding_sha256="a" * 64,
        route_sha256="b" * 64,
        active_generation=active_generation,
        active_manifest_sha256=("c" * 64 if active_generation else None),
    )


class _Resolver:
    def __init__(self, snapshot: CommunityGraphRouteSnapshot) -> None:
        self.snapshot = snapshot
        self.inspect_calls = 0
        self.acquire_calls = 0
        self.revalidate_calls = 0
        self.initialize_calls = 0

    def inspect_global_route(self) -> CommunityGraphRouteSnapshot:
        self.inspect_calls += 1
        return self.snapshot

    def acquire_global_route(self) -> CommunityGraphRouteSnapshot:
        self.acquire_calls += 1
        return self.snapshot

    def revalidate_snapshot(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        require_physical: bool = False,
    ) -> CommunityGraphRouteSnapshot:
        del require_physical
        self.revalidate_calls += 1
        assert snapshot == self.snapshot
        return snapshot

    def initialize_global_route(self, *, create_physical: Any = None):
        del create_physical
        self.initialize_calls += 1
        return self.snapshot

    def admit_grafx_route(self, snapshot, database, *, operation):
        del database, operation
        assert snapshot == self.snapshot


class _BindingStore:
    def __init__(self, root: Path) -> None:
        self.root = root


class _Pool:
    def __init__(self) -> None:
        self.acquire_calls = 0
        self.close_calls = 0

    def acquire(self, path: Path, *, page_size: int):
        del path, page_size
        self.acquire_calls += 1
        return _Lease(object())

    def close(self, path: Path) -> bool:
        del path
        self.close_calls += 1
        return True


class _Lease:
    def __init__(self, database: object) -> None:
        self.database = database
        self.releases = 0

    def release(self) -> bool:
        self.releases += 1
        return True


class _LadybugRuntime:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.bootstrap_calls = 0
        self.close_calls = 0

    def state(self, *, generation: str | None = None) -> GraphRuntimeState:
        return GraphRuntimeState.from_observation(
            board_id="_global",
            storage_ref=StorageRef("global-discovery", "test"),
            state=GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE,
            generation=generation,
            reason_code="test",
            observed_at=datetime.now(UTC),
        )

    def materialization_observation_paths(self) -> tuple[Path, ...]:
        return (self.path,)

    def bootstrap(self):
        self.bootstrap_calls += 1
        return object()

    def close(self) -> None:
        self.close_calls += 1

    @contextmanager
    def post_write_verification_scope(self):
        yield

    def flush_after_write_batch(self) -> None:
        pass

    def purge(self, *, reason: str = "manual"):
        del reason

    def erase_storage_for_privacy(self, **kwargs):
        return kwargs


def test_builder_retains_shared_dependency_identity_and_does_not_initialize(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    binding_path = _binding_manifest(tmp_path)
    binding_path.parent.mkdir(parents=True)
    binding_path.write_text("binding", encoding="utf-8")
    store = _BindingStore(tmp_path)
    resolver = _Resolver(snapshot)
    pool = _Pool()
    lock = threading.RLock()
    created: list[_LadybugRuntime] = []

    def runtime_factory(path: Path) -> _LadybugRuntime:
        runtime = _LadybugRuntime(path)
        created.append(runtime)
        return runtime

    bundle = build_community_routed_global_graph_composition(
        binding_store=store,  # type: ignore[arg-type]
        resolver=resolver,  # type: ignore[arg-type]
        grafx_pool=pool,  # type: ignore[arg-type]
        global_lock=lock,
        revalidate_write_fence=lambda _phase: None,
        ladybug_runtime_factory=runtime_factory,  # type: ignore[arg-type]
    )

    assert bundle.binding_store is store
    assert bundle.resolver is resolver
    assert bundle.grafx_pool is pool
    assert bundle.global_lock is lock
    assert bundle.runtime._global_lock is lock
    assert bundle.recovery._global_lock is lock
    assert resolver.inspect_calls == resolver.acquire_calls == 0
    assert pool.acquire_calls == 0
    assert created == []

    observed = bundle.runtime.state(generation="health-1")
    assert observed.generation == "health-1"
    assert resolver.inspect_calls == 1
    assert resolver.acquire_calls == 0
    assert pool.acquire_calls == 0
    assert len(created) == 1
    assert created[0].bootstrap_calls == 0


def test_grafx_operation_lease_rotates_without_leaking_a_pin(tmp_path: Path) -> None:
    class Manager:
        def __init__(self) -> None:
            self.leases: list[_Lease] = []
            self.closes = 0

        def acquire(self, path: Path, *, page_size: int) -> _Lease:
            del path, page_size
            lease = _Lease(object())
            self.leases.append(lease)
            return lease

        def close(self, path: Path) -> bool:
            del path
            self.closes += 1
            return True

    manager = Manager()
    holder = _RotatingGrafxLease(
        manager,  # type: ignore[arg-type]
        path=tmp_path / "database",
        page_size=4096,
        admit=lambda _database: None,
    )

    first = holder.database()
    assert holder.database() is first
    assert len(manager.leases) == 1

    holder.close()
    assert manager.leases[0].releases == 1
    assert manager.closes == 1

    second = holder.database()
    assert second is not first
    holder.release()
    assert manager.leases[1].releases == 1
    assert len(manager.leases) == 2


@pytest.mark.parametrize(
    ("kind", "generation_factory"),
    [
        ("grafx_global_discovery_recovery", _grafx_recovery_generation_id),
        (
            "grafx_global_discovery_recovery_adoption",
            _grafx_adoption_generation_id,
        ),
    ],
)
def test_recovery_transition_requires_exact_authenticated_attempt_manifest(
    tmp_path: Path,
    kind: str,
    generation_factory: Any,
) -> None:
    initial = _snapshot(tmp_path, backend="grafx")
    run_id = "gdr_run_1234"
    epoch = 7
    attempt_id = recovery_attempt_id(run_id, epoch)
    generation = generation_factory(
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
    )
    active = generation_graph_path(initial.anchor_path, generation)
    manifest_path = active.parent / "generation_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    body = {
        "layout_version": 1,
        "generation_id": generation,
        "kind": kind,
        "run_id": run_id,
        "epoch": epoch,
        "attempt_id": attempt_id,
    }
    manifest_sha = canonical_sha256(body)
    manifest_path.write_text(
        json.dumps({**body, "manifest_sha256": manifest_sha}),
        encoding="utf-8",
    )
    observed = replace(
        initial,
        active_path=active,
        active_generation=generation,
        active_manifest_sha256=manifest_sha,
        route_sha256="d" * 64,
    )

    assert _validate_authenticated_recovery_transition(
        initial=initial,
        previous=initial,
        observed=observed,
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
    )
    assert not _validate_authenticated_recovery_transition(
        initial=initial,
        previous=initial,
        observed=observed,
        run_id=run_id,
        epoch=epoch,
        attempt_id="forged",
    )
    forged = dict(body)
    forged["kind"] = "untrusted_cutover"
    forged_sha = canonical_sha256(forged)
    manifest_path.write_text(
        json.dumps({**forged, "manifest_sha256": forged_sha}),
        encoding="utf-8",
    )
    assert not _validate_authenticated_recovery_transition(
        initial=initial,
        previous=initial,
        observed=replace(observed, active_manifest_sha256=forged_sha),
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
    )

    forged_generation = "gdr_forged_same_attempt"
    forged_active = generation_graph_path(initial.anchor_path, forged_generation)
    forged_active.parent.mkdir(parents=True)
    forged_body = {
        **body,
        "generation_id": forged_generation,
    }
    forged_manifest_sha = canonical_sha256(forged_body)
    (forged_active.parent / "generation_manifest.json").write_text(
        json.dumps({**forged_body, "manifest_sha256": forged_manifest_sha}),
        encoding="utf-8",
    )
    assert not _validate_authenticated_recovery_transition(
        initial=initial,
        previous=initial,
        observed=replace(
            observed,
            active_path=forged_active,
            active_generation=forged_generation,
            active_manifest_sha256=forged_manifest_sha,
            route_sha256="e" * 64,
        ),
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
    )


def test_ladybug_recovery_transition_requires_deterministic_generation(
    tmp_path: Path,
) -> None:
    initial = _snapshot(tmp_path, backend="ladybug")
    run_id = "gdr_run_5678"
    epoch = 3
    attempt_id = recovery_attempt_id(run_id, epoch)
    generation = _ladybug_recovery_generation_id(
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
    )
    active = generation_graph_path(initial.anchor_path, generation)
    active.parent.mkdir(parents=True)
    body = {
        "layout_version": 1,
        "generation_id": generation,
        "run_id": run_id,
        "epoch": epoch,
        "attempt_id": attempt_id,
    }
    manifest_sha = canonical_sha256(body)
    (active.parent / "generation_manifest.json").write_text(
        json.dumps({**body, "manifest_sha256": manifest_sha}),
        encoding="utf-8",
    )
    observed = replace(
        initial,
        active_path=active,
        active_generation=generation,
        active_manifest_sha256=manifest_sha,
        route_sha256="d" * 64,
    )

    assert _validate_authenticated_recovery_transition(
        initial=initial,
        previous=initial,
        observed=observed,
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
    )

    forged_generation = "gdr_run_5678_attempt_999"
    forged_active = generation_graph_path(initial.anchor_path, forged_generation)
    forged_active.parent.mkdir(parents=True)
    forged_body = {**body, "generation_id": forged_generation}
    forged_sha = canonical_sha256(forged_body)
    (forged_active.parent / "generation_manifest.json").write_text(
        json.dumps({**forged_body, "manifest_sha256": forged_sha}),
        encoding="utf-8",
    )
    assert not _validate_authenticated_recovery_transition(
        initial=initial,
        previous=initial,
        observed=replace(
            observed,
            active_path=forged_active,
            active_generation=forged_generation,
            active_manifest_sha256=forged_sha,
            route_sha256="e" * 64,
        ),
        run_id=run_id,
        epoch=epoch,
        attempt_id=attempt_id,
    )


def test_privacy_capture_structurally_excludes_a_present_target() -> None:
    class Runtime:
        def execute(self, statement: str, params=None):
            del params
            if statement.startswith("MATCH (n:Board)"):
                rows = (
                    ("target", "Target", "secret", [1.0], 0, 0, 1, "now"),
                    ("survivor", "Survivor", "safe", [2.0], 0, 0, 1, "now"),
                )
            elif statement.startswith("MATCH (n:DecisionDigest)"):
                rows = (
                    (
                        "target-digest",
                        "target",
                        "source-target",
                        "secret",
                        "secret",
                        "Decision",
                        "canonical",
                        False,
                        [1.0],
                        "now",
                    ),
                    (
                        "survivor-digest",
                        "survivor",
                        "source-survivor",
                        "safe",
                        "safe",
                        "Decision",
                        "canonical",
                        False,
                        [2.0],
                        "now",
                    ),
                )
            elif "CONTAINS_DECISION" in statement:
                rows = (
                    ("target", "target-digest"),
                    ("survivor", "survivor-digest"),
                )
            else:
                rows = ()
            return type("Result", (), {"rows": rows})()

    captured = _GlobalPrivacyCoordinator._capture(
        Runtime(),  # type: ignore[arg-type]
        board_id="target",
        survivor_board_ids=None,
    )

    assert captured["survivor_board_ids"] == ["survivor"]
    assert [row[0] for row in captured["rows"]["boards"]] == ["survivor"]
    assert [row[0] for row in captured["rows"]["digests"]] == ["survivor-digest"]
    assert captured["rows"]["contains_decision"] == [["survivor", "survivor-digest"]]


def test_ladybug_purge_quarantines_only_selected_layout_and_preserves_binding(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(tmp_path)
    global_root = tmp_path / "global"
    global_root.mkdir(parents=True)
    binding_path = _binding_manifest(tmp_path)
    binding = b"immutable-binding"
    binding_path.write_bytes(binding)
    snapshot.anchor_path.write_bytes(b"main")
    snapshot.anchor_path.with_name(snapshot.anchor_path.name + ".wal").write_bytes(
        b"wal"
    )
    (global_root / "active_generation.json").write_text("{}", encoding="utf-8")
    generation_root = global_root / "discovery.generations" / "gdr_old_1234"
    generation_root.mkdir(parents=True)
    (generation_root / snapshot.anchor_path.name).write_bytes(b"old")
    grafx_residue = global_root / "grafx" / "other-backend"
    grafx_residue.mkdir(parents=True)
    (grafx_residue / "grafx.meta").write_text("keep", encoding="utf-8")

    resolver = _Resolver(snapshot)

    class Ladybug:
        closes = 0

        def close_snapshot(self, _snapshot) -> None:
            self.closes += 1

    class Grafx:
        def close_all(self) -> int:
            raise AssertionError("unselected Grafx must not be closed by purge")

    def quarantine(_snapshot, targets: tuple[Path, ...], _reason: str) -> int:
        moved = 0
        for target in targets:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            moved += 1
        return moved

    ladybug = Ladybug()
    purge = _GlobalPurgeCoordinator(
        binding_store=_BindingStore(tmp_path),  # type: ignore[arg-type]
        resolver=resolver,  # type: ignore[arg-type]
        ladybug=ladybug,  # type: ignore[arg-type]
        grafx=Grafx(),  # type: ignore[arg-type]
        revalidate_write_fence=lambda _phase: None,
        quarantine_targets=quarantine,
    )

    receipt = purge(snapshot, "manual")

    assert receipt.status == "purged"
    assert ladybug.closes == 1
    assert binding_path.read_bytes() == binding
    assert grafx_residue.is_dir()
    assert not snapshot.anchor_path.exists()
    assert not (global_root / "active_generation.json").exists()
    assert not (global_root / "discovery.generations").exists()


def test_real_grafx_purge_then_bootstrap_rematerializes_exact_bound_route(
    tmp_path: Path,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = CommunityGraphRouteResolver(
        store,
        board_backend="ladybug",
        global_backend="grafx",
        grafx_page_size=PULSE_GRAFX_DEFAULT_PAGE_SIZE,
    )

    def quarantine(
        _snapshot: CommunityGraphRouteSnapshot,
        targets: tuple[Path, ...],
        _reason: str,
    ) -> int:
        for target in targets:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        return len(targets)

    bundle = build_community_routed_global_graph_composition(
        binding_store=store,
        resolver=resolver,
        grafx_pool=CommunityGrafxDatabasePool(tmp_path),
        global_lock=threading.RLock(),
        revalidate_write_fence=lambda _phase: None,
        quarantine_targets=quarantine,
    )
    initialized = bundle.initialize_global_route()
    binding_path = _binding_manifest(store.root)
    binding_before = binding_path.read_bytes()

    receipt = bundle.runtime.purge(reason="rebuild-from-scratch")

    assert receipt.status == "purged"
    assert binding_path.read_bytes() == binding_before
    assert not initialized.active_path.exists()

    handle = bundle.runtime.bootstrap()
    rebound = resolver.acquire_global_route()

    assert handle.opened
    assert rebound == initialized
    assert binding_path.read_bytes() == binding_before
    assert initialized.active_path.is_dir()
    assert "Board" in bundle.runtime.list_schema_objects()
    bundle.close_all_on_shutdown()


def test_privacy_sweeps_both_layouts_restores_survivors_and_keeps_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(tmp_path)
    global_root = tmp_path / "global"
    global_root.mkdir(parents=True)
    binding_path = _binding_manifest(tmp_path)
    binding = b"authenticated-binding"
    binding_path.write_bytes(binding)
    (global_root / "graph_backend_binding.json.lock").write_text("", encoding="utf-8")
    (global_root / ".graph_route_initialization.lock").write_text("", encoding="utf-8")
    snapshot.anchor_path.write_bytes(b"ladybug")
    (global_root / "quarantine" / "global-discovery").mkdir(parents=True)
    (global_root / "quarantine" / "global-discovery" / "secret").write_bytes(b"target")
    (global_root / "grafx" / "generation-1").mkdir(parents=True)
    (global_root / "grafx" / "generation-1" / "grafx.meta").write_bytes(b"target")

    store = _BindingStore(tmp_path)
    resolver = _Resolver(snapshot)

    class Manager:
        def close_all(self) -> int:
            return 1

    class Runtime:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.flushes = 0

        def execute(self, statement: str, params=None):
            del params
            self.statements.append(statement)
            return type("Result", (), {"rows": ()})()

        def flush_after_write_batch(self) -> None:
            self.flushes += 1

    runtime = Runtime()
    coordinator = _GlobalPrivacyCoordinator(
        binding_store=store,  # type: ignore[arg-type]
        resolver=resolver,  # type: ignore[arg-type]
        ladybug=Manager(),  # type: ignore[arg-type]
        grafx=Manager(),  # type: ignore[arg-type]
        grafx_sessions=object(),  # type: ignore[arg-type]
        revalidate_write_fence=lambda _phase: None,
    )
    survivors = CommunityGlobalDiscoveryRuntime._build_privacy_survivor_snapshot(
        board_id="deleted-board",
        rows={
            "boards": [
                [
                    "survivor",
                    "Survivor",
                    "summary",
                    [0.0],
                    0,
                    0,
                    0,
                    "2026-08-28T00:00:00Z",
                ]
            ],
            "topics": [],
            "entities": [],
            "digests": [],
            "has_topic": [],
            "mentions_entity": [],
            "contains_decision": [],
            "decision_mentions_entity": [],
            "decision_derives_from": [],
        },
        survivor_board_ids={"survivor"},
    )
    journal = tmp_path / ".global-privacy-survivors-test.json"
    journal.write_text(json.dumps(survivors), encoding="utf-8")
    monkeypatch.setattr(
        coordinator,
        "_durable_survivors",
        lambda *_args, **_kwargs: (survivors, journal),
    )

    @contextmanager
    def fresh(_snapshot):
        yield runtime

    monkeypatch.setattr(coordinator, "_fresh_bound_runtime", fresh)
    monkeypatch.setattr(
        coordinator,
        "_capture",
        lambda *_args, **_kwargs: survivors,
    )

    receipt = coordinator(
        snapshot,
        "deleted-board",
        "privacy",
        ("survivor",),
    )

    assert receipt["status"] == "purged"
    assert binding_path.read_bytes() == binding
    assert (global_root / "graph_backend_binding.json.lock").exists()
    assert (global_root / ".graph_route_initialization.lock").exists()
    assert not snapshot.anchor_path.exists()
    assert not (global_root / "grafx").exists()
    assert not (global_root / "quarantine").exists()
    assert not journal.exists()
    assert any(
        statement.startswith("CREATE (n:Board") for statement in runtime.statements
    )
    assert runtime.flushes == 1


@pytest.mark.parametrize("inside_verification_scope", [False, True])
def test_real_grafx_privacy_handles_present_target_and_dual_layout(
    tmp_path: Path,
    inside_verification_scope: bool,
) -> None:
    store = CommunityGraphBackendBindingStore(tmp_path)
    resolver = CommunityGraphRouteResolver(
        store,
        board_backend="ladybug",
        global_backend="grafx",
        grafx_page_size=PULSE_GRAFX_DEFAULT_PAGE_SIZE,
    )
    pool = CommunityGrafxDatabasePool(tmp_path)
    bundle = build_community_routed_global_graph_composition(
        binding_store=store,
        resolver=resolver,
        grafx_pool=pool,
        global_lock=threading.RLock(),
        revalidate_write_fence=lambda _phase: None,
    )
    vector = [1.0, *([0.0] * 383)]

    initialized = bundle.initialize_global_route()
    assert initialized.backend == "grafx"
    for board_id in ("target", "survivor"):
        bundle.runtime.upsert_board_summary(
            board_id=board_id,
            name=board_id.title(),
            summary=f"{board_id} summary",
            summary_embedding=vector,
            decision_count=1,
            synced_at="2026-08-28T00:00:00Z",
        )
        digest_id = f"{board_id}-digest"
        bundle.runtime.upsert_decision_digest(
            digest_id=digest_id,
            board_id=board_id,
            original_node_id=f"{board_id}-source",
            title=board_id.title(),
            summary=f"{board_id} digest",
            node_type="Decision",
            graph_layer="canonical",
            embedding=vector,
            created_at="2026-08-28T00:00:00Z",
        )
        bundle.runtime.link_board_digest(board_id=board_id, digest_id=digest_id)

    # An unselected legacy residue contains target bytes too and must be swept
    # by privacy without influencing route selection.
    legacy_residue = store.global_ladybug_path()
    legacy_residue.write_bytes(b"target-private-residue")
    binding_path = _binding_manifest(store.root)
    binding_before = binding_path.read_bytes()

    scope = (
        bundle.runtime.post_write_verification_scope()
        if inside_verification_scope
        else nullcontext()
    )
    with scope:
        receipt = bundle.runtime.erase_storage_for_privacy(
            board_id="target",
            reason="privacy-test",
            survivor_board_ids=("survivor",),
        )

    assert receipt["status"] == "purged"
    assert binding_path.read_bytes() == binding_before
    assert not legacy_residue.exists()
    assert bundle.runtime.execute(
        "MATCH (b:Board {board_id: $board_id}) RETURN count(b)",
        {"board_id": "target"},
    ).rows == ((0,),)
    assert bundle.runtime.execute(
        "MATCH (b:Board {board_id: $board_id}) RETURN count(b)",
        {"board_id": "survivor"},
    ).rows == ((1,),)
    assert bundle.runtime.execute(
        "MATCH (d:DecisionDigest) WHERE d.board_id = $board_id RETURN count(d)",
        {"board_id": "survivor"},
    ).rows == ((1,),)
    assert bundle.close_all_on_shutdown()["grafx_closed"] >= 1


def test_shutdown_attempts_ladybug_and_grafx_before_reporting_failure() -> None:
    calls: list[str] = []

    class Ladybug:
        def close_all(self) -> int:
            calls.append("ladybug")
            raise RuntimeError("ladybug-close")

    class Grafx:
        def close_all(self) -> int:
            calls.append("grafx")
            return 2

    lock = threading.RLock()
    shutdown = CommunityGlobalGraphShutdown(
        ladybug=Ladybug(),  # type: ignore[arg-type]
        grafx=Grafx(),  # type: ignore[arg-type]
        global_lock=lock,
    )

    with pytest.raises(CommunityGlobalGraphShutdownError):
        shutdown()

    assert calls == ["ladybug", "grafx"]
