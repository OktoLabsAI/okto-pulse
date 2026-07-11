"""Architecture Design API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import ConflictError, EntityNotFoundError
from okto_pulse.core.application.use_cases.architecture_crud import (
    ArchitecturePropagationLegacyReportCommand,
    ArchitecturePropagationLegacyReportUseCase,
    CopyArchitectureFromSpecToCardCommand,
    CopyArchitectureFromSpecToCardUseCase,
    CreateArchitectureCommand,
    CreateArchitectureUseCase,
    DeleteArchitectureDesignCommand,
    DeleteArchitectureDesignUseCase,
    GetArchitectureDesignCommand,
    GetArchitectureDesignUseCase,
    GetArchitectureDiagramPayloadCommand,
    GetArchitectureDiagramPayloadUseCase,
    GetArchitectureDiffCommand,
    GetArchitectureDiffUseCase,
    ImportExcalidrawArchitectureDiagramCommand,
    ImportExcalidrawArchitectureDiagramUseCase,
    ListArchitectureCommand,
    ListArchitectureUseCase,
    UpdateArchitectureDesignCommand,
    UpdateArchitectureDesignUseCase,
    UpdateArchitectureDiagramPayloadCommand,
    UpdateArchitectureDiagramPayloadUseCase,
    ValidateArchitecturePayloadCommand,
    ValidateArchitecturePayloadUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.models.schemas import (
    ArchitectureDesignCreate,
    ArchitectureDesignResponse,
    ArchitectureDesignSummary,
    ArchitectureDesignUpdate,
    ArchitectureDiagramFormat,
    ArchitectureDiagramPayloadResponse,
    ArchitectureDiagramType,
    ArchitectureDiffResponse,
    ArchitectureWarningAcknowledgementRequest,
)
from okto_pulse.core.services.architecture import (
    ArchitecturePayloadValidationError,
    ArchitecturePropagationBlocked,
    CARD_ARCHITECTURE_READ_ONLY_MESSAGE,
    CardArchitectureReadOnlyError,
    ArchitectureWarningAcknowledgementRequired,
)
from okto_pulse.core.services.effective_resource_propagation import (
    ResourceLineageResolutionError,
    ResourcePropagationError,
)

router = APIRouter()


class DiagramPayloadUpdate(BaseModel):
    """Payload replacement for an existing diagram."""

    payload: dict[str, Any] | list[Any] | str
    format: ArchitectureDiagramFormat | None = None
    change_summary: str | None = None
    architecture_warning_acknowledgement: ArchitectureWarningAcknowledgementRequest | None = None


class ExcalidrawImportRequest(BaseModel):
    """Import an Excalidraw scene into an architecture design."""

    title: str = Field(..., min_length=1, max_length=255)
    payload: dict[str, Any]
    diagram_type: ArchitectureDiagramType = "other"
    description: str | None = None
    order_index: int = 0
    replace_diagram_id: str | None = None
    change_summary: str | None = None
    architecture_warning_acknowledgement: ArchitectureWarningAcknowledgementRequest | None = None


class CopyArchitectureRequest(BaseModel):
    """Optional filter for copy architecture operations."""

    design_ids: list[str] | None = None
    architecture_warning_acknowledgement: ArchitectureWarningAcknowledgementRequest | None = None


class ArchitecturePayloadValidationRequest(BaseModel):
    """Dry-run payload used to critique an Architecture Design without persisting it."""

    title: str | None = None
    global_description: str | None = None
    entities: list[dict[str, Any]] | None = None
    interfaces: list[dict[str, Any]] | None = None
    diagrams: list[dict[str, Any]] | None = None
    design_id: str | None = None


class ArchitecturePayloadValidationResponse(BaseModel):
    """Semantic validation and authoring feedback for an Architecture Design payload."""

    valid: bool
    issues: list[str]
    warnings: list[str]
    structured_warnings: list[dict[str, Any]] = Field(default_factory=list)
    suppressed_warnings: list[dict[str, Any]] = Field(default_factory=list)
    suggested_fixes: list[str]
    summary: dict[str, Any]


def _http_error_from_value(error: ValueError) -> HTTPException:
    if isinstance(error, ArchitectureWarningAcknowledgementRequired):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error.to_payload())
    if isinstance(error, ArchitecturePropagationBlocked):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error.to_error_dict(),
        )
    if isinstance(error, CardArchitectureReadOnlyError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    message = str(error)
    status_code = (
        status.HTTP_422_UNPROCESSABLE_CONTENT
        if isinstance(error, ArchitecturePayloadValidationError)
        else status.HTTP_404_NOT_FOUND
        if "not found" in message
        else status.HTTP_422_UNPROCESSABLE_CONTENT
    )
    return HTTPException(status_code=status_code, detail=message)


def _http_error_from_conflict(error: ConflictError) -> HTTPException:
    """Map a use-case ``ConflictError`` raised by the design-mutability gate back to
    the exact legacy 409 detail: a ``card`` parent is read-only
    (``CARD_ARCHITECTURE_READ_ONLY_MESSAGE``); a ``spec`` whose validation passed is
    locked (the same detail the create-spec lock produces)."""
    if error.entity_type == "card_architecture_readonly":
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=CARD_ARCHITECTURE_READ_ONLY_MESSAGE,
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Spec is locked because validation passed. Move it back to draft or approved to edit architecture.",
    )


async def _list_architecture(
    parent_type: str,
    parent_id: str,
    user_id: str,
    uow: PulseUnitOfWork,
) -> list[ArchitectureDesignSummary]:
    """R01A FU5-S1A: thin REST adapter over ``ListArchitectureUseCase``. The parent
    existence gate (legacy ``_ensure_parent``) maps to the same 404
    ``"{parent_type} not found"``; the ``select``/projection stay in the
    repository the use case delegates to."""
    try:
        result = await ListArchitectureUseCase().execute(
            ListArchitectureCommand(parent_type, parent_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type} not found",
        )
    return result.summaries


async def _create_architecture(
    parent_type: str,
    parent_id: str,
    data: ArchitectureDesignCreate,
    user_id: str,
    uow: PulseUnitOfWork,
) -> ArchitectureDesignResponse:
    """R01A FU5-S1A: thin REST adapter over ``CreateArchitectureUseCase``. Reproduces
    the legacy error mapping exactly — parent missing → 404 ``"{parent_type} not
    found"``; spec-architecture lock → 409; ``repo.create``'s ``ValueError`` family
    → ``_http_error_from_value``."""
    try:
        result = await CreateArchitectureUseCase().execute(
            CreateArchitectureCommand(parent_type, parent_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type} not found",
        )
    except ConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Spec is locked because validation passed. Move it back to draft or approved to edit architecture.",
        )
    except ValueError as error:
        raise _http_error_from_value(error)
    return result.response


@router.get("/ideations/{ideation_id}/architecture", response_model=list[ArchitectureDesignSummary])
async def list_ideation_architecture(
    ideation_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _list_architecture("ideation", ideation_id, user_id, uow)


@router.post(
    "/ideations/{ideation_id}/architecture",
    response_model=ArchitectureDesignResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_ideation_architecture(
    ideation_id: str,
    data: ArchitectureDesignCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _create_architecture("ideation", ideation_id, data, user_id, uow)


@router.get("/refinements/{refinement_id}/architecture", response_model=list[ArchitectureDesignSummary])
async def list_refinement_architecture(
    refinement_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _list_architecture("refinement", refinement_id, user_id, uow)


@router.post(
    "/refinements/{refinement_id}/architecture",
    response_model=ArchitectureDesignResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_refinement_architecture(
    refinement_id: str,
    data: ArchitectureDesignCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _create_architecture("refinement", refinement_id, data, user_id, uow)


@router.get("/specs/{spec_id}/architecture", response_model=list[ArchitectureDesignSummary])
async def list_spec_architecture(
    spec_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _list_architecture("spec", spec_id, user_id, uow)


@router.post(
    "/specs/{spec_id}/architecture",
    response_model=ArchitectureDesignResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_spec_architecture(
    spec_id: str,
    data: ArchitectureDesignCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _create_architecture("spec", spec_id, data, user_id, uow)


@router.get("/cards/{card_id}/architecture", response_model=list[ArchitectureDesignSummary])
async def list_card_architecture(
    card_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _list_architecture("card", card_id, user_id, uow)


@router.post(
    "/cards/{card_id}/architecture",
    response_model=ArchitectureDesignResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_card_architecture(
    card_id: str,
    data: ArchitectureDesignCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    return await _create_architecture("card", card_id, data, user_id, uow)


@router.post("/architecture/validate", response_model=ArchitecturePayloadValidationResponse)
async def validate_architecture_payload(
    data: ArchitecturePayloadValidationRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return the same Architecture Design critique exposed to MCP agents."""
    request_payload = data.model_dump(mode="json", exclude_none=True)
    design_id = request_payload.pop("design_id", None)
    result = await ValidateArchitecturePayloadUseCase().execute(
        ValidateArchitecturePayloadCommand(request_payload, design_id),
        actor=RESTAdapterContract.actor(user_id),
        uow=uow,
    )
    return result.critique


