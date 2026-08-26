from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    CommunityKuzuGraphTransaction,
)
from okto_pulse.core.kg.interfaces.graph_transaction import (
    GraphTransactionScope,
    ProjectionActiveSetIntent,
    ProjectionActiveSetReconciliationError,
    ProjectionNodeRef,
    SOURCE_PROJECTION_REMOVED_REASON,
)
from okto_pulse.core.application.processors.deterministic_kg import (
    RelationalProjectionActiveRef,
    RelationalProjectionActiveSetIntent,
)
from okto_pulse.core.kg import primitives
from okto_pulse.core.kg.schemas import (
    EdgeCandidate,
    NodeCandidate,
    ReconciliationHint,
    ReconciliationOperation,
)
from okto_pulse.core.kg.transaction import TransactionOrchestrator


OWNER_ID = "12345678-refinement-c8"
OTHER_OWNER_ID = f"{OWNER_ID}-collision"
ZERO_OWNER_ID = "87654321-refinement-c8-zero"
ROOT_ID = f"refinement_{OWNER_ID[:8]}_entity"
OTHER_ROOT_ID = f"refinement_{OTHER_OWNER_ID[:8]}_entity_collision"
ZERO_ROOT_ID = f"refinement_{ZERO_OWNER_ID[:8]}_entity"
IMPOSTOR_ROOT_ID = f"{ROOT_ID}_impostor"


def _decision_ref(owner_id: str, ledger_id: str) -> str:
    return f"refinement:{owner_id}:rdl:{ledger_id}:decision"


def _alternative_ref(owner_id: str, ledger_id: str, marker: str) -> str:
    return f"refinement:{owner_id}:rdl:{ledger_id}:alternative:{marker * 64}"


def _node_attrs(
    source_ref: str,
    *,
    content: str,
    created_by_agent: str = "system:layer1_worker",
    revocation_reason: str = "",
) -> dict[str, object]:
    return {
        "title": content,
        "content": content,
        "source_artifact_ref": source_ref,
        "graph_layer": "working",
        "maturity_status": "working_mature",
        "created_by_agent": created_by_agent,
        "source_confidence": 1.0,
        "relevance_score": 0.8,
        "revocation_reason": revocation_reason,
    }


def _edge_attrs(
    marker: str,
    *,
    node_type: str,
    rule_id: str | None = None,
) -> dict[str, object]:
    projection_kind = {
        "Decision": "decision",
        "Alternative": "alternative",
    }[node_type]
    return {
        "confidence": 1.0,
        "created_by_session_id": f"edge-session-{marker}",
        "created_at": "2026-07-28T12:00:00.000000",
        "layer": "deterministic",
        "rule_id": (rule_id or f"belongs_to/relational_rdl_{projection_kind}@v2.0"),
        "created_by": "worker_layer1",
        "fallback_reason": "",
    }


def _reason(scope: GraphTransactionScope, node_type: str, node_id: str) -> str:
    before_image = scope.snapshot_node_properties(
        node_type,
        node_id,
        ("revocation_reason",),
    )
    assert before_image is not None
    return str(before_image.attrs["revocation_reason"] or "")


def _content(scope: GraphTransactionScope, node_type: str, node_id: str) -> str:
    before_image = scope.snapshot_node_properties(
        node_type,
        node_id,
        ("content",),
    )
    assert before_image is not None
    return str(before_image.attrs["content"] or "")


def _incident_edge_count(
    scope: GraphTransactionScope,
    node_type: str,
    node_id: str,
) -> int:
    # This assertion targets the Community adapter's complete before-image
    # semantics. The method is intentionally adapter-internal; all mutations
    # in this test still go through the public transaction protocol.
    return len(scope._snapshot_incident_edges(node_type, node_id))  # type: ignore[attr-defined]


def _owned_projection_rows(
    scope: GraphTransactionScope,
    *,
    node_type: str,
    owner_id: str,
    owner_node_id: str,
) -> tuple[tuple[object, ...], ...]:
    return scope.execute(
        f"MATCH (n:{node_type})-[r:belongs_to]->"
        "(owner:Entity {id: $owner_node_id}) "
        "WHERE owner.source_artifact_ref = $owner_ref "
        "RETURN n.id, n.revocation_reason, r.rule_id ORDER BY n.id",
        {
            "owner_node_id": owner_node_id,
            "owner_ref": f"refinement:{owner_id}",
        },
    ).rows


