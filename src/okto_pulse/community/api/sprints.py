"""Sprint API endpoints.

Spec R01A REST-FU7-S2: every endpoint here now routes through a transport-free
use case (``application/use_cases/sprints_crud.py``) over a ``PulseUnitOfWork`` —
no endpoint binds ``get_db`` / a raw ``AsyncSession`` / ``select`` anymore. This
module is a thin inbound adapter: it builds the command/actor, maps the typed
use-case errors back to the EXACT legacy HTTP status + detail
(``EntityNotFoundError`` → the per-entity 404 string, ``SprintOperationError`` →
400 ``to_dict()``, ``ValueError`` → 400 ``str``), and shapes the response
payloads (the sprint re-fetch, the evaluation envelope, the assign/unassign lane
envelope). The legacy sprint endpoints carried no permission gate, so neither do
these.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import EntityNotFoundError
from okto_pulse.core.application.use_cases.sprints_crud import (
    AssignSprintTasksCommand,
    AssignSprintTasksUseCase,
    CreateSprintCommand,
    CreateSprintUseCase,
    DeleteSprintCommand,
    DeleteSprintUseCase,
    GetSprintCommand,
    GetSprintUseCase,
    ListBoardSprintsCommand,
    ListBoardSprintsUseCase,
    ListSprintHistoryCommand,
    ListSprintHistoryUseCase,
    ListSprintsCommand,
    ListSprintsUseCase,
    MoveSprintCommand,
    MoveSprintUseCase,
    SubmitSprintEvaluationCommand,
    SubmitSprintEvaluationUseCase,
    SuggestSprintsCommand,
    SuggestSprintsUseCase,
    UnassignSprintTasksCommand,
    UnassignSprintTasksUseCase,
    UpdateSprintCommand,
    UpdateSprintUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.models.schemas import (
    SprintCreate,
    SprintHistoryResponse,
    SprintMove,
    SprintResponse,
    SprintSummary,
    SprintUpdate,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.cancellation import CancellationReasonRequiredError
from okto_pulse.core.application.errors import SprintOperationError

router = APIRouter()


_NOT_FOUND_DETAIL = {
    "board": "Board not found",
    "spec": "Spec not found",
    "sprint": "Sprint not found",
    "spec_or_board": "Spec or board not found",
}


def _not_found(exc: EntityNotFoundError) -> str:
    """Map the typed ``EntityNotFoundError`` back to the exact legacy 404 detail."""
    return _NOT_FOUND_DETAIL.get(exc.entity_type, "Not found")


@router.get("/boards/{board_id}/sprints", response_model=list[SprintSummary])
async def list_board_sprints(
    board_id: str,
    status_filter: str | None = Query(None, alias="status"),
    spec_id: str | None = Query(None, alias="spec_id"),
    include_archived: bool = Query(False),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all sprints for a board, optionally filtered by status and/or spec."""
    try:
        result = await ListBoardSprintsUseCase().execute(
            ListBoardSprintsCommand(
                board_id,
                status_filter=status_filter,
                spec_id=spec_id,
                include_archived=include_archived,
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.sprints


@router.post(
    "/boards/{board_id}/specs/{spec_id}/sprints",
    response_model=SprintResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sprint(
    board_id: str,
    spec_id: str,
    data: SprintCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new sprint for a spec."""
    try:
        result = await CreateSprintUseCase().execute(
            CreateSprintCommand(board_id, data, spec_id=spec_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except SprintOperationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.sprint


@router.get("/boards/{board_id}/specs/{spec_id}/sprints", response_model=list[SprintSummary])
async def list_sprints(
    board_id: str,
    spec_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List sprints for a spec."""
    try:
        result = await ListSprintsUseCase().execute(
            ListSprintsCommand(spec_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.sprints


@router.get("/sprints/{sprint_id}", response_model=SprintResponse)
async def get_sprint(
    sprint_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a sprint by ID with full details."""
    try:
        result = await GetSprintUseCase().execute(
            GetSprintCommand(sprint_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.sprint


@router.patch("/sprints/{sprint_id}", response_model=SprintResponse)
async def update_sprint(
    sprint_id: str,
    data: SprintUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a sprint."""
    try:
        result = await UpdateSprintUseCase().execute(
            UpdateSprintCommand(sprint_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except SprintOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.sprint


@router.post("/sprints/{sprint_id}/move", response_model=SprintResponse)
async def move_sprint(
    sprint_id: str,
    data: SprintMove,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Move a sprint to a different status."""
    try:
        result = await MoveSprintUseCase().execute(
            MoveSprintCommand(sprint_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except CancellationReasonRequiredError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.to_dict())
    except SprintOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.sprint


@router.delete("/sprints/{sprint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sprint(
    sprint_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a sprint."""
    try:
        await DeleteSprintUseCase().execute(
            DeleteSprintCommand(sprint_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except SprintOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))


@router.post("/sprints/{sprint_id}/evaluations")
async def submit_evaluation(
    sprint_id: str,
    evaluation: dict,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Submit an evaluation for a sprint."""
    try:
        result = await SubmitSprintEvaluationUseCase().execute(
            SubmitSprintEvaluationCommand(sprint_id, evaluation),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except SprintOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    sprint = result.sprint
    return {"success": True, "evaluation_id": sprint.evaluations[-1]["id"] if sprint.evaluations else None}


@router.post("/sprints/{sprint_id}/assign-tasks")
async def assign_tasks(
    sprint_id: str,
    data: dict,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Assign cards to a sprint. Cards must belong to the same spec."""
    card_ids = data.get("card_ids", [])
    if not card_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="card_ids required")
    try:
        result = await AssignSprintTasksUseCase().execute(
            AssignSprintTasksCommand(sprint_id, card_ids),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except SprintOperationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    count = result.assigned
    sprint = result.sprint
    lane_type = sprint.lane_type.value if sprint else None
    accepted_card_types = (
        ["bug", "test"]
        if lane_type == "hotfix"
        else ["normal", "test", "bug"]
    )
    return {
        "success": True,
        "assigned": count,
        "assigned_count": count,
        "lane_type": lane_type,
        "accepted_card_types": accepted_card_types,
    }


@router.post("/sprints/{sprint_id}/unassign-tasks")
async def unassign_tasks(
    sprint_id: str,
    data: dict,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Remove cards from a sprint (set sprint_id to null)."""
    card_ids = data.get("card_ids", [])
    if not card_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="card_ids required")
    try:
        result = await UnassignSprintTasksUseCase().execute(
            UnassignSprintTasksCommand(sprint_id, card_ids),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except SprintOperationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.to_dict())
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return {"success": True, "unassigned": result.unassigned}


@router.get("/sprints/{sprint_id}/history", response_model=list[SprintHistoryResponse])
async def list_history(
    sprint_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List sprint history."""
    try:
        # This route has no board path parameter. Resolve the real parent through
        # the application boundary, then run the history read with a board-bound
        # actor instead of trusting caller-supplied scope.
        resolved = await GetSprintUseCase().execute(
            GetSprintCommand(sprint_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
        result = await ListSprintHistoryUseCase().execute(
            ListSprintHistoryCommand(sprint_id),
            actor=RESTAdapterContract.actor(
                user_id,
                board_id=resolved.sprint.board_id,
            ),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.history


@router.get("/boards/{board_id}/specs/{spec_id}/sprints/suggest")
async def suggest_sprints(
    board_id: str,
    spec_id: str,
    threshold: int = 8,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Suggest sprint breakdown for a spec."""
    try:
        result = await SuggestSprintsUseCase().execute(
            SuggestSprintsCommand(spec_id, threshold, board_id=board_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"suggestions": result.suggestions, "count": len(result.suggestions)}
