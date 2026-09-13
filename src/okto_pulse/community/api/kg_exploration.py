"""Optional graph capabilities: neutral ports, existing board/CT authorization."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from okto_pulse.core.kg.blocking_io import run_blocking_graph_io
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
)
from okto_pulse.core.kg.interfaces.ranked_graph_search import RankedGraphQuery
from okto_pulse.core.kg.interfaces.registry import get_kg_registry
from okto_pulse.core.kg.guarded_write import guarded_board_write, GuardedWriteError
from okto_pulse.core.application.use_cases.code_traceability_kg_access import (
    require_code_traceability_safe_arbitrary_query,
)
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.community.api import kg_routes as kg

router = APIRouter(prefix="/kg/boards/{board_id}/exploration", tags=["knowledge-graph"])
logger = logging.getLogger(__name__)


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


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_type: str
    reason: str = Field(min_length=1, max_length=1024)


def capability(name):
    provider = getattr(get_kg_registry(), name, None)
    if provider is None:
        raise GraphCapabilityUnavailable(
            "The configured graph provider does not offer this capability.",
            details={"capability": name},
        )
    return provider


async def authorize(board_id, actor, uow, *, write=False):
    await kg._require_kg_operation(
        actor,
        operation="kg.operations.schema.migrate"
        if write
        else "kg.query.related_context",
        legacy_operation="kg.admin.settings_write" if write else "board:read",
        board_id=board_id,
        uow=uow,
        require_board_read=True,
    )


async def authorize_unfiltered(board_id, actor, uow, *, write=False):
    await authorize(board_id, actor, uow, write=write)
    access = await kg._code_traceability_kg_read_access(
        actor=actor, board_id=board_id, uow=uow
    )
    try:
        require_code_traceability_safe_arbitrary_query(access)
    except PermissionDeniedError as exc:
        raise kg.RESTAdapterContract.http_error(exc) from exc
    return access


async def authorize_history(board_id, actor, uow, *, write=False):
    await authorize_unfiltered(board_id, actor, uow, write=write)
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


async def invoke_admin(board_id, operation):
    def guarded():
        try:
            with guarded_board_write(
                board_id,
                operation="graph_exploration_admin",
                owner_id="community.exploration",
                mutation_ref="explicit_graph_administration",
            ) as lease:
                result = operation()
                lease.ensure_durable()
                return result
        except GuardedWriteError as exc:
            # A failed post-write lifecycle can follow an applied native commit.
            # Report refusal; never automatically replay the operation.
            raise GraphCapabilityUnavailable(
                "Graph administration was refused or could not be acknowledged.",
                details={"reason": exc.code},
            ) from exc

    return await invoke(board_id, guarded)


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


@router.post("/search/prepare")
async def prepare_search(
    board_id: str,
    body: PrepareRequest,
    actor=Depends(kg.require_kg_board_writer_actor),
    uow=Depends(kg.get_unit_of_work),
):
    await authorize(board_id, actor, uow, write=True)

    def prepare():
        result = capability("ranked_graph_search").prepare(
            board_id, body.node_type, reason=body.reason
        )
        logger.info(
            "kg.ranked_search.prepared board=%s node_type=%s reason=%r",
            board_id,
            body.node_type,
            body.reason,
        )
        return result

    return await invoke_admin(board_id, prepare)


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


class HistoryActivation(HistoryScope):
    reason: str = Field(min_length=1, max_length=1024)
    acknowledge_one_way: Literal[True]


class HistoryRead(HistoryScope):
    at: str = Field(min_length=49, max_length=49)
    max_rows: int = Field(1000, ge=1, le=10000, strict=True)
    max_bytes: int = Field(16777216, ge=1, le=67108864, strict=True)


class HistoryDiff(HistoryRead):
    before: str = Field(min_length=49, max_length=49)


class HistoryPrune(HistoryScope):
    before: str = Field(min_length=49, max_length=49)
    reason: str = Field(min_length=1, max_length=1024)
    max_bytes: int = Field(16777216, ge=1, le=67108864, strict=True)
    acknowledge_history_loss: Literal[True]


@router.post("/history/activate")
async def activate_history(
    board_id: str,
    body: HistoryActivation,
    actor=Depends(kg.require_kg_board_writer_actor),
    uow=Depends(kg.get_unit_of_work),
):
    await authorize_history(board_id, actor, uow, write=True)

    def activate():
        result = capability("graph_history").activate(
            board_id,
            tuple(body.node_types),
            tuple(body.relationship_types),
            reason=body.reason,
        )
        logger.info("kg.history.activated board=%s reason=%r", board_id, body.reason)
        return result

    return await invoke_admin(board_id, activate)


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


@router.post("/history/prune")
async def history_prune(
    board_id: str,
    body: HistoryPrune,
    actor=Depends(kg.require_kg_board_writer_actor),
    uow=Depends(kg.get_unit_of_work),
):
    await authorize_history(board_id, actor, uow, write=True)

    def prune():
        result = capability("graph_history").prune(
            board_id,
            body.before,
            tuple(body.node_types),
            tuple(body.relationship_types),
            reason=body.reason,
            max_bytes=body.max_bytes,
        )
        logger.info(
            "kg.history.pruned board=%s reason=%r before=%s",
            board_id,
            body.reason,
            body.before,
        )
        return result

    return await invoke_admin(board_id, prune)


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
