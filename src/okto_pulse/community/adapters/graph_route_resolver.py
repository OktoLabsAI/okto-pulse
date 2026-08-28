"""Fail-closed routing over immutable Community graph backend bindings.

The resolver has two deliberately different doors.  ``inspect_*`` authenticates
the persisted decision even when its database is missing, which keeps recovery
and privacy operations routable.  ``acquire_*`` additionally requires the live
physical route and is the ordinary runtime door.  Neither door consults current
settings after a binding exists.

Only explicit ``initialize_*`` calls may discover or create unbound storage.
Discovery and publication share a per-scope process/file lock, so a database
created immediately before a crash is adopted on the next call and a binding is
never published before the physical database has been admitted.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphLockContention,
    GraphUnavailable,
)

from okto_pulse.community.adapters.filesystem_erasure import (
    contained_lexical_path,
    is_filesystem_alias,
)
from okto_pulse.community.adapters.global_discovery_layout import (
    GENERATION_MANIFEST_FILENAME,
    ActiveGeneration,
    GlobalDiscoveryLayoutError,
    active_pointer_path,
    generations_root,
    read_active_generation,
)
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBinding,
    CommunityGraphBackendBindingStore,
    GrafxDatabaseAdmission,
    GraphBackend,
    GraphBindingScope,
    admit_grafx_database,
)
from okto_pulse.community.config import validate_grafx_page_size

_INITIAL_GENERATION = "generation-1"
_ROUTE_LOCK_FILENAME = ".graph_route_initialization.lock"
_GRAFX_IDENTITY_FILENAME = "grafx.meta"
_LADYBUG_SIDECAR_SUFFIXES = (".wal", ".shadow", ".wal.checkpoint")


@dataclass(frozen=True, slots=True)
class CommunityGraphRouteCandidate:
    """The exact physical result an initialization callback must produce."""

    scope: GraphBindingScope
    scope_id: str
    backend: GraphBackend
    generation: str
    binding_path: Path
    anchor_path: Path
    page_size: int | None


@dataclass(frozen=True, slots=True)
class CommunityGraphRouteSnapshot:
    """An immutable, callback-safe route and its authenticated digests."""

    scope: GraphBindingScope
    scope_id: str
    backend: GraphBackend
    generation: str
    binding_path: Path
    anchor_path: Path
    active_path: Path
    page_size: int | None
    binding_sha256: str
    route_sha256: str
    active_generation: str | None = None
    active_manifest_sha256: str | None = None


GraphPhysicalCreationCallback = Callable[[CommunityGraphRouteCandidate], object | None]
GrafxDatabaseOpener = Callable[[Path], object]


@dataclass(frozen=True, slots=True)
class _DetectedRoute:
    backend: GraphBackend
    generation: str
    binding_path: Path
    page_size: int | None
    database: object | None = None


def _capability(
    reason: str,
    *,
    operation: str,
    scope: GraphBindingScope,
    scope_id: str,
    **details: object,
) -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        "The Community graph route was refused.",
        details={
            "operation": operation,
            "reason": reason,
            "scope": scope,
            "scope_id": scope_id,
            **details,
        },
    )


def _corruption(
    reason: str,
    *,
    scope: GraphBindingScope,
    scope_id: str,
    **details: object,
) -> GraphCorruption:
    return GraphCorruption(
        "The persisted Community graph route is inconsistent.",
        details={
            "operation": "inspect_community_graph_route",
            "reason": reason,
            "scope": scope,
            "scope_id": scope_id,
            **details,
        },
    )


def _unavailable(
    reason: str,
    *,
    operation: str,
    scope: GraphBindingScope,
    scope_id: str,
    error_type: str | None = None,
) -> GraphUnavailable:
    details: dict[str, object] = {
        "operation": operation,
        "reason": reason,
        "scope": scope,
        "scope_id": scope_id,
    }
    if error_type is not None:
        details["error_type"] = error_type
    return GraphUnavailable(
        "The Community graph route is unavailable.", details=details
    )


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(Path(os.path.abspath(left)))) == os.path.normcase(
        str(Path(os.path.abspath(right)))
    )


class CommunityGraphRouteResolver:
    """Resolve Board and Global routes without changing any Core protocol."""

    def __init__(
        self,
        binding_store: CommunityGraphBackendBindingStore,
        *,
        board_backend: GraphBackend,
        global_backend: GraphBackend,
        grafx_page_size: int,
        open_grafx_database: GrafxDatabaseOpener | None = None,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        if board_backend not in {"ladybug", "grafx"}:
            raise ValueError("board_backend must be 'ladybug' or 'grafx'")
        if global_backend not in {"ladybug", "grafx"}:
            raise ValueError("global_backend must be 'ladybug' or 'grafx'")
        self._store = binding_store
        self._root = binding_store.root
        self._board_backend = board_backend
        self._global_backend = global_backend
        self._grafx_page_size = validate_grafx_page_size(grafx_page_size)
        self._open_grafx_database = open_grafx_database
        if (
            isinstance(lock_timeout_seconds, bool)
            or not isinstance(lock_timeout_seconds, (int, float))
            or lock_timeout_seconds <= 0
        ):
            raise ValueError("lock_timeout_seconds must be positive")
        self._lock_timeout_seconds = float(lock_timeout_seconds)

    # -- read-only routing -------------------------------------------------

    def inspect_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        binding = self._store.inspect_board_binding(board_id)
        return self._snapshot(binding, require_active_physical=False)

    def acquire_board_route(self, board_id: str) -> CommunityGraphRouteSnapshot:
        binding = self._store.acquire_board_binding(board_id)
        return self._snapshot(binding, require_active_physical=True)

    def inspect_global_route(self) -> CommunityGraphRouteSnapshot:
        binding = self._store.inspect_global_binding()
        return self._snapshot(binding, require_active_physical=False)

    def acquire_global_route(self) -> CommunityGraphRouteSnapshot:
        binding = self._store.acquire_global_binding()
        return self._snapshot(binding, require_active_physical=True)

    def revalidate_snapshot(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        require_physical: bool = False,
    ) -> CommunityGraphRouteSnapshot:
        """Re-read one route and refuse a binding or active-pointer cutover."""

        if snapshot.scope == "board":
            current = (
                self.acquire_board_route(snapshot.scope_id)
                if require_physical
                else self.inspect_board_route(snapshot.scope_id)
            )
        else:
            if snapshot.scope_id != "global":
                raise _capability(
                    "graph_route_snapshot_scope_invalid",
                    operation="revalidate_community_graph_route",
                    scope=snapshot.scope,
                    scope_id=snapshot.scope_id,
                )
            current = (
                self.acquire_global_route()
                if require_physical
                else self.inspect_global_route()
            )
        if current != snapshot:
            raise _capability(
                "graph_route_snapshot_mismatch",
                operation="revalidate_community_graph_route",
                scope=snapshot.scope,
                scope_id=snapshot.scope_id,
                expected_binding_sha256=snapshot.binding_sha256,
                observed_binding_sha256=current.binding_sha256,
                expected_route_sha256=snapshot.route_sha256,
                observed_route_sha256=current.route_sha256,
            )
        return current

    def admit_grafx_route(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        database: object,
        *,
        operation: str,
    ) -> GrafxDatabaseAdmission:
        """Admit only the still-current exact Grafx target and persisted geometry."""

        if snapshot.backend != "grafx" or snapshot.page_size is None:
            raise _capability(
                "grafx_route_admission_requires_grafx",
                operation=operation,
                scope=snapshot.scope,
                scope_id=snapshot.scope_id,
            )
        current = self.revalidate_snapshot(snapshot)
        self._require_expected_path(
            current.active_path,
            expected="directory",
            scope=current.scope,
            scope_id=current.scope_id,
            missing_is_unavailable=True,
        )
        self._require_grafx_marker(
            current.active_path,
            scope=current.scope,
            scope_id=current.scope_id,
        )
        return admit_grafx_database(
            database,
            expected_page_size=current.page_size,
            expected_path=current.active_path,
            operation=operation,
        )

    # -- explicit initialization -----------------------------------------

    def initialize_board_route(
        self,
        board_id: str,
        *,
        create_physical: GraphPhysicalCreationCallback | None = None,
    ) -> CommunityGraphRouteSnapshot:
        try:
            return self.inspect_board_route(board_id)
        except GraphCapabilityUnavailable as failure:
            if failure.details.get("reason") != "binding_missing":
                raise
        ladybug_path = self._store.board_ladybug_path(board_id)
        return self._initialize_missing(
            scope="board",
            scope_id=board_id,
            lock_path=ladybug_path.parent / _ROUTE_LOCK_FILENAME,
            configured_backend=self._board_backend,
            create_physical=create_physical,
        )

    def initialize_global_route(
        self,
        *,
        create_physical: GraphPhysicalCreationCallback | None = None,
    ) -> CommunityGraphRouteSnapshot:
        try:
            return self.inspect_global_route()
        except GraphCapabilityUnavailable as failure:
            if failure.details.get("reason") != "binding_missing":
                raise
        return self._initialize_missing(
            scope="global",
            scope_id="global",
            lock_path=self._store.global_ladybug_path().parent / _ROUTE_LOCK_FILENAME,
            configured_backend=self._global_backend,
            create_physical=create_physical,
        )

    def _initialize_missing(
        self,
        *,
        scope: GraphBindingScope,
        scope_id: str,
        lock_path: Path,
        configured_backend: GraphBackend,
        create_physical: GraphPhysicalCreationCallback | None,
    ) -> CommunityGraphRouteSnapshot:
        operation = "initialize_community_graph_route"
        # This gate must precede mkdir and FileLock.  A board/global directory
        # may already be a Windows junction (reported as a normal directory by
        # pathlib on Python 3.11); following it here would publish the lock
        # outside the configured root before routing had refused the alias.
        self._require_no_alias(lock_path, scope=scope, scope_id=scope_id)
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as failure:
            raise _unavailable(
                "graph_route_lock_directory_create_failed",
                operation=operation,
                scope=scope,
                scope_id=scope_id,
                error_type=type(failure).__name__,
            ) from failure
        lock = FileLock(str(lock_path), timeout=self._lock_timeout_seconds)
        try:
            with lock:
                existing = self._inspect_if_bound(scope=scope, scope_id=scope_id)
                if existing is not None:
                    return existing
                detected = self._detect(scope=scope, scope_id=scope_id)
                if detected is None:
                    candidate = self._initial_candidate(
                        scope=scope,
                        scope_id=scope_id,
                        backend=configured_backend,
                    )
                    if create_physical is None:
                        raise _capability(
                            "graph_route_creation_callback_required",
                            operation=operation,
                            scope=scope,
                            scope_id=scope_id,
                            backend=configured_backend,
                        )
                    try:
                        created_database = create_physical(candidate)
                    except (
                        GraphCapabilityUnavailable,
                        GraphCorruption,
                        GraphLockContention,
                        GraphUnavailable,
                    ):
                        raise
                    except Exception as failure:
                        raise _unavailable(
                            "graph_route_creation_failed",
                            operation=operation,
                            scope=scope,
                            scope_id=scope_id,
                            error_type=type(failure).__name__,
                        ) from failure
                    supplied = (
                        {candidate.binding_path: created_database}
                        if created_database is not None
                        else None
                    )
                    detected = self._detect(
                        scope=scope,
                        scope_id=scope_id,
                        supplied_grafx=supplied,
                    )
                    if (
                        detected is None
                        or detected.backend != candidate.backend
                        or detected.generation != candidate.generation
                        or not _same_path(detected.binding_path, candidate.binding_path)
                        or detected.page_size != candidate.page_size
                    ):
                        raise _capability(
                            "graph_route_creation_result_mismatch",
                            operation=operation,
                            scope=scope,
                            scope_id=scope_id,
                            backend=configured_backend,
                        )
                binding = self._publish_detected(
                    scope=scope,
                    scope_id=scope_id,
                    detected=detected,
                )
                return self._snapshot(binding, require_active_physical=True)
        except FileLockTimeout as failure:
            raise GraphLockContention(
                "The Community graph route initialization lock is contended.",
                details={
                    "operation": operation,
                    "reason": "graph_route_initialization_lock_contention",
                    "scope": scope,
                    "scope_id": scope_id,
                },
            ) from failure

    def _inspect_if_bound(
        self, *, scope: GraphBindingScope, scope_id: str
    ) -> CommunityGraphRouteSnapshot | None:
        try:
            return (
                self.inspect_board_route(scope_id)
                if scope == "board"
                else self.inspect_global_route()
            )
        except GraphCapabilityUnavailable as failure:
            if failure.details.get("reason") == "binding_missing":
                return None
            raise

    def _publish_detected(
        self,
        *,
        scope: GraphBindingScope,
        scope_id: str,
        detected: _DetectedRoute,
    ) -> CommunityGraphBackendBinding:
        if scope == "board":
            return self._store.initialize_board_binding(
                board_id=scope_id,
                backend=detected.backend,
                generation=detected.generation,
                physical_path=detected.binding_path,
                page_size=detected.page_size,
                database=detected.database,
            )
        return self._store.initialize_global_binding(
            backend=detected.backend,
            generation=detected.generation,
            physical_path=detected.binding_path,
            page_size=detected.page_size,
            database=detected.database,
        )

    # -- physical discovery ----------------------------------------------

    def _detect(
        self,
        *,
        scope: GraphBindingScope,
        scope_id: str,
        supplied_grafx: dict[Path, object] | None = None,
    ) -> _DetectedRoute | None:
        if scope == "board":
            return self._detect_board(scope_id, supplied_grafx=supplied_grafx)
        return self._detect_global(supplied_grafx=supplied_grafx)

    def _detect_board(
        self,
        board_id: str,
        *,
        supplied_grafx: dict[Path, object] | None,
    ) -> _DetectedRoute | None:
        scope: GraphBindingScope = "board"
        ladybug = self._store.board_ladybug_path(board_id)
        ladybug_kind = self._path_kind(ladybug, scope=scope, scope_id=board_id)
        ladybug_sidecars = self._ladybug_sidecars_present(
            ladybug, scope=scope, scope_id=board_id
        )
        if ladybug_kind not in {None, "file"}:
            self._raise_ambiguous(scope=scope, scope_id=board_id)
        if ladybug_kind is None and ladybug_sidecars:
            self._raise_ambiguous(scope=scope, scope_id=board_id)
        if ladybug_kind == "file":
            self._require_no_alias(ladybug, scope=scope, scope_id=board_id)

        grafx_root = self._store.board_grafx_path(board_id, _INITIAL_GENERATION).parent
        grafx_kind = self._path_kind(grafx_root, scope=scope, scope_id=board_id)
        grafx_present = grafx_kind is not None
        if grafx_kind not in {None, "directory"}:
            self._raise_ambiguous(scope=scope, scope_id=board_id)
        if ladybug_kind == "file" and grafx_present:
            self._raise_ambiguous(scope=scope, scope_id=board_id)
        if ladybug_kind == "file":
            return _DetectedRoute(
                backend="ladybug",
                generation=_INITIAL_GENERATION,
                binding_path=ladybug,
                page_size=None,
            )
        if not grafx_present:
            return None

        self._require_no_alias(grafx_root, scope=scope, scope_id=board_id)
        children = self._children(grafx_root, scope=scope, scope_id=board_id)
        if len(children) != 1:
            self._raise_ambiguous(scope=scope, scope_id=board_id)
        path = children[0]
        if self._path_kind(path, scope=scope, scope_id=board_id) != "directory":
            self._raise_ambiguous(scope=scope, scope_id=board_id)
        try:
            canonical = self._store.board_grafx_path(board_id, path.name)
        except GraphCapabilityUnavailable:
            self._raise_ambiguous(scope=scope, scope_id=board_id)
        if not _same_path(path, canonical):
            self._raise_ambiguous(scope=scope, scope_id=board_id)
        database, page_size = self._authenticate_grafx(
            path,
            scope=scope,
            scope_id=board_id,
            supplied=self._supplied_database(path, supplied_grafx),
        )
        return _DetectedRoute(
            backend="grafx",
            generation=path.name,
            binding_path=path,
            page_size=page_size,
            database=database,
        )

    def _detect_global(
        self, *, supplied_grafx: dict[Path, object] | None
    ) -> _DetectedRoute | None:
        scope: GraphBindingScope = "global"
        scope_id = "global"
        ladybug_anchor = self._store.global_ladybug_path()
        ladybug_artifacts = self._global_ladybug_artifacts_present(ladybug_anchor)
        grafx_root = self._store.global_grafx_path(_INITIAL_GENERATION).parent
        grafx_kind = self._path_kind(grafx_root, scope=scope, scope_id=scope_id)
        grafx_artifacts = grafx_kind is not None
        if grafx_kind not in {None, "directory"}:
            self._raise_ambiguous(scope=scope, scope_id=scope_id)
        if ladybug_artifacts and grafx_artifacts:
            self._raise_ambiguous(scope=scope, scope_id=scope_id)
        if ladybug_artifacts:
            return self._detect_global_ladybug(ladybug_anchor)
        if not grafx_artifacts:
            return None
        return self._detect_global_grafx(grafx_root, supplied_grafx=supplied_grafx)

    def _global_ladybug_artifacts_present(self, anchor: Path) -> bool:
        for path in (anchor, active_pointer_path(anchor), generations_root(anchor)):
            if self._path_kind(path, scope="global", scope_id="global") is not None:
                return True
        return self._ladybug_sidecars_present(anchor, scope="global", scope_id="global")

    def _ladybug_sidecars_present(
        self,
        primary: Path,
        *,
        scope: GraphBindingScope,
        scope_id: str,
    ) -> bool:
        """Recognize only the WAL/checkpoint sidecars owned by Ladybug runtime."""

        return any(
            self._path_kind(
                primary.with_name(primary.name + suffix),
                scope=scope,
                scope_id=scope_id,
            )
            is not None
            for suffix in _LADYBUG_SIDECAR_SUFFIXES
        )

    def _detect_global_ladybug(self, anchor: Path) -> _DetectedRoute:
        scope: GraphBindingScope = "global"
        scope_id = "global"
        anchor_kind = self._path_kind(anchor, scope=scope, scope_id=scope_id)
        if anchor_kind not in {None, "file"}:
            self._raise_ambiguous(scope=scope, scope_id=scope_id)
        if anchor_kind == "file":
            self._require_no_alias(anchor, scope=scope, scope_id=scope_id)
        self._authenticated_active(
            anchor,
            expected="file",
            scope=scope,
            scope_id=scope_id,
            require_physical=True,
        )
        generation_kind = self._path_kind(
            generations_root(anchor), scope=scope, scope_id=scope_id
        )
        if generation_kind not in {None, "directory"}:
            self._raise_ambiguous(scope=scope, scope_id=scope_id)
        if anchor_kind != "file":
            self._raise_ambiguous(scope=scope, scope_id=scope_id)
        return _DetectedRoute(
            backend="ladybug",
            generation=_INITIAL_GENERATION,
            binding_path=anchor,
            page_size=None,
        )

    def _detect_global_grafx(
        self,
        grafx_root: Path,
        *,
        supplied_grafx: dict[Path, object] | None,
    ) -> _DetectedRoute:
        scope: GraphBindingScope = "global"
        scope_id = "global"
        self._require_no_alias(grafx_root, scope=scope, scope_id=scope_id)
        children = self._children(grafx_root, scope=scope, scope_id=scope_id)
        reserved = {"active_generation.json", "discovery.generations"}
        anchors = [child for child in children if child.name not in reserved]
        if len(anchors) != 1:
            self._raise_ambiguous(scope=scope, scope_id=scope_id)
        for child in children:
            if child.name == "active_generation.json":
                if self._path_kind(child, scope=scope, scope_id=scope_id) != "file":
                    self._raise_ambiguous(scope=scope, scope_id=scope_id)
            elif child.name == "discovery.generations":
                if (
                    self._path_kind(child, scope=scope, scope_id=scope_id)
                    != "directory"
                ):
                    self._raise_ambiguous(scope=scope, scope_id=scope_id)
            elif self._path_kind(child, scope=scope, scope_id=scope_id) != "directory":
                self._raise_ambiguous(scope=scope, scope_id=scope_id)
        anchor = anchors[0]
        try:
            canonical = self._store.global_grafx_path(anchor.name)
        except GraphCapabilityUnavailable:
            self._raise_ambiguous(scope=scope, scope_id=scope_id)
        if not _same_path(anchor, canonical):
            self._raise_ambiguous(scope=scope, scope_id=scope_id)
        database, page_size = self._authenticate_grafx(
            anchor,
            scope=scope,
            scope_id=scope_id,
            supplied=self._supplied_database(anchor, supplied_grafx),
        )
        active = self._authenticated_active(
            anchor,
            expected="directory",
            scope=scope,
            scope_id=scope_id,
            require_physical=True,
        )
        if active is not None:
            _active_database, active_page_size = self._authenticate_grafx(
                active.graph_path,
                scope=scope,
                scope_id=scope_id,
                supplied=self._supplied_database(active.graph_path, supplied_grafx),
            )
            if active_page_size != page_size:
                self._raise_ambiguous(scope=scope, scope_id=scope_id)
        return _DetectedRoute(
            backend="grafx",
            generation=anchor.name,
            binding_path=anchor,
            page_size=page_size,
            database=database,
        )

    def _initial_candidate(
        self,
        *,
        scope: GraphBindingScope,
        scope_id: str,
        backend: GraphBackend,
    ) -> CommunityGraphRouteCandidate:
        if scope == "board":
            path = (
                self._store.board_ladybug_path(scope_id)
                if backend == "ladybug"
                else self._store.board_grafx_path(scope_id, _INITIAL_GENERATION)
            )
        else:
            path = (
                self._store.global_ladybug_path()
                if backend == "ladybug"
                else self._store.global_grafx_path(_INITIAL_GENERATION)
            )
        return CommunityGraphRouteCandidate(
            scope=scope,
            scope_id=scope_id,
            backend=backend,
            generation=_INITIAL_GENERATION,
            binding_path=path,
            anchor_path=path,
            page_size=self._grafx_page_size if backend == "grafx" else None,
        )

    # -- route authentication --------------------------------------------

    def _snapshot(
        self,
        binding: CommunityGraphBackendBinding,
        *,
        require_active_physical: bool,
    ) -> CommunityGraphRouteSnapshot:
        expected = "directory" if binding.backend == "grafx" else "file"
        if binding.scope == "board":
            anchor = binding.physical_path
            active_path = binding.physical_path
            active_generation = None
            manifest_sha256 = None
        else:
            anchor = (
                binding.physical_path
                if binding.backend == "grafx"
                else self._store.global_ladybug_path()
            )
            active = self._authenticated_active(
                anchor,
                expected=expected,
                scope=binding.scope,
                scope_id=binding.scope_id,
                require_physical=require_active_physical,
            )
            active_path = active.graph_path if active is not None else anchor
            active_generation = active.generation_id if active is not None else None
            manifest_sha256 = active.manifest_sha256 if active is not None else None
            if binding.backend == "ladybug" and not _same_path(
                binding.physical_path, anchor
            ):
                raise _corruption(
                    "global_route_binding_anchor_invalid",
                    scope=binding.scope,
                    scope_id=binding.scope_id,
                )
        self._require_expected_path(
            active_path,
            expected=expected,
            scope=binding.scope,
            scope_id=binding.scope_id,
            missing_is_unavailable=require_active_physical,
        )
        route_sha256 = self._route_sha256(
            binding=binding,
            anchor=anchor,
            active_path=active_path,
            active_generation=active_generation,
            active_manifest_sha256=manifest_sha256,
        )
        return CommunityGraphRouteSnapshot(
            scope=binding.scope,
            scope_id=binding.scope_id,
            backend=binding.backend,
            generation=binding.generation,
            binding_path=binding.physical_path,
            anchor_path=anchor,
            active_path=active_path,
            page_size=binding.page_size,
            binding_sha256=binding.binding_sha256,
            route_sha256=route_sha256,
            active_generation=active_generation,
            active_manifest_sha256=manifest_sha256,
        )

    def _route_sha256(
        self,
        *,
        binding: CommunityGraphBackendBinding,
        anchor: Path,
        active_path: Path,
        active_generation: str | None,
        active_manifest_sha256: str | None,
    ) -> str:
        def relative(path: Path) -> str:
            contained = contained_lexical_path(self._root, path)
            return PurePosixPath(*contained.relative_to(self._root).parts).as_posix()

        body = {
            "scope": binding.scope,
            "scope_id": binding.scope_id,
            "backend": binding.backend,
            "generation": binding.generation,
            "binding_path": relative(binding.physical_path),
            "anchor_path": relative(anchor),
            "active_path": relative(active_path),
            "page_size": binding.page_size,
            "binding_sha256": binding.binding_sha256,
            "active_generation": active_generation,
            "active_manifest_sha256": active_manifest_sha256,
        }
        encoded = json.dumps(
            body, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _authenticated_active(
        self,
        anchor: Path,
        *,
        expected: str,
        scope: GraphBindingScope,
        scope_id: str,
        require_physical: bool,
    ) -> ActiveGeneration | None:
        pointer = active_pointer_path(anchor)
        pointer_kind = self._path_kind(pointer, scope=scope, scope_id=scope_id)
        if pointer_kind is None:
            return None
        if pointer_kind != "file":
            raise _corruption(
                "global_route_pointer_invalid", scope=scope, scope_id=scope_id
            )
        self._require_no_alias(pointer, scope=scope, scope_id=scope_id)
        generation_root = generations_root(anchor)
        if (
            self._path_kind(generation_root, scope=scope, scope_id=scope_id)
            != "directory"
        ):
            raise _corruption(
                "global_route_generations_root_invalid",
                scope=scope,
                scope_id=scope_id,
            )
        # Gate the shared root before the layout reader can traverse it while
        # authenticating the generation manifest.  A post-read alias check is
        # too late: the reparse target would already have been opened.
        self._require_no_alias(generation_root, scope=scope, scope_id=scope_id)
        try:
            active = read_active_generation(anchor)
        except GlobalDiscoveryLayoutError as failure:
            raise _corruption(
                "global_route_pointer_or_manifest_invalid",
                scope=scope,
                scope_id=scope_id,
                layout_reason=failure.reason,
            ) from failure
        if active is None:  # pointer was observed and cannot disappear into fallback
            raise _corruption(
                "global_route_pointer_disappeared", scope=scope, scope_id=scope_id
            )
        lexical_generation = generation_root / active.generation_id
        lexical_target = lexical_generation / anchor.name
        self._require_expected_path(
            generation_root,
            expected="directory",
            scope=scope,
            scope_id=scope_id,
            missing_is_unavailable=False,
        )
        self._require_expected_path(
            lexical_generation,
            expected="directory",
            scope=scope,
            scope_id=scope_id,
            missing_is_unavailable=False,
        )
        if not _same_path(active.graph_path, lexical_target):
            raise _corruption(
                "global_route_active_target_mismatch", scope=scope, scope_id=scope_id
            )
        try:
            contained_lexical_path(generation_root, active.graph_path)
        except ValueError as failure:
            raise _corruption(
                "global_route_active_target_outside_generations",
                scope=scope,
                scope_id=scope_id,
            ) from failure
        manifest = lexical_generation / GENERATION_MANIFEST_FILENAME
        self._require_expected_path(
            manifest,
            expected="file",
            scope=scope,
            scope_id=scope_id,
            missing_is_unavailable=False,
        )
        if self._path_kind(manifest, scope=scope, scope_id=scope_id) is None:
            raise _corruption(
                "global_route_manifest_missing", scope=scope, scope_id=scope_id
            )
        self._require_expected_path(
            active.graph_path,
            expected=expected,
            scope=scope,
            scope_id=scope_id,
            missing_is_unavailable=require_physical,
        )
        return active

    # -- filesystem and Grafx admission helpers --------------------------

    def _authenticate_grafx(
        self,
        path: Path,
        *,
        scope: GraphBindingScope,
        scope_id: str,
        supplied: object | None,
    ) -> tuple[object, int]:
        self._require_expected_path(
            path,
            expected="directory",
            scope=scope,
            scope_id=scope_id,
            missing_is_unavailable=False,
        )
        self._require_grafx_marker(path, scope=scope, scope_id=scope_id)
        database = supplied
        if database is None:
            if self._open_grafx_database is None:
                raise _capability(
                    "grafx_route_adoption_opener_required",
                    operation="initialize_community_graph_route",
                    scope=scope,
                    scope_id=scope_id,
                )
            try:
                database = self._open_grafx_database(path)
            except (
                GraphCapabilityUnavailable,
                GraphCorruption,
                GraphLockContention,
                GraphUnavailable,
            ):
                raise
            except Exception as failure:
                raise _unavailable(
                    "grafx_route_open_failed",
                    operation="initialize_community_graph_route",
                    scope=scope,
                    scope_id=scope_id,
                    error_type=type(failure).__name__,
                ) from failure
        try:
            observed_page_size = database.identity.page_size
        except Exception as failure:
            raise _unavailable(
                "grafx_route_identity_unavailable",
                operation="initialize_community_graph_route",
                scope=scope,
                scope_id=scope_id,
                error_type=type(failure).__name__,
            ) from failure
        try:
            page_size = validate_grafx_page_size(observed_page_size)
        except (TypeError, ValueError) as failure:
            raise _capability(
                "grafx_route_persisted_page_size_invalid",
                operation="initialize_community_graph_route",
                scope=scope,
                scope_id=scope_id,
            ) from failure
        admit_grafx_database(
            database,
            expected_page_size=page_size,
            expected_path=path,
            operation="initialize_community_graph_route",
        )
        return database, page_size

    def _require_grafx_marker(
        self, path: Path, *, scope: GraphBindingScope, scope_id: str
    ) -> None:
        marker = path / _GRAFX_IDENTITY_FILENAME
        if self._path_kind(marker, scope=scope, scope_id=scope_id) != "file":
            self._raise_ambiguous(scope=scope, scope_id=scope_id)
        self._require_no_alias(marker, scope=scope, scope_id=scope_id)

    @staticmethod
    def _supplied_database(
        path: Path, supplied: dict[Path, object] | None
    ) -> object | None:
        if supplied is None:
            return None
        for candidate, database in supplied.items():
            if _same_path(candidate, path):
                return database
        return None

    def _children(
        self, path: Path, *, scope: GraphBindingScope, scope_id: str
    ) -> list[Path]:
        try:
            return sorted(path.iterdir(), key=lambda item: item.name)
        except OSError as failure:
            raise _unavailable(
                "graph_route_storage_enumeration_failed",
                operation="initialize_community_graph_route",
                scope=scope,
                scope_id=scope_id,
                error_type=type(failure).__name__,
            ) from failure

    def _path_kind(
        self, path: Path, *, scope: GraphBindingScope, scope_id: str
    ) -> str | None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as failure:
            raise _unavailable(
                "graph_route_storage_probe_failed",
                operation="inspect_community_graph_route",
                scope=scope,
                scope_id=scope_id,
                error_type=type(failure).__name__,
            ) from failure
        if stat.S_ISREG(metadata.st_mode):
            return "file"
        if stat.S_ISDIR(metadata.st_mode):
            return "directory"
        return "other"

    def _require_expected_path(
        self,
        path: Path,
        *,
        expected: str,
        scope: GraphBindingScope,
        scope_id: str,
        missing_is_unavailable: bool,
    ) -> None:
        kind = self._path_kind(path, scope=scope, scope_id=scope_id)
        if kind is None:
            if missing_is_unavailable:
                raise _unavailable(
                    "graph_route_active_database_missing",
                    operation="acquire_community_graph_route",
                    scope=scope,
                    scope_id=scope_id,
                )
            return
        if kind != expected:
            raise _corruption(
                "graph_route_physical_type_invalid", scope=scope, scope_id=scope_id
            )
        self._require_no_alias(path, scope=scope, scope_id=scope_id)

    def _require_no_alias(
        self, path: Path, *, scope: GraphBindingScope, scope_id: str
    ) -> None:
        try:
            candidate = contained_lexical_path(self._root, path)
            current = self._root
            if is_filesystem_alias(current):
                raise ValueError("root_alias")
            for segment in candidate.relative_to(self._root).parts:
                current /= segment
                if is_filesystem_alias(current):
                    raise ValueError("path_alias")
        except (OSError, RuntimeError, ValueError) as failure:
            raise _corruption(
                "graph_route_filesystem_alias_refused",
                scope=scope,
                scope_id=scope_id,
            ) from failure

    def _raise_ambiguous(self, *, scope: GraphBindingScope, scope_id: str) -> None:
        raise _capability(
            "graph_route_storage_ambiguous",
            operation="initialize_community_graph_route",
            scope=scope,
            scope_id=scope_id,
        )


__all__ = [
    "CommunityGraphRouteCandidate",
    "CommunityGraphRouteResolver",
    "CommunityGraphRouteSnapshot",
    "GrafxDatabaseOpener",
    "GraphPhysicalCreationCallback",
]
