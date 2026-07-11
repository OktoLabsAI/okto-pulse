"""Board API endpoints."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    CreateBoardCommand,
    CreateBoardUseCase,
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.boards_crud import (
    ArchiveTreeCommand,
    ArchiveTreeUseCase,
    CreateCardInBoardCommand,
    CreateCardInBoardUseCase,
    DeleteBoardCommand,
    DeleteBoardUseCase,
    GetBoardColumnsCommand,
    GetBoardColumnsUseCase,
    GetBoardCommand,
    GetBoardUseCase,
    ListBoardsCommand,
    ListBoardSharesCommand,
    ListBoardSharesUseCase,
    ListBoardsUseCase,
    RestoreTreeCommand,
    RestoreTreeUseCase,
    RevokeBoardShareCommand,
    RevokeBoardShareUseCase,
    ShareBoardCommand,
    ShareBoardUseCase,
    UpdateBoardCommand,
    UpdateBoardShareCommand,
    UpdateBoardShareUseCase,
    UpdateBoardUseCase,
)
from okto_pulse.core.domain.enums import CardStatus
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user, get_realm_id
from okto_pulse.core.models import (
    BoardCreate,
    BoardResponse,
    BoardShareCreate,
    BoardShareResponse,
    BoardShareUpdate,
    BoardSummary,
    BoardUpdate,
    CardCreate,
    CardResponse,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.application.errors import CardOperationError

router = APIRouter()


@router.post("", response_model=BoardResponse, status_code=status.HTTP_201_CREATED)
async def create_board(
    data: BoardCreate,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new board."""
    # Spec #04: the migrated REST flow obtains a request-scoped PulseUnitOfWork
    # (from the persistence port, bound to the request session via get_db) and
    # calls the transport-free use case — no raw AsyncSession in the handler's
    # contract with the use case. Behavior (payload/201/commit/re-fetch/effective
    # settings/realm_id) is preserved, and the get_db dependency override still
    # applies because get_unit_of_work depends on it.
    result = await CreateBoardUseCase().execute(
        CreateBoardCommand(data),
        actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
        uow=uow,
    )
    return result.board


