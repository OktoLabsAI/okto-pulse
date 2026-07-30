"""REST endpoints for administrative DefaultBoardConfiguration (spec 9df814bc /
card 7da43521, FR7).

active template, version history, create/activate/deactivate, and the board
default-config diff. The MCP twin tools share the same orchestrator
(``DefaultBoardConfigApiService``) so REST and MCP never diverge. Request models
forbid extra fields and named bypass intents are rejected with a structured error.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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

NonEmptyString = Annotated[str, Field(strict=True, min_length=1)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
RevisionDigest = Annotated[
    str,
    Field(strict=True, pattern=r"^[0-9a-f]{64}$"),
]


class GuidelineDefaultRefRequest(BaseModel):
    """Native B11 write contract for one immutable default revision pin.

    All four revision fields are required so a native client cannot silently
    promote a historical default to the current head.  Core verifies the pin
    against the selected immutable revision.  Identity-only refs and legacy
    aliases intentionally stay out of this native contract.
    """

    model_config = ConfigDict(extra="forbid")

    guideline_id: NonEmptyString
    priority: NonNegativeInt = 0
    revision_id: NonEmptyString
    revision_number: PositiveInt
    semantic_version: NonEmptyString
    revision_digest: RevisionDigest


class _CompatibleGuidelineDefaultRefRequest(GuidelineDefaultRefRequest):
    """Compatibility-only aliases accepted by board-config import.

    They are never exposed by native create/update endpoints.  This lets a
    historical export retain honest legacy provenance while the live write
    surface remains closed to the six canonical B11 fields.
    """

    revision_id: NonEmptyString | None = None
    revision_number: PositiveInt | None = None
    semantic_version: NonEmptyString | None = None
    revision_digest: RevisionDigest | None = None
    guideline_version: PositiveInt | None = None
    legacy_version: PositiveInt | None = None
    legacy_version_unresolvable: bool | None = None


class GuidelineRevisionPinResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: NonEmptyString
    revision_number: PositiveInt
    semantic_version: NonEmptyString
    revision_digest: RevisionDigest


class DefaultGuidelineCandidateResponse(BaseModel):
    """Unambiguous head-vs-default candidate projection.

    The scalar revision fields are retained additively for old readers and
    always mirror ``head_revision``.  New clients should use the nested objects.
    """

    model_config = ConfigDict(extra="forbid")

    guideline_id: NonEmptyString
    title: str
    scope: str
    guideline_version: PositiveInt
    revision_id: NonEmptyString
    revision_number: PositiveInt
    semantic_version: NonEmptyString
    revision_digest: RevisionDigest
    head_revision: GuidelineRevisionPinResponse
    default_revision: GuidelineRevisionPinResponse | None
    retired: bool
    eligible: bool
    eligibility_reason: Literal["guideline_retired"] | None
    is_default: bool
    priority: NonNegativeInt | None


class DefaultGuidelineCandidatesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    template_id: str | None
    template_version: PositiveInt | None
    candidates: list[DefaultGuidelineCandidateResponse]


class DefaultBoardConfigVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings_payload: dict[str, Any] | None = None
    scope: str = "global"
    guideline_default_refs: list[GuidelineDefaultRefRequest] | None = None
    design_system_default_ref: dict[str, Any] | None = None
    spec_checklist_mode: Literal["off", "advisory", "blocking"] | None = None
    activate: bool = False


class _DefaultBoardConfigImportVersionRequest(
    DefaultBoardConfigVersionCreateRequest
):
    """Import twin that alone accepts the documented legacy pin aliases."""

    guideline_default_refs: list[_CompatibleGuidelineDefaultRefRequest] | None = None


class UpdateDefaultGuidelineRefsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guideline_default_refs: list[GuidelineDefaultRefRequest] | None = None


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


def _reject_duplicate_guideline_refs(
    refs: list[GuidelineDefaultRefRequest] | None,
) -> None:
    """Reject a duplicate identity before any use case can open a write path."""

    seen: set[str] = set()
    for ref in refs or []:
        if ref.guideline_id in seen:
            raise DefaultBoardConfigurationError(
                "default_guideline_duplicate",
                "A guideline can appear only once in a default configuration.",
                422,
                {"guideline_id": ref.guideline_id},
            )
        seen.add(ref.guideline_id)


def _reject_inline_guideline_refs(raw: dict[str, Any]) -> None:
    """Preserve the stable semantic reason before closed-schema validation."""

    refs = raw.get("guideline_default_refs")
    if not isinstance(refs, list):
        return
    for position, ref in enumerate(refs):
        if isinstance(ref, dict) and not ref.get("guideline_id"):
            raise DefaultBoardConfigurationError(
                "default_guideline_inline_not_allowed",
                "Inline guideline defaults are not allowed; reference a global "
                "catalog guideline by guideline_id.",
                422,
                {"position": position, "ref": ref},
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
        _reject_inline_guideline_refs(raw)
        req = DefaultBoardConfigVersionCreateRequest.model_validate(raw)
        _reject_duplicate_guideline_refs(req.guideline_default_refs)
    except AmendmentRevisionApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)
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
        return _DefaultBoardConfigImportVersionRequest.model_validate(
            payload
        ).model_dump(exclude_unset=True)

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


@router.get(
    "/guidelines/default-candidates",
    response_model=DefaultGuidelineCandidatesResponse,
)
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
        _reject_inline_guideline_refs(raw)
        req = UpdateDefaultGuidelineRefsRequest.model_validate(raw)
        _reject_duplicate_guideline_refs(req.guideline_default_refs)
    except AmendmentRevisionApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except DefaultBoardConfigurationError as exc:
        raise _err(exc)
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await UpdateDefaultGuidelineRefsUseCase().execute(
            DefaultBoardConfigCommand(
                template_id=template_id,
                payload=req.model_dump(),
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