def _count_nodes_by_source_ref(
    board_id: str,
    *,
    node_type: str,
    source_ref: str,
) -> int:
    with kg_runtime.open_board_connection(board_id) as (_db, conn):
        result = conn.execute(
            f"MATCH (n:{node_type}) "
            "WHERE n.source_artifact_ref = $source_ref RETURN count(n)",
            {"source_ref": source_ref},
        )
        try:
            return int(result.get_next()[0]) if result.has_next() else 0
        finally:
            result.close()


def _create_projection_node(
    scope: GraphTransactionScope,
    *,
    node_type: str,
    node_id: str,
    source_ref: str,
    root_id: str,
    content: str,
    created_by_agent: str = "system:layer1_worker",
    revocation_reason: str = "",
    with_edge: bool = True,
    edge_rule_id: str | None = None,
) -> None:
    scope.create_node(
        node_type,
        node_id,
        _node_attrs(
            source_ref,
            content=content,
            created_by_agent=created_by_agent,
            revocation_reason=revocation_reason,
        ),
        source_session_id=f"node-session-{node_id}",
    )
    if with_edge:
        assert scope.create_edge(
            "belongs_to",
            node_type,
            "Entity",
            node_id,
            root_id,
            _edge_attrs(
                node_id,
                node_type=node_type,
                rule_id=edge_rule_id,
            ),
        )


