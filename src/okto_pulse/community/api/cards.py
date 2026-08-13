"""Card API endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.download_headers import (
    attachment_content_disposition,
)
from okto_pulse.community.api.knowledge_propagation import (
    KnowledgePropagationContractError,
    KnowledgePropagationServiceError,
    knowledge_propagation_error_response,
    rollback_and_record_knowledge_error,
)
from okto_pulse.community.api.permission_errors import (
    permission_denied_http_error,
)
from okto_pulse.community.api.spec_dependency_errors import (
    spec_dependency_http_error,
)
from okto_pulse.core.application.knowledge_propagation_projection import (
    project_knowledge_mutation_response,
    project_refresh_response,
    project_technical_read_response,
)
from okto_pulse.core.application.use_cases.card_crud import (
    GetBugRegressionScenarioCandidatesCommand,
    GetBugRegressionScenarioCandidatesUseCase,
    GetCardActivityCommand,
    GetCardActivityUseCase,
    GetCardKnowledgeCommand,
    GetCardKnowledgeUseCase,
    GetCardSeenStatusCommand,
    GetCardSeenStatusUseCase,
    LinkTestTaskToBugCommand,
    LinkTestTaskToBugUseCase,
    ListCardKnowledgeCommand,
    ListCardKnowledgeUseCase,
    RequireCardWriteAccessCommand,
    RequireCardWriteAccessUseCase,
    UnlinkTestTaskFromBugCommand,
    UnlinkTestTaskFromBugUseCase,
)
from okto_pulse.core.application.use_cases import (
    AddCardDependencyCommand,
    AddCardDependencyUseCase,
    CommandValidationError,
    DeleteCardCommand,
    DeleteCardUseCase,
    DeleteTaskValidationCommand,
    DeleteTaskValidationUseCase,
    EntityNotFoundError,
    GetCardCommand,
    GetCardDependenciesCommand,
    GetCardDependenciesUseCase,
    GetCardDependentsCommand,
    GetCardDependentsUseCase,
    GetCardUseCase,
    GetTaskValidationCommand,
    GetTaskValidationUseCase,
    ListTaskValidationsCommand,
    ListTaskValidationsUseCase,
    MoveCardCommand,
    MoveCardUseCase,
    PermissionDeniedError,
    RemoveCardDependencyCommand,
    RemoveCardDependencyUseCase,
    SubmitTaskValidationCommand,
    SubmitTaskValidationUseCase,
    UpdateCardCommand,
    UpdateCardUseCase,
)
from okto_pulse.core.domain.spec_dependency import SpecDependencyOperationError
from okto_pulse.core.application.use_cases.knowledge_propagation import (
    DropCardKnowledgeAssignmentsCommand,
    DropCardKnowledgeAssignmentsUseCase,
    GetCardKnowledgePropagationCommand,
    GetCardKnowledgePropagationUseCase,
    RefreshCardKnowledgeAssignmentsCommand,
    RefreshCardKnowledgeAssignmentsUseCase,
    ReplaceCardKnowledgeAssignmentsCommand,
    ReplaceCardKnowledgeAssignmentsUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.domain.guideline_policy_transition import (
    PolicyTransitionRejected,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.models import (
    ActivityLogResponse,
    CardMove,
    CardResponse,
    CardUpdate,
)
from okto_pulse.core.models.knowledge_propagation import (
    KnowledgeAssignmentDropRequest,
    KnowledgeAssignmentRefreshRequest,
    KnowledgeAssignmentReplaceRequest,
    KnowledgeMutationResponse,
    KnowledgeRefreshResponse,
    KnowledgeTechnicalReadResponse,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgePropagationPortError,
)
from okto_pulse.core.application.errors import (
    CARD_RESOURCE_READ_ONLY_MESSAGE,
    CancellationReasonRequiredError,
    CardOperationError,
    CardResourceReadOnlyError,
    ResourceGateError,
)
from okto_pulse.core.services.gate_contracts import GateContractError
from okto_pulse.core.services.bug_regression_preview import (
    BugRegressionScenarioPreviewError,
)

router = APIRouter()


def _resource_gate_detail(exc: ResourceGateError) -> dict:
    return {
        "error": exc.code,
        "message": str(exc),
        "details": exc.details,
    }


async def _require_card_write_access(
    card_id: str,
    user_id: str,
    uow: PulseUnitOfWork,
    *,
    kb_id: str | None = None,
) -> None:
    try:
        await RequireCardWriteAccessUseCase().execute(
            RequireCardWriteAccessCommand(card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    if kb_id is None:
        return
    try:
        await GetCardKnowledgeUseCase().execute(
            GetCardKnowledgeCommand(card_id, kb_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        detail = (
            "Card not found"
            if exc.entity_type == "card"
            else "Knowledge entry not found"
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


@router.get("/{card_id}", response_model=CardResponse)
async def get_card(
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a card by ID with all attachments, Q&A, and comments."""
    try:
        result = await GetCardUseCase().execute(
            GetCardCommand(card_id), actor=RESTAdapterContract.actor(user_id), uow=uow
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _card_knowledge_error_response(uow, exc)
    return result.card


@router.get("/{card_id}/regression-scenario-candidates")
async def get_bug_regression_scenario_candidates(
    card_id: str,
    board_id: str = Query(...),
    affected_task_ids: list[str] | None = Query(None),
    candidate_scenario_ids: list[str] | None = Query(None),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Preview reusable regression scenarios for a bug without mutating spec/card state."""

    try:
        result = await GetBugRegressionScenarioCandidatesUseCase().execute(
            GetBugRegressionScenarioCandidatesCommand(
                card_id,
                board_id,
                affected_task_ids=affected_task_ids,
                candidate_scenario_ids=candidate_scenario_ids,
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    except BugRegressionScenarioPreviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    return result.payload


@router.patch("/{card_id}", response_model=CardResponse)
async def update_card(
    card_id: str,
    data: CardUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a card."""
    try:
        result = await UpdateCardUseCase().execute(
            UpdateCardCommand(card_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except CardOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except CardResourceReadOnlyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _card_knowledge_error_response(uow, exc)
    return result.card


@router.post("/{card_id}/move", response_model=CardResponse)
async def move_card(
    card_id: str,
    data: CardMove,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Move a card to a different column/position."""
    try:
        result = await MoveCardUseCase().execute(
            MoveCardCommand(card_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except CancellationReasonRequiredError as e:
        detail = e.to_dict()
        # Pagination/move clients consume the typed REST error discriminator
        # under ``detail.error``.  Keep the legacy ``code`` key additive for
        # existing callers while publishing the canonical route envelope.
        detail["error"] = e.code
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    except CardOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except GateContractError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except ResourceGateError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_resource_gate_detail(e),
        )
    except PolicyTransitionRejected as e:
        raise RESTAdapterContract.http_error(e) from e
    except SpecDependencyOperationError as exc:
        raise spec_dependency_http_error(exc) from exc
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _card_knowledge_error_response(uow, exc)
    return result.card


# ---- Dependencies ----


@router.get("/{card_id}/dependencies")
async def get_dependencies(
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get cards this card depends on."""
    try:
        result = await GetCardDependenciesUseCase().execute(
            GetCardDependenciesCommand(card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    return [
        {"id": d.id, "title": d.title, "status": d.status.value}
        for d in result.dependencies
    ]


@router.get("/{card_id}/dependents")
async def get_dependents(
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get cards that depend on this card."""
    try:
        result = await GetCardDependentsUseCase().execute(
            GetCardDependentsCommand(card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    return [
        {"id": d.id, "title": d.title, "status": d.status.value}
        for d in result.dependents
    ]


@router.post(
    "/{card_id}/dependencies/{depends_on_id}", status_code=status.HTTP_201_CREATED
)
async def add_dependency(
    card_id: str,
    depends_on_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Add a dependency: card_id depends on depends_on_id."""
    try:
        result = await AddCardDependencyUseCase().execute(
            AddCardDependencyCommand(card_id, depends_on_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except CardOperationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.to_dict(),
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    return {
        "id": result.dependency_id,
        "card_id": card_id,
        "depends_on_id": depends_on_id,
    }


@router.delete(
    "/{card_id}/dependencies/{depends_on_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_dependency(
    card_id: str,
    depends_on_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Remove a dependency."""
    try:
        await RemoveCardDependencyUseCase().execute(
            RemoveCardDependencyCommand(card_id, depends_on_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Dependency not found"
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc


@router.get("/{card_id}/activity", response_model=list[ActivityLogResponse])
async def get_card_activity(
    card_id: str,
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get activity log for a specific card."""
    try:
        result = await GetCardActivityUseCase().execute(
            GetCardActivityCommand(card_id, limit=limit),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    return result.activity


@router.get("/{card_id}/seen")
async def get_card_seen_status(
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get seen status for all items in a card (comments, QA) by agents."""
    try:
        result = await GetCardSeenStatusUseCase().execute(
            GetCardSeenStatusCommand(card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    return result.data


# ---- Bug card: link test tasks ----


@router.post("/{card_id}/test-tasks", status_code=status.HTTP_201_CREATED)
async def link_test_task_to_bug(
    card_id: str,
    body: dict,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Link a test task to a bug card. Validates same spec and new scenario."""
    test_task_id = body.get("test_task_id")
    try:
        result = await LinkTestTaskToBugUseCase().execute(
            LinkTestTaskToBugCommand(card_id, test_task_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except CommandValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as e:
        detail = "Card not found" if e.entity_type == "card" else "Test task not found"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )

    return {
        "success": True,
        "bug_card_id": card_id,
        "test_task_id": test_task_id,
        "is_unblocked": result.is_unblocked,
    }


@router.delete(
    "/{card_id}/test-tasks/{test_task_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def unlink_test_task_from_bug(
    card_id: str,
    test_task_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Unlink a test task from a bug card."""
    try:
        await UnlinkTestTaskFromBugUseCase().execute(
            UnlinkTestTaskFromBugCommand(card_id, test_task_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        detail = (
            "Card not found" if exc.entity_type == "card" else "Test task not found"
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


@router.delete("/{card_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_card(
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a card."""
    try:
        await DeleteCardUseCase().execute(
            DeleteCardCommand(card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except CardOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc


# ---- Task Validation Endpoints ----


@router.post("/{card_id}/validate", status_code=status.HTTP_201_CREATED)
async def submit_task_validation(
    card_id: str,
    data: dict,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Submit a task validation for a card in 'validation' status."""
    try:
        result = await SubmitTaskValidationUseCase().execute(
            SubmitTaskValidationCommand(card_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except CommandValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except GateContractError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except ResourceGateError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_resource_gate_detail(e),
        )
    except PolicyTransitionRejected as e:
        raise RESTAdapterContract.http_error(e) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    return result.validation


@router.get("/{card_id}/validations")
async def list_task_validations(
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all validations for a card (reverse chronological)."""
    try:
        result = await ListTaskValidationsUseCase().execute(
            ListTaskValidationsCommand(card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return {
        "card_id": card_id,
        "total": len(result.validations),
        "validations": result.validations,
    }


@router.get("/{card_id}/validations/{validation_id}")
async def get_task_validation(
    card_id: str,
    validation_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a single validation by ID."""
    try:
        result = await GetTaskValidationUseCase().execute(
            GetTaskValidationCommand(card_id, validation_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Validation not found"
        )
    return result.validation


@router.delete(
    "/{card_id}/validations/{validation_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_task_validation(
    card_id: str,
    validation_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a validation entry."""
    try:
        await DeleteTaskValidationUseCase().execute(
            DeleteTaskValidationCommand(card_id, validation_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Validation not found"
        )


async def _card_knowledge_error_response(
    uow: PulseUnitOfWork,
    error: (
        KnowledgePropagationContractError
        | KnowledgePropagationPortError
        | KnowledgePropagationServiceError
    ),
):
    if isinstance(error, KnowledgePropagationServiceError):
        await rollback_and_record_knowledge_error(uow, error)
    else:
        await uow.rollback()
    return knowledge_propagation_error_response(error)


def _card_not_found_response() -> JSONResponse:
    return knowledge_propagation_error_response(
        KnowledgePropagationServiceError(
            "card_not_found",
            "Card not found",
        )
    )


@router.put(
    "/{card_id}/knowledge-assignments",
    response_model=KnowledgeMutationResponse,
)
async def replace_card_knowledge_assignments(
    card_id: str,
    data: KnowledgeAssignmentReplaceRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Atomically replace the card's governed v2 Knowledge selection."""
    try:
        result = await ReplaceCardKnowledgeAssignmentsUseCase().execute(
            ReplaceCardKnowledgeAssignmentsCommand(card_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
        return project_knowledge_mutation_response(result)
    except EntityNotFoundError:
        return _card_not_found_response()
    except (
        KnowledgePropagationContractError,
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _card_knowledge_error_response(uow, exc)


@router.post(
    "/{card_id}/knowledge-assignments/drop",
    response_model=KnowledgeMutationResponse,
)
async def drop_card_knowledge_assignments(
    card_id: str,
    data: KnowledgeAssignmentDropRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Drop selected roots, or all effective roots for an explicit empty list."""
    try:
        result = await DropCardKnowledgeAssignmentsUseCase().execute(
            DropCardKnowledgeAssignmentsCommand(card_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
        return project_knowledge_mutation_response(result)
    except EntityNotFoundError:
        return _card_not_found_response()
    except (
        KnowledgePropagationContractError,
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _card_knowledge_error_response(uow, exc)


@router.post(
    "/{card_id}/knowledge-assignments/refresh",
    response_model=KnowledgeRefreshResponse,
)
async def refresh_card_knowledge_assignments(
    card_id: str,
    data: KnowledgeAssignmentRefreshRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Refresh snapshot assignments by stable root Knowledge ID."""
    try:
        result = await RefreshCardKnowledgeAssignmentsUseCase().execute(
            RefreshCardKnowledgeAssignmentsCommand(card_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
        return project_refresh_response(result)
    except EntityNotFoundError:
        return _card_not_found_response()
    except (
        KnowledgePropagationContractError,
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _card_knowledge_error_response(uow, exc)


@router.get(
    "/{card_id}/knowledge-assignments",
    response_model=KnowledgeTechnicalReadResponse,
)
async def get_card_knowledge_assignments(
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return the technical dual-read projection for the card."""
    try:
        result = await GetCardKnowledgePropagationUseCase().execute(
            GetCardKnowledgePropagationCommand(card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
        return project_technical_read_response(result.read_result)
    except EntityNotFoundError:
        return _card_not_found_response()
    except (
        KnowledgePropagationContractError,
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _card_knowledge_error_response(uow, exc)


# ==================== CARD KNOWLEDGE BASE ====================
# Inline JSONB on Card.knowledge_bases. Symmetric to spec_knowledge but
# scoped to a single task; persisted as a snapshot at the card level.


class CardKnowledgeCreate(BaseModel):
    title: str
    content: str
    description: Optional[str] = None
    mime_type: str = "text/markdown"
    source: str = "manual"


class CardKnowledgeUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    description: Optional[str] = None
    mime_type: Optional[str] = None


@router.get("/{card_id}/knowledge")
async def list_card_knowledge(
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all KE attached to a card."""
    try:
        result = await ListCardKnowledgeUseCase().execute(
            ListCardKnowledgeCommand(card_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Card not found"
        )
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _card_knowledge_error_response(uow, exc)
    return {"card_id": card_id, "knowledge": result.knowledge}


@router.post("/{card_id}/knowledge", status_code=status.HTTP_201_CREATED)
async def create_card_knowledge(
    card_id: str,
    data: CardKnowledgeCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Blocked: card Knowledge Base resources are read-only governed snapshots."""
    await _require_card_write_access(card_id, user_id, uow)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=CARD_RESOURCE_READ_ONLY_MESSAGE,
    )


@router.get("/{card_id}/knowledge/{kb_id}")
async def get_card_knowledge(
    card_id: str,
    kb_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a single KE."""
    try:
        result = await GetCardKnowledgeUseCase().execute(
            GetCardKnowledgeCommand(card_id, kb_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as e:
        detail = (
            "Card not found" if e.entity_type == "card" else "Knowledge entry not found"
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    except (
        KnowledgePropagationPortError,
        KnowledgePropagationServiceError,
    ) as exc:
        return await _card_knowledge_error_response(uow, exc)
    return result.knowledge


@router.patch("/{card_id}/knowledge/{kb_id}")
async def update_card_knowledge(
    card_id: str,
    kb_id: str,
    data: CardKnowledgeUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Blocked: card Knowledge Base resources are read-only governed snapshots."""
    await _require_card_write_access(card_id, user_id, uow, kb_id=kb_id)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=CARD_RESOURCE_READ_ONLY_MESSAGE,
    )


@router.delete("/{card_id}/knowledge/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_card_knowledge(
    card_id: str,
    kb_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Blocked: card Knowledge Base resources are read-only governed snapshots."""
    await _require_card_write_access(card_id, user_id, uow, kb_id=kb_id)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=CARD_RESOURCE_READ_ONLY_MESSAGE,
    )


@router.get("/{card_id}/knowledge/{kb_id}/download")
async def download_card_knowledge(
    card_id: str,
    kb_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Download a KE as a plain markdown file with Content-Disposition attachment."""
    try:
        result = await GetCardKnowledgeUseCase().execute(
            GetCardKnowledgeCommand(card_id, kb_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as e:
        detail = (
            "Card not found" if e.entity_type == "card" else "Knowledge entry not found"
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    target = result.knowledge

    safe_title = "".join(
        c if c.isalnum() or c in "-_." else "_"
        for c in (target.get("title") or "knowledge")
    )
    filename = f"{safe_title or 'knowledge'}.md"
    body = (
        f"# {target.get('title', '')}\n\n"
        f"> {target.get('description') or ''}\n\n"
        f"{target.get('content', '')}\n"
    )
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": attachment_content_disposition(
                filename,
                fallback_filename="knowledge.md",
            )
        },
    )
