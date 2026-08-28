"""Concrete composition for the backend-routed Global Discovery graph.

This module is deliberately separate from the application composition root.  It
receives the process-wide graph routing dependencies that are shared with the
Board graph and builds the Global runtime/recovery leaves around those exact
objects.  Nothing in this module consults settings after composition and no read
or constructor initializes a route.

There are three distinct authorities here:

* Core's Global writer lease is only *revalidated* by runtime operations;
* Ladybug's process writer is still acquired by the Ladybug physical runtime;
* ``global_lock`` serializes runtime, recovery, privacy and shutdown within this
  process and is the same injected ``threading.RLock`` everywhere.

Grafx runtime sessions retain one pool lease for their complete scope.  A
checkpoint close/reopen explicitly releases, closes and reacquires that lease,
so the session never exposes a closed pooled handle and never leaves a pin
behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
)
from okto_pulse.core.kg.interfaces.graph_runtime_store import GraphPurgeResult

from okto_pulse.community.adapters.cypher_statement_policy import (
    statement_is_write,
)
from okto_pulse.community.adapters.filesystem_erasure import (
    fsync_directory,
    is_filesystem_alias,
    reject_filesystem_alias_ancestry,
    remove_contained_tree,
    validate_scope_id,
)
from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
    bootstrap_marker_path,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    GENERATION_MANIFEST_FILENAME,
    active_pointer_path,
    canonical_sha256,
    generation_graph_path,
    generations_root,
)
from okto_pulse.community.adapters.global_discovery_recovery import (
    CommunityGlobalDiscoveryRecovery,
)
from okto_pulse.community.adapters.global_discovery_recovery import (
    _physical_generation_id as _ladybug_recovery_generation_id,
)
from okto_pulse.community.adapters.global_discovery_runtime import (
    CommunityGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.grafx_database_pool import (
    CommunityGrafxDatabasePool,
    GrafxDatabaseLease,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    CommunityGrafxGlobalDiscoveryRecovery,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    _adoption_generation_id as _grafx_adoption_generation_id,
)
from okto_pulse.community.adapters.grafx_global_discovery_recovery import (
    _generation_id as _grafx_recovery_generation_id,
)
from okto_pulse.community.adapters.grafx_global_discovery_runtime import (
    CommunityGrafxGlobalDiscoveryRuntime,
)
from okto_pulse.community.adapters.grafx_global_operational import (
    global_layout_targets,
    has_grafx_identity,
    require_global_grafx_admission,
    validate_plain_global_artifact,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    GLOBAL_BINDING_FILENAME,
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteCandidate,
    CommunityGraphRouteResolver,
    CommunityGraphRouteSnapshot,
)
from okto_pulse.community.adapters.routed_global_discovery import (
    CommunityGlobalDiscoveryRuntimeOperationSession,
    CommunityRoutedGlobalDiscoveryRecovery,
    CommunityRoutedGlobalDiscoveryRuntime,
)

_GLOBAL_SCOPE_ID = "global"
_GLOBAL_BOARD_ID = "_global"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_PRIVACY_JOURNAL_BYTES = 256 * 1024 * 1024
_PRESERVED_GLOBAL_CONTROL_FILES = frozenset(
    {
        GLOBAL_BINDING_FILENAME,
        f"{GLOBAL_BINDING_FILENAME}.lock",
        ".graph_route_initialization.lock",
    }
)


class _GlobalLock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> object: ...


class _RuntimeLike(Protocol):
    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any: ...

    def bootstrap(self) -> Any: ...

    def flush_after_write_batch(self) -> None: ...


RuntimeFactory = Callable[[Path], CommunityGlobalDiscoveryRuntime]
GrafxConnect = Callable[..., Any]
FenceRevalidator = Callable[[str], None]
QuarantineTargets = Callable[
    [CommunityGraphRouteSnapshot, tuple[Path, ...], str],
    int,
]
StandaloneGlobalPurge = Callable[
    [CommunityGraphRouteSnapshot, str],
    GraphPurgeResult,
]
StandaloneGlobalPrivacy = Callable[
    [CommunityGraphRouteSnapshot, str, str, tuple[str, ...] | None],
    dict[str, object],
]


def _default_fence_revalidator(_phase: str) -> None:
    from okto_pulse.core.ports.global_discovery_recovery_control import (
        assert_global_discovery_writer_fence,
    )

    assert_global_discovery_writer_fence()


def _canonical_path(path: Path) -> str:
    return os.path.normcase(str(Path(os.path.abspath(path))))


def _same_path(left: Path, right: Path) -> bool:
    return _canonical_path(left) == _canonical_path(right)


def _binding_bytes(path: Path) -> bytes:
    reject_filesystem_alias_ancestry(path.parent)
    metadata = path.lstat()
    if is_filesystem_alias(path) or not stat.S_ISREG(metadata.st_mode):
        raise GraphCorruption(
            "The Global graph binding is not a plain file.",
            details={
                "operation": "global_graph_administration",
                "reason": "global_binding_file_unsafe",
            },
        )
    encoded = path.read_bytes()
    if path.lstat() != metadata:
        raise GraphCorruption(
            "The Global graph binding changed while it was read.",
            details={
                "operation": "global_graph_administration",
                "reason": "global_binding_changed_during_read",
            },
        )
    return encoded


def _require_binding_unchanged(
    path: Path,
    expected: bytes,
) -> None:
    if _binding_bytes(path) != expected:
        raise GraphCorruption(
            "The Global graph binding changed during administration.",
            details={
                "operation": "global_graph_administration",
                "reason": "global_binding_changed",
            },
        )


class _GlobalAdministrationBinding:
    """Late-bind the one durable administration path into operation sessions.

    A post-write verification session retains the selected physical handle.
    Purge/privacy must first release that pin, then use the same standalone
    coordinator as an ordinary call.  This binding breaks the construction
    cycle without creating a fallback to either backend leaf.
    """

    def __init__(self) -> None:
        self._purge: StandaloneGlobalPurge | None = None
        self._privacy: StandaloneGlobalPrivacy | None = None

    def bind(
        self,
        *,
        purge: StandaloneGlobalPurge,
        privacy: StandaloneGlobalPrivacy,
    ) -> None:
        if self._purge is not None or self._privacy is not None:
            raise RuntimeError("global_graph_administration_already_bound")
        self._purge = purge
        self._privacy = privacy

    def purge(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        close_active: Callable[[], None],
        reason: str,
    ) -> GraphPurgeResult:
        purge = self._purge
        if purge is None:
            raise RuntimeError("global_graph_administration_unbound")
        close_active()
        return purge(snapshot, reason)

    def privacy(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        close_active: Callable[[], None],
        board_id: str,
        reason: str,
        survivor_board_ids: tuple[str, ...] | None,
    ) -> dict[str, object]:
        privacy = self._privacy
        if privacy is None:
            raise RuntimeError("global_graph_administration_unbound")
        close_active()
        return privacy(snapshot, board_id, reason, survivor_board_ids)


class _LadybugGlobalRuntimeManager:
    """Retain the one persistent Ladybug Global runtime without opening on read."""

    def __init__(self, runtime_factory: RuntimeFactory | None = None) -> None:
        self._runtime_factory = runtime_factory or (
            lambda path: CommunityGlobalDiscoveryRuntime(
                graph_path_provider=lambda: path,
            )
        )
        self._runtimes: dict[str, CommunityGlobalDiscoveryRuntime] = {}

    def runtime_for(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> CommunityGlobalDiscoveryRuntime:
        key = _canonical_path(snapshot.anchor_path)
        runtime = self._runtimes.get(key)
        if runtime is None:
            runtime = self._runtime_factory(snapshot.anchor_path)
            self._runtimes[key] = runtime
        return runtime

    def runtime_for_path(self, path: Path) -> CommunityGlobalDiscoveryRuntime:
        key = _canonical_path(path)
        runtime = self._runtimes.get(key)
        if runtime is None:
            runtime = self._runtime_factory(path)
            self._runtimes[key] = runtime
        return runtime

    def close_snapshot(self, snapshot: CommunityGraphRouteSnapshot) -> None:
        runtime = self._runtimes.get(_canonical_path(snapshot.anchor_path))
        if runtime is not None:
            runtime.close()

    def close_all(self) -> int:
        failures: list[BaseException] = []
        closed = 0
        for runtime in tuple(self._runtimes.values()):
            try:
                runtime.close()
            except Exception as failure:  # noqa: BLE001 - attempt every handle
                failures.append(failure)
            else:
                closed += 1
        if failures:
            raise CommunityGlobalGraphShutdownError(
                ladybug_failures=tuple(failures),
            )
        return closed


class _GrafxGlobalPoolManager:
    """Track only Global paths inside the process-wide Board/Global pool."""

    def __init__(self, pool: CommunityGrafxDatabasePool) -> None:
        self.pool = pool
        self._paths: dict[str, Path] = {}

    def acquire(self, path: Path, *, page_size: int) -> GrafxDatabaseLease:
        lease = self.pool.acquire(path, page_size=page_size)
        self._paths[_canonical_path(path)] = Path(path)
        return lease

    def close(self, path: Path) -> bool:
        key = _canonical_path(path)
        closed = self.pool.close(path)
        if closed or key in self._paths:
            self._paths.pop(key, None)
        return closed

    def close_all(self) -> int:
        failures: list[tuple[Path, BaseException]] = []
        closed = 0
        for key, path in tuple(self._paths.items()):
            try:
                did_close = self.pool.close(path)
            except Exception as failure:  # noqa: BLE001 - attempt every handle
                failures.append((path, failure))
            else:
                self._paths.pop(key, None)
                closed += int(did_close)
        if failures:
            raise CommunityGlobalGraphShutdownError(
                grafx_failures=tuple(failures),
            )
        return closed

    @property
    def tracked_paths(self) -> tuple[Path, ...]:
        return tuple(self._paths[key] for key in sorted(self._paths))


class _RotatingGrafxLease:
    """One operation pin that can be safely rotated by close/reopen."""

    def __init__(
        self,
        manager: _GrafxGlobalPoolManager,
        *,
        path: Path,
        page_size: int,
        admit: Callable[[Any], None],
    ) -> None:
        self._manager = manager
        self._path = Path(path)
        self._page_size = page_size
        self._admit = admit
        self._lease: GrafxDatabaseLease | None = None

    def database(self) -> Any:
        lease = self._lease
        if lease is None:
            lease = self._manager.acquire(
                self._path,
                page_size=self._page_size,
            )
            try:
                self._admit(lease.database)
            except BaseException:
                lease.release()
                self._lease = None
                raise
            self._lease = lease
        return lease.database

    def release(self) -> None:
        lease = self._lease
        self._lease = None
        if lease is not None:
            lease.release()

    def close(self) -> None:
        self.release()
        self._manager.close(self._path)


class _GrafxRuntimeSessionFactory:
    def __init__(
        self,
        *,
        resolver: CommunityGraphRouteResolver,
        pool_manager: _GrafxGlobalPoolManager,
        revalidate_write_fence: FenceRevalidator,
        administration: _GlobalAdministrationBinding,
    ) -> None:
        self._resolver = resolver
        self._pool_manager = pool_manager
        self._revalidate_write_fence = revalidate_write_fence
        self._administration = administration

    @contextmanager
    def __call__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> Iterator[CommunityGlobalDiscoveryRuntimeOperationSession]:
        if snapshot.page_size is None:
            raise GraphCapabilityUnavailable(
                "The Grafx Global route has no persisted page size.",
                details={
                    "operation": "compose_grafx_global_runtime",
                    "reason": "grafx_route_page_size_missing",
                },
            )

        def admit(database: Any) -> None:
            self._resolver.admit_grafx_route(
                snapshot,
                database,
                operation="compose_grafx_global_runtime",
            )

        holder = _RotatingGrafxLease(
            self._pool_manager,
            path=snapshot.active_path,
            page_size=snapshot.page_size,
            admit=admit,
        )

        def fence(phase: str) -> None:
            self._revalidate_write_fence(phase)
            self._resolver.revalidate_snapshot(snapshot, require_physical=True)

        # Pin before exposing the session.  Even operations that happen not to
        # touch the handle still retain the exact backend/generation contract.
        holder.database()
        runtime = CommunityGrafxGlobalDiscoveryRuntime(
            holder.database,
            lambda: snapshot.anchor_path,
            holder.close,
            fence,
            admission=admit,
        )
        try:
            yield CommunityGlobalDiscoveryRuntimeOperationSession(
                runtime=runtime,
                post_write_verification_scope_unguarded=(
                    runtime.post_write_verification_scope
                ),
                flush_after_write_batch_unguarded=runtime.flush_after_write_batch,
                close_unguarded=runtime.close,
                purge_unguarded=lambda reason: self._administration.purge(
                    snapshot,
                    close_active=runtime.close,
                    reason=reason,
                ),
                erase_storage_for_privacy_unguarded=lambda board_id, reason, survivors: (
                    self._administration.privacy(
                        snapshot,
                        close_active=runtime.close,
                        board_id=board_id,
                        reason=reason,
                        survivor_board_ids=survivors,
                    )
                ),
            )
        finally:
            holder.release()


class _LadybugRuntimeSessionFactory:
    def __init__(
        self,
        manager: _LadybugGlobalRuntimeManager,
        administration: _GlobalAdministrationBinding,
    ) -> None:
        self._manager = manager
        self._administration = administration

    @contextmanager
    def __call__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> Iterator[CommunityGlobalDiscoveryRuntimeOperationSession]:
        runtime = self._manager.runtime_for(snapshot)
        yield CommunityGlobalDiscoveryRuntimeOperationSession(
            runtime=runtime,
            # These callbacks are unguarded with respect to the routed Global
            # lock/Core lease.  Ladybug intentionally still takes its own
            # process-wide physical writer and lifecycle gate.
            post_write_verification_scope_unguarded=(
                runtime.post_write_verification_scope
            ),
            flush_after_write_batch_unguarded=runtime.flush_after_write_batch,
            close_unguarded=runtime.close,
            purge_unguarded=lambda reason: self._administration.purge(
                snapshot,
                close_active=runtime.close,
                reason=reason,
            ),
            erase_storage_for_privacy_unguarded=lambda board_id, reason, survivors: (
                self._administration.privacy(
                    snapshot,
                    close_active=runtime.close,
                    board_id=board_id,
                    reason=reason,
                    survivor_board_ids=survivors,
                )
            ),
        )


class _SnapshotFingerprintBinding:
    def __init__(self) -> None:
        self._provider: Callable[[], str] | None = None

    def bind(self, provider: Callable[[], str]) -> None:
        if not callable(provider):
            raise TypeError("snapshot fingerprint provider must be callable")
        if self._provider is not None and self._provider is not provider:
            raise RuntimeError("global_discovery_snapshot_fingerprint_already_bound")
        self._provider = provider

    def current(self) -> str:
        provider = self._provider
        if provider is None:
            raise RuntimeError("global_discovery_snapshot_fingerprint_unavailable")
        value = str(provider()).strip()
        if not value:
            raise RuntimeError("global_discovery_snapshot_fingerprint_unavailable")
        return value


class _LadybugRecoveryFactory:
    def __init__(
        self,
        manager: _LadybugGlobalRuntimeManager,
        fingerprint: _SnapshotFingerprintBinding,
    ) -> None:
        self._manager = manager
        self._fingerprint = fingerprint

    def __call__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> CommunityGlobalDiscoveryRecovery:
        return CommunityGlobalDiscoveryRecovery(
            global_runtime=self._manager.runtime_for(snapshot),
            graph_path_provider=lambda: snapshot.anchor_path,
            snapshot_fingerprint_provider=self._fingerprint.current,
        )


class _GrafxRecoveryFactory:
    def __init__(
        self,
        *,
        resolver: CommunityGraphRouteResolver,
        pool_manager: _GrafxGlobalPoolManager,
        fingerprint: _SnapshotFingerprintBinding,
        connect: GrafxConnect | None,
    ) -> None:
        self._resolver = resolver
        self._pool_manager = pool_manager
        self._fingerprint = fingerprint
        self._connect = connect

    def __call__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> CommunityGrafxGlobalDiscoveryRecovery:
        if snapshot.page_size is None:
            raise GraphCapabilityUnavailable(
                "The Grafx recovery route has no persisted page size.",
                details={
                    "operation": "compose_grafx_global_recovery",
                    "reason": "grafx_route_page_size_missing",
                },
            )

        def connect(path: Path) -> Any:
            opener = self._connect
            if opener is None:
                import okto_grafx

                opener = okto_grafx.connect
            return opener(path, page_size=snapshot.page_size)

        def binding_fence(_phase: str) -> None:
            observed = self._resolver.inspect_global_route()
            if not _immutable_binding_matches(snapshot, observed):
                raise GraphCapabilityUnavailable(
                    "The Global recovery binding changed.",
                    details={
                        "operation": "compose_grafx_global_recovery",
                        "reason": "recovery_binding_changed",
                    },
                )

        def close_live() -> None:
            # Recovery owns the shared Global lock, so no operation session can
            # retain this pin while the active pointer is switched.
            self._pool_manager.close(snapshot.active_path)

        return CommunityGrafxGlobalDiscoveryRecovery(
            lambda: snapshot.anchor_path,
            connect,
            close_live,
            binding_fence,
            admission=require_global_grafx_admission,
            snapshot_fingerprint_provider=self._fingerprint.current,
        )


def _immutable_binding_matches(
    initial: CommunityGraphRouteSnapshot,
    observed: CommunityGraphRouteSnapshot,
) -> bool:
    return (
        observed.scope == initial.scope
        and observed.scope_id == initial.scope_id
        and observed.backend == initial.backend
        and observed.generation == initial.generation
        and _same_path(observed.binding_path, initial.binding_path)
        and _same_path(observed.anchor_path, initial.anchor_path)
        and observed.page_size == initial.page_size
        and observed.binding_sha256 == initial.binding_sha256
    )


def _strict_json_object(
    path: Path,
    *,
    max_bytes: int = _MAX_MANIFEST_BYTES,
) -> dict[str, object]:
    reject_filesystem_alias_ancestry(path.parent)
    metadata = path.lstat()
    if (
        is_filesystem_alias(path)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > max_bytes
    ):
        raise ValueError("recovery_manifest_unsafe")
    encoded = path.read_bytes()
    if len(encoded) > max_bytes or path.lstat() != metadata:
        raise ValueError("recovery_manifest_changed")

    def strict(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("recovery_manifest_duplicate_key")
            result[key] = value
        return result

    document = json.loads(encoded.decode("utf-8"), object_pairs_hook=strict)
    if type(document) is not dict:
        raise ValueError("recovery_manifest_not_object")
    return document


def _validate_authenticated_recovery_transition(
    *,
    initial: CommunityGraphRouteSnapshot,
    previous: CommunityGraphRouteSnapshot,
    observed: CommunityGraphRouteSnapshot,
    run_id: str,
    epoch: int,
    attempt_id: str,
) -> bool:
    """Accept only the manifest/pointer written for this exact worker attempt."""

    # One recovery invocation owns at most one active-pointer transition.  Once
    # a new snapshot was accepted, neither a rollback to ``initial`` nor a
    # second generation may become current under the same operation.
    if previous != initial or not _immutable_binding_matches(initial, observed):
        return False
    if (
        observed.active_generation is None
        or observed.active_manifest_sha256 is None
        or observed.active_path.parent.name != observed.active_generation
    ):
        return False
    manifest_path = observed.active_path.parent / GENERATION_MANIFEST_FILENAME
    try:
        document = _strict_json_object(manifest_path)
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    supplied_sha = document.get("manifest_sha256")
    body = {key: value for key, value in document.items() if key != "manifest_sha256"}
    if (
        supplied_sha != canonical_sha256(body)
        or supplied_sha != observed.active_manifest_sha256
        or document.get("generation_id") != observed.active_generation
        or document.get("run_id") != run_id
        or type(document.get("epoch")) is not int
        or document.get("epoch") != epoch
        or document.get("attempt_id") != attempt_id
    ):
        return False
    kind = document.get("kind")
    try:
        if initial.backend == "grafx":
            if kind == "grafx_global_discovery_recovery":
                expected_generation = _grafx_recovery_generation_id(
                    run_id=run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                )
            elif kind == "grafx_global_discovery_recovery_adoption":
                expected_generation = _grafx_adoption_generation_id(
                    run_id=run_id,
                    epoch=epoch,
                    attempt_id=attempt_id,
                )
            else:
                return False
        else:
            # Ladybug's durable generation manifest predates the explicit
            # ``kind`` field.  Refuse a Grafx/ad-hoc vocabulary there.
            if kind is not None:
                return False
            expected_generation = _ladybug_recovery_generation_id(
                run_id=run_id,
                epoch=epoch,
                attempt_id=attempt_id,
            )
    except (TypeError, ValueError):
        return False
    return observed.active_generation == expected_generation and _same_path(
        observed.active_path,
        generation_graph_path(initial.anchor_path, expected_generation),
    )


class _ComposedRoutedGlobalDiscoveryRecovery(CommunityRoutedGlobalDiscoveryRecovery):
    def __init__(
        self,
        *args: Any,
        fingerprint_binding: _SnapshotFingerprintBinding,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._fingerprint_binding = fingerprint_binding

    def bind_snapshot_fingerprint_provider(
        self,
        provider: Callable[[], str],
    ) -> None:
        self._fingerprint_binding.bind(provider)


def _grafx_state(
    snapshot: CommunityGraphRouteSnapshot,
    generation: str | None,
) -> Any:
    def no_database() -> Any:
        raise AssertionError("Grafx state must not open a database")

    runtime = CommunityGrafxGlobalDiscoveryRuntime(
        no_database,
        lambda: snapshot.anchor_path,
        lambda: None,
        lambda _phase: None,
    )
    return runtime.state(generation=generation)


def _grafx_materialization_paths(
    snapshot: CommunityGraphRouteSnapshot,
) -> tuple[Path, ...]:
    # Metadata only.  Do not enumerate or resolve the active pointer here.
    return (
        snapshot.anchor_path,
        active_pointer_path(snapshot.anchor_path),
        generations_root(snapshot.anchor_path),
    )


class _GlobalPurgeCoordinator:
    def __init__(
        self,
        *,
        binding_store: CommunityGraphBackendBindingStore,
        resolver: CommunityGraphRouteResolver,
        ladybug: _LadybugGlobalRuntimeManager,
        grafx: _GrafxGlobalPoolManager,
        revalidate_write_fence: FenceRevalidator,
        quarantine_targets: QuarantineTargets | None,
    ) -> None:
        self._binding_path = binding_store.root / "global" / GLOBAL_BINDING_FILENAME
        self._storage_root = binding_store.root
        self._resolver = resolver
        self._ladybug = ladybug
        self._grafx = grafx
        self._revalidate_write_fence = revalidate_write_fence
        self._quarantine_targets = quarantine_targets or self._default_quarantine

    def _default_quarantine(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        targets: tuple[Path, ...],
        reason: str,
    ) -> int:
        from okto_pulse.core.kg.quarantine import KGQuarantineService

        from okto_pulse.community.adapters.local_storage_ref import local_storage_ref

        service = KGQuarantineService(
            base_storage_ref_hint=local_storage_ref(self._storage_root),
            scope_storage_refs=[local_storage_ref(self._storage_root / "global")],
        )
        response = service.create(
            board_id=_GLOBAL_BOARD_ID,
            graph_type="global_discovery",
            affected_storage_refs=[local_storage_ref(target) for target in targets],
            reason=reason,
            correlation_ids=[],
        )
        return response.files_moved

    def _targets(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> tuple[Path, ...]:
        targets = list(global_layout_targets(snapshot.anchor_path))
        if snapshot.backend == "ladybug":
            marker = bootstrap_marker_path(snapshot.anchor_path)
            try:
                marker.lstat()
            except FileNotFoundError:
                pass
            else:
                targets.append(marker)
        unique: list[Path] = []
        for target in targets:
            if _same_path(target, self._binding_path):
                raise GraphCorruption(
                    "The purge target overlaps the immutable binding.",
                    details={
                        "operation": "purge_global_discovery",
                        "reason": "purge_target_overlaps_binding",
                    },
                )
            if target not in unique:
                validate_plain_global_artifact(target)
                unique.append(target)
        return tuple(unique)

    def __call__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        reason: str,
    ) -> GraphPurgeResult:
        binding = _binding_bytes(self._binding_path)
        try:
            self._revalidate_write_fence("purge_global_discovery")
            self._resolver.revalidate_snapshot(snapshot, require_physical=False)
            if snapshot.backend == "ladybug":
                self._ladybug.close_snapshot(snapshot)
            else:
                self._grafx.close_all()
            targets = self._targets(snapshot)
            if not targets:
                return GraphPurgeResult(
                    board_id=_GLOBAL_BOARD_ID,
                    removed=False,
                    not_found=True,
                    status="not_found",
                    reason=reason,
                    backend=snapshot.backend,
                )
            self._revalidate_write_fence("purge_global_discovery")
            _require_binding_unchanged(self._binding_path, binding)
            moved = self._quarantine_targets(snapshot, targets, reason)
            _require_binding_unchanged(self._binding_path, binding)
            remaining = tuple(target for target in targets if target.exists())
            if moved <= 0 or remaining:
                return GraphPurgeResult(
                    board_id=_GLOBAL_BOARD_ID,
                    removed=False,
                    not_found=False,
                    status="failed",
                    reason=reason,
                    backend=snapshot.backend,
                    error_code="purge_absence_unverified",
                )
            return GraphPurgeResult(
                board_id=_GLOBAL_BOARD_ID,
                removed=True,
                not_found=False,
                status="purged",
                reason=reason,
                backend=snapshot.backend,
            )
        except Exception as failure:  # noqa: BLE001 - purge returns a receipt
            return GraphPurgeResult(
                board_id=_GLOBAL_BOARD_ID,
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend=snapshot.backend,
                error_code=str(getattr(failure, "code", None) or "graph_error"),
            )


_PRIVACY_STATEMENTS: Mapping[str, str] = {
    "boards": (
        "MATCH (n:Board) RETURN n.board_id, n.name, n.summary, "
        "n.summary_embedding, n.topic_count, n.entity_count, "
        "n.decision_count, n.last_sync_at"
    ),
    "digests": (
        "MATCH (n:DecisionDigest) RETURN n.id, n.board_id, "
        "n.original_node_id, n.title, n.one_line_summary, n.node_type, "
        "n.graph_layer, coalesce(n.source_revoked, false), "
        "n.embedding, n.created_at"
    ),
    "topics": "MATCH (b:Board)-[:HAS_TOPIC]->(n:Topic) RETURN DISTINCT n.id",
    "entities": ("MATCH (b:Board)-[:MENTIONS_ENTITY]->(n:Entity) RETURN DISTINCT n.id"),
    "decision_entities": (
        "MATCH (d:DecisionDigest)-[:DECISION_MENTIONS_ENTITY]->"
        "(n:Entity) RETURN DISTINCT n.id"
    ),
    "has_topic": "MATCH (a:Board)-[:HAS_TOPIC]->(b:Topic) RETURN a.board_id, b.id",
    "mentions_entity": (
        "MATCH (a:Board)-[:MENTIONS_ENTITY]->(b:Entity) RETURN a.board_id, b.id"
    ),
    "contains_decision": (
        "MATCH (a:Board)-[:CONTAINS_DECISION]->(b:DecisionDigest) "
        "RETURN a.board_id, b.id"
    ),
    "decision_mentions_entity": (
        "MATCH (a:DecisionDigest)-[:DECISION_MENTIONS_ENTITY]->"
        "(b:Entity) RETURN a.id, b.id"
    ),
    "decision_derives_from": (
        "MATCH (a:DecisionDigest)-[:DECISION_DERIVES_FROM]->"
        "(b:DecisionDigest) RETURN a.id, b.id"
    ),
}


class _PrivacyRuntimeFacade:
    """Give Grafx the existing backend-neutral survivor restore helpers."""

    def __init__(self, runtime: _RuntimeLike) -> None:
        self._runtime = runtime

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return self._runtime.execute(statement, params)

    _timestamp_expression = staticmethod(
        CommunityGlobalDiscoveryRuntime._timestamp_expression
    )


class _GlobalPrivacyCoordinator:
    def __init__(
        self,
        *,
        binding_store: CommunityGraphBackendBindingStore,
        resolver: CommunityGraphRouteResolver,
        ladybug: _LadybugGlobalRuntimeManager,
        grafx: _GrafxGlobalPoolManager,
        grafx_sessions: _GrafxRuntimeSessionFactory,
        revalidate_write_fence: FenceRevalidator,
    ) -> None:
        self._binding_store = binding_store
        self._binding_path = binding_store.root / "global" / GLOBAL_BINDING_FILENAME
        self._resolver = resolver
        self._ladybug = ladybug
        self._grafx = grafx
        self._grafx_sessions = grafx_sessions
        self._revalidate_write_fence = revalidate_write_fence

    @staticmethod
    def _journal_path(root: Path, board_id: str) -> Path:
        suffix = hashlib.sha256(board_id.encode("utf-8")).hexdigest()[:24]
        return root / f".global-privacy-survivors-{suffix}.json"

    @staticmethod
    def _physical_exists(snapshot: CommunityGraphRouteSnapshot) -> bool:
        if snapshot.backend == "grafx":
            return has_grafx_identity(snapshot.active_path)
        try:
            return snapshot.active_path.is_file()
        except OSError:
            return False

    @contextmanager
    def _runtime(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> Iterator[_RuntimeLike]:
        if snapshot.backend == "ladybug":
            yield self._ladybug.runtime_for(snapshot)
            return
        with self._grafx_sessions(snapshot) as session:
            yield session.runtime

    @staticmethod
    def _capture(
        runtime: _RuntimeLike,
        *,
        board_id: str,
        survivor_board_ids: tuple[str, ...] | None,
    ) -> dict[str, Any]:
        # Capture the complete live topology first.  The canonical projection
        # below removes the target Board, every target-owned digest and every
        # incident relationship structurally.  Requiring an earlier logical
        # cascade would make physical privacy dependent on call ordering and
        # would refuse the exact administrative case this capability owns.
        rows: dict[str, list[list[Any]]] = {}
        normalize = CommunityGlobalDiscoveryRuntime._privacy_snapshot_value
        for name, statement in _PRIVACY_STATEMENTS.items():
            result = runtime.execute(statement)
            rows[name] = [[normalize(value) for value in row] for row in result.rows]
        rows["entities"].extend(rows.pop("decision_entities"))
        authority = (
            set(survivor_board_ids)
            if survivor_board_ids is not None
            else {
                str(row[0]) for row in rows["boards"] if row and str(row[0]) != board_id
            }
        )
        authority.discard(board_id)
        return CommunityGlobalDiscoveryRuntime._build_privacy_survivor_snapshot(
            board_id=board_id,
            rows=rows,
            survivor_board_ids=authority,
        )

    @staticmethod
    def _write_journal(path: Path, snapshot: dict[str, Any]) -> None:
        from okto_pulse.community.adapters.global_discovery_layout import (
            write_json_atomic,
        )

        write_json_atomic(path, snapshot)

    @staticmethod
    def _load_journal(path: Path, *, board_id: str) -> dict[str, Any] | None:
        try:
            document = _strict_json_object(
                path,
                max_bytes=_MAX_PRIVACY_JOURNAL_BYTES,
            )
        except FileNotFoundError:
            return None
        CommunityGlobalDiscoveryRuntime._validate_privacy_survivor_snapshot(
            document,
            board_id=board_id,
        )
        return document

    def _durable_survivors(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        board_id: str,
        survivor_board_ids: tuple[str, ...] | None,
    ) -> tuple[dict[str, Any], Path]:
        journal_path = self._journal_path(self._binding_store.root, board_id)
        durable = self._load_journal(journal_path, board_id=board_id)
        requested = (
            None
            if survivor_board_ids is None
            else sorted(set(survivor_board_ids) - {board_id})
        )
        if (
            durable is not None
            and requested is not None
            and durable.get("survivor_board_ids") != requested
        ):
            raise RuntimeError("global_discovery_privacy_survivor_authority_changed")

        current: dict[str, Any] | None = None
        if self._physical_exists(snapshot):
            with self._runtime(snapshot) as runtime:
                current = self._capture(
                    runtime,
                    board_id=board_id,
                    survivor_board_ids=(
                        tuple(durable["survivor_board_ids"])
                        if durable is not None
                        else survivor_board_ids
                    ),
                )
        if durable is None:
            if current is None:
                raise RuntimeError("global_discovery_privacy_survivor_source_missing")
            durable = current
        elif current is not None:
            merged = CommunityGlobalDiscoveryRuntime._merge_privacy_survivor_rows(
                journal_rows=durable["rows"],
                current_rows=current["rows"],
            )
            durable = CommunityGlobalDiscoveryRuntime._build_privacy_survivor_snapshot(
                board_id=board_id,
                rows=merged,
                survivor_board_ids=set(durable["survivor_board_ids"]),
            )
        self._write_journal(journal_path, durable)
        CommunityGlobalDiscoveryRuntime._validate_privacy_survivor_snapshot(
            durable,
            board_id=board_id,
        )
        return durable, journal_path

    def _close_both(self) -> tuple[int, int]:
        ladybug_closed = 0
        grafx_closed = 0
        failures: list[BaseException] = []
        try:
            ladybug_closed = self._ladybug.close_all()
        except Exception as failure:  # noqa: BLE001 - attempt both engines
            failures.append(failure)
        try:
            grafx_closed = self._grafx.close_all()
        except Exception as failure:  # noqa: BLE001 - attempt both engines
            failures.append(failure)
        if failures:
            raise CommunityGlobalGraphShutdownError(
                administration_failures=tuple(failures),
            )
        return ladybug_closed, grafx_closed

    def _erase_dual_layout(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        binding: bytes,
    ) -> tuple[int, int]:
        global_root = self._binding_store.root / "global"
        reject_filesystem_alias_ancestry(global_root)
        files_removed = 0
        directories_removed = 0
        for target in sorted(global_root.iterdir(), key=lambda item: item.name):
            if target.name in _PRESERVED_GLOBAL_CONTROL_FILES:
                continue

            def fenced() -> None:
                self._revalidate_write_fence("privacy_erase_global_discovery")
                _require_binding_unchanged(self._binding_path, binding)

            files, directories = remove_contained_tree(
                target,
                base_dir=global_root,
                before_mutation=fenced,
            )
            files_removed += files
            directories_removed += directories
        fsync_directory(global_root)
        _require_binding_unchanged(self._binding_path, binding)
        residues = tuple(
            child.name
            for child in global_root.iterdir()
            if child.name not in _PRESERVED_GLOBAL_CONTROL_FILES
        )
        if residues:
            raise RuntimeError("global_discovery_privacy_dual_layout_residue")
        return files_removed, directories_removed

    @contextmanager
    def _fresh_bound_runtime(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> Iterator[_RuntimeLike]:
        if snapshot.backend == "ladybug":
            runtime = self._ladybug.runtime_for_path(snapshot.anchor_path)
            runtime.bootstrap()
            yield runtime
            return
        if snapshot.page_size is None:
            raise RuntimeError("grafx_route_page_size_missing")
        holder = _RotatingGrafxLease(
            self._grafx,
            path=snapshot.anchor_path,
            page_size=snapshot.page_size,
            admit=require_global_grafx_admission,
        )
        runtime = CommunityGrafxGlobalDiscoveryRuntime(
            holder.database,
            lambda: snapshot.anchor_path,
            holder.close,
            self._revalidate_write_fence,
            admission=require_global_grafx_admission,
        )
        try:
            runtime.bootstrap()
            yield runtime
        finally:
            holder.release()

    def __call__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        board_id: str,
        reason: str,
        survivor_board_ids: tuple[str, ...] | None,
    ) -> dict[str, object]:
        safe_board_id = validate_scope_id(board_id)
        binding = _binding_bytes(self._binding_path)
        self._revalidate_write_fence("privacy_erase_global_discovery")
        survivors, journal_path = self._durable_survivors(
            snapshot,
            board_id=safe_board_id,
            survivor_board_ids=survivor_board_ids,
        )
        # From this point on, any failure is explicitly partial and the durable
        # target-free journal remains for an exact retry.
        self._close_both()
        files_removed, directories_removed = self._erase_dual_layout(
            snapshot,
            binding=binding,
        )
        with self._fresh_bound_runtime(snapshot) as runtime:
            restored = (
                CommunityGlobalDiscoveryRuntime._restore_privacy_survivor_snapshot(
                    _PrivacyRuntimeFacade(runtime),
                    survivors,
                )
            )
            runtime.flush_after_write_batch()
            observed = self._capture(
                runtime,
                board_id=safe_board_id,
                survivor_board_ids=tuple(survivors["survivor_board_ids"]),
            )
        if observed["manifest"] != survivors["manifest"]:
            raise RuntimeError("global_discovery_privacy_survivor_verification_failed")
        _require_binding_unchanged(self._binding_path, binding)
        rebound = self._resolver.acquire_global_route()
        if not _immutable_binding_matches(snapshot, rebound):
            raise RuntimeError("global_discovery_privacy_binding_changed")
        journal_path.unlink()
        fsync_directory(self._binding_store.root)
        return {
            "board_id": safe_board_id,
            "reason": reason,
            "objects_removed": files_removed,
            "directories_removed": directories_removed,
            "verified_absent": True,
            "survivors_restored": restored,
            "status": (
                "purged" if files_removed or directories_removed else "not_found"
            ),
        }


class CommunityGlobalGraphRouteInitializer:
    """The sole explicit initialization door for the routed Global graph."""

    def __init__(
        self,
        *,
        resolver: CommunityGraphRouteResolver,
        routed_runtime: CommunityRoutedGlobalDiscoveryRuntime,
        ladybug: _LadybugGlobalRuntimeManager,
        grafx: _GrafxGlobalPoolManager,
        global_lock: _GlobalLock,
        revalidate_write_fence: FenceRevalidator,
    ) -> None:
        self._resolver = resolver
        self._routed_runtime = routed_runtime
        self._ladybug = ladybug
        self._grafx = grafx
        self._global_lock = global_lock
        self._revalidate_write_fence = revalidate_write_fence

    def _create_physical(
        self, candidate: CommunityGraphRouteCandidate
    ) -> object | None:
        self._revalidate_write_fence("initialize_global_route")
        if candidate.backend == "ladybug":
            runtime = self._ladybug.runtime_for_path(candidate.anchor_path)
            try:
                runtime.bootstrap()
            finally:
                runtime.close()
            return None
        if candidate.page_size is None:
            raise RuntimeError("grafx_route_page_size_missing")
        holder = _RotatingGrafxLease(
            self._grafx,
            path=candidate.anchor_path,
            page_size=candidate.page_size,
            admit=require_global_grafx_admission,
        )
        database = holder.database()
        runtime = CommunityGrafxGlobalDiscoveryRuntime(
            holder.database,
            lambda: candidate.anchor_path,
            holder.close,
            self._revalidate_write_fence,
            admission=require_global_grafx_admission,
        )
        try:
            runtime.bootstrap()
            return database
        except BaseException:
            holder.close()
            raise
        finally:
            holder.release()

    def __call__(self) -> CommunityGraphRouteSnapshot:
        with self._global_lock:
            self._revalidate_write_fence("initialize_global_route")
            self._resolver.initialize_global_route(
                create_physical=self._create_physical,
            )
            # Existing/adopted physical stores receive the same schema and
            # geometry admission as newly-created ones.  This is still explicit
            # init; no state/read path reaches it.
            self._routed_runtime.bootstrap()
            return self._resolver.acquire_global_route()


class CommunityGlobalGraphShutdownError(RuntimeError):
    """All Global handles were attempted, but at least one could not close."""

    def __init__(
        self,
        *,
        ladybug_failures: tuple[BaseException, ...] = (),
        grafx_failures: tuple[tuple[Path, BaseException], ...] = (),
        administration_failures: tuple[BaseException, ...] = (),
    ) -> None:
        self.ladybug_failures = ladybug_failures
        self.grafx_failures = grafx_failures
        self.administration_failures = administration_failures
        super().__init__("global_graph_shutdown_partial")


class CommunityGlobalGraphShutdown:
    def __init__(
        self,
        *,
        ladybug: _LadybugGlobalRuntimeManager,
        grafx: _GrafxGlobalPoolManager,
        global_lock: _GlobalLock,
    ) -> None:
        self._ladybug = ladybug
        self._grafx = grafx
        self._global_lock = global_lock

    def __call__(self) -> dict[str, int]:
        with self._global_lock:
            ladybug_closed = 0
            grafx_closed = 0
            failures: list[BaseException] = []
            try:
                ladybug_closed = self._ladybug.close_all()
            except Exception as failure:  # noqa: BLE001 - attempt both engines
                failures.append(failure)
            try:
                grafx_closed = self._grafx.close_all()
            except Exception as failure:  # noqa: BLE001 - attempt both engines
                failures.append(failure)
            if failures:
                raise CommunityGlobalGraphShutdownError(
                    administration_failures=tuple(failures),
                )
            return {
                "ladybug_closed": ladybug_closed,
                "grafx_closed": grafx_closed,
            }


@dataclass(frozen=True, slots=True)
class CommunityRoutedGlobalGraphComposition:
    """Identity-bearing bundle published by the application composition root."""

    binding_store: CommunityGraphBackendBindingStore
    resolver: CommunityGraphRouteResolver
    grafx_pool: CommunityGrafxDatabasePool
    global_lock: _GlobalLock
    runtime: CommunityRoutedGlobalDiscoveryRuntime
    recovery: CommunityRoutedGlobalDiscoveryRecovery
    initializer: CommunityGlobalGraphRouteInitializer
    shutdown: CommunityGlobalGraphShutdown

    def initialize_global_route(self) -> CommunityGraphRouteSnapshot:
        return self.initializer()

    def close_all_on_shutdown(self) -> dict[str, int]:
        return self.shutdown()


def build_community_routed_global_graph_composition(
    *,
    binding_store: CommunityGraphBackendBindingStore,
    resolver: CommunityGraphRouteResolver,
    grafx_pool: CommunityGrafxDatabasePool,
    global_lock: threading.RLock,
    revalidate_write_fence: FenceRevalidator | None = None,
    ladybug_runtime_factory: RuntimeFactory | None = None,
    grafx_connect: GrafxConnect | None = None,
    quarantine_targets: QuarantineTargets | None = None,
) -> CommunityRoutedGlobalGraphComposition:
    """Build Global routing from the exact shared dependencies supplied by caller."""

    if binding_store is None or resolver is None or grafx_pool is None:
        raise TypeError("binding_store, resolver and grafx_pool are required")
    if global_lock is None:
        raise TypeError("global_lock is required")
    revalidate = revalidate_write_fence or _default_fence_revalidator
    ladybug = _LadybugGlobalRuntimeManager(ladybug_runtime_factory)
    grafx = _GrafxGlobalPoolManager(grafx_pool)
    administration = _GlobalAdministrationBinding()
    ladybug_sessions = _LadybugRuntimeSessionFactory(ladybug, administration)
    grafx_sessions = _GrafxRuntimeSessionFactory(
        resolver=resolver,
        pool_manager=grafx,
        revalidate_write_fence=revalidate,
        administration=administration,
    )
    purge = _GlobalPurgeCoordinator(
        binding_store=binding_store,
        resolver=resolver,
        ladybug=ladybug,
        grafx=grafx,
        revalidate_write_fence=revalidate,
        quarantine_targets=quarantine_targets,
    )
    privacy = _GlobalPrivacyCoordinator(
        binding_store=binding_store,
        resolver=resolver,
        ladybug=ladybug,
        grafx=grafx,
        grafx_sessions=grafx_sessions,
        revalidate_write_fence=revalidate,
    )
    administration.bind(purge=purge, privacy=privacy)

    def ladybug_state(
        snapshot: CommunityGraphRouteSnapshot,
        generation: str | None,
    ) -> Any:
        return ladybug.runtime_for(snapshot).state(generation=generation)

    runtime = CommunityRoutedGlobalDiscoveryRuntime(
        resolver,
        global_lock=global_lock,
        revalidate_write_fence=revalidate,
        statement_is_write=statement_is_write,
        ladybug_session_factory=ladybug_sessions,
        grafx_session_factory=grafx_sessions,
        ladybug_state=ladybug_state,
        grafx_state=_grafx_state,
        ladybug_materialization_paths=lambda snapshot: ladybug.runtime_for(
            snapshot
        ).materialization_observation_paths(),
        grafx_materialization_paths=_grafx_materialization_paths,
        ladybug_close_unguarded=ladybug.close_snapshot,
        grafx_close_unguarded=lambda _snapshot: grafx.close_all(),
        ladybug_purge_unguarded=purge,
        grafx_purge_unguarded=purge,
        ladybug_privacy_erase_unguarded=privacy,
        grafx_privacy_erase_unguarded=privacy,
    )
    fingerprint = _SnapshotFingerprintBinding()
    recovery = _ComposedRoutedGlobalDiscoveryRecovery(
        resolver,
        global_lock=global_lock,
        ladybug_factory=_LadybugRecoveryFactory(ladybug, fingerprint),
        grafx_factory=_GrafxRecoveryFactory(
            resolver=resolver,
            pool_manager=grafx,
            fingerprint=fingerprint,
            connect=grafx_connect,
        ),
        validate_authenticated_transition=_validate_authenticated_recovery_transition,
        fingerprint_binding=fingerprint,
    )
    initializer = CommunityGlobalGraphRouteInitializer(
        resolver=resolver,
        routed_runtime=runtime,
        ladybug=ladybug,
        grafx=grafx,
        global_lock=global_lock,
        revalidate_write_fence=revalidate,
    )
    shutdown = CommunityGlobalGraphShutdown(
        ladybug=ladybug,
        grafx=grafx,
        global_lock=global_lock,
    )
    return CommunityRoutedGlobalGraphComposition(
        binding_store=binding_store,
        resolver=resolver,
        grafx_pool=grafx_pool,
        global_lock=global_lock,
        runtime=runtime,
        recovery=recovery,
        initializer=initializer,
        shutdown=shutdown,
    )


__all__ = [
    "CommunityGlobalGraphRouteInitializer",
    "CommunityGlobalGraphShutdown",
    "CommunityGlobalGraphShutdownError",
    "CommunityRoutedGlobalGraphComposition",
    "build_community_routed_global_graph_composition",
]
