"""Current guideline catalog, context creation and Board unlink REST adapter.

Revision editing, retirement, adoption and import/export use policy_governance.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    ActorContext,
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.policy_governance import (
    require_policy_governance_capabilities,
)
from okto_pulse.core.application.use_cases.guidelines_crud import (
    CreateGuidelineCommand,
    CreateGuidelineUseCase,
    GetBoardGuidelinesCommand,
    GetBoardGuidelinesUseCase,
    GetGuidelineCommand,
    GetGuidelineUseCase,
    CreateBoardGuidelineCommand,
    CreateBoardGuidelineUseCase,
    ListGuidelinesCommand,
    ListGuidelinesUseCase,
    UnlinkBoardGuidelineCommand,
    UnlinkBoardGuidelineUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_principal, require_user
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX
from okto_pulse.core.models.schemas import (
    BoardGuidelineCreate,
    GuidelineCreate,
    GuidelineResponse,
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
    """Map the typed ``EntityNotFoundError`` back to the typed 404 detail."""
    return _NOT_FOUND_DETAIL.get(exc.entity_type, "Not found")


def _guideline_policy_actor(
    principal: Principal,
    *,
    capability: str,
    board_id: str | None = None,
):
    """Authorize guideline creation or unlink through the current capabilities."""

    actor = RESTAdapterContract.actor_from_principal(
        principal,
        board_id=board_id,
    )
    try:
        require_policy_governance_capabilities(actor, capability)
    except PermissionDeniedError as error:
        raise _policy_http_error(error)
    return actor


def _policy_http_error(error: Exception) -> HTTPException:
    """Project governed guideline errors through the bounded Core contract."""

    from okto_pulse.core.inbound.guideline_policy_error import (
        guideline_policy_http_status,
        project_guideline_policy_error,
    )

    try:
        http_status = guideline_policy_http_status(error)
        detail = project_guideline_policy_error(error)
    except TypeError:
        raise error
    return HTTPException(status_code=http_status, detail=detail)


def _require_board_guideline_adoption_manager(
    board_id: str,
    principal: Principal = Depends(require_principal),
) -> ActorContext:
    """Authorize unlink before FastAPI resolves its UoW."""

    return _guideline_policy_actor(
        principal,
        capability="guidelines.adoption.manage",
        board_id=board_id,
    )




# ============================================================================
# Global Guidelines CRUD
# ============================================================================


@router.get("/guidelines", response_model=list[GuidelineResponse])
async def list_guidelines(
    # ``le=PAGE_OFFSET_MAX``: an offset above SQLite's
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
    actor = _guideline_policy_actor(
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
async def create_board_guideline(
    board_id: str,
    data: BoardGuidelineCreate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create inline context; governed links require preview then adoption."""
    actor = _guideline_policy_actor(
        principal,
        capability="guidelines.revisions.create",
        board_id=board_id,
    )
    try:
        result = await CreateBoardGuidelineUseCase().execute(
            CreateBoardGuidelineCommand(board_id, data),
            actor=actor,
            uow=uow,
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
