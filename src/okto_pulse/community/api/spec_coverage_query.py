"""Read-only coverage transport; authorization and semantics remain in Core."""
from fastapi import APIRouter, Depends, HTTPException, Query
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.permission_errors import permission_denied_http_error
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import EntityNotFoundError, PermissionDeniedError
from okto_pulse.core.application.use_cases.spec_coverage_query import SpecCoverageCommand, SpecCoverageUseCase
from okto_pulse.core.kg.interfaces.graph_errors import GraphError, GraphQueryTimeout
from okto_pulse.core.models.spec_coverage_query import SpecCoverageResponse
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


@router.get('/boards/{board_id}/specs/{spec_id}/coverage', response_model=SpecCoverageResponse)
async def spec_coverage(
    board_id: str, spec_id: str,
    limit: int = Query(200, ge=1, le=1000),
    cursor: str | None = Query(None, max_length=256),
    timeout_ms: int | None = Query(None, ge=1, le=30000),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        return await SpecCoverageUseCase().execute(
            SpecCoverageCommand(board_id, spec_id, limit, cursor, timeout_ms),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id), uow=uow)
    except EntityNotFoundError as exc:
        raise HTTPException(404, detail={'code': 'spec_not_found', 'message': 'Spec not found or unavailable'}) from exc
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    except GraphQueryTimeout as exc:
        raise HTTPException(504, detail={'code': 'graph_query_timeout', 'message': 'Coverage query timed out'}) from exc
    except GraphError as exc:
        raise HTTPException(503, detail={'code': 'spec_coverage_unavailable', 'message': 'Coverage query unavailable'}) from exc
    except ValueError as exc:
        code = str(exc)
        if code in {'spec_coverage_cursor_stale', 'spec_coverage_source_changed'}:
            raise HTTPException(409, detail={'code': code, 'message': 'The query scope changed. Reload the first page.'}) from exc
        if code == 'spec_coverage_cursor_invalid':
            raise HTTPException(400, detail={'code': code, 'message': 'Invalid coverage cursor'}) from exc
        raise HTTPException(503, detail={'code': 'spec_coverage_unavailable', 'message': 'Coverage query unavailable'}) from exc
