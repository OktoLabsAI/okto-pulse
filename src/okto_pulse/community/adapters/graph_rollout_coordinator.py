"""Fail-closed Board rollout orchestration for Ladybug to Grafx.

This module owns only the durable control plane.  Engine-specific fixed
snapshots, logical copying, candidate certification and result comparison are
supplied through :class:`BoardGraphShadowCyclePort`; the coordinator never
turns physical discovery into a routing authority.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphError,
)

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.graph_backend_binding import (
    CommunityGraphBackendBinding,
    CommunityGraphBackendBindingStore,
)
from okto_pulse.community.adapters.graph_rollout_journal import (
    CommunityGraphRolloutJournal,
    ComparisonReceipt,
    GraphRolloutRecord,
    ReplayCheckpoint,
    RolloutDivergence,
    RolloutEndpointIdentity,
)
from okto_pulse.community.config import (
    PULSE_GRAFX_DEFAULT_PAGE_SIZE,
    validate_grafx_page_size,
)


class BoardGraphRolloutRefused(GraphCapabilityUnavailable):
    """A requested rollout action is not legal in the durable state."""

    code = "board_graph_rollout_refused"


class BoardGraphRolloutInvariantError(GraphCorruption):
    """The binding and journal authorities form no safe protocol state."""

    code = "board_graph_rollout_invariant_error"


@dataclass(frozen=True, slots=True)
class ShadowCaptureRequest:
    board_id: str
    source: RolloutEndpointIdentity
    through_seq: int


@dataclass(frozen=True, slots=True)
class ShadowCopyEvidence:
    source_fingerprint: str
    target_fingerprint: str


@dataclass(frozen=True, slots=True)
class ShadowComparisonEvidence:
    corpus_sha256: str
    source_result_sha256: str
    target_result_sha256: str
    query_count: int
    details: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BoardGraphShadowCycleResult:
    rollout: GraphRolloutRecord
    through_seq: int
    checkpoint: ReplayCheckpoint | None
    receipt: ComparisonReceipt | None
    divergence: RolloutDivergence | None

    @property
    def matched(self) -> bool:
        return self.checkpoint is not None and self.receipt is not None


class BoardGraphShadowCyclePort(Protocol):
    """Backend-specific work performed around the durable control plane.

    ``capture_fixed_source`` runs while the Board mutation window is held and
    must return already-open fixed views which remain valid until
    ``close_fixed_source``.  Copy/certification/comparison run after that short
    freeze.  The candidate context must cold-open the copied generation,
    validate its M-PULSE-5 certificate and run ``verify("all")`` before it
    yields the database object used for Grafx binding admission.
    """

    def capture_fixed_source(self, request: ShadowCaptureRequest) -> object: ...

    def copy_snapshot(
        self,
        capture: object,
        candidate: RolloutEndpointIdentity,
    ) -> ShadowCopyEvidence: ...

    def open_certified_candidate(
        self,
        *,
        board_id: str,
        candidate: RolloutEndpointIdentity,
        expected_fingerprint: str | None = None,
    ) -> AbstractContextManager[object]: ...

    def compare_fixed_views(
        self,
        capture: object,
        candidate: RolloutEndpointIdentity,
        candidate_database: object,
    ) -> ShadowComparisonEvidence: ...

    def close_fixed_source(self, capture: object) -> None: ...


class BoardMutationWindow(Protocol):
    def __call__(
        self, board_id: str, *, phase: str
    ) -> AbstractContextManager[None]: ...


JournalFactory = Callable[[str], CommunityGraphRolloutJournal]
GenerationFactory = Callable[[str], str]


def _refused(reason: str, *, operation: str, **details: object) -> Exception:
    return BoardGraphRolloutRefused(
        "The Board graph rollout operation was refused.",
        details={"operation": operation, "reason": reason, **details},
    )


def _invariant(
    reason: str, *, operation: str, board_id: str, **details: object
) -> Exception:
    return BoardGraphRolloutInvariantError(
        "The Board graph binding and rollout journal disagree.",
        details={
            "operation": operation,
            "reason": reason,
            "scope": "board",
            "scope_id": board_id,
            **details,
        },
    )


def _default_generation(_board_id: str) -> str:
    return f"rollout-{uuid.uuid4().hex}"


class CommunityBoardGraphRolloutCoordinator:
    """Orchestrate one Board migration without dual-write or in-place copy."""

    def __init__(
        self,
        binding_store: CommunityGraphBackendBindingStore,
        shadow_port: BoardGraphShadowCyclePort,
        *,
        journal_factory: JournalFactory | None = None,
        mutation_window: BoardMutationWindow = kg_runtime.board_storage_mutation_window,
        generation_factory: GenerationFactory = _default_generation,
        grafx_page_size: int = PULSE_GRAFX_DEFAULT_PAGE_SIZE,
    ) -> None:
        if not callable(mutation_window) or not callable(generation_factory):
            raise _refused(
                "rollout_port_invalid", operation="configure_board_graph_rollout"
            )
        try:
            self._page_size = validate_grafx_page_size(grafx_page_size)
            root = Path(binding_store.root)
        except (AttributeError, TypeError, ValueError) as exc:
            raise _refused(
                "rollout_configuration_invalid",
                operation="configure_board_graph_rollout",
            ) from exc
        self._bindings = binding_store
        self._shadow = shadow_port
        self._journal_factory = journal_factory or (
            lambda board_id: CommunityGraphRolloutJournal(root, board_id)
        )
        self._mutation_window = mutation_window
        self._generation_factory = generation_factory

    def start(self, board_id: str) -> GraphRolloutRecord:
        """Start from the authenticated Ladybug binding and a fresh target."""

        operation = "start_board_graph_rollout"
        journal = self._journal(board_id)
        with self._mutation_window(board_id, phase=operation):
            existing = journal.read_if_exists()
            if existing is not None:
                return self._recover_locked(
                    board_id=board_id,
                    journal=journal,
                    operation=operation,
                )

            source_binding = self._bindings.acquire_board_binding(board_id)
            if source_binding.backend != "ladybug":
                raise _refused(
                    "rollout_requires_ladybug_source",
                    operation=operation,
                    board_id=board_id,
                    observed_backend=source_binding.backend,
                )
            generation = self._fresh_generation(board_id, current=None)
            candidate_path = self._bindings.board_grafx_path(board_id, generation)
            self._require_absent_candidate(
                candidate_path, board_id=board_id, operation=operation
            )
            return journal.start(
                source=self._endpoint(source_binding),
                candidate=RolloutEndpointIdentity(
                    backend="grafx",
                    binding_sha256=None,
                    generation=generation,
                    physical_path=candidate_path,
                    page_size=self._page_size,
                ),
            )

    def run_shadow_cycle(self, board_id: str) -> BoardGraphShadowCycleResult:
        """Build, certify, reconcile and compare one fixed source boundary."""

        operation = "run_board_graph_shadow_cycle"
        journal = self._journal(board_id)
        capture: object | None = None

        with self._mutation_window(board_id, phase=f"{operation}.capture"):
            rollout = self._recover_locked(
                board_id=board_id,
                journal=journal,
                operation=operation,
            )
            if rollout.state != "shadowing":
                raise _refused(
                    "shadow_cycle_state_invalid",
                    operation=operation,
                    board_id=board_id,
                    observed_state=rollout.state,
                )
            binding = self._bindings.acquire_board_binding(board_id)
            self._require_binding_matches(
                binding,
                rollout.source,
                board_id=board_id,
                operation=operation,
            )
            through_seq = journal.capture_high_water()
            candidate = self._cycle_candidate(board_id, rollout)
            capture = self._shadow.capture_fixed_source(
                ShadowCaptureRequest(
                    board_id=board_id,
                    source=rollout.source,
                    through_seq=through_seq,
                )
            )

        try:
            copy = self._shadow.copy_snapshot(capture, candidate)
            with self._shadow.open_certified_candidate(
                board_id=board_id, candidate=candidate
            ) as database:
                prospective = self._bindings.prepare_board_binding_candidate(
                    board_id=board_id,
                    backend="grafx",
                    generation=candidate.generation,
                    physical_path=candidate.physical_path,
                    page_size=candidate.page_size,
                    database=database,
                )
                certified_candidate = self._endpoint(prospective)
                comparison = self._shadow.compare_fixed_views(
                    capture,
                    certified_candidate,
                    database,
                )
        finally:
            # A close failure deliberately prevents all durable certification.
            if capture is not None:
                self._shadow.close_fixed_source(capture)

        with self._mutation_window(board_id, phase=f"{operation}.publish"):
            current = self._recover_locked(
                board_id=board_id,
                journal=journal,
                operation=operation,
            )
            if current.state != "shadowing":
                raise _refused(
                    "shadow_cycle_state_changed",
                    operation=operation,
                    board_id=board_id,
                    observed_state=current.state,
                )
            active = self._bindings.acquire_board_binding(board_id)
            self._require_binding_matches(
                active,
                rollout.source,
                board_id=board_id,
                operation=operation,
            )
            if current.state_version != rollout.state_version:
                raise _refused(
                    "shadow_cycle_cas_stale",
                    operation=operation,
                    board_id=board_id,
                    expected_version=rollout.state_version,
                    observed_version=current.state_version,
                )
            if current.candidate.binding_sha256 is None:
                if self._same_candidate_location(current.candidate, candidate):
                    current = journal.certify_candidate(
                        expected_version=current.state_version,
                        candidate_binding_sha256=prospective.binding_sha256,
                    )
                else:
                    # A process may have died after creating or partly writing
                    # the first, still-unbound generation.  Never reopen that
                    # path as an empty sink: preserve it as crash evidence and
                    # CAS the journal directly to a separately certified path.
                    current = journal.replace_candidate(
                        expected_version=current.state_version,
                        expected_candidate=current.candidate,
                        replacement=certified_candidate,
                    )
            else:
                current = journal.replace_candidate(
                    expected_version=current.state_version,
                    expected_candidate=current.candidate,
                    replacement=certified_candidate,
                )

            if copy.source_fingerprint != copy.target_fingerprint:
                divergence = journal.record_divergence(
                    direction="shadow",
                    through_seq=through_seq,
                    expected_fingerprint=copy.source_fingerprint,
                    actual_fingerprint=copy.target_fingerprint,
                    generation=current.candidate.generation,
                    details={
                        "stage": "logical_fingerprint",
                        **dict(comparison.details),
                    },
                )
                return BoardGraphShadowCycleResult(
                    rollout=journal.read(),
                    through_seq=through_seq,
                    checkpoint=None,
                    receipt=None,
                    divergence=divergence,
                )

            checkpoint = journal.reconcile_snapshot(
                direction="shadow",
                through_seq=through_seq,
                expected_binding_sha256=rollout.source.binding_sha256 or "",
                source_fingerprint=copy.source_fingerprint,
                target_fingerprint=copy.target_fingerprint,
                generation=current.candidate.generation,
            )
            if comparison.source_result_sha256 != comparison.target_result_sha256:
                divergence = journal.record_divergence(
                    direction="shadow",
                    through_seq=through_seq,
                    expected_fingerprint=comparison.source_result_sha256,
                    actual_fingerprint=comparison.target_result_sha256,
                    generation=current.candidate.generation,
                    details={"stage": "result_corpus", **dict(comparison.details)},
                )
                return BoardGraphShadowCycleResult(
                    rollout=journal.read(),
                    through_seq=through_seq,
                    checkpoint=checkpoint,
                    receipt=None,
                    divergence=divergence,
                )
            receipt = journal.record_comparison_receipt(
                direction="shadow",
                through_seq=through_seq,
                generation=current.candidate.generation,
                corpus_sha256=comparison.corpus_sha256,
                source_result_sha256=comparison.source_result_sha256,
                target_result_sha256=comparison.target_result_sha256,
                query_count=comparison.query_count,
            )
            return BoardGraphShadowCycleResult(
                rollout=journal.read(),
                through_seq=through_seq,
                checkpoint=checkpoint,
                receipt=receipt,
                divergence=None,
            )

    def promote(self, board_id: str) -> GraphRolloutRecord:
        """Pass the durable canary gate and atomically route to Grafx."""

        operation = "promote_board_graph_rollout"
        journal = self._journal(board_id)
        with self._mutation_window(board_id, phase=operation):
            rollout = self._recover_locked(
                board_id=board_id,
                journal=journal,
                operation=operation,
            )
            if rollout.state in {
                "grafx_active_rollback_open",
                "grafx_active_rollback_closed",
            }:
                return rollout
            if rollout.state not in {"shadowing", "canary_ready"}:
                raise _refused(
                    "cutover_state_invalid",
                    operation=operation,
                    board_id=board_id,
                    observed_state=rollout.state,
                )
            binding = self._bindings.acquire_board_binding(board_id)
            self._require_binding_matches(
                binding,
                rollout.source,
                board_id=board_id,
                operation=operation,
            )
            if rollout.candidate.binding_sha256 is None:
                raise _refused(
                    "candidate_binding_not_certified",
                    operation=operation,
                    board_id=board_id,
                )
            expected_fingerprint = self._durable_candidate_fingerprint(
                board_id=board_id,
                journal=journal,
                candidate=rollout.candidate,
                operation=operation,
            )
            with self._verified_candidate_database(
                board_id,
                rollout.candidate,
                expected_fingerprint=expected_fingerprint,
            ) as database:
                # A failed cold reopen/certificate/verify leaves shadowing
                # intact, so another fresh shadow generation can supersede a
                # bad candidate.  The gate and binding CAS follow only while
                # the admitted candidate handle remains alive.
                if rollout.state == "shadowing":
                    rollout = journal.compare_and_set_state(
                        expected_state="shadowing",
                        expected_version=rollout.state_version,
                        new_state="canary_ready",
                    )
                rollout = journal.require_current_canary_gate(
                    expected_version=rollout.state_version
                )
                published = self._bindings.compare_and_swap_board_binding(
                    board_id=board_id,
                    expected_binding_sha256=binding.binding_sha256,
                    backend="grafx",
                    generation=rollout.candidate.generation,
                    physical_path=rollout.candidate.physical_path,
                    page_size=rollout.candidate.page_size,
                    database=database,
                )
            self._require_binding_matches(
                published,
                rollout.candidate,
                board_id=board_id,
                operation=operation,
            )
            return journal.compare_and_set_state(
                expected_state="canary_ready",
                expected_version=rollout.state_version,
                new_state="grafx_active_rollback_open",
            )

    def rollback(self, board_id: str) -> GraphRolloutRecord:
        """Restore untouched Ladybug only while the durable window is open."""

        operation = "rollback_board_graph_rollout"
        journal = self._journal(board_id)
        with self._mutation_window(board_id, phase=operation):
            rollout = self._recover_locked(
                board_id=board_id,
                journal=journal,
                operation=operation,
            )
            if rollout.state == "rolled_back":
                return rollout
            if rollout.state != "grafx_active_rollback_open":
                raise _refused(
                    "rollback_window_not_open",
                    operation=operation,
                    board_id=board_id,
                    observed_state=rollout.state,
                )
            binding = self._bindings.acquire_board_binding(board_id)
            self._require_binding_matches(
                binding,
                rollout.candidate,
                board_id=board_id,
                operation=operation,
            )
            restored = self._bindings.compare_and_swap_board_binding(
                board_id=board_id,
                expected_binding_sha256=binding.binding_sha256,
                backend="ladybug",
                generation=rollout.source.generation,
                physical_path=rollout.source.physical_path,
                page_size=None,
            )
            self._require_binding_matches(
                restored,
                rollout.source,
                board_id=board_id,
                operation=operation,
            )
            return journal.compare_and_set_state(
                expected_state="grafx_active_rollback_open",
                expected_version=rollout.state_version,
                new_state="rolled_back",
            )

    def recover(self, board_id: str) -> GraphRolloutRecord:
        """Reconcile only the explicitly safe binding/state crash pairs."""

        operation = "recover_board_graph_rollout"
        journal = self._journal(board_id)
        with self._mutation_window(board_id, phase=operation):
            return self._recover_locked(
                board_id=board_id,
                journal=journal,
                operation=operation,
            )

    def complete(self, board_id: str) -> GraphRolloutRecord:
        """Seal a rollout only after rollback closed or actually completed."""

        operation = "complete_board_graph_rollout"
        journal = self._journal(board_id)
        with self._mutation_window(board_id, phase=operation):
            rollout = self._recover_locked(
                board_id=board_id,
                journal=journal,
                operation=operation,
            )
            if rollout.state == "completed":
                return rollout
            if rollout.state not in {
                "grafx_active_rollback_closed",
                "rolled_back",
            }:
                raise _refused(
                    "rollout_completion_state_invalid",
                    operation=operation,
                    board_id=board_id,
                    observed_state=rollout.state,
                )
            required = (
                rollout.candidate
                if rollout.state == "grafx_active_rollback_closed"
                else rollout.source
            )
            binding = self._bindings.acquire_board_binding(board_id)
            self._require_binding_matches(
                binding,
                required,
                board_id=board_id,
                operation=operation,
            )
            return journal.compare_and_set_state(
                expected_state=rollout.state,
                expected_version=rollout.state_version,
                new_state="completed",
            )

    def _recover_locked(
        self,
        *,
        board_id: str,
        journal: CommunityGraphRolloutJournal,
        operation: str,
    ) -> GraphRolloutRecord:
        rollout = journal.read()
        if rollout.state == "erased":
            return rollout
        binding = self._bindings.acquire_board_binding(board_id)
        source_matches = self._binding_matches(binding, rollout.source)
        candidate_matches = self._binding_matches(binding, rollout.candidate)

        if rollout.state == "shadowing":
            if source_matches:
                return rollout
        elif rollout.state == "canary_ready":
            if source_matches:
                return rollout
            if candidate_matches:
                expected_fingerprint = self._durable_candidate_fingerprint(
                    board_id=board_id,
                    journal=journal,
                    candidate=rollout.candidate,
                    operation=operation,
                )
                with self._verified_candidate_database(
                    board_id,
                    rollout.candidate,
                    expected_fingerprint=expected_fingerprint,
                ):
                    pass
                rollout = journal.require_current_canary_gate(
                    expected_version=rollout.state_version
                )
                return journal.compare_and_set_state(
                    expected_state="canary_ready",
                    expected_version=rollout.state_version,
                    new_state="grafx_active_rollback_open",
                )
        elif rollout.state == "grafx_active_rollback_open":
            if candidate_matches:
                expected_fingerprint = self._durable_candidate_fingerprint(
                    board_id=board_id,
                    journal=journal,
                    candidate=rollout.candidate,
                    operation=operation,
                )
                with self._verified_candidate_database(
                    board_id,
                    rollout.candidate,
                    expected_fingerprint=expected_fingerprint,
                ):
                    pass
                return rollout
            if source_matches:
                return journal.compare_and_set_state(
                    expected_state="grafx_active_rollback_open",
                    expected_version=rollout.state_version,
                    new_state="rolled_back",
                )
        elif rollout.state == "grafx_active_rollback_closed":
            if candidate_matches:
                with self._verified_candidate_database(board_id, rollout.candidate):
                    pass
                return rollout
        elif rollout.state == "rolled_back":
            if source_matches:
                return rollout
        elif rollout.state == "completed" and (source_matches or candidate_matches):
            return rollout

        raise _invariant(
            "binding_state_pair_impossible",
            operation=operation,
            board_id=board_id,
            rollout_state=rollout.state,
            binding_backend=binding.backend,
            binding_sha256=binding.binding_sha256,
        )

    def _cycle_candidate(
        self, board_id: str, rollout: GraphRolloutRecord
    ) -> RolloutEndpointIdentity:
        if rollout.candidate.binding_sha256 is None and not (
            self._candidate_path_exists(
                rollout.candidate.physical_path,
                board_id=board_id,
                operation="run_board_graph_shadow_cycle",
            )
        ):
            return rollout.candidate
        generation = self._fresh_generation(
            board_id, current=rollout.candidate.generation
        )
        path = self._bindings.board_grafx_path(board_id, generation)
        self._require_absent_candidate(
            path, board_id=board_id, operation="run_board_graph_shadow_cycle"
        )
        return RolloutEndpointIdentity(
            backend="grafx",
            binding_sha256=None,
            generation=generation,
            physical_path=path,
            page_size=self._page_size,
        )

    def _fresh_generation(self, board_id: str, *, current: str | None) -> str:
        try:
            generation = self._generation_factory(board_id)
            path = self._bindings.board_grafx_path(board_id, generation)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise _refused(
                "candidate_generation_invalid",
                operation="allocate_board_graph_rollout_generation",
                board_id=board_id,
            ) from exc
        if generation == current:
            raise _refused(
                "candidate_generation_not_fresh",
                operation="allocate_board_graph_rollout_generation",
                board_id=board_id,
            )
        self._require_absent_candidate(
            path,
            board_id=board_id,
            operation="allocate_board_graph_rollout_generation",
        )
        return generation

    @contextmanager
    def _verified_candidate_database(
        self,
        board_id: str,
        candidate: RolloutEndpointIdentity,
        *,
        expected_fingerprint: str | None = None,
    ) -> Iterator[object]:
        if candidate.binding_sha256 is None:
            raise _invariant(
                "candidate_binding_not_certified",
                operation="verify_board_graph_rollout_candidate",
                board_id=board_id,
            )
        with self._shadow.open_certified_candidate(
            board_id=board_id,
            candidate=candidate,
            expected_fingerprint=expected_fingerprint,
        ) as database:
            prospective = self._bindings.prepare_board_binding_candidate(
                board_id=board_id,
                backend="grafx",
                generation=candidate.generation,
                physical_path=candidate.physical_path,
                page_size=candidate.page_size,
                database=database,
            )
            self._require_binding_matches(
                prospective,
                candidate,
                board_id=board_id,
                operation="verify_board_graph_rollout_candidate",
            )
            yield database

    @staticmethod
    def _durable_candidate_fingerprint(
        *,
        board_id: str,
        journal: CommunityGraphRolloutJournal,
        candidate: RolloutEndpointIdentity,
        operation: str,
    ) -> str:
        """Authenticate the exact cold candidate against its durable checkpoint.

        A binding certificate authenticates the location and catalog, not the
        logical rows.  Before cutover (and while rollback is still open), the
        immutable shadow checkpoint is therefore the authority for the bytes
        that may be routed.  Once rollback is closed, Grafx may legitimately
        contain later writes and callers omit this comparison.
        """

        checkpoint = journal.read_checkpoint("shadow")
        if checkpoint is None:
            raise _refused(
                "candidate_checkpoint_missing",
                operation=operation,
                board_id=board_id,
            )
        if (
            checkpoint.generation != candidate.generation
            or checkpoint.binding_sha256 != candidate.binding_sha256
            or checkpoint.physical_path != candidate.physical_path
            or checkpoint.page_size != candidate.page_size
        ):
            raise _refused(
                "candidate_checkpoint_stale",
                operation=operation,
                board_id=board_id,
            )
        if checkpoint.source_fingerprint != checkpoint.target_fingerprint:
            raise _refused(
                "candidate_checkpoint_diverged",
                operation=operation,
                board_id=board_id,
            )
        return checkpoint.target_fingerprint

    def _journal(self, board_id: str) -> CommunityGraphRolloutJournal:
        try:
            return self._journal_factory(board_id)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            if isinstance(exc, GraphError):
                raise
            raise _refused(
                "journal_factory_failed",
                operation="configure_board_graph_rollout",
                board_id=board_id,
            ) from exc

    @staticmethod
    def _endpoint(
        binding: CommunityGraphBackendBinding,
    ) -> RolloutEndpointIdentity:
        return RolloutEndpointIdentity(
            backend=binding.backend,
            binding_sha256=binding.binding_sha256,
            generation=binding.generation,
            physical_path=binding.physical_path,
            page_size=binding.page_size,
        )

    @staticmethod
    def _binding_matches(
        binding: CommunityGraphBackendBinding, endpoint: RolloutEndpointIdentity
    ) -> bool:
        return endpoint.binding_sha256 is not None and (
            binding.backend == endpoint.backend
            and binding.binding_sha256 == endpoint.binding_sha256
            and binding.generation == endpoint.generation
            and binding.physical_path == endpoint.physical_path
            and binding.page_size == endpoint.page_size
        )

    @classmethod
    def _require_binding_matches(
        cls,
        binding: CommunityGraphBackendBinding,
        endpoint: RolloutEndpointIdentity,
        *,
        board_id: str,
        operation: str,
    ) -> None:
        if not cls._binding_matches(binding, endpoint):
            raise _invariant(
                "binding_identity_mismatch",
                operation=operation,
                board_id=board_id,
                binding_backend=binding.backend,
                binding_sha256=binding.binding_sha256,
                endpoint_backend=endpoint.backend,
                endpoint_binding_sha256=endpoint.binding_sha256,
            )

    @staticmethod
    def _same_candidate_location(
        left: RolloutEndpointIdentity, right: RolloutEndpointIdentity
    ) -> bool:
        return (
            left.backend == right.backend
            and left.generation == right.generation
            and left.physical_path == right.physical_path
            and left.page_size == right.page_size
        )

    @staticmethod
    def _candidate_path_exists(path: Path, *, board_id: str, operation: str) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise _refused(
                "candidate_path_probe_failed",
                operation=operation,
                board_id=board_id,
                error_type=type(exc).__name__,
            ) from exc
        return True

    @staticmethod
    def _require_absent_candidate(path: Path, *, board_id: str, operation: str) -> None:
        if not CommunityBoardGraphRolloutCoordinator._candidate_path_exists(
            path, board_id=board_id, operation=operation
        ):
            return
        raise _refused(
            "candidate_generation_not_fresh",
            operation=operation,
            board_id=board_id,
            candidate_path=os.fspath(path),
        )


__all__ = [
    "BoardGraphRolloutInvariantError",
    "BoardGraphRolloutRefused",
    "BoardGraphShadowCyclePort",
    "BoardGraphShadowCycleResult",
    "CommunityBoardGraphRolloutCoordinator",
    "ShadowCaptureRequest",
    "ShadowComparisonEvidence",
    "ShadowCopyEvidence",
]
