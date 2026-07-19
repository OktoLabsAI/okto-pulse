"""Endpoints for the currently authenticated user/agent.

Exposes effective permissions for frontend gating (`GET /me/permissions`).
Backend still enforces authorization — this endpoint only lets the frontend
reflect the same intent in UI (hide/disable buttons that would be rejected
anyway). Missing and inaccessible boards share the same 404 envelope.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from okto_pulse.community.api.auth_deps import get_realm_id, require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.permission_presets import (
    GetMyPermissionsCommand,
    GetMyPermissionsUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


class PermissionsResponse(BaseModel):
    board_id: str
    preset_name: str | None
    flags: dict

    model_config = {"from_attributes": True}


@router.get("/me/permissions", response_model=PermissionsResponse)
async def get_my_permissions(
    board_id: str = Query(..., description="Board to resolve permissions against"),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> PermissionsResponse:
    """Return the authenticated user's effective permission flags for a board.

    Flags are resolved via ``resolve_permissions(agent_flags, preset_flags,
    board_overrides)`` — the exact same path used by runtime permission
    checks. Board overrides that restrict flags (ceiling model) appear as
    ``False``.

    Legacy agents (``permissions`` column non-null, flat list) are mapped
    through ``map_legacy_permissions`` first. Agents with no granular data
    at all default to the full registry (True for everything), matching the
    historical "full access" compat path.
    """
    try:
        result = await GetMyPermissionsUseCase().execute(
            GetMyPermissionsCommand(board_id=board_id),
            actor=RESTAdapterContract.actor(
                user_id,
                realm_id=realm_id,
                board_id=board_id,
            ),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found",
        ) from exc
    permissions = result.permissions
    return PermissionsResponse(
        board_id=permissions.board_id,
        preset_name=permissions.preset_name,
        flags=permissions.flags,
    )