@pytest.mark.asyncio
async def test_real_kuzu_projection_active_set_is_exact_and_compensable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"c8-projection-active-set-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    active_decision_ref = _decision_ref(OWNER_ID, "ledger-active")
    stale_alternative_ref = _alternative_ref(
        OWNER_ID,
        "ledger-stale",
        "a",
    )
    reactivated_alternative_ref = _alternative_ref(
        OWNER_ID,
        "ledger-reactivated",
        "b",
    )
    source_deleted_ref = _decision_ref(OWNER_ID, "ledger-source-deleted")
    zero_decision_ref = _decision_ref(ZERO_OWNER_ID, "ledger-zero-decision")
    zero_alternative_ref = _alternative_ref(
        ZERO_OWNER_ID,
        "ledger-zero-alternative",
        "c",
    )
    scope: GraphTransactionScope | None = None

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        assert isinstance(scope, GraphTransactionScope)

        for root_id in (
            ROOT_ID,
            OTHER_ROOT_ID,
            ZERO_ROOT_ID,
            IMPOSTOR_ROOT_ID,
        ):
            root_owner_id = {
                ROOT_ID: OWNER_ID,
                OTHER_ROOT_ID: OTHER_OWNER_ID,
                ZERO_ROOT_ID: ZERO_OWNER_ID,
                IMPOSTOR_ROOT_ID: OWNER_ID,
            }[root_id]
            scope.create_node(
                "Entity",
                root_id,
                {
                    "title": root_id,
                    "source_artifact_ref": f"refinement:{root_owner_id}",
                },
                source_session_id=f"root-session-{root_id}",
            )

        _create_projection_node(
            scope,
            node_type="Decision",
            node_id="decision-active",
            source_ref=active_decision_ref,
            root_id=ROOT_ID,
            content="active decision",
        )
        _create_projection_node(
            scope,
            node_type="Alternative",
            node_id="alternative-stale",
            source_ref=stale_alternative_ref,
            root_id=ROOT_ID,
            content="stale content must remain",
        )
        # Kuzu permits byte-identical parallel relationships.  The complete
        # before-image and its compensation must retain both copies.
        assert scope.create_edge(
            "belongs_to",
            "Alternative",
            "Entity",
            "alternative-stale",
            ROOT_ID,
            _edge_attrs("alternative-stale", node_type="Alternative"),
        )
        _create_projection_node(
            scope,
            node_type="Alternative",
            node_id="alternative-reactivated",
            source_ref=reactivated_alternative_ref,
            root_id=ROOT_ID,
            content="reactivated alternative",
            revocation_reason=SOURCE_PROJECTION_REMOVED_REASON,
            with_edge=False,
        )
        _create_projection_node(
            scope,
            node_type="Decision",
            node_id="decision-source-deleted",
            source_ref=source_deleted_ref,
            root_id=ROOT_ID,
            content="deleted decision",
            revocation_reason="source_deleted",
        )

        # Similar-looking identities must remain outside this owner's active
        # set: an owner-prefix collision, untrusted provenance, malformed
        # namespace grammar, and a node-label/reference type mismatch.
        _create_projection_node(
            scope,
            node_type="Alternative",
            node_id="alternative-other-owner",
            source_ref=_alternative_ref(
                OTHER_OWNER_ID,
                "ledger-other",
                "d",
            ),
            root_id=OTHER_ROOT_ID,
            content="other owner",
        )
        _create_projection_node(
            scope,
            node_type="Decision",
            node_id="decision-untrusted",
            source_ref=_decision_ref(OWNER_ID, "ledger-untrusted"),
            root_id=ROOT_ID,
            content="untrusted actor",
            created_by_agent="user:manual",
        )
        _create_projection_node(
            scope,
            node_type="Decision",
            node_id="decision-malformed",
            source_ref=f"{_decision_ref(OWNER_ID, 'ledger-malformed')}:suffix",
            root_id=ROOT_ID,
            content="malformed ref",
        )
        _create_projection_node(
            scope,
            node_type="Alternative",
            node_id="alternative-wrong-type",
            source_ref=_decision_ref(OWNER_ID, "ledger-wrong-type"),
            root_id=ROOT_ID,
            content="wrong type",
        )
        _create_projection_node(
            scope,
            node_type="Decision",
            node_id="decision-wrong-root",
            source_ref=_decision_ref(OWNER_ID, "ledger-wrong-root"),
            root_id=OTHER_ROOT_ID,
            content="wrong owner root",
        )
        _create_projection_node(
            scope,
            node_type="Decision",
            node_id="decision-wrong-owner-node-id",
            source_ref=_decision_ref(
                OWNER_ID,
                "ledger-wrong-owner-node-id",
            ),
            root_id=IMPOSTOR_ROOT_ID,
            content="same owner ref but wrong root identity",
        )
        _create_projection_node(
            scope,
            node_type="Alternative",
            node_id="alternative-wrong-rule",
            source_ref=_alternative_ref(
                OWNER_ID,
                "ledger-wrong-rule",
                "e",
            ),
            root_id=ROOT_ID,
            content="wrong ownership rule",
            edge_rule_id="belongs_to/refinement_rdl@1.0",
        )

        _create_projection_node(
            scope,
            node_type="Decision",
            node_id="decision-zero",
            source_ref=zero_decision_ref,
            root_id=ZERO_ROOT_ID,
            content="zero decision",
        )
        _create_projection_node(
            scope,
            node_type="Alternative",
            node_id="alternative-zero",
            source_ref=zero_alternative_ref,
            root_id=ZERO_ROOT_ID,
            content="zero alternative",
        )

        receipt = scope.reconcile_projection_active_set(
            ProjectionActiveSetIntent(
                owner_type="refinement",
                owner_id=OWNER_ID,
                namespace="rdl",
                owner_node_id=ROOT_ID,
                active_nodes=(
                    ProjectionNodeRef(
                        "Decision",
                        "decision-active",
                        active_decision_ref,
                    ),
                    ProjectionNodeRef(
                        "Alternative",
                        "alternative-reactivated",
                        reactivated_alternative_ref,
                    ),
                    ProjectionNodeRef(
                        "Decision",
                        "decision-source-deleted",
                        source_deleted_ref,
                    ),
                ),
            )
        )

        assert len(receipt.before_images) == 2
        assert _reason(scope, "Decision", "decision-active") == ""
        assert (
            _reason(scope, "Alternative", "alternative-stale")
            == SOURCE_PROJECTION_REMOVED_REASON
        )
        assert (
            _content(scope, "Alternative", "alternative-stale")
            == "stale content must remain"
        )
        assert (
            _incident_edge_count(
                scope,
                "Alternative",
                "alternative-stale",
            )
            == 0
        )
        assert _reason(scope, "Alternative", "alternative-reactivated") == ""

        # An active source-deletion tombstone is never reactivated by the
        # reversible projection-removal mechanism.
        assert _reason(scope, "Decision", "decision-source-deleted") == "source_deleted"
        assert (
            _incident_edge_count(
                scope,
                "Decision",
                "decision-source-deleted",
            )
            == 1
        )

        for node_type, node_id in (
            ("Alternative", "alternative-other-owner"),
            ("Decision", "decision-untrusted"),
            ("Decision", "decision-malformed"),
            ("Alternative", "alternative-wrong-type"),
            ("Decision", "decision-wrong-root"),
            ("Decision", "decision-wrong-owner-node-id"),
            ("Alternative", "alternative-wrong-rule"),
        ):
            assert _reason(scope, node_type, node_id) == ""
            assert _incident_edge_count(scope, node_type, node_id) == 1

        scope.compensate_projection_active_set(receipt)
        assert _reason(scope, "Alternative", "alternative-stale") == ""
        assert (
            _reason(scope, "Alternative", "alternative-reactivated")
            == SOURCE_PROJECTION_REMOVED_REASON
        )
        assert (
            _incident_edge_count(
                scope,
                "Alternative",
                "alternative-stale",
            )
            == 2
        )

        # Empty active_nodes is a real replacement, not an early-return/no-op.
        zero_receipt = scope.reconcile_projection_active_set(
            ProjectionActiveSetIntent(
                owner_type="refinement",
                owner_id=ZERO_OWNER_ID,
                namespace="rdl",
                owner_node_id=ZERO_ROOT_ID,
                active_nodes=(),
            )
        )
        assert len(zero_receipt.before_images) == 2
        for node_type, node_id in (
            ("Decision", "decision-zero"),
            ("Alternative", "alternative-zero"),
        ):
            assert (
                _reason(scope, node_type, node_id) == SOURCE_PROJECTION_REMOVED_REASON
            )
            assert _incident_edge_count(scope, node_type, node_id) == 0

        await scope.commit()
    finally:
        if scope is not None:
            await scope.rollback()
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_real_kuzu_rdl_semantic_change_updates_one_current_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"c8-rdl-current-state-{uuid4().hex}"
    session_id = f"c8-rdl-current-state-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()
    scope: GraphTransactionScope | None = None
    source_ref = _decision_ref(OWNER_ID, "ledger-current-state")

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        scope.create_node(
            "Entity",
            ROOT_ID,
            {
                "title": ROOT_ID,
                "source_artifact_ref": f"refinement:{OWNER_ID}",
            },
            source_session_id="root-session",
        )
        scope.create_node(
            "Entity",
            "board-root-current-state",
            {
                "title": "Board root",
                "source_artifact_ref": f"board:{board_id}",
            },
            source_session_id="board-root-session",
        )
        assert scope.create_edge(
            "belongs_to",
            "Entity",
            "Entity",
            ROOT_ID,
            "board-root-current-state",
            {
                "confidence": 1.0,
                "created_by_session_id": "root-edge-session",
                "created_at": "2026-07-28T15:00:00.000000",
                "layer": "deterministic",
                "rule_id": "belongs_to/refinement_to_board@v2.0",
                "created_by": "worker_layer1",
                "fallback_reason": "",
            },
        )
        _create_projection_node(
            scope,
            node_type="Decision",
            node_id="decision-current",
            source_ref=source_ref,
            root_id=ROOT_ID,
            content="old resolved decision",
        )
        await scope.commit()

        candidates = {
            "projection-owner": NodeCandidate(
                candidate_id="projection-owner",
                node_type="Entity",
                title="Projection owner",
                content="Refinement root",
                source_artifact_ref=f"refinement:{OWNER_ID}",
                graph_layer="working",
                maturity_status="working_mature",
                source_confidence=1.0,
            ),
            "projection-member": NodeCandidate(
                candidate_id="projection-member",
                node_type="Decision",
                title="New resolved decision",
                content="new resolved decision",
                source_artifact_ref=source_ref,
                graph_layer="working",
                maturity_status="working_mature",
                source_confidence=1.0,
            ),
        }
        hints = {
            "projection-owner": ReconciliationHint(
                candidate_id="projection-owner",
                operation=ReconciliationOperation.NOOP,
                target_node_id=ROOT_ID,
                confidence=1.0,
                reason="owner already exists",
            ),
            "projection-member": ReconciliationHint(
                candidate_id="projection-member",
                operation=ReconciliationOperation.SUPERSEDE,
                target_node_id="decision-current",
                confidence=1.0,
                reason="resolved RDL content changed",
            ),
        }
        edges = {
            "projection-belongs-to": EdgeCandidate(
                candidate_id="projection-belongs-to",
                edge_type="belongs_to",
                from_candidate_id="projection-member",
                to_candidate_id="projection-owner",
                confidence=1.0,
                layer="deterministic",
                rule_id="belongs_to/relational_rdl_decision@v2.0",
                created_by="worker_layer1",
            ),
            "projection-mentions-owner": EdgeCandidate(
                candidate_id="projection-mentions-owner",
                edge_type="mentions",
                from_candidate_id="projection-member",
                to_candidate_id="projection-owner",
                confidence=1.0,
                layer="deterministic",
                rule_id="mentions/relational_rdl_owner@v2.0",
                created_by="worker_layer1",
            ),
        }
        intent = RelationalProjectionActiveSetIntent(
            owner_type="refinement",
            owner_id=OWNER_ID,
            namespace="rdl",
            active_refs=(
                RelationalProjectionActiveRef(
                    node_type="Decision",
                    candidate_id="projection-member",
                    source_artifact_ref=source_ref,
                ),
            ),
        )
        transaction = CommunityKuzuGraphTransaction()
        monkeypatch.setattr(
            primitives,
            "get_kg_registry",
            lambda: SimpleNamespace(graph_transaction=transaction),
        )

        (
            candidate_to_graph_id,
            _counters,
            _records,
            _committed_at,
            _connectivity,
            _cognitive_source_records,
        ) = await asyncio.to_thread(
            primitives._do_graph_commit,
            board_id,
            session_id,
            candidates,
            edges,
            hints,
            "system:layer1_worker",
            _FixedEmbedder(),
            "healthy",
            session_content_hash="c8-rdl-current-state-hash",
            session_artifact_id=OWNER_ID,
            session_artifact_type="refinement",
            relational_projection_candidate_ids=frozenset({"projection-member"}),
            relational_projection_active_set_intent=intent,
        )

        assert candidate_to_graph_id["projection-member"] == "decision-current"
        with kg_runtime.open_board_connection(board_id) as (_db, conn):
            nodes = conn.execute(
                "MATCH (n:Decision) "
                "WHERE n.source_artifact_ref = $source_ref "
                "RETURN n.id, n.content, n.revocation_reason, "
                "n.superseded_by ORDER BY n.id",
                {"source_ref": source_ref},
            )
            try:
                assert nodes.get_next() == [
                    "decision-current",
                    "new resolved decision",
                    "",
                    None,
                ]
                assert not nodes.has_next()
            finally:
                nodes.close()
            owned = conn.execute(
                "MATCH (n:Decision)-[r:belongs_to]->"
                "(owner:Entity {id: $owner_node_id}) "
                "WHERE n.source_artifact_ref = $source_ref "
                "AND owner.source_artifact_ref = $owner_ref "
                "RETURN n.id, r.rule_id",
                {
                    "owner_node_id": ROOT_ID,
                    "source_ref": source_ref,
                    "owner_ref": f"refinement:{OWNER_ID}",
                },
            )
            try:
                assert owned.get_next() == [
                    "decision-current",
                    "belongs_to/relational_rdl_decision@v2.0",
                ]
                assert not owned.has_next()
            finally:
                owned.close()
            supersedes = conn.execute(
                "MATCH (newer:Decision)-[r:supersedes]->"
                "(older:Decision) "
                "WHERE newer.source_artifact_ref = $source_ref "
                "OR older.source_artifact_ref = $source_ref "
                "RETURN count(r)",
                {"source_ref": source_ref},
            )
            try:
                assert supersedes.get_next()[0] == 0
            finally:
                supersedes.close()
    finally:
        if scope is not None:
            await scope.rollback()
        kg_runtime.close_all_connections(board_id)


