"""Community HTTP mapping for selective Knowledge propagation v2 errors."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import status
from fastapi.responses import JSONResponse

from okto_pulse.core.application.knowledge_propagation_projection import (
    project_knowledge_propagation_error,
)
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgePropagationContractError,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgePropagationPortError,
    get_knowledge_mutation_audit_sink,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgePropagationServiceError,
)
from okto_pulse.core.repositories import PulseUnitOfWork, UnitOfWorkFactory


_CONFLICT_CODES = frozenset(
    {
        "knowledge_propagation_idempotency_conflict",
        "knowledge_propagation_revision_conflict",
        "knowledge_propagation_concurrent_write",
        "knowledge_propagation_target_collision",
        "knowledge_propagation_constraint_conflict",
        "knowledge_propagation_parent_changed",
        "knowledge_propagation_parent_not_eligible",
        "knowledge_propagation_preflight_stale",
        "knowledge_propagation_supersession_conflict",
        "knowledge_propagation_temporal_conflict",
        "knowledge_creation_target_collision",
        "knowledge_creation_replay_target_mismatch",
        "knowledge_creation_race",
    }
)
_NOT_FOUND_CODES = frozenset(
    {
        "knowledge_propagation_target_not_found",
        "knowledge_propagation_parent_not_found",
        "card_not_found",
        "refinement_not_found",
    }
)
_SERVICE_UNAVAILABLE_CODES = frozenset(
    {
        "knowledge_propagation_port_not_configured",
        "knowledge_propagation_effective_read_failed",
        "knowledge_propagation_parent_evidence_read_failed",
        "knowledge_propagation_ledger_read_failed",
        "knowledge_propagation_scope_read_failed",
    }
)
_ResultT = TypeVar("_ResultT")


async def execute_knowledge_creation_with_one_retry(
    *,
    uow: PulseUnitOfWork,
    uow_factory: UnitOfWorkFactory,
    actor: ActorContext,
    operation: Callable[[PulseUnitOfWork], Awaitable[_ResultT]],
) -> _ResultT:
    """Retry a normalized first-create race once in a fresh transaction."""

    try:
        return await operation(uow)
    except (KnowledgePropagationPortError, KnowledgePropagationServiceError) as exc:
        if exc.code != "knowledge_creation_race":
            raise
        await uow.rollback()
        await uow.close()

    realm_scope = uow_factory.resolve_realm_scope()
    async with uow_factory(realm_scope=realm_scope, actor=actor) as retry_uow:
        return await operation(retry_uow)


async def rollback_and_record_knowledge_error(
    uow: PulseUnitOfWork,
    error: KnowledgePropagationServiceError,
) -> None:
    """Close the failed write before appending its independent audit attempt."""

    await uow.rollback()
    await uow.close()
    if error.ledger_attempt is not None:
        await get_knowledge_mutation_audit_sink().append_after_rollback(
            error.ledger_attempt
        )


def knowledge_propagation_error_response(
    error: (
        KnowledgePropagationContractError
        | KnowledgePropagationPortError
        | KnowledgePropagationServiceError
    ),
) -> JSONResponse:
    """Return the stable REST envelope shared with the MCP boundary."""

    code = error.code
    if code in _CONFLICT_CODES:
        status_code = status.HTTP_409_CONFLICT
    elif code in _NOT_FOUND_CODES:
        status_code = status.HTTP_404_NOT_FOUND
    elif code in _SERVICE_UNAVAILABLE_CODES or code.endswith("_unavailable"):
        status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    return JSONResponse(
        status_code=status_code,
        content=project_knowledge_propagation_error(error),
    )


__all__ = [
    "KnowledgePropagationContractError",
    "KnowledgePropagationPortError",
    "KnowledgePropagationServiceError",
    "execute_knowledge_creation_with_one_retry",
    "knowledge_propagation_error_response",
    "rollback_and_record_knowledge_error",
]
