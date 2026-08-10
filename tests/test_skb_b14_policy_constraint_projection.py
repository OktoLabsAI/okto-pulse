"""Semantic-guideline outbox and KG lineage regressions.

The historical B14 suite asserted deterministic ``Rule -> Constraint``
materialization.  SK-B3 deliberately retires that projection: semantic
guideline authority is now represented by physical ``Entity`` subtypes while
old rule ``Constraint`` nodes remain terminal audit history.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    CommunityKuzuGraphTransaction,
)
from okto_pulse.community.adapters.semantic_guideline_kg_events import (
    SEMANTIC_GUIDELINE_PROJECTION_HANDLER,
    SemanticGuidelineProjectionFact,
    stage_semantic_guideline_projection_events,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
)
from okto_pulse.community.adapters.sqlalchemy_policy_constraint_projection import (
    CommunitySqlAlchemyPolicyConstraintProjection,
    POLICY_CONSTRAINT_ACTOR,
    PolicyConstraintProjectionConflict,
    SEMANTIC_GUIDELINE_KG_ACTOR,
    SEMANTIC_GUIDELINE_KG_CONTRACT,
    SEMANTIC_GUIDELINE_LEGACY_TERMINATED_REASON,
    SEMANTIC_GUIDELINE_SOURCE_REMOVED_REASON,
    _decode_semantic_context,
    _desired_semantic_node,
    _semantic_node_id,
    _waiver_projection_state,
)
from okto_pulse.core.kg.interfaces.graph_transaction import (
    GraphStatementResult,
)


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
BOARD_ID = "board-semantic-kg"


class _SemanticGraphScope:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, dict[str, Any]]] = {
            "Entity": {},
            "Constraint": {},
        }
        self.edges: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._before: (
            tuple[
                dict[str, dict[str, dict[str, Any]]],
                dict[tuple[str, str, str, str, str], dict[str, Any]],
            ]
            | None
        ) = None

    async def __aenter__(self) -> "_SemanticGraphScope":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        params = params or {}
        normalized = " ".join(statement.split())
        if normalized == "BEGIN TRANSACTION":
            self._before = (deepcopy(self.nodes), deepcopy(self.edges))
            return GraphStatementResult()
        if normalized == "COMMIT":
            self._before = None
            return GraphStatementResult()
        if normalized == "ROLLBACK":
            assert self._before is not None
            self.nodes, self.edges = self._before
            self._before = None
            return GraphStatementResult()
        if normalized.startswith("MATCH (n:Entity) RETURN n.id"):
            rows = [
                (
                    node_id,
                    attrs.get("source_artifact_ref"),
                    attrs.get("created_by_agent"),
                    attrs.get("source_content_hash"),
                    attrs.get("title"),
                    attrs.get("content"),
                    attrs.get("context"),
                    attrs.get("justification"),
                    attrs.get("generation"),
                    attrs.get("superseded_by"),
                    attrs.get("revocation_reason"),
                    attrs.get("kind_of"),
                )
                for node_id, attrs in sorted(self.nodes["Entity"].items())
            ]
            return GraphStatementResult.from_rows(rows)
        if normalized.startswith("MATCH (n:Constraint) RETURN n.id"):
            rows = [
                (
                    node_id,
                    attrs.get("source_artifact_ref"),
                    attrs.get("created_by_agent"),
                    attrs.get("revocation_reason"),
                    attrs.get("superseded_by"),
                )
                for node_id, attrs in sorted(self.nodes["Constraint"].items())
            ]
            return GraphStatementResult.from_rows(rows)
        if normalized.startswith(
            "MATCH (n:Entity) WHERE n.source_artifact_ref = $source_ref"
        ):
            rows = [
                (node_id,)
                for node_id, attrs in self.nodes["Entity"].items()
                if attrs.get("source_artifact_ref") == params["source_ref"]
            ]
            return GraphStatementResult.from_rows(rows)
        if normalized.startswith("MATCH (n:Entity {id: $node_id}) SET"):
            node = self.nodes["Entity"][str(params["node_id"])]
            if "ended_at" not in params:
                node["superseded_at"] = None
            else:
                node["superseded_at"] = params["ended_at"]
                if "reason" in params:
                    node["superseded_by"] = None
                    node["revocation_reason"] = params["reason"]
            return GraphStatementResult()
        if normalized.startswith("MATCH (n:Constraint {id: $node_id}) SET"):
            node = self.nodes["Constraint"][str(params["node_id"])]
            node["superseded_by"] = None
            node["superseded_at"] = params["ended_at"]
            node["revocation_reason"] = params["reason"]
            return GraphStatementResult()
        raise AssertionError(f"unexpected statement: {normalized}")

    def create_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
        *,
        source_session_id: str,
    ) -> None:
        assert node_id not in self.nodes[node_type]
        self.nodes[node_type][node_id] = {
            **deepcopy(attrs),
            "source_session_id": source_session_id,
        }

    def update_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
    ) -> None:
        self.nodes[node_type][node_id].update(deepcopy(attrs))

    def edge_exists(
        self,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
    ) -> bool:
        return (
            edge_type,
            from_type,
            to_type,
            from_id,
            to_id,
        ) in self.edges

    def create_edge(
        self,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
        attrs: dict[str, Any],
    ) -> bool:
        key = (edge_type, from_type, to_type, from_id, to_id)
        assert key not in self.edges
        self.edges[key] = deepcopy(attrs)
        return True


class _SemanticGraph:
    def __init__(self) -> None:
        self.scope = _SemanticGraphScope()

    async def begin(self, board_id: str) -> _SemanticGraphScope:
        assert board_id == BOARD_ID
        return self.scope


@pytest.mark.asyncio
async def test_policy_projection_keeps_api_loop_responsive_during_graph_write():
    """The complete native graph scope must run on one non-loop thread."""

    loop_thread_id = threading.get_ident()
    graph_entered = threading.Event()
    release_graph = threading.Event()
    graph_thread_ids: list[int] = []
    graph_context_values: list[str | None] = []
    projection_context: ContextVar[str | None] = ContextVar(
        "test_policy_projection_context",
        default=None,
    )

    class _BlockingScope(_SemanticGraphScope):
        def execute(
            self,
            statement: str,
            params: dict[str, Any] | None = None,
        ) -> GraphStatementResult:
            graph_thread_ids.append(threading.get_ident())
            graph_context_values.append(projection_context.get())
            if " ".join(statement.split()) == "BEGIN TRANSACTION":
                graph_entered.set()
                if not release_graph.wait(timeout=5.0):
                    raise AssertionError("graph test worker was not released")
            return super().execute(statement, params)

        async def __aenter__(self) -> "_BlockingScope":
            graph_thread_ids.append(threading.get_ident())
            graph_context_values.append(projection_context.get())
            return self

        async def __aexit__(self, *_exc: object) -> None:
            graph_thread_ids.append(threading.get_ident())
            graph_context_values.append(projection_context.get())

    class _BlockingGraph:
        def __init__(self) -> None:
            self.scope = _BlockingScope()

        async def begin(self, board_id: str) -> _BlockingScope:
            assert board_id == BOARD_ID
            graph_thread_ids.append(threading.get_ident())
            graph_context_values.append(projection_context.get())
            return self.scope

    graph = _BlockingGraph()

    def _resolve_graph() -> _BlockingGraph:
        graph_thread_ids.append(threading.get_ident())
        graph_context_values.append(projection_context.get())
        return graph

    projector = CommunitySqlAlchemyPolicyConstraintProjection(
        graph_transaction_resolver=_resolve_graph
    )
    context_token = projection_context.set("assessment-event")
    try:
        projection = asyncio.create_task(
            projector._reconcile(  # noqa: SLF001
                board_id=BOARD_ID,
                operation="sync",
                event_id="event-off-loop",
                desired=(),
                projected_at=NOW,
            )
        )
        try:
            for _ in range(200):
                if graph_entered.is_set():
                    break
                await asyncio.sleep(0.005)
            assert graph_entered.is_set()

            loop_probe = asyncio.Event()
            asyncio.get_running_loop().call_soon(loop_probe.set)
            await asyncio.wait_for(loop_probe.wait(), timeout=0.25)
            assert not projection.done()
        finally:
            release_graph.set()

        result = await asyncio.wait_for(projection, timeout=2.0)
    finally:
        projection_context.reset(context_token)

    assert result.active_count == 0
    assert result.node_ids == ()
    assert graph_thread_ids
    assert set(graph_thread_ids) == {graph_thread_ids[0]}
    assert graph_thread_ids[0] != loop_thread_id
    assert set(graph_context_values) == {"assessment-event"}


@pytest.mark.asyncio
async def test_policy_projection_cancellation_waits_for_graph_scope_close():
    """Cancellation cannot detach a graph transaction that still owns I/O."""

    graph_entered = threading.Event()
    release_graph = threading.Event()
    graph_closed = threading.Event()
    statements: list[str] = []

    class _CancellationScope(_SemanticGraphScope):
        def execute(
            self,
            statement: str,
            params: dict[str, Any] | None = None,
        ) -> GraphStatementResult:
            normalized = " ".join(statement.split())
            statements.append(normalized)
            if normalized == "BEGIN TRANSACTION":
                graph_entered.set()
                if not release_graph.wait(timeout=5.0):
                    raise AssertionError("graph cancellation worker was not released")
            return super().execute(statement, params)

        async def __aexit__(self, *_exc: object) -> None:
            graph_closed.set()

    class _CancellationGraph:
        def __init__(self) -> None:
            self.scope = _CancellationScope()

        async def begin(self, board_id: str) -> _CancellationScope:
            assert board_id == BOARD_ID
            return self.scope

    graph = _CancellationGraph()
    projector = CommunitySqlAlchemyPolicyConstraintProjection(
        graph_transaction_resolver=lambda: graph
    )
    projection = asyncio.create_task(
        projector._reconcile(  # noqa: SLF001
            board_id=BOARD_ID,
            operation="sync",
            event_id="event-cancel-drain",
            desired=(),
            projected_at=NOW,
        )
    )
    try:
        for _ in range(200):
            if graph_entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert graph_entered.is_set()

        projection.cancel()
        await asyncio.sleep(0.02)
        assert not projection.done()
        assert not graph_closed.is_set()
    finally:
        release_graph.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(projection, timeout=2.0)
    assert "COMMIT" in statements
    assert graph_closed.is_set()


def _desired_chain() -> tuple[Any, ...]:
    revision = _semantic_node_id("revision", "revision-1")
    metric = _semantic_node_id("metric_definition", "revision-1:metric-1")
    binding = _semantic_node_id("binding_configuration", "binding-1:1")
    receipt = _semantic_node_id("assessment_receipt", "receipt-1")
    result = _semantic_node_id("metric_result", "result-1")
    identities = (
        ("revision", "revision-1", ()),
        ("metric_definition", "revision-1:metric-1", (revision,)),
        ("binding_configuration", "binding-1:1", (revision,)),
        ("assessment_receipt", "receipt-1", (binding, revision)),
        ("metric_result", "result-1", (receipt, metric)),
        ("waiver", "waiver-1", (result, receipt)),
        ("skip", "skip-1", (binding,)),
    )
    return tuple(
        _desired_semantic_node(
            kind=kind,
            identity=identity,
            digest=str(index) * 64,
            generation=1,
            active=True,
            reason=None,
            successor_id=None,
            lineage_ids=lineage,
            title=f"{kind} title",
            content=f"{kind} content",
            payload={"identity": identity},
            created_at=NOW,
            projected_at=NOW,
        )
        for index, (kind, identity, lineage) in enumerate(identities, start=1)
    )


@pytest.mark.parametrize(
    ("status", "expires_at", "binding_active", "expected"),
    (
        (
            "revoked",
            NOW - timedelta(minutes=1),
            False,
            (False, "semantic_guideline_waiver_revoked"),
        ),
        (
            "approved",
            NOW - timedelta(minutes=1),
            False,
            (False, "semantic_guideline_waiver_scheduled_expiry"),
        ),
        (
            "requested",
            NOW + timedelta(minutes=1),
            False,
            (False, "semantic_guideline_waiver_binding_inactive"),
        ),
        ("approved", None, True, (True, None)),
    ),
)
def test_waiver_projection_tombstone_precedence(
    status: str,
    expires_at: datetime | None,
    binding_active: bool,
    expected: tuple[bool, str | None],
) -> None:
    active_bindings = {("binding-1", 1)} if binding_active else set()

    assert (
        _waiver_projection_state(
            status=status,
            expires_at=expires_at,
            binding_id="binding-1",
            binding_revision=1,
            active_binding_keys=active_bindings,
            projected_at=NOW,
        )
        == expected
    )


@pytest.mark.asyncio
async def test_semantic_projection_is_entity_only_complete_and_replay_safe():
    graph = _SemanticGraph()
    graph.scope.nodes["Constraint"]["guideline-revision:old:rule:old"] = {
        "source_artifact_ref": "guideline-revision:old:rule:old",
        "created_by_agent": POLICY_CONSTRAINT_ACTOR,
        "superseded_by": None,
        "revocation_reason": None,
    }
    projector = CommunitySqlAlchemyPolicyConstraintProjection(
        graph_transaction_resolver=lambda: graph
    )
    desired = _desired_chain()

    first = await projector._reconcile(  # noqa: SLF001
        board_id=BOARD_ID,
        operation="sync",
        event_id="event-1",
        desired=desired,
        projected_at=NOW,
    )

    assert first.activated_count == 7
    assert first.ended_count == 1
    assert first.active_count == 7
    assert all(
        node["created_by_agent"] == SEMANTIC_GUIDELINE_KG_ACTOR
        for node_id, node in graph.scope.nodes["Entity"].items()
        if node_id.startswith("semantic-guideline:")
    )
    assert len(graph.scope.nodes["Constraint"]) == 1
    legacy = graph.scope.nodes["Constraint"]["guideline-revision:old:rule:old"]
    assert legacy["revocation_reason"] == SEMANTIC_GUIDELINE_LEGACY_TERMINATED_REASON

    lineage_edges = {
        (from_id, to_id)
        for (
            edge,
            from_type,
            to_type,
            from_id,
            to_id,
        ), attrs in graph.scope.edges.items()
        if edge == "belongs_to"
        and from_type == to_type == "Entity"
        and attrs.get("rule_id", "").startswith(
            "belongs_to/semantic_guideline_lineage@"
        )
    }
    assert lineage_edges == {
        (node.node_id, target) for node in desired for target in node.lineage_ids
    }

    replay = await projector._reconcile(  # noqa: SLF001
        board_id=BOARD_ID,
        operation="sync",
        event_id="event-1",
        desired=desired,
        projected_at=NOW,
    )
    assert replay.replayed is True
    assert replay.activated_count == replay.ended_count == 0

    # Empty text is not equivalent to SQL NULL for Core's consolidation
    # provenance audit. Reconcile must therefore repair it to actual NULL.
    repaired_node_id = desired[0].node_id
    graph.scope.nodes["Entity"][repaired_node_id]["source_content_hash"] = ""
    repaired = await projector._reconcile(  # noqa: SLF001
        board_id=BOARD_ID,
        operation="sync",
        event_id="event-repair-empty-provenance",
        desired=desired,
        projected_at=NOW,
    )
    assert repaired.activated_count == repaired.ended_count == 0
    assert graph.scope.nodes["Entity"][repaired_node_id]["source_content_hash"] is None

    removed = await projector._reconcile(  # noqa: SLF001
        board_id=BOARD_ID,
        operation="sync",
        event_id="event-2",
        desired=desired[:-2],
        projected_at=NOW,
    )
    assert removed.ended_count == 2
    for node in desired[-2:]:
        assert (
            graph.scope.nodes["Entity"][node.node_id]["revocation_reason"]
            == SEMANTIC_GUIDELINE_SOURCE_REMOVED_REASON
        )

    rebuilt = await projector._reconcile(  # noqa: SLF001
        board_id=BOARD_ID,
        operation="rebuild",
        event_id=None,
        desired=desired,
        projected_at=NOW,
    )
    assert rebuilt.activated_count == 2
    assert rebuilt.active_count == 7
    assert len(graph.scope.nodes["Constraint"]) == 1
    assert legacy["revocation_reason"]


@pytest.mark.asyncio
async def test_semantic_projection_outbox_is_atomic_and_causation_idempotent(
    tmp_path: Path,
):
    database = tmp_path / "semantic-kg-outbox.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        session.add(
            Board(
                id=BOARD_ID,
                name="Semantic KG",
                owner_id="owner-1",
            )
        )

    fact = SemanticGuidelineProjectionFact(
        entity_kind="revision",
        entity_id="revision-1",
        entity_digest="a" * 64,
    )
    writer = factory()
    await writer.begin()
    staged = await stage_semantic_guideline_projection_events(
        writer,
        board_id=BOARD_ID,
        actor_id="agent-1",
        actor_type="agent",
        occurred_at=NOW,
        causation_id="revision:revision-1",
        facts=(fact,),
    )
    await writer.flush()
    async with factory() as reader:
        assert (await reader.scalar(select(func.count(DomainEventRow.id)))) == 0
    await writer.rollback()
    await writer.close()

    async with factory() as session, session.begin():
        committed = await stage_semantic_guideline_projection_events(
            session,
            board_id=BOARD_ID,
            actor_id="agent-1",
            actor_type="agent",
            occurred_at=NOW,
            causation_id="revision:revision-1",
            facts=(fact, fact),
        )
        assert committed == staged

    async with factory() as session, session.begin():
        replay = await stage_semantic_guideline_projection_events(
            session,
            board_id=BOARD_ID,
            actor_id="agent-1",
            actor_type="agent",
            occurred_at=NOW,
            causation_id="revision:revision-1",
            facts=(fact,),
        )
        assert replay == committed
        second_cause = await stage_semantic_guideline_projection_events(
            session,
            board_id=BOARD_ID,
            actor_id="agent-1",
            actor_type="agent",
            occurred_at=NOW,
            causation_id="adoption:event-2",
            facts=(fact,),
        )
        assert second_cause[0].event_id != replay[0].event_id

    async with factory() as session:
        assert await session.scalar(select(func.count(DomainEventRow.id))) == 2
        assert (
            await session.scalar(
                select(func.count(DomainEventHandlerExecution.id)).where(
                    DomainEventHandlerExecution.handler_name
                    == SEMANTIC_GUIDELINE_PROJECTION_HANDLER
                )
            )
            == 2
        )
    await engine.dispose()


def test_semantic_context_envelope_is_strict_and_reserves_authority_fields():
    node = _desired_semantic_node(
        kind="revision",
        identity="revision-reserved-fields",
        digest="a" * 64,
        generation=1,
        active=True,
        reason=None,
        successor_id=None,
        title="Reserved fields",
        content="Reserved fields remain projector-owned.",
        payload={
            "contract": "untrusted-contract",
            "kind": "skip",
            "authority_digest": "b" * 64,
            "revision_id": "revision-reserved-fields",
        },
        created_at=NOW,
        projected_at=NOW,
    )

    assert node.attrs["context"].startswith("json:")
    context = _decode_semantic_context(node.attrs["context"])
    assert context["contract"] == SEMANTIC_GUIDELINE_KG_CONTRACT
    assert context["kind"] == "revision"
    assert context["authority_digest"] == "a" * 64
    assert node.attrs["source_content_hash"] is None

    with pytest.raises(
        PolicyConstraintProjectionConflict,
        match="semantic_guideline_graph_context_invalid",
    ):
        _decode_semantic_context("{contract: semantic-guideline-kg/v1, kind: revision}")

    for invalid_json in (
        '{"contract":"semantic-guideline-kg/v1",'
        '"contract":"semantic-guideline-kg/v1","kind":"revision"}',
        '{"contract":"semantic-guideline-kg/v1","kind":"revision","score":NaN}',
    ):
        with pytest.raises(
            PolicyConstraintProjectionConflict,
            match="semantic_guideline_graph_context_invalid",
        ):
            _decode_semantic_context(invalid_json)

    with pytest.raises(
        PolicyConstraintProjectionConflict,
        match="semantic_guideline_graph_context_invalid",
    ):
        _desired_semantic_node(
            kind="revision",
            identity="revision-non-finite-payload",
            digest="a" * 64,
            generation=1,
            active=True,
            reason=None,
            successor_id=None,
            title="Non-finite payload",
            content="Non-finite JSON values must fail closed.",
            payload={"score": float("nan")},
            created_at=NOW,
            projected_at=NOW,
        )


@pytest.mark.asyncio
async def test_semantic_projection_kuzu_round_trip_is_replay_safe_and_not_audit_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Exercise the context codec and provenance split through real Kuzu."""

    board_id = "semantic-projection-kuzu-round-trip"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()
    try:
        kg_runtime.bootstrap_board_graph(board_id)
        projector = CommunitySqlAlchemyPolicyConstraintProjection(
            graph_transaction_resolver=CommunityKuzuGraphTransaction
        )
        desired = _desired_chain()

        first = await projector._reconcile(  # noqa: SLF001
            board_id=board_id,
            operation="sync",
            event_id="event-kuzu-first",
            desired=desired,
            projected_at=NOW,
        )
        replay = await projector._reconcile(  # noqa: SLF001
            board_id=board_id,
            operation="sync",
            event_id="event-kuzu-replay",
            desired=desired,
            projected_at=NOW,
        )

        assert first.active_count == len(desired)
        assert first.activated_count == len(desired)
        assert replay.replayed is True
        assert replay.activated_count == replay.ended_count == 0

        with kg_runtime.open_board_connection(board_id) as (_db, connection):
            result = connection.execute(
                "MATCH (n:Entity) RETURN n.id, n.context, n.source_content_hash"
            )
            rows = []
            while result.has_next():
                rows.append(result.get_next())
            result.close()

        desired_by_id = {node.node_id: node for node in desired}
        semantic_rows = [
            row for row in rows if str(row[0]).startswith("semantic-guideline:")
        ]
        assert len(semantic_rows) == len(desired)
        for node_id, encoded_context, source_content_hash in semantic_rows:
            assert isinstance(encoded_context, str)
            assert encoded_context.startswith("json:")
            context = json.loads(encoded_context.removeprefix("json:"))
            assert context["contract"] == SEMANTIC_GUIDELINE_KG_CONTRACT
            assert context["authority_digest"] == desired_by_id[str(node_id)].digest
            assert source_content_hash is None
    finally:
        kg_runtime.close_all_connections(board_id)
        kg_runtime.reset_bootstrap_cache_for_tests()
