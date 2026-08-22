from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    CommunityKuzuGraphTransaction,
)
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.application.processors.consolidation import (
    _worker_edge_to_candidate,
    _worker_node_to_candidate,
)
from okto_pulse.core.application.processors.deterministic_kg import (
    DeterministicWorker,
)
from okto_pulse.core.infra.config import configure_settings
from okto_pulse.core.kg import primitives
from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetIntent,
    ProjectionEdgeRef,
)
from okto_pulse.core.kg.providers.testing.embedding import (
    TestingStubEmbeddingProvider,
)
from okto_pulse.core.kg.transaction import TransactionOrchestrator


def _commit_spec_worker_result_to_kuzu(
    *,
    board_id: str,
    artifact_id: str,
    session_id: str,
    result: object,
) -> tuple[dict, object, list, object, dict, list[dict]]:
    node_candidates = {
        node.candidate_id: node
        for node in (_worker_node_to_candidate(item) for item in result.nodes)
    }
    edge_candidates = {
        edge.candidate_id: edge
        for edge in (_worker_edge_to_candidate(item) for item in result.edges)
    }
    return primitives._do_graph_commit(
        board_id,
        session_id,
        node_candidates,
        edge_candidates,
        {},
        "system:historical_consolidation",
        TestingStubEmbeddingProvider(),
        "healthy",
        result.content_hash,
        artifact_id,
        frozenset(),
        "spec",
        result.spec_lineage_parent_intent,
        frozenset(result.relational_projection_candidate_ids),
        result.relational_projection_active_set_intent,
    )


def _read_rows(conn: object, cypher: str, params: dict) -> list[tuple]:
    cursor = conn.execute(cypher, params)
    try:
        rows: list[tuple] = []
        while cursor.has_next():
            rows.append(tuple(cursor.get_next()))
        return rows
    finally:
        cursor.close()


