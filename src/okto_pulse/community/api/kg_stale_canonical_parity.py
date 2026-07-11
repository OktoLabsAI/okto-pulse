"""R2-IMP4 — REST drilldown for stale-canonical parity.

Read-only. KG Health surfaces ``stale_canonical_parity`` as an aggregate
``health_issues[]`` entry pointing here via ``drill_down_tool``; the per-node
rows (canonical deterministic nodes whose source regressed, annotated with the
Global Discovery digest staleness) live in this drilldown. Mirrored 1:1 by the
MCP tool ``okto_pulse_kg_stale_canonical_parity_list``. NO mutation / demotion /
reconcile / Global Discovery sync — diagnostic only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    ListStaleCanonicalParityCommand,
    ListStaleCanonicalParityUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.repositories import PulseUnitOfWork

router = APIRouter()


@router.get(
    "/kg/{board_id}/stale-canonical-parity",
    tags=["kg-stale-canonical-parity"],
)
async def list_stale_canonical_parity_endpoint(
    board_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict[str, Any]:
    """Read-only list of stale-canonical parity signals.

    Each item: node_id, node_type, source_artifact_ref, board_graph_stale,
    global_discovery_stale_digest, expected_graph_layer, expected_maturity_status,
    current_source_status, recommended_action. ``global_discovery_evaluation`` is
    ``not_evaluated`` (never a false healthy) if R1 digest metadata is unreadable.

    Spec R01A IMP4: routes through the transport-free use case via
    ``get_unit_of_work`` — no raw ``AsyncSession``/``get_db`` in the handler.
    """
    result = await ListStaleCanonicalParityUseCase().execute(
        ListStaleCanonicalParityCommand(board_id, limit=limit, offset=offset),
        actor=RESTAdapterContract.actor(user_id, board_id=board_id),
        uow=uow,
    )
    return result.data
