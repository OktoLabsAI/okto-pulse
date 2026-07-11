"""Stories and Topics API endpoints.

Spec R01A REST-FU6-S2: every endpoint here now routes through a transport-free
use case (``application/use_cases/stories_crud.py``) over a
``PulseUnitOfWork`` — no endpoint binds ``get_db`` / a raw ``AsyncSession`` /
``select`` anymore. This module is a thin inbound adapter: it builds the
command/actor, maps the typed use-case errors back to the EXACT legacy HTTP
status + detail (``EntityNotFoundError`` → the per-entity 404 string,
``PermissionDeniedError`` → 403 with the ``json``-decoded detail,
``TopicOperationError`` → 400/409, ``ValueError`` → 400), and shapes the
response payloads (topic summaries, the merge envelope, the ideation payload).

The three topic-by-id endpoints (update / delete / merge) resolve the topic's
``board_id`` (+ pre-mutation ``archived``) inside their use case via the
``StoryService.get_topic`` reader (spec R01A REST-FU6-S2 rework), so the adapter
no longer issues a ``db.get(Topic, …)`` itself — it carries zero direct ORM.
"""

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.stories_crud import (
    ArchiveStoryCommand,
    ArchiveStoryUseCase,
    ConvertStoriesCommand,
    ConvertStoriesUseCase,
    CreateStoryCommand,
    CreateStoryUseCase,
    CreateTopicCommand,
    CreateTopicUseCase,
    DeleteTopicCommand,
    DeleteTopicUseCase,
    GetStoryCommand,
    GetStoryUseCase,
    LinkStoriesToIdeationCommand,
    LinkStoriesToIdeationUseCase,
    LinkStoryToIdeationCommand,
    LinkStoryToIdeationUseCase,
    ListStoriesCommand,
    ListStoriesUseCase,
    ListTopicsCommand,
    ListTopicsUseCase,
    MergeTopicsCommand,
    MergeTopicsUseCase,
    MoveStoryCommand,
    MoveStoryUseCase,
    UpdateStoryCommand,
    UpdateStoryUseCase,
    UpdateTopicCommand,
    UpdateTopicUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.models.schemas import (
    StoryConversionRequest,
    StoryConversionResponse,
    StoryCreate,
    StoryLinkCreate,
    StoryMove,
    StoryResponse,
    StorySummary,
    StoryUpdate,
    TopicCreate,
    TopicDeleteResponse,
    TopicMergeRequest,
    TopicMergeResponse,
    TopicSummary,
    TopicUpdate,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.application.errors import (
    InvalidTopicMergeError,
    TopicNameConflictError,
    TopicNotEmptyError,
    TopicOperationError,
)

router = APIRouter()


class IdeationStoriesLinkRequest(BaseModel):
    """Request body for linking one or more Stories to an Ideation."""

    story_ids: list[str] = Field(..., min_length=1)


_NOT_FOUND_DETAIL = {
    "board": "Board not found",
    "topic": "Topic not found",
    "story": "Story not found",
    "story_or_ideation": "Story or Ideation not found",
}


def _not_found(exc: EntityNotFoundError) -> str:
    """Map the typed ``EntityNotFoundError`` back to the exact legacy 404 detail."""
    return _NOT_FOUND_DETAIL.get(exc.entity_type, "Not found")


def _permission_detail(message: str):
    try:
        return json.loads(message)
    except json.JSONDecodeError:
        return message


def _raise_permission_denied(message: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=_permission_detail(message),
    )


def _topic_operation_detail(exc: TopicOperationError) -> dict:
    return {"code": exc.code, "detail": str(exc), **exc.details}


def _raise_topic_operation_error(exc: TopicOperationError) -> None:
    status_code = status.HTTP_400_BAD_REQUEST
    if isinstance(exc, (TopicNameConflictError, TopicNotEmptyError)):
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(exc, InvalidTopicMergeError):
        status_code = status.HTTP_400_BAD_REQUEST
    raise HTTPException(status_code=status_code, detail=_topic_operation_detail(exc)) from exc


def _ideation_payload(ideation) -> dict:
    return {
        "id": ideation.id,
        "board_id": ideation.board_id,
        "title": ideation.title,
        "description": ideation.description,
        "problem_statement": ideation.problem_statement,
        "complexity": ideation.complexity.value if ideation.complexity else None,
        "status": ideation.status.value,
        "version": ideation.version,
        "assignee_id": ideation.assignee_id,
        "created_by": ideation.created_by,
        "created_at": ideation.created_at.isoformat() if ideation.created_at else None,
        "updated_at": ideation.updated_at.isoformat() if ideation.updated_at else None,
        "labels": ideation.labels,
        "archived": getattr(ideation, "archived", False),
        "pre_archive_status": getattr(ideation, "pre_archive_status", None),
    }


def _topic_summary_payload(topic) -> TopicSummary:
    return TopicSummary.model_validate(topic)


@router.post("/boards/{board_id}/topics", response_model=TopicSummary, status_code=status.HTTP_201_CREATED)
async def create_topic(
    board_id: str,
    data: TopicCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a board-scoped Story Topic."""
    try:
        result = await CreateTopicUseCase().execute(
            CreateTopicCommand(board_id, data),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except TopicOperationError as exc:
        _raise_topic_operation_error(exc)
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return _topic_summary_payload(result.topic)


@router.get("/boards/{board_id}/topics", response_model=list[TopicSummary])
async def list_topics(
    board_id: str,
    include_archived: bool = Query(False, alias="include_archived"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List Story Topics for a board."""
    try:
        result = await ListTopicsUseCase().execute(
            ListTopicsCommand(board_id, include_archived=include_archived),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return [_topic_summary_payload(topic) for topic in result.topics]


@router.patch("/topics/{topic_id}", response_model=TopicSummary)
async def update_topic(
    topic_id: str,
    data: TopicUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a Story Topic."""
    try:
        result = await UpdateTopicUseCase().execute(
            UpdateTopicCommand(topic_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except TopicOperationError as exc:
        _raise_topic_operation_error(exc)
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return _topic_summary_payload(result.topic)


@router.delete("/topics/{topic_id}", response_model=TopicDeleteResponse)
async def delete_topic(
    topic_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a Story Topic only when it has no associated Stories."""
    try:
        await DeleteTopicUseCase().execute(
            DeleteTopicCommand(topic_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except TopicOperationError as exc:
        _raise_topic_operation_error(exc)
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return TopicDeleteResponse(success=True, deleted_topic_id=topic_id)


@router.post("/topics/{source_topic_id}/merge", response_model=TopicMergeResponse)
async def merge_topics(
    source_topic_id: str,
    data: TopicMergeRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Merge a source Topic into an active target Topic."""
    try:
        result = await MergeTopicsUseCase().execute(
            MergeTopicsCommand(source_topic_id, data.target_topic_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except TopicOperationError as exc:
        _raise_topic_operation_error(exc)
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    merged = result.result
    return {
        **merged,
        "source": _topic_summary_payload(merged["source"]),
        "target": _topic_summary_payload(merged["target"]),
    }


@router.post("/boards/{board_id}/stories", response_model=StoryResponse, status_code=status.HTTP_201_CREATED)
async def create_story(
    board_id: str,
    data: StoryCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a lightweight Story before Ideation."""
    try:
        result = await CreateStoryUseCase().execute(
            CreateStoryCommand(board_id, data),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result.story


@router.get("/boards/{board_id}/stories", response_model=list[StorySummary])
async def list_stories(
    board_id: str,
    status_filter: str | None = Query(None, alias="status"),
    topic_id: str | None = Query(None),
    search: str | None = Query(None),
    linked: bool | None = Query(None),
    converted: bool | None = Query(None),
    include_archived: bool = Query(False, alias="include_archived"),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List Stories for a board."""
    try:
        result = await ListStoriesUseCase().execute(
            ListStoriesCommand(
                board_id,
                status_filter=status_filter,
                topic_id=topic_id,
                search=search,
                linked=linked,
                converted=converted,
                include_archived=include_archived,
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result.stories


@router.get("/stories/{story_id}", response_model=StoryResponse)
async def get_story(
    story_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a Story with Topic and Ideation links."""
    try:
        result = await GetStoryUseCase().execute(
            GetStoryCommand(story_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.story


@router.patch("/stories/{story_id}", response_model=StoryResponse)
async def update_story(
    story_id: str,
    data: StoryUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a Story."""
    try:
        result = await UpdateStoryUseCase().execute(
            UpdateStoryCommand(story_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result.story


@router.post("/stories/{story_id}/move", response_model=StoryResponse)
async def move_story(
    story_id: str,
    data: StoryMove,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Change Story lifecycle status."""
    try:
        result = await MoveStoryUseCase().execute(
            MoveStoryCommand(story_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return result.story


@router.delete("/stories/{story_id}", response_model=StoryResponse)
async def archive_story(
    story_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Archive a Story without deleting historical lineage."""
    try:
        result = await ArchiveStoryUseCase().execute(
            ArchiveStoryCommand(story_id, archived=True),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.story


@router.post("/stories/{story_id}/restore", response_model=StoryResponse)
async def restore_story(
    story_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Restore an archived Story."""
    try:
        result = await ArchiveStoryUseCase().execute(
            ArchiveStoryCommand(story_id, archived=False),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    return result.story


@router.post("/stories/{story_id}/ideations", response_model=StoryResponse)
async def link_story_to_ideation(
    story_id: str,
    data: StoryLinkCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Link a Story to an existing Ideation."""
    try:
        result = await LinkStoryToIdeationUseCase().execute(
            LinkStoryToIdeationCommand(story_id, data.ideation_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return result.story


@router.post("/ideations/{ideation_id}/stories")
async def link_stories_to_ideation(
    ideation_id: str,
    data: IdeationStoriesLinkRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Contract-compatible endpoint for linking Stories to an existing Ideation."""
    try:
        result = await LinkStoriesToIdeationUseCase().execute(
            LinkStoriesToIdeationCommand(ideation_id, data.story_ids),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"success": True, "ideation_id": result.ideation_id, "story_ids": result.story_ids}


@router.post("/boards/{board_id}/stories/convert-to-ideation", response_model=StoryConversionResponse)
@router.post("/boards/{board_id}/stories/convert", response_model=StoryConversionResponse)
async def convert_stories(
    board_id: str,
    data: StoryConversionRequest,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create or link an Ideation from selected Stories."""
    try:
        result = await ConvertStoriesUseCase().execute(
            ConvertStoriesCommand(board_id, data),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        _raise_permission_denied(exc.message)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "success": True,
        "ideation": _ideation_payload(result.ideation),
        "links": result.links,
        "propagated_mockups": result.propagated_mockups,
    }
