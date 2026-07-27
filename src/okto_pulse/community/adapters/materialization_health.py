"""Community materialization evidence, census, and generation adapters.

Graph observations are metadata-only. Relational evidence is read in an
isolated async session under the request's monotonic deadline. Normal KG writes
advance the durable generation inside their audit/outbox transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable

from sqlalchemy import func, or_, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision,
    AppSetting,
    Card,
    ConsolidationDeadLetter,
    ConsolidationQueue,
    GlobalUpdateOutbox,
    Ideation,
    Refinement,
    Spec,
    Sprint,
    Story,
)
from okto_pulse.core.kg.board_source_store import decision_sources_from_spec
from okto_pulse.core.kg.interfaces.graph_runtime_store import (
    GraphRuntimeObservationState,
    GraphRuntimeState,
)
from okto_pulse.core.kg.interfaces.storage_ref import StorageRef
from okto_pulse.core.ports.materialization_health import (
    BoardHealthCensus,
    CensusStatus,
    HealthProbeDeadline,
    MaterializationEvidence,
    MaterializationEvidenceRequest,
    record_first_write_acknowledged,
    run_bounded_health_probe,
)
from okto_pulse.core.ports.global_outbox import (
    GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL,
    GLOBAL_OUTBOX_MAX_RETRIES,
)

logger = logging.getLogger("okto_pulse.community.materialization_health")

INITIAL_MATERIALIZATION_GENERATION = "unmaterialized-v1"
_GENERATION_KEY_PREFIX = "kg_mat_gen:"
_BOARD_STAT_PROBE = "materialization_board_stat"
_DISCOVERY_STAT_PROBE = "materialization_discovery_stat"
_CENSUS_SESSION_CLEANUP_TIMEOUT_SECONDS = 2.0
_CENSUS_SESSION_CLEANUP_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _teardown_census_session(
    session: Any,
    exit_: Callable[..., Awaitable[None]] | None,
    exc_info: tuple[type[BaseException] | None, BaseException | None, Any],
) -> None:
    """Rollback and close one census session as an indivisible cleanup task."""

    try:
        in_transaction = getattr(session, "in_transaction", None)
        if callable(in_transaction) and in_transaction():
            await session.rollback()
    finally:
        if exit_ is not None:
            await exit_(*exc_info)
        else:
            await session.close()


async def _drain_census_session_cleanup(
    cleanup: Awaitable[None],
) -> None:
    """Drain rollback/close before the preparation's temporary loop exits.

    The session and its teardown must stay on the loop/thread that created
    them. A first deadline requests cancellation; a second deadline only emits
    the explicit restart-required boundary. Production process supervision owns
    termination if a driver ignores cancellation indefinitely.
    """

    loop = asyncio.get_running_loop()
    cleanup_task = loop.create_task(cleanup)
    deadline = loop.time() + _CENSUS_SESSION_CLEANUP_TIMEOUT_SECONDS
    cancellation: asyncio.CancelledError | None = None
    parent_cancel_requested = False
    while not cleanup_task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({cleanup_task}, timeout=remaining)
        except asyncio.CancelledError as exc:
            # Do not let repeated task cancellation strand a checked-out
            # connection. Promptly request cancellation of the teardown task
            # so a cancellation-aware driver can enter rollback/close without
            # waiting out the normal cleanup grace period. The cancellation is
            # re-raised only after that teardown drains.
            cancellation = exc
            parent_cancel_requested = True
            break

    if cleanup_task.done():
        cleanup_task.result()
        if cancellation is not None:
            raise cancellation
        return

    if parent_cancel_requested:
        logger.info(
            "kg.materialization_census.session_cleanup_cancel_requested",
            extra={
                "event": (
                    "kg.materialization_census.session_cleanup_cancel_requested"
                ),
            },
        )
    else:
        logger.error(
            "kg.materialization_census.session_cleanup_timeout timeout_s=%.3f",
            _CENSUS_SESSION_CLEANUP_TIMEOUT_SECONDS,
            extra={
                "event": "kg.materialization_census.session_cleanup_timeout",
                "timeout_s": _CENSUS_SESSION_CLEANUP_TIMEOUT_SECONDS,
            },
        )
    cleanup_task.cancel()

    cancel_deadline = (
        loop.time() + _CENSUS_SESSION_CLEANUP_CANCEL_DRAIN_TIMEOUT_SECONDS
    )
    while not cleanup_task.done():
        remaining = cancel_deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({cleanup_task}, timeout=remaining)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            continue

    if not cleanup_task.done():
        logger.critical(
            "kg.materialization_census.session_cleanup_restart_required "
            "cancel_drain_timeout_s=%.3f",
            _CENSUS_SESSION_CLEANUP_CANCEL_DRAIN_TIMEOUT_SECONDS,
            extra={
                "event": (
                    "kg.materialization_census.session_cleanup_restart_required"
                ),
                "cancel_drain_timeout_s": (
                    _CENSUS_SESSION_CLEANUP_CANCEL_DRAIN_TIMEOUT_SECONDS
                ),
                "process_boundary_required": True,
            },
        )
        # Do not move a live AsyncSession to another loop/thread. A
        # non-cooperative driver requires process supervision to terminate the
        # worker; repeated cancellation is the only safe in-process action.
        cleanup_task.cancel()
        if cancellation is not None:
            raise cancellation
        raise TimeoutError(
            "materialization census session cleanup requires process restart"
        )

    with suppress(BaseException):
        cleanup_task.result()
    if cancellation is not None:
        raise cancellation
    raise TimeoutError("materialization census session cleanup timed out")


@asynccontextmanager
async def _cancel_safe_census_session_scope(
    session_factory: Callable[..., Any],
) -> AsyncIterator[Any]:
    scope = session_factory()
    enter = getattr(scope, "__aenter__", None)
    exit_ = getattr(scope, "__aexit__", None)
    if callable(enter) and callable(exit_):
        session = await enter()
    else:
        session = scope
        exit_ = None
    exc_info: tuple[type[BaseException] | None, BaseException | None, Any] = (
        None,
        None,
        None,
    )
    try:
        yield session
    except BaseException as exc:
        exc_info = (type(exc), exc, exc.__traceback__)
        raise
    finally:
        await _drain_census_session_cleanup(
            _teardown_census_session(session, exit_, exc_info)
        )


def materialization_generation_key(board_id: str) -> str:
    """Return a bounded, board-scoped AppSetting key without leaking IDs."""

    digest = hashlib.sha256(str(board_id).encode("utf-8")).hexdigest()[:48]
    return f"{_GENERATION_KEY_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class MaterializationGenerationAdvance:
    board_id: str
    previous_generation: str
    generation: str
    correlation_id: str | None


class CommunityMaterializationGenerationStore:
    """Durable per-board generation backed by the existing AppSetting table."""

    def __init__(
        self,
        session_factory: Callable[..., Any],
        *,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._sf = session_factory
        self._token_factory = token_factory or (lambda: f"mg_{uuid.uuid4().hex}")

    async def current(self, board_id: str) -> str:
        async with self._sf() as session:
            row = await session.get(
                AppSetting,
                materialization_generation_key(board_id),
            )
            return (
                str(row.value)
                if row is not None
                else INITIAL_MATERIALIZATION_GENERATION
            )

    async def advance_in_session(
        self,
        session: Any,
        *,
        board_id: str,
        correlation_id: str | None = None,
    ) -> MaterializationGenerationAdvance:
        key = materialization_generation_key(board_id)
        row = await session.get(AppSetting, key)
        previous = (
            str(row.value) if row is not None else INITIAL_MATERIALIZATION_GENERATION
        )
        generation = str(self._token_factory()).strip()
        if not generation or len(generation) > 64:
            raise ValueError("materialization_generation_token_invalid")
        if generation == previous:
            raise ValueError("materialization_generation_must_advance")
        if row is None:
            session.add(AppSetting(key=key, value=generation))
        else:
            row.value = generation
        await session.flush()
        return MaterializationGenerationAdvance(
            board_id=str(board_id),
            previous_generation=previous,
            generation=generation,
            correlation_id=(
                str(correlation_id) if correlation_id is not None else None
            ),
        )

    @staticmethod
    def log_advanced(advance: MaterializationGenerationAdvance) -> None:
        """Emit the post-commit integration event with bounded generation IDs."""

        logger.info(
            "kg.materialization_generation_advanced board=%s correlation=%s",
            advance.board_id,
            advance.correlation_id,
            extra={
                "event": "kg.materialization_generation_advanced",
                "board_id": advance.board_id,
                "correlation_id": advance.correlation_id,
                "previous_generation_sha256": hashlib.sha256(
                    advance.previous_generation.encode("utf-8")
                ).hexdigest()[:16],
                "generation_sha256": hashlib.sha256(
                    advance.generation.encode("utf-8")
                ).hexdigest()[:16],
            },
        )
        try:
            record_first_write_acknowledged(
                board_id=advance.board_id,
                previous_generation=advance.previous_generation,
                generation=advance.generation,
                correlation_id=advance.correlation_id,
                is_first_write=(
                    advance.previous_generation == INITIAL_MATERIALIZATION_GENERATION
                ),
            )
        except Exception as exc:  # observability must never block write ACK
            logger.warning(
                "kg.materialization_first_write_observability_failed board=%s error=%s",
                advance.board_id,
                type(exc).__name__,
                extra={
                    "event": ("kg.materialization_first_write_observability_failed"),
                    "board_id": advance.board_id,
                    "error_type": type(exc).__name__,
                },
            )


def _unavailable_census(
    *,
    generation: str | None,
    reason_code: str,
) -> BoardHealthCensus:
    return BoardHealthCensus(
        generation=generation,
        status=CensusStatus.UNAVAILABLE,
        source_count=None,
        queue_depth=None,
        active_queue_count=None,
        dead_letter_count=None,
        global_outbox_dead_letter_count=None,
        reason_code=reason_code,
        observed_at=_utcnow(),
    )


class CommunitySqlAlchemyMaterializationCensus:
    """Authoritative board-scoped source/queue/DLQ read model."""

    _SOURCE_MODELS = (
        Story,
        Ideation,
        Refinement,
        Sprint,
        Card,
        AmendmentHotfixRevision,
    )

    def __init__(self, session_factory: Callable[..., Any]) -> None:
        self._sf = session_factory

    async def snapshot(
        self,
        board_id: str,
        *,
        generation: str,
        deadline: HealthProbeDeadline,
    ) -> BoardHealthCensus:
        remaining = deadline.remaining_seconds(now=time.monotonic())
        if remaining <= 0.0:
            return _unavailable_census(
                generation=generation,
                reason_code="board_census_timeout",
            )
        try:
            async with asyncio.timeout(remaining):
                return await self._snapshot(
                    board_id=str(board_id),
                    generation=str(generation),
                    deadline=deadline,
                )
        except TimeoutError:
            return _unavailable_census(
                generation=generation,
                reason_code="board_census_timeout",
            )
        except Exception:
            return _unavailable_census(
                generation=generation,
                reason_code="board_census_io_error",
            )

    async def _snapshot(
        self,
        *,
        board_id: str,
        generation: str,
        deadline: HealthProbeDeadline,
    ) -> BoardHealthCensus:
        def count(model: Any, *predicates: Any) -> Any:
            return (
                select(func.count())
                .select_from(model)
                .where(*predicates)
                .scalar_subquery()
            )

        source_counts = [
            count(model, model.board_id == board_id).label(model.__tablename__)
            for model in self._SOURCE_MODELS
        ]
        active_queue_filter = (
            ConsolidationQueue.board_id == board_id,
            ConsolidationQueue.status.in_(("pending", "claimed")),
        )
        active_outbox_filter = (
            GlobalUpdateOutbox.board_id == board_id,
            GlobalUpdateOutbox.processed_at.is_(None),
            GlobalUpdateOutbox.retry_count >= 0,
            GlobalUpdateOutbox.retry_count < GLOBAL_OUTBOX_MAX_RETRIES,
            GlobalUpdateOutbox.retry_count != GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL,
        )
        terminal_outbox_filter = (
            GlobalUpdateOutbox.board_id == board_id,
            GlobalUpdateOutbox.processed_at.is_(None),
            or_(
                GlobalUpdateOutbox.retry_count == GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL,
                GlobalUpdateOutbox.retry_count >= GLOBAL_OUTBOX_MAX_RETRIES,
            ),
        )
        statement = select(
            *source_counts,
            count(ConsolidationQueue, *active_queue_filter).label(
                "consolidation_active"
            ),
            count(GlobalUpdateOutbox, *active_outbox_filter).label("outbox_active"),
            count(
                ConsolidationDeadLetter,
                ConsolidationDeadLetter.board_id == board_id,
            ).label("consolidation_dead_letter"),
            count(GlobalUpdateOutbox, *terminal_outbox_filter).label("outbox_terminal"),
        )

        async with _cancel_safe_census_session_scope(self._sf) as session:
            row = (await session.execute(statement)).one()._mapping
            if deadline.expired(now=time.monotonic()):
                raise TimeoutError
            specs = (
                await session.execute(
                    select(
                        Spec.id,
                        Spec.version,
                        Spec.title,
                        Spec.created_at,
                        Spec.decisions,
                    ).where(Spec.board_id == board_id)
                )
            ).all()
            if deadline.expired(now=time.monotonic()):
                raise TimeoutError

        source_count = sum(
            int(row[model.__tablename__] or 0) for model in self._SOURCE_MODELS
        )
        source_count += len(specs)
        source_count += sum(
            len(decision_sources_from_spec(spec._mapping)) for spec in specs
        )
        consolidation_active = int(row["consolidation_active"] or 0)
        outbox_active = int(row["outbox_active"] or 0)
        return BoardHealthCensus(
            generation=generation,
            status=CensusStatus.AVAILABLE,
            source_count=source_count,
            queue_depth=consolidation_active,
            active_queue_count=consolidation_active + outbox_active,
            dead_letter_count=int(row["consolidation_dead_letter"] or 0),
            global_outbox_dead_letter_count=int(row["outbox_terminal"] or 0),
            reason_code="board_census_available",
            observed_at=_utcnow(),
        )


def _probe_timeout_state(
    *,
    board_id: str,
    generation: str,
    storage_ref: Any,
    backend: str,
    reason_code: str,
) -> GraphRuntimeState:
    return GraphRuntimeState.from_observation(
        board_id=board_id,
        storage_ref=storage_ref,
        state=GraphRuntimeObservationState.PRESENT_UNREADABLE_OR_ERROR,
        generation=generation,
        reason_code=reason_code,
        observed_at=_utcnow(),
        backend=backend,
        details={"source": "bounded_materialization_evidence"},
    )


class CommunityMaterializationEvidenceProbe:
    """Collect one generation-fenced snapshot under one absolute deadline."""

    def __init__(
        self,
        *,
        board_store: Any,
        census: Any,
        discovery_store: Any,
        generation_store: Any,
        mutation_guard: Any | None = None,
    ) -> None:
        self._board_store = board_store
        self._census = census
        self._discovery_store = discovery_store
        self._generation_store = generation_store
        self._mutation_guard = mutation_guard

    async def current_generation(self, board_id: str) -> str:
        return await self._generation_store.current(board_id)

    async def probe(
        self,
        request: MaterializationEvidenceRequest,
    ) -> MaterializationEvidence:
        if self._mutation_guard is None:
            return await self._collect_evidence(request)

        before = self._mutation_guard.capture(request.board_id)
        try:
            evidence = await self._collect_evidence(request)
        except BaseException:
            self._mutation_guard.complete(
                board_id=request.board_id,
                before=before,
            )
            raise
        guard_result = self._mutation_guard.complete(
            board_id=request.board_id,
            before=before,
        )
        if guard_result.outcome == "violation":
            evidence = replace(
                evidence,
                census=_unavailable_census(
                    generation=None,
                    reason_code="health_read_side_mutation_detected",
                ),
            )
        return evidence

    async def _collect_evidence(
        self,
        request: MaterializationEvidenceRequest,
    ) -> MaterializationEvidence:
        generation = request.generation
        board_fallback = _probe_timeout_state(
            board_id=request.board_id,
            generation=generation,
            storage_ref=StorageRef(
                f"board:{request.board_id}",
                "community_local_graph",
            ),
            backend="community_local_graph",
            reason_code="board_graph_probe_timeout",
        )
        discovery_fallback = _probe_timeout_state(
            board_id="_global",
            generation=generation,
            storage_ref=StorageRef(
                "global-discovery",
                "community_local_graph",
            ),
            backend="community_local_graph",
            reason_code="global_discovery_probe_timeout",
        )

        async def board_probe() -> GraphRuntimeState:
            result = await run_bounded_health_probe(
                name=_BOARD_STAT_PROBE,
                board_id=request.board_id,
                generation_id=generation,
                build=lambda: self._board_store.graph_state(
                    request.board_id,
                    generation=generation,
                ),
                fallback=board_fallback,
                deadline_at=request.deadline.deadline_at,
            )
            return result.value

        async def discovery_probe() -> GraphRuntimeState:
            result = await run_bounded_health_probe(
                name=_DISCOVERY_STAT_PROBE,
                board_id=request.board_id,
                generation_id=generation,
                build=lambda: self._discovery_store.state(generation=generation),
                fallback=discovery_fallback,
                deadline_at=request.deadline.deadline_at,
            )
            return result.value

        board_store, census, discovery_store = await asyncio.gather(
            board_probe(),
            self._census.snapshot(
                request.board_id,
                generation=generation,
                deadline=request.deadline,
            ),
            discovery_probe(),
        )

        remaining = request.deadline.remaining_seconds(now=time.monotonic())
        if remaining <= 0.0:
            census = _unavailable_census(
                generation=None,
                reason_code="materialization_generation_check_timeout",
            )
        else:
            try:
                observed_generation = await asyncio.wait_for(
                    self._generation_store.current(request.board_id),
                    timeout=remaining,
                )
            except TimeoutError:
                census = _unavailable_census(
                    generation=None,
                    reason_code="materialization_generation_check_timeout",
                )
            except Exception:
                census = _unavailable_census(
                    generation=None,
                    reason_code="materialization_generation_provider_unavailable",
                )
            else:
                if observed_generation != generation:
                    census = replace(
                        census,
                        generation=observed_generation,
                        reason_code="materialization_generation_changed",
                    )

        return MaterializationEvidence(
            board_store=board_store,
            census=census,
            discovery_store=discovery_store,
        )


__all__ = [
    "INITIAL_MATERIALIZATION_GENERATION",
    "CommunityMaterializationEvidenceProbe",
    "CommunityMaterializationGenerationStore",
    "CommunitySqlAlchemyMaterializationCensus",
    "MaterializationGenerationAdvance",
    "materialization_generation_key",
]
