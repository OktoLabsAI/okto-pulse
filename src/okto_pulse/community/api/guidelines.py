"""Guideline API endpoints.

Spec R01A REST-FU7-S3: every endpoint here now routes through a transport-free
use case (``application/use_cases/guidelines_crud.py``) over a
``PulseUnitOfWork`` — no endpoint binds ``get_db`` / a raw ``AsyncSession``
anymore. This module is a thin inbound adapter: it builds the command/actor,
maps the typed use-case errors back to the EXACT legacy HTTP status + detail
(``EntityNotFoundError`` → the per-entity 404 string, ``CommandValidationError``
→ the 422 inline-create detail), and returns the use case's shaped payloads.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    ActorContext,
    CommandValidationError,
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.policy_governance import (
    require_policy_governance_capabilities,
)
from okto_pulse.core.application.use_cases.guidelines_crud import (
    CreateGuidelineCommand,
    CreateGuidelineUseCase,
    DeleteGuidelineCommand,
    DeleteGuidelineUseCase,
    GetBoardGuidelinesCommand,
    GetBoardGuidelinesUseCase,
    GetGuidelineCommand,
    GetGuidelineUseCase,
    LinkOrCreateBoardGuidelineCommand,
    LinkOrCreateBoardGuidelineUseCase,
    ListGuidelinesCommand,
    ListGuidelinesUseCase,
    UnlinkBoardGuidelineCommand,
    UnlinkBoardGuidelineUseCase,
    UpdateBoardGuidelinePriorityCommand,
    UpdateBoardGuidelinePriorityUseCase,
    UpdateGuidelineCommand,
    UpdateGuidelineUseCase,
)
from okto_pulse.core.application.use_cases.import_export import (
    KIND_GUIDELINES,
    EnvelopeError,
    ExportGuidelinesCommand,
    ExportGuidelinesUseCase,
    ImportGuidelinesCommand,
    ImportGuidelinesUseCase,
    ImportItemError,
    parse_import_envelope,
    validate_items,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_principal, require_user
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyBindingConflict,
)
from okto_pulse.core.models.schemas import (
    BoardGuidelineLinkRequest,
    GuidelineCreate,
    GuidelineResponse,
    GuidelineUpdate,
)
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


_NOT_FOUND_DETAIL = {
    "board": "Board not found",
    "guideline": "Guideline not found",
    "guideline_owned": "Guideline not found or not owned by user",
    "link": "Link not found",
}


def _not_found(exc: EntityNotFoundError) -> str:
    """Map the typed ``EntityNotFoundError`` back to the exact legacy 404 detail."""
    return _NOT_FOUND_DETAIL.get(exc.entity_type, "Not found")


def _legacy_policy_actor(
    principal: Principal,
    *,
    capability: str,
    board_id: str | None = None,
):
    """Authorize legacy PATCH/DELETE against the introduced SK-B leaves."""

    actor = RESTAdapterContract.actor_from_principal(
        principal,
        board_id=board_id,
    )
    try:
        require_policy_governance_capabilities(actor, capability)
    except PermissionDeniedError as error:
        from okto_pulse.core.inbound.guideline_policy_error import (
            guideline_policy_http_status,
            project_guideline_policy_error,
        )

        raise HTTPException(
            status_code=guideline_policy_http_status(error),
            detail=project_guideline_policy_error(error),
        )
    return actor


def _require_board_guideline_adoption_manager(
    board_id: str,
    principal: Principal = Depends(require_principal),
) -> ActorContext:
    """Authorize the legacy unlink before FastAPI resolves its UoW."""

    return _legacy_policy_actor(
        principal,
        capability="guidelines.adoption.manage",
        board_id=board_id,
    )


def _guideline_adoption_preview_required() -> HTTPException:
    """Project the governed replacement for legacy link/priority mutation."""

    from okto_pulse.core.inbound.guideline_policy_error import (
        guideline_policy_http_status,
        project_guideline_policy_error,
    )
    from okto_pulse.core.ports.guideline_policy import (
        GuidelinePolicyBindingConflict,
    )

    error = GuidelinePolicyBindingConflict(
        "guideline_impact_preview_required"
    )
    return HTTPException(
        status_code=guideline_policy_http_status(error),
        detail=project_guideline_policy_error(error),
    )


# ============================================================================
# Global Guidelines CRUD
# ============================================================================


@router.get("/guidelines", response_model=list[GuidelineResponse])
async def list_guidelines(
    # ``le=PAGE_OFFSET_MAX``: see kg_canonical_debt — an offset above SQLite's
    # signed 64-bit INTEGER reached the SQL OFFSET bind and surfaced as an
    # uncaught OverflowError (HTTP 500 text/plain). Bounded here it becomes the
    # canonical typed 422 JSON.
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(50, ge=1, le=200),
    tag: str | None = Query(None),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List global guidelines for the current user."""
    result = await ListGuidelinesUseCase().execute(
        ListGuidelinesCommand(offset=offset, limit=limit, tag=tag),
        actor=RESTAdapterContract.actor(user_id),
        uow=uow,
    )
    return result.guidelines


