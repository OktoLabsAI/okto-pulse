from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.kuzu_graph_transaction import (
    CommunityKuzuGraphTransaction,
)
from okto_pulse.core.kg.interfaces.graph_transaction import (
    SpecLineageEdgeSnapshot,
    SpecLineageReconciliationError,
)


SPEC_ID = "spec-source"
IDEATION_ID = "ideation-parent"
REFINEMENT_ID = "refinement-parent"
BOARD_ROOT_ID = "board-root"

IDEATION_RULE = "belongs_to/spec_to_ideation@1.0"
REFINEMENT_RULE = "belongs_to/spec_to_refinement@1.0"
BOARD_RULE = "belongs_to/spec_to_board@1.0"


def _edge_attrs(rule_id: str, session_id: str) -> dict[str, object]:
    return {
        "confidence": 1.0,
        "created_by_session_id": session_id,
        "created_at": "2026-07-25T12:00:00.000000",
        "layer": "deterministic",
        "rule_id": rule_id,
        "created_by": "worker_deterministic_v1",
        "fallback_reason": "",
    }


async def _seed_scope(board_id: str):
    scope = await CommunityKuzuGraphTransaction().begin(board_id)
    for node_id in (SPEC_ID, IDEATION_ID, REFINEMENT_ID, BOARD_ROOT_ID):
        scope.create_node(
            "Entity",
            node_id,
            {},
            source_session_id="seed-nodes",
        )
    assert scope.create_edge(
        "belongs_to",
        "Entity",
        "Entity",
        SPEC_ID,
        IDEATION_ID,
        _edge_attrs(IDEATION_RULE, "session-old"),
    )
    assert scope.create_edge(
        "belongs_to",
        "Entity",
        "Entity",
        SPEC_ID,
        BOARD_ROOT_ID,
        _edge_attrs(BOARD_RULE, "session-board"),
    )
    return scope


def _outgoing_edges(board_id: str) -> list[tuple[str, str]]:
    with kg_runtime.open_board_connection(board_id) as (_db, conn):
        result = conn.execute(
            "MATCH (source:Entity {id: $source_id})"
            "-[r:belongs_to]->(target:Entity) "
            "RETURN target.id, r.rule_id ORDER BY target.id, r.rule_id",
            {"source_id": SPEC_ID},
        )
        try:
            rows: list[tuple[str, str]] = []
            while result.has_next():
                row = result.get_next()
                rows.append((str(row[0]), str(row[1] or "")))
            return rows
        finally:
            result.close()


@pytest.mark.asyncio
async def test_real_adapter_relink_retry_and_restore_first_compensation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"spec-lineage-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        scope = await _seed_scope(board_id)
        receipt = scope.reconcile_spec_lineage_parent(
            SPEC_ID,
            REFINEMENT_ID,
            _edge_attrs(REFINEMENT_RULE, "session-new"),
        )
        await scope.commit()

        assert receipt.new_edge_created is True
        assert len(receipt.removed_edges) == 1
        assert _outgoing_edges(board_id) == [
            (BOARD_ROOT_ID, BOARD_RULE),
            (REFINEMENT_ID, REFINEMENT_RULE),
        ]

        retry_scope = await CommunityKuzuGraphTransaction().begin(board_id)
        retry = retry_scope.reconcile_spec_lineage_parent(
            SPEC_ID,
            REFINEMENT_ID,
            _edge_attrs(REFINEMENT_RULE, "session-retry"),
        )
        assert retry.new_edge_created is False
        assert retry.removed_edges == ()
        retry_scope.compensate_spec_lineage_parent(receipt)
        await retry_scope.commit()

        assert _outgoing_edges(board_id) == [
            (BOARD_ROOT_ID, BOARD_RULE),
            (IDEATION_ID, IDEATION_RULE),
        ]
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_real_adapter_clear_retry_restore_and_legacy_preservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"spec-lineage-clear-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        scope = await _seed_scope(board_id)
        scope.create_node(
            "Entity",
            "legacy-parent",
            {},
            source_session_id="seed-legacy",
        )
        assert scope.create_edge(
            "belongs_to",
            "Entity",
            "Entity",
            SPEC_ID,
            "legacy-parent",
            {
                **_edge_attrs("legacy_pre_v2", "seed-legacy"),
                "layer": "legacy",
            },
        )

        receipt = scope.clear_spec_lineage_parent(SPEC_ID)
        await scope.commit()

        assert receipt.target_id is None
        assert receipt.new_edge_created is False
        assert len(receipt.removed_edges) == 1
        assert receipt.ambiguous_legacy_edges == 1
        assert _outgoing_edges(board_id) == [
            (BOARD_ROOT_ID, BOARD_RULE),
            ("legacy-parent", "legacy_pre_v2"),
        ]

        retry_scope = await CommunityKuzuGraphTransaction().begin(board_id)
        retry = retry_scope.clear_spec_lineage_parent(SPEC_ID)
        assert retry.removed_edges == ()
        assert retry.ambiguous_legacy_edges == 1
        retry_scope.compensate_spec_lineage_parent(receipt)
        await retry_scope.commit()

        assert _outgoing_edges(board_id) == [
            (BOARD_ROOT_ID, BOARD_RULE),
            (IDEATION_ID, IDEATION_RULE),
            ("legacy-parent", "legacy_pre_v2"),
        ]
    finally:
        kg_runtime.close_all_connections(board_id)


@pytest.mark.asyncio
async def test_real_adapter_partial_delete_and_restore_failure_carries_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_id = f"spec-lineage-partial-{uuid4().hex}"
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: tmp_path / "kg")
    kg_runtime.reset_bootstrap_cache_for_tests()

    try:
        kg_runtime.bootstrap_board_graph(board_id)
        scope = await _seed_scope(board_id)
        original_delete = scope._delete_spec_lineage_edge
        original_create = scope.create_edge
        delete_applied = False

        def _delete_then_fail(snapshot: SpecLineageEdgeSnapshot) -> None:
            nonlocal delete_applied
            original_delete(snapshot)
            delete_applied = True
            raise RuntimeError("injected delete failure after auto-commit")

        def _fail_restore(*args, **kwargs):
            if delete_applied and args[4] == IDEATION_ID:
                raise RuntimeError("injected restore failure")
            return original_create(*args, **kwargs)

        scope._delete_spec_lineage_edge = _delete_then_fail  # type: ignore[method-assign]
        scope.create_edge = _fail_restore  # type: ignore[method-assign]

        with pytest.raises(SpecLineageReconciliationError) as excinfo:
            scope.reconcile_spec_lineage_parent(
                SPEC_ID,
                REFINEMENT_ID,
                _edge_attrs(REFINEMENT_RULE, "session-new"),
            )

        assert excinfo.value.code == "spec_lineage_partial_cleanup_restore_failed"
        assert excinfo.value.receipt is not None
        await scope.rollback()
        assert _outgoing_edges(board_id) == [
            (BOARD_ROOT_ID, BOARD_RULE),
            (REFINEMENT_ID, REFINEMENT_RULE),
        ]

        recovery_scope = await CommunityKuzuGraphTransaction().begin(board_id)
        recovery_scope.compensate_spec_lineage_parent(excinfo.value.receipt)
        await recovery_scope.commit()

        assert _outgoing_edges(board_id) == [
            (BOARD_ROOT_ID, BOARD_RULE),
            (IDEATION_ID, IDEATION_RULE),
        ]
    finally:
        kg_runtime.close_all_connections(board_id)
