"""Real Grafx conformance at the Core transaction-orchestrator boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import okto_grafx
import pytest
from okto_grafx import Timestamp
from okto_pulse.core.kg.interfaces.graph_transaction import (
    SpecLineageEdgeSnapshot,
)
from okto_pulse.core.kg.transaction import TransactionOrchestrator

from okto_pulse.community.adapters.grafx_graph_transaction import (
    CommunityGrafxGraphTransaction,
)

BOARD_ID = "grafx-orchestrator-board"
SESSION_ID = "projection-session"
FOREIGN_SESSION_ID = "foreign-session"

SPEC_ID = "spec-source"
OLD_PARENT_ID = "ideation-parent"
NEW_PARENT_ID = "refinement-parent"
OTHER_SOURCE_ID = "other-source"

IDEATION_RULE = "belongs_to/spec_to_ideation@1.0"
REFINEMENT_RULE = "belongs_to/spec_to_refinement@1.0"
REFERENCE_RULE = "belongs_to/spec_reference@1.0"

LOGICAL_RELATIONSHIP = "belongs_to"
PHYSICAL_RELATIONSHIP = "belongs_to__entity__entity"
CREATED_AT = "2026-08-26T12:00:00.000000Z"


@dataclass
class _Fence:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, board_id: str, phase: str) -> None:
        assert board_id == BOARD_ID
        self.calls.append((board_id, phase))


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
def harness(tmp_path: Path) -> Any:
    database = okto_grafx.connect(tmp_path / "grafx-orchestrator")
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Entity("
            "id STRING, source_session_id STRING, PRIMARY KEY(id))"
        )
        schema.execute(
            f"CREATE REL TABLE {PHYSICAL_RELATIONSHIP}("
            "FROM Entity TO Entity, confidence DOUBLE, "
            "created_by_session_id STRING, created_at TIMESTAMP, "
            "layer STRING, rule_id STRING, created_by STRING, "
            "fallback_reason STRING)"
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


def _attrs(
    rule_id: str,
    session_id: str,
    *,
    confidence: float,
    created_by: str,
) -> dict[str, object]:
    return {
        "confidence": confidence,
        "created_by_session_id": session_id,
        "created_at": CREATED_AT,
        "layer": "deterministic",
        "rule_id": rule_id,
        "created_by": created_by,
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


def _edge_rows(database: Any) -> tuple[tuple[object, ...], ...]:
    rows = database.execute(
        f"MATCH (source:Entity)-[r:{PHYSICAL_RELATIONSHIP}]->(target:Entity) "
        "RETURN source.id, target.id, r.confidence, "
        "r.created_by_session_id, r.created_at, r.layer, r.rule_id, "
        "r.created_by, r.fallback_reason"
    ).rows
    return tuple(sorted(tuple(_normalize(value) for value in row) for row in rows))


def _expected_row(
    source_id: str,
    target_id: str,
    attrs: dict[str, object],
) -> tuple[object, ...]:
    return (
        source_id,
        target_id,
        attrs["confidence"],
        attrs["created_by_session_id"],
        attrs["created_at"],
        attrs["layer"],
        attrs["rule_id"],
        attrs["created_by"],
        attrs["fallback_reason"],
    )


async def _seed_nodes(provider: CommunityGrafxGraphTransaction) -> None:
    async with await provider.begin(BOARD_ID) as scope:
        for node_id in (
            SPEC_ID,
            OLD_PARENT_ID,
            NEW_PARENT_ID,
            OTHER_SOURCE_ID,
        ):
            scope.create_node(
                "Entity",
                node_id,
                {},
                source_session_id="seed-nodes",
            )


async def _create_edge(
    provider: CommunityGrafxGraphTransaction,
    source_id: str,
    target_id: str,
    attrs: dict[str, object],
) -> None:
    async with await provider.begin(BOARD_ID) as scope:
        assert scope.create_edge(
            LOGICAL_RELATIONSHIP,
            "Entity",
            "Entity",
            source_id,
            target_id,
            attrs,
        )


@pytest.mark.asyncio
async def test_session_cleanup_preserves_only_the_exact_lineage_snapshot(
    harness: _Harness,
) -> None:
    """G3: a same-identity owned duplicate is not part of the before-image."""

    await _seed_nodes(harness.provider)
    preserved = _attrs(
        IDEATION_RULE,
        SESSION_ID,
        confidence=0.61,
        created_by="preserved-before-image",
    )
    same_identity_duplicate = _attrs(
        IDEATION_RULE,
        SESSION_ID,
        confidence=0.62,
        created_by="not-in-before-image",
    )
    same_rule_other_target = _attrs(
        IDEATION_RULE,
        SESSION_ID,
        confidence=0.63,
        created_by="other-target",
    )
    same_endpoints_other_rule = _attrs(
        REFINEMENT_RULE,
        SESSION_ID,
        confidence=0.64,
        created_by="other-rule",
    )
    generic_owned = _attrs(
        REFERENCE_RULE,
        SESSION_ID,
        confidence=0.65,
        created_by="generic-owned",
    )
    foreign = _attrs(
        REFERENCE_RULE,
        FOREIGN_SESSION_ID,
        confidence=0.66,
        created_by="foreign",
    )

    for source_id, target_id, attrs in (
        (SPEC_ID, OLD_PARENT_ID, preserved),
        (SPEC_ID, OLD_PARENT_ID, same_identity_duplicate),
        (SPEC_ID, NEW_PARENT_ID, same_rule_other_target),
        (SPEC_ID, OLD_PARENT_ID, same_endpoints_other_rule),
        (SPEC_ID, NEW_PARENT_ID, generic_owned),
        (OTHER_SOURCE_ID, NEW_PARENT_ID, foreign),
    ):
        await _create_edge(harness.provider, source_id, target_id, attrs)

    async with await harness.provider.begin(BOARD_ID) as scope:
        scope.delete_edges_by_session_preserving_spec_lineage(
            SESSION_ID,
            (
                SpecLineageEdgeSnapshot(
                    source_id=SPEC_ID,
                    target_id=OLD_PARENT_ID,
                    rule_id=IDEATION_RULE,
                    attrs=dict(preserved),
                ),
            ),
        )

    assert _edge_rows(harness.database) == tuple(
        sorted(
            (
                _expected_row(SPEC_ID, OLD_PARENT_ID, preserved),
                _expected_row(OTHER_SOURCE_ID, NEW_PARENT_ID, foreign),
            )
        )
    )


@pytest.mark.asyncio
async def test_orchestrator_reopens_scope_and_preserves_restored_parent(
    harness: _Harness,
) -> None:
    """G4-lineage: replay the Core ``_compensate_graph_writes`` sequence."""

    await _seed_nodes(harness.provider)
    old_parent = _attrs(
        IDEATION_RULE,
        SESSION_ID,
        confidence=0.71,
        created_by="historical-worker",
    )
    await _create_edge(
        harness.provider,
        SPEC_ID,
        OLD_PARENT_ID,
        old_parent,
    )

    apply_scope = await harness.provider.begin(BOARD_ID)
    async with apply_scope:
        orchestrator = TransactionOrchestrator(
            graph_scope=apply_scope,
            session_id=SESSION_ID,
            board_id=BOARD_ID,
        )
        orchestrator.create_edge(
            LOGICAL_RELATIONSHIP,
            SPEC_ID,
            NEW_PARENT_ID,
            attrs={
                "confidence": 0.81,
                "created_at": CREATED_AT,
                "layer": "deterministic",
                "rule_id": REFINEMENT_RULE,
                "created_by": "historical-worker",
                "fallback_reason": "",
            },
            from_type="Entity",
            to_type="Entity",
        )
        orchestrator.create_edge(
            LOGICAL_RELATIONSHIP,
            OTHER_SOURCE_ID,
            NEW_PARENT_ID,
            attrs={
                "confidence": 0.82,
                "created_at": CREATED_AT,
                "layer": "cognitive",
                "rule_id": REFERENCE_RULE,
                "created_by": "cognitive-worker",
                "fallback_reason": "",
            },
            from_type="Entity",
            to_type="Entity",
        )
        records = list(orchestrator.records)

    lineage_records = [
        record for record in records if record.lineage_receipt is not None
    ]
    assert len(lineage_records) == 1
    receipt = lineage_records[0].lineage_receipt
    assert receipt is not None
    assert receipt.new_edge_created is True
    assert receipt.source_id == SPEC_ID
    assert receipt.target_id == NEW_PARENT_ID
    assert tuple(
        (edge.source_id, edge.target_id, edge.rule_id) for edge in receipt.removed_edges
    ) == ((SPEC_ID, OLD_PARENT_ID, IDEATION_RULE),)
    assert receipt.removed_edges[0].attrs == old_parent
    assert _edge_rows(harness.database) != (
        _expected_row(SPEC_ID, OLD_PARENT_ID, old_parent),
    )

    # This is deliberately the same reopen-and-replay sequence used by
    # core.kg.primitives._compensate_graph_writes.
    compensation_scope = await harness.provider.begin(BOARD_ID)
    assert compensation_scope is not apply_scope
    async with compensation_scope:
        replay = TransactionOrchestrator(
            graph_scope=compensation_scope,
            session_id=SESSION_ID,
            board_id=BOARD_ID,
        )
        replay.records = records
        await replay.compensate()

    # The restored parent is owned by the same failed session.  The generic
    # cleanup did run (the reference edge is gone), but it must preserve the
    # exact lineage before-image supplied by the receipt.
    assert _edge_rows(harness.database) == (
        _expected_row(SPEC_ID, OLD_PARENT_ID, old_parent),
    )


@pytest.mark.asyncio
async def test_orchestrator_compensates_two_lineage_swaps_before_cleanup(
    harness: _Harness,
) -> None:
    """Accumulated candidates may include an intermediate edge now absent."""

    await _seed_nodes(harness.provider)
    old_parent = _attrs(
        IDEATION_RULE,
        SESSION_ID,
        confidence=0.91,
        created_by="historical-worker",
    )
    await _create_edge(harness.provider, SPEC_ID, OLD_PARENT_ID, old_parent)

    apply_scope = await harness.provider.begin(BOARD_ID)
    async with apply_scope:
        orchestrator = TransactionOrchestrator(
            graph_scope=apply_scope,
            session_id=SESSION_ID,
            board_id=BOARD_ID,
        )
        orchestrator.create_edge(
            LOGICAL_RELATIONSHIP,
            SPEC_ID,
            NEW_PARENT_ID,
            attrs={
                "confidence": 0.92,
                "created_at": CREATED_AT,
                "layer": "deterministic",
                "rule_id": REFINEMENT_RULE,
                "created_by": "historical-worker",
                "fallback_reason": "",
            },
            from_type="Entity",
            to_type="Entity",
        )
        orchestrator.create_edge(
            LOGICAL_RELATIONSHIP,
            SPEC_ID,
            OTHER_SOURCE_ID,
            attrs={
                "confidence": 0.93,
                "created_at": CREATED_AT,
                "layer": "deterministic",
                "rule_id": IDEATION_RULE,
                "created_by": "historical-worker",
                "fallback_reason": "",
            },
            from_type="Entity",
            to_type="Entity",
        )
        records = list(orchestrator.records)

    assert len([record for record in records if record.lineage_receipt]) == 2

    compensation_scope = await harness.provider.begin(BOARD_ID)
    async with compensation_scope:
        replay = TransactionOrchestrator(
            graph_scope=compensation_scope,
            session_id=SESSION_ID,
            board_id=BOARD_ID,
        )
        replay.records = records
        await replay.compensate()

    assert _edge_rows(harness.database) == (
        _expected_row(SPEC_ID, OLD_PARENT_ID, old_parent),
    )
