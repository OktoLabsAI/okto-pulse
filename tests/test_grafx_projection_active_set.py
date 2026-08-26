"""M-PULSE-1 regressions for the Grafx projection active-set primitives.

Every test here runs against a real durable Grafx database.  The provider stages its whole
scope, so "atomic" means something specific and testable: either the complete active set is
visible after commit or none of it is, and a refusal never leaves a partial one staged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import okto_grafx
import pytest
from okto_pulse.core.kg.interfaces.graph_errors import GraphError, GraphLockContention
from okto_pulse.core.kg.interfaces.graph_transaction import (
    SOURCE_PROJECTION_REMOVED_REASON,
    ProjectionActiveSetIntent,
    ProjectionActiveSetReconciliationError,
    ProjectionEdgeRef,
    ProjectionNodeRef,
)

from okto_pulse.community.adapters import grafx_graph_transaction as provider_module

BOARD_ID = "grafx-projection-board"
OWNER_ID = "ref-1"
FOREIGN_OWNER_ID = "ref-2"
OWNER_NODE_ID = "owner-entity"
FOREIGN_OWNER_NODE_ID = "foreign-owner-entity"
SPEC_ROOT_ID = "spec-root"
SYSTEM_AGENT = "system:rdl-projector"
DECISION_RULE = "belongs_to/relational_rdl_decision@v2.0"
ALTERNATIVE_RULE = "belongs_to/relational_rdl_alternative@v2.0"
RELATES_RULE = "relates_to/relational_rdl_alternative@v2.0"
DEPENDENCY_RULE_A = "precedes/spec_dependency/rule-a"
DEPENDENCY_RULE_B = "precedes/spec_dependency/rule-b"
AUTHORED_RULE = "manual/authored-by-hand"
ALTERNATIVE_HASH = "a" * 64
FOREIGN_HASH = "b" * 64

KEEP_DECISION_REF = f"refinement:{OWNER_ID}:rdl:ledger-1:decision"
STALE_DECISION_REF = f"refinement:{OWNER_ID}:rdl:ledger-2:decision"
KEEP_ALTERNATIVE_REF = (
    f"refinement:{OWNER_ID}:rdl:ledger-1:alternative:{ALTERNATIVE_HASH}"
)
FOREIGN_DECISION_REF = f"refinement:{FOREIGN_OWNER_ID}:rdl:ledger-9:decision"

NODE_TYPES = ("Entity", "Decision", "Alternative")
RELATIONSHIP_PAIRS = (
    ("belongs_to", "Decision", "Entity"),
    ("belongs_to", "Alternative", "Entity"),
    ("relates_to", "Decision", "Alternative"),
    ("precedes", "Entity", "Entity"),
    ("supports", "Entity", "Entity"),
)
_PHYSICAL_TABLES = {
    ("belongs_to", "Decision", "Entity"): "belongs_to_decision",
    ("belongs_to", "Alternative", "Entity"): "belongs_to_alternative",
    ("relates_to", "Decision", "Alternative"): "relates_to",
    ("precedes", "Entity", "Entity"): "precedes",
    ("supports", "Entity", "Entity"): "supports",
}
EDGE_PROPERTIES = ("confidence", "created_by_session_id", "rule_id")
_FENCE_LOST = "test graph write fence lost"
_INJECTED_AFTER_EDGE_DELETION = "injected after the incident edges were deleted"
_INJECTED_COMPENSATION_REFUSAL = "compensation refused"
_INJECTED_SIGNAL = "injected process signal after a staged write"


def _resolve_table(edge_type: str, from_type: str, to_type: str) -> str:
    return _PHYSICAL_TABLES[(edge_type, from_type, to_type)]


def test_the_provider_under_test_is_this_worktree() -> None:
    """Resolution is asserted, not assumed: a suite may silently exercise another checkout.

    ``tests/conftest.py`` anchors the checkout paths itself and wins over ``PYTHONPATH``, so
    running from a worktree is not on its own enough to know which source ran.  Everything
    below is a statement about the file this names.
    """

    resolved = Path(provider_module.__file__).resolve()
    expected = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "okto_pulse"
        / "community"
        / "adapters"
        / "grafx_graph_transaction.py"
    )
    assert resolved == expected.resolve(), f"provider resolved to {resolved}"


@dataclass
class _DeterministicFence:
    """Controllable Pulse authority used to prove checks happen before effects."""

    allowed: bool = True
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_from_call: int | None = None

    def __call__(self, board_id: str, phase: str) -> None:
        self.calls.append((board_id, phase))
        if self.fail_from_call is not None and len(self.calls) >= self.fail_from_call:
            raise GraphLockContention(
                _FENCE_LOST,
                details={"board_id": board_id, "phase": phase},
            )
        if not self.allowed:
            raise GraphLockContention(
                _FENCE_LOST,
                details={"board_id": board_id, "phase": phase},
            )


class _InjectedFailure(RuntimeError):
    """Deterministic failure injected after the first projection write."""


class _InjectedProcessSignal(BaseException):
    """Non-Exception control-flow signal injected after a staged projection write."""


@pytest.fixture
def grafx_database(tmp_path: Path) -> Any:
    database = okto_grafx.connect(tmp_path / "grafx-projection-board")
    with database.begin("write") as schema:
        schema.execute(
            "CREATE VECTOR SPACE pulse_test {dimension: 4, metric: 'cosine'}"
        )
        for node_type in NODE_TYPES:
            schema.execute(
                f"CREATE NODE TABLE {node_type}("
                "id STRING, source_session_id STRING, title STRING, "
                "content STRING, revocation_reason STRING, "
                "source_artifact_ref STRING, created_by_agent STRING, "
                "relevance_score DOUBLE, embedding VECTOR(pulse_test), "
                "PRIMARY KEY(id))"
            )
        for edge_type, from_type, to_type in RELATIONSHIP_PAIRS:
            physical = _resolve_table(edge_type, from_type, to_type)
            schema.execute(
                f"CREATE REL TABLE {physical}("
                f"FROM {from_type} TO {to_type}, confidence DOUBLE, "
                "created_by_session_id STRING, rule_id STRING)"
            )
        # Present in the catalog and absent from the configured pairs on purpose: an edge
        # stored here has no logical name, which is what the inversion must refuse.
        schema.execute(
            "CREATE REL TABLE shadow_link(FROM Decision TO Entity, note STRING)"
        )
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def fence() -> _DeterministicFence:
    return _DeterministicFence()


def _provider(
    database: Any,
    fence: _DeterministicFence,
    *,
    relationship_pairs: tuple[tuple[str, str, str], ...] = RELATIONSHIP_PAIRS,
) -> Any:
    def resolve_database(board_id: str) -> Any:
        if board_id != BOARD_ID:
            raise KeyError(board_id)
        return database

    return provider_module.CommunityGrafxGraphTransaction(
        database_resolver=resolve_database,
        revalidate_fence=fence,
        node_types=NODE_TYPES,
        relationship_pairs=relationship_pairs,
        relationship_table_resolver=_resolve_table,
    )


def _projection_node(
    scope: Any,
    node_type: str,
    node_id: str,
    source_artifact_ref: str,
    *,
    created_by_agent: str = SYSTEM_AGENT,
    revocation_reason: str = "",
) -> None:
    scope.create_node(
        node_type,
        node_id,
        {
            "title": f"{node_type} {node_id}",
            "content": f"payload of {node_id}",
            "revocation_reason": revocation_reason,
            "source_artifact_ref": source_artifact_ref,
            "created_by_agent": created_by_agent,
            "relevance_score": 0.5,
        },
        source_session_id=f"session-{node_id}",
    )


def _edge(
    scope: Any,
    edge_type: str,
    from_type: str,
    to_type: str,
    from_id: str,
    to_id: str,
    rule_id: str,
    *,
    confidence: float = 0.75,
) -> None:
    assert scope.create_edge(
        edge_type,
        from_type,
        to_type,
        from_id,
        to_id,
        {
            "confidence": confidence,
            "created_by_session_id": "seeding-session",
            "rule_id": rule_id,
        },
    )


async def _seed(provider: Any) -> None:
    """Build a board that carries in-scope, out-of-scope and hand-authored graph alike."""

    async with await provider.begin(BOARD_ID) as scope:
        _projection_node(
            scope,
            "Entity",
            OWNER_NODE_ID,
            f"refinement:{OWNER_ID}",
            created_by_agent="human:author",
        )
        _projection_node(
            scope,
            "Entity",
            FOREIGN_OWNER_NODE_ID,
            f"refinement:{FOREIGN_OWNER_ID}",
            created_by_agent="human:author",
        )
        _projection_node(
            scope,
            "Entity",
            SPEC_ROOT_ID,
            "spec:root",
            created_by_agent="human:author",
        )
        for prerequisite in ("prereq-a", "prereq-b", "prereq-c"):
            _projection_node(
                scope,
                "Entity",
                prerequisite,
                f"spec:{prerequisite}",
                created_by_agent="human:author",
            )
        _projection_node(scope, "Decision", "dec-keep", KEEP_DECISION_REF)
        _projection_node(scope, "Decision", "dec-stale", STALE_DECISION_REF)
        _projection_node(scope, "Alternative", "alt-keep", KEEP_ALTERNATIVE_REF)
        _projection_node(scope, "Decision", "dec-foreign", FOREIGN_DECISION_REF)

        _edge(
            scope,
            "belongs_to",
            "Decision",
            "Entity",
            "dec-keep",
            OWNER_NODE_ID,
            DECISION_RULE,
        )
        _edge(
            scope,
            "belongs_to",
            "Decision",
            "Entity",
            "dec-stale",
            OWNER_NODE_ID,
            DECISION_RULE,
        )
        _edge(
            scope,
            "belongs_to",
            "Alternative",
            "Entity",
            "alt-keep",
            OWNER_NODE_ID,
            ALTERNATIVE_RULE,
        )
        _edge(
            scope,
            "belongs_to",
            "Decision",
            "Entity",
            "dec-foreign",
            FOREIGN_OWNER_NODE_ID,
            DECISION_RULE,
        )
        # Decision -> Alternative: distinct types on either end, so a laterality mistake in
        # the incidence match cannot hide behind a symmetric pair.
        _edge(
            scope,
            "relates_to",
            "Decision",
            "Alternative",
            "dec-stale",
            "alt-keep",
            RELATES_RULE,
        )
        _edge(
            scope,
            "supports",
            "Entity",
            "Entity",
            OWNER_NODE_ID,
            FOREIGN_OWNER_NODE_ID,
            "manual/support",
        )
        _edge(
            scope,
            "precedes",
            "Entity",
            "Entity",
            "prereq-a",
            SPEC_ROOT_ID,
            DEPENDENCY_RULE_A,
        )
        _edge(
            scope,
            "precedes",
            "Entity",
            "Entity",
            "prereq-b",
            SPEC_ROOT_ID,
            DEPENDENCY_RULE_B,
        )
        # Hand-authored precedence into the same root: not this projection's to remove.
        _edge(
            scope,
            "precedes",
            "Entity",
            "Entity",
            "prereq-c",
            SPEC_ROOT_ID,
            AUTHORED_RULE,
        )


def _nodes(database: Any) -> tuple[tuple[Any, ...], ...]:
    rows: list[tuple[Any, ...]] = []
    for node_type in NODE_TYPES:
        rows.extend(
            (node_type, *row)
            for row in database.execute(
                f"MATCH (n:{node_type}) RETURN n.id, n.title, n.content, "
                "n.revocation_reason, n.source_artifact_ref, n.created_by_agent, "
                "n.relevance_score, n.source_session_id"
            ).rows
        )
    return tuple(sorted(rows, key=repr))


def _edges(database: Any) -> tuple[tuple[Any, ...], ...]:
    rows: list[tuple[Any, ...]] = []
    for edge_type, from_type, to_type in RELATIONSHIP_PAIRS:
        physical = _resolve_table(edge_type, from_type, to_type)
        projection = ", ".join(f"r.{name}" for name in EDGE_PROPERTIES)
        rows.extend(
            (edge_type, from_type, to_type, *row)
            for row in database.execute(
                f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type}) "
                f"RETURN a.id, b.id, {projection}"
            ).rows
        )
    return tuple(sorted(rows, key=repr))


def _graph(database: Any) -> tuple[Any, Any]:
    return _nodes(database), _edges(database)


def _revocation_reason(database: Any, node_type: str, node_id: str) -> str:
    rows = database.execute(
        f"MATCH (n:{node_type}) WHERE n.id = $node_id RETURN n.revocation_reason",
        {"node_id": node_id},
    ).rows
    assert len(rows) == 1
    return str(rows[0][0] or "")


def _rdl_intent(
    *,
    active_nodes: tuple[ProjectionNodeRef, ...],
    owner_node_id: str | None = OWNER_NODE_ID,
    owner_type: str = "refinement",
    namespace: str = "rdl",
    active_edges: tuple[ProjectionEdgeRef, ...] = (),
) -> ProjectionActiveSetIntent:
    return ProjectionActiveSetIntent(
        owner_type=owner_type,
        owner_id=OWNER_ID,
        namespace=namespace,
        owner_node_id=owner_node_id,
        active_nodes=active_nodes,
        active_edges=active_edges,
    )


KEEP_DECISION = ProjectionNodeRef("Decision", "dec-keep", KEEP_DECISION_REF)
KEEP_ALTERNATIVE = ProjectionNodeRef("Alternative", "alt-keep", KEEP_ALTERNATIVE_REF)


def _dependency_intent(
    *,
    active_edges: tuple[ProjectionEdgeRef, ...],
    owner_node_id: str | None = SPEC_ROOT_ID,
    active_nodes: tuple[ProjectionNodeRef, ...] = (),
) -> ProjectionActiveSetIntent:
    return ProjectionActiveSetIntent(
        owner_type="spec",
        owner_id=OWNER_ID,
        namespace="dependencies",
        owner_node_id=owner_node_id,
        active_nodes=active_nodes,
        active_edges=active_edges,
    )


ACTIVE_DEPENDENCY_A = ProjectionEdgeRef(
    "precedes", "Entity", "Entity", "prereq-a", SPEC_ROOT_ID, DEPENDENCY_RULE_A
)


# --------------------------------------------------------------------------------------
# refinement/rdl route
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stale_member_loses_every_incident_edge_and_keeps_its_identity(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )

    assert _revocation_reason(grafx_database, "Decision", "dec-stale") == (
        SOURCE_PROJECTION_REMOVED_REASON
    )
    assert _revocation_reason(grafx_database, "Decision", "dec-keep") == ""
    assert _revocation_reason(grafx_database, "Alternative", "alt-keep") == ""
    # The identity survives; only the projection's claim on it is withdrawn.
    assert grafx_database.execute(
        "MATCH (n:Decision) WHERE n.id = $id RETURN n.title", {"id": "dec-stale"}
    ).rows

    edges = _edges(grafx_database)
    incident_to_stale = [row for row in edges if "dec-stale" in row]
    assert incident_to_stale == [], incident_to_stale
    # Both directions were incident to the removed node, and both are gone.
    assert not any(row[0] == "relates_to" for row in edges)
    # Everything outside the projection is untouched.
    assert any(row[0] == "supports" for row in edges)
    assert any(
        row[:4] == ("belongs_to", "Decision", "Entity", "dec-keep") for row in edges
    )
    assert any(
        row[:4] == ("belongs_to", "Decision", "Entity", "dec-foreign") for row in edges
    )

    assert {image.node_id for image in receipt.before_images} == {"dec-stale"}
    stale_image = receipt.before_images[0]
    assert stale_image.attrs["title"] == "Decision dec-stale"
    assert {edge.edge_type for edge in stale_image.incident_edges} == {
        "belongs_to",
        "relates_to",
    }


@pytest.mark.asyncio
async def test_a_removed_member_returning_to_the_active_set_is_restored(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    async with await provider.begin(BOARD_ID) as scope:
        scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )

    stale_ref = ProjectionNodeRef("Decision", "dec-stale", STALE_DECISION_REF)
    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE, stale_ref))
        )

    assert _revocation_reason(grafx_database, "Decision", "dec-stale") == ""
    assert {image.node_id for image in receipt.before_images} == {"dec-stale"}


@pytest.mark.asyncio
async def test_compensation_restores_the_payload_and_every_incident_edge(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )
        scope.compensate_projection_active_set(receipt)

    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_compensation_is_repeatable_in_a_fresh_scope(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """A compensator may be retried, and may run in a scope that never applied anything."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )
    assert _graph(grafx_database) != before

    async with await provider.begin(BOARD_ID) as scope:
        scope.compensate_projection_active_set(receipt)
    restored = _graph(grafx_database)
    assert restored == before

    async with await provider.begin(BOARD_ID) as scope:
        scope.compensate_projection_active_set(receipt)
    assert _graph(grafx_database) == restored


