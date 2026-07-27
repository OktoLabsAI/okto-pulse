"""REST endpoints for administrative DefaultBoardConfiguration (spec 9df814bc /
card 7da43521, FR7).

active template, version history, create/activate/deactivate, and the board
default-config diff. The MCP twin tools share the same orchestrator
(``DefaultBoardConfigApiService``) so REST and MCP never diverge. Request models
forbid extra fields and named bypass intents are rejected with a structured error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.admin_catalog import (
    ActivateDefaultBoardConfigVersionUseCase,
    CreateDefaultBoardConfigVersionUseCase,
    DeactivateDefaultBoardConfigVersionUseCase,
    DefaultBoardConfigCommand,
    GetActiveDefaultBoardConfigUseCase,
    GetBoardDefaultConfigDiffUseCase,
    ListDefaultBoardConfigVersionsUseCase,
    ListDefaultGuidelineCandidatesUseCase,
    SetDefaultDesignSystemUseCase,
    UpdateDefaultGuidelineRefsUseCase,
)
from okto_pulse.core.application.use_cases.import_export import (
    KIND_BOARD_CONFIG,
    EnvelopeError,
    ExportBoardConfigCommand,
    ExportBoardConfigUseCase,
    ImportBoardConfigCommand,
    ImportBoardConfigUseCase,
    ImportItemError,
    parse_import_envelope,
    validate_items,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_principal, require_user
from okto_pulse.core.application.use_cases import PermissionDeniedError
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.amendment_revision_api import AmendmentRevisionApiError
from okto_pulse.core.services.default_board_config_api import (
    reject_bypass_fields,
)
from okto_pulse.core.services.default_board_configuration import (
    DefaultBoardConfigurationError,
)

router = APIRouter()

# query_scope is derived inside the admin/catalog use cases from the REST actor.


class DefaultBoardConfigVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings_payload: dict[str, Any] | None = None
    scope: str = "global"
    guideline_default_refs: list[Any] | None = None
    design_system_default_ref: dict[str, Any] | None = None
    activate: bool = False


class UpdateDefaultGuidelineRefsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guideline_default_refs: list[Any] | None = None


class SetTemplateDesignSystemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    design_system_id: str
    version: int | None = None
    snapshot: dict[str, Any] | None = None
    gate_mode: str = "off"


def _err(exc: DefaultBoardConfigurationError) -> HTTPException:
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


@router.get("/default-board-config/active")
async def get_active_default_board_config(
    scope: str = "global",
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        result = await GetActiveDefaultBoardConfigUseCase().execute(
            DefaultBoardConfigCommand(scope=scope),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
        return result.data
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)


@router.get("/default-board-config/versions")
async def list_default_board_config_versions(
    scope: str = "global",
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        result = await ListDefaultBoardConfigVersionsUseCase().execute(
            DefaultBoardConfigCommand(scope=scope),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
        return result.data
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)


@router.post("/default-board-config/versions")
async def create_default_board_config_version(
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    try:
        reject_bypass_fields(raw)
        req = DefaultBoardConfigVersionCreateRequest.model_validate(raw)
    except AmendmentRevisionApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await CreateDefaultBoardConfigVersionUseCase().execute(
            DefaultBoardConfigCommand(payload=req.model_dump()),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=db,
        )
        return result.data
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)


@router.get("/default-board-config/export")
async def export_default_board_config(
    scope: str = "global",
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    """Export every default board configuration version (oldest first, the
    active one marked ``is_active``) as a schema_version-1 envelope
    (kind=board_config)."""
    try:
        return await ExportBoardConfigUseCase().execute(
            ExportBoardConfigCommand(scope=scope),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)


@router.post("/default-board-config/import")
async def import_default_board_config(
    envelope: dict[str, Any] = Body(default_factory=dict),
    dry_run: bool = False,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Import a kind=board_config envelope. Every item becomes a NEW template
    version through the same validated creation path as
    POST /default-board-config/versions (``is_active`` maps to ``activate``,
    so the exported active version becomes the active template). Any invalid
    item → 400 and NOTHING is mutated (all-or-nothing). ``dry_run=true``
    validates and reports without persisting."""
    try:
        raw_items = parse_import_envelope(envelope, kind=KIND_BOARD_CONFIG)
    except EnvelopeError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_envelope", "message": str(exc)},
        )

    def _validate(raw: dict[str, Any]) -> dict[str, Any]:
        payload = dict(raw)
        # Export marks the active version with ``is_active``; the creation
        # request model (extra=forbid) expresses that intent as ``activate``.
        is_active = bool(payload.pop("is_active", False))
        payload.setdefault("activate", is_active)
        try:
            reject_bypass_fields(payload)
        except AmendmentRevisionApiError as exc:
            raise ImportItemError(-1, exc.to_dict())
        return DefaultBoardConfigVersionCreateRequest.model_validate(payload).model_dump()

    parsed, errors = validate_items(raw_items, _validate)
    if errors:
        raise HTTPException(
            status_code=400,
            detail={"created": 0, "skipped": [], "errors": errors},
        )
    try:
        result = await ImportBoardConfigUseCase().execute(
            ImportBoardConfigCommand(items=parsed, dry_run=dry_run),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=db,
        )
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)
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


