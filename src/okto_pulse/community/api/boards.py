"""Board API endpoints."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    statement_budget,
)
from okto_pulse.community.api.columns_pagination import (
    KANBAN_STATUSES,
    assignee_facet_request,
    batch_type_facet_request,
    card_summary,
    column_page_request,
    column_type_facet_request,
    page_meta,
    parse_columns_parameters,
)
from okto_pulse.community.api.cards_pagination import (
    card_page_request,
    validate_card_list_query,
)
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
from okto_pulse.community.api.permission_errors import (
    permission_denied_http_error,
)
from okto_pulse.community.api.spec_dependency_errors import (
    spec_dependency_http_error,
)
from okto_pulse.community.api.pagination import (
    project_page,
    resolve_window,
    run_paginated_list,
)
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
from okto_pulse.core.application.knowledge_propagation_projection import (
    project_card_create_response,
)
from okto_pulse.core.domain.enums import CardStatus
from okto_pulse.core.domain.spec_dependency import SpecDependencyOperationError
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.core.models import (
    BoardCreate,
    BoardResponse,
    BoardShareCreate,
    BoardShareResponse,
    BoardShareUpdate,
    BoardSummary,
    BoardUpdate,
    CardCreate,
    CardCreateResponse,
    CardPageItem,
    ColumnsResponseUnion,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgePropagationPortError,
)
from okto_pulse.core.models.schemas import PageEnvelope
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.application.errors import CardOperationError
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX
from okto_pulse.core.ports.authentication import Principal

router = APIRouter()


@router.post("", response_model=BoardResponse, status_code=status.HTTP_201_CREATED)
async def create_board(
    data: BoardCreate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a new board."""
    # Spec #04: the migrated REST flow obtains a request-scoped PulseUnitOfWork
    # (from the persistence port, bound to the request session via get_db) and
    # calls the transport-free use case — no raw AsyncSession in the handler's
    # contract with the use case. Behavior (payload/201/commit/re-fetch/effective
    # settings/realm_id) is preserved, and the get_db dependency override still
    # applies because get_unit_of_work depends on it.
    try:
        result = await CreateBoardUseCase().execute(
            CreateBoardCommand(data),
            actor=RESTAdapterContract.actor_from_principal(principal),
            uow=uow,
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    return result.board


@router.get("", response_model=list[BoardSummary])
async def list_boards(
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(20, ge=1, le=100),
    view: Literal["my", "shared", "all"] = Query("my"),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List boards for the current user. view: my|shared|all."""
    result = await ListBoardsUseCase().execute(
        ListBoardsCommand(offset=offset, limit=limit, view=view),
        actor=RESTAdapterContract.actor_from_principal(principal),
        uow=uow,
    )
    return result.boards


@router.get("/{board_id}", response_model=BoardResponse)
async def get_board(
    board_id: str,
    compact: bool = Query(
        False,
        description="When true, omit inline cards/agents and return only the overview envelope with counts. Default false preserves the legacy full payload for the existing frontend.",
    ),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get a board by ID. By default returns the full board (legacy shape);
    pass `compact=true` for the overview envelope (Ideação token-optimization
    Story 2 — ~200B vs ~10KB).
    """
    try:
        result = await GetBoardUseCase().execute(
            GetBoardCommand(board_id, compact=compact),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Board not found"
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    return result.board


@router.patch("/{board_id}", response_model=BoardResponse)
async def update_board(
    board_id: str,
    data: BoardUpdate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a board."""
    try:
        result = await UpdateBoardUseCase().execute(
            UpdateBoardCommand(board_id, data),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Board not found"
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    return result.board


@router.delete("/{board_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_board(
    board_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Delete a board and all its cards."""
    try:
        await DeleteBoardUseCase().execute(
            DeleteBoardCommand(board_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Board not found"
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc


@router.get(
    "/{board_id}/cards",
    response_model=PageEnvelope[CardPageItem],
    dependencies=[Depends(validate_card_list_query)],
)
async def list_board_cards(
    board_id: str,
    status_filter: str | None = Query(None, alias="status"),
    spec_ids: str | None = Query(
        None,
        description="CSV spec ids; __unlinked__ includes cards without a spec.",
    ),
    sprint_id: str | None = Query(None),
    priority: str | None = Query(None),
    card_types: str | None = Query(
        None,
        description="CSV card types: normal,test,bug.",
    ),
    assignee_id: str | None = Query(None),
    labels: str | None = Query(
        None,
        description="CSV labels with ANY semantics and exact membership.",
    ),
    search: str | None = Query(
        None,
        description="Server-side search across title, description and labels.",
    ),
    include_archived: bool = Query(False),
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(25),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List a board's cards through the lean, fully filtered C7 projection."""

    resolved_offset, resolved_limit = resolve_window(offset, limit)
    request = card_page_request(
        board_id,
        status_value=status_filter,
        spec_ids=spec_ids,
        sprint_id=sprint_id,
        priority=priority,
        card_types=card_types,
        assignee_id=assignee_id,
        labels=labels,
        search=search,
        include_archived=include_archived,
        offset=resolved_offset,
        limit=resolved_limit,
    )
    actor = RESTAdapterContract.actor_from_principal(
        principal,
        board_id=board_id,
    )
    use_case = GetBoardColumnsUseCase()
    try:
        page = await run_paginated_list(
            uow,
            request,
            preflight=lambda: use_case.preflight(
                GetBoardColumnsCommand(board_id),
                actor=actor,
                uow=uow,
            ),
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "board_not_found"},
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    return project_page(
        page,
        lambda record: CardPageItem(**record.values),
    )


@router.post(
    "/{board_id}/cards",
    response_model=CardCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_card(
    board_id: str,
    request: Request,
    data: CardCreate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Create a card, preserving v1 unless propagation v2 is explicit."""
    actor = RESTAdapterContract.actor_from_principal(principal, board_id=board_id)
    command = CreateCardInBoardCommand(board_id, data)

    async def _execute(target_uow: PulseUnitOfWork):
        return await CreateCardInBoardUseCase().execute(
            command,
            actor=actor,
            uow=target_uow,
        )

    try:
        if (
            "knowledge_propagation" in data.model_fields_set
            and data.knowledge_propagation is None
        ):
            return knowledge_propagation_error_response(
                KnowledgePropagationContractError(
                    "knowledge_propagation_envelope_required",
                    (
                        "knowledge_propagation must be a complete v2 envelope "
                        "when the field is present"
                    ),
                )
            )
        if data.knowledge_propagation is None:
            result = await _execute(uow)
            return result.card
        result = await execute_knowledge_creation_with_one_retry(
            uow=uow,
            uow_factory=get_unit_of_work_factory(request),
            actor=actor,
            operation=_execute,
        )
        return project_card_create_response(result.knowledge_mutation)
    except KnowledgePropagationServiceError as exc:
        await rollback_and_record_knowledge_error(uow, exc)
        return knowledge_propagation_error_response(exc)
    except (KnowledgePropagationContractError, KnowledgePropagationPortError) as exc:
        await uow.rollback()
        return knowledge_propagation_error_response(exc)
    except CardOperationError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.to_dict())
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found or not owned by user",
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get(
    "/{board_id}/columns",
    responses={200: {"model": ColumnsResponseUnion}},
    dependencies=[Depends(parse_columns_parameters)],
)
async def get_board_columns(
    board_id: str,
    request: Request,
    _per_column_limit: str | None = Query(
        None,
        alias="per_column_limit",
        description="Opt-in integer window per column (1..100).",
    ),
    _column: str | None = Query(
        None,
        alias="column",
        description="Optional single CardStatus continuation column.",
    ),
    _offset: str | None = Query(
        None,
        alias="offset",
        description="Non-negative offset; requires column.",
    ),
    _spec_ids: str | None = Query(
        None,
        alias="spec_ids",
        description="CSV spec ids; __unlinked__ includes cards without a spec.",
    ),
    _card_types: list[str] | None = Query(
        None,
        alias="card_types",
        description="Repeatable status:normal,test mapping; last status wins.",
    ),
    _search: str | None = Query(None, alias="search"),
    _assignee_id: str | None = Query(None, alias="assignee_id"),
    _include_archived: bool = Query(
        False,
        alias="include_archived",
        description=(
            "Legacy-compatible boolean; opt-in columns accepts strict true|false."
        ),
    ),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Get literal legacy columns or an opt-in bounded columns response."""

    parameters = parse_columns_parameters(request)
    actor = RESTAdapterContract.actor_from_principal(
        principal,
        board_id=board_id,
    )
    use_case = GetBoardColumnsUseCase()

    if parameters is not None:
        try:
            # Authorization happens before the productive 23/4 data budget.
            await use_case.preflight(
                GetBoardColumnsCommand(board_id),
                actor=actor,
                uow=uow,
            )
        except EntityNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "board_not_found"},
            )
        except PermissionDeniedError as exc:
            raise permission_denied_http_error(exc) from exc

        session = uow.services.cards.db
        if parameters.column is not None:
            async with statement_budget(session, 4) as budget:
                page = await uow.services.entity_pages.list(
                    column_page_request(
                        board_id,
                        parameters.column,
                        parameters,
                        offset=parameters.offset,
                    )
                )
                facet_rows = await uow.services.entity_pages.group_count(
                    column_type_facet_request(
                        board_id,
                        parameters.column,
                        parameters,
                    )
                )
                if not 1 <= budget.used <= 4:
                    raise RuntimeError(
                        "columns_statement_budget_mismatch: "
                        f"column mode used {budget.used}, cap 4"
                    )

            card_type_facet = {
                str(getattr(row.values[0], "value", row.values[0])): row.count
                for row in facet_rows
            }
            meta = page_meta(page, card_type_facet)
            return {
                "board_id": board_id,
                "column": parameters.column,
                "items": [card_summary(record) for record in page.items],
                "meta": meta,
                "offset": parameters.offset,
                "limit": parameters.per_column_limit,
                "next_offset": (
                    parameters.offset + len(page.items) if meta["has_more"] else None
                ),
            }

        async with statement_budget(session, 23) as budget:
            columns: dict[str, list[dict]] = {}
            columns_meta: dict[str, dict] = {}
            for card_status in KANBAN_STATUSES:
                page = await uow.services.entity_pages.list(
                    column_page_request(board_id, card_status, parameters)
                )
                columns[card_status] = [card_summary(record) for record in page.items]
                columns_meta[card_status] = page_meta(page)

            type_rows = await uow.services.entity_pages.group_count(
                batch_type_facet_request(board_id, parameters)
            )
            assignee_rows = await uow.services.entity_pages.group_count(
                assignee_facet_request(board_id, parameters)
            )
            if not 1 <= budget.used <= 23:
                raise RuntimeError(
                    "columns_statement_budget_mismatch: "
                    f"batch used {budget.used}, cap 23"
                )

        for row in type_rows:
            card_status = str(getattr(row.values[0], "value", row.values[0]))
            card_type = str(getattr(row.values[1], "value", row.values[1]))
            if card_status in columns_meta:
                columns_meta[card_status]["facets"]["card_type"][card_type] = row.count
        assignee_facet = sorted(
            ({"value": row.values[0], "count": row.count} for row in assignee_rows),
            key=lambda item: (
                item["value"] is not None,
                "" if item["value"] is None else str(item["value"]),
            ),
        )
        return {
            "board_id": board_id,
            "columns": columns,
            "columns_meta": {
                "columns": columns_meta,
                "facets": {"assignee": assignee_facet},
            },
        }

    # Literal legacy path: retain the fully hydrated board and exact wire.
    include_archived = _include_archived
    try:
        result = await use_case.execute(
            GetBoardColumnsCommand(board_id),
            actor=actor,
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found",
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc

    board = result.board
    columns = {card_status.value: [] for card_status in CardStatus}
    for card in board.cards:
        if not include_archived and getattr(card, "archived", False):
            continue
        columns[card.status.value].append(
            {
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
                "card_type": getattr(card, "card_type", "normal") or "normal",
                "origin_task_id": getattr(card, "origin_task_id", None),
                "severity": getattr(card, "severity", None),
                "linked_test_task_ids": getattr(
                    card,
                    "linked_test_task_ids",
                    None,
                ),
                "archived": getattr(card, "archived", False),
                "open_qa_count": sum(
                    1
                    for question in (card.qa_items or [])
                    if question.answered_at is None
                ),
            }
        )

    return {"board_id": board_id, "columns": columns}


# ==================== ARCHIVE ====================


@router.post("/{board_id}/archive/{entity_type}/{entity_id}")
async def archive_tree(
    board_id: str,
    entity_type: str,
    entity_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Archive an entity and all its descendants in cascade."""
    try:
        result = await ArchiveTreeUseCase().execute(
            ArchiveTreeCommand(board_id, entity_type, entity_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type.capitalize()} not found",
        ) from exc
    except SpecDependencyOperationError as exc:
        raise spec_dependency_http_error(exc) from exc
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    return {"success": True, "archived_count": result.counts}


@router.post("/{board_id}/restore/{entity_type}/{entity_id}")
async def restore_tree(
    board_id: str,
    entity_type: str,
    entity_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Restore an archived entity and all its descendants."""
    try:
        result = await RestoreTreeUseCase().execute(
            RestoreTreeCommand(board_id, entity_type, entity_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{exc.entity_type.capitalize()} not found",
        ) from exc
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
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
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Share a board with another user (owner/admin only)."""
    try:
        result = await ShareBoardUseCase().execute(
            ShareBoardCommand(board_id, data),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found",
        ) from exc
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    return result.share


@router.get("/{board_id}/shares", response_model=list[BoardShareResponse])
async def list_board_shares(
    board_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """List all shares for a board."""
    try:
        result = await ListBoardSharesUseCase().execute(
            ListBoardSharesCommand(board_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=uow,
        )
    except EntityNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Board not found"
        )
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    return result.shares


@router.patch("/{board_id}/shares/{share_id}", response_model=BoardShareResponse)
async def update_board_share(
    board_id: str,
    share_id: str,
    data: BoardShareUpdate,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Update a share's permission (owner/admin only)."""
    try:
        result = await UpdateBoardShareUseCase().execute(
            UpdateBoardShareCommand(board_id, share_id, data),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board or share not found",
        ) from exc
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    return result.share


@router.delete("/{board_id}/shares/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_board_share(
    board_id: str,
    share_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Revoke a share (owner/admin can revoke, shared user can leave)."""
    try:
        await RevokeBoardShareUseCase().execute(
            RevokeBoardShareCommand(board_id, share_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board or share not found",
        ) from exc
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
