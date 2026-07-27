"""Q&A API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.card_collaboration import (
    AnswerCardQuestionCommand,
    AnswerCardQuestionUseCase,
    CardNotFoundError,
    CreateCardQuestionCommand,
    CreateCardQuestionUseCase,
    DeleteCardQuestionCommand,
    DeleteCardQuestionUseCase,
    QuestionNotFoundError,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.models import QACreate, QAAnswer, QAResponse
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.application.errors import (
    QASelfAnsweringNotAllowedError,
    QASelectionError,
)

router = APIRouter()


@router.post("/card/{card_id}", response_model=QAResponse, status_code=status.HTTP_201_CREATED)
async def create_question(
    card_id: str,
    data: QACreate,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new question on a card."""
    try:
        result = await CreateCardQuestionUseCase().execute(
            CreateCardQuestionCommand(card_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=db,
        )
    except CardNotFoundError as exc:
        raise RESTAdapterContract.http_error(exc, not_found_detail="Card not found")
    return result.qa


@router.post("/{qa_id}/answer", response_model=QAResponse)
async def answer_question(
    qa_id: str,
    data: QAAnswer,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Answer a question."""
    try:
        result = await AnswerCardQuestionUseCase().execute(
            AnswerCardQuestionCommand(qa_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=db,
        )
    except QASelfAnsweringNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"reason": exc.reason, "message": str(exc)},
        ) from exc
    except QASelectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.to_error_dict(),
        ) from exc
    except QuestionNotFoundError as exc:
        raise RESTAdapterContract.http_error(exc, not_found_detail="Q&A item not found")
    return result.qa


@router.delete("/{qa_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_question(
    qa_id: str,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a Q&A item."""
    try:
        await DeleteCardQuestionUseCase().execute(
            DeleteCardQuestionCommand(qa_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=db,
        )
    except QuestionNotFoundError as exc:
        raise RESTAdapterContract.http_error(exc, not_found_detail="Q&A item not found")