@router.get("/architecture/propagation-legacy-report")
async def architecture_propagation_legacy_report(
    board_id: str = Query(...),
    limit: int = Query(100),
    offset: int = Query(0),
    include_clean: bool = Query(False),
    parent_type_filter: str = Query(""),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Spec C: read-only, forward-only diagnostic of legacy Architecture Design snapshots
    whose SOURCE is now ineligible for propagation. Bounded/idempotent; never mutates
    snapshots, findings, or SDLC status. Registered before /architecture/{design_id} so the
    static path wins route matching."""
    result = await ArchitecturePropagationLegacyReportUseCase().execute(
        ArchitecturePropagationLegacyReportCommand(
            board_id,
            limit=limit,
            offset=offset,
            include_clean=include_clean,
            parent_type_filter=parent_type_filter,
        ),
        actor=RESTAdapterContract.actor(user_id),
        uow=uow,
    )
    return result.report


@router.get("/architecture/{design_id}", response_model=ArchitectureDesignResponse)
async def get_architecture_design(
    design_id: str,
    include_payloads: bool = Query(False),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        result = await GetArchitectureDesignUseCase().execute(
            GetArchitectureDesignCommand(design_id, include_payloads),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type} not found",
        )
    return result.response


@router.patch("/architecture/{design_id}", response_model=ArchitectureDesignResponse)
async def update_architecture_design(
    design_id: str,
    data: ArchitectureDesignUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        result = await UpdateArchitectureDesignUseCase().execute(
            UpdateArchitectureDesignCommand(design_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type} not found",
        )
    except ConflictError as exc:
        raise _http_error_from_conflict(exc)
    except ValueError as error:
        raise _http_error_from_value(error)
    return result.response


@router.delete("/architecture/{design_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_architecture_design(
    design_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        await DeleteArchitectureDesignUseCase().execute(
            DeleteArchitectureDesignCommand(design_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type} not found",
        )
    except ConflictError as exc:
        raise _http_error_from_conflict(exc)


@router.get(
    "/architecture/{design_id}/diagrams/{diagram_id}/payload",
    response_model=ArchitectureDiagramPayloadResponse,
)
async def get_architecture_diagram_payload(
    design_id: str,
    diagram_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """R01A FU5-S1C: thin REST adapter over ``GetArchitectureDiagramPayloadUseCase``.
    The two distinct legacy 404s are preserved via the use case's
    ``EntityNotFoundError`` entity type — a missing design → "Architecture design
    not found"; a missing diagram / absent ``adapter_payload_ref`` / unresolved
    store payload → "Diagram payload not found"."""
    try:
        result = await GetArchitectureDiagramPayloadUseCase().execute(
            GetArchitectureDiagramPayloadCommand(design_id, diagram_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type} not found",
        )
    return result.response


@router.put("/architecture/{design_id}/diagrams/{diagram_id}/payload", response_model=ArchitectureDesignResponse)
async def update_architecture_diagram_payload(
    design_id: str,
    diagram_id: str,
    data: DiagramPayloadUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """R01A FU5-S1C: thin REST adapter over ``UpdateArchitectureDiagramPayloadUseCase``.
    Mutability gate → 404 design / 409 card-read-only / 409 spec-locked; a missing
    diagram → 404 "Diagram not found"; ``repo.update``'s ``ValueError`` family →
    ``_http_error_from_value`` — exactly the legacy mapping."""
    try:
        result = await UpdateArchitectureDiagramPayloadUseCase().execute(
            UpdateArchitectureDiagramPayloadCommand(
                design_id,
                diagram_id,
                data.format,
                data.payload,
                data.change_summary,
                data.architecture_warning_acknowledgement,
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type} not found",
        )
    except ConflictError as exc:
        raise _http_error_from_conflict(exc)
    except ValueError as error:
        raise _http_error_from_value(error)
    return result.response


@router.post("/architecture/{design_id}/diagrams/import-excalidraw", response_model=ArchitectureDesignResponse)
async def import_excalidraw_architecture_diagram(
    design_id: str,
    data: ExcalidrawImportRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """R01A FU5-S1C: thin REST adapter over ``ImportExcalidrawArchitectureDiagramUseCase``.
    Mutability gate → 404 design / 409 card-read-only / 409 spec-locked; a missing
    ``replace_diagram_id`` → 404 "Diagram not found"; ``repo.update``'s
    ``ValueError`` family → ``_http_error_from_value`` — exactly the legacy
    mapping."""
    try:
        result = await ImportExcalidrawArchitectureDiagramUseCase().execute(
            ImportExcalidrawArchitectureDiagramCommand(
                design_id,
                data.title,
                data.payload,
                data.diagram_type,
                data.description,
                data.order_index,
                data.replace_diagram_id,
                data.change_summary,
                data.architecture_warning_acknowledgement,
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type} not found",
        )
    except ConflictError as exc:
        raise _http_error_from_conflict(exc)
    except ValueError as error:
        raise _http_error_from_value(error)
    return result.response


@router.get("/architecture/{design_id}/diff", response_model=ArchitectureDiffResponse)
async def get_architecture_diff(
    design_id: str,
    from_version: int = Query(..., ge=1),
    to_version: int = Query(..., ge=1),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """R01A FU5-S1C: thin REST adapter over ``GetArchitectureDiffUseCase``. The
    repository's "version not found" ``ValueError`` → ``_http_error_from_value``
    (404) exactly as the legacy endpoint did."""
    try:
        result = await GetArchitectureDiffUseCase().execute(
            GetArchitectureDiffCommand(design_id, from_version, to_version),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as error:
        raise _http_error_from_value(error)
    return result.response


@router.post("/cards/{card_id}/copy-architecture-from-spec/{spec_id}", response_model=list[ArchitectureDesignResponse])
async def copy_architecture_from_spec_to_card(
    card_id: str,
    spec_id: str,
    data: CopyArchitectureRequest | None = None,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """R01A FU5-S1C: thin REST adapter over ``CopyArchitectureFromSpecToCardUseCase``.
    A missing card → 404 "card not found" (legacy ``_ensure_parent``); the
    propagation service's structured errors keep their EXACT legacy mapping and
    ordering — ``ResourceLineageResolutionError`` → 422, ``ResourcePropagationError``
    → 422 (with ``spec_id``), ``ArchitecturePropagationBlocked`` → 422 structured,
    then the generic ``ValueError`` → ``_http_error_from_value``."""
    try:
        result = await CopyArchitectureFromSpecToCardUseCase().execute(
            CopyArchitectureFromSpecToCardCommand(
                card_id,
                spec_id,
                (data.design_ids if data else None),
                (data.architecture_warning_acknowledgement if data else None),
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type} not found",
        )
    except ResourceLineageResolutionError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error.to_error_dict(),
        ) from error
    except ResourcePropagationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error.to_error_dict(spec_id=spec_id),
        ) from error
    except ArchitecturePropagationBlocked as error:
        # Spec B: a source design ineligible for propagation surfaces the canonical
        # structured error (architecture_propagation_blocked), not a generic 422.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error.to_error_dict(),
        ) from error
    except ValueError as error:
        raise _http_error_from_value(error)
    return result.responses
