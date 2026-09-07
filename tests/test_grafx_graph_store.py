"""M-PULSE-6 contract and real-engine coverage for the Grafx board store."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import okto_grafx
import pytest
from okto_grafx import Timestamp
from okto_grafx.errors import GrafxStorageError
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphLockContention,
    GraphUnavailable,
)
from okto_pulse.core.kg.interfaces.graph_store import (
    QueryFilters,
    SemanticGraphStore,
)
from okto_pulse.core.kg.kg_service import _as_iso_timestamp
from okto_pulse.core.kg.schema_contract import SCHEMA_VERSION

from okto_pulse.community.adapters.grafx_graph_store import (
    CommunityGrafxGraphStore,
    _normalize_value,
)
from okto_pulse.community.adapters.grafx_schema_manifest import (
    PULSE_GRAFX_SCHEMA_MANIFEST,
)

BOARD_ID = "grafx-board-store-contract"
DIMENSION = 384
PORT_METHODS = (
    "bootstrap",
    "capabilities",
    "create_edge",
    "create_node",
    "delete_edges_by_session",
    "delete_nodes_by_session",
    "edge_exists",
    "find_active_by_source_ref",
    "find_by_artifact",
    "find_by_topic",
    "find_contradictions",
    "find_node_types",
    "get_alternatives",
    "get_constraint_detail",
    "get_learnings_for_area",
    "get_schema_info",
    "get_schema_version",
    "increment_attestation",
    "list_node_properties",
    "list_schema_objects",
    "mark_superseded",
    "traverse_supersedence",
    "update_node",
    "vector_search",
)


@dataclass
class _Fence:
    calls: list[tuple[str, str]] = field(default_factory=list)
    deny_phase: str | None = None

    def __call__(self, board_id: str, phase: str) -> None:
        self.calls.append((board_id, phase))
        if phase == self.deny_phase:
            raise GraphLockContention(
                "test fence refused the graph write",
                details={"board_id": board_id, "phase": phase},
            )


@pytest.fixture(scope="module")
def real_store(tmp_path_factory: pytest.TempPathFactory):
    path = tmp_path_factory.mktemp("grafx-graph-store") / "board"
    database = okto_grafx.connect(path, page_size=4096)
    fence = _Fence()
    store = CommunityGrafxGraphStore(
        lambda board_id: database if board_id == BOARD_ID else _missing_board(board_id),
        fence,
    )
    store.bootstrap(BOARD_ID)
    try:
        yield store, database, fence, path
    finally:
        database.close()


def _missing_board(board_id: str) -> Any:
    raise KeyError(board_id)


def _attrs(
    title: str,
    source_ref: str,
    session_id: str,
    *,
    content: str = "",
    generation: int = 0,
    kind_of: str = "semantic",
    embedding: list[float] | None = None,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "title": title,
        "content": content,
        "context": "context",
        "justification": "justification",
        "source_artifact_ref": source_ref,
        "graph_layer": "canonical",
        "maturity_status": "active",
        "source_session_id": session_id,
        "created_at": "2026-08-28T00:00:00Z",
        "created_by_agent": "test",
        "source_confidence": 0.9,
        "relevance_score": 0.8,
        "query_hits": 0,
        "priority_boost": 0.0,
        "revocation_reason": "",
        "human_curated": False,
        "generation": generation,
        "attestation_count": 1,
        "kind_of": kind_of,
    }
    if embedding is not None:
        attrs["embedding"] = embedding
    return attrs


def _edge_attrs(session_id: str, rule_id: str) -> dict[str, Any]:
    return {
        "confidence": 0.91,
        "created_by_session_id": session_id,
        "created_at": "2026-08-28T00:00:00Z",
        "layer": "deterministic",
        "rule_id": rule_id,
        "created_by": "test",
        "fallback_reason": "",
    }


def test_grafx_timestamps_match_the_current_ladybug_boundary_shape() -> None:
    expected = datetime(2026, 8, 28, 12, 34, 56, 123456)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = expected.replace(tzinfo=UTC) - epoch
    micros = (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )

    assert _normalize_value(Timestamp(micros=micros)) == expected
    assert _normalize_value(expected) is expected
    assert _normalize_value(
        datetime(
            2026,
            8,
            28,
            9,
            34,
            56,
            123456,
            tzinfo=timezone(-timedelta(hours=3)),
        )
    ) == expected
    assert _as_iso_timestamp(_normalize_value(Timestamp(micros=micros))) == (
        "2026-08-28T12:34:56.123456"
    )
    assert _as_iso_timestamp(
        _normalize_value(Timestamp(micros=micros - 123456))
    ) == "2026-08-28T12:34:56"


def test_the_provider_implements_the_exact_24_method_core_surface() -> None:
    protocol_methods = {
        name
        for name, member in inspect.getmembers(SemanticGraphStore, inspect.isfunction)
        if not name.startswith("_")
    }
    provider_methods = {
        name
        for name, member in inspect.getmembers(
            CommunityGrafxGraphStore, inspect.isfunction
        )
        if not name.startswith("_")
    }

    assert len(protocol_methods) == 24
    assert protocol_methods <= provider_methods
    assert isinstance(
        CommunityGrafxGraphStore(lambda _board: object(), lambda *_args: None),
        SemanticGraphStore,
    )


@pytest.mark.parametrize("method_name", PORT_METHODS)
def test_each_core_port_method_is_a_public_callable(method_name: str) -> None:
    assert callable(getattr(CommunityGrafxGraphStore, method_name))


def test_a_write_resolves_once_and_fences_immediately_before_effect_and_commit() -> (
    None
):
    trace: list[str] = []

    class _Transaction:
        active = True

        def execute(self, _statement: str, _parameters: dict[str, Any]) -> object:
            trace.append("write")
            return SimpleNamespace(rows=())

        def commit(self) -> None:
            trace.append("commit")
            self.active = False

        def rollback(self) -> None:
            trace.append("rollback")
            self.active = False

    column = SimpleNamespace(
        name="id",
        type=SimpleNamespace(name="STRING"),
        is_vector=False,
    )
    definition = SimpleNamespace(kind="node", columns=(column,))
    transaction = _Transaction()

    class _Database:
        catalog = SimpleNamespace(
            catalog=SimpleNamespace(table=lambda _name: definition)
        )

        def begin(self, mode: str) -> _Transaction:
            assert mode == "write"
            return transaction

    resolutions: list[str] = []

    def resolve(board_id: str) -> Any:
        resolutions.append(board_id)
        return _Database()

    def fence(_board_id: str, phase: str) -> None:
        trace.append(f"fence:{phase}")

    CommunityGrafxGraphStore(resolve, fence).create_node(BOARD_ID, "Decision", "d1", {})

    assert resolutions == [BOARD_ID]
    assert trace == ["fence:create_node", "write", "fence:commit", "commit"]


def test_every_port_method_roundtrips_over_one_real_grafx_board(real_store) -> None:
    store, _database, fence, _path = real_store
    bootstrap_phases = [phase for _board, phase in fence.calls]
    assert bootstrap_phases.count("bootstrap") == (
        len(PULSE_GRAFX_SCHEMA_MANIFEST.spaces)
        + len(PULSE_GRAFX_SCHEMA_MANIFEST.tables)
        + 1  # BoardMeta is stamped only after the catalog commit validates.
    )
    assert bootstrap_phases.count("commit") == 2
    fence.calls.clear()

    # Idempotent bootstrap performs no writes and therefore needs no write fence.
    store.bootstrap(BOARD_ID)
    assert fence.calls == []

    zeroes = [0.0] * DIMENSION
    vector = [1.0, *zeroes[1:]]
    nodes = (
        (
            "Decision",
            "decision-old",
            _attrs(
                "Architecture choice old",
                "artifact:architecture",
                "node-session",
            ),
        ),
        (
            "Decision",
            "decision-new",
            _attrs(
                "Architecture choice new",
                "artifact:architecture:new",
                "node-session",
                generation=2,
                embedding=vector,
            ),
        ),
        (
            "Alternative",
            "alternative-1",
            _attrs("Alternative one", "artifact:alternative", "node-session"),
        ),
        (
            "Constraint",
            "constraint-1",
            _attrs("Constraint one", "artifact:constraint", "node-session"),
        ),
        (
            "Bug",
            "bug-1",
            _attrs(
                "Storage incident",
                "artifact:bug",
                "node-session",
                content="storage corruption in area",
            ),
        ),
        (
            "Learning",
            "learning-1",
            _attrs(
                "Storage learning",
                "artifact:learning",
                "node-session",
                content="validate every durable write",
            ),
        ),
    )
    for node_type, node_id, attrs in nodes:
        store.create_node(BOARD_ID, node_type, node_id, attrs)

    edges = (
        (
            "supersedes",
            "Decision",
            "Decision",
            "decision-old",
            "decision-new",
            "supersedes/rule",
        ),
        (
            "contradicts",
            "Decision",
            "Decision",
            "decision-old",
            "decision-new",
            "contradicts/rule",
        ),
        (
            "relates_to",
            "Decision",
            "Alternative",
            "decision-old",
            "alternative-1",
            "alternative/rule",
        ),
        (
            "violates",
            "Bug",
            "Constraint",
            "bug-1",
            "constraint-1",
            "violation/rule",
        ),
        (
            "validates",
            "Learning",
            "Bug",
            "learning-1",
            "bug-1",
            "learning/rule",
        ),
    )
    for edge_type, from_type, to_type, from_id, to_id, rule_id in edges:
        store.create_edge(
            BOARD_ID,
            edge_type,
            from_id,
            to_id,
            _edge_attrs("edge-session", rule_id),
            from_type=from_type,
            to_type=to_type,
        )

    filters = QueryFilters(
        min_confidence=0.5,
        min_relevance=0.3,
        max_rows=20,
    )
    topic_rows = store.find_by_topic(BOARD_ID, "Decision", "Architecture", filters)
    assert {row[0] for row in topic_rows} == {"decision-old", "decision-new"}
    assert all(row[3] == datetime(2026, 8, 28) for row in topic_rows)
    assert all(len(row) == 8 for row in topic_rows)

    artifact_rows = store.find_by_artifact(
        BOARD_ID,
        "artifact:architecture",
        filters,
        graph_layer="canonical",
    )
    assert artifact_rows
    assert any(row[2] == "alternative-1" for row in artifact_rows)
    assert all("__" not in str(row[6]) for row in artifact_rows)
    assert all("__" not in str(row[7]) for row in artifact_rows if row[7])

    chain = store.traverse_supersedence(BOARD_ID, "decision-old", max_depth=3)
    assert [row[0] for row in chain] == ["decision-new"]
    assert store.traverse_supersedence(BOARD_ID, "decision-old", max_depth=0) == []

    contradictions = store.find_contradictions(BOARD_ID, None, 10)
    assert contradictions == [
        [
            "decision-old",
            "Architecture choice old",
            "decision-new",
            "Architecture choice new",
            0.91,
        ]
    ]
    assert store.find_contradictions(BOARD_ID, "missing", 10) == []

    vector_hits = store.vector_search(
        BOARD_ID,
        "Decision",
        vector,
        1,
        0.9,
        graph_layer="canonical",
    )
    assert [hit["node_id"] for hit in vector_hits] == ["decision-new"]
    assert not any(
        type(value).__module__.startswith("okto_grafx")
        for hit in vector_hits
        for value in hit.values()
    )

    active = store.find_active_by_source_ref(
        BOARD_ID, "Decision", "artifact:architecture:new"
    )
    assert active is not None
    assert active["node_id"] == "decision-new"
    assert active["generation"] == 2

    main, origins, violations = store.get_constraint_detail(BOARD_ID, "constraint-1")
    assert main[0][:2] == ["constraint-1", "Constraint one"]
    assert origins == []
    assert violations == [["bug-1", "Storage incident"]]

    alternatives = store.get_alternatives(BOARD_ID, "decision-old", 10)
    assert alternatives[0][:2] == ["alternative-1", "Alternative one"]
    learnings = store.get_learnings_for_area(BOARD_ID, "storage", filters)
    assert learnings[0][0] == "learning-1"
    assert store.get_learnings_for_area(BOARD_ID, None, filters) == learnings
    assert store.get_learnings_for_area(BOARD_ID, "", filters) == learnings
    assert store.get_learnings_for_area(BOARD_ID, "unrelated-area", filters) == []

    assert store.edge_exists(
        BOARD_ID,
        "contradicts",
        "Decision",
        "Decision",
        "decision-old",
        "decision-new",
        "contradicts/rule",
    )
    assert store.find_node_types(BOARD_ID, "decision-new") == ("Decision",)

    store.update_node(
        BOARD_ID, "Decision", "decision-new", {"title": "Architecture final"}
    )
    store.increment_attestation(
        BOARD_ID,
        "Decision",
        "decision-new",
        attested_at="2026-08-28T01:00:00Z",
    )
    store.mark_superseded(
        BOARD_ID,
        "Decision",
        "decision-old",
        superseded_by="decision-new",
        superseded_at="2026-08-28T02:00:00Z",
        revocation_reason="replaced",
    )
    assert (
        store.find_active_by_source_ref(BOARD_ID, "Decision", "artifact:architecture")
        is None
    )

    assert store.get_schema_version(BOARD_ID) == SCHEMA_VERSION
    info = store.get_schema_info(BOARD_ID, include_internal=True)
    assert info["schema_version"] == SCHEMA_VERSION
    assert info["internal_node_types"] == [{"name": "BoardMeta", "stable": False}]
    objects = store.list_schema_objects(BOARD_ID)
    assert "Decision" in objects and "relates_to" in objects
    assert not any("__" in name for name in objects)
    properties = store.list_node_properties(BOARD_ID, "Decision")
    assert properties[0] == "id" and "embedding" in properties
    assert store.list_node_properties(BOARD_ID, "Unknown") == ()
    capabilities = store.capabilities()
    assert capabilities.indexed_similarity
    assert capabilities.schema_introspection
    assert capabilities.mutable_indexed_attributes

    assert store.delete_edges_by_session(BOARD_ID, "edge-session") == len(edges)
    assert not store.edge_exists(
        BOARD_ID,
        "contradicts",
        "Decision",
        "Decision",
        "decision-old",
        "decision-new",
    )
    assert store.delete_nodes_by_session(BOARD_ID, "node-session") == len(nodes)
    assert store.find_node_types(BOARD_ID, "decision-new") == ()

    mutation_phases = [phase for _board, phase in fence.calls]
    assert "create_node" in mutation_phases
    assert "create_edge" in mutation_phases
    assert "update_node" in mutation_phases
    assert "increment_attestation" in mutation_phases
    assert "mark_superseded" in mutation_phases
    assert "delete_edges_by_session" in mutation_phases
    assert "delete_nodes_by_session" in mutation_phases
    assert mutation_phases.count("commit") >= 1


@pytest.mark.parametrize("deny_phase", ("create_node", "commit"))
def test_a_lost_fence_discards_or_prevents_the_real_write(
    real_store, deny_phase: str
) -> None:
    store, _database, fence, _path = real_store
    fence.calls.clear()
    fence.deny_phase = deny_phase
    node_id = f"must-rollback-{deny_phase}"
    try:
        with pytest.raises(GraphLockContention):
            store.create_node(
                BOARD_ID,
                "Decision",
                node_id,
                _attrs("Must rollback", "artifact:rollback", "rollback-session"),
            )
    finally:
        fence.deny_phase = None

    expected = [(BOARD_ID, "create_node")]
    if deny_phase == "commit":
        expected.append((BOARD_ID, "commit"))
    assert fence.calls == expected
    assert store.find_node_types(BOARD_ID, node_id) == ()


def test_a_post_durable_commit_failure_is_explicit_and_not_retryable() -> None:
    failure = GrafxStorageError("publication failed after durability")

    class _Transaction:
        active = True
        report: object | None = None

        def execute(self, _statement: str, _parameters: dict[str, Any]) -> object:
            return SimpleNamespace(rows=())

        def commit(self) -> None:
            self.active = False
            self.report = SimpleNamespace(durable=True, wrote=True, csn=73)
            raise failure

        def rollback(self) -> None:
            self.active = False

    column = SimpleNamespace(
        name="id",
        type=SimpleNamespace(name="STRING"),
        is_vector=False,
    )
    definition = SimpleNamespace(kind="node", columns=(column,))
    transaction = _Transaction()
    database = SimpleNamespace(
        catalog=SimpleNamespace(
            catalog=SimpleNamespace(table=lambda _name: definition)
        ),
        begin=lambda _mode: transaction,
    )
    store = CommunityGrafxGraphStore(
        lambda _board_id: database,
        lambda *_args: None,
    )

    with pytest.raises(GraphUnavailable) as raised:
        store.create_node(BOARD_ID, "Decision", "durable", {})

    assert raised.value.__cause__ is failure
    assert raised.value.retryable is False
    assert raised.value.details["commit_durable"] is True
    assert raised.value.details["write_may_be_applied"] is True
    assert raised.value.details["commit_csn"] == 73


def test_backend_failure_is_mapped_without_leaking_a_grafx_exception() -> None:
    failure = GrafxStorageError("private backend path must not escape")
    store = CommunityGrafxGraphStore(
        lambda _board_id: (_ for _ in ()).throw(failure),
        lambda *_args: None,
    )

    with pytest.raises(GraphUnavailable) as raised:
        store.get_schema_version(BOARD_ID)

    assert raised.value.__cause__ is failure
    assert type(raised.value).__module__.startswith("okto_pulse.core")
    assert "private backend path" not in str(raised.value)


def test_catalog_failure_during_vector_coercion_is_also_normalized() -> None:
    failure = GrafxStorageError("private vector catalog failure")
    columns = (
        SimpleNamespace(
            name="id",
            type=SimpleNamespace(name="STRING"),
            is_vector=False,
        ),
        SimpleNamespace(
            name="embedding",
            type=SimpleNamespace(name="VECTOR_F64"),
            is_vector=True,
            vector_space="decision_embedding_idx",
        ),
    )
    definition = SimpleNamespace(kind="node", columns=columns)
    catalog = SimpleNamespace(
        table=lambda _name: definition,
        space=lambda _name: (_ for _ in ()).throw(failure),
    )
    database = SimpleNamespace(catalog=SimpleNamespace(catalog=catalog))
    store = CommunityGrafxGraphStore(
        lambda _board_id: database,
        lambda *_args: None,
    )

    with pytest.raises(GraphUnavailable) as raised:
        store.create_node(
            BOARD_ID,
            "Decision",
            "vector-failure",
            {"embedding": [0.0] * DIMENSION},
        )

    assert raised.value.__cause__ is failure
    assert raised.value.details["operation"] == "vector_property_schema"
