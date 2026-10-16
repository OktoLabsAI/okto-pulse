"""Bounded lineage transport alongside the existing visual SDLC endpoint."""
from fastapi import APIRouter, Depends, HTTPException, Query
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.permission_errors import permission_denied_http_error
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import EntityNotFoundError, PermissionDeniedError
from okto_pulse.core.application.use_cases.lineage_query import LineageCommand, LineageUseCase
from okto_pulse.core.kg.interfaces.graph_errors import GraphError, GraphQueryTimeout
from okto_pulse.core.models.lineage_query import LineageResponse
from okto_pulse.core.ports.traceability import TraceabilityReadError
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


@router.get('/boards/{board_id}/lineage', response_model=LineageResponse)
async def lineage(
    board_id: str,
    subject_ref: str = Query(..., pattern=r'^(spec|card|ideation|refinement|story|amendment_hotfix_revision):[^:\s]+$', max_length=4096),
    limit: int = Query(200, ge=1, le=1000), cursor: str | None = Query(None, max_length=256),
    max_depth: int = Query(3, ge=1, le=32), timeout_ms: int | None = Query(None, ge=1, le=30000),
    user_id: str = Depends(require_user), uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        return await LineageUseCase().execute(LineageCommand(board_id, subject_ref, limit, cursor, max_depth, timeout_ms),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id), uow=uow)
    except EntityNotFoundError as exc:
        raise HTTPException(404, detail={'code': 'lineage_subject_not_found', 'message': 'Lineage subject not found or unavailable'}) from exc
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    except GraphQueryTimeout as exc:
        raise HTTPException(504, detail={'code': 'graph_query_timeout', 'message': 'Lineage query timed out'}) from exc
    except GraphError as exc:
        raise HTTPException(503, detail={'code': 'lineage_unavailable', 'message': 'Lineage query unavailable'}) from exc
    except TraceabilityReadError as exc:
        status, code = (404, 'lineage_subject_not_found') if exc.status_code == 404 else (503, 'lineage_unavailable')
        raise HTTPException(status, detail={'code': code, 'message': 'Lineage query unavailable'}) from exc
    except ValueError as exc:
        code = str(exc)
        if code in {'lineage_cursor_stale', 'lineage_source_changed'}:
            raise HTTPException(409, detail={'code': code, 'message': 'The source changed. Reload the first page.'}) from exc
        if code == 'lineage_cursor_invalid':
            raise HTTPException(400, detail={'code': code, 'message': 'Invalid lineage cursor'}) from exc
        if code in {'lineage_source_node_bound', 'lineage_source_edge_bound', 'lineage_summary_payload_bound', 'lineage_row_payload_bound'}:
            raise HTTPException(409, detail={'code': code, 'message': 'The lineage scope exceeds the query resource limit.'}) from exc
        raise HTTPException(503, detail={'code': 'lineage_unavailable', 'message': 'Lineage query unavailable'}) from exc