@router.post("/default-board-config/versions/{template_id}/activate")
async def activate_default_board_config_version(
    template_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    try:
        result = await ActivateDefaultBoardConfigVersionUseCase().execute(
            DefaultBoardConfigCommand(template_id=template_id),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=db,
        )
        return result.data
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)


@router.post("/default-board-config/versions/{template_id}/deactivate")
async def deactivate_default_board_config_version(
    template_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    try:
        result = await DeactivateDefaultBoardConfigVersionUseCase().execute(
            DefaultBoardConfigCommand(template_id=template_id),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=db,
        )
        return result.data
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)


@router.get("/boards/{board_id}/default-config-diff")
async def get_board_default_config_diff(
    board_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    try:
        result = await GetBoardDefaultConfigDiffUseCase().execute(
            DefaultBoardConfigCommand(board_id=board_id),
            actor=RESTAdapterContract.actor(actor, board_id=board_id),
            uow=db,
        )
        return result.data
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)


# -- guideline defaults (spec 8a2fad91 / card 5cb88511) ----------------------


@router.get("/guidelines/default-candidates")
async def list_default_guideline_candidates(
    scope: str = "global",
    template_id: str | None = None,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    actor: str = Depends(require_user),
) -> dict[str, Any]:
    """Global catalog guidelines with derived eligibility + current default status
    from the umbrella template (api_019810c9)."""
    try:
        result = await ListDefaultGuidelineCandidatesUseCase().execute(
            DefaultBoardConfigCommand(scope=scope, template_id=template_id or ""),
            actor=RESTAdapterContract.actor(actor),
            uow=db,
        )
        return result.data
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)


@router.post("/default-board-configurations/{template_id}/guidelines")
async def update_default_guideline_refs(
    template_id: str,
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Update guideline_default_refs for a template using only global catalog
    guidelines (api_0845ff2a). Active template => copy-on-write new version."""
    try:
        reject_bypass_fields(raw)
        req = UpdateDefaultGuidelineRefsRequest.model_validate(raw)
    except AmendmentRevisionApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await UpdateDefaultGuidelineRefsUseCase().execute(
            DefaultBoardConfigCommand(
                template_id=template_id,
                payload={"guideline_default_refs": req.guideline_default_refs},
            ),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=db,
        )
        return result.data
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)


@router.post("/default-board-configurations/{template_id}/design-system")
async def set_default_design_system(
    template_id: str,
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Set the Design System default reference + canonical gate mode on a template
    (api_3ed0aee6). Active template => copy-on-write new version. The design_system_id
    must be a real global active DesignSystem (inline/synthetic rejected)."""
    try:
        reject_bypass_fields(raw)
        req = SetTemplateDesignSystemRequest.model_validate(raw)
    except AmendmentRevisionApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await SetDefaultDesignSystemUseCase().execute(
            DefaultBoardConfigCommand(template_id=template_id, payload=req.model_dump()),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=db,
        )
        return result.data
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)
