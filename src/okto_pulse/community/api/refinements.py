"""Refinement API endpoints.

Spec R01A REST-FU6-S3: every endpoint here now routes through a transport-free
use case (``application/use_cases/refinements_crud.py``) over a
``PulseUnitOfWork`` — no endpoint binds ``get_db`` / a raw ``AsyncSession``
anymore. This module is a thin inbound adapter: it builds the command/actor, maps
the typed use-case errors back to the EXACT legacy HTTP status + detail
(``EntityNotFoundError`` → the per-entity 404 string, ``ValueError`` → 400 where
the legacy endpoint caught it, ``QASelfAnsweringNotAllowedError`` → 403 with its
``{reason, message}`` detail), and returns the service payloads. The refinement
transition + content/critical-context gates stay inside ``RefinementService``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import EntityNotFoundError
from okto_pulse.core.application.use_cases.refinements_crud import (
    AnswerRefinementQuestionCommand,
    AnswerRefinementQuestionUseCase,
    CreateRefinementCommand,
    CreateRefinementKnowledgeCommand,
    CreateRefinementKnowledgeUseCase,
    CreateRefinementQuestionCommand,
    CreateRefinementQuestionUseCase,
    CreateRefinementUseCase,
    DeleteRefinementCommand,
    DeleteRefinementKnowledgeCommand,
    DeleteRefinementKnowledgeUseCase,
    DeleteRefinementQuestionCommand,
    DeleteRefinementQuestionUseCase,
    DeleteRefinementUseCase,
    DeriveSpecFromRefinementCommand,
    DeriveSpecFromRefinementUseCase,
    GetRefinementCommand,
    GetRefinementKnowledgeCommand,
    GetRefinementKnowledgeUseCase,
    GetRefinementSnapshotCommand,
    GetRefinementSnapshotUseCase,
    GetRefinementUseCase,
    ListRefinementHistoryCommand,
    ListRefinementHistoryUseCase,
    ListRefinementKnowledgeCommand,
    ListRefinementKnowledgeUseCase,
    ListRefinementQACommand,
    ListRefinementQAUseCase,
    ListRefinementSnapshotsCommand,
    ListRefinementSnapshotsUseCase,
    ListRefinementsCommand,
    ListRefinementsUseCase,
    MoveRefinementCommand,
    MoveRefinementUseCase,
    UpdateRefinementCommand,
    UpdateRefinementUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.models.schemas import (
    RefinementCreate,
    RefinementHistoryResponse,
    RefinementKnowledgeCreate,
    RefinementKnowledgeResponse,
    RefinementKnowledgeSummary,
    RefinementMove,
    RefinementQAAnswer,
    RefinementQACreate,
    RefinementQAResponse,
    RefinementResponse,
    RefinementSnapshotResponse,
    RefinementSnapshotSummary,
    RefinementSummary,
    RefinementUpdate,
    SpecResponse,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.application.errors import (
    CancellationReasonRequiredError,
    QASelfAnsweringNotAllowedError,
)

router = APIRouter()


_NOT_FOUND_DETAIL = {
    "refinement_ideation_owner": "Ideation not found or board not owned by user",
    "ideation": "Ideation not found",
    "refinement": "Refinement not found",
    "refinement_qa": "Q&A item not found",
    "refinement_knowledge": "Knowledge base item not found",
}


def _not_found(exc: EntityNotFoundError) -> str:
    """Map the typed ``EntityNotFoundError`` back to the exact legacy 404 detail."""
    return _NOT_FOUND_DETAIL.get(exc.entity_type, "Not found")


@router.post(
    "/ideations/{ideation_id}/refinements",
    response_model=RefinementResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_refinement(
    ideation_id: str,
    data: RefinementCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new refinement for a done ideation."""
    try:
        result = await CreateRefinementUseCase().execute(
            CreateRefinementCommand(ideation_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.refinement


@router.get("/ideations/{ideation_id}/refinements", response_model=list[RefinementSummary])
async def list_refinements(
    ideation_id: str,
    status_filter: str | None = Query(None, alias="status"),
    include_archived: bool = Query(False, alias="include_archived"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List refinements for an ideation, optionally filtered by status."""
    try:
        result = await ListRefinementsUseCase().execute(
            ListRefinementsCommand(
                ideation_id,
                status_filter=status_filter,
                include_archived=include_archived,
            ),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.refinements


@router.get("/refinements/{refinement_id}", response_model=RefinementResponse)
async def get_refinement(
    refinement_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a refinement by ID with nested data."""
    try:
        result = await GetRefinementUseCase().execute(
            GetRefinementCommand(refinement_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.refinement


@router.patch("/refinements/{refinement_id}", response_model=RefinementResponse)
async def update_refinement(
    refinement_id: str,
    data: RefinementUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a refinement. Bumps version when content fields change."""
    try:
        result = await UpdateRefinementUseCase().execute(
            UpdateRefinementCommand(refinement_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.refinement


@router.post("/refinements/{refinement_id}/move", response_model=RefinementResponse)
async def move_refinement(
    refinement_id: str,
    data: RefinementMove,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Change refinement status."""
    try:
        result = await MoveRefinementUseCase().execute(
            MoveRefinementCommand(refinement_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except CancellationReasonRequiredError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.to_dict())
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.refinement


@router.delete("/refinements/{refinement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_refinement(
    refinement_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a refinement."""
    try:
        await DeleteRefinementUseCase().execute(
            DeleteRefinementCommand(refinement_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))


@router.post("/refinements/{refinement_id}/derive-spec", response_model=SpecResponse)
async def derive_spec(
    refinement_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Derive a spec from a refinement."""
    try:
        result = await DeriveSpecFromRefinementUseCase().execute(
            DeriveSpecFromRefinementCommand(refinement_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.spec


@router.get("/refinements/{refinement_id}/history", response_model=list[RefinementHistoryResponse])
async def list_refinement_history(
    refinement_id: str,
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get detailed change history for a refinement."""
    try:
        result = await ListRefinementHistoryUseCase().execute(
            ListRefinementHistoryCommand(refinement_id, limit=limit),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc)
        )
    return result.history


# ==================== REFINEMENT Q&A ====================


@router.get("/refinements/{refinement_id}/qa", response_model=list[RefinementQAResponse])
async def list_refinement_qa(
    refinement_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all Q&A items for a refinement."""
    try:
        result = await ListRefinementQAUseCase().execute(
            ListRefinementQACommand(refinement_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc)
        )
    return result.items


@router.post("/refinements/{refinement_id}/qa", response_model=RefinementQAResponse, status_code=status.HTTP_201_CREATED)
async def create_refinement_question(
    refinement_id: str,
    data: RefinementQACreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Ask a question on a refinement."""
    try:
        result = await CreateRefinementQuestionUseCase().execute(
            CreateRefinementQuestionCommand(refinement_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.qa


@router.post("/refinements/{refinement_id}/qa/{qa_id}/answer", response_model=RefinementQAResponse)
async def answer_refinement_question(
    refinement_id: str,
    qa_id: str,
    data: RefinementQAAnswer,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Answer a refinement Q&A question."""
    try:
        result = await AnswerRefinementQuestionUseCase().execute(
            AnswerRefinementQuestionCommand(refinement_id, qa_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except QASelfAnsweringNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"reason": exc.reason, "message": str(exc)},
        ) from exc
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.qa


@router.delete("/refinements/{refinement_id}/qa/{qa_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_refinement_question(
    refinement_id: str,
    qa_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a refinement Q&A item."""
    try:
        await DeleteRefinementQuestionUseCase().execute(
            DeleteRefinementQuestionCommand(refinement_id, qa_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))


# ==================== REFINEMENT SNAPSHOTS ====================


@router.get("/refinements/{refinement_id}/snapshots", response_model=list[RefinementSnapshotSummary])
async def list_refinement_snapshots(
    refinement_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all version snapshots for a refinement."""
    try:
        result = await ListRefinementSnapshotsUseCase().execute(
            ListRefinementSnapshotsCommand(refinement_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc)
        )
    return result.snapshots


@router.get("/refinements/{refinement_id}/snapshots/{version}", response_model=RefinementSnapshotResponse)
async def get_refinement_snapshot(
    refinement_id: str,
    version: int,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a specific version snapshot of a refinement."""
    try:
        result = await GetRefinementSnapshotUseCase().execute(
            GetRefinementSnapshotCommand(refinement_id, version),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Snapshot v{version} not found"
        )
    return result.snapshot


# ==================== REFINEMENT KNOWLEDGE BASE ====================


@router.get("/refinements/{refinement_id}/knowledge", response_model=list[RefinementKnowledgeSummary])
async def list_refinement_knowledge(
    refinement_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all knowledge base items for a refinement."""
    try:
        result = await ListRefinementKnowledgeUseCase().execute(
            ListRefinementKnowledgeCommand(refinement_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc)
        )
    return result.items


@router.get("/refinements/{refinement_id}/knowledge/{knowledge_id}", response_model=RefinementKnowledgeResponse)
async def get_refinement_knowledge(
    refinement_id: str,
    knowledge_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a knowledge base item with full content."""
    try:
        result = await GetRefinementKnowledgeUseCase().execute(
            GetRefinementKnowledgeCommand(refinement_id, knowledge_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.knowledge


@router.post(
    "/refinements/{refinement_id}/knowledge",
    response_model=RefinementKnowledgeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_refinement_knowledge(
    refinement_id: str,
    data: RefinementKnowledgeCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a knowledge base item on a refinement."""
    try:
        result = await CreateRefinementKnowledgeUseCase().execute(
            CreateRefinementKnowledgeCommand(refinement_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.knowledge


@router.delete("/refinements/{refinement_id}/knowledge/{knowledge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_refinement_knowledge(
    refinement_id: str,
    knowledge_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a knowledge base item from a refinement."""
    try:
        await DeleteRefinementKnowledgeUseCase().execute(
            DeleteRefinementKnowledgeCommand(refinement_id, knowledge_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