@router.post("/guidelines", response_model=GuidelineResponse, status_code=status.HTTP_201_CREATED)
async def create_guideline(
    data: GuidelineCreate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a guideline identity and its immutable initial revision."""
    actor = _legacy_policy_actor(
        principal,
        capability="guidelines.revisions.create",
        board_id=data.board_id,
    )
    result = await CreateGuidelineUseCase().execute(
        CreateGuidelineCommand(data),
        actor=actor,
        uow=uow,
    )
    return result.guideline


# NOTE: the literal /guidelines/export and /guidelines/import routes MUST be
# declared BEFORE the parametric /guidelines/{guideline_id} below — FastAPI
# matches in registration order, so the param route would otherwise swallow
# "export" as a guideline id (same shadowing as /guidelines/default-candidates,
# see api/router.py).


@router.get("/guidelines/export")
async def export_guidelines(
    board_id: str | None = Query(None),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Export the actor's global guidelines + (if board_id) the board's inline
    guidelines as a schema_version-1 envelope (kind=guidelines)."""
    try:
        return await ExportGuidelinesUseCase().execute(
            ExportGuidelinesCommand(board_id=board_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))


@router.post("/guidelines/import")
async def import_guidelines(
    envelope: dict,
    dry_run: bool = Query(False),
    board_id: str | None = Query(None),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Import a kind=guidelines envelope. Items are validated against the same
    ``GuidelineCreate`` schema as POST /guidelines; identical titles in the
    target partition are skipped as duplicates; any invalid item → 400 and
    NOTHING is mutated (all-or-nothing). ``dry_run=true`` validates and reports
    without persisting. ``board_id`` retargets inline items to that board."""
    # Authorize before parsing the document or entering a use-case read path.
    # The use case repeats this capability check as defense in depth.
    actor = _legacy_policy_actor(
        principal,
        capability="guidelines.revisions.create",
        board_id=board_id,
    )
    try:
        raw_items = parse_import_envelope(envelope, kind=KIND_GUIDELINES)
    except EnvelopeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_envelope", "message": str(exc)},
        )
    parsed, errors = validate_items(raw_items, GuidelineCreate.model_validate)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"created": 0, "skipped": [], "errors": errors},
        )
    try:
        result = await ImportGuidelinesUseCase().execute(
            ImportGuidelinesCommand(items=parsed, board_id=board_id, dry_run=dry_run),
            actor=actor,
            uow=uow,
        )
    except ImportItemError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "created": 0,
                "skipped": [],
                "errors": [{"index": exc.index, "detail": exc.detail}],
            },
        )
    return result.payload(dry_run=dry_run)


@router.get("/guidelines/{guideline_id}", response_model=GuidelineResponse)
async def get_guideline(
    guideline_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a guideline by ID."""
    try:
        result = await GetGuidelineUseCase().execute(
            GetGuidelineCommand(guideline_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.guideline


@router.patch("/guidelines/{guideline_id}", response_model=GuidelineResponse)
async def update_guideline(
    guideline_id: str,
    data: GuidelineUpdate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a guideline."""
    actor = _legacy_policy_actor(
        principal,
        capability="guidelines.revisions.create",
    )
    try:
        result = await UpdateGuidelineUseCase().execute(
            UpdateGuidelineCommand(guideline_id, data),
            actor=actor,
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.guideline


@router.delete("/guidelines/{guideline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_guideline(
    guideline_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a guideline."""
    actor = _legacy_policy_actor(
        principal,
        capability="guidelines.revisions.retire",
    )
    try:
        await DeleteGuidelineUseCase().execute(
            DeleteGuidelineCommand(guideline_id),
            actor=actor,
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))


# ============================================================================
# Board Guidelines (linked + inline)
# ============================================================================


@router.get("/boards/{board_id}/guidelines")
async def get_board_guidelines(
    board_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get all guidelines for a board (linked globals + inline), sorted by priority."""
    try:
        result = await GetBoardGuidelinesUseCase().execute(
            GetBoardGuidelinesCommand(board_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.items


@router.post("/boards/{board_id}/guidelines", status_code=status.HTTP_201_CREATED)
async def link_or_create_board_guideline(
    board_id: str,
    data: BoardGuidelineLinkRequest,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create inline context; governed links require preview then adoption."""
    actor = _legacy_policy_actor(
        principal,
        capability=(
            "guidelines.adoption.manage"
            if data.guideline_id
            else "guidelines.revisions.create"
        ),
        board_id=board_id,
    )
    try:
        result = await LinkOrCreateBoardGuidelineUseCase().execute(
            LinkOrCreateBoardGuidelineCommand(board_id, data),
            actor=actor,
            uow=uow,
        )
    except GuidelinePolicyBindingConflict:
        raise _guideline_adoption_preview_required()
    except CommandValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.payload


@router.delete("/boards/{board_id}/guidelines/{guideline_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unlink_board_guideline(
    board_id: str,
    guideline_id: str,
    actor: ActorContext = Depends(
        _require_board_guideline_adoption_manager
    ),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Unlink a guideline from a board."""
    try:
        await UnlinkBoardGuidelineUseCase().execute(
            UnlinkBoardGuidelineCommand(board_id, guideline_id),
            actor=actor,
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))


@router.patch("/boards/{board_id}/guidelines/{guideline_id}")
async def update_board_guideline_priority(
    board_id: str,
    guideline_id: str,
    data: dict,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Reject the legacy direct mutation in favor of preview then adoption."""
    priority = data.get("priority", 0)
    actor = _legacy_policy_actor(
        principal,
        capability="guidelines.adoption.manage",
        board_id=board_id,
    )
    try:
        await UpdateBoardGuidelinePriorityUseCase().execute(
            UpdateBoardGuidelinePriorityCommand(
                board_id,
                guideline_id,
                priority,
            ),
            actor=actor,
            uow=uow,
        )
    except GuidelinePolicyBindingConflict:
        raise _guideline_adoption_preview_required()
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=_not_found(exc),
        )
    raise RuntimeError("legacy guideline priority unexpectedly mutated state")
