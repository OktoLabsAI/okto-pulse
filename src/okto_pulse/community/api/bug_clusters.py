"""Bounded, read-only Analytics transport; authority remains in the use case."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.permission_errors import permission_denied_http_error
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import EntityNotFoundError, PermissionDeniedError
from okto_pulse.core.application.use_cases.bug_clusters import BugClustersCommand, BugClustersUseCase
from okto_pulse.core.kg.interfaces.graph_errors import GraphError, GraphQueryTimeout
from okto_pulse.core.models.bug_clusters import BugClustersRequest, BugClustersResponse
from okto_pulse.core.ports.bug_clusters import BugClusterGrouping
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


@router.get('/boards/{board_id}/analytics/bug-clusters', response_model=BugClustersResponse)
async def board_bug_clusters(
    board_id: str,
    date_from: str | None = Query(None, alias='from'),
    date_to: str | None = Query(None, alias='to'),
    group_by: BugClusterGrouping = Query('proxy'),
    bug_status: str | None = Query(None, alias='status'),
    severity: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    cursor: str | None = Query(None, max_length=256),
    timeout_ms: int | None = Query(None, ge=1, le=30000),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Default: last 15 UTC days. Reuse returned window bounds for pagination."""
    try:
        # A cursor without its original window must not silently acquire a new
        # clock-dependent scope. Date-only upper bounds include that UTC day.
        window = BugClustersRequest(view='bugs', date_from=date_from, date_to=date_to,
            cursor=cursor).window(datetime.now(timezone.utc))
        result = await BugClustersUseCase().execute(
            BugClustersCommand(board_id, window, group_by,
                bug_status, severity, limit, cursor, timeout_ms),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id), uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(404, detail={'code': 'board_not_found', 'message': 'Board not found'}) from exc
    except PermissionDeniedError as exc:
        raise permission_denied_http_error(exc) from exc
    except GraphQueryTimeout as exc:
        raise HTTPException(504, detail={'code': 'graph_query_timeout', 'message': 'Cluster query timed out'}) from exc
    except GraphError as exc:
        # Native diagnostic details can contain paths or private graph contents.
        raise HTTPException(503, detail={'code': 'bug_clusters_unavailable', 'message': 'Cluster query unavailable'}) from exc
    except ValueError as exc:
        code = str(exc)
        if code in {'bug_clusters_cursor_stale', 'bug_clusters_source_changed'}:
            raise HTTPException(409, detail={'code': code, 'message': 'The query scope changed. Reload the first page.'}) from exc
        public = {'bug_clusters_cursor_invalid', 'bug_clusters_cursor_window_required',
            'bug_clusters_window_invalid', 'analytics_temporal_query_invalid',
            'bug_clusters_filter_invalid'}
        if code in public:
            raise HTTPException(400, detail={'code': code, 'message': 'Invalid cluster query'}) from exc
        raise HTTPException(503, detail={'code': 'bug_clusters_unavailable', 'message': 'Cluster query unavailable'}) from exc
    return result