@pytest.mark.asyncio
async def test_a_member_with_foreign_revocation_provenance_is_left_alone(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    async with await provider.begin(BOARD_ID) as scope:
        scope.update_node(
            "Decision",
            "dec-stale",
            {"revocation_reason": "superseded_by_human"},
        )
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )

    assert receipt.before_images == ()
    assert _graph(grafx_database) == before


@pytest.mark.parametrize(
    ("intent_kwargs", "code"),
    [
        (
            {"owner_type": "spec", "namespace": "rdl"},
            "projection_active_set_scope_invalid",
        ),
        (
            {"namespace": "other"},
            "projection_active_set_scope_invalid",
        ),
        (
            {"active_edges": (ACTIVE_DEPENDENCY_A,)},
            "projection_active_set_member_invalid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_rdl_route_refuses_an_out_of_scope_intent(
    grafx_database: Any,
    fence: _DeterministicFence,
    intent_kwargs: dict[str, Any],
    code: str,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(
                    active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE),
                    **intent_kwargs,
                )
            )

    assert raised.value.code == code
    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_a_duplicate_source_reference_is_refused_before_any_mutation(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    duplicate = ProjectionNodeRef("Decision", "dec-keep", KEEP_DECISION_REF)
    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, duplicate))
            )

    assert raised.value.code == "projection_active_set_member_duplicate"
    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_a_member_from_another_owner_is_outside_the_scope(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    foreign = ProjectionNodeRef("Decision", "dec-foreign", FOREIGN_DECISION_REF)

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, foreign))
            )

    assert raised.value.code == "projection_active_set_member_invalid"


