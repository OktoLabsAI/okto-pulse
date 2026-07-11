"""Resource Gate API endpoints."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.operational_rest import (
    BoardNotFoundError,
    ClearResourceNotApplicableCommand,
    ClearResourceNotApplicableUseCase,
    GetEffectiveResourcesUseCase,
    GetResourceGateSummaryUseCase,
    GetSpecResourceTaskCoverageUseCase,
    MarkResourceNotApplicableCommand,
    MarkResourceNotApplicableUseCase,
    ResourceGateEntityCommand,
    ResourceGateTaskCoverageCommand,
    UpdateResourceGateBoardSettingsCommand,
    UpdateResourceGateBoardSettingsUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.resource_gate import (
    ResourceGateError,
    ResourceGateJustificationRequired,
    ResourceGateNotFound,
)

router = APIRouter()

EntityType = Literal["ideation", "refinement", "spec", "card"]
ResourceType = Literal["architecture", "mockup", "knowledge_base"]
SourceChannel = Literal["ui", "api", "mcp"]


class ResourceNotApplicableRequest(BaseModel):
    resource_type: ResourceType
    source_channel: SourceChannel = "api"
    justification: str | None = None


class ClearResourceNotApplicableRequest(BaseModel):
    source_channel: SourceChannel = "api"
    reason: str | None = None


class ResourceGateBoardSettingsUpdate(BaseModel):
    require_spec_resource_task_coverage: bool


class ResourceGateBoardSettingsResponse(BaseModel):
    board_id: str
    settings: dict[str, Any]


def _resource_gate_exception(exc: ResourceGateError) -> HTTPException:
    status_code = status.HTTP_400_BAD_REQUEST
    if isinstance(exc, ResourceGateNotFound):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, ResourceGateJustificationRequired):
        status_code = status.HTTP_400_BAD_REQUEST
    return HTTPException(
        status_code=status_code,
        detail={
            "code": exc.code,
            "message": str(exc),
            "details": exc.details,
        },
    )


def _board_not_found(exc: BoardNotFoundError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Board not found",
    )


@router.get("/resource-gate/specs/{spec_id}/task-coverage")
async def get_spec_resource_task_coverage(
    spec_id: str,
    board_id: str = Query(...),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Validate Resource Gate Level 2 coverage for a spec."""
    try:
        result = await GetSpecResourceTaskCoverageUseCase().execute(
            ResourceGateTaskCoverageCommand(board_id, spec_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except ResourceGateError as exc:
        raise _resource_gate_exception(exc) from exc


@router.get("/resource-gate/{entity_type}/{entity_id}")
async def get_resource_gate_summary(
    entity_type: EntityType,
    entity_id: str,
    board_id: str = Query(...),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return Provided/N/A/Missing Resource Gate summary for an entity."""
    try:
        result = await GetResourceGateSummaryUseCase().execute(
            ResourceGateEntityCommand(board_id, entity_type, entity_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except ResourceGateError as exc:
        raise _resource_gate_exception(exc) from exc


@router.get("/resource-gate/{entity_type}/{entity_id}/effective-resources")
async def get_effective_resources(
    entity_type: EntityType,
    entity_id: str,
    board_id: str = Query(...),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return hydrated effective Resource Gate resources for UI rendering."""
    try:
        result = await GetEffectiveResourcesUseCase().execute(
            ResourceGateEntityCommand(board_id, entity_type, entity_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except ResourceGateError as exc:
        raise _resource_gate_exception(exc) from exc


@router.post("/resource-gate/{entity_type}/{entity_id}/not-applicable")
async def mark_resource_not_applicable(
    entity_type: EntityType,
    entity_id: str,
    data: ResourceNotApplicableRequest,
    board_id: str = Query(...),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Mark one mandatory resource type as not applicable."""
    try:
        result = await MarkResourceNotApplicableUseCase().execute(
            MarkResourceNotApplicableCommand(
                board_id,
                entity_type,
                entity_id,
                data.resource_type,
                data.justification,
                data.source_channel,
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except ResourceGateError as exc:
        raise _resource_gate_exception(exc) from exc


@router.delete("/resource-gate/{entity_type}/{entity_id}/not-applicable/{resource_type}")
async def clear_resource_not_applicable(
    entity_type: EntityType,
    entity_id: str,
    resource_type: ResourceType,
    data: ClearResourceNotApplicableRequest | None = Body(default=None),
    board_id: str = Query(...),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Clear the active N/A mark for a resource type."""
    payload = data or ClearResourceNotApplicableRequest()
    try:
        result = await ClearResourceNotApplicableUseCase().execute(
            ClearResourceNotApplicableCommand(
                board_id,
                entity_type,
                entity_id,
                resource_type,
                payload.reason,
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except ResourceGateError as exc:
        raise _resource_gate_exception(exc) from exc


@router.patch(
    "/boards/{board_id}/settings/resource-gate",
    response_model=ResourceGateBoardSettingsResponse,
)
async def update_resource_gate_board_settings(
    board_id: str,
    data: ResourceGateBoardSettingsUpdate,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update board-level Resource Gate settings."""
    try:
        result = await UpdateResourceGateBoardSettingsUseCase().execute(
            UpdateResourceGateBoardSettingsCommand(
                board_id,
                data.require_spec_resource_task_coverage,
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
