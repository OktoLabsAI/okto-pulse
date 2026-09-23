"""Optional graph capabilities: neutral ports, existing board/CT authorization."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from okto_pulse.core.kg.blocking_io import run_blocking_graph_io
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
)
from okto_pulse.core.kg.interfaces.ranked_graph_search import RankedGraphQuery
from okto_pulse.core.services.application_kg import (
    get_current_provider_registry as get_kg_registry,
)
from okto_pulse.core.application.use_cases.code_traceability_kg_access import (
    require_code_traceability_safe_arbitrary_query,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.community.api import kg_routes as kg

router = APIRouter(prefix="/kg/boards/{board_id}/exploration", tags=["knowledge-graph"])


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    node_type: str
    query: str = Field(min_length=1, max_length=4096)
    mode: Literal["text", "hybrid"] = "text"
    limit: int = Field(20, ge=1, le=200, strict=True)
    graph_layer: Literal["all", "canonical", "working"] = "canonical"
    include_superseded: bool = False
    include_code_traceability: bool = False
    min_confidence: float = Field(0.5, ge=0, le=1)
    candidate_limit: int = Field(100, ge=1, le=1000, strict=True)
    max_filter_rows: int = Field(10000, ge=1, le=100000, strict=True)
    timeout_seconds: float = Field(10, ge=0.001, le=30)
    phrase: bool = False


def capability(name):
    provider = getattr(get_kg_registry(), name, None)
    if provider is None:
        raise GraphCapabilityUnavailable(
            "The configured graph provider does not offer this capability.",
            details={"capability": name},
        )
    return provider


async def authorize(board_id, actor, uow):
    await kg._require_kg_operation(
        actor,
        operation="kg.query.related_context",
        legacy_operation="board:read",
        board_id=board_id,
        uow=uow,
        require_board_read=True,
    )


async def authorize_unfiltered(board_id, actor, uow):
    await authorize(board_id, actor, uow)
    access = await kg._code_traceability_kg_read_access(
        actor=actor, board_id=board_id, uow=uow
    )
    try:
        require_code_traceability_safe_arbitrary_query(access)
    except PermissionDeniedError as exc:
        raise kg.RESTAdapterContract.http_error(exc) from exc
    return access


async def authorize_history(board_id, actor, uow):
    await authorize_unfiltered(board_id, actor, uow)
    # Retained/deleted values and commit metadata are audit observations, not
    # ordinary current-node search. Reuse the existing audit permission.
    await kg._require_kg_operation(
        actor,
        operation="kg.operations.audit.read",
        legacy_operation="kg.admin.settings_read",
        board_id=board_id,
        uow=uow,
    )


async def invoke(board_id, operation):
    try:
        return await run_blocking_graph_io(
            operation, task_name=f"community.kg.exploration:{board_id}"
        )
    except GraphError as exc:
        return kg._graph_problem(exc)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc


@router.get("/search/readiness/{node_type}")
async def search_readiness(
    board_id: str,
    node_type: str,
    actor=Depends(kg.require_kg_board_actor),
    uow=Depends(kg.get_unit_of_work),
):
    await authorize(board_id, actor, uow)
    return await invoke(
        board_id,
        lambda: capability("ranked_graph_search").readiness(board_id, node_type),
    )


@router.post("/search")
async def search(
    board_id: str,
    body: SearchRequest,
    actor=Depends(kg.require_kg_board_actor),
    uow=Depends(kg.get_unit_of_work),
):
    # Native corpus statistics include all indexed documents. Require complete
    # CT authority, even when result rows themselves exclude CT by default.
    access = await authorize_unfiltered(board_id, actor, uow)
    fields = body.model_dump()
    fields["include_code_traceability"] = (
        body.include_code_traceability and access.allowed
    )

    def read():
        provider = capability("ranked_graph_search")
        if body.mode == "hybrid":
            embedder = get_kg_registry().embedding_provider
            if embedder is None:
                raise GraphCapabilityUnavailable(
                    "Hybrid retrieval requires an embedding provider."
                )
            fields["vector"] = tuple(float(x) for x in embedder.encode(body.query))
        return provider.search(board_id, RankedGraphQuery(**fields))

    return await invoke(board_id, read)


class HistoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_types: list[str] = Field(min_length=1, max_length=32)
    relationship_types: list[str] = Field(default_factory=list, max_length=100)


class HistoryRead(HistoryScope):
    at: str = Field(min_length=49, max_length=49)
    max_rows: int = Field(1000, ge=1, le=10000, strict=True)
    max_bytes: int = Field(16777216, ge=1, le=67108864, strict=True)


class HistoryDiff(HistoryRead):
    before: str = Field(min_length=49, max_length=49)


@router.get("/history/commits")
async def history_commits(
    board_id: str,
    after: str | None = None,
    limit: int = 100,
    actor=Depends(kg.require_kg_board_actor),
    uow=Depends(kg.get_unit_of_work),
):
    await authorize_history(board_id, actor, uow)
    return await invoke(
        board_id,
        lambda: capability("graph_history").commits(board_id, after=after, limit=limit),
    )


@router.post("/history/as-of")
async def history_as_of(
    board_id: str,
    body: HistoryRead,
    actor=Depends(kg.require_kg_board_actor),
    uow=Depends(kg.get_unit_of_work),
):
    await authorize_history(board_id, actor, uow)
    return await invoke(
        board_id,
        lambda: capability("graph_history").as_of(
            board_id,
            body.at,
            tuple(body.node_types),
            tuple(body.relationship_types),
            max_rows=body.max_rows,
            max_bytes=body.max_bytes,
        ),
    )


@router.post("/history/diff")
async def history_diff(
    board_id: str,
    body: HistoryDiff,
    actor=Depends(kg.require_kg_board_actor),
    uow=Depends(kg.get_unit_of_work),
):
    await authorize_history(board_id, actor, uow)
    return await invoke(
        board_id,
        lambda: capability("graph_history").diff(
            board_id,
            body.before,
            body.at,
            tuple(body.node_types),
            tuple(body.relationship_types),
            max_rows=body.max_rows,
            max_bytes=body.max_bytes,
        ),
    )


class AnalyticsRequest(HistoryScope):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    algorithm: Literal["components", "cycles", "dependency_impact"]
    graph_layer: Literal["all", "canonical", "working"] = "canonical"
    include_code_traceability: bool = False
    source_type: str | None = None
    source_id: str | None = None
    direction: Literal["in", "out", "both"] = "out"
    max_depth: int = Field(10, ge=0, le=100, strict=True)
    max_nodes: int = Field(1000, ge=1, le=10000, strict=True)
    max_edges: int = Field(10000, ge=1, le=100000, strict=True)
    timeout_seconds: float = Field(10, ge=0.001, le=30)


@router.post("/analytics")
async def analyze_graph(
    board_id: str,
    body: AnalyticsRequest,
    actor=Depends(kg.require_kg_board_actor),
    uow=Depends(kg.get_unit_of_work),
):
    await authorize(board_id, actor, uow)
    access = await kg._code_traceability_kg_read_access(
        actor=actor, board_id=board_id, uow=uow
    )
    options = body.model_dump()
    options.update(
        node_types=tuple(body.node_types),
        relationship_types=tuple(body.relationship_types),
        include_code_traceability=body.include_code_traceability and access.allowed,
    )
    return await invoke(
        board_id, lambda: capability("graph_analytics").analyze(board_id, **options)
    )
