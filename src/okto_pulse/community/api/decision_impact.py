"""Read-only Decision impact; Core owns authorization and interpretation."""
from fastapi import APIRouter, Depends, HTTPException, Query
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.permission_errors import permission_denied_http_error
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import EntityNotFoundError, PermissionDeniedError
from okto_pulse.core.application.use_cases.decision_impact import DecisionImpactCommand, DecisionImpactUseCase
from okto_pulse.core.kg.interfaces.graph_errors import GraphError, GraphQueryTimeout
from okto_pulse.core.models.decision_impact import DecisionImpactResponse
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


@router.get('/boards/{board_id}/specs/{spec_id}/decisions/{decision_id}/impact', response_model=DecisionImpactResponse)
async def decision_impact(
    board_id: str, spec_id: str, decision_id: str,
    limit: int = Query(200, ge=1, le=1000),
    cursor: str | None = Query(None, max_length=256),
    max_depth: int = Query(3, ge=1, le=8),
    timeout_ms: int | None = Query(None, ge=1, le=30000),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        return await DecisionImpactUseCase().execute(
            DecisionImpactCommand(board_id, spec_id, decision_id, limit, cursor, max_depth, timeout_ms),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id), uow=uow)
    except EntityNotFoundError as exc:
        raise HTTPException(404, detail={'code': 'decision_not_found', 'message': 'Decision not found or unavailable'}) from exc
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    except GraphQueryTimeout as exc:
        raise HTTPException(504, detail={'code': 'graph_query_timeout', 'message': 'Impact query timed out'}) from exc
    except GraphError as exc:
        raise HTTPException(503, detail={'code': 'decision_impact_unavailable', 'message': 'Impact query unavailable'}) from exc
    except ValueError as exc:
        code = str(exc)
        if code in {'decision_impact_cursor_stale', 'spec_coverage_source_changed'}:
            raise HTTPException(409, detail={'code': code, 'message': 'The query scope changed. Reload the first page.'}) from exc
        if code == 'decision_impact_cursor_invalid':
            raise HTTPException(400, detail={'code': code, 'message': 'Invalid impact cursor'}) from exc
        raise HTTPException(503, detail={'code': 'decision_impact_unavailable', 'message': 'Impact query unavailable'}) from exc
