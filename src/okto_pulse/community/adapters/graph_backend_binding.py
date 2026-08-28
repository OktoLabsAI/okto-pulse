"""Persisted, fail-closed graph-backend bindings for Community runtimes.

The binding is deliberately smaller than the M-PULSE-7 cutover protocol.  It
can initialize one scope idempotently and acquire an immutable snapshot, but it
cannot replace a different binding.  A future CAS cutover therefore cannot be
accidentally approximated by overwriting this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

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
    fsync_directory,
    validate_scope_id,
)
from okto_pulse.community.config import (
    PULSE_GRAFX_DEFAULT_PAGE_SIZE,
    PULSE_GRAFX_MIN_PAGE_SIZE,
    validate_grafx_page_size,
)

GraphBackend = Literal["ladybug", "grafx"]
GraphBindingScope = Literal["board", "global"]

BINDING_FORMAT = "okto-pulse-community-graph-binding/1"
BOARD_BINDING_FILENAME = "graph_backend_binding.json"
GLOBAL_BINDING_FILENAME = "graph_backend_binding.json"
MAX_BINDING_BYTES = 16 * 1024

_BACKENDS: frozenset[str] = frozenset({"ladybug", "grafx"})
_BINDING_KEYS: frozenset[str] = frozenset(
    {
        "binding_format",
        "scope",
        "scope_id",
        "backend",
        "generation",
        "physical_path",
        "page_size",
        "binding_sha256",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_SEGMENT_FORBIDDEN = frozenset('<>:"|?*')
_WINDOWS_RESERVED_SEGMENTS: frozenset[str] = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


@dataclass(frozen=True, slots=True)
class CommunityGraphBackendBinding:
    """One backend/path decision pinned for the lifetime of a caller scope."""

    scope: GraphBindingScope
    scope_id: str
    backend: GraphBackend
    generation: str
    physical_path: Path
    page_size: int | None
    binding_sha256: str


@dataclass(frozen=True, slots=True)
class GrafxDatabaseAdmission:
    """Evidence that one already-open Grafx database has safe Pulse geometry."""

    page_size: int
    minimum_page_size: int


def _capability(reason: str, *, operation: str, **details: object) -> Exception:
    return GraphCapabilityUnavailable(
        "The Community graph backend binding was refused.",
        details={
            "operation": operation,
            "reason": reason,
            **details,
        },
    )


def _corruption(reason: str, *, scope: str, scope_id: str) -> Exception:
    return GraphCorruption(
        "The persisted Community graph backend binding is invalid.",
        details={
            "operation": "acquire_graph_backend_binding",
            "reason": reason,
            "scope": scope,
            "scope_id": scope_id,
        },
    )


def _unavailable(
    reason: str,
    *,
    operation: str,
    scope: str,
    scope_id: str,
    error_type: str | None = None,
) -> Exception:
    details: dict[str, object] = {
        "operation": operation,
        "reason": reason,
        "scope": scope,
        "scope_id": scope_id,
    }
    if error_type is not None:
        details["error_type"] = error_type
    return GraphUnavailable(
        "The Community graph backend binding is unavailable.",
        details=details,
    )


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _binding_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def _validate_portable_segment(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name}_not_string")
    normalized = validate_scope_id(value, field_name=field_name)
    if (
        len(normalized) > 128
        or normalized[-1:] in {".", " "}
        or normalized.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_SEGMENTS
        or any(character in _PORTABLE_SEGMENT_FORBIDDEN for character in normalized)
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(f"{field_name}_not_portable")
    return normalized


def _validate_backend(value: object) -> GraphBackend:
    if type(value) is not str or value not in _BACKENDS:
        raise ValueError("backend_invalid")
    return value


def _canonical_root(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not raw.strip() or "://" in raw:
        raise _capability(
            "kg_base_dir_not_local",
            operation="configure_graph_backend_bindings",
        )
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise _capability(
            "kg_base_dir_not_absolute",
            operation="configure_graph_backend_bindings",
        )
    lexical = Path(os.path.abspath(expanded))
    try:
        resolved = expanded.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _capability(
            "kg_base_dir_unresolvable",
            operation="configure_graph_backend_bindings",
        ) from exc
    if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
        raise _capability(
            "kg_base_dir_alias_refused",
            operation="configure_graph_backend_bindings",
        )
    return resolved


def _canonical_physical_path(
    root: Path,
    value: str | os.PathLike[str],
    *,
    scope: GraphBindingScope,
    scope_id: str,
    backend: GraphBackend,
    generation: str,
) -> Path:
    raw = os.fspath(value)
    if not raw.strip() or "://" in raw:
        raise ValueError("physical_path_not_local")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise ValueError("physical_path_not_absolute")
    lexical = contained_lexical_path(root, Path(os.path.abspath(candidate)))
    resolved = candidate.resolve(strict=False)
    if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
        raise ValueError("physical_path_alias_refused")

    relative = lexical.relative_to(root)
    parts = relative.parts
    if scope == "board":
        required_prefix = ("boards", scope_id)
    else:
        required_prefix = ("global",)
    if tuple(parts[: len(required_prefix)]) != required_prefix:
        raise ValueError("physical_path_scope_mismatch")

    lowered = tuple(part.casefold() for part in parts)
    if backend == "grafx":
        expected = (
            root / "boards" / scope_id / "grafx" / generation
            if scope == "board"
            else root / "global" / "grafx" / generation
        )
        if os.path.normcase(str(lexical)) != os.path.normcase(str(expected)):
            raise ValueError("grafx_physical_path_not_canonical")
    else:
        if "grafx" in lowered or lexical.suffix.casefold() != ".lbug":
            raise ValueError("ladybug_physical_path_not_canonical")
        if scope == "board":
            expected = root / "boards" / scope_id / "graph.lbug"
            if os.path.normcase(str(lexical)) != os.path.normcase(str(expected)):
                raise ValueError("ladybug_physical_path_not_canonical")
        else:
            legacy = root / "global" / "discovery.lbug"
            generation_path = (
                root
                / "global"
                / "discovery.generations"
                / generation
                / "discovery.lbug"
            )
            if os.path.normcase(str(lexical)) not in {
                os.path.normcase(str(legacy)),
                os.path.normcase(str(generation_path)),
            }:
                raise ValueError("ladybug_physical_path_not_canonical")
    return lexical


def _require_physical_database(
    path: Path,
    *,
    backend: GraphBackend,
    scope: GraphBindingScope,
    scope_id: str,
) -> None:
    try:
        exists_as_expected = path.is_dir() if backend == "grafx" else path.is_file()
    except OSError as exc:
        raise _unavailable(
            "physical_path_probe_failed",
            operation="acquire_graph_backend_binding",
            scope=scope,
            scope_id=scope_id,
            error_type=type(exc).__name__,
        ) from exc
    if not exists_as_expected:
        raise _unavailable(
            "physical_database_missing",
            operation="acquire_graph_backend_binding",
            scope=scope,
            scope_id=scope_id,
        )


def admit_grafx_database(
    database: object,
    *,
    expected_page_size: int,
    operation: str,
    expected_path: Path | None = None,
) -> GrafxDatabaseAdmission:
    """Fail before schema mutation when persisted Grafx geometry is unsafe."""

    try:
        configured_page_size = validate_grafx_page_size(expected_page_size)
    except ValueError as exc:
        raise _capability(
            "grafx_page_size_configuration_invalid",
            operation=operation,
            backend="okto_grafx",
        ) from exc

    try:
        identity = database.identity
        observed_page_size = identity.page_size
    except Exception as exc:
        raise GraphUnavailable(
            "The Grafx database identity could not be inspected.",
            details={
                "backend": "okto_grafx",
                "operation": operation,
                "reason": "grafx_identity_unavailable",
                "error_type": type(exc).__name__,
            },
        ) from exc

    if type(observed_page_size) is not int:
        raise _capability(
            "grafx_persisted_page_size_invalid",
            operation=operation,
            backend="okto_grafx",
        )
    if observed_page_size < PULSE_GRAFX_MIN_PAGE_SIZE:
        raise _capability(
            "grafx_page_size_below_pulse_minimum",
            operation=operation,
            backend="okto_grafx",
            observed_page_size=observed_page_size,
            minimum_page_size=PULSE_GRAFX_MIN_PAGE_SIZE,
        )
    try:
        validate_grafx_page_size(observed_page_size)
    except ValueError as exc:
        raise _capability(
            "grafx_persisted_page_size_invalid",
            operation=operation,
            backend="okto_grafx",
        ) from exc
    if observed_page_size != configured_page_size:
        raise _capability(
            "grafx_page_size_configuration_mismatch",
            operation=operation,
            backend="okto_grafx",
            observed_page_size=observed_page_size,
            configured_page_size=configured_page_size,
        )

    if expected_path is not None:
        try:
            observed_path = Path(str(database.path)).resolve(strict=False)
            expected_resolved_path = expected_path.resolve(strict=False)
        except Exception as exc:
            raise _capability(
                "grafx_database_path_unavailable",
                operation=operation,
                backend="okto_grafx",
            ) from exc
        if os.path.normcase(str(observed_path)) != os.path.normcase(
            str(expected_resolved_path)
        ):
            raise _capability(
                "grafx_database_path_mismatch",
                operation=operation,
                backend="okto_grafx",
            )

    return GrafxDatabaseAdmission(
        page_size=observed_page_size,
        minimum_page_size=PULSE_GRAFX_MIN_PAGE_SIZE,
    )


class CommunityGraphBackendBindingStore:
    """Initialize and acquire immutable graph bindings below one data root."""

    def __init__(
        self,
        kg_base_dir: str | os.PathLike[str],
        *,
        lock_timeout_seconds: float = 10.0,
    ) -> None:
        self._root = _canonical_root(kg_base_dir)
        if (
            isinstance(lock_timeout_seconds, bool)
            or not isinstance(lock_timeout_seconds, (int, float))
            or lock_timeout_seconds <= 0
        ):
            raise _capability(
                "binding_lock_timeout_invalid",
                operation="configure_graph_backend_bindings",
            )
        self._lock_timeout_seconds = float(lock_timeout_seconds)

    @property
    def root(self) -> Path:
        return self._root

    def board_ladybug_path(self, board_id: str) -> Path:
        safe_board_id = self._validated_segment(board_id, field_name="board_id")
        return self._root / "boards" / safe_board_id / "graph.lbug"

    def board_grafx_path(self, board_id: str, generation: str) -> Path:
        safe_board_id = self._validated_segment(board_id, field_name="board_id")
        safe_generation = self._validated_segment(generation, field_name="generation")
        return self._root / "boards" / safe_board_id / "grafx" / safe_generation

    def global_ladybug_path(self) -> Path:
        return self._root / "global" / "discovery.lbug"

    def global_ladybug_generation_path(self, generation: str) -> Path:
        safe_generation = self._validated_segment(generation, field_name="generation")
        return (
            self._root
            / "global"
            / "discovery.generations"
            / safe_generation
            / "discovery.lbug"
        )

    def global_grafx_path(self, generation: str) -> Path:
        safe_generation = self._validated_segment(generation, field_name="generation")
        return self._root / "global" / "grafx" / safe_generation

    def initialize_board_binding(
        self,
        *,
        board_id: str,
        backend: GraphBackend,
        generation: str,
        physical_path: str | os.PathLike[str],
        page_size: int | None = None,
        database: object | None = None,
    ) -> CommunityGraphBackendBinding:
        safe_board_id = self._validated_segment(board_id, field_name="board_id")
        return self._initialize(
            scope="board",
            scope_id=safe_board_id,
            backend=backend,
            generation=generation,
            physical_path=physical_path,
            page_size=page_size,
            database=database,
        )

    def initialize_global_binding(
        self,
        *,
        backend: GraphBackend,
        generation: str,
        physical_path: str | os.PathLike[str],
        page_size: int | None = None,
        database: object | None = None,
    ) -> CommunityGraphBackendBinding:
        return self._initialize(
            scope="global",
            scope_id="global",
            backend=backend,
            generation=generation,
            physical_path=physical_path,
            page_size=page_size,
            database=database,
        )

    def acquire_board_binding(self, board_id: str) -> CommunityGraphBackendBinding:
        safe_board_id = self._validated_segment(board_id, field_name="board_id")
        return self._acquire(
            self._board_binding_path(safe_board_id),
            scope="board",
            scope_id=safe_board_id,
        )

    def acquire_global_binding(self) -> CommunityGraphBackendBinding:
        return self._acquire(
            self._global_binding_path(),
            scope="global",
            scope_id="global",
        )

    @staticmethod
    def admit_database(
        binding: CommunityGraphBackendBinding,
        database: object,
        *,
        operation: str,
    ) -> GrafxDatabaseAdmission:
        if binding.backend != "grafx" or binding.page_size is None:
            raise _capability(
                "grafx_admission_requires_grafx_binding",
                operation=operation,
            )
        return admit_grafx_database(
            database,
            expected_page_size=binding.page_size,
            expected_path=binding.physical_path,
            operation=operation,
        )

    @staticmethod
    def _validated_segment(value: object, *, field_name: str) -> str:
        try:
            return _validate_portable_segment(value, field_name=field_name)
        except (TypeError, ValueError) as exc:
            raise _capability(
                f"{field_name}_invalid",
                operation="resolve_graph_backend_binding",
                field=field_name,
            ) from exc

    def _board_binding_path(self, board_id: str) -> Path:
        return self._root / "boards" / board_id / BOARD_BINDING_FILENAME

    def _global_binding_path(self) -> Path:
        return self._root / "global" / GLOBAL_BINDING_FILENAME

    def _initialize(
        self,
        *,
        scope: GraphBindingScope,
        scope_id: str,
        backend: object,
        generation: object,
        physical_path: str | os.PathLike[str],
        page_size: object,
        database: object | None,
    ) -> CommunityGraphBackendBinding:
        try:
            safe_backend = _validate_backend(backend)
            safe_generation = _validate_portable_segment(
                generation, field_name="generation"
            )
            safe_path = _canonical_physical_path(
                self._root,
                physical_path,
                scope=scope,
                scope_id=scope_id,
                backend=safe_backend,
                generation=safe_generation,
            )
            if safe_backend == "grafx":
                effective_page_size = validate_grafx_page_size(
                    PULSE_GRAFX_DEFAULT_PAGE_SIZE if page_size is None else page_size
                )
            elif page_size is not None:
                raise ValueError("ladybug_page_size_must_be_null")
            else:
                effective_page_size = None
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _capability(
                "binding_argument_invalid",
                operation="initialize_graph_backend_binding",
                scope=scope,
                scope_id=scope_id,
            ) from exc

        _require_physical_database(
            safe_path,
            backend=safe_backend,
            scope=scope,
            scope_id=scope_id,
        )
        if safe_backend == "grafx":
            if database is None:
                raise _capability(
                    "grafx_database_admission_required",
                    operation="initialize_graph_backend_binding",
                    scope=scope,
                    scope_id=scope_id,
                )
            admit_grafx_database(
                database,
                expected_page_size=effective_page_size,
                expected_path=safe_path,
                operation="initialize_graph_backend_binding",
            )

        body = self._body(
            scope=scope,
            scope_id=scope_id,
            backend=safe_backend,
            generation=safe_generation,
            physical_path=safe_path,
            page_size=effective_page_size,
        )
        digest = _binding_sha256(body)
        candidate = CommunityGraphBackendBinding(
            scope=scope,
            scope_id=scope_id,
            backend=safe_backend,
            generation=safe_generation,
            physical_path=safe_path,
            page_size=effective_page_size,
            binding_sha256=digest,
        )
        path = (
            self._board_binding_path(scope_id)
            if scope == "board"
            else self._global_binding_path()
        )
        self._publish_initial(path, body={**body, "binding_sha256": digest})
        persisted = self._acquire(path, scope=scope, scope_id=scope_id)
        if persisted != candidate:
            raise _corruption(
                "binding_readback_mismatch",
                scope=scope,
                scope_id=scope_id,
            )
        return persisted

    def _body(
        self,
        *,
        scope: GraphBindingScope,
        scope_id: str,
        backend: GraphBackend,
        generation: str,
        physical_path: Path,
        page_size: int | None,
    ) -> dict[str, object]:
        relative = physical_path.relative_to(self._root)
        return {
            "binding_format": BINDING_FORMAT,
            "scope": scope,
            "scope_id": scope_id,
            "backend": backend,
            "generation": generation,
            "physical_path": PurePosixPath(*relative.parts).as_posix(),
            "page_size": page_size,
        }

    def _publish_initial(self, path: Path, *, body: Mapping[str, Any]) -> None:
        scope = "global" if path == self._global_binding_path() else "board"
        scope_id = "global" if scope == "global" else path.parent.name
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _unavailable(
                "binding_directory_create_failed",
                operation="initialize_graph_backend_binding",
                scope=scope,
                scope_id=scope_id,
                error_type=type(exc).__name__,
            ) from exc

        lock = FileLock(f"{path}.lock", timeout=self._lock_timeout_seconds)
        try:
            with lock:
                if path.exists():
                    existing = self._acquire(
                        path,
                        scope=scope,
                        scope_id=scope_id,
                    )
                    candidate_digest = str(body["binding_sha256"])
                    if existing.binding_sha256 != candidate_digest:
                        raise _capability(
                            "binding_conflict",
                            operation="initialize_graph_backend_binding",
                            scope=scope,
                            scope_id=scope_id,
                        )
                    return
                self._write_json_atomic(path, body)
        except FileLockTimeout as exc:
            raise GraphLockContention(
                "The Community graph backend binding lock is contended.",
                details={
                    "operation": "initialize_graph_backend_binding",
                    "reason": "binding_lock_contention",
                    "scope": scope,
                    "scope_id": scope_id,
                },
            ) from exc
        except (GraphCapabilityUnavailable, GraphCorruption, GraphUnavailable):
            raise
        except OSError as exc:
            raise _unavailable(
                "binding_publication_failed",
                operation="initialize_graph_backend_binding",
                scope=scope,
                scope_id=scope_id,
                error_type=type(exc).__name__,
            ) from exc

    @staticmethod
    def _write_json_atomic(path: Path, body: Mapping[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    dict(body), stream, ensure_ascii=True, sort_keys=True, indent=2
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            fsync_directory(path.parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _acquire(
        self,
        path: Path,
        *,
        scope: GraphBindingScope,
        scope_id: str,
    ) -> CommunityGraphBackendBinding:
        try:
            path.lstat()
            junction_check = getattr(path, "is_junction", None)
            if path.is_symlink() or (junction_check is not None and junction_check()):
                raise ValueError("binding_path_alias_refused")
            lexical_path = Path(os.path.abspath(path))
            if os.path.normcase(str(lexical_path)) != os.path.normcase(
                str(path.resolve(strict=False))
            ):
                raise ValueError("binding_parent_alias_refused")
            with path.open("rb") as stream:
                encoded = stream.read(MAX_BINDING_BYTES + 1)
            if len(encoded) > MAX_BINDING_BYTES:
                raise ValueError("binding_too_large")
            raw = encoded.decode("utf-8")
            document = json.loads(raw, object_pairs_hook=_strict_object)
        except FileNotFoundError as exc:
            raise _capability(
                "binding_missing",
                operation="acquire_graph_backend_binding",
                scope=scope,
                scope_id=scope_id,
            ) from exc
        except OSError as exc:
            raise _unavailable(
                "binding_read_failed",
                operation="acquire_graph_backend_binding",
                scope=scope,
                scope_id=scope_id,
                error_type=type(exc).__name__,
            ) from exc
        except (RuntimeError, TypeError, ValueError) as exc:
            raise _corruption(
                "binding_document_unreadable",
                scope=scope,
                scope_id=scope_id,
            ) from exc

        try:
            if type(document) is not dict or set(document) != _BINDING_KEYS:
                raise ValueError("binding_shape_invalid")
            supplied_digest = document["binding_sha256"]
            if (
                type(supplied_digest) is not str
                or _SHA256_RE.fullmatch(supplied_digest) is None
            ):
                raise ValueError("binding_digest_invalid")
            body = {
                key: value for key, value in document.items() if key != "binding_sha256"
            }
            if _binding_sha256(body) != supplied_digest:
                raise ValueError("binding_digest_mismatch")
            if document["binding_format"] != BINDING_FORMAT:
                raise ValueError("binding_format_unsupported")
            if document["scope"] != scope or document["scope_id"] != scope_id:
                raise ValueError("binding_scope_mismatch")
            backend = _validate_backend(document["backend"])
            generation = _validate_portable_segment(
                document["generation"], field_name="generation"
            )
            relative_text = document["physical_path"]
            if type(relative_text) is not str or "\\" in relative_text:
                raise ValueError("binding_physical_path_invalid")
            relative = PurePosixPath(relative_text)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("binding_physical_path_invalid")
            physical_path = _canonical_physical_path(
                self._root,
                self._root.joinpath(*relative.parts),
                scope=scope,
                scope_id=scope_id,
                backend=backend,
                generation=generation,
            )
            raw_page_size = document["page_size"]
            if backend == "grafx":
                page_size = validate_grafx_page_size(raw_page_size)
            elif raw_page_size is not None:
                raise ValueError("ladybug_binding_page_size_not_null")
            else:
                page_size = None
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _corruption(
                "binding_document_invalid",
                scope=scope,
                scope_id=scope_id,
            ) from exc

        _require_physical_database(
            physical_path,
            backend=backend,
            scope=scope,
            scope_id=scope_id,
        )
        return CommunityGraphBackendBinding(
            scope=scope,
            scope_id=scope_id,
            backend=backend,
            generation=generation,
            physical_path=physical_path,
            page_size=page_size,
            binding_sha256=supplied_digest,
        )


__all__ = [
    "BINDING_FORMAT",
    "BOARD_BINDING_FILENAME",
    "CommunityGraphBackendBinding",
    "CommunityGraphBackendBindingStore",
    "GLOBAL_BINDING_FILENAME",
    "GrafxDatabaseAdmission",
    "GraphBackend",
    "GraphBindingScope",
    "admit_grafx_database",
]
