"""Dead Letter Inspector REST endpoint (spec ed17b1fe — Wave 2 NC 1ede3471).

GET /api/v1/kg/queue/dead-letter — listing of DLQ rows for a board.
POST /api/v1/kg/queue/dead-letter/redrive — requeue selected or all rows.
Pagination via ``limit`` (1-200, default 50) + ``offset`` (>=0, default 0).
Board access is preflighted before the DLQ reader is constructed or called.

Both transports delegate to the same use case, idempotency rules and
``kg.operations.queue.reprocess`` permission.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    ListDeadLetterRowsCommand,
    ListDeadLetterRowsUseCase,
    ReprocessDeadLetterRowsCommand,
    ReprocessDeadLetterRowsUseCase,
)
from okto_pulse.core.application.use_cases.list_dead_letter_rows import (
    DeadLetterBoardNotFoundError,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import (
    get_current_user,
    get_realm_id,
    require_user,
)
from okto_pulse.community.api.kg_routes import require_kg_board_writer_actor
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX
from okto_pulse.core.domain.code_traceability_kg import (
    CODE_TRACEABILITY_KG_SUBTYPES,
    KGDeadLetterReprocessScope,
)
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


class DeadLetterRow(BaseModel):
    id: str
    board_id: str
    artifact_type: str
    artifact_id: str
    original_queue_id: str | None
    attempts: int
    errors: list[dict[str, Any]]
    dead_lettered_at: str | None


class DeadLetterListResponse(BaseModel):
    rows: list[DeadLetterRow]
    total: int
    limit: int
    offset: int


class DeadLetterRedrivePayload(BaseModel):
    board_id: str
    dead_letter_ids: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    redrive_all: bool = False
    scope: KGDeadLetterReprocessScope = KGDeadLetterReprocessScope.GENERIC
    process_now: bool = True

    @model_validator(mode="after")
    def require_one_selection_mode(self) -> DeadLetterRedrivePayload:
        if self.redrive_all == (self.dead_letter_ids is not None):
            raise ValueError(
                "provide dead_letter_ids or set redrive_all=true, but not both"
            )
        return self


class DeadLetterRedriveResponse(BaseModel):
    success: bool
    blocked: bool = False
    mutated: bool
    scope: str
    requested: int | None = None
    selected: int
    requeued: list[dict[str, Any]] = Field(default_factory=list)
    already_queued: list[dict[str, Any]] = Field(default_factory=list)
    requeued_count: int
    already_queued_count: int
    recovery_by_class: dict[str, int] = Field(default_factory=dict)
    idempotency_key: str | None = None
    worker_running: bool | None = None
    processed_now_count: int | None = None
    process_now_mode: str | None = None
    remaining: int | None = None
    batches: int | None = None
    stop_reason: str | None = None


@router.get(
    "/kg/queue/dead-letter",
    response_model=DeadLetterListResponse,
)
async def get_dead_letter(
    board_id: str = Query(..., description="Board UUID (required)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> DeadLetterListResponse:
    """List dead-lettered consolidation rows for a board.

    Returns ``{rows, total, limit, offset}``. Each row includes the
    full ``errors[]`` history from the TR16 schema (one entry per
    attempt: error_type, message, occurred_at, traceback).

    Spec R01A IMP2: the handler is the REST inbound adapter — it obtains a
    request-scoped ``PulseUnitOfWork`` (bound to the request session via
    ``get_db``, so the dependency override still applies) and calls the
    transport-free use case. No raw ``AsyncSession``/``get_db`` in the handler's
    contract with the use case; payload/permission are unchanged.
    """
    try:
        result = await ListDeadLetterRowsUseCase().execute(
            ListDeadLetterRowsCommand(board_id, limit=limit, offset=offset),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except DeadLetterBoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    return DeadLetterListResponse(**result.data)


@router.post(
    "/kg/queue/dead-letter/redrive",
    response_model=DeadLetterRedriveResponse,
)
async def redrive_dead_letter(
    payload: DeadLetterRedrivePayload,
    user_id: str = Depends(require_user),
    user: dict[str, Any] | None = Depends(get_current_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> DeadLetterRedriveResponse:
    """Requeue selected or all accessible DLQ rows and wake the worker.

    Explicit selection is capped at 200 rows. ``redrive_all`` drains bounded
    200-row transactions, bounded by the initially visible row count. Concurrent
    additions or blocked rows may remain and are reported, never retried forever.
    Generic and Code Traceability rows retain their separate authorization
    scopes. The shared use cases own permission checks, deduplication and each
    transaction commit.
    """
    actor = await require_kg_board_writer_actor(
        board_id=payload.board_id,
        user_id=user_id,
        user=user,
        realm_id=realm_id,
        uow=uow,
    )
    try:
        if payload.redrive_all:
            data = await _redrive_all_accessible_rows(
                payload.board_id,
                actor=actor,
                uow=uow,
            )
        else:
            assert payload.dead_letter_ids is not None
            result = await ReprocessDeadLetterRowsUseCase().execute(
                ReprocessDeadLetterRowsCommand(
                    payload.board_id,
                    dead_letter_ids=payload.dead_letter_ids,
                    limit=len(payload.dead_letter_ids),
                    scope=payload.scope,
                ),
                actor=actor,
                uow=uow,
            )
            data = dict(result.data)
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    except DeadLetterBoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc

    if payload.process_now and bool(data.get("mutated")):
        await _wake_consolidation_worker(data)
    return DeadLetterRedriveResponse(**data)


async def _redrive_all_accessible_rows(
    board_id: str,
    *,
    actor: Any,
    uow: PulseUnitOfWork,
) -> dict[str, Any]:
    """Drain the finite accessible DLQ through bounded scoped transactions."""

    aggregate: dict[str, Any] = {
        "success": True,
        "blocked": False,
        "mutated": False,
        "scope": "all",
        "requested": 0,
        "selected": 0,
        "requeued": [],
        "already_queued": [],
        "requeued_count": 0,
        "already_queued_count": 0,
        "recovery_by_class": {},
        "idempotency_key": "board_id+artifact_type+artifact_id",
        "remaining": 0,
        "batches": 0,
        "stop_reason": None,
    }
    initial_total: int | None = None
    attempted_ids: set[str] = set()
    refresh_remaining = False
    while True:
        listed = await ListDeadLetterRowsUseCase().execute(
            ListDeadLetterRowsCommand(board_id, limit=200, offset=0),
            actor=actor,
            uow=uow,
        )
        rows = list(listed.data.get("rows") or [])
        visible_total = int(listed.data.get("total") or 0)
        if initial_total is None:
            initial_total = visible_total
            aggregate["requested"] = visible_total
        aggregate["remaining"] = visible_total
        if not rows:
            break
        if len(attempted_ids) >= initial_total:
            aggregate["stop_reason"] = "initial_selection_limit"
            break

        # Workers can fail again while this request is requeuing earlier rows.
        # Do not chase new arrivals beyond the initial count or retry one ID
        # indefinitely if the scoped service leaves a blocked row in the DLQ.
        fresh_rows = []
        for row in rows:
            identifier = str(row["id"])
            if identifier not in attempted_ids:
                attempted_ids.add(identifier)
                fresh_rows.append(row)
                if len(attempted_ids) >= initial_total:
                    break
        if not fresh_rows:
            aggregate["stop_reason"] = "repeated_rows"
            break

        scoped_ids: dict[KGDeadLetterReprocessScope, list[str]] = {
            KGDeadLetterReprocessScope.GENERIC: [],
            KGDeadLetterReprocessScope.CODE_TRACEABILITY: [],
        }
        for row in fresh_rows:
            scope = (
                KGDeadLetterReprocessScope.CODE_TRACEABILITY
                if str(row.get("artifact_type") or "")
                in CODE_TRACEABILITY_KG_SUBTYPES
                else KGDeadLetterReprocessScope.GENERIC
            )
            scoped_ids[scope].append(str(row["id"]))

        progressed = False
        aggregate["batches"] += 1
        for scope, identifiers in scoped_ids.items():
            if not identifiers:
                continue
            result = await ReprocessDeadLetterRowsUseCase().execute(
                ReprocessDeadLetterRowsCommand(
                    board_id,
                    dead_letter_ids=identifiers,
                    limit=len(identifiers),
                    scope=scope,
                ),
                actor=actor,
                uow=uow,
            )
            data = result.data
            aggregate["success"] = bool(aggregate["success"]) and bool(
                data.get("success", True)
            )
            selected = int(data.get("selected") or 0)
            mutated = bool(data.get("mutated", selected > 0))
            progressed = progressed or mutated
            aggregate["mutated"] = bool(aggregate["mutated"]) or mutated
            aggregate["blocked"] = bool(aggregate["blocked"]) or bool(
                data.get("blocked")
            )
            aggregate["selected"] += selected
            aggregate["requeued_count"] += int(data.get("requeued_count") or 0)
            aggregate["already_queued_count"] += int(
                data.get("already_queued_count") or 0
            )
            aggregate["requeued"].extend(data.get("requeued") or [])
            aggregate["already_queued"].extend(data.get("already_queued") or [])
            for recovery_class, count in dict(
                data.get("recovery_by_class") or {}
            ).items():
                recovery = aggregate["recovery_by_class"]
                recovery[recovery_class] = int(recovery.get(recovery_class) or 0) + int(
                    count or 0
                )

        if not progressed or aggregate["blocked"] or not aggregate["success"]:
            aggregate["success"] = False
            aggregate["stop_reason"] = (
                "blocked" if aggregate["blocked"]
                else "no_progress" if not progressed
                else "refused"
            )
            refresh_remaining = True
            break

    if refresh_remaining:
        listed = await ListDeadLetterRowsUseCase().execute(
            ListDeadLetterRowsCommand(board_id, limit=1, offset=0),
            actor=actor,
            uow=uow,
        )
        aggregate["remaining"] = int(listed.data.get("total") or 0)
    if int(aggregate["remaining"] or 0) > 0 or bool(aggregate["blocked"]):
        aggregate["success"] = False
    return aggregate


async def _wake_consolidation_worker(data: dict[str, Any]) -> None:
    from okto_pulse.core.application.runtime_workers import (
        process_runtime_worker_once,
        runtime_worker_is_running,
        signal_runtime_worker,
    )

    worker_running = runtime_worker_is_running("consolidation_worker")
    signal_runtime_worker("consolidation_worker")
    data["worker_running"] = worker_running
    if worker_running:
        data["processed_now_count"] = 0
        data["process_now_mode"] = "signalled_app_runner"
    else:
        data["processed_now_count"] = await process_runtime_worker_once(
            "consolidation_worker"
        )
        data["process_now_mode"] = "app_runner_direct_batch"
