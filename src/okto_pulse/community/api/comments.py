"""Comment API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.card_collaboration import (
    CardNotFoundError,
    ChoiceCommentNotFoundError,
    CommentMutationNotFoundError,
    CreateCardCommentCommand,
    CreateCardCommentUseCase,
    DeleteCardCommentCommand,
    DeleteCardCommentUseCase,
    InvalidChoiceResponseError,
    RespondToChoiceCommentCommand,
    RespondToChoiceCommentUseCase,
    UpdateCardCommentCommand,
    UpdateCardCommentUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.models import CommentCreate, CommentUpdate, CommentResponse
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


@router.post("/card/{card_id}", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def create_comment(
    card_id: str,
    data: CommentCreate,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new comment on a card."""
    try:
        result = await CreateCardCommentUseCase().execute(
            CreateCardCommentCommand(card_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=db,
        )
    except CardNotFoundError as exc:
        raise RESTAdapterContract.http_error(exc, not_found_detail="Card not found")
    return result.comment


@router.patch("/{comment_id}", response_model=CommentResponse)
async def update_comment(
    comment_id: str,
    data: CommentUpdate,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a comment."""
    try:
        result = await UpdateCardCommentUseCase().execute(
            UpdateCardCommentCommand(comment_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=db,
        )
    except CommentMutationNotFoundError as exc:
        raise RESTAdapterContract.http_error(
            exc, not_found_detail="Comment not found or not owned by user"
        )
    return result.comment


@router.post("/{comment_id}/respond", response_model=CommentResponse)
async def respond_to_choice(
    comment_id: str,
    data: dict,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Respond to a choice board comment."""
    try:
        result = await RespondToChoiceCommentUseCase().execute(
            RespondToChoiceCommentCommand(
                comment_id,
                selected=data.get("selected", []),
                free_text=data.get("free_text"),
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=db,
        )
    except ChoiceCommentNotFoundError as exc:
        raise RESTAdapterContract.http_error(exc, not_found_detail="Comment not found")
    except InvalidChoiceResponseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return result.comment


@router.delete("/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    comment_id: str,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a comment."""
    try:
        await DeleteCardCommentUseCase().execute(
            DeleteCardCommentCommand(comment_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=db,
        )
    except CommentMutationNotFoundError as exc:
        raise RESTAdapterContract.http_error(
            exc, not_found_detail="Comment not found or not owned by user"
        )
