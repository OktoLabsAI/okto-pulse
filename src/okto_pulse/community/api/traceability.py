"""Traceability API endpoints for SDLC lineage visualizations."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.knowledge_workspace import (
    KnowledgeWorkspaceProjectionError,
)
from okto_pulse.core.application.use_cases.operational_rest import (
    BoardNotFoundError,
    GetEffectiveResourcesCommand,
    GetEffectiveResourcesUseCase,
    GetLineageGraphCommand,
    GetLineageGraphUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.ports.traceability import (
    TraceabilityReadError,
)
from okto_pulse.core.services.resource_gate import (
    ResourceGateError,
    ResourceGateNotFound,
)

router = APIRouter(prefix="/boards", tags=["traceability"])


def _workspace_entity_type(entity_type: str) -> str | None:
    normalized = entity_type.strip().lower()
    if normalized in {"task", "test", "bug"}:
        return "card"
    if normalized in {"ideation", "refinement", "spec", "card"}:
        return normalized
    return None


def _workspace_counts(data: dict) -> dict[str, int]:
    return {
        "unique_effective_count": int(
            data.get("unique_effective_count") or 0
        ),
        "unique_root_version_count": int(
            data.get("unique_root_version_count") or 0
        ),
        "raw_attachment_count": int(data.get("raw_attachment_count") or 0),
        "workspace_item_count": int(data.get("workspace_item_count") or 0),
    }


@router.get("/{board_id}/lineage-graph")
async def get_lineage_graph(
    board_id: str,
    entity_type: str = Query(..., min_length=1),
    entity_id: str = Query(..., min_length=1),
    include_artifacts: bool = False,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
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
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
        data = result.data
        workspace_entity_type = _workspace_entity_type(entity_type)
        if workspace_entity_type is not None:
            workspace = await GetEffectiveResourcesUseCase().execute(
                GetEffectiveResourcesCommand(
                    board_id,
                    workspace_entity_type,
                    entity_id,
                    "summary",
                    None,
                    1,
                ),
                actor=RESTAdapterContract.actor(user_id, board_id=board_id),
                uow=uow,
            )
            counts = _workspace_counts(workspace.data)
            data["resource_counts"] = counts
            for node in data.get("nodes") or []:
                if str(node.get("entity_id") or "") == entity_id:
                    node["resource_counts"] = counts
                    break
        return data
    except BoardNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Board not found") from exc
    except KnowledgeWorkspaceProjectionError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=exc.to_dict(),
        ) from exc
    except ResourceGateError as exc:
        raise HTTPException(
            status_code=(
                404 if isinstance(exc, ResourceGateNotFound) else 400
            ),
            detail={
                "code": exc.code,
                "message": str(exc),
                "details": exc.details,
            },
        ) from exc
    except TraceabilityReadError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