@pytest.mark.asyncio
async def test_an_unowned_active_member_is_reported_missing(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    absent_ref = f"refinement:{OWNER_ID}:rdl:ledger-404:decision"
    absent = ProjectionNodeRef("Decision", "dec-absent", absent_ref)

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, absent))
            )

    assert raised.value.code == "projection_active_set_member_missing"


@pytest.mark.asyncio
async def test_a_reference_resolving_to_another_identity_is_a_conflict(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    wrong_identity = ProjectionNodeRef("Decision", "dec-stale", KEEP_DECISION_REF)

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(wrong_identity,))
            )

    assert raised.value.code == "projection_active_set_identity_conflict"


@pytest.mark.asyncio
async def test_one_source_reference_on_two_nodes_is_ambiguous(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    async with await provider.begin(BOARD_ID) as scope:
        _projection_node(scope, "Decision", "dec-twin", KEEP_DECISION_REF)
        _edge(
            scope,
            "belongs_to",
            "Decision",
            "Entity",
            "dec-twin",
            OWNER_NODE_ID,
            DECISION_RULE,
        )
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
            )

    assert raised.value.code == "projection_active_set_source_ref_ambiguous"
    assert _graph(grafx_database) == before


# --------------------------------------------------------------------------------------
# spec/dependencies route
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_dependency_edges_go_and_hand_authored_precedence_stays(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _dependency_intent(active_edges=(ACTIVE_DEPENDENCY_A,))
        )

    precedes = {
        (row[3], row[4], row[7])
        for row in _edges(grafx_database)
        if row[0] == "precedes"
    }
    assert precedes == {
        ("prereq-a", SPEC_ROOT_ID, DEPENDENCY_RULE_A),
        # Owned by nobody in this namespace: the rule prefix is what makes an edge ours.
        ("prereq-c", SPEC_ROOT_ID, AUTHORED_RULE),
    }
    assert len(receipt.edge_before_images) == 1
    removed = receipt.edge_before_images[0]
    assert removed.from_id == "prereq-b"
    assert removed.attrs["rule_id"] == DEPENDENCY_RULE_B
    # The receipt carries the complete payload, not only the identity.
    assert removed.attrs["confidence"] == pytest.approx(0.75)
    assert removed.attrs["created_by_session_id"] == "seeding-session"


