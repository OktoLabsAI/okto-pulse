from __future__ import annotations

import pytest

from okto_pulse.community.adapters.sqlalchemy_analytics_evidence import (
    _cognitive_snapshot_id,
    _decode_board_kg_cursor,
    _encode_board_kg_cursor,
)
from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItem


def _item(item_id: str, *, status: str = "pending") -> CognitiveConsolidationItem:
    return CognitiveConsolidationItem(
        item_id=item_id,
        board_id="board-1",
        kg_generation_id="generation-1",
        source_ref=f"spec:{item_id}",
        artifact_type="spec",
        status=status,
        recorded_at="2026-08-22T12:00:00Z",
    )


def test_board_kg_cursor_is_bound_to_the_exact_ledger_snapshot() -> None:
    first_snapshot = _cognitive_snapshot_id("generation-1", [_item("a"), _item("b")])
    cursor = _encode_board_kg_cursor(snapshot_id=first_snapshot, offset=1)

    assert _decode_board_kg_cursor(cursor, snapshot_id=first_snapshot) == 1
    assert first_snapshot == _cognitive_snapshot_id(
        "generation-1", [_item("b"), _item("a")]
    )

    changed_snapshot = _cognitive_snapshot_id(
        "generation-1", [_item("a"), _item("b"), _item("c")]
    )
    with pytest.raises(ValueError, match="board_kg_analytics_cursor_stale"):
        _decode_board_kg_cursor(cursor, snapshot_id=changed_snapshot)


def test_board_kg_cursor_rejects_generation_changes_and_malformed_offsets() -> None:
    snapshot = _cognitive_snapshot_id("generation-1", [_item("a")])
    next_generation = _cognitive_snapshot_id("generation-2", [_item("a")])
    cursor = _encode_board_kg_cursor(snapshot_id=snapshot, offset=1)

    with pytest.raises(ValueError, match="board_kg_analytics_cursor_stale"):
        _decode_board_kg_cursor(cursor, snapshot_id=next_generation)
    with pytest.raises(ValueError, match="board_kg_analytics_cursor_invalid"):
        _decode_board_kg_cursor("offset:1", snapshot_id=snapshot)
    with pytest.raises(ValueError, match="board_kg_analytics_cursor_invalid"):
        _decode_board_kg_cursor(
            _encode_board_kg_cursor(snapshot_id=snapshot, offset=-1),
            snapshot_id=snapshot,
        )
