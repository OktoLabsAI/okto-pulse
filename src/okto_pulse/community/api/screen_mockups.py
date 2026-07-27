"""Screen mockup REST endpoints with the MockupDesignSystemGate (spec 3a006f65 /
card 0192f58d, contract api_a5e13bc5).

Create/update a ScreenMockup on mockup-bearing source entities
(spec/ideation/refinement/story), carrying the Design System consumption metadata.
Card mockups are read-only governed snapshots and are refreshed through the
spec-to-card copy path.
(design_system_ref/version/evidence). The MockupDesignSystemGate runs BEFORE
persistence: blocking rejects an invalid/missing ref with an actionable structured
error; advisory persists + returns a design_system_gate warning + a queryable audit
row; off / no effective Design System does not block. HTML is sanitized exactly as the
MCP path. This is the REST twin of okto_pulse_add/update_screen_mockup — it shares the
same MockupDesignSystemGate, so REST and MCP never diverge.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.admin_catalog import (
    CreateScreenMockupCommand,
    CreateScreenMockupUseCase,
    ScreenMockupUseCaseError,
    UpdateScreenMockupCommand,
    UpdateScreenMockupUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


class CreateScreenMockupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str | None = None
    screen_type: str = "page"
    html_content: str = ""
    design_system_ref: str | None = None
    design_system_version: int | None = None
    design_system_evidence: Any | None = None


class UpdateScreenMockupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    description: str | None = None
    screen_type: str | None = None
    html_content: str | None = None
    design_system_ref: str | None = None
    design_system_version: int | None = None
    design_system_evidence: Any | None = None


def _invalid_request(exc: ValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": "invalid_request",
            "code": "invalid_request",
            "message": "Request body has unsupported or invalid fields.",
            "details": exc.errors(include_url=False),
        },
    )


@router.post("/{entity_type}/{entity_id}/screen-mockups", status_code=201)
async def create_screen_mockup(
    entity_type: str,
    entity_id: str,
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        req = CreateScreenMockupRequest.model_validate(raw)
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await CreateScreenMockupUseCase().execute(
            CreateScreenMockupCommand(entity_type, entity_id, req),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
        return result.data
    except ScreenMockupUseCaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.patch("/{entity_type}/{entity_id}/screen-mockups/{screen_id}")
async def update_screen_mockup(
    entity_type: str,
    entity_id: str,
    screen_id: str,
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        req = UpdateScreenMockupRequest.model_validate(raw)
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await UpdateScreenMockupUseCase().execute(
            UpdateScreenMockupCommand(entity_type, entity_id, screen_id, req),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
        return result.data
    except ScreenMockupUseCaseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
