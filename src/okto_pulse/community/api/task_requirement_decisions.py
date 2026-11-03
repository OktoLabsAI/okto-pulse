"""Human task requirement decisions; no client-owned author or scope metadata."""
from fastapi import APIRouter, Depends, Query, Response, HTTPException
from pydantic import ValidationError
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.core.application.use_cases.task_requirement_decision import (
    GetTaskRequirementOptionsUseCase, RecordTaskRequirementDecisionUseCase, GetTaskRequirementHistoryUseCase,
)
from okto_pulse.core.models.task_requirement_decision import (
    TaskRequirementOptionsQuery, TaskRequirementDecisionInput, TaskRequirementDecisionCommand, TaskRequirementHistoryQuery,
)

router = APIRouter(prefix='/boards/{board_id}/specs/{spec_id}/task-requirement-decisions')


async def _run(use_case, command, principal, uow):
    actor = RESTAdapterContract.actor_from_principal(principal, board_id=command.board_id)
    try:
        return await use_case.execute(command, actor=actor, uow=uow)
    except PermissionDeniedError as exc:
        raise HTTPException(403, detail={'code': 'permission_denied', 'message': str(exc)}) from exc
    except ValueError as exc:
        code = str(exc).removeprefix('task_requirement_')
        status = 409 if code in {'stale_basis', 'idempotency_conflict', 'reopen_required'} else 404 if code.endswith('not_found') else 503 if code.endswith('unavailable') else 422
        raise HTTPException(status, detail={'code': code, 'message': str(exc)}) from exc


@router.get('/options')
async def options(board_id: str, spec_id: str, response: Response,
    card_ids: list[str] = Query(min_length=1, max_length=50),
    principal=Depends(require_principal), uow=Depends(get_unit_of_work)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        command = TaskRequirementOptionsQuery(board_id=board_id, spec_id=spec_id, card_ids=card_ids)
    except ValidationError as exc:
        raise HTTPException(422, detail={'code': 'invalid_input'}) from exc
    return await _run(GetTaskRequirementOptionsUseCase(), command, principal, uow)


@router.post('')
async def apply(board_id: str, spec_id: str, body: TaskRequirementDecisionInput,
    principal=Depends(require_principal), uow=Depends(get_unit_of_work)):
    command = TaskRequirementDecisionCommand(board_id=board_id, spec_id=spec_id, **body.model_dump())
    return await _run(RecordTaskRequirementDecisionUseCase(), command, principal, uow)


@router.get('/history')
async def history(board_id: str, spec_id: str, card_id: str, response: Response,
    edition: int | None = Query(default=None, ge=1), before_revision: int | None = Query(default=None, ge=1),
    limit: int = Query(default=10, ge=1, le=20), record_id: str | None = Query(default=None, max_length=512),
    principal=Depends(require_principal), uow=Depends(get_unit_of_work)):
    response.headers['Cache-Control'] = 'no-store'
    try:
        query = TaskRequirementHistoryQuery(board_id=board_id, spec_id=spec_id, card_ids=[card_id],
            edition=edition, before_revision=before_revision, limit=limit, record_id=record_id)
    except ValidationError as exc:
        raise HTTPException(422, detail={'code': 'invalid_input'}) from exc
    return await _run(GetTaskRequirementHistoryUseCase(), query, principal, uow)
