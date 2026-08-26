"""M-PULSE-1 contract tests for the real Okto Grafx transaction provider.

This first batch deliberately stops at the structured ``GraphTransactionScope``
surface.  Generic statements remain unavailable until M-PULSE-2, while schema
bootstrap, Spec lineage and projection active-set reconciliation belong to later
milestones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import okto_grafx
import pytest
from okto_grafx import Database, Timestamp, Transaction
from okto_grafx.errors import (
    GrafxBufferBudgetExceeded,
    GrafxCorruptionDetected,
    GrafxDeviceFull,
    GrafxError,
    GrafxLeaseTimeout,
    GrafxLedgerError,
    GrafxQuarantineError,
    GrafxStorageError,
    GrafxUnsupportedOperation,
    GrafxWriteConflict,
)
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphCorruption,
    GraphError,
    GraphLockContention,
    GraphUnavailable,
)
from okto_pulse.core.kg.interfaces.graph_transaction import (
    GraphNodePropertyBeforeImage,
    GraphTransaction,
    GraphTransactionScope,
)

from okto_pulse.community.adapters.graph_memory_pressure import GraphMemoryPressure

BOARD_ID = "grafx-provider-board"
INJECTED_POST_STAGE_FAILURE = "injected failure after staging the node swap"
NODE_TYPES = ("Entity", "Decision")
RELATIONSHIP_PAIRS = (("supports", "Entity", "Entity"),)
NODE_COLUMNS = (
    "id",
    "source_session_id",
    "title",
    "content",
    "score",
    "active",
    "attestation_count",
    "last_attested_at",
    "superseded_by",
    "superseded_at",
    "revocation_reason",
    "context",
    "justification",
    "source_artifact_ref",
    "graph_layer",
    "maturity_status",
    "created_by_agent",
    "source_confidence",
    "relevance_score",
    "query_hits",
    "priority_boost",
    "human_curated",
    "generation",
    "source_span_quote",
    "source_content_hash",
    "created_at",
    "embedding",
)


@dataclass
class _DeterministicFence:
    """Controllable Pulse authority used to prove checks happen before effects."""

    allowed: bool = True
    calls: list[tuple[str, str]] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)

    def __call__(self, board_id: str, phase: str) -> None:
        self.calls.append((board_id, phase))
        self.trace.append("fence")
        if not self.allowed:
            raise GraphLockContention(
                "test graph write fence lost",
                details={"board_id": board_id, "phase": phase},
            )


class _InjectedProcessSignal(BaseException):
    """Non-Exception control-flow signal injected after a staged mutation."""


class _InjectedCleanupFailure(RuntimeError):
    """Deterministic rollback refusal used to prove primary-signal identity."""


@pytest.fixture
def grafx_database(tmp_path: Path) -> Any:
    """Open a real durable Grafx database with only this batch's tiny schema."""

    database = okto_grafx.connect(tmp_path / "grafx-board")
    with database.begin("write") as schema:
        schema.execute(
            "CREATE VECTOR SPACE pulse_test {dimension: 4, metric: 'cosine'}"
        )
        for node_type in NODE_TYPES:
            schema.execute(
                f"CREATE NODE TABLE {node_type}("
                "id STRING, source_session_id STRING, title STRING, "
                "content STRING, score DOUBLE, active BOOL, "
                "attestation_count INT64, last_attested_at TIMESTAMP, "
                "superseded_by STRING, superseded_at TIMESTAMP, "
                "revocation_reason STRING, context STRING, justification STRING, "
                "source_artifact_ref STRING, graph_layer STRING, "
                "maturity_status STRING, created_by_agent STRING, "
                "source_confidence DOUBLE, relevance_score DOUBLE, "
                "query_hits INT64, priority_boost DOUBLE, human_curated BOOL, "
                "generation INT64, source_span_quote STRING, "
                "source_content_hash STRING, created_at TIMESTAMP, "
                "embedding VECTOR(pulse_test), PRIMARY KEY(id))"
            )
        schema.execute(
            "CREATE REL TABLE supports("
            "FROM Entity TO Entity, confidence DOUBLE, "
            "created_by_session_id STRING, rule_id STRING)"
        )
        schema.execute(
            "CREATE REL TABLE hidden_supports(FROM Entity TO Entity, note STRING)"
        )
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def fence() -> _DeterministicFence:
    return _DeterministicFence()


def _provider(database: Any, fence: _DeterministicFence) -> Any:
    # Keep this import local so the RED suite reports individual missing-provider
    # failures instead of stopping collection before the contract can be counted.
    from okto_pulse.community.adapters.grafx_graph_transaction import (
        CommunityGrafxGraphTransaction,
    )

    def resolve_database(board_id: str) -> Any:
        if board_id != BOARD_ID:
            raise KeyError(board_id)
        return database

    return CommunityGrafxGraphTransaction(
        database_resolver=resolve_database,
        revalidate_fence=fence,
        node_types=NODE_TYPES,
        relationship_pairs=RELATIONSHIP_PAIRS,
    )


def _node(database: Any, node_id: str) -> tuple[Any, ...] | None:
    projection = ", ".join(f"n.{name}" for name in NODE_COLUMNS)
    rows = database.execute(
        f"MATCH (n:Entity {{id: $node_id}}) RETURN {projection}",
        {"node_id": node_id},
    ).rows
    assert len(rows) <= 1
    return rows[0] if rows else None


def _nodes(database: Any) -> tuple[tuple[Any, ...], ...]:
    rows: list[tuple[Any, ...]] = []
    for node_type in NODE_TYPES:
        typed = database.execute(
            f"MATCH (n:{node_type}) RETURN n.id, n.source_session_id, n.title"
        ).rows
        rows.extend((node_type, *row) for row in typed)
    return tuple(sorted(rows))