@router.get("", response_model=list[BoardSummary])
async def list_boards(
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    view: Literal["my", "shared", "all"] = Query("my"),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List boards for the current user. view: my|shared|all."""
    result = await ListBoardsUseCase().execute(
        ListBoardsCommand(offset=offset, limit=limit, view=view),
        actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
        uow=uow,
    )
    return result.boards


@router.get("/{board_id}", response_model=BoardResponse)
async def get_board(
    board_id: str,
    compact: bool = Query(False, description="When true, omit inline cards/agents and return only the overview envelope with counts. Default false preserves the legacy full payload for the existing frontend."),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a board by ID. By default returns the full board (legacy shape);
    pass `compact=true` for the overview envelope (Ideação token-optimization
    Story 2 — ~200B vs ~10KB).
    """
    try:
        result = await GetBoardUseCase().execute(
            GetBoardCommand(board_id, compact=compact),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.board


@router.patch("/{board_id}", response_model=BoardResponse)
async def update_board(
    board_id: str,
    data: BoardUpdate,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a board."""
    try:
        result = await UpdateBoardUseCase().execute(
            UpdateBoardCommand(board_id, data),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.board


@router.delete("/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board(
    board_id: str,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a board and all its cards."""
    try:
        await DeleteBoardUseCase().execute(
            DeleteBoardCommand(board_id),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")


@router.post("/{board_id}/cards", response_model=CardResponse, status_code=status.HTTP_201_CREATED)
async def create_card(
    board_id: str,
    data: CardCreate,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new card in a board."""
    try:
        result = await CreateCardInBoardUseCase().execute(
            CreateCardInBoardCommand(board_id, data),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except CardOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Board not found or not owned by user"
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return result.card


@router.get("/{board_id}/columns")
async def get_board_columns(
    board_id: str,
    include_archived: bool = Query(False, alias="include_archived"),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get board cards grouped by status/column."""
    try:
        result = await GetBoardColumnsUseCase().execute(
            GetBoardColumnsCommand(board_id),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")

    board = result.board
    # Group cards by status (exclude archived unless requested)
    columns = {s.value: [] for s in CardStatus}
    for card in board.cards:
        if not include_archived and getattr(card, "archived", False):
            continue
        columns[card.status.value].append({
            "id": card.id,
            "board_id": card.board_id,
            "spec_id": card.spec_id,
            "title": card.title,
            "description": card.description,
            "status": card.status.value,
            "priority": card.priority.value if card.priority else "none",
            "position": card.position,
            "assignee_id": card.assignee_id,
            "created_by": card.created_by,
            "created_at": card.created_at.isoformat(),
            "updated_at": card.updated_at.isoformat(),
            "due_date": card.due_date.isoformat() if card.due_date else None,
            "labels": card.labels or [],
            "test_scenario_ids": card.test_scenario_ids,
            "conclusions": card.conclusions,
            # Bug card fields
            "card_type": getattr(card, "card_type", "normal") or "normal",
            "origin_task_id": getattr(card, "origin_task_id", None),
            "severity": getattr(card, "severity", None),
            "linked_test_task_ids": getattr(card, "linked_test_task_ids", None),
            "archived": getattr(card, "archived", False),
            # Unanswered Q&A count (answered_at IS NULL) for the kanban card badge.
            # card.qa_items is eager-loaded by BoardService.get_board.
            "open_qa_count": sum(1 for q in (card.qa_items or []) if q.answered_at is None),
        })

    return {"board_id": board_id, "columns": columns}


# ==================== ARCHIVE ====================


@router.post("/{board_id}/archive/{entity_type}/{entity_id}")
async def archive_tree(
    board_id: str,
    entity_type: str,
    entity_id: str,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Archive an entity and all its descendants in cascade."""
    try:
        result = await ArchiveTreeUseCase().execute(
            ArchiveTreeCommand(entity_type, entity_id),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id, board_id=board_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"success": True, "archived_count": result.counts}


@router.post("/{board_id}/restore/{entity_type}/{entity_id}")
async def restore_tree(
    board_id: str,
    entity_type: str,
    entity_id: str,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Restore an archived entity and all its descendants."""
    try:
        result = await RestoreTreeUseCase().execute(
            RestoreTreeCommand(entity_type, entity_id),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id, board_id=board_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"success": True, "restored_count": result.counts}


# ==================== SHARES ====================


@router.post(
    "/{board_id}/shares",
    response_model=BoardShareResponse,
    status_code=status.HTTP_201_CREATED,
)
async def share_board(
    board_id: str,
    data: BoardShareCreate,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Share a board with another user (owner/admin only)."""
    try:
        result = await ShareBoardUseCase().execute(
            ShareBoardCommand(board_id, data),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
    return result.share


@router.get("/{board_id}/shares", response_model=list[BoardShareResponse])
async def list_board_shares(
    board_id: str,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all shares for a board."""
    try:
        result = await ListBoardSharesUseCase().execute(
            ListBoardSharesCommand(board_id),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.shares


@router.patch("/{board_id}/shares/{share_id}", response_model=BoardShareResponse)
async def update_board_share(
    board_id: str,
    share_id: str,
    data: BoardShareUpdate,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a share's permission (owner/admin only)."""
    try:
        result = await UpdateBoardShareUseCase().execute(
            UpdateBoardShareCommand(share_id, data),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id, board_id=board_id),
            uow=uow,
        )
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
    return result.share


@router.delete("/{board_id}/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_board_share(
    board_id: str,
    share_id: str,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Revoke a share (owner/admin can revoke, shared user can leave)."""
    try:
        await RevokeBoardShareUseCase().execute(
            RevokeBoardShareCommand(share_id),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id, board_id=board_id),
            uow=uow,
        )
    except PermissionDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