class _FixedEmbedder:
    def encode(self, _text: str) -> list[float]:
        return [0.0] * 384


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unresolved_candidate", "expected_code"),
    (
        (
            "projection-member",
            "relational_projection_member_unresolved",
        ),
        (
            "projection-owner",
            "relational_projection_owner_unresolved",
        ),
    ),
    ids=("member-unresolved", "owner-unresolved"),
)
async def test_real_kuzu_late_projection_resolution_error_compensates_all_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unresolved_candidate: str,
    expected_code: str,
) -> None:
    board_id = f"c8-projection-resolution-{uuid4().hex}"
    session_id = f"c8-session-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()
    kg_runtime.bootstrap_board_graph(board_id)

    owner_ref = f"refinement:{OWNER_ID}"
    board_ref = f"board:{board_id}"
    member_ref = _decision_ref(OWNER_ID, "ledger-resolution-failure")
    candidates = {
        "board-root": NodeCandidate(
            candidate_id="board-root",
            node_type="Entity",
            title="Board root",
            content="Board root",
            source_artifact_ref=board_ref,
            graph_layer="working",
            maturity_status="working_mature",
            source_confidence=1.0,
        ),
        "projection-owner": NodeCandidate(
            candidate_id="projection-owner",
            node_type="Entity",
            title="Projection owner",
            content="Refinement root",
            source_artifact_ref=owner_ref,
            graph_layer="working",
            maturity_status="working_mature",
            source_confidence=1.0,
        ),
        "projection-member": NodeCandidate(
            candidate_id="projection-member",
            node_type="Decision",
            title="Resolved RDL decision",
            content="Relational projection",
            source_artifact_ref=member_ref,
            graph_layer="working",
            maturity_status="working_mature",
            source_confidence=1.0,
        ),
    }
    hints = {
        candidate_id: ReconciliationHint(
            candidate_id=candidate_id,
            operation=ReconciliationOperation.ADD,
            target_node_id=None,
            confidence=1.0,
            reason="C8 late-resolution compensation test",
        )
        for candidate_id in candidates
    }
    edges = {
        "projection-belongs-to": EdgeCandidate(
            candidate_id="projection-belongs-to",
            edge_type="belongs_to",
            from_candidate_id="projection-member",
            to_candidate_id="projection-owner",
            confidence=1.0,
            layer="deterministic",
            rule_id="belongs_to/relational_rdl_decision@v2.0",
            created_by="worker_layer1",
        ),
        "projection-mentions-owner": EdgeCandidate(
            candidate_id="projection-mentions-owner",
            edge_type="mentions",
            from_candidate_id="projection-member",
            to_candidate_id="projection-owner",
            confidence=1.0,
            layer="deterministic",
            rule_id="mentions/relational_rdl_owner@v2.0",
            created_by="worker_layer1",
        ),
        "owner-belongs-to-board": EdgeCandidate(
            candidate_id="owner-belongs-to-board",
            edge_type="belongs_to",
            from_candidate_id="projection-owner",
            to_candidate_id="board-root",
            confidence=1.0,
            layer="deterministic",
            rule_id="belongs_to/refinement_to_board@v2.0",
            created_by="worker_layer1",
        ),
    }
    intent = RelationalProjectionActiveSetIntent(
        owner_type="refinement",
        owner_id=OWNER_ID,
        namespace="rdl",
        active_refs=(
            RelationalProjectionActiveRef(
                node_type="Decision",
                candidate_id="projection-member",
                source_artifact_ref=member_ref,
            ),
        ),
    )
    transaction = CommunityKuzuGraphTransaction()
    monkeypatch.setattr(
        primitives,
        "get_kg_registry",
        lambda: SimpleNamespace(graph_transaction=transaction),
    )
    observed_before_error: dict[str, object] = {}

    def _raise_after_projection_writes(
        orchestrator: TransactionOrchestrator,
        active_set_intent: ProjectionActiveSetIntent,
    ) -> None:
        assert active_set_intent.owner_node_id is not None
        assert len(active_set_intent.active_nodes) == 1
        member = active_set_intent.active_nodes[0]
        observed_before_error["owner"] = (
            orchestrator.graph_scope.snapshot_node_properties(
                "Entity",
                active_set_intent.owner_node_id,
                ("source_artifact_ref",),
            )
        )
        observed_before_error["member"] = (
            orchestrator.graph_scope.snapshot_node_properties(
                member.node_type,
                member.node_id,
                ("source_artifact_ref",),
            )
        )
        assert observed_before_error["owner"] is not None
        assert observed_before_error["member"] is not None
        raise primitives.KGPrimitiveError(
            expected_code,
            "injected late projection resolution failure",
            session_id=session_id,
            details={"candidate_id": unresolved_candidate},
        )

    monkeypatch.setattr(
        TransactionOrchestrator,
        "reconcile_projection_active_set",
        _raise_after_projection_writes,
    )

    try:
        with pytest.raises(primitives.KGPrimitiveError) as excinfo:
            await asyncio.to_thread(
                primitives._do_graph_commit,
                board_id,
                session_id,
                candidates,
                edges,
                hints,
                "system:layer1_worker",
                _FixedEmbedder(),
                "healthy",
                session_content_hash="c8-content-hash",
                session_artifact_id=OWNER_ID,
                session_artifact_type="refinement",
                relational_projection_candidate_ids=frozenset({"projection-member"}),
                relational_projection_active_set_intent=intent,
            )

        assert excinfo.value.code == expected_code
        assert observed_before_error["owner"] is not None
        assert observed_before_error["member"] is not None
        assert (
            _count_nodes_by_source_ref(
                board_id,
                node_type="Entity",
                source_ref=board_ref,
            )
            == 0
        )
        assert (
            _count_nodes_by_source_ref(
                board_id,
                node_type="Entity",
                source_ref=owner_ref,
            )
            == 0
        )
        assert (
            _count_nodes_by_source_ref(
                board_id,
                node_type="Decision",
                source_ref=member_ref,
            )
            == 0
        )
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_real_kuzu_property_before_image_restores_apply_then_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"c8-property-before-image-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()
    scope: GraphTransactionScope | None = None

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        scope.create_node(
            "Decision",
            "decision-before-image",
            _node_attrs(
                _decision_ref(OWNER_ID, "ledger-before-image"),
                content="original content",
            ),
            source_session_id="seed-session",
        )

        original_update = scope.update_node

        def _apply_then_raise(
            node_type: str,
            node_id: str,
            attrs: dict[str, object],
        ) -> None:
            original_update(node_type, node_id, attrs)
            raise RuntimeError("injected_driver_failure_after_apply")

        monkeypatch.setattr(scope, "update_node", _apply_then_raise)
        orchestrator = TransactionOrchestrator(
            scope,
            session_id="failing-session",
            board_id=board_id,
        )

        with pytest.raises(
            RuntimeError,
            match="injected_driver_failure_after_apply",
        ):
            orchestrator.update_node(
                "Decision",
                "decision-before-image",
                {"content": "temporary content"},
            )

        assert (
            _content(scope, "Decision", "decision-before-image") == "temporary content"
        )
        await orchestrator.compensate()
        assert (
            _content(scope, "Decision", "decision-before-image") == "original content"
        )

        await scope.commit()
    finally:
        if scope is not None:
            await scope.rollback()
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_real_kuzu_active_set_restores_after_commit_then_raise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"c8-active-set-after-commit-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()
    scope: GraphTransactionScope | None = None

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        scope = await CommunityKuzuGraphTransaction().begin(board_id)
        scope.create_node(
            "Entity",
            ROOT_ID,
            {
                "title": ROOT_ID,
                "source_artifact_ref": f"refinement:{OWNER_ID}",
            },
            source_session_id="root-session",
        )
        _create_projection_node(
            scope,
            node_type="Decision",
            node_id="decision-after-commit",
            source_ref=_decision_ref(OWNER_ID, "ledger-after-commit"),
            root_id=ROOT_ID,
            content="must be restored",
        )

        original_execute = scope.execute
        commit_raised = False

        def _commit_then_raise(cypher, params=None):
            nonlocal commit_raised
            result = original_execute(cypher, params)
            if not commit_raised and cypher.strip().upper() == "COMMIT":
                commit_raised = True
                raise RuntimeError("injected_driver_failure_after_commit")
            return result

        monkeypatch.setattr(scope, "execute", _commit_then_raise)

        with pytest.raises(
            ProjectionActiveSetReconciliationError,
        ) as excinfo:
            scope.reconcile_projection_active_set(
                ProjectionActiveSetIntent(
                    owner_type="refinement",
                    owner_id=OWNER_ID,
                    namespace="rdl",
                    owner_node_id=ROOT_ID,
                    active_nodes=(),
                )
            )

        assert excinfo.value.code == "projection_active_set_apply_failed"
        assert excinfo.value.receipt is not None
        assert len(excinfo.value.receipt.before_images) == 1
        assert _reason(scope, "Decision", "decision-after-commit") == ""
        assert (
            _incident_edge_count(
                scope,
                "Decision",
                "decision-after-commit",
            )
            == 1
        )

        await scope.commit()
    finally:
        if scope is not None:
            await scope.rollback()
        kg_runtime.close_all_connections(board_id)
