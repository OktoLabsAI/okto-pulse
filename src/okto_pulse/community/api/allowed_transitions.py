"""Allowed transition read model API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.allowed_transitions import (
    ALLOWED_TRANSITIONS_SOURCE,
    ListAllowedTransitionsCommand,
    ListAllowedTransitionsUseCase,
)
from okto_pulse.core.application.use_cases.base import (
    CommandValidationError,
    EntityNotFoundError,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import get_realm_id, require_user
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


class AllowedTransitionResponse(BaseModel):
    to_status: str
    label: str
    gate: str
    blocked_reason: str | None = None


class AllowedTransitionsResponse(BaseModel):
    board_id: str
    entity_type: str
    entity_id: str | None
    current_status: str
    allowed_transitions: list[AllowedTransitionResponse]
    source: str = ALLOWED_TRANSITIONS_SOURCE


@router.get(
    "/boards/{board_id}/allowed-transitions",
    response_model=AllowedTransitionsResponse,
)
async def get_allowed_transitions(
    board_id: str,
    entity_type: str = Query(...),
    entity_id: str | None = Query(None),
    current_status: str | None = Query(None),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return lifecycle actions allowed by the backend transition authority."""

    try:
        result = await ListAllowedTransitionsUseCase().execute(
            ListAllowedTransitionsCommand(
                board_id,
                entity_type,
                entity_id=entity_id,
                current_status=current_status,
            ),
            actor=RESTAdapterContract.actor(
                user_id,
                board_id=board_id,
                realm_id=realm_id,
            ),
            uow=uow,
        )
    except CommandValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except EntityNotFoundError as exc:
        detail = "Board not found" if exc.entity_type == "board" else f"{exc.entity_type.title()} not found"
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return result.read_model.to_dict()
