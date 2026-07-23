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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from okto_pulse.community.api.deps import (
    get_unit_of_work,
    get_unit_of_work_factory,
)
from okto_pulse.community.api.knowledge_propagation import (
    KnowledgePropagationContractError,
    KnowledgePropagationServiceError,
    execute_knowledge_creation_with_one_retry,
    knowledge_propagation_error_response,
    rollback_and_record_knowledge_error,
)
from okto_pulse.community.api.knowledge_governance import (
    KnowledgeGovernanceInvalidMetadata,
    knowledge_governance_error_response,
)
from okto_pulse.community.api.pagination import (
    anchor_scope,
    pagination_requested,
    project_page,
    record_fields,
    resolve_window,
    run_paginated_list,
    validate_pagination_query,
)
from okto_pulse.community.api.refinements_pagination import (
    refinement_board_page_request,
    validate_board_refinement_query,
)
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    PageRequest,
)
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
    ListBoardRefinementsCommand,
    ListBoardRefinementsUseCase,
    ListRefinementsCommand,
    ListRefinementsUseCase,
    MoveRefinementCommand,
    MoveRefinementUseCase,
    UpdateRefinementCommand,
    UpdateRefinementUseCase,
)
from okto_pulse.core.application.knowledge_propagation_projection import (
    project_derive_spec_response,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.models.schemas import (
    BoardRefinementPageItem,
    PageEnvelope,
    RefinementCreate,
    RefinementHistoryResponse,
    RefinementKnowledgeCreate,
    RefinementKnowledgeResponse,
    RefinementKnowledgeSummary,
    RefinementMove,
    RefinementPageItem,
    RefinementQAAnswer,
    RefinementQACreate,
    RefinementQAResponse,
    RefinementResponse,
    RefinementSnapshotResponse,
    RefinementSnapshotSummary,
    RefinementSummary,
    RefinementUpdate,
    DeriveSpecResponse,
)
from okto_pulse.core.models.knowledge_propagation import (
    DeriveSpecKnowledgeRequest,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgePropagationPortError,
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


@router.get(
    "/ideations/{ideation_id}/refinements",
    response_model=list[RefinementSummary] | PageEnvelope[RefinementPageItem],
    dependencies=[Depends(validate_pagination_query)],
)
async def list_refinements(
    ideation_id: str,
    status_filter: str | None = Query(None, alias="status"),
    include_archived: bool = Query(False, alias="include_archived"),
    offset: int | None = Query(None),
    limit: int | None = Query(None),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List refinements for an ideation, optionally filtered by status.

    With ``offset``/``limit``: paginated envelope (spec 8b33f9a8; the
    ideation id is the surface's identity anchor); without: legacy shape
    unchanged (DR9).
    """
    if pagination_requested(offset, limit):
        command = ListRefinementsCommand(
            ideation_id,
            status_filter=status_filter,
            include_archived=include_archived,
        )
        actor = RESTAdapterContract.actor(user_id)
        use_case = ListRefinementsUseCase()
        try:
            resolved_offset, resolved_limit = resolve_window(offset, limit)
            filters: tuple[ApplicationFilter, ...] = ()
            if status_filter:
                filters = (ApplicationFilter("status", "eq", status_filter),)
            page = await run_paginated_list(
                uow,
                lambda ideation: PageRequest(
                    surface="refinement_list",
                    scope=(
                        ApplicationFilter("board_id", "eq", ideation.board_id),
                        *anchor_scope(
                            "ideation_id",
                            ideation_id,
                            include_archived=include_archived,
                        ),
                    ),
                    offset=resolved_offset,
                    limit=resolved_limit,
                    filters=filters,
                ),
                preflight=lambda: use_case.preflight(
                    command, actor=actor, uow=uow
                ),
            )
        except EntityNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc)
            )
        return project_page(
            page,
            lambda record: RefinementPageItem(
                **record_fields(
                    record,
                    (
                        "id",
                        "ideation_id",
                        "board_id",
                        "title",
                        "description",
                        "status",
                        "version",
                        "assignee_id",
                        "created_by",
                        "created_at",
                        "updated_at",
                        "labels",
                        "archived",
                    ),
                )
            ),
        )
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


@router.get(
    "/boards/{board_id}/refinements",
    response_model=PageEnvelope[BoardRefinementPageItem],
    dependencies=[Depends(validate_board_refinement_query)],
)
async def list_board_refinements(
    board_id: str,
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(
        None,
        description=(
            "Server-side search across refinement title, description, labels "
            "and parent ideation title."
        ),
    ),
    derivation_pending: bool | None = Query(None),
    include_archived: bool = Query(False),
    labels: str | None = Query(
        None,
        description="CSV labels with ANY semantics and exact membership.",
    ),
    offset: int = Query(0),
    limit: int = Query(25),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List refinements across a board without parent-ideation fan-out."""

    resolved_offset, resolved_limit = resolve_window(offset, limit)
    command = ListBoardRefinementsCommand(board_id)
    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    try:
        page = await run_paginated_list(
            uow,
            refinement_board_page_request(
                board_id,
                status_value=status_filter,
                search=search,
                derivation_pending=derivation_pending,
                include_archived=include_archived,
                labels=labels,
                offset=resolved_offset,
                limit=resolved_limit,
            ),
            preflight=lambda: ListBoardRefinementsUseCase().preflight(
                command,
                actor=actor,
                uow=uow,
            ),
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "board_not_found"},
        )
    return project_page(
        page,
        lambda record: BoardRefinementPageItem(**record.values),
    )


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


@router.post(
    "/refinements/{refinement_id}/derive-spec",
    response_model=DeriveSpecResponse,
)
async def derive_spec(
    refinement_id: str,
    request: Request,
    data: DeriveSpecKnowledgeRequest | None = None,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Derive a spec, preserving the complete v1 path without a v2 body."""
    body_reader = getattr(request, "body", None)
    if data is None and callable(body_reader):
        raw_body = await body_reader()
        if isinstance(raw_body, bytes) and raw_body.strip() == b"null":
            return knowledge_propagation_error_response(
                KnowledgePropagationContractError(
                    "knowledge_propagation_envelope_required",
                    (
                        "the request body must be a complete v2 envelope "
                        "when a JSON body is present"
                    ),
                )
            )
    if data is not None and getattr(data, "kb_ids", None) is not None:
        return knowledge_propagation_error_response(
            KnowledgePropagationServiceError(
                "conflicting_propagation_parameters",
                "legacy kb_ids and knowledge_propagation v2 are mutually exclusive",
            )
        )
    actor = RESTAdapterContract.actor(user_id)
    command = DeriveSpecFromRefinementCommand(
        refinement_id,
        data.knowledge_propagation if data is not None else None,
    )

    async def _execute(target_uow: PulseUnitOfWork):
        return await DeriveSpecFromRefinementUseCase().execute(
            command,
            actor=actor,
            uow=target_uow,
        )

    try:
        if data is None:
            result = await _execute(uow)
            return result.spec
        result = await execute_knowledge_creation_with_one_retry(
            uow=uow,
            uow_factory=get_unit_of_work_factory(request),
            actor=actor,
            operation=_execute,
        )
        return project_derive_spec_response(result.knowledge_mutation)
    except KnowledgePropagationServiceError as exc:
        await rollback_and_record_knowledge_error(uow, exc)
        return knowledge_propagation_error_response(exc)
    except (KnowledgePropagationContractError, KnowledgePropagationPortError) as exc:
        await uow.rollback()
        return knowledge_propagation_error_response(exc)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_not_found(exc))


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
    except KnowledgeGovernanceInvalidMetadata as exc:
        return knowledge_governance_error_response(exc)
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
