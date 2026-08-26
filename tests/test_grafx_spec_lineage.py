"""Real Grafx regressions for the bounded Spec-lineage primitives.

The fixture deliberately gives the logical ``belongs_to`` relationship a
different physical table name.  These tests therefore exercise both the real
Grafx transaction and Pulse's endpoint-aware relationship-table resolver.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import okto_grafx
import pytest
from okto_grafx import QueryResult, Timestamp, Transaction
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphLockContention,
)
from okto_pulse.core.kg.interfaces.graph_transaction import (
    SpecLineageEdgeSnapshot,
    SpecLineageReconciliationError,
)

from okto_pulse.community.adapters.grafx_graph_transaction import (
    CommunityGrafxGraphTransaction,
)

BOARD_ID = "grafx-spec-lineage-board"
SPEC_ID = "spec-source"
IDEATION_ID = "ideation-parent"
REFINEMENT_ID = "refinement-parent"
BOARD_ROOT_ID = "board-root"
LEGACY_ID = "legacy-parent"

LOGICAL_RELATIONSHIP = "belongs_to"
PHYSICAL_RELATIONSHIP = "belongs_to__entity__entity"

IDEATION_RULE = "belongs_to/spec_to_ideation@1.0"
REFINEMENT_RULE = "belongs_to/spec_to_refinement@1.0"
BOARD_RULE = "belongs_to/spec_to_board@1.0"
PARALLEL_NON_LINEAGE_RULE = "belongs_to/spec_reference@1.0"
LEGACY_RULE = "legacy_pre_v2"
SELF_LOOP_RULE = "belongs_to/spec_self_reference@1.0"

LINEAGE_RELATIONSHIP_PROPERTIES = (
    ("confidence", "DOUBLE"),
    ("created_by_session_id", "STRING"),
    ("created_at", "TIMESTAMP"),
    ("layer", "STRING"),
    ("rule_id", "STRING"),
    ("created_by", "STRING"),
    ("fallback_reason", "STRING"),
)


class _InjectedProcessSignal(BaseException):
    """Non-Exception signal raised after Grafx has staged a lineage mutation."""


@dataclass
class _Fence:
    allowed: bool = True
    calls: list[tuple[str, str]] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)

    def __call__(self, board_id: str, phase: str) -> None:
        self.calls.append((board_id, phase))
        self.trace.append("fence")
        if not self.allowed:
            raise GraphLockContention(
                "lineage test fence lost",
                details={"board_id": board_id, "phase": phase},
            )


@dataclass(frozen=True)
class _Harness:
    database: Any
    provider: CommunityGrafxGraphTransaction
    fence: _Fence


def _relationship_table(edge_type: str, from_type: str, to_type: str) -> str:
    assert (edge_type, from_type, to_type) == (
        LOGICAL_RELATIONSHIP,
        "Entity",
        "Entity",
    )
    return PHYSICAL_RELATIONSHIP


@pytest.fixture
def lineage_harness(tmp_path: Path) -> Any:
    database = okto_grafx.connect(tmp_path / "grafx-lineage")
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Entity("
            "id STRING, source_session_id STRING, PRIMARY KEY(id))"
        )
        properties = ", ".join(
            f"{name} {property_type}"
            for name, property_type in LINEAGE_RELATIONSHIP_PROPERTIES
        )
        schema.execute(
            f"CREATE REL TABLE {PHYSICAL_RELATIONSHIP}("
            f"FROM Entity TO Entity, {properties})"
        )

    fence = _Fence()

    def resolve_database(board_id: str) -> Any:
        if board_id != BOARD_ID:
            raise KeyError(board_id)
        return database

    provider = CommunityGrafxGraphTransaction(
        database_resolver=resolve_database,
        revalidate_fence=fence,
        node_types=("Entity",),
        relationship_pairs=((LOGICAL_RELATIONSHIP, "Entity", "Entity"),),
        relationship_table_resolver=_relationship_table,
    )
    try:
        yield _Harness(database=database, provider=provider, fence=fence)
    finally:
        database.close()


def _edge_attrs(
    rule_id: str,
    session_id: str,
    *,
    layer: str = "deterministic",
    confidence: float = 1.0,
) -> dict[str, object]:
    return {
        "confidence": confidence,
        "created_by_session_id": session_id,
        "created_at": "2026-07-25T12:00:00.000000",
        "layer": layer,
        "rule_id": rule_id,
        "created_by": "worker_deterministic_v1",
        "fallback_reason": "",
    }


def _normalize(value: object) -> object:
    if isinstance(value, Timestamp):
        rendered = datetime.fromtimestamp(
            value.micros / 1_000_000,
            tz=UTC,
        ).isoformat(timespec="microseconds")
        return rendered.replace("+00:00", "Z")
    return value


def _snapshot_attrs(attrs: dict[str, object]) -> dict[str, object]:
    return {
        **attrs,
        "created_at": "2026-07-25T12:00:00.000000Z",
    }


def _edges(database: Any) -> tuple[tuple[object, ...], ...]:
    result = database.execute(
        f"MATCH (source:Entity)-[r:{PHYSICAL_RELATIONSHIP}]->(target:Entity) "
        "RETURN source.id, target.id, r.confidence, "
        "r.created_by_session_id, r.created_at, r.layer, r.rule_id, "
        "r.created_by, r.fallback_reason"
    )
    return tuple(
        sorted(tuple(_normalize(value) for value in row) for row in result.rows)
    )


def _nodes(database: Any) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            database.execute("MATCH (n:Entity) RETURN n.id, n.source_session_id").rows
        )
    )


def _state(database: Any) -> tuple[object, object]:
    return _nodes(database), _edges(database)


def _topology(database: Any) -> tuple[object, object]:
    nodes = tuple(sorted(database.execute("MATCH (n:Entity) RETURN n.id").rows))
    edges = tuple(
        sorted(
            database.execute(
                f"MATCH (a:Entity)-[r:{PHYSICAL_RELATIONSHIP}]->(b:Entity) "
                "RETURN a.id, b.id"
            ).rows
        )
    )
    return nodes, edges


def _edge_identities(database: Any) -> Counter[tuple[object, object, object]]:
    return Counter((row[0], row[1], row[6]) for row in _edges(database))


async def _seed(lineage_harness: _Harness, *, duplicate_lineage: bool = False) -> None:
    scope = await lineage_harness.provider.begin(BOARD_ID)
    for node_id in (
        SPEC_ID,
        IDEATION_ID,
        REFINEMENT_ID,
        BOARD_ROOT_ID,
        LEGACY_ID,
    ):
        scope.create_node(
            "Entity",
            node_id,
            {},
            source_session_id=f"seed-node:{node_id}",
        )

    old_attrs = _edge_attrs(IDEATION_RULE, "session-old", confidence=0.91)
    assert scope.create_edge(
        LOGICAL_RELATIONSHIP,
        "Entity",
        "Entity",
        SPEC_ID,
        IDEATION_ID,
        old_attrs,
    )
    if duplicate_lineage:
        assert scope.create_edge(
            LOGICAL_RELATIONSHIP,
            "Entity",
            "Entity",
            SPEC_ID,
            IDEATION_ID,
            _edge_attrs(IDEATION_RULE, "session-duplicate", confidence=0.37),
        )

    # Same endpoints as the old deterministic parent, but outside the bounded
    # rule family.  An endpoint-only delete would incorrectly erase it.
    assert scope.create_edge(
        LOGICAL_RELATIONSHIP,
        "Entity",
        "Entity",
        SPEC_ID,
        IDEATION_ID,
        _edge_attrs(PARALLEL_NON_LINEAGE_RULE, "session-parallel"),
    )
    assert scope.create_edge(
        LOGICAL_RELATIONSHIP,
        "Entity",
        "Entity",
        SPEC_ID,
        SPEC_ID,
        _edge_attrs(SELF_LOOP_RULE, "session-self-loop"),
    )
    assert scope.create_edge(
        LOGICAL_RELATIONSHIP,
        "Entity",
        "Entity",
        SPEC_ID,
        BOARD_ROOT_ID,
        _edge_attrs(BOARD_RULE, "session-board"),
    )
    assert scope.create_edge(
        LOGICAL_RELATIONSHIP,
        "Entity",
        "Entity",
        SPEC_ID,
        LEGACY_ID,
        _edge_attrs(
            LEGACY_RULE,
            "session-legacy",
            layer="legacy",
            confidence=0.5,
        ),
    )
    await scope.commit()


@pytest.mark.parametrize(
    "missing_property",
    tuple(name for name, _property_type in LINEAGE_RELATIONSHIP_PROPERTIES),
)
@pytest.mark.asyncio
async def test_lineage_requires_the_complete_standard_relationship_schema(
    tmp_path: Path,
    missing_property: str,
) -> None:
    database = okto_grafx.connect(tmp_path / f"missing-{missing_property}")
    included = tuple(
        (name, property_type)
        for name, property_type in LINEAGE_RELATIONSHIP_PROPERTIES
        if name != missing_property
    )
    properties = ", ".join(
        f"{name} {property_type}" for name, property_type in included
    )
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Entity("
            "id STRING, source_session_id STRING, PRIMARY KEY(id))"
        )
        schema.execute(
            f"CREATE REL TABLE {PHYSICAL_RELATIONSHIP}("
            f"FROM Entity TO Entity, {properties})"
        )

    fence = _Fence()
    provider = CommunityGrafxGraphTransaction(
        database_resolver=lambda _board_id: database,
        revalidate_fence=fence,
        node_types=("Entity",),
        relationship_pairs=((LOGICAL_RELATIONSHIP, "Entity", "Entity"),),
        relationship_table_resolver=_relationship_table,
    )
    try:
        seed_scope = await provider.begin(BOARD_ID)
        for node_id in (SPEC_ID, REFINEMENT_ID):
            seed_scope.create_node(
                "Entity",
                node_id,
                {},
                source_session_id=f"seed-node:{node_id}",
            )
        await seed_scope.commit()
        before = _topology(database)

        scope = await provider.begin(BOARD_ID)
        with pytest.raises(GraphCapabilityUnavailable):
            scope.reconcile_spec_lineage_parent(
                SPEC_ID,
                REFINEMENT_ID,
                _edge_attrs(REFINEMENT_RULE, "session-invalid-schema"),
            )
        await scope.commit()
        assert _topology(database) == before
    finally:
        database.close()


@pytest.mark.parametrize("operation", ("reconcile", "clear", "compensate"))
@pytest.mark.asyncio
async def test_each_lineage_primitive_freezes_physical_schema_once(
    lineage_harness: _Harness,
    operation: str,
) -> None:
    await _seed(lineage_harness)
    receipt = None
    if operation == "compensate":
        apply_scope = await lineage_harness.provider.begin(BOARD_ID)
        receipt = apply_scope.reconcile_spec_lineage_parent(
            SPEC_ID,
            REFINEMENT_ID,
            _edge_attrs(REFINEMENT_RULE, "session-before-compensation"),
        )
        await apply_scope.commit()

    before = _state(lineage_harness.database)
    resolver_calls: list[tuple[str, str, str]] = []

    def one_shot_relationship_table(
        edge_type: str,
        from_type: str,
        to_type: str,
    ) -> str:
        resolver_calls.append((edge_type, from_type, to_type))
        if len(resolver_calls) != 1:
            raise AssertionError("lineage operation re-resolved its physical schema")
        return PHYSICAL_RELATIONSHIP

    provider = CommunityGrafxGraphTransaction(
        database_resolver=lambda _board_id: lineage_harness.database,
        revalidate_fence=lineage_harness.fence,
        node_types=("Entity",),
        relationship_pairs=((LOGICAL_RELATIONSHIP, "Entity", "Entity"),),
        relationship_table_resolver=one_shot_relationship_table,
    )
    scope = await provider.begin(BOARD_ID)
    if operation == "reconcile":
        scope.reconcile_spec_lineage_parent(
            SPEC_ID,
            REFINEMENT_ID,
            _edge_attrs(REFINEMENT_RULE, "session-frozen-schema"),
        )
    elif operation == "clear":
        scope.clear_spec_lineage_parent(SPEC_ID)
    else:
        assert receipt is not None
        scope.compensate_spec_lineage_parent(receipt)
    await scope.rollback()

    assert resolver_calls == [(LOGICAL_RELATIONSHIP, "Entity", "Entity")]
    assert _state(lineage_harness.database) == before


@pytest.mark.parametrize(
    ("source_id", "target_id", "rule_id", "expected_code"),
    (
        (
            SPEC_ID,
            REFINEMENT_ID,
            BOARD_RULE,
            "spec_lineage_rule_out_of_scope",
        ),
        (
            "missing-source",
            REFINEMENT_ID,
            REFINEMENT_RULE,
            "spec_lineage_endpoint_not_found",
        ),
        (
            SPEC_ID,
            "missing-target",
            REFINEMENT_RULE,
            "spec_lineage_endpoint_not_found",
        ),
    ),
)
@pytest.mark.asyncio
async def test_reconcile_preflight_failures_leave_no_staged_mutation(
    lineage_harness: _Harness,
    source_id: str,
    target_id: str,
    rule_id: str,
    expected_code: str,
) -> None:
    await _seed(lineage_harness)
    before = _state(lineage_harness.database)
    scope = await lineage_harness.provider.begin(BOARD_ID)

    with pytest.raises(SpecLineageReconciliationError) as excinfo:
        scope.reconcile_spec_lineage_parent(
            source_id,
            target_id,
            _edge_attrs(rule_id, "session-invalid"),
        )

    assert excinfo.value.code == expected_code
    assert excinfo.value.receipt is None
    # Callers are allowed to catch a bounded preflight error.  A later commit
    # must still be a no-op, whether the provider kept or poisoned the scope.
    await scope.commit()
    assert _state(lineage_harness.database) == before


@pytest.mark.asyncio
async def test_reconcile_is_create_first_exact_and_returns_complete_receipt(
    lineage_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(lineage_harness)
    scope = await lineage_harness.provider.begin(BOARD_ID)
    raw_transaction = scope._transaction
    original_execute = Transaction.execute
    mutations: list[str] = []

    def record_mutations(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if transaction is raw_transaction:
            normalized = statement.upper()
            if "CREATE (" in normalized and f":{PHYSICAL_RELATIONSHIP}" in statement:
                mutations.append("create")
            if "DELETE R" in normalized and f":{PHYSICAL_RELATIONSHIP}" in statement:
                mutations.append("delete")
        return original_execute(transaction, statement, params)

    monkeypatch.setattr(Transaction, "execute", record_mutations)
    replacement_attrs = _edge_attrs(
        REFINEMENT_RULE,
        "session-new",
        confidence=0.83,
    )
    receipt = scope.reconcile_spec_lineage_parent(
        SPEC_ID,
        REFINEMENT_ID,
        replacement_attrs,
    )

    assert mutations[:2] == ["create", "delete"]
    assert receipt.source_id == SPEC_ID
    assert receipt.target_id == REFINEMENT_ID
    assert receipt.target_rule_id == REFINEMENT_RULE
    assert receipt.target_attrs == replacement_attrs
    assert receipt.new_edge_created is True
    assert receipt.ambiguous_legacy_edges == 1
    assert receipt.removed_edges == (
        SpecLineageEdgeSnapshot(
            source_id=SPEC_ID,
            target_id=IDEATION_ID,
            rule_id=IDEATION_RULE,
            attrs=_snapshot_attrs(
                _edge_attrs(IDEATION_RULE, "session-old", confidence=0.91)
            ),
        ),
    )
    await scope.commit()

    identities = _edge_identities(lineage_harness.database)
    assert identities[(SPEC_ID, IDEATION_ID, IDEATION_RULE)] == 0
    assert identities[(SPEC_ID, REFINEMENT_ID, REFINEMENT_RULE)] == 1
    assert identities[(SPEC_ID, IDEATION_ID, PARALLEL_NON_LINEAGE_RULE)] == 1
    assert identities[(SPEC_ID, SPEC_ID, SELF_LOOP_RULE)] == 1
    assert identities[(SPEC_ID, BOARD_ROOT_ID, BOARD_RULE)] == 1
    assert identities[(SPEC_ID, LEGACY_ID, LEGACY_RULE)] == 1


@pytest.mark.asyncio
async def test_reconcile_retry_is_idempotent_and_retry_receipt_owns_no_edge(
    lineage_harness: _Harness,
) -> None:
    await _seed(lineage_harness)
    first_scope = await lineage_harness.provider.begin(BOARD_ID)
    first_attrs = _edge_attrs(REFINEMENT_RULE, "session-first")
    first = first_scope.reconcile_spec_lineage_parent(
        SPEC_ID,
        REFINEMENT_ID,
        first_attrs,
    )
    await first_scope.commit()
    assert first.new_edge_created is True

    converged = _state(lineage_harness.database)
    retry_scope = await lineage_harness.provider.begin(BOARD_ID)
    retry_attrs = _edge_attrs(REFINEMENT_RULE, "session-retry", confidence=0.17)
    retry = retry_scope.reconcile_spec_lineage_parent(
        SPEC_ID,
        REFINEMENT_ID,
        retry_attrs,
    )

    assert retry.target_attrs == retry_attrs
    assert retry.new_edge_created is False
    assert retry.removed_edges == ()
    retry_scope.compensate_spec_lineage_parent(retry)
    await retry_scope.commit()

    assert _state(lineage_harness.database) == converged
    matching = [
        row
        for row in _edges(lineage_harness.database)
        if row[0:2] == (SPEC_ID, REFINEMENT_ID) and row[6] == REFINEMENT_RULE
    ]
    assert len(matching) == 1
    assert matching[0][3] == "session-first"


@pytest.mark.asyncio
async def test_clear_is_complete_idempotent_and_compensable(
    lineage_harness: _Harness,
) -> None:
    await _seed(lineage_harness)
    clear_scope = await lineage_harness.provider.begin(BOARD_ID)
    receipt = clear_scope.clear_spec_lineage_parent(SPEC_ID)

    assert receipt.source_id == SPEC_ID
    assert receipt.target_id is None
    assert receipt.target_rule_id is None
    assert receipt.target_attrs == {}
    assert receipt.new_edge_created is False
    assert receipt.ambiguous_legacy_edges == 1
    assert receipt.removed_edges == (
        SpecLineageEdgeSnapshot(
            source_id=SPEC_ID,
            target_id=IDEATION_ID,
            rule_id=IDEATION_RULE,
            attrs=_snapshot_attrs(
                _edge_attrs(IDEATION_RULE, "session-old", confidence=0.91)
            ),
        ),
    )
    await clear_scope.commit()

    identities = _edge_identities(lineage_harness.database)
    assert identities[(SPEC_ID, IDEATION_ID, IDEATION_RULE)] == 0
    assert identities[(SPEC_ID, IDEATION_ID, PARALLEL_NON_LINEAGE_RULE)] == 1
    assert identities[(SPEC_ID, SPEC_ID, SELF_LOOP_RULE)] == 1
    assert identities[(SPEC_ID, BOARD_ROOT_ID, BOARD_RULE)] == 1
    assert identities[(SPEC_ID, LEGACY_ID, LEGACY_RULE)] == 1

    retry_scope = await lineage_harness.provider.begin(BOARD_ID)
    retry = retry_scope.clear_spec_lineage_parent(SPEC_ID)
    assert retry.removed_edges == ()
    assert retry.ambiguous_legacy_edges == 1
    retry_scope.compensate_spec_lineage_parent(receipt)
    await retry_scope.commit()

    # Compensation itself is replay-safe and does not duplicate the restored
    # deterministic identity.
    replay_scope = await lineage_harness.provider.begin(BOARD_ID)
    replay_scope.compensate_spec_lineage_parent(receipt)
    await replay_scope.commit()
    identities = _edge_identities(lineage_harness.database)
    assert identities[(SPEC_ID, IDEATION_ID, IDEATION_RULE)] == 1
    assert identities[(SPEC_ID, IDEATION_ID, PARALLEL_NON_LINEAGE_RULE)] == 1
    assert identities[(SPEC_ID, SPEC_ID, SELF_LOOP_RULE)] == 1
    assert identities[(SPEC_ID, LEGACY_ID, LEGACY_RULE)] == 1


@pytest.mark.asyncio
async def test_compensation_restores_first_and_removes_only_owned_replacement(
    lineage_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(lineage_harness)
    apply_scope = await lineage_harness.provider.begin(BOARD_ID)
    original_receipt = apply_scope.reconcile_spec_lineage_parent(
        SPEC_ID,
        REFINEMENT_ID,
        _edge_attrs(REFINEMENT_RULE, "session-created"),
    )
    await apply_scope.commit()

    # A retry did not create the replacement, so compensating its receipt must
    # not delete the already-existing edge.
    retry_scope = await lineage_harness.provider.begin(BOARD_ID)
    retry_receipt = retry_scope.reconcile_spec_lineage_parent(
        SPEC_ID,
        REFINEMENT_ID,
        _edge_attrs(REFINEMENT_RULE, "session-retry"),
    )
    assert retry_receipt.new_edge_created is False
    retry_scope.compensate_spec_lineage_parent(retry_receipt)
    await retry_scope.commit()
    assert (
        _edge_identities(lineage_harness.database)[
            (SPEC_ID, REFINEMENT_ID, REFINEMENT_RULE)
        ]
        == 1
    )

    compensate_scope = await lineage_harness.provider.begin(BOARD_ID)
    raw_transaction = compensate_scope._transaction
    original_execute = Transaction.execute
    mutations: list[str] = []

    def record_mutations(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if transaction is raw_transaction:
            normalized = statement.upper()
            if "CREATE (" in normalized and f":{PHYSICAL_RELATIONSHIP}" in statement:
                mutations.append("restore")
            if "DELETE R" in normalized and f":{PHYSICAL_RELATIONSHIP}" in statement:
                mutations.append("remove-replacement")
        return original_execute(transaction, statement, params)

    monkeypatch.setattr(Transaction, "execute", record_mutations)
    compensate_scope.compensate_spec_lineage_parent(original_receipt)
    assert mutations[:2] == ["restore", "remove-replacement"]
    await compensate_scope.commit()

    identities = _edge_identities(lineage_harness.database)
    assert identities[(SPEC_ID, IDEATION_ID, IDEATION_RULE)] == 1
    assert identities[(SPEC_ID, REFINEMENT_ID, REFINEMENT_RULE)] == 0
    assert identities[(SPEC_ID, IDEATION_ID, PARALLEL_NON_LINEAGE_RULE)] == 1
    assert identities[(SPEC_ID, SPEC_ID, SELF_LOOP_RULE)] == 1


@pytest.mark.parametrize("operation", ("reconcile", "clear"))
@pytest.mark.asyncio
async def test_parallel_lineage_identity_fails_closed_without_mutation(
    lineage_harness: _Harness,
    operation: str,
) -> None:
    await _seed(lineage_harness, duplicate_lineage=True)
    before = _state(lineage_harness.database)
    assert (
        _edge_identities(lineage_harness.database)[
            (SPEC_ID, IDEATION_ID, IDEATION_RULE)
        ]
        == 2
    )
    scope = await lineage_harness.provider.begin(BOARD_ID)

    with pytest.raises(SpecLineageReconciliationError) as excinfo:
        if operation == "reconcile":
            scope.reconcile_spec_lineage_parent(
                SPEC_ID,
                REFINEMENT_ID,
                _edge_attrs(REFINEMENT_RULE, "session-refused"),
            )
        else:
            scope.clear_spec_lineage_parent(SPEC_ID)

    assert excinfo.value.code == "spec_lineage_edge_metadata_inconsistent"
    assert excinfo.value.receipt is None
    await scope.commit()
    assert _state(lineage_harness.database) == before


@pytest.mark.parametrize("operation", ("reconcile", "clear"))
@pytest.mark.asyncio
async def test_lineage_mutations_obey_real_transaction_rollback(
    lineage_harness: _Harness,
    operation: str,
) -> None:
    await _seed(lineage_harness)
    before = _state(lineage_harness.database)
    scope = await lineage_harness.provider.begin(BOARD_ID)

    if operation == "reconcile":
        scope.reconcile_spec_lineage_parent(
            SPEC_ID,
            REFINEMENT_ID,
            _edge_attrs(REFINEMENT_RULE, "session-rollback"),
        )
    else:
        scope.clear_spec_lineage_parent(SPEC_ID)
    await scope.rollback()

    assert _state(lineage_harness.database) == before


@pytest.mark.parametrize("operation", ("reconcile", "clear"))
@pytest.mark.asyncio
async def test_lineage_fence_precedes_every_engine_access(
    lineage_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    await _seed(lineage_harness)
    before = _state(lineage_harness.database)
    scope = await lineage_harness.provider.begin(BOARD_ID)
    raw_transaction = scope._transaction
    original_execute = Transaction.execute
    engine_calls = 0

    def record_engine_access(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal engine_calls
        if transaction is raw_transaction:
            engine_calls += 1
        return original_execute(transaction, statement, params)

    monkeypatch.setattr(Transaction, "execute", record_engine_access)
    lineage_harness.fence.trace.clear()
    lineage_harness.fence.allowed = False

    with pytest.raises(GraphLockContention):
        if operation == "reconcile":
            scope.reconcile_spec_lineage_parent(
                SPEC_ID,
                REFINEMENT_ID,
                _edge_attrs(REFINEMENT_RULE, "session-fenced"),
            )
        else:
            scope.clear_spec_lineage_parent(SPEC_ID)

    assert lineage_harness.fence.trace == ["fence"]
    assert engine_calls == 0
    await scope.rollback()
    assert _state(lineage_harness.database) == before


@pytest.mark.asyncio
async def test_apply_then_raise_cannot_be_committed_after_error_is_caught(
    lineage_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(lineage_harness)
    before = _state(lineage_harness.database)
    scope = await lineage_harness.provider.begin(BOARD_ID)
    raw_transaction = scope._transaction
    original_execute = Transaction.execute
    injected = False

    def apply_then_raise(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal injected
        result = original_execute(transaction, statement, params)
        if (
            transaction is raw_transaction
            and not injected
            and "CREATE (" in statement.upper()
            and f":{PHYSICAL_RELATIONSHIP}" in statement
        ):
            injected = True
            raise _InjectedProcessSignal("injected signal after staging replacement")
        return result

    monkeypatch.setattr(Transaction, "execute", apply_then_raise)
    with pytest.raises(_InjectedProcessSignal):
        scope.reconcile_spec_lineage_parent(
            SPEC_ID,
            REFINEMENT_ID,
            _edge_attrs(REFINEMENT_RULE, "session-apply-then-raise"),
        )

    assert injected is True
    # Core catches bounded lineage errors.  Even if it later commits the scope,
    # a replacement staged before the exception cannot leak through.
    await scope.commit()
    assert _state(lineage_harness.database) == before


@pytest.mark.parametrize("operation", ("clear", "compensate"))
@pytest.mark.asyncio
async def test_process_signal_after_mutation_poisons_clear_and_compensation(
    lineage_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    await _seed(lineage_harness)
    receipt = None
    if operation == "compensate":
        apply_scope = await lineage_harness.provider.begin(BOARD_ID)
        receipt = apply_scope.reconcile_spec_lineage_parent(
            SPEC_ID,
            REFINEMENT_ID,
            _edge_attrs(REFINEMENT_RULE, "session-before-signal"),
        )
        await apply_scope.commit()

    before = _state(lineage_harness.database)
    scope = await lineage_harness.provider.begin(BOARD_ID)
    raw_transaction = scope._transaction
    original_execute = Transaction.execute
    injected = False

    def apply_then_signal(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal injected
        result = original_execute(transaction, statement, params)
        normalized = statement.upper()
        is_target_mutation = (operation == "clear" and "DELETE R" in normalized) or (
            operation == "compensate"
            and "CREATE (" in normalized
            and f":{PHYSICAL_RELATIONSHIP}" in statement
        )
        if transaction is raw_transaction and not injected and is_target_mutation:
            injected = True
            raise _InjectedProcessSignal(
                f"injected signal after staged lineage {operation}"
            )
        return result

    monkeypatch.setattr(Transaction, "execute", apply_then_signal)
    with pytest.raises(_InjectedProcessSignal):
        if operation == "clear":
            scope.clear_spec_lineage_parent(SPEC_ID)
        else:
            assert receipt is not None
            scope.compensate_spec_lineage_parent(receipt)

    assert injected is True
    await scope.commit()
    assert _state(lineage_harness.database) == before


@pytest.mark.asyncio
async def test_delete_confirmation_failure_cannot_commit_partial_relink(
    lineage_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed(lineage_harness)
    before = _state(lineage_harness.database)
    scope = await lineage_harness.provider.begin(BOARD_ID)
    raw_transaction = scope._transaction
    original_execute = Transaction.execute
    delete_suppressed = False

    def silently_suppress_old_parent_delete(
        transaction: Any,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        nonlocal delete_suppressed
        effective_params = params or {}
        if (
            transaction is raw_transaction
            and not delete_suppressed
            and "DELETE R" in statement.upper()
            and effective_params.get("rule_id") == IDEATION_RULE
        ):
            delete_suppressed = True
            return QueryResult()
        return original_execute(transaction, statement, params)

    monkeypatch.setattr(
        Transaction,
        "execute",
        silently_suppress_old_parent_delete,
    )
    with pytest.raises(SpecLineageReconciliationError) as excinfo:
        scope.reconcile_spec_lineage_parent(
            SPEC_ID,
            REFINEMENT_ID,
            _edge_attrs(REFINEMENT_RULE, "session-unconfirmed-delete"),
        )

    assert delete_suppressed is True
    assert excinfo.value.code == "spec_lineage_edge_delete_unconfirmed"
    assert excinfo.value.receipt is not None
    assert excinfo.value.receipt.new_edge_created is True
    await scope.commit()
    assert _state(lineage_harness.database) == before
