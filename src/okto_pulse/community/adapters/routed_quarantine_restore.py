"""Backend-neutral routing for Board quarantine restore operations.

The quarantine manifest is classified without opening either graph engine.
The persisted Board route is then inspected and is the sole authority for
selecting a concrete restore adapter.  A missing binding is tolerated only for
the strictly-recognised Ladybug quarantine formats that predate persisted
bindings; it is never a reason to guess a Grafx route from settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphLockContention,
    GraphUnavailable,
)
from okto_pulse.core.kg.interfaces.quarantine_restore import (
    QuarantineRestoreError,
    QuarantineRestoreErrorCode,
    RestorePlan,
    RestoreReport,
)

from okto_pulse.community.adapters.filesystem_erasure import (
    is_filesystem_alias,
    reject_filesystem_alias_ancestry,
    validate_scope_id,
)
from okto_pulse.community.adapters.grafx_board_storage import (
    GRAFX_DIRECTORY_PAYLOAD,
    GRAFX_DIRECTORY_QUARANTINE_FORMAT,
    GRAFX_DIRECTORY_QUARANTINE_KIND,
    _canonical_sha256,
)
from okto_pulse.community.adapters.graph_route_resolver import (
    CommunityGraphRouteResolver,
    CommunityGraphRouteSnapshot,
)

_QUARANTINE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_QUARANTINE_ENTRIES = 4096
_MANIFEST_JSON = "manifest.json"
_MANIFEST_TEXT = "manifest.txt"
_RESTORE_OPERATION = "restore_operation.json"
_GRAFX_WAL_FORMAT = "pulse_grafx_quarantine/1"
_GRAFX_WAL_KINDS = frozenset({"grafx_wal_only", "grafx_restore_backup"})
_GRAFX_WAL_RELATIVE_RE = re.compile(r"wal/[0-9]{12}\.wal\Z")
_LADYBUG_WAL_KIND = "kg_wal_only_quarantine"
_LADYBUG_FILE_NAMES = frozenset(
    {
        "graph.lbug",
        "graph.lbug.wal",
        "graph.lbug.shadow",
        "graph.lbug.wal.checkpoint",
    }
)
_LADYBUG_WAL_FILE_NAMES = _LADYBUG_FILE_NAMES - {"graph.lbug"}
_GRAFX_EXCLUSIVE_MARKERS = frozenset(
    {
        "format",
        "database_path",
        "manifest_sha256",
        "inventory_sha256",
        "payload_relative",
        "directories",
        "binding_sha256",
        "generation",
        "native_quarantine_ids",
    }
)
_LADYBUG_EXCLUSIVE_MARKERS = frozenset(
    {"graph_path", "planned_files", "main_file", "original_board_dir"}
)
_LADYBUG_WAL_FIELDS = frozenset(
    {
        "kind",
        "quarantine_id",
        "board_id",
        "reason",
        "created_at",
        "graph_path",
        "planned_files",
        "files",
        "main_untouched",
        "main_file",
        "error",
    }
)
_LADYBUG_WAL_INVENTORY_FIELDS = frozenset({"name", "size", "sha256"})

ManifestBackend = Literal["ladybug", "grafx"]


class _RestoreProvider(Protocol):
    def plan(self, quarantine_id: str) -> RestorePlan: ...

    def apply(self, quarantine_id: str) -> RestoreReport: ...


GrafxRestoreFactory = Callable[
    [CommunityGraphRouteSnapshot],
    _RestoreProvider,
]
SnapshotDatabaseOpener = Callable[[CommunityGraphRouteSnapshot, Path], Any]


@dataclass(frozen=True, slots=True)
class _ManifestRoute:
    backend: ManifestBackend
    board_id: str
    manifest_format: str
    document: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class _SelectedRestore:
    classification: _ManifestRoute
    snapshot: CommunityGraphRouteSnapshot | None
    provider: _RestoreProvider


class CommunityGrafxSnapshotRestoreFactory:
    """Build the concrete Grafx adapter pinned to one immutable snapshot.

    Every path lookup and every concrete fence callback authenticates the same
    snapshot again.  The injected callbacks are physical primitives owned by
    the shared composition (pool/lifecycle/maintenance); this factory neither
    constructs another resolver nor opens a database while being built.
    """

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        quarantine_root: str | os.PathLike[str],
        open_database: SnapshotDatabaseOpener,
        close_board: Callable[[str], None],
        board_is_locked: Callable[[str], bool],
        revalidate_fence: Callable[[str, str], None],
        mutation_guard: Callable[[str], Any],
    ) -> None:
        self._resolver = resolver
        self._quarantine_root = Path(quarantine_root)
        self._open_database = open_database
        self._close_board = close_board
        self._board_is_locked = board_is_locked
        self._revalidate_fence = revalidate_fence
        self._mutation_guard = mutation_guard

    def __call__(
        self,
        snapshot: CommunityGraphRouteSnapshot,
    ) -> _RestoreProvider:
        if (
            snapshot.scope != "board"
            or snapshot.backend != "grafx"
            or snapshot.page_size is None
        ):
            raise ValueError("grafx_restore_snapshot_invalid")
        board_id = snapshot.scope_id

        def current_path(expected_board_id: str) -> Path:
            self._require_board(expected_board_id, board_id)
            current = self._resolver.revalidate_snapshot(snapshot)
            if current.backend != "grafx" or current.page_size is None:
                raise ValueError("grafx_restore_snapshot_backend_changed")
            return current.active_path

        def open_fixed(path: Path):
            current = self._resolver.revalidate_snapshot(snapshot)
            if not _same_path(path, current.active_path):
                raise ValueError("grafx_restore_open_path_mismatch")
            return self._open_database(current, current.active_path)

        def close_fixed(expected_board_id: str) -> None:
            self._require_board(expected_board_id, board_id)
            self._resolver.revalidate_snapshot(snapshot)
            self._close_board(expected_board_id)
            self._resolver.revalidate_snapshot(snapshot)

        def locked_fixed(expected_board_id: str) -> bool:
            self._require_board(expected_board_id, board_id)
            self._resolver.revalidate_snapshot(snapshot)
            return bool(self._board_is_locked(expected_board_id))

        def fence_fixed(expected_board_id: str, phase: str) -> None:
            self._require_board(expected_board_id, board_id)
            self._resolver.revalidate_snapshot(snapshot)
            self._revalidate_fence(expected_board_id, phase)
            self._resolver.revalidate_snapshot(snapshot)

        def guard_fixed(expected_board_id: str):
            self._require_board(expected_board_id, board_id)
            self._resolver.revalidate_snapshot(snapshot)
            return self._mutation_guard(expected_board_id)

        from okto_pulse.community.adapters.grafx_quarantine_restore import (
            CommunityGrafxQuarantineRestore,
        )

        return CommunityGrafxQuarantineRestore(
            quarantine_root=self._quarantine_root,
            database_path_resolver=current_path,
            open_database=open_fixed,
            close_board=close_fixed,
            board_is_locked=locked_fixed,
            revalidate_fence=fence_fixed,
            mutation_guard=guard_fixed,
        )

    @staticmethod
    def _require_board(observed: str, expected: str) -> None:
        if observed != expected:
            raise ValueError("grafx_restore_snapshot_board_mismatch")


def _refused(
    reason: str,
    *,
    quarantine_id: str,
    **details: object,
) -> QuarantineRestoreError:
    return QuarantineRestoreError(
        QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND,
        reason=reason,
        details={"quarantine_id": quarantine_id, **details},
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _plain_file_bytes(path: Path, *, quarantine_id: str) -> bytes:
    try:
        metadata = path.lstat()
        if is_filesystem_alias(path) or not stat.S_ISREG(metadata.st_mode):
            raise OSError("manifest is not a plain file")
        if metadata.st_size <= 0 or metadata.st_size > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest size is outside the accepted bound")
        with path.open("rb") as stream:
            encoded = stream.read(_MAX_MANIFEST_BYTES + 1)
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ValueError("manifest exceeds the accepted bound")
        return encoded
    except (OSError, ValueError) as failure:
        raise _refused(
            f"quarantine manifest is unreadable: {type(failure).__name__}",
            quarantine_id=quarantine_id,
            manifest_path=str(path),
        ) from failure


def _required_board_id(value: object, *, quarantine_id: str) -> str:
    if type(value) is not str:
        raise _refused("quarantine board_id is invalid", quarantine_id=quarantine_id)
    try:
        board_id = validate_scope_id(value, field_name="board_id")
    except ValueError as failure:
        raise _refused(
            "quarantine board_id is invalid", quarantine_id=quarantine_id
        ) from failure
    if len(board_id) > 128:
        raise _refused("quarantine board_id is invalid", quarantine_id=quarantine_id)
    return board_id


def _required_absolute_path(
    value: object,
    *,
    quarantine_id: str,
    label: str = "Grafx quarantine database_path",
) -> Path:
    if type(value) is not str or not value.strip() or "://" in value:
        raise _refused(
            f"{label} is invalid",
            quarantine_id=quarantine_id,
        )
    path = Path(value)
    if not path.is_absolute() or path == path.parent or not path.name:
        raise _refused(
            f"{label} is invalid",
            quarantine_id=quarantine_id,
        )
    return Path(os.path.abspath(path))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(Path(os.path.abspath(left)))) == os.path.normcase(
        str(Path(os.path.abspath(right)))
    )


def _plain_file_fingerprint(path: Path) -> tuple[int, str]:
    """Hash one stable plain file without following an alias."""

    before = path.lstat()
    if is_filesystem_alias(path) or not stat.S_ISREG(before.st_mode):
        raise OSError("payload is not a plain file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.lstat()
    if (
        is_filesystem_alias(path)
        or not stat.S_ISREG(after.st_mode)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_dev", None) != getattr(after, "st_dev", None)
        or getattr(before, "st_ino", None) != getattr(after, "st_ino", None)
    ):
        raise OSError("payload changed while being authenticated")
    return int(after.st_size), digest.hexdigest()


class CommunityRoutedQuarantineRestore:
    """Route restore calls using one authenticated persisted Board binding."""

    def __init__(
        self,
        resolver: CommunityGraphRouteResolver,
        *,
        quarantine_root: str | os.PathLike[str],
        ladybug: _RestoreProvider,
        grafx_factory: GrafxRestoreFactory | None,
    ) -> None:
        if resolver is None:
            raise TypeError("resolver is required")
        self._resolver = resolver
        self._root = Path(os.path.abspath(Path(quarantine_root).expanduser()))
        if self._root == self._root.parent or not self._root.name:
            raise ValueError("quarantine_root is too broad")
        self._ladybug = ladybug
        self._grafx_factory = grafx_factory

    def plan(self, quarantine_id: str) -> RestorePlan:
        selected = self._select(quarantine_id)
        plan = selected.provider.plan(quarantine_id)
        self._validate_plan(selected, plan, quarantine_id=quarantine_id)
        return plan

    def apply(self, quarantine_id: str) -> RestoreReport:
        # Selection is deliberately repeated here.  A prior dry-run is not an
        # authority for apply and no cached manifest/binding decision survives.
        selected = self._select(quarantine_id)
        report = selected.provider.apply(quarantine_id)
        self._validate_report(selected, report, quarantine_id=quarantine_id)
        return report

    def apply_rebuild_compensation(
        self,
        quarantine_id: str,
        *,
        expected_board_id: str,
        run_id: str,
        owner_token: str | None,
    ) -> RestoreReport:
        selected = self._select(quarantine_id)
        if selected.classification.board_id != expected_board_id:
            raise ValueError("rebuild_compensation_restore_board_mismatch")
        callback = getattr(selected.provider, "apply_rebuild_compensation", None)
        if not callable(callback):
            raise TypeError("rebuild_compensation_restore_unavailable")
        report = callback(
            quarantine_id,
            expected_board_id=expected_board_id,
            run_id=run_id,
            owner_token=owner_token,
        )
        self._validate_report(selected, report, quarantine_id=quarantine_id)
        return report

    def discard_rebuild_candidate(
        self,
        *,
        expected_board_id: str,
        run_id: str,
        owner_token: str | None,
    ) -> dict[str, object]:
        try:
            snapshot = self._resolver.inspect_board_route(expected_board_id)
        except (
            GraphCapabilityUnavailable,
            GraphCorruption,
            GraphLockContention,
            GraphUnavailable,
        ) as failure:
            raise ValueError("rebuild_candidate_discard_route_unavailable") from failure
        self._require_board_snapshot(snapshot, expected_board_id, quarantine_id=None)
        provider = self._provider_for_snapshot(
            snapshot,
            quarantine_id="rebuild-candidate",
        )
        callback = getattr(provider, "discard_rebuild_candidate", None)
        if not callable(callback):
            raise TypeError("rebuild_candidate_discard_unavailable")
        return callback(
            expected_board_id=expected_board_id,
            run_id=run_id,
            owner_token=owner_token,
        )

    def _select(self, quarantine_id: str) -> _SelectedRestore:
        classification = self._classify(quarantine_id)
        try:
            snapshot = self._resolver.inspect_board_route(classification.board_id)
        except GraphCapabilityUnavailable as failure:
            if failure.details.get("reason") == "binding_missing":
                if classification.backend != "ladybug":
                    raise _refused(
                        "Grafx quarantine has no persisted Board binding",
                        quarantine_id=quarantine_id,
                        board_id=classification.board_id,
                    ) from failure
                self._require_legacy_target_safe(
                    classification.board_id,
                    quarantine_id=quarantine_id,
                )
                self._validate_missing_binding_manifest(
                    classification,
                    quarantine_id=quarantine_id,
                )
                return _SelectedRestore(classification, None, self._ladybug)
            raise _refused(
                "persisted Board route was refused",
                quarantine_id=quarantine_id,
                board_id=classification.board_id,
                route_reason=failure.details.get("reason"),
            ) from failure
        except (GraphCorruption, GraphLockContention, GraphUnavailable) as failure:
            raise _refused(
                "persisted Board route is unavailable",
                quarantine_id=quarantine_id,
                board_id=classification.board_id,
                error_type=type(failure).__name__,
            ) from failure

        self._require_board_snapshot(
            snapshot,
            classification.board_id,
            quarantine_id=quarantine_id,
        )
        if snapshot.backend != classification.backend:
            raise _refused(
                "quarantine backend conflicts with the persisted Board binding",
                quarantine_id=quarantine_id,
                board_id=classification.board_id,
                manifest_backend=classification.backend,
                bound_backend=snapshot.backend,
            )
        self._validate_manifest_binding(classification, snapshot, quarantine_id)
        return _SelectedRestore(
            classification,
            snapshot,
            self._provider_for_snapshot(snapshot, quarantine_id=quarantine_id),
        )

    def _require_legacy_target_safe(
        self,
        board_id: str,
        *,
        quarantine_id: str,
    ) -> None:
        target = self._root.parent / "boards" / board_id
        try:
            reject_filesystem_alias_ancestry(target)
        except (OSError, ValueError) as failure:
            raise _refused(
                "legacy Ladybug restore target crosses a filesystem alias",
                quarantine_id=quarantine_id,
                board_id=board_id,
            ) from failure

    def _validate_missing_binding_manifest(
        self,
        classification: _ManifestRoute,
        *,
        quarantine_id: str,
    ) -> None:
        document = classification.document
        if document is None or document.get("kind") != _LADYBUG_WAL_KIND:
            return
        recorded_path = _required_absolute_path(
            document.get("graph_path"),
            quarantine_id=quarantine_id,
            label="Ladybug WAL quarantine graph_path",
        )
        expected_path = (
            self._root.parent / "boards" / classification.board_id / "graph.lbug"
        )
        if not _same_path(recorded_path, expected_path):
            raise _refused(
                "Ladybug WAL quarantine path conflicts with its legacy target",
                quarantine_id=quarantine_id,
                board_id=classification.board_id,
            )

    def _provider_for_snapshot(
        self,
        snapshot: CommunityGraphRouteSnapshot,
        *,
        quarantine_id: str,
    ) -> _RestoreProvider:
        if snapshot.backend == "ladybug":
            return self._ladybug
        factory = self._grafx_factory
        if factory is None:
            raise _refused(
                "Grafx quarantine restore dependencies are not composed",
                quarantine_id=quarantine_id,
                board_id=snapshot.scope_id,
            )
        provider = factory(snapshot)
        if not callable(getattr(provider, "plan", None)) or not callable(
            getattr(provider, "apply", None)
        ):
            raise _refused(
                "Grafx quarantine restore factory returned an invalid provider",
                quarantine_id=quarantine_id,
                board_id=snapshot.scope_id,
            )
        return provider

    @staticmethod
    def _require_board_snapshot(
        snapshot: CommunityGraphRouteSnapshot,
        board_id: str,
        *,
        quarantine_id: str | None,
    ) -> None:
        if (
            snapshot.scope != "board"
            or snapshot.scope_id != board_id
            or snapshot.backend not in {"ladybug", "grafx"}
            or (snapshot.backend == "grafx" and snapshot.page_size is None)
        ):
            if quarantine_id is None:
                raise ValueError("rebuild_candidate_discard_route_invalid")
            raise _refused(
                "persisted Board route snapshot is invalid",
                quarantine_id=quarantine_id,
                board_id=board_id,
            )

    def _validate_manifest_binding(
        self,
        classification: _ManifestRoute,
        snapshot: CommunityGraphRouteSnapshot,
        quarantine_id: str,
    ) -> None:
        document = classification.document
        if document is None:
            return
        if classification.backend == "ladybug":
            if document.get("kind") != _LADYBUG_WAL_KIND:
                return
            recorded_path = _required_absolute_path(
                document.get("graph_path"),
                quarantine_id=quarantine_id,
                label="Ladybug WAL quarantine graph_path",
            )
            if not _same_path(recorded_path, snapshot.active_path):
                raise _refused(
                    "Ladybug WAL quarantine path conflicts with the persisted Board binding",
                    quarantine_id=quarantine_id,
                    board_id=classification.board_id,
                )
            return
        recorded_path = _required_absolute_path(
            document.get("database_path"), quarantine_id=quarantine_id
        )
        if not _same_path(recorded_path, snapshot.active_path):
            raise _refused(
                "Grafx quarantine path conflicts with the persisted Board binding",
                quarantine_id=quarantine_id,
                board_id=classification.board_id,
            )
        if document.get("kind") == GRAFX_DIRECTORY_QUARANTINE_KIND:
            generation = document.get("generation")
            binding_sha256 = document.get("binding_sha256")
            if (
                generation != snapshot.generation
                or binding_sha256 != snapshot.binding_sha256
            ):
                raise _refused(
                    "Grafx quarantine generation or binding digest is stale",
                    quarantine_id=quarantine_id,
                    board_id=classification.board_id,
                )

    def _classify(self, quarantine_id: str) -> _ManifestRoute:
        directory = self._quarantine_directory(quarantine_id)
        manifest_json = directory / _MANIFEST_JSON
        manifest_text = directory / _MANIFEST_TEXT
        json_present = self._plain_path_present(manifest_json, quarantine_id)
        text_present = self._plain_path_present(manifest_text, quarantine_id)
        if json_present and text_present:
            raise _refused(
                "quarantine contains mixed manifest formats",
                quarantine_id=quarantine_id,
            )
        if json_present:
            document = self._read_json_manifest(manifest_json, quarantine_id)
            route = self._classify_json(document, quarantine_id)
            self._validate_backend_namespace(directory, route, quarantine_id)
            return route
        if text_present:
            route = self._classify_legacy_text(
                manifest_text,
                quarantine_id=quarantine_id,
            )
            self._validate_backend_namespace(directory, route, quarantine_id)
            return route
        raise _refused(
            "quarantine has no recognised manifest",
            quarantine_id=quarantine_id,
        )

    def _quarantine_directory(self, quarantine_id: str) -> Path:
        if (
            type(quarantine_id) is not str
            or _QUARANTINE_ID_RE.fullmatch(quarantine_id) is None
        ):
            raise _refused("quarantine_id is invalid", quarantine_id=str(quarantine_id))
        directory = self._root / quarantine_id
        try:
            reject_filesystem_alias_ancestry(self._root)
            metadata = directory.lstat()
            if is_filesystem_alias(directory) or not stat.S_ISDIR(metadata.st_mode):
                raise OSError("quarantine is not a plain directory")
            children = tuple(directory.iterdir())
            if len(children) > _MAX_QUARANTINE_ENTRIES:
                raise ValueError("quarantine entry count exceeds the accepted bound")
            for child in children:
                if is_filesystem_alias(child):
                    raise _refused(
                        "quarantine entry is an unsafe filesystem alias",
                        quarantine_id=quarantine_id,
                        entry=child.name,
                    )
        except QuarantineRestoreError:
            raise
        except (OSError, ValueError) as failure:
            label = (
                "filesystem alias ancestry"
                if "alias" in str(failure)
                else "unavailable"
            )
            raise _refused(
                f"quarantine is {label}: {type(failure).__name__}",
                quarantine_id=quarantine_id,
                quarantine_dir=str(directory),
            ) from failure
        return directory

    @staticmethod
    def _plain_path_present(path: Path, quarantine_id: str) -> bool:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as failure:
            raise _refused(
                "quarantine manifest could not be inspected",
                quarantine_id=quarantine_id,
                manifest_path=str(path),
            ) from failure
        if is_filesystem_alias(path) or not stat.S_ISREG(metadata.st_mode):
            raise _refused(
                "quarantine manifest is not a plain file",
                quarantine_id=quarantine_id,
                manifest_path=str(path),
            )
        return True

    @staticmethod
    def _read_json_manifest(path: Path, quarantine_id: str) -> Mapping[str, object]:
        encoded = _plain_file_bytes(path, quarantine_id=quarantine_id)
        try:
            document = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_strict_object,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as failure:
            detail = str(failure)
            raise _refused(
                f"quarantine JSON manifest is invalid: {detail or type(failure).__name__}",
                quarantine_id=quarantine_id,
                manifest_path=str(path),
            ) from failure
        if type(document) is not dict:
            raise _refused(
                "quarantine JSON manifest is not an object",
                quarantine_id=quarantine_id,
            )
        return document

    def _classify_json(
        self,
        document: Mapping[str, object],
        quarantine_id: str,
    ) -> _ManifestRoute:
        manifest_format = document.get("format")
        kind = document.get("kind")
        if (
            manifest_format == GRAFX_DIRECTORY_QUARANTINE_FORMAT
            and kind == GRAFX_DIRECTORY_QUARANTINE_KIND
        ):
            self._reject_hybrid_markers(
                document,
                backend="grafx",
                quarantine_id=quarantine_id,
            )
            board_id = self._validate_grafx_directory_header(document, quarantine_id)
            return _ManifestRoute(
                "grafx", board_id, GRAFX_DIRECTORY_QUARANTINE_FORMAT, document
            )
        if manifest_format == _GRAFX_WAL_FORMAT and kind in _GRAFX_WAL_KINDS:
            self._reject_hybrid_markers(
                document,
                backend="grafx",
                quarantine_id=quarantine_id,
            )
            board_id = self._validate_grafx_wal_header(document, quarantine_id)
            return _ManifestRoute("grafx", board_id, _GRAFX_WAL_FORMAT, document)

        grafx_poisoned = (
            (
                manifest_format is not None
                and type(manifest_format) is str
                and "grafx" in manifest_format.casefold()
            )
            or (kind is not None and type(kind) is str and "grafx" in kind.casefold())
            or bool(set(document).intersection(_GRAFX_EXCLUSIVE_MARKERS))
        )
        if grafx_poisoned:
            raise _refused(
                "quarantine has incomplete, mixed, or unknown Grafx markers",
                quarantine_id=quarantine_id,
            )
        if manifest_format is not None:
            raise _refused(
                "quarantine manifest format is unsupported",
                quarantine_id=quarantine_id,
            )
        self._reject_hybrid_markers(
            document,
            backend="ladybug",
            quarantine_id=quarantine_id,
        )
        return self._classify_ladybug_json(document, quarantine_id)

    @staticmethod
    def _reject_hybrid_markers(
        document: Mapping[str, object],
        *,
        backend: ManifestBackend,
        quarantine_id: str,
    ) -> None:
        forbidden = (
            _LADYBUG_EXCLUSIVE_MARKERS
            if backend == "grafx"
            else _GRAFX_EXCLUSIVE_MARKERS
        )
        conflicts = sorted(set(document).intersection(forbidden))
        if conflicts:
            other = "Ladybug" if backend == "grafx" else "Grafx"
            raise _refused(
                f"quarantine mixes {backend} and {other} manifest markers",
                quarantine_id=quarantine_id,
                conflicting_markers=conflicts,
            )

    @staticmethod
    def _validate_grafx_wal_header(
        document: Mapping[str, object], quarantine_id: str
    ) -> str:
        board_id = _required_board_id(
            document.get("board_id"), quarantine_id=quarantine_id
        )
        if (
            document.get("quarantine_id") != quarantine_id
            or document.get("complete") is not True
            or document.get("main_untouched") is not True
            or type(document.get("files")) is not list
            or not document.get("files")
        ):
            raise _refused(
                "Grafx WAL quarantine header is incomplete",
                quarantine_id=quarantine_id,
            )
        _required_absolute_path(
            document.get("database_path"), quarantine_id=quarantine_id
        )
        seen: set[str] = set()
        for raw in document["files"]:
            if type(raw) is not dict:
                raise _refused(
                    "Grafx WAL quarantine inventory is invalid",
                    quarantine_id=quarantine_id,
                )
            relative = raw.get("relative_path")
            size = raw.get("size_bytes")
            digest = raw.get("sha256")
            if (
                type(relative) is not str
                or _GRAFX_WAL_RELATIVE_RE.fullmatch(relative) is None
                or relative in seen
                or type(size) is not int
                or size < 0
                or type(digest) is not str
                or _SHA256_RE.fullmatch(digest) is None
            ):
                raise _refused(
                    "Grafx WAL quarantine inventory is invalid or duplicated",
                    quarantine_id=quarantine_id,
                )
            seen.add(relative)
        return board_id

    @staticmethod
    def _validate_grafx_directory_header(
        document: Mapping[str, object], quarantine_id: str
    ) -> str:
        board_id = _required_board_id(
            document.get("board_id"), quarantine_id=quarantine_id
        )
        generation = document.get("generation")
        binding_digest = document.get("binding_sha256")
        inventory_digest = document.get("inventory_sha256")
        manifest_digest = document.get("manifest_sha256")
        if (
            document.get("quarantine_id") != quarantine_id
            or document.get("complete") is not True
            or document.get("phase") != "captured"
            or type(generation) is not str
            or not generation
            or type(binding_digest) is not str
            or _SHA256_RE.fullmatch(binding_digest) is None
            or type(inventory_digest) is not str
            or _SHA256_RE.fullmatch(inventory_digest) is None
            or type(manifest_digest) is not str
            or _SHA256_RE.fullmatch(manifest_digest) is None
            or document.get("payload_relative") != GRAFX_DIRECTORY_PAYLOAD
            or type(document.get("directories")) is not list
            or type(document.get("files")) is not list
            or not document.get("files")
        ):
            raise _refused(
                "Grafx directory quarantine header is incomplete",
                quarantine_id=quarantine_id,
            )
        _required_absolute_path(
            document.get("database_path"), quarantine_id=quarantine_id
        )
        try:
            safe_generation = validate_scope_id(
                generation,
                field_name="generation",
            )
        except ValueError as failure:
            raise _refused(
                "Grafx directory quarantine generation is invalid",
                quarantine_id=quarantine_id,
            ) from failure
        if safe_generation != generation:
            raise _refused(
                "Grafx directory quarantine generation is invalid",
                quarantine_id=quarantine_id,
            )

        raw_directories = document["directories"]
        directories: list[str] = []
        for raw in raw_directories:
            if type(raw) is not str:
                raise _refused(
                    "Grafx directory quarantine inventory is invalid",
                    quarantine_id=quarantine_id,
                )
            relative = PurePosixPath(raw)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or relative.as_posix() != raw
            ):
                raise _refused(
                    "Grafx directory quarantine inventory path is unsafe",
                    quarantine_id=quarantine_id,
                )
            directories.append(raw)
        if directories != sorted(set(directories)):
            raise _refused(
                "Grafx directory quarantine directories are not canonical",
                quarantine_id=quarantine_id,
            )

        files: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in document["files"]:
            if type(raw) is not dict:
                raise _refused(
                    "Grafx directory quarantine file inventory is invalid",
                    quarantine_id=quarantine_id,
                )
            relative = raw.get("relative_path")
            size = raw.get("size_bytes")
            digest = raw.get("sha256")
            path = PurePosixPath(relative) if type(relative) is str else None
            if (
                path is None
                or path.is_absolute()
                or not path.parts
                or ".." in path.parts
                or path.as_posix() != relative
                or relative in seen
                or type(size) is not int
                or size < 0
                or type(digest) is not str
                or _SHA256_RE.fullmatch(digest) is None
            ):
                raise _refused(
                    "Grafx directory quarantine file inventory is unsafe",
                    quarantine_id=quarantine_id,
                )
            seen.add(relative)
            files.append(
                {
                    "relative_path": relative,
                    "size_bytes": size,
                    "sha256": digest,
                }
            )
        if files != sorted(files, key=lambda item: str(item["relative_path"])):
            raise _refused(
                "Grafx directory quarantine files are not canonical",
                quarantine_id=quarantine_id,
            )
        if inventory_digest != _canonical_sha256(
            {"directories": directories, "files": files}
        ):
            raise _refused(
                "Grafx directory quarantine inventory digest is invalid",
                quarantine_id=quarantine_id,
            )
        authenticated = {
            key: value for key, value in document.items() if key != "manifest_sha256"
        }
        if manifest_digest != _canonical_sha256(authenticated):
            raise _refused(
                "Grafx directory quarantine manifest authentication failed",
                quarantine_id=quarantine_id,
            )
        return board_id

    @staticmethod
    def _classify_ladybug_json(
        document: Mapping[str, object], quarantine_id: str
    ) -> _ManifestRoute:
        kind = document.get("kind")
        if kind not in {None, _LADYBUG_WAL_KIND}:
            raise _refused(
                "Ladybug quarantine kind is unsupported",
                quarantine_id=quarantine_id,
            )
        if kind == _LADYBUG_WAL_KIND:
            board_id = CommunityRoutedQuarantineRestore._validate_ladybug_wal_header(
                document,
                quarantine_id,
            )
            return _ManifestRoute("ladybug", board_id, _MANIFEST_JSON, document)

        affected = document.get("affected_paths_relative")
        if (
            document.get("quarantine_id") != quarantine_id
            or document.get("graph_type") != "board_graph"
            or type(affected) is not list
            or not affected
            or type(document.get("files_moved")) is not int
            or document.get("files_moved") != len(affected)
        ):
            raise _refused(
                "Ladybug quarantine header is incomplete",
                quarantine_id=quarantine_id,
            )
        board_id = _required_board_id(
            document.get("board_id"), quarantine_id=quarantine_id
        )
        names: list[str] = []
        for value in affected:
            if type(value) is not str:
                raise _refused(
                    "Ladybug quarantine inventory is invalid",
                    quarantine_id=quarantine_id,
                )
            relative = PurePosixPath(value)
            if (
                relative.is_absolute()
                or len(relative.parts) != 1
                or relative.as_posix() != value
                or value not in _LADYBUG_FILE_NAMES
            ):
                raise _refused(
                    "Ladybug quarantine inventory is not canonical",
                    quarantine_id=quarantine_id,
                )
            names.append(value)
        if len(names) != len(set(names)):
            raise _refused(
                "Ladybug quarantine inventory contains duplicates",
                quarantine_id=quarantine_id,
            )
        return _ManifestRoute("ladybug", board_id, _MANIFEST_JSON, document)

    @staticmethod
    def _validate_ladybug_wal_header(
        document: Mapping[str, object],
        quarantine_id: str,
    ) -> str:
        if set(document) != _LADYBUG_WAL_FIELDS:
            raise _refused(
                "Ladybug WAL quarantine schema is not canonical",
                quarantine_id=quarantine_id,
            )
        board_id = _required_board_id(
            document.get("board_id"), quarantine_id=quarantine_id
        )
        reason = document.get("reason")
        created_at = document.get("created_at")
        planned = document.get("planned_files")
        moved = document.get("files")
        if (
            document.get("quarantine_id") != quarantine_id
            or document.get("main_untouched") is not True
            or document.get("error") is not None
            or type(reason) is not str
            or not reason.strip()
            or type(created_at) is not str
            or not created_at.strip()
            or type(planned) is not list
            or not planned
            or type(moved) is not list
            or not moved
        ):
            raise _refused(
                "Ladybug WAL quarantine header is incomplete or non-terminal",
                quarantine_id=quarantine_id,
            )

        graph_path = _required_absolute_path(
            document.get("graph_path"),
            quarantine_id=quarantine_id,
            label="Ladybug WAL quarantine graph_path",
        )
        try:
            reject_filesystem_alias_ancestry(graph_path)
        except (OSError, ValueError) as failure:
            raise _refused(
                "Ladybug WAL quarantine graph_path crosses a filesystem alias",
                quarantine_id=quarantine_id,
            ) from failure
        if (
            graph_path.name != "graph.lbug"
            or graph_path.parent.name != board_id
            or document.get("main_file") != graph_path.name
        ):
            raise _refused(
                "Ladybug WAL quarantine main path or identity is invalid",
                quarantine_id=quarantine_id,
            )

        planned_names: list[str] = []
        for raw in planned:
            if type(raw) is not dict or set(raw) != _LADYBUG_WAL_INVENTORY_FIELDS:
                raise _refused(
                    "Ladybug WAL quarantine planned inventory is invalid",
                    quarantine_id=quarantine_id,
                )
            name = raw.get("name")
            size = raw.get("size")
            digest = raw.get("sha256")
            if (
                type(name) is not str
                or name not in _LADYBUG_WAL_FILE_NAMES
                or name in planned_names
                or type(size) is not int
                or size < 0
                or type(digest) is not str
                or _SHA256_RE.fullmatch(digest) is None
            ):
                raise _refused(
                    "Ladybug WAL quarantine planned inventory is invalid or duplicated",
                    quarantine_id=quarantine_id,
                )
            planned_names.append(name)

        moved_names: list[str] = []
        for name in moved:
            if (
                type(name) is not str
                or name not in _LADYBUG_WAL_FILE_NAMES
                or name in moved_names
            ):
                raise _refused(
                    "Ladybug WAL quarantine moved inventory is invalid or duplicated",
                    quarantine_id=quarantine_id,
                )
            moved_names.append(name)
        if moved_names != planned_names:
            raise _refused(
                "Ladybug WAL quarantine planned and moved inventories differ",
                quarantine_id=quarantine_id,
            )
        return board_id

    @staticmethod
    def _classify_legacy_text(path: Path, *, quarantine_id: str) -> _ManifestRoute:
        encoded = _plain_file_bytes(path, quarantine_id=quarantine_id)
        try:
            text = encoded.decode("utf-8")
        except UnicodeError as failure:
            raise _refused(
                "legacy Ladybug manifest is not UTF-8",
                quarantine_id=quarantine_id,
            ) from failure
        if (
            not quarantine_id.startswith("interrupted-checkpoint-")
            or "Sidecars orfaos de checkpoint interrompido" not in text
            or "Main file preservado no lugar." not in text
            or not any(name in text for name in _LADYBUG_FILE_NAMES - {"graph.lbug"})
        ):
            raise _refused(
                "legacy Ladybug text manifest is not recognised",
                quarantine_id=quarantine_id,
            )
        match = _UUID_RE.search(quarantine_id) or _UUID_RE.search(text)
        if match is None:
            raise _refused(
                "legacy Ladybug text manifest has no board identity",
                quarantine_id=quarantine_id,
            )
        return _ManifestRoute("ladybug", match.group(0), _MANIFEST_TEXT, None)

    @staticmethod
    def _validate_backend_namespace(
        directory: Path,
        route: _ManifestRoute,
        quarantine_id: str,
    ) -> None:
        entries = {entry.name: entry for entry in directory.iterdir()}
        if route.backend == "ladybug":
            allowed_metadata = {_MANIFEST_JSON, _MANIFEST_TEXT, _RESTORE_OPERATION}
            payload_names = set(entries).difference(allowed_metadata)
            document = route.document
            if document is not None and document.get("kind") is None:
                expected = set(document.get("affected_paths_relative", []))
                if payload_names != expected:
                    raise _refused(
                        "Ladybug quarantine payload does not match its inventory",
                        quarantine_id=quarantine_id,
                    )
            elif document is not None and document.get("kind") == _LADYBUG_WAL_KIND:
                expected = set(document.get("files", []))
                if payload_names != expected:
                    raise _refused(
                        "Ladybug WAL quarantine payload does not match its moved inventory",
                        quarantine_id=quarantine_id,
                    )
            if not payload_names or not payload_names.issubset(_LADYBUG_FILE_NAMES):
                raise _refused(
                    "Ladybug quarantine contains an alternate-backend payload",
                    quarantine_id=quarantine_id,
                )
            for name in payload_names:
                try:
                    if not stat.S_ISREG(entries[name].lstat().st_mode):
                        raise OSError(name)
                except OSError as failure:
                    raise _refused(
                        "Ladybug quarantine payload is not a plain file",
                        quarantine_id=quarantine_id,
                    ) from failure
            if document is not None and document.get("kind") == _LADYBUG_WAL_KIND:
                planned = {
                    str(item["name"]): item
                    for item in document["planned_files"]
                    if type(item) is dict
                }
                for name in payload_names:
                    try:
                        size, digest = _plain_file_fingerprint(entries[name])
                    except OSError as failure:
                        raise _refused(
                            "Ladybug WAL quarantine payload could not be authenticated",
                            quarantine_id=quarantine_id,
                            payload=name,
                        ) from failure
                    expected = planned[name]
                    if size != expected["size"] or digest != expected["sha256"]:
                        raise _refused(
                            "Ladybug WAL quarantine payload failed size or digest integrity",
                            quarantine_id=quarantine_id,
                            payload=name,
                        )
            return

        allowed = {_MANIFEST_JSON, _RESTORE_OPERATION}
        if route.manifest_format == GRAFX_DIRECTORY_QUARANTINE_FORMAT:
            payload_name = GRAFX_DIRECTORY_PAYLOAD.split("/", 1)[0]
        else:
            payload_name = "payload"
        allowed.add(payload_name)
        if set(entries).difference(allowed):
            raise _refused(
                "Grafx quarantine contains an alternate-backend payload",
                quarantine_id=quarantine_id,
            )
        payload = entries.get(payload_name)
        if payload is None:
            raise _refused(
                "Grafx quarantine payload is missing",
                quarantine_id=quarantine_id,
            )
        try:
            if not stat.S_ISDIR(payload.lstat().st_mode):
                raise OSError(payload_name)
        except OSError as failure:
            raise _refused(
                "Grafx quarantine payload is not a plain directory",
                quarantine_id=quarantine_id,
            ) from failure

    def _validate_plan(
        self,
        selected: _SelectedRestore,
        plan: RestorePlan,
        *,
        quarantine_id: str,
    ) -> None:
        if (
            plan.quarantine_id != quarantine_id
            or plan.board_id != selected.classification.board_id
        ):
            raise _refused(
                "restore provider returned a mismatched plan",
                quarantine_id=quarantine_id,
            )
        snapshot = selected.snapshot
        expected_board_dir = (
            self._root.parent / "boards" / plan.board_id
            if snapshot is None
            else (
                snapshot.active_path
                if snapshot.backend == "grafx"
                else snapshot.active_path.parent
            )
        )
        if not _same_path(Path(plan.board_dir), expected_board_dir):
            raise _refused(
                "restore provider plan conflicts with the persisted route",
                quarantine_id=quarantine_id,
                board_id=plan.board_id,
            )

    @staticmethod
    def _validate_report(
        selected: _SelectedRestore,
        report: RestoreReport,
        *,
        quarantine_id: str,
    ) -> None:
        if (
            report.quarantine_id != quarantine_id
            or report.board_id != selected.classification.board_id
        ):
            raise _refused(
                "restore provider returned a mismatched report",
                quarantine_id=quarantine_id,
            )


__all__ = [
    "CommunityGrafxSnapshotRestoreFactory",
    "CommunityRoutedQuarantineRestore",
    "GrafxRestoreFactory",
    "SnapshotDatabaseOpener",
]
