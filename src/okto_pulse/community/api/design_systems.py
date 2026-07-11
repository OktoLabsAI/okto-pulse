"""Design System catalog + board-link REST endpoints (spec 3a006f65 / card 1392f59d).

Catalog CRUD (global + board-inline, versioned) and the singular board<->design-system
effective link, sharing ``DesignSystemService`` with the MCP twins so REST and MCP
never diverge. Request models forbid extra fields and named bypass intents are
rejected with a structured error. Default application + gate mode (card #2) and the
mockup gate (card #3) are NOT here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.admin_catalog import (
    CreateDesignSystemUseCase,
    DeleteDesignSystemUseCase,
    DesignSystemCommand,
    GetBoardDesignSystemUseCase,
    GetDesignSystemUseCase,
    LinkBoardDesignSystemUseCase,
    ListDesignSystemsUseCase,
    UnlinkBoardDesignSystemUseCase,
    UpdateDesignSystemUseCase,
)
from okto_pulse.core.application.use_cases.import_export import (
    KIND_DESIGN_SYSTEMS,
    EnvelopeError,
    ExportDesignSystemsCommand,
    ExportDesignSystemsUseCase,
    ImportDesignSystemsCommand,
    ImportDesignSystemsUseCase,
    ImportItemError,
    parse_import_envelope,
    validate_items,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.amendment_revision_api import (
    AmendmentRevisionApiError,
    reject_bypass_fields,
)
from okto_pulse.core.services.design_system import (
    DesignSystemError,
)

router = APIRouter()


class CreateDesignSystemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    scope: str = "global"
    board_id: str | None = None
    payload: dict[str, Any] | None = None
    status: str = "active"


class UpdateDesignSystemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    payload: dict[str, Any] | None = None
    status: str | None = None


class LinkDesignSystemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    design_system_id: str


def _err(exc: DesignSystemError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.to_dict())


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


@router.post("/design-systems")
async def create_design_system(
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        reject_bypass_fields(raw)
        req = CreateDesignSystemRequest.model_validate(raw)
    except AmendmentRevisionApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await CreateDesignSystemUseCase().execute(
            DesignSystemCommand(payload=req.model_dump()),
            actor=RESTAdapterContract.actor(actor, board_id=req.board_id),
            uow=db,
        )
        return result.data
    except DesignSystemError as exc:
        raise _err(exc)


@router.get("/design-systems")
async def list_design_systems(
    scope: str = "global",
    board_id: str | None = None,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> list[dict[str, Any]]:
    result = await ListDesignSystemsUseCase().execute(
        DesignSystemCommand(scope=scope, board_id=board_id or ""),
        actor=RESTAdapterContract.actor(actor, board_id=board_id),
        uow=db,
    )
    return result.data


# NOTE: the literal /design-systems/export and /design-systems/import routes
# MUST be declared BEFORE the parametric /design-systems/{design_system_id}
# below — FastAPI matches in registration order, so the param route would
# otherwise swallow "export"/"import" as a design_system_id.


@router.get("/design-systems/export")
async def export_design_systems(
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    """Export the whole GLOBAL Design System catalog as a schema_version-1
    envelope (kind=design_systems)."""
    result = await ExportDesignSystemsUseCase().execute(
        ExportDesignSystemsCommand(),
        actor=RESTAdapterContract.actor(actor),
        uow=db,
    )
    return result


@router.post("/design-systems/import")
async def import_design_systems(
    envelope: dict[str, Any] = Body(default_factory=dict),
    dry_run: bool = False,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    """Import a kind=design_systems envelope. Items are validated against the
    same ``CreateDesignSystemRequest`` schema (+ bypass-field rejection) as
    POST /design-systems; matching titles in the same catalog partition are
    skipped as duplicates; any invalid item → 400 and NOTHING is mutated
    (all-or-nothing). ``dry_run=true`` validates and reports without persisting."""
    try:
        raw_items = parse_import_envelope(envelope, kind=KIND_DESIGN_SYSTEMS)
    except EnvelopeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_envelope", "message": str(exc)},
        )

    def _validate(raw: dict[str, Any]) -> dict[str, Any]:
        try:
            reject_bypass_fields(raw)
        except AmendmentRevisionApiError as exc:
            raise ImportItemError(-1, exc.to_dict())
        return CreateDesignSystemRequest.model_validate(raw).model_dump()

    parsed, errors = validate_items(raw_items, _validate)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"created": 0, "skipped": [], "errors": errors},
        )
    try:
        result = await ImportDesignSystemsUseCase().execute(
            ImportDesignSystemsCommand(items=parsed, dry_run=dry_run),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
    except ImportItemError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "created": 0,
                "skipped": [],
                "errors": [{"index": exc.index, "detail": exc.detail}],
            },
        )
    return result.payload(dry_run=dry_run)


@router.get("/design-systems/{design_system_id}/export")
async def export_design_system(
    design_system_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    """Export a single Design System (any scope) as a one-item envelope."""
    try:
        result = await ExportDesignSystemsUseCase().execute(
            ExportDesignSystemsCommand(design_system_id=design_system_id),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
        return result
    except DesignSystemError as exc:
        raise _err(exc)


@router.get("/design-systems/{design_system_id}")
async def get_design_system(
    design_system_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        result = await GetDesignSystemUseCase().execute(
            DesignSystemCommand(design_system_id=design_system_id),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
        return result.data
    except DesignSystemError as exc:
        raise _err(exc)


@router.patch("/design-systems/{design_system_id}")
async def update_design_system(
    design_system_id: str,
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        reject_bypass_fields(raw)
        req = UpdateDesignSystemRequest.model_validate(raw)
    except AmendmentRevisionApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await UpdateDesignSystemUseCase().execute(
            DesignSystemCommand(
                design_system_id=design_system_id,
                payload=req.model_dump(exclude_unset=True),
            ),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
        return result.data
    except DesignSystemError as exc:
        raise _err(exc)


@router.delete("/design-systems/{design_system_id}", status_code=204)
async def delete_design_system(
    design_system_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> None:
    result = await DeleteDesignSystemUseCase().execute(
        DesignSystemCommand(design_system_id=design_system_id),
        actor=RESTAdapterContract.actor(actor),
        uow=db,
    )
    if not result.data:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "design_system_not_found",
                "code": "design_system_not_found",
                "message": "Design System not found.",
            },
        )


@router.post("/boards/{board_id}/design-system")
async def link_board_design_system(
    board_id: str,
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        reject_bypass_fields(raw)
        req = LinkDesignSystemRequest.model_validate(raw)
    except AmendmentRevisionApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await LinkBoardDesignSystemUseCase().execute(
            DesignSystemCommand(board_id=board_id, payload=req.model_dump()),
            actor=RESTAdapterContract.actor(actor, board_id=board_id),
            uow=db,
        )
        link = result.data
        return {
            "board_id": link.board_id,
            "design_system_id": link.design_system_id,
            "design_system_version": link.design_system_version,
        }
    except DesignSystemError as exc:
        raise _err(exc)


@router.delete("/boards/{board_id}/design-system", status_code=204)
async def unlink_board_design_system(
    board_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> None:
    result = await UnlinkBoardDesignSystemUseCase().execute(
        DesignSystemCommand(board_id=board_id),
        actor=RESTAdapterContract.actor(actor, board_id=board_id),
        uow=db,
    )
    if not result.data:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "design_system_link_not_found",
                "code": "design_system_link_not_found",
                "message": "No Design System is linked to this board.",
            },
        )


@router.get("/boards/{board_id}/design-system")
async def get_board_design_system(
    board_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    result = await GetBoardDesignSystemUseCase().execute(
        DesignSystemCommand(board_id=board_id),
        actor=RESTAdapterContract.actor(actor, board_id=board_id),
        uow=db,
    )
    return result.data