@pytest.mark.asyncio
async def test_two_desired_edges_over_one_pair_are_refused_even_under_different_rules(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """The Core/memory reading governs here: the endpoints alone make the set ambiguous."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)
    twin = ProjectionEdgeRef(
        "precedes", "Entity", "Entity", "prereq-a", SPEC_ROOT_ID, DEPENDENCY_RULE_B
    )

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _dependency_intent(active_edges=(ACTIVE_DEPENDENCY_A, twin))
            )

    assert raised.value.code == "projection_active_set_member_invalid"
    assert _graph(grafx_database) == before


@pytest.mark.parametrize(
    "edge",
    [
        ProjectionEdgeRef(
            "supports", "Entity", "Entity", "prereq-a", SPEC_ROOT_ID, DEPENDENCY_RULE_A
        ),
        ProjectionEdgeRef(
            "precedes",
            "Decision",
            "Entity",
            "dec-keep",
            SPEC_ROOT_ID,
            DEPENDENCY_RULE_A,
        ),
        ProjectionEdgeRef(
            "precedes", "Entity", "Entity", "prereq-a", OWNER_NODE_ID, DEPENDENCY_RULE_A
        ),
        ProjectionEdgeRef(
            "precedes", "Entity", "Entity", "prereq-a", SPEC_ROOT_ID, AUTHORED_RULE
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_dependency_edge_outside_the_exact_scope_is_refused(
    grafx_database: Any,
    fence: _DeterministicFence,
    edge: ProjectionEdgeRef,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _dependency_intent(active_edges=(edge,))
            )

    assert raised.value.code == "projection_active_set_member_invalid"
    assert _graph(grafx_database) == before


@pytest.mark.parametrize(
    "intent_kwargs",
    [
        {"owner_node_id": None},
        {"active_nodes": (KEEP_DECISION,)},
    ],
)
@pytest.mark.asyncio
async def test_the_dependency_route_requires_its_root_and_owns_no_nodes(
    grafx_database: Any,
    fence: _DeterministicFence,
    intent_kwargs: dict[str, Any],
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _dependency_intent(
                    active_edges=(ACTIVE_DEPENDENCY_A,),
                    **intent_kwargs,
                )
            )

    assert raised.value.code == "projection_active_set_member_invalid"


@pytest.mark.asyncio
async def test_a_desired_dependency_edge_that_is_not_stored_is_missing(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)
    absent = ProjectionEdgeRef(
        "precedes", "Entity", "Entity", "prereq-c", SPEC_ROOT_ID, DEPENDENCY_RULE_A
    )

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _dependency_intent(active_edges=(ACTIVE_DEPENDENCY_A, absent))
            )

    assert raised.value.code == "projection_active_set_member_missing"
    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_dependency_compensation_is_exact_and_repeatable_in_a_fresh_scope(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _dependency_intent(active_edges=(ACTIVE_DEPENDENCY_A,))
        )
    assert _graph(grafx_database) != before

    async with await provider.begin(BOARD_ID) as scope:
        scope.compensate_projection_active_set(receipt)
    assert _graph(grafx_database) == before

    async with await provider.begin(BOARD_ID) as scope:
        scope.compensate_projection_active_set(receipt)
    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_an_empty_dependency_reconciliation_removes_every_owned_edge(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _dependency_intent(active_edges=())
        )

    precedes = {
        (row[3], row[7]) for row in _edges(grafx_database) if row[0] == "precedes"
    }
    assert precedes == {("prereq-c", AUTHORED_RULE)}
    assert {edge.from_id for edge in receipt.edge_before_images} == {
        "prereq-a",
        "prereq-b",
    }


# --------------------------------------------------------------------------------------
# fencing, atomicity and the physical-to-logical inversion
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_process_signal_after_a_write_keeps_its_own_identity(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt is not an outcome: reporting it as one is how it gets swallowed."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)
    original = provider_module._GrafxTransactionScope._remove_projection_member
    # Built out here so the assertion below can be about THIS object.  Matching the type
    # only would be satisfied by a provider that raised a fresh instance of the same class,
    # which is precisely the wrapping this test exists to forbid.
    primary_signal = _InjectedProcessSignal(_INJECTED_SIGNAL)

    def signalling_remove(self: Any, before_image: Any) -> None:
        self._projection_delete_incident_edges(
            before_image.node_type,
            before_image.node_id,
        )
        raise primary_signal

    monkeypatch.setattr(
        provider_module._GrafxTransactionScope,
        "_remove_projection_member",
        signalling_remove,
    )

    async with await provider.begin(BOARD_ID) as scope:
        # The signal itself reaches the caller, not a reconciliation error wrapping it.
        with pytest.raises(_InjectedProcessSignal) as escaped:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
            )
        assert escaped.value is primary_signal
        monkeypatch.setattr(
            provider_module._GrafxTransactionScope,
            "_remove_projection_member",
            original,
        )

    # And the board it interrupted is whole.
    assert _graph(grafx_database) == before


class _UnwrittenResult:
    """A mutation result that reports success for a write that never happened."""

    rows = (("dec-stale",),)


@pytest.mark.asyncio
async def test_a_removal_the_engine_did_not_apply_fails_typed_and_leaves_nothing_behind(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-write confirmation is load-bearing, and it is asked to prove it here.

    Everything else in this suite exercises writes that take, which is precisely why none of
    them can tell whether the confirmation is wired up at all.  This one lets the removal
    report success without applying, so a divergent confirmation is the only thing between a
    silent half-removal and the caller.
    """

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)
    original = provider_module._GrafxTransactionScope._mutation

    def unapplied_removal(
        self: Any,
        statement: str,
        params: Any = None,
        *,
        operation: str,
    ) -> Any:
        if operation == "remove_projection_member":
            # The fence is still honoured; only the write is dropped.
            self._fence(operation)
            return _UnwrittenResult()
        return original(self, statement, params, operation=operation)

    monkeypatch.setattr(
        provider_module._GrafxTransactionScope, "_mutation", unapplied_removal
    )

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
            )
        assert raised.value.code == "projection_active_set_apply_failed"
        cause = raised.value.__cause__
        assert isinstance(cause, GraphError)
        assert cause.details["code"] == "projection_stale_member_cleanup_unconfirmed"
        monkeypatch.setattr(
            provider_module._GrafxTransactionScope, "_mutation", original
        )

    # The incident edges really were deleted before the confirmation refused; the board is
    # whole only because the before-image put them back.
    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_restoring_parallel_edges_keeps_each_distinct_payload(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """Two edges over one pair that differ only in payload are two different edges."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    async with await provider.begin(BOARD_ID) as scope:
        _edge(
            scope,
            "belongs_to",
            "Decision",
            "Entity",
            "dec-stale",
            OWNER_NODE_ID,
            DECISION_RULE,
            confidence=0.25,
        )
    before = _graph(grafx_database)
    # before is (nodes, edges); the edges are the second half of it.
    parallel = [
        row
        for row in before[1]
        if row[:4] == ("belongs_to", "Decision", "Entity", "dec-stale")
    ]
    assert len(parallel) == 2, parallel
    assert len({row[5] for row in parallel}) == 2, parallel

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )
        scope.compensate_projection_active_set(receipt)

    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_compensation_refuses_to_restore_onto_a_node_that_is_gone(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """False from a node write is an outcome, not a quieter kind of success."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )
    with grafx_database.begin("write") as removal:
        removal.execute(
            "MATCH (n:Decision) WHERE n.id = $node_id DETACH DELETE n",
            {"node_id": "dec-stale"},
        )
    after_removal = _graph(grafx_database)

    scope = await provider.begin(BOARD_ID)
    with pytest.raises(GraphError) as raised:
        scope.compensate_projection_active_set(receipt)
    assert raised.value.details["code"] == (
        "projection_active_set_compensation_node_missing"
    )
    await scope.rollback()
    assert _graph(grafx_database) == after_removal


@pytest.mark.asyncio
async def test_compensation_removes_an_edge_that_appeared_after_the_apply(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """Exact means the recorded multiset and nothing else, not merely 'at least'.

    The three phases are three committed scopes on purpose.  Doing them in one would let the
    staged overlay answer the question, and the overlay is not where the audited failure
    lives: compensation has to reach a board it did not itself put into this state.
    """

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )
    applied = _graph(grafx_database)
    assert applied != before

    # Something else attaches an edge to the removed member, and commits it.
    async with await provider.begin(BOARD_ID) as scope:
        _edge(
            scope,
            "belongs_to",
            "Decision",
            "Entity",
            "dec-stale",
            FOREIGN_OWNER_NODE_ID,
            DECISION_RULE,
        )
    assert _graph(grafx_database) != applied

    async with await provider.begin(BOARD_ID) as scope:
        scope.compensate_projection_active_set(receipt)

    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_a_compensation_that_cannot_confirm_itself_poisons_the_scope(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-compensated board is not allowed to commit, and says so typed."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )
    after_apply = _graph(grafx_database)

    def restore_nothing(self: Any, edges: Any) -> None:
        del edges

    monkeypatch.setattr(
        provider_module._GrafxTransactionScope,
        "_projection_restore_edges",
        restore_nothing,
    )

    scope = await provider.begin(BOARD_ID)
    with pytest.raises(GraphError) as raised:
        scope.compensate_projection_active_set(receipt)
    assert raised.value.details["code"] == (
        "projection_active_set_compensation_unconfirmed"
    )

    # Poisoned: the node payload it did write must not reach commit.
    with pytest.raises(GraphError):
        scope.update_node("Decision", "dec-keep", {"title": "should not apply"})
    await scope.rollback()
    assert _graph(grafx_database) == after_apply


@pytest.mark.asyncio
async def test_restoring_parallel_edges_does_not_collapse_their_multiplicity(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """Two byte-identical edges are two edges; Grafx stores both, so both must come back."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    async with await provider.begin(BOARD_ID) as scope:
        # A second edge indistinguishable from the seeded one: nothing but the count tells
        # them apart, which is exactly what a set-based restore cannot preserve.
        _edge(
            scope,
            "belongs_to",
            "Decision",
            "Entity",
            "dec-stale",
            OWNER_NODE_ID,
            DECISION_RULE,
        )
    before = _graph(grafx_database)
    seeded = [
        row
        for row in before[1]
        if row[:4] == ("belongs_to", "Decision", "Entity", "dec-stale")
    ]
    assert len(seeded) == 2, seeded

    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )
        scope.compensate_projection_active_set(receipt)

    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_a_lost_fence_refuses_the_reconciliation_before_any_read(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        fence.allowed = False
        with pytest.raises(GraphLockContention):
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
            )
        fence.allowed = True

    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_authority_is_revalidated_before_every_projection_write(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """Not left to construction: the fence is counted against the writes it must guard."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)

    async with await provider.begin(BOARD_ID) as scope:
        fence.calls.clear()
        scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )

    phases = [phase for _board, phase in fence.calls]
    assert phases[0] == "reconcile_projection_active_set"
    # One removed member: two incident edges deleted, then the payload write.
    assert phases.count("delete_projection_incident_edge") == 2
    assert phases.count("remove_projection_member") == 1
    assert all(board == BOARD_ID for board, _phase in fence.calls)


@pytest.mark.asyncio
async def test_a_fence_lost_mid_apply_leaves_the_board_exactly_as_it_was(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """All-or-none under a mid-flight authority loss, proven by the whole graph."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        fence.calls.clear()
        # Let the reconciliation start and fail on the write that follows the first
        # incident-edge deletion, which is precisely the half-applied state.
        fence.fail_from_call = 3
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
            )
        fence.fail_from_call = None

    assert raised.value.code in {
        "projection_active_set_apply_failed",
        "projection_active_set_apply_and_restore_failed",
    }
    # Either it was put back, or the scope refused to commit; both leave this true.
    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_a_failure_after_the_first_write_restores_the_complete_before_image(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    original = provider_module._GrafxTransactionScope._remove_projection_member
    calls: list[str] = []

    def failing_remove(self: Any, before_image: Any) -> None:
        calls.append(before_image.node_id)
        # Delete the edges, then fail: the damage is real before the error is raised.
        self._projection_delete_incident_edges(
            before_image.node_type,
            before_image.node_id,
        )
        raise _InjectedFailure(_INJECTED_AFTER_EDGE_DELETION)

    monkeypatch.setattr(
        provider_module._GrafxTransactionScope,
        "_remove_projection_member",
        failing_remove,
    )

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
            )
        assert raised.value.code == "projection_active_set_apply_failed"
        assert isinstance(raised.value.__cause__, _InjectedFailure)
        # Restored inside the same live scope, not merely discarded at commit.
        monkeypatch.setattr(
            provider_module._GrafxTransactionScope,
            "_remove_projection_member",
            original,
        )

    assert calls == ["dec-stale"]
    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_an_unrestorable_failure_poisons_the_scope_so_nothing_commits(
    grafx_database: Any,
    fence: _DeterministicFence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the before-image cannot be put back, the staged scope must not reach commit."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    def failing_remove(self: Any, before_image: Any) -> None:
        self._projection_delete_incident_edges(
            before_image.node_type,
            before_image.node_id,
        )
        raise _InjectedFailure(_INJECTED_AFTER_EDGE_DELETION)

    def failing_compensate(self: Any, receipt: Any) -> None:
        raise _InjectedFailure(_INJECTED_COMPENSATION_REFUSAL)

    monkeypatch.setattr(
        provider_module._GrafxTransactionScope,
        "_remove_projection_member",
        failing_remove,
    )
    monkeypatch.setattr(
        provider_module._GrafxTransactionScope,
        "compensate_projection_active_set",
        failing_compensate,
    )

    scope = await provider.begin(BOARD_ID)
    with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
        scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )

    assert raised.value.code == "projection_active_set_apply_and_restore_failed"
    assert isinstance(raised.value.__cause__, _InjectedFailure)
    # Poisoned: the scope refuses further work and its staged effects never commit.
    with pytest.raises(GraphError):
        scope.update_node("Decision", "dec-keep", {"title": "should not apply"})
    await scope.rollback()

    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_an_edge_with_no_single_logical_name_refuses_before_mutating(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """Two logical types over one table would make a before-image restore the wrong edge."""

    ambiguous_pairs = (*RELATIONSHIP_PAIRS, ("mirrors", "Decision", "Entity"))

    def ambiguous_table(edge_type: str, from_type: str, to_type: str) -> str:
        if edge_type == "mirrors":
            return "belongs_to_decision"
        return _resolve_table(edge_type, from_type, to_type)

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    before = _graph(grafx_database)

    ambiguous_provider = provider_module.CommunityGrafxGraphTransaction(
        database_resolver=lambda board_id: grafx_database,
        revalidate_fence=fence,
        node_types=NODE_TYPES,
        relationship_pairs=ambiguous_pairs,
        relationship_table_resolver=ambiguous_table,
    )
    async with await ambiguous_provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
            )

    assert raised.value.code == "projection_active_set_snapshot_failed"
    assert _graph(grafx_database) == before


@pytest.mark.asyncio
async def test_a_stored_edge_with_no_logical_name_cannot_be_silently_dropped(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    """An unmapped table is not a licence to forget the edges it holds."""

    provider = _provider(grafx_database, fence)
    await _seed(provider)
    with grafx_database.begin("write") as seeding:
        seeding.execute(
            "MATCH (a:Decision), (b:Entity) "
            "WHERE a.id = $from_id AND b.id = $to_id "
            "CREATE (a)-[:shadow_link {note: 'unmapped'}]->(b)",
            {"from_id": "dec-stale", "to_id": OWNER_NODE_ID},
        )
    before = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        with pytest.raises(ProjectionActiveSetReconciliationError) as raised:
            scope.reconcile_projection_active_set(
                _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
            )

    assert raised.value.code == "projection_active_set_snapshot_failed"
    assert _graph(grafx_database) == before
    assert grafx_database.execute(
        "MATCH (a:Decision)-[r:shadow_link]->(b:Entity) RETURN a.id, r.note"
    ).rows


@pytest.mark.asyncio
async def test_an_empty_active_set_leaves_an_untouched_board_untouched(
    grafx_database: Any,
    fence: _DeterministicFence,
) -> None:
    provider = _provider(grafx_database, fence)
    await _seed(provider)
    async with await provider.begin(BOARD_ID) as scope:
        receipt = scope.reconcile_projection_active_set(
            _rdl_intent(active_nodes=(KEEP_DECISION, KEEP_ALTERNATIVE))
        )
        scope.compensate_projection_active_set(receipt)
    settled = _graph(grafx_database)

    async with await provider.begin(BOARD_ID) as scope:
        empty = scope.reconcile_projection_active_set(
            _rdl_intent(
                active_nodes=(
                    KEEP_DECISION,
                    KEEP_ALTERNATIVE,
                    ProjectionNodeRef("Decision", "dec-stale", STALE_DECISION_REF),
                )
            )
        )
    assert empty.before_images == ()
    assert _graph(grafx_database) == settled
