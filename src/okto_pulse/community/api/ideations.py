"""Ideation API endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.knowledge_governance import (
    KnowledgeGovernanceInvalidMetadata,
    knowledge_governance_error_response,
)
from okto_pulse.community.api.lookups import (
    lookup_page_request,
    lookup_response,
    validate_ideation_lookup_query,
)
from okto_pulse.community.api.pagination import (
    board_scope,
    pagination_requested,
    project_page,
    record_fields,
    resolve_window,
    run_paginated_list,
    search_groups,
    validate_ideation_pagination_query,
)
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    PageRequest,
)
from okto_pulse.core.application.use_cases import (
    AnswerIdeationQuestionCommand,
    AnswerIdeationQuestionUseCase,
    CreateIdeationCommand,
    CreateIdeationKnowledgeCommand,
    CreateIdeationKnowledgeUseCase,
    CreateIdeationQuestionCommand,
    CreateIdeationQuestionUseCase,
    CreateIdeationUseCase,
    DeleteIdeationCommand,
    DeleteIdeationKnowledgeCommand,
    DeleteIdeationKnowledgeUseCase,
    DeleteIdeationQuestionCommand,
    DeleteIdeationQuestionUseCase,
    DeleteIdeationUseCase,
    DeriveSpecCommand,
    DeriveSpecUseCase,
    EntityNotFoundError,
    EvaluateComplexityCommand,
    EvaluateComplexityUseCase,
    GetIdeationCommand,
    GetIdeationKnowledgeCommand,
    GetIdeationKnowledgeUseCase,
    GetIdeationSnapshotCommand,
    GetIdeationSnapshotUseCase,
    GetIdeationUseCase,
    ListIdeationHistoryCommand,
    ListIdeationHistoryUseCase,
    ListIdeationKnowledgeCommand,
    ListIdeationKnowledgeUseCase,
    ListIdeationQACommand,
    ListIdeationQAUseCase,
    ListIdeationSnapshotsCommand,
    ListIdeationSnapshotsUseCase,
    ListIdeationsCommand,
    ListIdeationsUseCase,
    MoveIdeationCommand,
    MoveIdeationUseCase,
    SetIdeationAmbiguityGateSkipCommand,
    SetIdeationAmbiguityGateSkipUseCase,
    UpdateIdeationCommand,
    UpdateIdeationUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.models.schemas import (
    IdeationAmbiguityGateSkipUpdate,
    IdeationCreate,
    IdeationHistoryResponse,
    IdeationKnowledgeCreate,
    IdeationKnowledgeResponse,
    IdeationKnowledgeSummary,
    IdeationMove,
    IdeationPageItem,
    IdeationQAAnswer,
    IdeationQACreate,
    IdeationQAResponse,
    IdeationResponse,
    IdeationSnapshotResponse,
    IdeationSnapshotSummary,
    IdeationSummary,
    IdeationUpdate,
    LookupResponse,
    PageEnvelope,
    SpecResponse,
)
from okto_pulse.core.application.errors import (
    AmbiguityGateError,
    CancellationReasonRequiredError,
    QASelfAnsweringNotAllowedError,
)
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX

router = APIRouter()


@router.post(
    "/boards/{board_id}/ideations",
    response_model=IdeationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_ideation(
    board_id: str,
    data: IdeationCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new ideation in a board."""
    try:
        result = await CreateIdeationUseCase().execute(
            CreateIdeationCommand(board_id, data),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found or not owned by user",
        )
    return result.ideation


