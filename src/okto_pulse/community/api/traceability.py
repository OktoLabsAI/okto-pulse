"""Traceability API endpoints for SDLC lineage visualizations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.operational_rest import (
    GetLineageGraphCommand,
    GetLineageGraphUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.ports.traceability import (
    TraceabilityReadError,
)

router = APIRouter(prefix="/boards", tags=["traceability"])


@router.get("/{board_id}/lineage-graph")
async def get_lineage_graph(
    board_id: str,
    entity_type: str = Query(..., min_length=1),
    entity_id: str = Query(..., min_length=1),
    include_artifacts: bool = False,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict:
    """Return a UI-only SDLC lineage graph rooted at the selected entity."""
    try:
        result = await GetLineageGraphUseCase().execute(
            GetLineageGraphCommand(
                board_id,
                entity_type,
                entity_id,
                include_artifacts,
            ),
            actor=RESTAdapterContract.actor("traceability-rest", board_id=board_id),
            uow=db,
        )
        return result.data
    except TraceabilityReadError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
