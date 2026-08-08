"""R7 IMP4 — REST drilldown for canonical Learning partition integrity.

Read-only. KG Health surfaces ``canonical_partition_integrity`` as an AGGREGATE
``health_issues[]`` entry that points here via ``drill_down_tool``; the per-node
rows (go-forward holds, historical debt, mixed-evidence deferred, provenance-only
observed) live ONLY in this drilldown. Mirrored 1:1 by the MCP tool
``okto_pulse_kg_canonical_partition_integrity_list``. No skip/retry/mutation.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.operational_rest import (
    BoardNotFoundError,
    CanonicalPartitionDetailCommand,
    CanonicalPartitionListCommand,
    GetCanonicalPartitionIntegrityDetailUseCase,
    ListCanonicalPartitionIntegrityUseCase,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.kg.cognitive_readiness import CognitiveReadinessError
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.ports.application_persistence import PAGE_OFFSET_MAX

router = APIRouter()


@router.get(
    "/kg/{board_id}/canonical-partition-integrity",
    tags=["kg-canonical-partition-integrity"],
)
async def list_canonical_partition_integrity_endpoint(
    board_id: str,
    reason_code: str | None = Query(None),
    graph_layer: str | None = Query(None),
    source_ref: str | None = Query(None),
    node_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict[str, Any]:
    """Read-only list of canonical-partition-integrity signals.

    Each item carries an S-KG-02 ``classification`` (Learning-centric verdict;
    the response also includes a ``classification_counts`` census) alongside the
    preserved ``status`` / ``counts``. Mirrored 1:1 by the MCP tool.

    Errors: 400 ``invalid_filter`` (bad enum filter) / 503 ``kg_health_unavailable``.
    """
    try:
        result = await ListCanonicalPartitionIntegrityUseCase().execute(
            CanonicalPartitionListCommand(
                board_id,
                reason_code,
                graph_layer,
                source_ref,
                node_id,
                status,
                limit,
                offset,
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    except CognitiveReadinessError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc


@router.get(
    "/kg/{board_id}/canonical-partition-integrity/{node_id}",
    tags=["kg-canonical-partition-integrity"],
)
async def get_canonical_partition_integrity_detail_endpoint(
    board_id: str,
    node_id: str,
    user_id: str = Depends(require_user),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict[str, Any]:
    """Read-only detail for one canonical Learning node.

    Carries the S-KG-02 ``classification`` plus ``canonical_edges`` /
    ``working_edges`` (validates -> Bug) and ``relates_to_edges`` (the taxonomy
    associations), consistent with the list/MCP surface.

    Error: 404 ``canonical_partition_item_not_found``.
    """
    try:
        result = await GetCanonicalPartitionIntegrityDetailUseCase().execute(
            CanonicalPartitionDetailCommand(board_id, node_id),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=db,
        )
        return result.data
    except BoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc) from exc
    except CognitiveReadinessError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.to_dict()) from exc