@router.get(
    "/boards/{board_id}/ideations",
    response_model=list[IdeationSummary] | PageEnvelope[IdeationPageItem],
    dependencies=[Depends(validate_ideation_pagination_query)],
)
async def list_ideations(
    board_id: str,
    status_filter: str | None = Query(None, alias="status"),
    search: str | None = Query(None),
    derivation_pending: bool | None = Query(None),
    include_archived: bool = Query(False, alias="include_archived"),
    offset: int | None = Query(None),
    limit: int | None = Query(None),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List ideations for a board, optionally filtered by status.

    With ``offset``/``limit``: paginated envelope (spec 8b33f9a8); without:
    legacy shape unchanged (DR9).
    """
    if pagination_requested(offset, limit):
        command = ListIdeationsCommand(
            board_id,
            status_filter=status_filter,
            include_archived=include_archived,
        )
        actor = RESTAdapterContract.actor(user_id, board_id=board_id)
        use_case = ListIdeationsUseCase()
        try:
            resolved_offset, resolved_limit = resolve_window(offset, limit)
            filters: tuple[ApplicationFilter, ...] = ()
            if status_filter:
                filters = (ApplicationFilter("status", "eq", status_filter),)
            if derivation_pending is not None:
                filters = (
                    *filters,
                    ApplicationFilter(
                        "derivation_pending",
                        "is_true" if derivation_pending else "is_false",
                        None,
                    ),
                )
            page = await run_paginated_list(
                uow,
                PageRequest(
                    surface="ideation_list",
                    scope=board_scope(board_id, include_archived=include_archived),
                    offset=resolved_offset,
                    limit=resolved_limit,
                    filters=filters,
                    any_groups=search_groups(search, ("title", "description")),
                ),
                preflight=lambda: use_case.preflight(
                    command, actor=actor, uow=uow
                ),
            )
        except EntityNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Board not found"
            )
        return project_page(
            page,
            lambda record: IdeationPageItem(
                **record_fields(
                    record,
                    (
                        "id",
                        "board_id",
                        "title",
                        "description",
                        "problem_statement",
                        "complexity",
                        "status",
                        "version",
                        "assignee_id",
                        "created_by",
                        "created_at",
                        "updated_at",
                        "labels",
                        "archived",
                        "scope_assessment",
                    ),
                )
            ),
        )
    try:
        result = await ListIdeationsUseCase().execute(
            ListIdeationsCommand(
                board_id, status_filter=status_filter, include_archived=include_archived
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    return result.ideations


@router.get(
    "/boards/{board_id}/ideations/lookup",
    response_model=LookupResponse,
    dependencies=[Depends(validate_ideation_lookup_query)],
)
async def lookup_ideations(
    board_id: str,
    search: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(20),
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return a compact, purpose-filtered ideation typeahead page."""

    command = ListIdeationsCommand(
        board_id,
        status_filter=None,
        include_archived=False,
    )
    actor = RESTAdapterContract.actor(user_id, board_id=board_id)
    try:
        page = await run_paginated_list(
            uow,
            lookup_page_request(
                "ideation_lookup",
                board_id,
                statuses=status_filter,
                search=search,
                offset=offset,
                limit=limit,
            ),
            preflight=lambda: ListIdeationsUseCase().preflight(
                command,
                actor=actor,
                uow=uow,
            ),
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found",
        )
    return lookup_response(page)


@router.get("/ideations/{ideation_id}", response_model=IdeationResponse)
async def get_ideation(
    ideation_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get an ideation by ID with nested data."""
    try:
        result = await GetIdeationUseCase().execute(
            GetIdeationCommand(ideation_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found")
    return result.ideation


@router.patch("/ideations/{ideation_id}", response_model=IdeationResponse)
async def update_ideation(
    ideation_id: str,
    data: IdeationUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update an ideation. Bumps version when content fields change."""
    try:
        result = await UpdateIdeationUseCase().execute(
            UpdateIdeationCommand(ideation_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found")
    return result.ideation


@router.post("/ideations/{ideation_id}/move", response_model=IdeationResponse)
async def move_ideation(
    ideation_id: str,
    data: IdeationMove,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Change ideation status."""
    # Thin REST adapter (spec #09): delegate to the transport-free use case.
    # AmbiguityGateError → 400 and a missing ideation → 404 are preserved exactly;
    # any other error propagates unchanged (legacy behavior).
    try:
        result = await MoveIdeationUseCase().execute(
            MoveIdeationCommand(ideation_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except CancellationReasonRequiredError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=e.to_dict()) from e
    except (AmbiguityGateError, EntityNotFoundError) as e:
        raise RESTAdapterContract.http_error(e, not_found_detail="Ideation not found") from e
    return result.ideation


@router.patch("/ideations/{ideation_id}/ambiguity-gate-skip", response_model=IdeationResponse)
async def set_ideation_ambiguity_gate_skip(
    ideation_id: str,
    data: IdeationAmbiguityGateSkipUpdate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Persist the per-ideation Max ambiguity gate skip override (spec 2485780b).

    Dedicated write path that works while the ideation is in evaluating status
    without opening the generic update_ideation draft-only guard to other
    fields. Rejects archived ideations and emits an auditable activity entry.
    """
    try:
        result = await SetIdeationAmbiguityGateSkipUseCase().execute(
            SetIdeationAmbiguityGateSkipCommand(ideation_id, data.skip_ambiguity_gate),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found")
    return result.ideation


@router.delete("/ideations/{ideation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ideation(
    ideation_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete an ideation."""
    try:
        await DeleteIdeationUseCase().execute(
            DeleteIdeationCommand(ideation_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found")


@router.post("/ideations/{ideation_id}/evaluate", response_model=IdeationResponse)
async def evaluate_complexity(
    ideation_id: str,
    request: Request,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Evaluate ideation complexity. Accepts scores + justifications in body."""
    body = await request.json()
    try:
        result = await EvaluateComplexityUseCase().execute(
            EvaluateComplexityCommand(ideation_id, body),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found")
    return result.ideation


@router.post("/ideations/{ideation_id}/derive-spec", response_model=SpecResponse)
async def derive_spec(
    ideation_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a spec draft from a done ideation."""
    try:
        result = await DeriveSpecUseCase().execute(
            DeriveSpecCommand(ideation_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found")
    return result.spec


@router.get("/ideations/{ideation_id}/snapshots", response_model=list[IdeationSnapshotSummary])
async def list_ideation_snapshots(
    ideation_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all version snapshots for an ideation."""
    try:
        result = await ListIdeationSnapshotsUseCase().execute(
            ListIdeationSnapshotsCommand(ideation_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found"
        )
    return result.snapshots


@router.get("/ideations/{ideation_id}/snapshots/{version}", response_model=IdeationSnapshotResponse)
async def get_ideation_snapshot(
    ideation_id: str,
    version: int,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a specific version snapshot of an ideation."""
    try:
        result = await GetIdeationSnapshotUseCase().execute(
            GetIdeationSnapshotCommand(ideation_id, version),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Snapshot v{version} not found"
        )
    return result.snapshot


@router.get("/ideations/{ideation_id}/history", response_model=list[IdeationHistoryResponse])
async def list_ideation_history(
    ideation_id: str,
    limit: int = Query(50, ge=1, le=200),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get detailed change history for an ideation."""
    try:
        result = await ListIdeationHistoryUseCase().execute(
            ListIdeationHistoryCommand(ideation_id, limit=limit),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found"
        )
    return result.history


# ==================== IDEATION KNOWLEDGE BASE ====================


@router.get("/ideations/{ideation_id}/knowledge", response_model=list[IdeationKnowledgeSummary])
async def list_ideation_knowledge(
    ideation_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all knowledge base items for an ideation."""
    try:
        result = await ListIdeationKnowledgeUseCase().execute(
            ListIdeationKnowledgeCommand(ideation_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found"
        )
    return result.items


@router.get("/ideations/{ideation_id}/knowledge/{knowledge_id}", response_model=IdeationKnowledgeResponse)
async def get_ideation_knowledge(
    ideation_id: str,
    knowledge_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a knowledge base item with full content."""
    try:
        result = await GetIdeationKnowledgeUseCase().execute(
            GetIdeationKnowledgeCommand(ideation_id, knowledge_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base item not found")
    return result.knowledge


@router.post(
    "/ideations/{ideation_id}/knowledge",
    response_model=IdeationKnowledgeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_ideation_knowledge(
    ideation_id: str,
    data: IdeationKnowledgeCreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a knowledge base item on an ideation."""
    try:
        result = await CreateIdeationKnowledgeUseCase().execute(
            CreateIdeationKnowledgeCommand(ideation_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found")
    except KnowledgeGovernanceInvalidMetadata as exc:
        return knowledge_governance_error_response(exc)
    return result.knowledge


@router.delete("/ideations/{ideation_id}/knowledge/{knowledge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ideation_knowledge(
    ideation_id: str,
    knowledge_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a knowledge base item from an ideation."""
    try:
        await DeleteIdeationKnowledgeUseCase().execute(
            DeleteIdeationKnowledgeCommand(ideation_id, knowledge_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Knowledge base item not found")


# ==================== IDEATION Q&A ====================


@router.get("/ideations/{ideation_id}/qa", response_model=list[IdeationQAResponse])
async def list_ideation_qa(
    ideation_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all Q&A items for an ideation."""
    try:
        result = await ListIdeationQAUseCase().execute(
            ListIdeationQACommand(ideation_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found"
        )
    return result.items


@router.post("/ideations/{ideation_id}/qa", response_model=IdeationQAResponse, status_code=status.HTTP_201_CREATED)
async def create_ideation_question(
    ideation_id: str,
    data: IdeationQACreate,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Ask a question on an ideation."""
    try:
        result = await CreateIdeationQuestionUseCase().execute(
            CreateIdeationQuestionCommand(ideation_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ideation not found")
    return result.qa


@router.post("/ideations/{ideation_id}/qa/{qa_id}/answer", response_model=IdeationQAResponse)
async def answer_ideation_question(
    ideation_id: str,
    qa_id: str,
    data: IdeationQAAnswer,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Answer an ideation Q&A question."""
    try:
        result = await AnswerIdeationQuestionUseCase().execute(
            AnswerIdeationQuestionCommand(ideation_id, qa_id, data),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except QASelfAnsweringNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"reason": exc.reason, "message": str(exc)},
        ) from exc
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Q&A item not found")
    return result.qa


@router.delete("/ideations/{ideation_id}/qa/{qa_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ideation_question(
    ideation_id: str,
    qa_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete an ideation Q&A item."""
    try:
        await DeleteIdeationQuestionUseCase().execute(
            DeleteIdeationQuestionCommand(ideation_id, qa_id),
            actor=RESTAdapterContract.actor(user_id),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Q&A item not found")
