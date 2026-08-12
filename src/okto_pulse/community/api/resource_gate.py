"""Resource Gate API endpoints."""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel

from okto_pulse.community.api.auth_deps import get_realm_id, require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.knowledge_workspace import (
    KnowledgeWorkspaceProjectionError,
)
from okto_pulse.core.domain.human_validation_cycle import (
    SubjectEditRequiresDraftError,
)
from okto_pulse.core.application.use_cases.operational_rest import (
    BoardNotFoundError,
    ClearResourceNotApplicableCommand,
    ClearResourceNotApplicableUseCase,
    GetEffectiveResourcesCommand,
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
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.resource_gate import (
    ResourceGateError,
    ResourceGateJustificationRequired,
    ResourceGateNotFound,
)

router = APIRouter()
_WORKSPACE_LOGGER = logging.getLogger(
    "okto_pulse.community.knowledge_workspace"
)

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


def _workspace_projection_exception(
    exc: KnowledgeWorkspaceProjectionError,
) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=exc.to_dict(),
    )


@router.get("/resource-gate/specs/{spec_id}/task-coverage")
async def get_spec_resource_task_coverage(
    spec_id: str,
    board_id: str = Query(...),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Validate Resource Gate Level 2 coverage for a spec."""
    try:
        result = await GetSpecResourceTaskCoverageUseCase().execute(
            ResourceGateTaskCoverageCommand(board_id, spec_id),
            actor=RESTAdapterContract.actor(
                user_id,
                realm_id=realm_id,
                board_id=board_id,
            ),
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
    realm_id: str | None = Depends(get_realm_id),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return Provided/N/A/Missing Resource Gate summary for an entity."""
    try:
        result = await GetResourceGateSummaryUseCase().execute(
            ResourceGateEntityCommand(board_id, entity_type, entity_id),
            actor=RESTAdapterContract.actor(
                user_id,
                realm_id=realm_id,
                board_id=board_id,
            ),
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
    profile: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int | None = Query(None),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return a bounded Knowledge Workspace page.

    Omitting ``profile`` preserves the historical hydrated ``resources`` map
    during rolling upgrades.  Bounded workspace profiles are opt-in and never
    mix that heavy payload into their response, so ``summary`` cannot
    accidentally disclose bodies or exceed its byte budget.
    """
    started = time.perf_counter()
    requested_profile = "legacy" if profile is None else profile
    try:
        result = await GetEffectiveResourcesUseCase().execute(
            GetEffectiveResourcesCommand(
                board_id,
                entity_type,
                entity_id,
                requested_profile,
                cursor,
                limit,
            ),
            actor=RESTAdapterContract.actor(
                user_id,
                realm_id=realm_id,
                board_id=board_id,
            ),
            uow=db,
        )
        data = result.data
        _WORKSPACE_LOGGER.info(
            (
                "knowledge_workspace.read profile=%s count=%d "
                "unique_effective_count=%d raw_attachment_count=%d "
                "workspace_item_count=%d response_bytes=%d truncated=%s "
                "duration_ms=%.3f"
            ),
            str(data.get("profile") or requested_profile),
            int(data.get("count") or 0),
            int(data.get("unique_effective_count") or 0),
            int(data.get("raw_attachment_count") or 0),
            int(data.get("workspace_item_count") or 0),
            int(data.get("response_bytes") or 0),
            bool(data.get("truncated")),
            (time.perf_counter() - started) * 1000,
        )
        return data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except KnowledgeWorkspaceProjectionError as exc:
        raise _workspace_projection_exception(exc) from exc
    except ResourceGateError as exc:
        raise _resource_gate_exception(exc) from exc


@router.post("/resource-gate/{entity_type}/{entity_id}/not-applicable")
async def mark_resource_not_applicable(
    entity_type: EntityType,
    entity_id: str,
    data: ResourceNotApplicableRequest,
    board_id: str = Query(...),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
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
            actor=RESTAdapterContract.actor(
                user_id,
                realm_id=realm_id,
                board_id=board_id,
            ),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except SubjectEditRequiresDraftError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
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
    realm_id: str | None = Depends(get_realm_id),
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
            actor=RESTAdapterContract.actor(
                user_id,
                realm_id=realm_id,
                board_id=board_id,
            ),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
    except SubjectEditRequiresDraftError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
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
    realm_id: str | None = Depends(get_realm_id),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update board-level Resource Gate settings."""
    try:
        result = await UpdateResourceGateBoardSettingsUseCase().execute(
            UpdateResourceGateBoardSettingsCommand(
                board_id,
                data.require_spec_resource_task_coverage,
            ),
            actor=RESTAdapterContract.actor(
                user_id,
                realm_id=realm_id,
                board_id=board_id,
            ),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise _board_not_found(exc) from exc