def _edges(database: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        sorted(
            database.execute(
                "MATCH (a:Entity)-[r:supports]->(b:Entity) "
                "RETURN a.id, b.id, r.confidence, "
                "r.created_by_session_id, r.rule_id"
            ).rows
        )
    )


def _hidden_edges(database: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        sorted(
            database.execute(
                "MATCH (a:Entity)-[r:hidden_supports]->(b:Entity) "
                "RETURN a.id, b.id, r.note"
            ).rows
        )
    )


def _graph_state(database: Any) -> tuple[object, object]:
    return _nodes(database), _edges(database)


async def _seed_graph(provider: Any) -> None:
    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "entity-1",
            {
                "title": "before",
                "content": "private payload",
                "score": 1.25,
                "active": True,
                "attestation_count": 4,
            },
            source_session_id="node-remove",
        )
        scope.create_node(
            "Entity",
            "entity-2",
            {"title": "peer", "score": 2.0, "active": True},
            source_session_id="node-keep",
        )
        scope.create_node(
            "Entity",
            "entity-3",
            {"title": "incoming", "score": 3.0, "active": True},
            source_session_id="node-remove",
        )
        assert scope.create_edge(
            "supports",
            "Entity",
            "Entity",
            "entity-1",
            "entity-2",
            {
                "confidence": 0.7,
                "created_by_session_id": "edge-remove",
                "rule_id": "rule/outgoing/1",
            },
        )
        assert scope.create_edge(
            "supports",
            "Entity",
            "Entity",
            "entity-1",
            "entity-1",
            {
                "confidence": 1.0,
                "created_by_session_id": "edge-keep",
                "rule_id": "rule/self-loop",
            },
        )
        assert scope.create_edge(
            "supports",
            "Entity",
            "Entity",
            "entity-1",
            "entity-2",
            {
                "confidence": 0.9,
                "created_by_session_id": "edge-keep",
                "rule_id": "rule/outgoing/2",
            },
        )
        assert scope.create_edge(
            "supports",
            "Entity",
            "Entity",
            "entity-3",
            "entity-1",
            {
                "confidence": 0.8,
                "created_by_session_id": "edge-keep",
                "rule_id": "rule/incoming",
            },
        )


async def _seed_tombstone_graph(provider: Any) -> None:
    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "tombstone-target",
            {
                "title": "Private title",
                "content": "Private content",
                "context": "Private context",
                "justification": "Private justification",
                "score": 9.0,
                "active": True,
                "source_artifact_ref": "spec:deleted:fr:private",
                "graph_layer": "canonical",
                "maturity_status": "canonical_mature",
                "created_by_agent": "system:deterministic",
                "source_confidence": 0.8,
                "relevance_score": 0.9,
                "query_hits": 12,
                "priority_boost": 0.4,
                "human_curated": True,
                "generation": 7,
                "source_span_quote": "Private source quote",
                "source_content_hash": "private-hash",
                "created_at": "2026-08-26T09:30:00Z",
                "embedding": [1.0, 0.0, 0.0, 0.0],
            },
            source_session_id="tombstone-source-session",
        )
        for node_id in ("tombstone-peer", "tombstone-incoming"):
            scope.create_node(
                "Entity",
                node_id,
                {"title": node_id},
                source_session_id="tombstone-control-session",
            )
        for from_id, to_id, rule_id, confidence in (
            ("tombstone-target", "tombstone-target", "tombstone/self", 1.0),
            ("tombstone-target", "tombstone-peer", "tombstone/out/1", 0.7),
            ("tombstone-target", "tombstone-peer", "tombstone/out/2", 0.9),
            ("tombstone-incoming", "tombstone-target", "tombstone/in", 0.8),
            ("tombstone-peer", "tombstone-incoming", "tombstone/unrelated", 0.2),
        ):
            assert scope.create_edge(
                "supports",
                "Entity",
                "Entity",
                from_id,
                to_id,
                {
                    "confidence": confidence,
                    "created_by_session_id": "tombstone-edge-session",
                    "rule_id": rule_id,
                },
            )
        assert scope.create_edge(
            "hidden_supports",
            "Entity",
            "Entity",
            "tombstone-target",
            "tombstone-peer",
            {"note": "unconfigured incident edge"},
        )


