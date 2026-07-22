"""Community Global Discovery runtime adapter.

The core owns query semantics and schema constants; the Community edition owns
the local LadybugDB path, handle lifecycle and quarantine behavior.
"""

from __future__ import annotations

import gc
import hashlib
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn

from okto_pulse.community.adapters.board_graph_runtime import (
    CommunityBoardGraphRuntime,
)
from okto_pulse.community.adapters.graph_error_mapping import map_graph_error
from okto_pulse.community.adapters.graph_memory_pressure import (
    GRAPH_OPEN_MEMORY_COOLDOWN_S,
    clear_graph_open_memory_failure,
    raise_if_graph_open_memory_cooldown,
    record_graph_open_memory_failure,
    reset_graph_open_memory_circuit_for_tests,
    run_graph_database_open,
)
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    _materialize,
    _statement_is_write,
    _statement_kind,
)
from okto_pulse.community.adapters.ladybug_writer import ladybug_writer_scope
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.kg.interfaces.graph_lifecycle import GraphHandle
from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphPurgeResult,
    GraphRuntimeObservationState,
    GraphRuntimeState,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef

logger = logging.getLogger("okto_pulse.community.global_discovery_runtime")

GLOBAL_DISCOVERY_FILENAME = "discovery.lbug"
LIFECYCLE_EXCLUSIVE_TIMEOUT_S = 30.0
GLOBAL_OPEN_MEMORY_COOLDOWN_S = GRAPH_OPEN_MEMORY_COOLDOWN_S


def _reset_global_open_memory_circuit_for_tests() -> None:
    """Compatibility helper for the process-wide graph-open breaker."""

    reset_graph_open_memory_circuit_for_tests()

_VECTOR_USE_PATTERN = re.compile(
    r"(?:VECTOR_INDEX|EMBEDDING)",
    re.IGNORECASE,
)
_DIGEST_REPAIR_MAX_PRIMARY_DRAINS = 32


def _digest_repair_staging_id(
    *, digest_id: str, board_id: str, original_node_id: str
) -> str:
    identity = "\x00".join((digest_id, board_id, original_node_id))
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"dd_repair_{suffix}"


def _is_duplicate_primary_key_error(exc: BaseException) -> bool:
    normalized = str(exc).lower()
    return (
        "duplicated primary key value" in normalized
        or "duplicate primary key value" in normalized
    )


def _vector_index_already_exists_error(exc: BaseException) -> bool:
    """Whether a CREATE_VECTOR_INDEX failure is a benign already-exists outcome.

    Only this narrow class may be swallowed during an idempotent re-bootstrap;
    every other DDL/IO/index failure must propagate (blocker 24).
    """

    normalized = str(exc).lower()
    return "already exists" in normalized and "index" in normalized


class _LifecycleReadWriteGate:
    """Keep shared connections alive while close/reopen owns the DB handle.

    Ladybug connections borrow the runtime's shared ``Database`` object.  The
    small ``_lock`` on the runtime protects only pointer publication; it cannot
    prevent ``Database.close()`` after a reader has obtained a connection.  This
    gate lets materialized reads overlap while giving close/flush one re-entrant,
    writer-preferred exclusive lifecycle section.
    """

    def __init__(
        self,
        *,
        exclusive_timeout_s: float = LIFECYCLE_EXCLUSIVE_TIMEOUT_S,
    ) -> None:
        if exclusive_timeout_s <= 0:
            raise ValueError("exclusive_timeout_s must be positive")
        self._condition = threading.Condition()
        self._readers = 0
        self._waiting_exclusive = 0
        self._exclusive_owner: int | None = None
        self._exclusive_depth = 0
        self._exclusive_timeout_s = exclusive_timeout_s

    @contextmanager
    def shared(self) -> Iterator[None]:
        owner = threading.get_ident()
        counted = False
        with self._condition:
            if self._exclusive_owner != owner:
                while self._exclusive_owner is not None or self._waiting_exclusive > 0:
                    self._condition.wait()
                self._readers += 1
                counted = True
        try:
            yield
        finally:
            if counted:
                with self._condition:
                    self._readers -= 1
                    if self._readers == 0:
                        self._condition.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        owner = threading.get_ident()
        with self._condition:
            if self._exclusive_owner == owner:
                self._exclusive_depth += 1
            else:
                self._waiting_exclusive += 1
                deadline = time.monotonic() + self._exclusive_timeout_s
                try:
                    while self._exclusive_owner is not None or self._readers:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            logger.error(
                                "global_discovery.lifecycle_exclusive_timeout "
                                "readers=%d owner=%s timeout_ms=%d",
                                self._readers,
                                self._exclusive_owner,
                                int(self._exclusive_timeout_s * 1000),
                                extra={
                                    "event": (
                                        "global_discovery.lifecycle_exclusive_timeout"
                                    ),
                                    "readers": self._readers,
                                    "timeout_ms": int(self._exclusive_timeout_s * 1000),
                                },
                            )
                            raise GraphLockContention(
                                "global_discovery.lifecycle_exclusive_timeout: "
                                f"readers={self._readers} "
                                f"timeout_ms={int(self._exclusive_timeout_s * 1000)}",
                                details={
                                    "readers": self._readers,
                                    "timeout_ms": int(self._exclusive_timeout_s * 1000),
                                    "error_code": GraphLockContention.code,
                                    "retryable": GraphLockContention.retryable,
                                },
                            )
                        self._condition.wait(timeout=remaining)
                    self._exclusive_owner = owner
                    self._exclusive_depth = 1
                finally:
                    self._waiting_exclusive -= 1
                    if self._waiting_exclusive == 0 and self._exclusive_owner is None:
                        self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                if self._exclusive_owner != owner:
                    raise RuntimeError("global discovery lifecycle owner changed")
                self._exclusive_depth -= 1
                if self._exclusive_depth == 0:
                    self._exclusive_owner = None
                    self._condition.notify_all()


def _statement_requires_vector_extension(statement: str) -> bool:
    """Return whether a statement uses vector data/index functionality.

    ``LOAD VECTOR`` is connection-local but still writes Ladybug's WAL.  Most
    parity, health and schema-discovery reads do not touch vectors and must not
    pay that mutation on every fresh connection.
    """

    without_comments = re.sub(
        r"//[^\n]*|/\*.*?\*/",
        " ",
        statement,
        flags=re.DOTALL,
    )
    without_literals = re.sub(
        r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"",
        " ",
        without_comments,
    )
    return _statement_is_write(without_literals) or (
        _VECTOR_USE_PATTERN.search(without_literals) is not None
    )