@pytest.mark.asyncio
async def test_real_graph_spec_precedence_converges_and_compensates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"skm-precedence-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    configure_settings(CommunitySettings())
    kg_runtime.reset_bootstrap_cache_for_tests()
    scope = None
    try:
        kg_runtime.bootstrap_board_graph(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        dependent_id = "dependent-spec-node"
        prerequisite_id = "prerequisite-spec-node"
        scope.create_node(
            "Entity",
            dependent_id,
            {"title": "Dependent", "source_artifact_ref": "spec:dependent"},
            source_session_id="dependent-session",
        )
        scope.create_node(
            "Entity",
            prerequisite_id,
            {
                "title": "Prerequisite",
                "source_artifact_ref": "spec:prerequisite",
            },
            source_session_id="prerequisite-session",
        )
        rule_id = "precedes/spec_dependency/dependency-1@v2.0"
        assert scope.create_edge(
            "precedes",
            "Entity",
            "Entity",
            prerequisite_id,
            dependent_id,
            {
                "confidence": 1.0,
                "layer": "deterministic",
                "rule_id": rule_id,
                "created_by": "worker_layer1",
                "fallback_reason": "",
            },
        )

        active = ProjectionActiveSetIntent(
            owner_type="spec",
            owner_id="dependent",
            namespace="dependencies",
            owner_node_id=dependent_id,
            active_edges=(
                ProjectionEdgeRef(
                    edge_type="precedes",
                    from_type="Entity",
                    to_type="Entity",
                    from_id=prerequisite_id,
                    to_id=dependent_id,
                    rule_id=rule_id,
                ),
            ),
        )
        assert scope.reconcile_projection_active_set(active).edge_before_images == ()

        removed = scope.reconcile_projection_active_set(
            ProjectionActiveSetIntent(
                owner_type="spec",
                owner_id="dependent",
                namespace="dependencies",
                owner_node_id=dependent_id,
            )
        )
        assert len(removed.edge_before_images) == 1
        assert not scope.execute(
            "MATCH (:Entity)-[r:precedes]->"
            "(owner:Entity {id: $owner}) RETURN r.rule_id",
            {"owner": dependent_id},
        ).rows

        scope.compensate_projection_active_set(removed)
        rows = scope.execute(
            "MATCH (:Entity)-[r:precedes]->"
            "(owner:Entity {id: $owner}) RETURN r.rule_id",
            {"owner": dependent_id},
        ).rows
        assert rows == ((rule_id,),)

        replacement_rule_id = "precedes/spec_dependency/dependency-1@v3.0"
        orchestrator = TransactionOrchestrator(
            graph_scope=scope,
            session_id="replacement-session",
            board_id=board_id,
        )
        orchestrator.create_edge(
            "precedes",
            prerequisite_id,
            dependent_id,
            attrs={
                "confidence": 1.0,
                "layer": "deterministic",
                "rule_id": replacement_rule_id,
                "created_by": "worker_layer1",
                "fallback_reason": "",
            },
            from_type="Entity",
            to_type="Entity",
        )
        orchestrator.reconcile_projection_active_set(
            ProjectionActiveSetIntent(
                owner_type="spec",
                owner_id="dependent",
                namespace="dependencies",
                owner_node_id=dependent_id,
                active_edges=(
                    ProjectionEdgeRef(
                        edge_type="precedes",
                        from_type="Entity",
                        to_type="Entity",
                        from_id=prerequisite_id,
                        to_id=dependent_id,
                        rule_id=replacement_rule_id,
                    ),
                ),
            )
        )
        rows = scope.execute(
            "MATCH (:Entity)-[r:precedes]->"
            "(owner:Entity {id: $owner}) RETURN r.rule_id",
            {"owner": dependent_id},
        ).rows
        assert rows == ((replacement_rule_id,),)

        await orchestrator.compensate()
        rows = scope.execute(
            "MATCH (:Entity)-[r:precedes]->"
            "(owner:Entity {id: $owner}) RETURN r.rule_id",
            {"owner": dependent_id},
        ).rows
        assert rows == ((rule_id,),)
        await scope.commit()
    finally:
        if scope is not None:
            await scope.rollback()
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_real_core_pipeline_waits_for_exact_active_prerequisite_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign successor and same-prefix decoy cannot satisfy PRECEDES.

    This exercises the Core deterministic worker and commit primitive against
    the real Community Kuzu transaction.  The first dependent attempt must
    remain read-only while the exact prerequisite has only a corrupt historical
    generation.  Once the authoritative prerequisite is materialized, the same
    dependent payload converges to one edge bound to that active exact root.
    """

    board_id = f"skm-core-kuzu-{uuid4().hex}"
    dependent_spec_id = "aaaaaaaa-0000-0000-0000-000000000001"
    prerequisite_spec_id = "bbbbbbbb-0000-0000-0000-000000000001"
    same_prefix_decoy_spec_id = "bbbbbbbb-ffff-ffff-ffff-ffffffffffff"
    dependency_id = "dependency-core-kuzu-1"
    prerequisite_ref = f"spec:{prerequisite_spec_id}"
    dependent_ref = f"spec:{dependent_spec_id}"
    decoy_ref = f"spec:{same_prefix_decoy_spec_id}"
    historical_prerequisite_id = "historical-prerequisite-root"
    foreign_successor_id = "foreign-successor-root"
    decoy_node_id = "same-prefix-decoy-root"

    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    configure_settings(CommunitySettings())
    kg_runtime.reset_bootstrap_cache_for_tests()
    graph_transaction = CommunityKuzuGraphTransaction()
    monkeypatch.setattr(
        primitives,
        "get_kg_registry",
        lambda: SimpleNamespace(graph_transaction=graph_transaction),
    )

    dependent_result = DeterministicWorker().process_spec(
        {
            "id": dependent_spec_id,
            "board_id": board_id,
            "title": "Dependent Spec",
            "description": "Depends on the exact prerequisite.",
            "context": "SK-M real Kuzu regression",
            "status": "draft",
            "spec_dependencies": [
                {
                    "dependency_id": dependency_id,
                    "dependent_spec_id": dependent_spec_id,
                    "prerequisite_spec_id": prerequisite_spec_id,
                    "prerequisite_title": "Exact Prerequisite",
                    "prerequisite_status": "draft",
                    "prerequisite_version": 1,
                }
            ],
        }
    )

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        async with await graph_transaction.begin(board_id) as setup:
            setup.create_node(
                "Entity",
                decoy_node_id,
                {
                    "title": "Same-prefix decoy",
                    "source_artifact_ref": decoy_ref,
                    "generation": 0,
                    "graph_layer": "working",
                },
                source_session_id="setup-decoy",
            )
            setup.create_node(
                "Entity",
                historical_prerequisite_id,
                {
                    "title": "Historical exact prerequisite",
                    "source_artifact_ref": prerequisite_ref,
                    "generation": 7,
                    "graph_layer": "working",
                },
                source_session_id="setup-corrupt-history",
            )
            setup.create_node(
                "Entity",
                foreign_successor_id,
                {
                    "title": "Foreign semantic successor",
                    "source_artifact_ref": "ideation:foreign-successor",
                    "generation": 8,
                    "graph_layer": "working",
                },
                source_session_id="setup-corrupt-history",
            )
            setup.mark_superseded(
                "Entity",
                historical_prerequisite_id,
                superseded_by=foreign_successor_id,
                superseded_at="2026-08-12T00:00:00+00:00",
                revocation_reason="legacy cross-source supersedence",
            )
            assert setup.create_edge(
                "supersedes",
                "Entity",
                "Entity",
                foreign_successor_id,
                historical_prerequisite_id,
                {"confidence": 1.0},
            )

        with pytest.raises(primitives.KGPrimitiveError) as pending:
            _commit_spec_worker_result_to_kuzu(
                board_id=board_id,
                artifact_id=dependent_spec_id,
                session_id="dependent-before-prerequisite",
                result=dependent_result,
            )
        assert pending.value.code == "relational_projection_endpoint_pending"

        with kg_runtime.open_board_connection(board_id) as (_db, conn):
            assert (
                _read_rows(
                    conn,
                    "MATCH (n:Entity) "
                    "WHERE n.source_artifact_ref = $ref "
                    "AND n.superseded_by IS NULL RETURN n.id",
                    {"ref": prerequisite_ref},
                )
                == []
            )
            assert (
                _read_rows(
                    conn,
                    "MATCH (n:Entity) WHERE n.source_artifact_ref = $ref RETURN n.id",
                    {"ref": dependent_ref},
                )
                == []
            )
            assert (
                _read_rows(
                    conn,
                    "MATCH (:Entity)-[r:precedes]->(:Entity) RETURN r.rule_id",
                    {},
                )
                == []
            )

        prerequisite_result = DeterministicWorker().process_spec(
            {
                "id": prerequisite_spec_id,
                "board_id": board_id,
                "title": "Exact Prerequisite",
                "description": "Authoritative prerequisite content.",
                "context": "SK-M real Kuzu regression",
                "status": "draft",
                "spec_dependencies": [],
            }
        )
        prerequisite_mapping, *_ = _commit_spec_worker_result_to_kuzu(
            board_id=board_id,
            artifact_id=prerequisite_spec_id,
            session_id="materialize-exact-prerequisite",
            result=prerequisite_result,
        )
        prerequisite_candidate_id = f"spec_{prerequisite_spec_id[:8]}_entity"
        active_prerequisite_id = prerequisite_mapping[prerequisite_candidate_id]
        assert active_prerequisite_id not in {
            historical_prerequisite_id,
            foreign_successor_id,
            decoy_node_id,
        }

        dependent_mapping, *_ = _commit_spec_worker_result_to_kuzu(
            board_id=board_id,
            artifact_id=dependent_spec_id,
            session_id="dependent-after-prerequisite",
            result=dependent_result,
        )
        dependency_endpoint = f"kgref:Entity:{prerequisite_ref}"
        assert dependent_mapping[dependency_endpoint] == active_prerequisite_id

        with kg_runtime.open_board_connection(board_id) as (_db, conn):
            active_exact = _read_rows(
                conn,
                "MATCH (n:Entity) "
                "WHERE n.source_artifact_ref = $ref "
                "AND n.superseded_by IS NULL RETURN n.id",
                {"ref": prerequisite_ref},
            )
            assert active_exact == [(active_prerequisite_id,)]
            assert _read_rows(
                conn,
                "MATCH (prerequisite:Entity)-[r:precedes]->"
                "(dependent:Entity) "
                "WHERE dependent.source_artifact_ref = $dependent_ref "
                "AND r.rule_id STARTS WITH $rule_prefix "
                "RETURN prerequisite.id, prerequisite.source_artifact_ref, "
                "dependent.id, dependent.source_artifact_ref, r.rule_id",
                {
                    "dependent_ref": dependent_ref,
                    "rule_prefix": f"precedes/spec_dependency/{dependency_id}@",
                },
            ) == [
                (
                    active_prerequisite_id,
                    prerequisite_ref,
                    dependent_mapping[f"spec_{dependent_spec_id[:8]}_entity"],
                    dependent_ref,
                    next(
                        edge.rule_id
                        for edge in dependent_result.edges
                        if edge.edge_type == "precedes"
                    ),
                )
            ]
    finally:
        kg_runtime.close_all_connections(board_id)