@pytest.mark.asyncio
async def test_begin_and_commit_publish_one_real_grafx_transaction(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    assert isinstance(provider, GraphTransaction)

    scope = await provider.begin(BOARD_ID)
    assert isinstance(scope, GraphTransactionScope)
    scope.create_node(
        "Entity",
        "committed",
        {"title": "durable"},
        source_session_id="session-commit",
    )
    assert _node(grafx_database, "committed") is None

    await scope.commit()
    await scope.commit()

    assert _node(grafx_database, "committed")[:3] == (
        "committed",
        "session-commit",
        "durable",
    )
    # The injected Database is provider-owned infrastructure, not scope-owned.
    assert grafx_database.closed is False
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_rollback_discards_the_real_grafx_transaction(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    scope = await _provider(grafx_database, fence).begin(BOARD_ID)
    scope.create_node(
        "Entity",
        "rolled-back",
        {"title": "must not publish"},
        source_session_id="session-rollback",
    )

    await scope.rollback()
    await scope.rollback()

    assert _node(grafx_database, "rolled-back") is None
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_async_context_manager_commits_normally_and_rolls_back_on_error(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)

    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "context-commit",
            {"title": "kept"},
            source_session_id="context-session",
        )

    class _AbortScope(RuntimeError):
        pass

    with pytest.raises(_AbortScope):
        async with await provider.begin(BOARD_ID) as scope:
            scope.create_node(
                "Entity",
                "context-rollback",
                {"title": "discarded"},
                source_session_id="context-session",
            )
            raise _AbortScope("abort the scope")

    assert _node(grafx_database, "context-commit") is not None
    assert _node(grafx_database, "context-rollback") is None


@pytest.mark.asyncio
async def test_async_context_rolls_back_when_the_commit_fence_fails(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    entered_scope: Any | None = None

    with pytest.raises(GraphLockContention):
        async with await provider.begin(BOARD_ID) as scope:
            entered_scope = scope
            scope.create_node(
                "Entity",
                "fence-failed-on-exit",
                {"title": "must be rolled back"},
                source_session_id="fence-failed-session",
            )
            fence.allowed = False

    assert entered_scope is not None
    assert entered_scope._transaction.active is False
    fence.allowed = True
    assert _node(grafx_database, "fence-failed-on-exit") is None


@pytest.mark.asyncio
async def test_pending_nodes_edges_existence_and_type_lookup_share_owner_view(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)

    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "from-pending",
            {"title": "from"},
            source_session_id="session-overlay",
        )
        scope.create_node(
            "Entity",
            "to-pending",
            {"title": "to"},
            source_session_id="session-overlay",
        )
        scope.create_node(
            "Decision",
            "typed-decision",
            {"title": "decision"},
            source_session_id="session-overlay",
        )

        assert scope.find_node_types("from-pending") == ("Entity",)
        assert scope.find_node_types("typed-decision") == ("Decision",)
        assert scope.find_node_types("missing") == ()
        assert scope.create_edge(
            "supports",
            "Entity",
            "Entity",
            "from-pending",
            "to-pending",
            {
                "confidence": 0.75,
                "created_by_session_id": "session-overlay",
                "rule_id": "overlay/rule",
            },
        )
        assert scope.edge_exists(
            "supports",
            "Entity",
            "Entity",
            "from-pending",
            "to-pending",
        )
        assert scope.edge_exists(
            "supports",
            "Entity",
            "Entity",
            "from-pending",
            "to-pending",
            "overlay/rule",
        )
        assert not scope.edge_exists(
            "supports",
            "Entity",
            "Entity",
            "from-pending",
            "to-pending",
            "different/rule",
        )
        assert not scope.create_edge(
            "supports",
            "Entity",
            "Entity",
            "from-pending",
            "missing",
            {"created_by_session_id": "session-overlay"},
        )

        # No staged state is visible through an independent read transaction.
        assert _node(grafx_database, "from-pending") is None

    assert _edges(grafx_database) == (
        (
            "from-pending",
            "to-pending",
            0.75,
            "session-overlay",
            "overlay/rule",
        ),
    )


@pytest.mark.asyncio
async def test_update_snapshot_and_restore_use_exact_before_image(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "before-image",
            {"title": "v1", "content": "original", "score": 1.0},
            source_session_id="before-image-session",
        )

    async with await provider.begin(BOARD_ID) as scope:
        scope.update_node(
            "Entity",
            "before-image",
            {"title": "v2", "score": 2.0},
        )
        updated = scope.snapshot_node_properties(
            "Entity",
            "before-image",
            ("title", "content", "score"),
        )
        assert updated == GraphNodePropertyBeforeImage(
            node_type="Entity",
            node_id="before-image",
            attrs={"title": "v2", "content": "original", "score": 2.0},
        )

    assert _node(grafx_database, "before-image")[2:5] == (
        "v2",
        "original",
        2.0,
    )

    async with await provider.begin(BOARD_ID) as scope:
        before = scope.snapshot_node_properties(
            "Entity",
            "before-image",
            ("title", "content", "score"),
        )
        assert before is not None
        scope.update_node(
            "Entity",
            "before-image",
            {"title": "transient", "content": "changed", "score": 99.0},
        )
        scope.restore_node_properties(before)
        assert (
            scope.snapshot_node_properties(
                "Entity",
                "before-image",
                ("title", "content", "score"),
            )
            == before
        )
        assert (
            scope.snapshot_node_properties(
                "Entity",
                "absent",
                ("title",),
            )
            is None
        )

    assert _node(grafx_database, "before-image")[2:5] == (
        "v2",
        "original",
        2.0,
    )


@pytest.mark.asyncio
async def test_replace_payload_is_exact_and_preserves_incident_edge_multiset(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_graph(provider)
    original_node = _node(grafx_database, "entity-1")
    original_edges = _edges(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        assert not scope.replace_node_payload(
            "Entity",
            "absent",
            {"title": "ignored"},
            source_session_id="replacement-session",
        )
        assert scope.replace_node_payload(
            "Entity",
            "entity-1",
            {"title": "after", "score": 9.5},
            source_session_id="replacement-session",
        )
        replaced = scope.snapshot_node_properties(
            "Entity",
            "entity-1",
            NODE_COLUMNS,
        )
        assert replaced is not None
        replaced_values = tuple(replaced.attrs[name] for name in NODE_COLUMNS)
        assert replaced_values[:11] == (
            "entity-1",
            "replacement-session",
            "after",
            None,
            9.5,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        assert replaced_values[11:] == (None,) * (len(NODE_COLUMNS) - 11)
        assert scope.edge_exists(
            "supports",
            "Entity",
            "Entity",
            "entity-1",
            "entity-2",
            "rule/outgoing/1",
        )
        assert scope.edge_exists(
            "supports",
            "Entity",
            "Entity",
            "entity-3",
            "entity-1",
            "rule/incoming",
        )

        # Replacement remains owner-only until the scope commits.
        assert _node(grafx_database, "entity-1") == original_node
        assert _edges(grafx_database) == original_edges

    assert _node(grafx_database, "entity-1")[:11] == (
        "entity-1",
        "replacement-session",
        "after",
        None,
        9.5,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    assert _edges(grafx_database) == original_edges
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_replace_payload_proves_edges_from_the_catalog_not_stale_configuration(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfigured physical relation remains part of the safety proof."""

    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "catalog-proof-source",
            {"title": "before"},
            source_session_id="catalog-proof-session",
        )
        scope.create_node(
            "Entity",
            "catalog-proof-target",
            {"title": "target"},
            source_session_id="catalog-proof-session",
        )
        assert scope.create_edge(
            "hidden_supports",
            "Entity",
            "Entity",
            "catalog-proof-source",
            "catalog-proof-target",
            {"note": "not present in relationship_pairs"},
        )

    assert _hidden_edges(grafx_database) == (
        (
            "catalog-proof-source",
            "catalog-proof-target",
            "not present in relationship_pairs",
        ),
    )
    scope = await provider.begin(BOARD_ID)
    original_execute = Transaction.execute
    damaged = False

    def damage_unconfigured_edge_after_payload_set(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal damaged
        result = original_execute(transaction, statement, params)
        if (
            not damaged
            and statement.startswith("MATCH (n:Entity)")
            and " SET " in statement
        ):
            damaged = True
            original_execute(
                transaction,
                "MATCH (a:Entity)-[r:hidden_supports]->(b:Entity) "
                "WHERE a.id = $source_id AND b.id = $target_id DELETE r",
                {
                    "source_id": "catalog-proof-source",
                    "target_id": "catalog-proof-target",
                },
            )
        return result

    monkeypatch.setattr(
        Transaction, "execute", damage_unconfigured_edge_after_payload_set
    )
    try:
        with pytest.raises(GraphError) as refused:
            scope.replace_node_payload(
                "Entity",
                "catalog-proof-source",
                {"title": "after"},
                source_session_id="replacement-session",
            )
        assert refused.value.details["payload_confirmed"] is True
        assert refused.value.details["edges_confirmed"] is False
        assert damaged is True
    finally:
        await scope.rollback()

    assert _node(grafx_database, "catalog-proof-source")[2] == "before"
    assert len(_hidden_edges(grafx_database)) == 1


@pytest.mark.asyncio
async def test_source_deleted_tombstone_erases_payload_and_all_incident_edges(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_tombstone_graph(provider)
    committed_before = _node(grafx_database, "tombstone-target")
    edges_before = _edges(grafx_database)
    hidden_before = _hidden_edges(grafx_database)
    assert committed_before is not None
    assert len(edges_before) == 5
    assert len(hidden_before) == 1

    scope = await provider.begin(BOARD_ID)
    assert not scope.replace_with_source_deleted_tombstone(
        "Entity",
        "absent-tombstone",
        graph_layer="working",
        maturity_status="working_stale",
        revocation_reason="source_deleted",
        relevance_score=0.0,
    )
    assert scope.replace_with_source_deleted_tombstone(
        "Entity",
        "tombstone-target",
        graph_layer="working",
        maturity_status="working_stale",
        revocation_reason="source_deleted",
        relevance_score=0.0,
    )

    snapshot = scope.snapshot_node_properties(
        "Entity",
        "tombstone-target",
        NODE_COLUMNS,
    )
    assert snapshot is not None
    expected = {name: None for name in NODE_COLUMNS}
    expected.update(
        {
            "id": "tombstone-target",
            "source_session_id": "tombstone-source-session",
            "title": "",
            "content": "",
            "revocation_reason": "source_deleted",
            "context": "",
            "justification": "",
            "source_artifact_ref": "spec:deleted:fr:private",
            "graph_layer": "working",
            "maturity_status": "working_stale",
            "created_by_agent": "system:deterministic",
            "source_confidence": 0.0,
            "relevance_score": 0.0,
            "query_hits": 0,
            "priority_boost": 0.0,
            "human_curated": False,
            "generation": 7,
            "source_span_quote": "",
            "created_at": "2026-08-26T09:30:00.000000Z",
        }
    )
    assert snapshot.attrs == expected
    assert not scope.edge_exists(
        "supports",
        "Entity",
        "Entity",
        "tombstone-target",
        "tombstone-peer",
    )
    assert not scope.edge_exists(
        "hidden_supports",
        "Entity",
        "Entity",
        "tombstone-target",
        "tombstone-peer",
    )
    assert scope.edge_exists(
        "supports",
        "Entity",
        "Entity",
        "tombstone-peer",
        "tombstone-incoming",
        "tombstone/unrelated",
    )
    assert _node(grafx_database, "tombstone-target") == committed_before
    assert _edges(grafx_database) == edges_before
    assert _hidden_edges(grafx_database) == hidden_before

    await scope.commit()

    assert _node(grafx_database, "tombstone-target") != committed_before
    assert _edges(grafx_database) == (
        (
            "tombstone-peer",
            "tombstone-incoming",
            0.2,
            "tombstone-edge-session",
            "tombstone/unrelated",
        ),
    )
    assert _hidden_edges(grafx_database) == ()
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_source_deleted_tombstone_rolls_back_payload_vector_and_edges(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_tombstone_graph(provider)
    before = (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    )

    scope = await provider.begin(BOARD_ID)
    assert scope.replace_with_source_deleted_tombstone(
        "Entity",
        "tombstone-target",
        graph_layer="working",
        maturity_status="working_stale",
        revocation_reason="source_deleted",
        relevance_score=0.0,
    )
    assert (
        scope.snapshot_node_properties(
            "Entity",
            "tombstone-target",
            ("embedding",),
        ).attrs["embedding"]
        is None
    )
    await scope.rollback()

    assert (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    ) == before
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_source_deleted_tombstone_rejects_incomplete_schema_before_mutation(
    tmp_path: Path,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = okto_grafx.connect(tmp_path / "grafx-incomplete-tombstone-schema")
    try:
        with database.begin("write") as schema:
            schema.execute(
                "CREATE NODE TABLE Entity("
                "id STRING, source_session_id STRING, title STRING, content STRING, "
                "context STRING, justification STRING, source_artifact_ref STRING, "
                "graph_layer STRING, maturity_status STRING, created_at TIMESTAMP, "
                "created_by_agent STRING, source_confidence DOUBLE, "
                "relevance_score DOUBLE, query_hits INT64, priority_boost DOUBLE, "
                "revocation_reason STRING, human_curated BOOL, generation INT64, "
                "source_span_quote STRING, source_content_hash STRING, PRIMARY KEY(id))"
            )
        provider = _provider(database, fence)
        async with await provider.begin(BOARD_ID) as seed:
            seed.create_node(
                "Entity",
                "incomplete-schema-target",
                {"title": "Private title", "content": "Private content"},
                source_session_id="incomplete-schema-session",
            )
        before = database.execute(
            "MATCH (n:Entity {id: $node_id}) "
            "RETURN n.id, n.source_session_id, n.title, n.content",
            {"node_id": "incomplete-schema-target"},
        ).rows

        scope = await provider.begin(BOARD_ID)
        original_execute = Transaction.execute
        mutating_statements: list[str] = []

        def trace_mutations(
            transaction: Any,
            statement: str,
            params: dict[str, Any] | None = None,
        ) -> Any:
            if any(token in statement for token in ("CREATE ", " SET ", " DELETE ")):
                mutating_statements.append(statement)
            return original_execute(transaction, statement, params)

        monkeypatch.setattr(Transaction, "execute", trace_mutations)
        try:
            with pytest.raises(GraphCapabilityUnavailable) as refused:
                scope.replace_with_source_deleted_tombstone(
                    "Entity",
                    "incomplete-schema-target",
                    graph_layer="working",
                    maturity_status="working_stale",
                    revocation_reason="source_deleted",
                    relevance_score=0.0,
                )
            assert refused.value.details["missing_properties"] == ("embedding",)
            assert mutating_statements == []
        finally:
            await scope.rollback()

        assert (
            database.execute(
                "MATCH (n:Entity {id: $node_id}) "
                "RETURN n.id, n.source_session_id, n.title, n.content",
                {"node_id": "incomplete-schema-target"},
            ).rows
            == before
        )
    finally:
        database.close()


@pytest.mark.asyncio
async def test_source_deleted_tombstone_apply_then_raise_poison_rolls_back(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_tombstone_graph(provider)
    before = (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    )
    scope = await provider.begin(BOARD_ID)
    original_execute = Transaction.execute
    applied_mutations = 0

    def apply_then_raise(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal applied_mutations
        result = original_execute(transaction, statement, params)
        if "DETACH DELETE n CREATE" in statement:
            applied_mutations += 1
            raise GrafxStorageError(INJECTED_POST_STAGE_FAILURE)
        return result

    monkeypatch.setattr(Transaction, "execute", apply_then_raise)
    with pytest.raises(GraphUnavailable):
        scope.replace_with_source_deleted_tombstone(
            "Entity",
            "tombstone-target",
            graph_layer="working",
            maturity_status="working_stale",
            revocation_reason="source_deleted",
            relevance_score=0.0,
        )

    assert applied_mutations == 1
    assert scope._finished is True
    assert scope._transaction.active is False
    await scope.commit()
    assert (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    ) == before
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_source_deleted_tombstone_preserves_process_signal_identity(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_tombstone_graph(provider)
    before = (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    )
    scope = await provider.begin(BOARD_ID)
    original_execute = Transaction.execute
    primary_signal = _InjectedProcessSignal()
    applied_mutations = 0

    def apply_then_signal(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal applied_mutations
        result = original_execute(transaction, statement, params)
        if "DETACH DELETE n CREATE" in statement:
            applied_mutations += 1
            raise primary_signal
        return result

    monkeypatch.setattr(Transaction, "execute", apply_then_signal)
    with pytest.raises(_InjectedProcessSignal) as escaped:
        scope.replace_with_source_deleted_tombstone(
            "Entity",
            "tombstone-target",
            graph_layer="working",
            maturity_status="working_stale",
            revocation_reason="source_deleted",
            relevance_score=0.0,
        )

    assert escaped.value is primary_signal
    assert applied_mutations == 1
    assert scope._finished is True
    assert scope._transaction.active is False
    await scope.commit()
    assert (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    ) == before
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_source_deleted_tombstone_cleanup_failure_preserves_primary_signal(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_tombstone_graph(provider)
    before = (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    )
    scope = await provider.begin(BOARD_ID)
    raw_transaction = scope._transaction
    original_execute = Transaction.execute
    original_rollback = Transaction.rollback
    primary_signal = _InjectedProcessSignal()
    cleanup_failure = _InjectedCleanupFailure()
    applied_mutations = 0

    def apply_then_signal(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal applied_mutations
        result = original_execute(transaction, statement, params)
        if "DETACH DELETE n CREATE" in statement:
            applied_mutations += 1
            raise primary_signal
        return result

    def refuse_cleanup(transaction: Any) -> None:
        if transaction is raw_transaction:
            raise cleanup_failure
        original_rollback(transaction)

    monkeypatch.setattr(Transaction, "execute", apply_then_signal)
    monkeypatch.setattr(Transaction, "rollback", refuse_cleanup)
    try:
        with pytest.raises(_InjectedProcessSignal) as escaped:
            scope.replace_with_source_deleted_tombstone(
                "Entity",
                "tombstone-target",
                graph_layer="working",
                maturity_status="working_stale",
                revocation_reason="source_deleted",
                relevance_score=0.0,
            )
        await scope.commit()
        assert scope._finished is True
    finally:
        monkeypatch.setattr(Transaction, "rollback", original_rollback)
        if raw_transaction.active:
            original_rollback(raw_transaction)

    assert escaped.value is primary_signal
    assert (
        escaped.value.__cause__ is cleanup_failure
        or escaped.value.__context__ is cleanup_failure
    )
    assert applied_mutations == 1
    assert (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    ) == before
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_source_deleted_tombstone_confirmation_mismatch_poison_rolls_back(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_tombstone_graph(provider)
    before = (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    )
    scope = await provider.begin(BOARD_ID)
    scope_type = type(scope)
    original_snapshot = scope_type._incident_edge_snapshot

    def force_confirmation_mismatch(
        transaction_scope: Any,
        node_type: str,
        node_id: str,
    ) -> Any:
        snapshot = original_snapshot(transaction_scope, node_type, node_id)
        if node_id == "tombstone-target":
            return {("forced-unremoved-edge",): 1}
        return snapshot

    monkeypatch.setattr(
        scope_type,
        "_incident_edge_snapshot",
        force_confirmation_mismatch,
    )
    with pytest.raises(GraphError) as refused:
        scope.replace_with_source_deleted_tombstone(
            "Entity",
            "tombstone-target",
            graph_layer="working",
            maturity_status="working_stale",
            revocation_reason="source_deleted",
            relevance_score=0.0,
        )

    assert refused.value.details["payload_confirmed"] is True
    assert refused.value.details["edges_removed"] is False
    assert scope._finished is True
    assert scope._transaction.active is False
    await scope.commit()
    assert (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    ) == before
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_source_deleted_tombstone_committed_retry_is_idempotent(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_tombstone_graph(provider)

    async with await provider.begin(BOARD_ID) as scope:
        assert scope.replace_with_source_deleted_tombstone(
            "Entity",
            "tombstone-target",
            graph_layer="working",
            maturity_status="working_stale",
            revocation_reason="source_deleted",
            relevance_score=0.0,
        )
    after_first_attempt = (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    )

    async with await provider.begin(BOARD_ID) as retry:
        assert retry.replace_with_source_deleted_tombstone(
            "Entity",
            "tombstone-target",
            graph_layer="working",
            maturity_status="working_stale",
            revocation_reason="source_deleted",
            relevance_score=0.0,
        )

    assert (
        _node(grafx_database, "tombstone-target"),
        _edges(grafx_database),
        _hidden_edges(grafx_database),
    ) == after_first_attempt
    assert grafx_database.verify("all").findings == ()


@pytest.mark.asyncio
async def test_source_deleted_tombstone_uses_one_mutating_statement(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_tombstone_graph(provider)
    scope = await provider.begin(BOARD_ID)
    original_execute = Transaction.execute
    mutating_statements: list[str] = []

    def trace_mutations(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if any(token in statement for token in ("CREATE ", " SET ", " DELETE ")):
            mutating_statements.append(statement)
        return original_execute(transaction, statement, params)

    monkeypatch.setattr(Transaction, "execute", trace_mutations)
    try:
        assert scope.replace_with_source_deleted_tombstone(
            "Entity",
            "tombstone-target",
            graph_layer="working",
            maturity_status="working_stale",
            revocation_reason="source_deleted",
            relevance_score=0.0,
        )
        assert len(mutating_statements) == 1
        assert "DETACH DELETE n CREATE (t:Entity" in mutating_statements[0]
        assert mutating_statements[0].endswith("RETURN t.id")
    finally:
        await scope.rollback()


@pytest.mark.asyncio
async def test_session_cleanup_deletes_exact_edges_then_detaches_session_nodes(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_graph(provider)

    async with await provider.begin(BOARD_ID) as scope:
        scope.delete_edges_by_session("edge-remove")
        assert not scope.edge_exists(
            "supports",
            "Entity",
            "Entity",
            "entity-1",
            "entity-2",
            "rule/outgoing/1",
        )
        assert scope.edge_exists(
            "supports",
            "Entity",
            "Entity",
            "entity-1",
            "entity-2",
            "rule/outgoing/2",
        )
        assert (
            scope.delete_nodes_by_session(
                "node-remove",
                ("Entity", "Decision"),
            )
            == ()
        )
        assert scope.find_node_types("entity-1") == ()
        assert scope.find_node_types("entity-2") == ("Entity",)
        assert scope.find_node_types("entity-3") == ()

    assert _nodes(grafx_database) == (("Entity", "entity-2", "node-keep", "peer"),)
    assert _edges(grafx_database) == ()
    assert grafx_database.verify("all").findings == ()


def _same_instant(actual: object, expected: str) -> bool:
    if isinstance(actual, Timestamp):
        actual_datetime = datetime.fromtimestamp(
            actual.micros / 1_000_000,
            tz=UTC,
        )
    elif isinstance(actual, datetime):
        actual_datetime = actual
    else:
        actual_datetime = datetime.fromisoformat(str(actual))
    expected_datetime = datetime.fromisoformat(expected)
    return actual_datetime == expected_datetime


@pytest.mark.asyncio
async def test_increment_attestation_uses_pulse_default_and_iso_timestamp(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "attestation-default",
            {"title": "default"},
            source_session_id="attestation-session",
        )
        scope.create_node(
            "Entity",
            "attestation-existing",
            {"title": "existing", "attestation_count": 7},
            source_session_id="attestation-session",
        )

    first_at = "2026-08-26T10:20:30Z"
    second_at = "2026-08-26T11:22:33+00:00"
    async with await provider.begin(BOARD_ID) as scope:
        scope.increment_attestation(
            "Entity",
            "attestation-default",
            attested_at=first_at,
        )
        scope.increment_attestation(
            "Entity",
            "attestation-existing",
            attested_at=first_at,
        )
        scope.increment_attestation(
            "Entity",
            "attestation-default",
            attested_at=second_at,
        )
        default = scope.snapshot_node_properties(
            "Entity",
            "attestation-default",
            ("attestation_count", "last_attested_at"),
        )
        existing = scope.snapshot_node_properties(
            "Entity",
            "attestation-existing",
            ("attestation_count", "last_attested_at"),
        )
        assert default is not None
        assert existing is not None
        assert default.attrs["attestation_count"] == 3
        assert _same_instant(default.attrs["last_attested_at"], second_at)
        assert existing.attrs["attestation_count"] == 8
        assert _same_instant(existing.attrs["last_attested_at"], first_at)

    assert _node(grafx_database, "attestation-default")[6] == 3
    assert _same_instant(
        _node(grafx_database, "attestation-default")[7],
        second_at,
    )


@pytest.mark.asyncio
async def test_mark_superseded_converts_iso_timestamp_and_round_trips(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "superseded",
            {"title": "old"},
            source_session_id="supersedence-session",
        )

    superseded_at = "2026-08-26T12:34:56.123456-03:00"
    async with await provider.begin(BOARD_ID) as scope:
        scope.mark_superseded(
            "Entity",
            "superseded",
            superseded_by="replacement",
            superseded_at=superseded_at,
            revocation_reason="source changed",
        )
        snapshot = scope.snapshot_node_properties(
            "Entity",
            "superseded",
            ("superseded_by", "superseded_at", "revocation_reason"),
        )
        assert snapshot is not None
        assert snapshot.attrs["superseded_by"] == "replacement"
        assert _same_instant(snapshot.attrs["superseded_at"], superseded_at)
        assert snapshot.attrs["revocation_reason"] == "source changed"

    stored = _node(grafx_database, "superseded")
    assert stored is not None
    assert stored[8] == "replacement"
    assert _same_instant(stored[9], superseded_at)
    assert stored[10] == "source changed"


@pytest.mark.asyncio
async def test_generic_execute_fails_closed_without_touching_grafx(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = await _provider(grafx_database, fence).begin(BOARD_ID)
    fence_calls_before = len(fence.calls)

    def forbidden_execute(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Grafx execute was reached behind the M-PULSE-2 fence")

    monkeypatch.setattr(Transaction, "execute", forbidden_execute)
    try:
        with pytest.raises(GraphCapabilityUnavailable) as refused:
            scope.execute("MATCH (n:Entity) RETURN n.id")
        assert refused.value.code == "graph_capability_unavailable"
        assert refused.value.details.get("capability")
        assert len(fence.calls) == fence_calls_before
    finally:
        await scope.rollback()


@pytest.mark.asyncio
async def test_begin_revalidates_fence_before_opening_engine_transaction(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database, fence)
    engine_begins = 0
    original_begin = Database.begin

    def traced_begin(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal engine_begins
        engine_begins += 1
        fence.trace.append("engine_begin")
        return original_begin(self, *args, **kwargs)

    monkeypatch.setattr(Database, "begin", traced_begin)
    fence.allowed = False
    fence.trace.clear()

    with pytest.raises(GraphLockContention):
        await provider.begin(BOARD_ID)

    assert fence.trace == ["fence"]
    assert engine_begins == 0
    assert fence.calls[-1][0] == BOARD_ID
    assert fence.calls[-1][1]


def _invoke_mutation(scope: Any, operation: str) -> object:
    if operation == "create_node":
        return scope.create_node(
            "Entity",
            "blocked-create",
            {"title": "blocked"},
            source_session_id="blocked-session",
        )
    if operation == "update_node":
        return scope.update_node("Entity", "entity-1", {"title": "blocked"})
    if operation == "replace_node_payload":
        return scope.replace_node_payload(
            "Entity",
            "entity-1",
            {"title": "blocked"},
            source_session_id="blocked-session",
        )
    if operation == "replace_with_source_deleted_tombstone":
        return scope.replace_with_source_deleted_tombstone(
            "Entity",
            "entity-1",
            graph_layer="working",
            maturity_status="working_stale",
            revocation_reason="source_deleted",
            relevance_score=0.0,
        )
    if operation == "restore_node_properties":
        return scope.restore_node_properties(
            GraphNodePropertyBeforeImage(
                node_type="Entity",
                node_id="entity-1",
                attrs={"title": "blocked"},
            )
        )
    if operation == "mark_superseded":
        return scope.mark_superseded(
            "Entity",
            "entity-1",
            superseded_by="replacement",
            superseded_at="2026-08-26T12:00:00Z",
            revocation_reason="blocked",
        )
    if operation == "create_edge":
        return scope.create_edge(
            "supports",
            "Entity",
            "Entity",
            "entity-2",
            "entity-1",
            {
                "created_by_session_id": "blocked-session",
                "rule_id": "blocked/rule",
            },
        )
    if operation == "delete_edges_by_session":
        return scope.delete_edges_by_session("edge-keep")
    if operation == "delete_nodes_by_session":
        return scope.delete_nodes_by_session("node-remove", ("Entity",))
    if operation == "increment_attestation":
        return scope.increment_attestation(
            "Entity",
            "entity-1",
            attested_at="2026-08-26T12:00:00Z",
        )
    raise AssertionError(f"unknown mutation: {operation}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    (
        "create_node",
        "update_node",
        "replace_node_payload",
        "replace_with_source_deleted_tombstone",
        "restore_node_properties",
        "mark_superseded",
        "create_edge",
        "delete_edges_by_session",
        "delete_nodes_by_session",
        "increment_attestation",
    ),
)
async def test_every_structured_mutation_revalidates_before_engine_access(
    operation: str,
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed_graph(provider)
    baseline = _graph_state(grafx_database)
    scope = await provider.begin(BOARD_ID)
    original_execute = Transaction.execute

    def traced_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        fence.trace.append("engine_execute")
        return original_execute(self, *args, **kwargs)

    monkeypatch.setattr(Transaction, "execute", traced_execute)
    fence.allowed = False
    fence.trace.clear()

    try:
        with pytest.raises(GraphLockContention):
            _invoke_mutation(scope, operation)
        assert fence.trace == ["fence"]
        assert fence.calls[-1][0] == BOARD_ID
        assert fence.calls[-1][1]
    finally:
        await scope.rollback()

    assert _graph_state(grafx_database) == baseline


@pytest.mark.asyncio
async def test_fence_loss_between_mutation_and_commit_prevents_publication(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = await _provider(grafx_database, fence).begin(BOARD_ID)
    scope.create_node(
        "Entity",
        "lost-before-commit",
        {"title": "must remain private"},
        source_session_id="lost-fence-session",
    )
    original_commit = Transaction.commit
    engine_commits = 0

    def traced_commit(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal engine_commits
        engine_commits += 1
        fence.trace.append("engine_commit")
        return original_commit(self, *args, **kwargs)

    monkeypatch.setattr(Transaction, "commit", traced_commit)
    fence.allowed = False
    fence.trace.clear()

    with pytest.raises(GraphLockContention):
        await scope.commit()

    assert fence.trace == ["fence"]
    assert engine_commits == 0
    assert _node(grafx_database, "lost-before-commit") is None
    await scope.rollback()
    assert _node(grafx_database, "lost-before-commit") is None


@pytest.mark.parametrize(
    ("failure", "expected_type"),
    (
        (GrafxWriteConflict("lost OCC validation"), GraphLockContention),
        (GrafxLeaseTimeout("writer lease timeout"), GraphLockContention),
        (GrafxBufferBudgetExceeded("buffer budget"), GraphMemoryPressure),
        (GrafxCorruptionDetected("checksum mismatch"), GraphCorruption),
        (GrafxLedgerError("ledger entry is absent"), GraphError),
        (GrafxQuarantineError("quarantine operation refused"), GraphError),
        (GrafxStorageError("device temporarily unavailable"), GraphUnavailable),
        (GrafxDeviceFull("device full"), GraphUnavailable),
        (
            GrafxUnsupportedOperation("unsupported graph operation"),
            GraphCapabilityUnavailable,
        ),
    ),
)
def test_grafx_errors_are_normalized_without_message_classification(
    failure: GrafxError,
    expected_type: type[GraphError],
) -> None:
    from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error

    mapped = map_grafx_error(failure, operation="provider_contract_test")

    assert type(mapped) is expected_type
    assert mapped.details["backend"] == "okto_grafx"
    assert mapped.details["operation"] == "provider_contract_test"
    assert mapped.details["backend_error_type"] == type(failure).__name__
    assert mapped.details["backend_error_code"] == failure.code
    assert mapped.details["backend_retryable"] is failure.retryable
    assert mapped.retryable is failure.retryable


def test_grafx_error_mapping_does_not_expose_backend_message_or_details() -> None:
    from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error

    secret = "C:/private/boards/board-secret/grafx.wal"
    failure = GrafxStorageError(
        f"failed to flush {secret}",
        path=secret,
        statement="MATCH (n) RETURN n",
    )

    mapped = map_grafx_error(failure, operation="commit")

    assert secret not in str(mapped)
    assert secret not in repr(mapped.details)
    assert "MATCH (n)" not in repr(mapped.details)


@pytest.mark.asyncio
async def test_post_durable_commit_failure_is_non_retryable_and_explicit() -> None:
    from okto_pulse.community.adapters.grafx_graph_transaction import (
        CommunityGrafxGraphTransaction,
    )

    failure = GrafxStorageError("publication failed after the barrier")

    class _PostDurableTransaction:
        active = True
        report: object | None = None

        def commit(self) -> None:
            self.active = False
            self.report = type(
                "CommitReportProbe",
                (),
                {"durable": True, "wrote": True, "csn": 41},
            )()
            raise failure

        def rollback(self) -> None:
            self.active = False

    transaction = _PostDurableTransaction()

    class _DatabaseProbe:
        def begin(self, mode: str) -> _PostDurableTransaction:
            assert mode == "write"
            return transaction

    provider = CommunityGrafxGraphTransaction(
        lambda _board_id: _DatabaseProbe(),  # type: ignore[arg-type]
        lambda _board_id, _phase: None,
        node_types=(),
        relationship_pairs=(),
    )
    scope = await provider.begin(BOARD_ID)

    with pytest.raises(GraphUnavailable) as raised:
        await scope.commit()

    assert raised.value.__cause__ is failure
    assert raised.value.retryable is False
    assert raised.value.details["commit_durable"] is True
    assert raised.value.details["write_may_be_applied"] is True
    assert raised.value.details["commit_csn"] == 41


def test_existing_core_graph_error_is_preserved_by_identity() -> None:
    from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error

    original = GraphUnavailable("already normalized", details={"sentinel": True})

    assert map_grafx_error(original, operation="ignored") is original


@pytest.mark.asyncio
async def test_provider_raises_normalized_error_chained_from_real_grafx_failure(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    async with await provider.begin(BOARD_ID) as scope:
        scope.create_node(
            "Entity",
            "duplicate",
            {"title": "original"},
            source_session_id="original-session",
        )

    scope = await provider.begin(BOARD_ID)
    try:
        with pytest.raises(GraphError) as normalized:
            scope.create_node(
                "Entity",
                "duplicate",
                {"title": "duplicate"},
                source_session_id="duplicate-session",
            )
        assert isinstance(normalized.value.__cause__, GrafxError)
        assert normalized.value.details["backend"] == "okto_grafx"
        assert normalized.value.details["operation"] == "create_node"
    finally:
        await scope.rollback()

    assert _node(grafx_database, "duplicate")[:3] == (
        "duplicate",
        "original-session",
        "original",
    )