class CommunityGlobalDiscoveryRuntime:
    """Concrete GlobalDiscoveryRuntime backed by local LadybugDB."""

    def __init__(
        self,
        graph_runtime: CommunityBoardGraphRuntime | None = None,
        *,
        graph_path_provider: Callable[[], Path] | None = None,
        open_memory_cooldown_s: float = GLOBAL_OPEN_MEMORY_COOLDOWN_S,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if open_memory_cooldown_s <= 0:
            raise ValueError("open_memory_cooldown_s must be positive")
        self._graph_runtime = graph_runtime or CommunityBoardGraphRuntime()
        self._graph_path_provider = graph_path_provider
        self._open_memory_cooldown_s = float(open_memory_cooldown_s)
        self._monotonic_clock = monotonic_clock
        self._lock = threading.RLock()
        self._lifecycle = _LifecycleReadWriteGate()
        self._db: Any | None = None
        self._db_path: Path | None = None
        # Health's absence/presence probe is deliberately metadata-only: it
        # must never bootstrap or replay a graph merely to classify it.  A
        # real runtime open can still prove that present storage is corrupt,
        # though.  Retain that bounded fact so the next metadata-only health
        # read cannot overwrite an observed open failure with a false healthy.
        self._corrupt_open_latch: tuple[Path, str] | None = None
        # Blocker 18/30: observable aggregate of the last bootstrap's
        # directory-fsync durability across every boundary (marker write, both
        # artifact/dir fsyncs, marker-clear dir fsync).  None until a bootstrap
        # completes durably.
        self._bootstrap_directory_fsync_supported: bool | None = None

    def _runtime(self):
        return self._graph_runtime

    def _raise_if_memory_open_cooldown(self, *, path: Path) -> None:
        raise_if_graph_open_memory_cooldown(
            path=path,
            monotonic_clock=self._monotonic_clock,
        )

    def _record_memory_open_failure(
        self,
        *,
        path: Path,
        exc: BaseException,
    ) -> None:
        record_graph_open_memory_failure(
            path=path,
            exc=exc,
            scope="global_discovery",
            cooldown_s=self._open_memory_cooldown_s,
            monotonic_clock=self._monotonic_clock,
        )

    def _clear_memory_open_failure(self) -> None:
        clear_graph_open_memory_failure()

    def _open_global_database(
        self,
        path: Path,
        *,
        on_corruption: Callable[[BaseException], None] | None = None,
    ) -> Any:
        """Open through the dedicated global budget and OOM circuit breaker."""

        runtime = self._runtime()
        opener = getattr(runtime, "open_global_kuzu_db", None)
        if not callable(opener):
            raise RuntimeError(
                "global_discovery_dedicated_open_capability_missing: the graph "
                "adapter must expose open_global_kuzu_db so Global Discovery "
                "cannot silently inherit the board memory budget"
            )
        return run_graph_database_open(
            path=path,
            opener=lambda: opener(path, on_corruption=on_corruption),
            scope="global_discovery",
            cooldown_s=self._open_memory_cooldown_s,
            monotonic_clock=self._monotonic_clock,
        )

    def _kg_base_dir(self) -> Path:
        from okto_pulse.core.services.application_kg import (
            get_current_provider_registry,
        )

        raw = get_current_provider_registry().config.kg_base_dir
        return Path(os.path.expanduser(raw)).resolve()

    def _legacy_global_graph_path(self) -> Path:
        if self._graph_path_provider is not None:
            return Path(self._graph_path_provider()).resolve()
        return self._kg_base_dir() / "global" / GLOBAL_DISCOVERY_FILENAME

    def _global_graph_path(self) -> Path:
        from okto_pulse.community.adapters.global_discovery_layout import (
            resolve_active_graph_path,
        )

        return resolve_active_graph_path(self._legacy_global_graph_path())

    def materialization_observation_paths(self) -> tuple[Path, ...]:
        """Metadata paths observed by health without resolving the pointer."""

        legacy = self._legacy_global_graph_path()
        from okto_pulse.community.adapters.global_discovery_layout import (
            active_pointer_path,
            generations_root,
        )
        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            bootstrap_marker_path,
        )

        # INV-E2: the durable incomplete-bootstrap marker is part of the
        # physical observation set — its existence overrides legacy/pointer
        # presence in ``state()``, so any observer of the materialization paths
        # must see it too.
        return (
            legacy,
            active_pointer_path(legacy),
            generations_root(legacy),
            bootstrap_marker_path(legacy),
        )

    @staticmethod
    def _storage_ref() -> StorageRef:
        return StorageRef("global-discovery", "community_local_graph")

    def _resolve_layout_paths(self) -> tuple[Path, Path, Path]:
        legacy = self._legacy_global_graph_path()
        from okto_pulse.community.adapters.global_discovery_layout import (
            active_pointer_path,
            generations_root,
        )

        return legacy, active_pointer_path(legacy), generations_root(legacy)

    def _provider_unavailable_state(
        self,
        *,
        generation: str | None,
        observed_at: datetime,
    ) -> GraphRuntimeState:
        return GraphRuntimeState.from_observation(
            board_id="_global",
            storage_ref=self._storage_ref(),
            state=GraphRuntimeObservationState.PROVIDER_UNAVAILABLE,
            generation=generation,
            reason_code="global_discovery_provider_unavailable",
            observed_at=observed_at,
            backend="community_local_graph",
            details={"source": "community_global_discovery_runtime"},
        )

    def state(self, *, generation: str | None = None) -> GraphRuntimeState:
        """Return metadata-only discovery state without resolving the pointer.

        Resolving the active generation reads and validates JSON manifests. A
        health absence probe must stay at the filesystem metadata boundary, so
        pointer presence is a readable candidate for the later native probe.

        INV-E2: a durable incomplete-bootstrap marker (a write-ahead intent
        log persisted before any bootstrap mutation) overrides every
        legacy/pointer presence check.  A metadata-only marker probe runs first
        so a half-written legacy graph can never be classified readable across
        process boundaries.
        """

        observed_at = datetime.now(timezone.utc)
        try:
            legacy, pointer, generation_root = self._resolve_layout_paths()
        except Exception:
            return self._provider_unavailable_state(
                generation=generation, observed_at=observed_at
            )

        marker = self._bootstrap_marker_observation(
            legacy=legacy,
            pointer=pointer,
            generation_root=generation_root,
            generation=generation,
            observed_at=observed_at,
        )
        if marker is not None:
            return marker

        return self._classify_primary_state(
            legacy=legacy,
            pointer=pointer,
            generation_root=generation_root,
            generation=generation,
            observed_at=observed_at,
        )

    def _bootstrap_marker_observation(
        self,
        *,
        legacy: Path,
        pointer: Path,
        generation_root: Path,
        generation: str | None,
        observed_at: datetime,
    ) -> GraphRuntimeState | None:
        """Project the durable incomplete-bootstrap marker, metadata-only.

        Existence alone forces ``PRESENT_UNREADABLE_OR_ERROR`` /
        ``global_discovery_bootstrap_incomplete`` / ``quarantined=True``.  A stat
        IO error on the marker itself is also treated fail-closed as unreadable.

        The observation carries a narrow, metadata-only ``primary_confirmed_absent``
        fact (computed without any mutation via the marker-agnostic classifier).
        ``state()`` stays authoritative and no general marker-bypass method is
        exposed on the Core protocol (Nexus msg_20533dbbce3741248416fc0e53b7ea4e /
        boundary ruling): the narrow ``init`` retry keys off this exact reason
        plus this exact boolean.
        """

        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            BOOTSTRAP_INCOMPLETE_REASON,
            bootstrap_marker_present,
        )

        try:
            present = bootstrap_marker_present(legacy)
        except OSError:
            return GraphRuntimeState.from_observation(
                board_id="_global",
                storage_ref=self._storage_ref(),
                state=GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code="global_discovery_marker_stat_io_error",
                observed_at=observed_at,
                backend="community_local_graph",
                quarantined=True,
                details={"source": "community_global_discovery_runtime"},
            )
        if not present:
            return None

        # Metadata-only, no mutation: classify the physical primary while
        # ignoring the marker so the narrow init retry can prove the previous
        # process died before creating any artifact.
        physical = self._classify_primary_state(
            legacy=legacy,
            pointer=pointer,
            generation_root=generation_root,
            generation=generation,
            observed_at=observed_at,
        )
        primary_confirmed_absent = (
            physical.state == GraphRuntimeObservationState.CONFIRMED_ABSENT
        )
        return replace(
            GraphRuntimeState.from_observation(
                board_id="_global",
                storage_ref=self._storage_ref(),
                state=GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code=BOOTSTRAP_INCOMPLETE_REASON,
                observed_at=observed_at,
                backend="community_local_graph",
                quarantined=True,
                details={
                    "source": "community_global_discovery_runtime",
                    "bootstrap_incomplete_marker": True,
                    "primary_confirmed_absent": primary_confirmed_absent,
                },
            ),
            status="quarantined",
        )

    def _classify_primary_state(
        self,
        *,
        legacy: Path,
        pointer: Path,
        generation_root: Path,
        generation: str | None,
        observed_at: datetime,
    ) -> GraphRuntimeState:
        def metadata_present(path: Path) -> bool:
            try:
                path.stat()
            except FileNotFoundError:
                return False
            return True

        try:
            legacy_present = metadata_present(legacy)
            pointer_present = metadata_present(pointer)
            generation_root_present = metadata_present(generation_root)
        except OSError:
            return GraphRuntimeState.from_observation(
                board_id="_global",
                storage_ref=self._storage_ref(),
                state=GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code="global_discovery_stat_io_error",
                observed_at=observed_at,
                backend="community_local_graph",
                details={"source": "community_global_discovery_runtime"},
            )

        if legacy_present or pointer_present:
            corrupt_open = self._corrupt_open_observation(
                generation=generation,
                observed_at=observed_at,
            )
            if corrupt_open is not None:
                return corrupt_open
            return replace(
                GraphRuntimeState.from_observation(
                    board_id="_global",
                    storage_ref=self._storage_ref(),
                    state=GraphRuntimeObservationState.PRESENT_READABLE_CANDIDATE,
                    generation=generation,
                    reason_code="global_discovery_metadata_present",
                    observed_at=observed_at,
                    backend="community_local_graph",
                    details={
                        "source": "community_global_discovery_runtime",
                        "legacy_present": legacy_present,
                        "active_pointer_present": pointer_present,
                    },
                ),
                status="healthy",
            )

        try:
            legacy.parent.stat()
        except FileNotFoundError:
            residues: tuple[str, ...] = ()
        except OSError:
            return GraphRuntimeState.from_observation(
                board_id="_global",
                storage_ref=self._storage_ref(),
                state=GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
                generation=generation,
                reason_code="global_discovery_parent_stat_io_error",
                observed_at=observed_at,
                backend="community_local_graph",
                details={"source": "community_global_discovery_runtime"},
            )
        else:
            try:
                residues = (
                    (generation_root.name,)
                    if generation_root_present
                    else tuple(
                        sorted(
                            child.name
                            for child in legacy.parent.iterdir()
                            if child.name.startswith(f"{legacy.name}.")
                        )
                    )
                )
            except OSError:
                return GraphRuntimeState.from_observation(
                    board_id="_global",
                    storage_ref=self._storage_ref(),
                    state=(GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR),
                    generation=generation,
                    reason_code="global_discovery_residue_scan_io_error",
                    observed_at=observed_at,
                    backend="community_local_graph",
                    details={"source": "community_global_discovery_runtime"},
                )
        if residues:
            return replace(
                GraphRuntimeState.from_observation(
                    board_id="_global",
                    storage_ref=self._storage_ref(),
                    state=(GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR),
                    generation=generation,
                    reason_code="global_discovery_residue_without_primary",
                    observed_at=observed_at,
                    backend="community_local_graph",
                    quarantined=True,
                    details={
                        "source": "community_global_discovery_runtime",
                        "residue_count": len(residues),
                    },
                ),
                status="quarantined",
            )
        return GraphRuntimeState.from_observation(
            board_id="_global",
            storage_ref=self._storage_ref(),
            state=GraphRuntimeObservationState.CONFIRMED_ABSENT,
            generation=generation,
            reason_code="global_discovery_confirmed_absent",
            observed_at=observed_at,
            backend="community_local_graph",
            details={"source": "community_global_discovery_runtime"},
        )

    def _corrupt_open_observation(
        self,
        *,
        generation: str | None,
        observed_at: datetime,
    ) -> GraphRuntimeState | None:
        """Project a previously proven corrupt open without touching storage.

        The read side never resolves the active-generation pointer.  Successful
        open/bootstrap/cutover paths clear the immutable latch explicitly;
        manual external repair therefore remains fail-closed until a real
        in-process open proves the repaired store.
        """

        with self._lock:
            latch = self._corrupt_open_latch
        if latch is None:
            return None
        _failed_path, reason_code = latch

        return GraphRuntimeState.from_observation(
            board_id="_global",
            storage_ref=self._storage_ref(),
            state=GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
            generation=generation,
            reason_code=reason_code,
            observed_at=observed_at,
            backend="community_local_graph",
            details={
                "source": "community_global_discovery_runtime",
                "corrupt_open_latched": True,
            },
        )

    def _record_corrupt_open_failure(
        self,
        *,
        path: Path,
        exc: BaseException,
    ) -> None:
        try:
            is_corrupt = self.is_ladybug_corruption_error(exc)
        except Exception:
            is_corrupt = False
        if not is_corrupt:
            return
        latch = (path.resolve(), "global_discovery_corrupt_open_failed")
        with self._lock:
            already_latched = self._corrupt_open_latch == latch
            self._corrupt_open_latch = latch
        if already_latched:
            return
        logger.warning(
            "global_discovery.corrupt_open_latched",
            extra={
                "event": "global_discovery.corrupt_open_latched",
                "reason_code": "global_discovery_corrupt_open_failed",
            },
        )

    def _clear_corrupt_open_failure(self, *, successful_path: Path) -> None:
        """Clear only after a write-side/native success proves readability."""

        _ = successful_path
        with self._lock:
            self._corrupt_open_latch = None

    def note_successful_generation_cutover(
        self,
        *,
        active_path: Path,
        fence_check: Callable[[], None],
    ) -> bool:
        """Retire stale corruption evidence after validated atomic cutover.

        Runs inside the recovery adapter's fenced, post-cutover protected
        region.  In the same region that clears the in-memory corruption latch
        it also physically clears the durable incomplete-bootstrap marker
        (INV-E2): a valid physical recovery has published and read back a good
        generation, so the write-ahead intent log is now reconciled.  Only this
        fenced path clears the marker; ``init`` never does except for the narrow
        physically-absent retry exception.

        ``fence_check`` is the caller's exact live fence and is mandatory /
        non-null (Nexus msg_08f6fa2df8ab4e728144e12e30ca7c67): a missing callback
        is a typed composition error, never a silent clear.  It is invoked
        *immediately before* any latch or physical clear (never relying on an
        earlier caller-side check) to close the TOCTOU gap and refuse an
        accidental invocation outside a valid guard.  Returns the directory-fsync
        support flag so the recovery journal can thread durability evidence.
        """

        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            BootstrapMarkerAuthorityError,
            clear_bootstrap_marker,
        )

        if fence_check is None:
            raise BootstrapMarkerAuthorityError(
                "global_discovery_bootstrap_marker_clear_unauthorized: "
                "note_successful_generation_cutover requires a live fence"
            )
        # Invoke the callback immediately before the latch clear as well.
        fence_check()
        self._clear_corrupt_open_failure(successful_path=active_path)
        # clear_bootstrap_marker re-invokes the callback immediately before the
        # physical unlink (single authority-checked unlink path).
        return clear_bootstrap_marker(
            self._legacy_global_graph_path(),
            fence_check=fence_check,
        )

    def require_write_token(self, *, operation: str = "") -> Any:
        from okto_pulse.core.ports.global_discovery_recovery_control import (
            assert_global_discovery_writer_fence,
        )
        from okto_pulse.core.kg.write_barrier import require_global_write_token

        # The ContextVar barrier alone can be entered without acquiring the
        # cross-process ``_global`` lock.  Every native mutation must prove the
        # exact live GlobalDiscoveryWriterLease used by outbox/recovery first.
        assert_global_discovery_writer_fence()
        return require_global_write_token()

    def _quarantine_service(self):
        from okto_pulse.community.adapters.local_storage_ref import local_storage_ref
        from okto_pulse.core.kg.quarantine import KGQuarantineService

        graph_dir = self._global_graph_path().parent
        return KGQuarantineService(
            base_storage_ref_hint=local_storage_ref(graph_dir.parent),
            scope_storage_refs=[local_storage_ref(graph_dir)],
        )

    def is_ladybug_corruption_error(self, exc: BaseException) -> bool:
        return self._runtime().is_ladybug_corruption_error(exc)

    def bootstrap(self) -> GraphHandle:
        with ladybug_writer_scope(
            scope="_global",
            phase="bootstrap",
        ):
            with self._lifecycle.exclusive():
                # The close is intentionally NOT here: the durable marker must
                # precede every bootstrap-side filesystem action, and a cached
                # DB close may checkpoint/flush WAL.  ``_bootstrap_with_writer_lease``
                # enforces the recovery-only boundary and publishes the marker
                # first, then closes.
                return self._bootstrap_with_writer_lease()

    def _enforce_bootstrap_marker_boundary(self) -> None:
        """Refuse a direct bootstrap over a live marker + non-absent primary.

        Metadata-only and mutation-free: an already-present marker is
        recovery-ceremony-only unless the primary is *physically*
        ``CONFIRMED_ABSENT`` (the previous process died before creating any
        artifact).  This is the unbypassable capability boundary — a direct
        ``runtime.bootstrap()`` under a writer token cannot overwrite/open/mutate
        or clear a marker that sits over a primary/pointer/residue.
        """

        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            bootstrap_marker_present,
        )

        legacy = self._legacy_global_graph_path()
        try:
            present = bootstrap_marker_present(legacy)
        except OSError as exc:
            raise RuntimeError(
                "global_discovery_marker_stat_io_error: cannot stat the "
                "incomplete-bootstrap marker; refusing bootstrap (fail closed)"
            ) from exc
        if not present:
            return
        if self._primary_confirmed_absent_without_marker():
            # Narrow retry exception: marker + physically absent primary.
            return
        raise RuntimeError(
            "global_discovery_bootstrap_refused_marker_present: an "
            "incomplete-bootstrap marker over a present/partial primary is "
            "recovery-ceremony-only; direct bootstrap is refused (zero mutation)"
        )

    def _primary_confirmed_absent_without_marker(self) -> bool:
        """Metadata-only, mutation-free physical-absence probe (ignores marker)."""

        observed_at = datetime.now(timezone.utc)
        legacy, pointer, generation_root = self._resolve_layout_paths()
        physical = self._classify_primary_state(
            legacy=legacy,
            pointer=pointer,
            generation_root=generation_root,
            generation=None,
            observed_at=observed_at,
        )
        return (
            physical.state == GraphRuntimeObservationState.CONFIRMED_ABSENT
        )

    def _bootstrap_with_writer_lease(self) -> GraphHandle:
        from okto_pulse.community.adapters.global_discovery_schema import (
            NODE_DDL,
            REL_DDL,
            VECTOR_INDEXES,
        )
        from okto_pulse.community.adapters.global_discovery_schema import (
            ensure_decision_digest_layer_column,
            raise_existing_global_graph_open_failed,
        )
        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            write_bootstrap_marker,
        )

        self.require_write_token(operation="bootstrap")
        legacy = self._legacy_global_graph_path()

        # Blocker 14: this is the single, unbypassable bootstrap choke point
        # (both the public ``bootstrap()`` and the ordinary auto-bootstrap reach
        # it).  A present marker plus ANY primary/pointer/residue is
        # recovery-ceremony-only; the sole retry exception is a marker plus a
        # freshly revalidated, physically absent primary.  Checked before the
        # marker write so a direct bootstrap cannot overwrite/open/mutate/clear a
        # marker sitting over a primary.
        self._enforce_bootstrap_marker_boundary()

        # INV-E2 (blocker 1): persist the durable incomplete-bootstrap intent log
        # as the FIRST bootstrap-side action, before any close/mkdir/open/DDL.
        # A cached-DB close may checkpoint/flush WAL, so the marker must precede
        # even that.  The marker writer creates its own parent directory.  Any
        # crash/failure before durable completion leaves this marker, so a fresh
        # runtime/process classifies Global Discovery
        # PRESENT_UNREADABLE_OR_ERROR / bootstrap_incomplete / quarantined
        # instead of trusting a half-written legacy graph.
        marker_write = write_bootstrap_marker(legacy)

        self._close_with_writer_lease()
        path = self._global_graph_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = self._open_global_database(
                path,
                on_corruption=lambda exc: self._record_corrupt_open_failure(
                    path=path,
                    exc=exc,
                ),
            )
        except Exception as exc:
            self._record_corrupt_open_failure(path=path, exc=exc)
            raise_existing_global_graph_open_failed(
                storage_locator=path,
                operation="bootstrap",
                exc=exc,
            )
        self._clear_corrupt_open_failure(successful_path=path)
        runtime = self._runtime()
        conn = runtime.new_connection(db)
        try:
            runtime.load_vector_extension(conn)
            for ddl in NODE_DDL:
                conn.execute(ddl)
            for ddl in REL_DDL:
                conn.execute(ddl)
            ensure_decision_digest_layer_column(conn)
            for table, idx_name, col in VECTOR_INDEXES:
                try:
                    conn.execute(
                        f"CALL CREATE_VECTOR_INDEX("
                        f"'{table}', '{idx_name}', '{col}', "
                        f"metric := 'cosine')"
                    )
                except Exception as exc:
                    # Blocker 24: only a proven already-exists outcome may be
                    # ignored (idempotent re-bootstrap).  Any real I/O/DDL/index
                    # failure must propagate so the marker is preserved and the
                    # store stays recovery-only — never silently cleared.
                    if not _vector_index_already_exists_error(exc):
                        raise
        finally:
            try:
                conn.close()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass
            del db
            gc.collect()

        # INV-E2 (blocker 2): the marker is retired only after real durable
        # completion, never on page-cache readability.  Acceptance ordering,
        # reusing the flush/fsync discipline: close -> fsync artifacts+directory
        # -> fresh probe/readback -> close -> fsync again -> revalidate exact
        # fence -> clear marker + directory fsync.
        self._complete_bootstrap_durably(
            path=path,
            legacy=legacy,
            marker_write_supported=marker_write.directory_fsync_supported,
        )
        return GraphHandle(
            board_id="_global",
            storage_ref=self._storage_ref(),
            opened=True,
            status="opened",
            locked=False,
            quarantined=False,
        )

    def _complete_bootstrap_durably(
        self, *, path: Path, legacy: Path, marker_write_supported: bool = True
    ) -> bool:
        """Durable-completion barrier before clearing the bootstrap marker.

        Ordering (blocker 2), consistent with ``_flush_after_write_batch``:
        close -> fsync artifacts + directory -> fresh probe/readback proving the
        required schema -> close (readback owns its handle) -> fsync again ->
        revalidate the exact live fence -> clear the marker + directory fsync.
        A failure at any step leaves the marker in place and raises, so the next
        process observes the unreadable truth.

        Blocker 18/30: aggregate directory-fsync support across every boundary
        (marker write, both artifact/dir fsyncs, marker-clear dir fsync) and
        record it as observable evidence (``self._bootstrap_directory_fsync_supported``)
        instead of discarding it.  Returns that aggregate.
        """

        directory_fsync_supported = bool(marker_write_supported)
        self._close_with_writer_lease()
        directory_fsync_supported = (
            self._fsync_global_artifacts_and_dir(path) and directory_fsync_supported
        )
        self._readback_global_discovery_schema(path)
        directory_fsync_supported = (
            self._fsync_global_artifacts_and_dir(path) and directory_fsync_supported
        )
        # Authority-checked, TOCTOU-free clear (single unlink path): the fence is
        # revalidated via ``require_write_token`` immediately before the unlink.
        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            clear_bootstrap_marker,
        )

        clear_supported = clear_bootstrap_marker(
            legacy,
            fence_check=lambda: self.require_write_token(
                operation="clear_bootstrap_marker"
            ),
        )
        directory_fsync_supported = bool(clear_supported) and directory_fsync_supported
        self._bootstrap_directory_fsync_supported = directory_fsync_supported
        return directory_fsync_supported

    def _fsync_global_artifacts_and_dir(self, path: Path) -> bool:
        """Fsync the graph artifacts and their directory; report dir-fsync support."""

        from okto_pulse.community.adapters.global_discovery_layout import (
            fsync_directory,
        )

        self._fsync_global_artifacts(path)
        return fsync_directory(path.parent)

    def _readback_global_discovery_schema(self, path: Path) -> None:
        """Reopen the freshly bootstrapped graph and confirm its core schema.

        Removing the incomplete-bootstrap marker is only safe once the primary
        has been read back successfully.  A failed readback leaves the marker in
        place and raises, so the next process observes the unreadable truth.
        """

        from okto_pulse.community.adapters.global_discovery_schema import (
            raise_existing_global_graph_open_failed,
        )

        runtime = self._runtime()
        try:
            db = self._open_global_database(
                path,
                on_corruption=lambda exc: self._record_corrupt_open_failure(
                    path=path,
                    exc=exc,
                ),
            )
        except Exception as exc:
            self._record_corrupt_open_failure(path=path, exc=exc)
            raise_existing_global_graph_open_failed(
                storage_locator=path,
                operation="bootstrap_readback",
                exc=exc,
            )
        try:
            conn = runtime.new_connection(db)
            try:
                result = _materialize(conn.execute("CALL SHOW_TABLES() RETURN name"))
                index_result = _materialize(
                    conn.execute("CALL SHOW_INDEXES() RETURN *")
                )
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        finally:
            try:
                db.close()
            except Exception:
                pass
            del db
            gc.collect()
        names = {str(row[0]) for row in result.rows if row and row[0]}
        required = {"Board", "Topic", "Entity", "DecisionDigest"}
        if not required <= names:
            raise RuntimeError(
                "global_discovery.bootstrap_readback_failed: required schema "
                f"missing after bootstrap; observed={sorted(names)}"
            )
        # Blocker 24: positively validate the required vector indexes exist, so a
        # swallowed/failed CREATE_VECTOR_INDEX cannot reach durable completion and
        # clear the marker.
        from okto_pulse.community.adapters.global_discovery_schema import (
            VECTOR_INDEXES,
        )

        observed_index_cells = {
            str(cell)
            for row in index_result.rows
            for cell in row
            if cell is not None
        }
        missing_indexes = [
            idx_name
            for _table, idx_name, _col in VECTOR_INDEXES
            if idx_name not in observed_index_cells
        ]
        if missing_indexes:
            raise RuntimeError(
                "global_discovery.bootstrap_readback_failed: required vector "
                f"index(es) missing after bootstrap: {sorted(missing_indexes)}"
            )

    def ensure_layer_schema(self) -> tuple[str, ...]:
        from okto_pulse.community.adapters.global_discovery_schema import (
            ensure_decision_digest_layer_column,
        )

        with ladybug_writer_scope(
            scope="_global",
            phase="ensure_layer_schema",
        ):
            with self._lifecycle.exclusive():
                self.require_write_token(operation="ensure_layer_schema")
                self._ensure_database_open_with_writer_lease()
                _db, conn = self._open_native()
                try:
                    return ensure_decision_digest_layer_column(conn)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def _database_is_open(self) -> bool:
        active_path = self._global_graph_path()
        with self._lock:
            return self._db is not None and self._db_path == active_path

    def _refuse_ordinary_open_if_bootstrap_marker_present(self) -> None:
        """Fail closed on the durable incomplete-bootstrap marker (blocker 8).

        Ordinary open/auto-bootstrap must never proceed while the marker exists.
        A present/partial primary under a marker is recovery-only truth; an
        absent primary under a marker may be retried only by the explicit
        ``init`` path after its narrow physically-absent proof.  This refuses all
        ordinary runtime opens/mutations so they cannot open, use, auto-bootstrap
        over, or clear either marker state.
        """

        from okto_pulse.community.adapters.global_discovery_bootstrap_marker import (
            bootstrap_marker_present,
        )

        legacy = self._legacy_global_graph_path()
        try:
            present = bootstrap_marker_present(legacy)
        except OSError as exc:
            raise RuntimeError(
                "global_discovery_marker_stat_io_error: cannot stat the "
                "incomplete-bootstrap marker; refusing ordinary operation "
                "(fail closed, zero mutation)"
            ) from exc
        if present:
            raise RuntimeError(
                "global_discovery_bootstrap_incomplete: a durable "
                "incomplete-bootstrap marker is present; ordinary Global "
                "Discovery operations are refused until the recovery ceremony "
                "reconciles it (or `init` retries a physically-absent primary). "
                "Zero mutation."
            )

    def _ensure_database_open_with_writer_lease(self) -> None:
        """Bootstrap/open while caller owns writer then lifecycle-exclusive."""

        from okto_pulse.community.adapters.global_discovery_schema import (
            raise_existing_global_graph_open_failed,
        )

        # INV-E2 gate: no ordinary open/auto-bootstrap over a live marker.
        self._refuse_ordinary_open_if_bootstrap_marker_present()

        path = self._global_graph_path()
        with self._lock:
            path_changed = self._db is not None and self._db_path != path
        if path_changed:
            self._close_with_writer_lease()
        if not path.exists():
            self._close_with_writer_lease()
            self._bootstrap_with_writer_lease()
        with self._lock:
            if self._db is not None:
                self._clear_corrupt_open_failure(successful_path=path)
                return
            try:
                self._db = self._open_global_database(
                    path,
                    on_corruption=lambda exc: self._record_corrupt_open_failure(
                        path=path,
                        exc=exc,
                    ),
                )
                self._db_path = path
            except Exception as exc:
                self._record_corrupt_open_failure(path=path, exc=exc)
                raise_existing_global_graph_open_failed(
                    storage_locator=path,
                    operation="open_connection",
                    exc=exc,
                )
            self._clear_corrupt_open_failure(successful_path=path)

    def _open_native(
        self,
        *,
        load_vector_extension: bool = True,
    ) -> tuple[Any, Any]:
        # Blocker 23: recheck the durable marker immediately before EVERY physical
        # connection borrow, so a marker published by another process after this
        # runtime warmed its handle makes the warm fast path (and every borrow)
        # refuse at once — for both marker+primary and marker+absent — with zero
        # open/mutation/clear.  This closes the warm-handle observation-to-use gap.
        self._refuse_ordinary_open_if_bootstrap_marker_present()

        path = self._global_graph_path()
        if not path.exists():
            raise RuntimeError(
                "global_discovery.lifecycle_invariant: artifact missing before "
                "connection borrow"
            )

        with self._lock:
            if self._db is None:
                raise RuntimeError(
                    "global_discovery.lifecycle_invariant: database not opened "
                    "before connection borrow"
                )
            if self._db_path != path:
                raise RuntimeError(
                    "global_discovery.lifecycle_invariant: active generation changed "
                    "before connection borrow"
                )
            conn = self._runtime().new_connection(self._db)
        if load_vector_extension:
            # INSTALL mutates the artifact and belongs exclusively to
            # bootstrap/migration.  LOAD is lazy because it also grows the WAL.
            self._runtime().load_vector_extension(conn, install=False)
        return self._db, conn

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        requires_vector = _statement_requires_vector_extension(statement)
        if _statement_is_write(statement) or requires_vector:
            self.require_write_token(
                operation=f"execute_{_statement_kind(statement).lower()}"
            )
            phase = (
                f"execute_{_statement_kind(statement).lower()}"
                if _statement_is_write(statement)
                else "execute_vector_load"
            )
            with ladybug_writer_scope(
                scope="_global",
                phase=phase,
            ):
                with self._lifecycle.exclusive():
                    self._ensure_database_open_with_writer_lease()
                with self._lifecycle.shared():
                    return self._execute_with_writer_lease(statement, params)
        # Warm non-vector reads stay concurrent. A cold read releases its
        # shared borrow before acquiring writer -> exclusive, avoiding the
        # shared->writer / writer->exclusive lock-order inversion.
        with self._lifecycle.shared():
            if self._database_is_open():
                return self._execute_with_writer_lease(statement, params)
        with ladybug_writer_scope(
            scope="_global",
            phase="execute_first_open",
        ):
            with self._lifecycle.exclusive():
                self._ensure_database_open_with_writer_lease()
            with self._lifecycle.shared():
                return self._execute_with_writer_lease(statement, params)

    def _execute_with_writer_lease(
        self,
        statement: str,
        params: dict[str, Any] | None,
    ) -> GraphStatementResult:
        native_scope = None
        try:
            _db, native_scope = self._open_native(
                load_vector_extension=_statement_requires_vector_extension(statement)
            )
            native_result = (
                native_scope.execute(statement, params)
                if params
                else native_scope.execute(statement)
            )
            return _materialize(native_result)
        except Exception as exc:
            raise map_graph_error(exc, operation="global_graph_statement") from exc
        finally:
            if native_scope is not None:
                try:
                    native_scope.close()
                except Exception:
                    pass

    def search_decision_digests(
        self,
        query_vector: list[float],
        *,
        board_ids: tuple[str, ...],
        graph_layer: str,
        top_k: int,
        min_similarity: float,
        exhaustive: bool = False,
    ) -> list[dict[str, Any]]:
        """Run indexed or exhaustive semantic search behind the graph port."""

        if not board_ids or top_k <= 0:
            return []

        if not exhaustive:
            try:
                result = self.execute(
                    "CALL QUERY_VECTOR_INDEX("
                    "'DecisionDigest', 'digest_embedding_idx', $vec, $search_k) "
                    "WITH node, distance "
                    "MATCH (b:Board)-[:CONTAINS_DECISION]->(node) "
                    "WHERE b.board_id IN $boards "
                    f"AND {tpl.layer_filter_clause('node')} "
                    "RETURN b.board_id, node.id, node.original_node_id, "
                    "node.title, node.one_line_summary, node.node_type, "
                    f"{tpl.layer_label_projection('node')}, distance "
                    "ORDER BY distance ASC LIMIT $search_k",
                    {
                        "vec": query_vector,
                        "search_k": top_k,
                        "boards": list(board_ids),
                        "graph_layer": graph_layer,
                    },
                )
                hits: list[dict[str, Any]] = []
                for row in result.rows:
                    similarity = max(0.0, min(1.0, 1.0 - float(row[7])))
                    if similarity >= min_similarity:
                        hits.append(
                            {
                                "board_id": row[0],
                                "digest_id": row[1],
                                "id": row[2],
                                "title": row[3],
                                "summary": row[4],
                                "node_type": row[5],
                                "graph_layer": row[6],
                                "similarity": similarity,
                            }
                        )
                if hits:
                    return hits[:top_k]
            except Exception as exc:
                logger.debug("global_discovery.index_search_failed err=%s", exc)

        try:
            result = self.execute(
                "MATCH (b:Board)-[:CONTAINS_DECISION]->(d:DecisionDigest) "
                "WHERE b.board_id IN $boards AND d.embedding IS NOT NULL "
                f"AND {tpl.layer_filter_clause('d')} "
                "RETURN b.board_id, d.id, d.original_node_id, d.title, "
                "d.one_line_summary, d.node_type, "
                f"{tpl.layer_label_projection('d')}, d.embedding LIMIT 500",
                {"boards": list(board_ids), "graph_layer": graph_layer},
            )
        except Exception as exc:
            logger.debug("global_discovery.exhaustive_search_failed err=%s", exc)
            return []

        query_norm = sum(value * value for value in query_vector) ** 0.5 or 1.0
        scored: list[dict[str, Any]] = []
        for row in result.rows:
            embedding = row[7]
            if not embedding or len(embedding) != len(query_vector):
                continue
            dot = sum(a * b for a, b in zip(query_vector, embedding))
            embedding_norm = sum(value * value for value in embedding) ** 0.5 or 1.0
            similarity = max(0.0, min(1.0, dot / (query_norm * embedding_norm)))
            if similarity < min_similarity:
                continue
            scored.append(
                {
                    "board_id": row[0],
                    "digest_id": row[1],
                    "id": row[2],
                    "title": row[3],
                    "summary": row[4],
                    "node_type": row[5],
                    "graph_layer": row[6],
                    "similarity": similarity,
                }
            )
        scored.sort(key=lambda item: item["similarity"], reverse=True)
        return scored[:top_k]

    def list_schema_objects(self) -> tuple[str, ...]:
        result = self.execute("CALL SHOW_TABLES() RETURN name")
        return tuple(sorted(str(row[0]) for row in result.rows if row and row[0]))

    def upsert_board_summary(
        self,
        *,
        board_id: str,
        name: str,
        summary: str,
        summary_embedding: list[float],
        decision_count: int,
        synced_at: str,
    ) -> None:
        # The existence probe and the following CREATE/SET form one logical
        # upsert.  Keep the process-wide Ladybug writer lease across both
        # statements so concurrent outbox calls cannot both observe a miss.
        with ladybug_writer_scope(
            scope="_global",
            phase="upsert_board_summary",
        ):
            self._upsert_board_summary_with_writer_lease(
                board_id=board_id,
                name=name,
                summary=summary,
                summary_embedding=summary_embedding,
                decision_count=decision_count,
                synced_at=synced_at,
            )

    def _upsert_board_summary_with_writer_lease(
        self,
        *,
        board_id: str,
        name: str,
        summary: str,
        summary_embedding: list[float],
        decision_count: int,
        synced_at: str,
    ) -> None:
        existing = self.execute(
            "MATCH (b:Board {board_id: $board_id}) RETURN b.board_id",
            {"board_id": board_id},
        )
        if existing.rows:
            self.execute(
                "MATCH (b:Board {board_id: $board_id}) "
                "SET b.decision_count = $decision_count, "
                "b.last_sync_at = timestamp($synced_at)",
                {
                    "board_id": board_id,
                    "decision_count": decision_count,
                    "synced_at": synced_at,
                },
            )
            return
        self.execute(
            "CREATE (b:Board {"
            "board_id: $board_id, name: $name, summary: $summary, "
            "summary_embedding: $embedding, topic_count: 0, entity_count: 0, "
            "decision_count: $decision_count, "
            "last_sync_at: timestamp($synced_at)})",
            {
                "board_id": board_id,
                "name": name,
                "summary": summary,
                "embedding": summary_embedding,
                "decision_count": decision_count,
                "synced_at": synced_at,
            },
        )

    def upsert_decision_digest(
        self,
        *,
        digest_id: str,
        board_id: str,
        original_node_id: str,
        title: str,
        summary: str,
        node_type: str,
        graph_layer: str,
        embedding: list[float],
        created_at: str,
    ) -> str:
        # Ladybug auto-commits every statement and does not expose an explicit
        # transaction API through this runtime.  Holding the process writer
        # lease across MATCH + CREATE/SET makes the check-create sequence
        # atomic with respect to every in-process graph writer.
        with ladybug_writer_scope(
            scope="_global",
            phase="upsert_decision_digest",
        ):
            return self._upsert_decision_digest_with_writer_lease(
                digest_id=digest_id,
                board_id=board_id,
                original_node_id=original_node_id,
                title=title,
                summary=summary,
                node_type=node_type,
                graph_layer=graph_layer,
                embedding=embedding,
                created_at=created_at,
            )

    def _upsert_decision_digest_with_writer_lease(
        self,
        *,
        digest_id: str,
        board_id: str,
        original_node_id: str,
        title: str,
        summary: str,
        node_type: str,
        graph_layer: str,
        embedding: list[float],
        created_at: str,
    ) -> str:
        existing = self.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id "
            "RETURN d.id",
            {
                "board_id": board_id,
                "original_node_id": original_node_id,
            },
        )
        canonical = self.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id "
            "RETURN d.board_id, d.original_node_id",
            {"digest_id": digest_id},
        )
        values = {
            "digest_id": digest_id,
            "board_id": board_id,
            "original_node_id": original_node_id,
            "title": title,
            "summary": summary,
            "node_type": node_type,
            "graph_layer": graph_layer,
        }
        existing_ids = [str(row[0]) for row in existing.rows if row and row[0]]
        canonical_identity_is_unique = (
            len(canonical.rows) == 1
            and str(canonical.rows[0][0] or "") == board_id
            and str(canonical.rows[0][1] or "") == original_node_id
        )
        healthy_existing_shape = (
            len(existing_ids) == 1
            and existing_ids[0] == digest_id
            and canonical_identity_is_unique
        )
        if (existing.rows and not healthy_existing_shape) or (
            not existing.rows and bool(canonical.rows)
        ):
            self._replace_and_verify_decision_digest_identity(
                digest_id=digest_id,
                board_id=board_id,
                original_node_id=original_node_id,
                title=title,
                summary=summary,
                node_type=node_type,
                graph_layer=graph_layer,
                embedding=embedding,
                created_at=created_at,
            )
            return "updated"
        if existing.rows:
            try:
                self.execute(
                    "MATCH (d:DecisionDigest) "
                    "WHERE d.board_id = $board_id "
                    "AND d.original_node_id = $original_node_id "
                    "SET d.board_id = $board_id, "
                    "d.original_node_id = $original_node_id, d.title = $title, "
                    "d.one_line_summary = $summary, d.node_type = $node_type, "
                    "d.graph_layer = $graph_layer",
                    values,
                )
            except Exception as exc:
                if not _is_duplicate_primary_key_error(exc):
                    raise
                self._replace_and_verify_decision_digest_identity(
                    digest_id=digest_id,
                    board_id=board_id,
                    original_node_id=original_node_id,
                    title=title,
                    summary=summary,
                    node_type=node_type,
                    graph_layer=graph_layer,
                    embedding=embedding,
                    created_at=created_at,
                    _allow_missing_duplicate_pk_recovery=True,
                )
                return "updated"
            self._verify_decision_digest_identity(
                digest_id=digest_id,
                board_id=board_id,
                original_node_id=original_node_id,
                graph_layer=graph_layer,
            )
            return "updated"
        try:
            self.execute(
                "CREATE (d:DecisionDigest {"
                "id: $digest_id, board_id: $board_id, "
                "original_node_id: $original_node_id, title: $title, "
                "one_line_summary: $summary, node_type: $node_type, "
                "graph_layer: $graph_layer, embedding: $embedding, "
                "created_at: timestamp($created_at)})",
                {**values, "embedding": embedding, "created_at": created_at},
            )
        except Exception as exc:
            if not _is_duplicate_primary_key_error(exc):
                raise
            self._replace_and_verify_decision_digest_identity(
                digest_id=digest_id,
                board_id=board_id,
                original_node_id=original_node_id,
                title=title,
                summary=summary,
                node_type=node_type,
                graph_layer=graph_layer,
                embedding=embedding,
                created_at=created_at,
                _allow_missing_duplicate_pk_recovery=True,
            )
            return "updated"
        self._verify_decision_digest_identity(
            digest_id=digest_id,
            board_id=board_id,
            original_node_id=original_node_id,
            graph_layer=graph_layer,
        )
        return "created"

    def _replace_and_verify_decision_digest_identity(
        self,
        *,
        digest_id: str,
        board_id: str,
        original_node_id: str,
        title: str,
        summary: str,
        node_type: str,
        graph_layer: str,
        embedding: list[float],
        created_at: str,
        _allow_missing_duplicate_pk_recovery: bool = False,
    ) -> None:
        replace_values = {
            "digest_id": digest_id,
            "board_id": board_id,
            "original_node_id": original_node_id,
            "title": title,
            "summary": summary,
            "node_type": node_type,
            "graph_layer": graph_layer,
            "embedding": embedding,
            "created_at": created_at,
        }
        if _allow_missing_duplicate_pk_recovery:
            # This capability is intentionally private and only supplied by
            # the two duplicate-primary-key catches above.  A corrupt Ladybug
            # PK index can reject CREATE while both semantic and canonical
            # scans remain empty; publishing staging is the only way to retain
            # a discoverable recovery marker before classifying that ghost.
            self.replace_decision_digest_identity(
                **replace_values,
                _allow_missing_duplicate_pk_recovery=True,
            )
        else:
            self.replace_decision_digest_identity(**replace_values)
        self._verify_decision_digest_identity(
            digest_id=digest_id,
            board_id=board_id,
            original_node_id=original_node_id,
            graph_layer=graph_layer,
        )

    def _verify_decision_digest_identity(
        self,
        *,
        digest_id: str,
        board_id: str,
        original_node_id: str,
        graph_layer: str,
    ) -> None:
        result = self.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id "
            "RETURN d.id, coalesce(d.graph_layer, 'legacy_unknown')",
            {
                "board_id": board_id,
                "original_node_id": original_node_id,
            },
        )
        canonical = self.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id "
            "RETURN d.board_id, d.original_node_id, "
            "coalesce(d.graph_layer, 'legacy_unknown')",
            {"digest_id": digest_id},
        )
        semantic_rows = list(result.rows)
        canonical_rows = list(canonical.rows)
        if (
            len(semantic_rows) != 1
            or str(semantic_rows[0][0] or "") != digest_id
            or str(semantic_rows[0][1] or "legacy_unknown") != graph_layer
            or len(canonical_rows) != 1
            or str(canonical_rows[0][0] or "") != board_id
            or str(canonical_rows[0][1] or "") != original_node_id
            or str(canonical_rows[0][2] or "legacy_unknown") != graph_layer
        ):
            raise RuntimeError(
                "global_discovery.digest_upsert_verification_failed: "
                f"board={board_id} identity={original_node_id} "
                f"semantic_rows={len(semantic_rows)} "
                f"canonical_rows={len(canonical_rows)}"
            )

    def replace_decision_digest_identity(
        self,
        *,
        digest_id: str,
        board_id: str,
        original_node_id: str,
        title: str,
        summary: str,
        node_type: str,
        graph_layer: str,
        embedding: list[float],
        created_at: str,
        _allow_missing_duplicate_pk_recovery: bool = False,
    ) -> int:
        """Replace every physical row for one source identity with one digest.

        Historical check-then-create races can leave multiple physical rows
        even though ``DecisionDigest.id`` is a primary key.  A lookup by that
        key only exposes one row in affected Ladybug files, so repair must
        match the semantic identity ``(board_id, original_node_id)``.

        A healthy identity is repaired in one atomic statement. A historically
        duplicated primary-key index needs a staged repair instead: Ladybug can
        hide the canonical row from a PK lookup and can validate mutations
        before duplicate deletion becomes visible. The staged path first
        publishes a deterministic recovery row, commits pure deletion of every
        canonical/semantic duplicate, then atomically swaps the recovery row
        for the requested key. A crash at either boundary leaves a discoverable
        semantic row, and retry resumes from the deterministic staging key.
        """

        with ladybug_writer_scope(
            scope="_global",
            phase="replace_decision_digest_identity",
        ):
            identity_params = {
                "digest_id": digest_id,
                "board_id": board_id,
                "original_node_id": original_node_id,
            }
            semantic_rows = self.execute(
                "MATCH (d:DecisionDigest) "
                "WHERE d.board_id = $board_id "
                "AND d.original_node_id = $original_node_id "
                "RETURN d.id",
                identity_params,
            )
            canonical_rows = self.execute(
                "MATCH (d:DecisionDigest) "
                "WHERE coalesce(d.id, '') = $digest_id RETURN d.id",
                identity_params,
            )
            semantic_ids = tuple(
                str(row[0]) for row in semantic_rows.rows if row and row[0]
            )
            staging_id = _digest_repair_staging_id(
                digest_id=digest_id,
                board_id=board_id,
                original_node_id=original_node_id,
            )
            identity_missing_from_scans = (
                not semantic_rows.rows and not canonical_rows.rows
            )
            if identity_missing_from_scans and not _allow_missing_duplicate_pk_recovery:
                raise RuntimeError(
                    "global_discovery.digest_replace_failed: board or digest "
                    "identity was not found"
                )
            relationship_queries = (
                "MATCH (d:DecisionDigest)-[r:DECISION_MENTIONS_ENTITY]->"
                "(other:Entity) WHERE ((d.board_id = $board_id AND "
                "d.original_node_id = $original_node_id) OR "
                "coalesce(d.id, '') = $digest_id) "
                "RETURN count(r)",
                "MATCH (d:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
                "(other:DecisionDigest) WHERE ((d.board_id = $board_id AND "
                "d.original_node_id = $original_node_id) OR "
                "coalesce(d.id, '') = $digest_id) "
                "RETURN count(r)",
                "MATCH (other:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
                "(d:DecisionDigest) WHERE ((d.board_id = $board_id AND "
                "d.original_node_id = $original_node_id) OR "
                "coalesce(d.id, '') = $digest_id) "
                "RETURN count(r)",
            )
            non_board_relationships = sum(
                int(result.rows[0][0] or 0) if result.rows else 0
                for result in (
                    self.execute(query, identity_params)
                    for query in relationship_queries
                )
            )
            if non_board_relationships:
                raise RuntimeError(
                    "global_discovery.digest_replace_relationships_present: "
                    f"board={board_id} identity={original_node_id} "
                    f"relationship_count={non_board_relationships}"
                )
            mutation_params = {
                "digest_id": digest_id,
                "board_id": board_id,
                "original_node_id": original_node_id,
                "title": title,
                "summary": summary,
                "node_type": node_type,
                "graph_layer": graph_layer,
                "embedding": embedding,
                "created_at": created_at,
            }
            staged_repair_required = (
                identity_missing_from_scans
                or staging_id in semantic_ids
                or len(canonical_rows.rows) > 1
                or (
                    digest_id in semantic_ids
                    and (not canonical_rows.rows or len(semantic_ids) > 1)
                )
            )
            if staged_repair_required:
                return self._replace_decision_digest_via_staging(
                    params=mutation_params,
                    staging_id=staging_id,
                    staging_present=staging_id in semantic_ids,
                    _literal_pk_rebuild_preflight=(
                        identity_missing_from_scans
                        and _allow_missing_duplicate_pk_recovery
                    ),
                )
            try:
                if canonical_rows.rows:
                    result = self.execute(
                        "MATCH (b:Board {board_id: $board_id}) "
                        "MATCH (replacement:DecisionDigest) "
                        "WHERE coalesce(replacement.id, '') = $digest_id "
                        "SET replacement.board_id = $board_id, "
                        "replacement.original_node_id = $original_node_id, "
                        "replacement.title = $title, "
                        "replacement.one_line_summary = $summary, "
                        "replacement.node_type = $node_type, "
                        "replacement.graph_layer = $graph_layer "
                        "WITH b, replacement "
                        "OPTIONAL MATCH (duplicate:DecisionDigest) "
                        "WHERE duplicate.board_id = $board_id "
                        "AND duplicate.original_node_id = $original_node_id "
                        "AND id(duplicate) <> id(replacement) "
                        "DETACH DELETE duplicate "
                        "WITH b, replacement, count(duplicate) + 1 AS removed "
                        "OPTIONAL MATCH "
                        "(:Board)-[link:CONTAINS_DECISION]->(replacement) "
                        "DELETE link "
                        "WITH b, replacement, removed, "
                        "count(link) AS removed_links "
                        "CREATE (b)-[:CONTAINS_DECISION]->(replacement) "
                        "RETURN removed, replacement.id",
                        mutation_params,
                    )
                else:
                    result = self.execute(
                        "MATCH (b:Board {board_id: $board_id}) "
                        "MATCH (d:DecisionDigest) "
                        "WHERE d.board_id = $board_id "
                        "AND d.original_node_id = $original_node_id "
                        "DETACH DELETE d "
                        "WITH b, count(d) AS removed "
                        "CREATE (replacement:DecisionDigest {"
                        "id: $digest_id, board_id: $board_id, "
                        "original_node_id: $original_node_id, title: $title, "
                        "one_line_summary: $summary, node_type: $node_type, "
                        "graph_layer: $graph_layer, embedding: $embedding, "
                        "created_at: timestamp($created_at)}) "
                        "CREATE (b)-[:CONTAINS_DECISION]->(replacement) "
                        "RETURN removed, replacement.id",
                        mutation_params,
                    )
            except Exception as exc:
                if not _is_duplicate_primary_key_error(exc):
                    raise
                return self._replace_decision_digest_via_staging(
                    params=mutation_params,
                    staging_id=staging_id,
                    staging_present=False,
                )
        if not result.rows:
            raise RuntimeError(
                "global_discovery.digest_replace_failed: board or digest "
                "identity was not found"
            )
        return int(result.rows[0][0])

    def _replace_decision_digest_via_staging(
        self,
        *,
        params: dict[str, Any],
        staging_id: str,
        staging_present: bool,
        _literal_pk_rebuild_preflight: bool = False,
    ) -> int:
        """Repair a corrupt PK index without ever leaving no semantic row."""

        staged_params = {**params, "staging_id": staging_id}
        if not staging_present:
            try:
                staged = self.execute(
                    "MATCH (b:Board {board_id: $board_id}) "
                    "CREATE (staging:DecisionDigest {"
                    "id: $staging_id, board_id: $board_id, "
                    "original_node_id: $original_node_id, title: $title, "
                    "one_line_summary: $summary, node_type: $node_type, "
                    "graph_layer: $graph_layer, embedding: $embedding, "
                    "created_at: timestamp($created_at)}) "
                    "CREATE (b)-[:CONTAINS_DECISION]->(staging) "
                    "RETURN staging.id",
                    staged_params,
                )
            except Exception as exc:
                if not _is_duplicate_primary_key_error(exc):
                    raise
                staged = self.execute(
                    "MATCH (staging:DecisionDigest) "
                    "WHERE staging.board_id = $board_id "
                    "AND staging.original_node_id = $original_node_id "
                    "AND coalesce(staging.id, '') = $staging_id "
                    "RETURN staging.id",
                    staged_params,
                )
            if not staged.rows:
                raise RuntimeError(
                    "global_discovery.digest_replace_staging_failed: "
                    f"board={params['board_id']} "
                    f"identity={params['original_node_id']}"
                )

        removed_count = 0
        if _literal_pk_rebuild_preflight:
            self._raise_literal_decision_digest_rebuild_required(
                params=staged_params,
            )

        removed = self.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id "
            "AND coalesce(d.id, '') <> $staging_id "
            "DETACH DELETE d WITH count(d) AS removed RETURN removed",
            staged_params,
        )
        if removed.rows:
            removed_count += int(removed.rows[0][0] or 0)
        hidden_canonical = self.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE coalesce(d.id, '') = $digest_id "
            "DETACH DELETE d WITH count(d) AS removed RETURN removed",
            staged_params,
        )
        if hidden_canonical.rows:
            removed_count += int(hidden_canonical.rows[0][0] or 0)

        remaining = self.execute(
            "MATCH (d:DecisionDigest) "
            "WHERE (d.board_id = $board_id "
            "AND d.original_node_id = $original_node_id "
            "AND coalesce(d.id, '') <> $staging_id) "
            "OR coalesce(d.id, '') = $digest_id "
            "RETURN d.id",
            staged_params,
        )
        if remaining.rows:
            raise RuntimeError(
                "global_discovery.digest_replace_cleanup_incomplete: "
                f"board={params['board_id']} "
                f"identity={params['original_node_id']} "
                f"remaining={len(remaining.rows)}"
            )

        swap_statement = (
            "MATCH (b:Board {board_id: $board_id}) "
            "MATCH (staging:DecisionDigest) "
            "WHERE staging.board_id = $board_id "
            "AND staging.original_node_id = $original_node_id "
            "DETACH DELETE staging "
            "WITH b, count(staging) AS removed_staging "
            "CREATE (replacement:DecisionDigest {"
            "id: $digest_id, board_id: $board_id, "
            "original_node_id: $original_node_id, title: $title, "
            "one_line_summary: $summary, node_type: $node_type, "
            "graph_layer: $graph_layer, embedding: $embedding, "
            "created_at: timestamp($created_at)}) "
            "CREATE (b)-[:CONTAINS_DECISION]->(replacement) "
            "RETURN replacement.id"
        )
        replacement: GraphStatementResult | None = None
        for primary_drain in range(_DIGEST_REPAIR_MAX_PRIMARY_DRAINS + 1):
            try:
                replacement = self.execute(swap_statement, staged_params)
                break
            except Exception as exc:
                if not _is_duplicate_primary_key_error(exc):
                    raise
                physical_rows = self.execute(
                    "MATCH (d:DecisionDigest) "
                    "WHERE coalesce(d.id, '') = $digest_id RETURN d.id",
                    staged_params,
                )
                if not physical_rows.rows:
                    literal_rows = self._literal_decision_digest_primary_rows(
                        params=staged_params,
                    )
                    if literal_rows.rows:
                        self._raise_literal_decision_digest_rebuild_required(
                            params=staged_params,
                        )
                    raise self._digest_pk_index_irreparable(
                        params=staged_params,
                        reason="literal_lookup_empty",
                    ) from exc
                if primary_drain >= _DIGEST_REPAIR_MAX_PRIMARY_DRAINS:
                    raise RuntimeError(
                        "global_discovery.digest_pk_index_irreparable "
                        f"board={params['board_id']} "
                        f"identity={params['original_node_id']} "
                        f"digest={params['digest_id']} "
                        "reason=primary_drain_limit "
                        "staging_preserved=true "
                        "recovery=global_discovery_rebuild_then_requeue"
                    ) from exc
                self.execute(
                    "MATCH (d:DecisionDigest) "
                    "WHERE coalesce(d.id, '') = $digest_id "
                    "DETACH DELETE d "
                    "WITH count(d) AS removed RETURN removed",
                    staged_params,
                )
        if replacement is None:
            raise RuntimeError(
                "global_discovery.digest_replace_staging_swap_failed: "
                f"board={params['board_id']} "
                f"identity={params['original_node_id']}"
            )
        if not replacement.rows:
            raise RuntimeError(
                "global_discovery.digest_replace_staging_swap_failed: "
                f"board={params['board_id']} "
                f"identity={params['original_node_id']}"
            )
        return removed_count or 1

    def _literal_decision_digest_primary_rows(
        self,
        *,
        params: dict[str, Any],
    ) -> GraphStatementResult:
        return self.execute(
            "MATCH (d:DecisionDigest {id: $digest_id}) "
            "RETURN d.id, d.board_id, d.original_node_id",
            params,
        )

    def _digest_pk_index_irreparable(
        self,
        *,
        params: dict[str, Any],
        reason: str,
        details: str = "",
    ) -> RuntimeError:
        detail_suffix = f" {details}" if details else ""
        return RuntimeError(
            "global_discovery.digest_pk_index_irreparable "
            f"board={params['board_id']} "
            f"identity={params['original_node_id']} "
            f"digest={params['digest_id']} "
            f"reason={reason}{detail_suffix} "
            "staging_preserved=true "
            "recovery=global_discovery_rebuild_then_requeue"
        )

    def _raise_literal_decision_digest_rebuild_required(
        self,
        *,
        params: dict[str, Any],
    ) -> NoReturn:
        """Classify an index-only row without mutating the corrupt artifact.

        Literal DELETE + CREATE can look converged until CHECKPOINT/reopen makes
        semantic and coalesce scans lose the row again. Once this shape is
        detected, only a rebuild into a fresh database is a durable recovery.
        """

        literal_rows = self._literal_decision_digest_primary_rows(params=params)
        if not literal_rows.rows:
            raise self._digest_pk_index_irreparable(
                params=params,
                reason="literal_lookup_empty",
            )

        relationship_queries = (
            "MATCH (d:DecisionDigest {id: $digest_id})"
            "-[r:DECISION_MENTIONS_ENTITY]->(:Entity) RETURN count(r)",
            "MATCH (d:DecisionDigest {id: $digest_id})"
            "-[r:DECISION_DERIVES_FROM]->(:DecisionDigest) RETURN count(r)",
            "MATCH (:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
            "(d:DecisionDigest {id: $digest_id}) RETURN count(r)",
        )
        rows = literal_rows.rows
        if len(rows) != 1 or len(rows[0]) < 3:
            raise self._digest_pk_index_irreparable(
                params=params,
                reason="literal_lookup_cardinality",
                details=f"literal_rows={len(rows)}",
            )
        observed_id, observed_board_id, observed_original_node_id = rows[0][:3]
        if (
            str(observed_id or "") != str(params["digest_id"])
            or str(observed_board_id or "") != str(params["board_id"])
            or str(observed_original_node_id or "") != str(params["original_node_id"])
        ):
            raise self._digest_pk_index_irreparable(
                params=params,
                reason="literal_identity_mismatch",
            )

        relationship_count = sum(
            int(result.rows[0][0] or 0) if result.rows else 0
            for result in (
                self.execute(query, params) for query in relationship_queries
            )
        )
        if relationship_count:
            raise self._digest_pk_index_irreparable(
                params=params,
                reason="literal_relationships_present",
                details=f"relationship_count={relationship_count}",
            )

        raise self._digest_pk_index_irreparable(
            params=params,
            reason="literal_rebuild_required",
        )

    def delete_decision_digests_guarded(
        self,
        *,
        board_id: str,
        original_node_ids: tuple[str, ...],
        include_malformed: bool = False,
    ) -> int:
        """Delete a complete stale set only when derived edges are absent.

        The preflight covers every target before the first delete and remains
        under the process writer lease through the deletion statement. This
        prevents stale/unembedded cleanup from silently erasing clustering
        semantics via ``DETACH DELETE``.
        """

        with ladybug_writer_scope(
            scope="_global",
            phase="delete_decision_digests_guarded",
        ):
            params = {
                "board_id": board_id,
                "original_node_ids": list(original_node_ids),
            }
            target_predicate = (
                "d.board_id = $board_id AND (d.original_node_id IN $original_node_ids"
            )
            if include_malformed:
                target_predicate += (
                    " OR d.original_node_id IS NULL OR d.original_node_id = ''"
                )
            target_predicate += ")"
            relationship_queries = (
                "MATCH (d:DecisionDigest)-[r:DECISION_MENTIONS_ENTITY]->"
                f"(:Entity) WHERE {target_predicate} RETURN count(r)",
                "MATCH (d:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
                f"(:DecisionDigest) WHERE {target_predicate} RETURN count(r)",
                "MATCH (:DecisionDigest)-[r:DECISION_DERIVES_FROM]->"
                f"(d:DecisionDigest) WHERE {target_predicate} RETURN count(r)",
            )
            derived_relationship_count = sum(
                int(result.rows[0][0] or 0) if result.rows else 0
                for result in (
                    self.execute(query, params) for query in relationship_queries
                )
            )
            if derived_relationship_count:
                raise RuntimeError(
                    "global_discovery.digest_prune_relationships_present: "
                    f"board={board_id} "
                    f"relationship_count={derived_relationship_count}"
                )
            result = self.execute(
                "MATCH (d:DecisionDigest) "
                f"WHERE {target_predicate} "
                "DETACH DELETE d WITH count(d) AS removed RETURN removed",
                params,
            )
        return int(result.rows[0][0] or 0) if result.rows else 0

    def link_board_digest(self, *, board_id: str, digest_id: str) -> None:
        self.execute(
            "MATCH (b:Board {board_id: $board_id}), "
            "(d:DecisionDigest {id: $digest_id}) "
            "MERGE (b)-[:CONTAINS_DECISION]->(d)",
            {"board_id": board_id, "digest_id": digest_id},
        )

    def normalize_board_digest_link(
        self,
        *,
        board_id: str,
        digest_id: str,
    ) -> int:
        """Replace only inbound Board links, preserving derived digest edges."""

        with ladybug_writer_scope(
            scope="_global",
            phase="normalize_board_digest_link",
        ):
            result = self.execute(
                "MATCH (b:Board {board_id: $board_id}), "
                "(d:DecisionDigest {id: $digest_id}) "
                "WHERE d.board_id = $board_id "
                "MATCH (:Board)-[r:CONTAINS_DECISION]->(d) "
                "DELETE r WITH b, d, count(r) AS removed "
                "CREATE (b)-[:CONTAINS_DECISION]->(d) "
                "RETURN removed",
                {"board_id": board_id, "digest_id": digest_id},
            )
        if not result.rows:
            raise RuntimeError(
                "global_discovery.digest_link_normalize_failed: "
                f"board={board_id} digest={digest_id}"
            )
        return int(result.rows[0][0] or 0)

    def delete_invalid_board_digest_links(
        self,
        *,
        board_id: str,
        expected_digest_ids: tuple[str, ...],
    ) -> int:
        """Remove corrupt outgoing Board links without deleting digest nodes."""

        with ladybug_writer_scope(
            scope="_global",
            phase="delete_invalid_board_digest_links",
        ):
            result = self.execute(
                "MATCH (b:Board)-[r:CONTAINS_DECISION]->(d:DecisionDigest) "
                "WHERE b.board_id = $board_id AND ("
                "coalesce(d.board_id, '') <> $board_id "
                "OR NOT (d.id IN $expected_digest_ids)) "
                "DELETE r WITH count(r) AS removed RETURN removed",
                {
                    "board_id": board_id,
                    "expected_digest_ids": list(expected_digest_ids),
                },
            )
        return int(result.rows[0][0] or 0) if result.rows else 0

    @contextmanager
    def post_write_verification_scope(self) -> Iterator[None]:
        """Keep flush, close/reopen and fresh reads isolated from all users."""

        with ladybug_writer_scope(
            scope="_global",
            phase="post_write_verification",
        ):
            with self._lifecycle.exclusive():
                yield

    @staticmethod
    def _fsync_if_file(path: Path) -> None:
        if not path.is_file():
            return
        # Windows rejects os.fsync on a read-only descriptor. r+b does not
        # truncate and gives a real durability boundary for file contents.
        with path.open("r+b") as fh:
            os.fsync(fh.fileno())

    def _fsync_global_artifacts(self, path: Path) -> None:
        self._fsync_if_file(path)
        if not path.parent.exists():
            return
        for sibling in sorted(path.parent.glob(path.name + ".*")):
            self._fsync_if_file(sibling)

    def flush_after_write_batch(self) -> None:
        """Close, fsync and reopen-probe discovery.lbug after a write batch."""

        self.require_write_token(operation="flush_after_write_batch")
        with ladybug_writer_scope(
            scope="_global",
            phase="flush_after_write_batch",
        ):
            with self._lifecycle.exclusive():
                self._flush_after_write_batch_with_writer_lease()

    def _flush_after_write_batch_with_writer_lease(self) -> None:

        path = self._global_graph_path()
        self.close()
        if not path.exists():
            raise RuntimeError(f"global discovery file missing at {path}")

        self._fsync_global_artifacts(path)

        self._ensure_database_open_with_writer_lease()
        _db, conn = self._open_native(load_vector_extension=False)
        try:
            res = conn.execute("CALL SHOW_TABLES() RETURN name")
            try:
                if res.has_next():
                    res.get_next()
            finally:
                if hasattr(res, "close"):
                    res.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
            self.close()

        self._fsync_global_artifacts(path)

    def close(self) -> None:
        with ladybug_writer_scope(
            scope="_global",
            phase="close_global_discovery",
        ):
            with self._lifecycle.exclusive():
                self._close_with_writer_lease()

    def _close_with_writer_lease(self) -> None:
        with self._lock:
            db = self._db
            if db is None:
                return
            self._db = None
            self._db_path = None
        if hasattr(db, "close"):
            try:
                db.close()
            except Exception as exc:
                logger.warning(
                    "global_connection.close_failed err=%s",
                    exc,
                    extra={"event": "global_connection.close_failed"},
                )
        del db
        gc.collect()

    def purge(self, *, reason: str = "manual") -> GraphPurgeResult:
        self.require_write_token(operation="purge")
        with ladybug_writer_scope(
            scope="_global",
            phase="purge_global_discovery",
        ):
            with self._lifecycle.exclusive():
                return self._purge_with_writer_lease(reason=reason)

    def _purge_with_writer_lease(self, *, reason: str) -> GraphPurgeResult:
        from okto_pulse.core.kg.quarantine import QuarantineError

        path = self._global_graph_path()
        self.close()
        targets: list[Path] = []
        if path.exists():
            targets.append(path)
        if path.parent.exists():
            targets.extend(sorted(path.parent.glob(path.name + ".*")))

        if not targets:
            return GraphPurgeResult(
                board_id="_global",
                removed=False,
                not_found=True,
                status="not_found",
                reason=reason,
                backend="community_local_graph",
            )

        service = self._quarantine_service()
        try:
            from okto_pulse.community.adapters.local_storage_ref import (
                local_storage_ref,
            )

            response = service.create(
                board_id="_global",
                graph_type="global_discovery",
                affected_storage_refs=[local_storage_ref(t) for t in targets],
                reason=reason,
                correlation_ids=[],
            )
        except QuarantineError as exc:
            logger.error(
                "global_discovery.purge_blocked_quarantine_failed "
                "reason=%s code=%s err=%s",
                reason,
                exc.code.value,
                exc.reason,
                extra={
                    "event": "global_discovery.purge_blocked_quarantine_failed",
                    "reason": reason,
                    "code": exc.code.value,
                },
            )
            return GraphPurgeResult(
                board_id="_global",
                removed=False,
                not_found=False,
                status="failed",
                reason=reason,
                backend="community_local_graph",
                error_code=exc.code.value,
            )

        moved_count = response.files_moved
        removed = [str(t) for t in targets[:moved_count]]
        logger.warning(
            "global_discovery.purged reason=%s removed=%d quarantine_id=%s manifest=%s",
            reason,
            moved_count,
            response.quarantine_id,
            response.manifest_ref,
            extra={
                "event": "global_discovery.purged",
                "reason": reason,
                "quarantine_id": response.quarantine_id,
                "manifest_ref": response.manifest_ref,
                "files_moved": moved_count,
            },
        )
        return GraphPurgeResult(
            board_id="_global",
            removed=bool(removed),
            not_found=not bool(removed),
            status="purged" if removed else "not_found",
            reason=reason,
            backend="community_local_graph",
        )

    def reset_for_tests(self) -> None:
        self.close()


__all__ = ["CommunityGlobalDiscoveryRuntime"]
