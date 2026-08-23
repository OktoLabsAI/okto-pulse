from __future__ import annotations

from datetime import UTC, datetime

import pytest

from okto_pulse.community.adapters import sqlalchemy_analytics_evidence
from okto_pulse.community.adapters.sqlalchemy_analytics_evidence import (
    CommunitySqlAlchemyBoardKgAnalyticsEvidence,
    _cognitive_snapshot_id,
    _decode_board_kg_cursor,
    _encode_board_kg_cursor,
)
from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItem
from okto_pulse.core.ports.analytics_foundation import (
    AnalyticsFoundationQuery,
    AnalyticsUtcWindow,
)
from okto_pulse.core.ports.board_kg_analytics import BoardKgAnalyticsQuery


def _item(
    item_id: str,
    *,
    status: str = "pending",
    recorded_at: str = "2026-08-22T12:00:00Z",
) -> CognitiveConsolidationItem:
    return CognitiveConsolidationItem(
        item_id=item_id,
        board_id="board-1",
        kg_generation_id="generation-1",
        source_ref=f"spec:{item_id}",
        artifact_type="spec",
        status=status,
        recorded_at=recorded_at,
    )


def _snapshot(
    generation: str,
    items: list[CognitiveConsolidationItem],
    *,
    board_id: str = "board-1",
    cognitive_status: tuple[str, ...] = (),
    artifact_types: tuple[str, ...] = (),
    window_from: datetime = datetime(2026, 8, 1, tzinfo=UTC),
    window_to: datetime = datetime(2026, 9, 1, tzinfo=UTC),
) -> str:
    return _cognitive_snapshot_id(
        generation,
        items,
        board_id=board_id,
        cognitive_status=cognitive_status,
        artifact_types=artifact_types,
        window_from=window_from,
        window_to=window_to,
    )


def test_board_kg_cursor_is_bound_to_the_exact_ledger_snapshot() -> None:
    first_snapshot = _snapshot("generation-1", [_item("a"), _item("b")])
    cursor = _encode_board_kg_cursor(snapshot_id=first_snapshot, offset=1)

    assert _decode_board_kg_cursor(cursor, snapshot_id=first_snapshot) == 1
    assert first_snapshot == _snapshot(
        "generation-1", [_item("b"), _item("a")]
    )

    changed_snapshot = _snapshot(
        "generation-1", [_item("a"), _item("b"), _item("c")]
    )
    with pytest.raises(ValueError, match="board_kg_analytics_cursor_stale"):
        _decode_board_kg_cursor(cursor, snapshot_id=changed_snapshot)


def test_board_kg_cursor_rejects_generation_changes_and_malformed_offsets() -> None:
    snapshot = _snapshot("generation-1", [_item("a")])
    next_generation = _snapshot("generation-2", [_item("a")])
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


def test_board_kg_cursor_rejects_board_and_filter_changes() -> None:
    items = [_item("a"), _item("b")]
    snapshot = _snapshot(
        "generation-1",
        items,
        cognitive_status=("pending",),
        artifact_types=("spec",),
    )
    cursor = _encode_board_kg_cursor(snapshot_id=snapshot, offset=1)

    changed_scopes = (
        _snapshot(
            "generation-1",
            items,
            board_id="board-2",
            cognitive_status=("pending",),
            artifact_types=("spec",),
        ),
        _snapshot(
            "generation-1",
            items,
            cognitive_status=("failed",),
            artifact_types=("spec",),
        ),
        _snapshot(
            "generation-1",
            items,
            cognitive_status=("pending",),
            artifact_types=("card",),
        ),
        _snapshot(
            "generation-1",
            items,
            cognitive_status=("pending",),
            artifact_types=("spec",),
            window_from=datetime(2026, 8, 2, tzinfo=UTC),
        ),
    )
    for changed_scope in changed_scopes:
        with pytest.raises(ValueError, match="board_kg_analytics_cursor_stale"):
            _decode_board_kg_cursor(cursor, snapshot_id=changed_scope)


@pytest.mark.asyncio
async def test_board_kg_cognitive_items_use_the_canonical_half_open_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = [
        _item("before", recorded_at="2026-07-31T23:59:59Z"),
        _item("from", recorded_at="2026-08-01T00:00:00Z"),
        _item("inside", recorded_at="2026-08-21T23:59:59Z"),
        _item("to", recorded_at="2026-08-22T00:00:00Z"),
    ]

    class StubStore:
        def latest_generation(self, board_id: str) -> str:
            assert board_id == "board-1"
            return "generation-1"

        def list_items(
            self, board_id: str, generation: str
        ) -> list[CognitiveConsolidationItem]:
            assert (board_id, generation) == ("board-1", "generation-1")
            return items

    monkeypatch.setattr(
        sqlalchemy_analytics_evidence,
        "require_rebuild_audit_artifact_store",
        lambda: object(),
    )
    monkeypatch.setattr(
        sqlalchemy_analytics_evidence,
        "CognitiveConsolidationItemStore",
        lambda _artifact_store: StubStore(),
    )
    query = BoardKgAnalyticsQuery(
        foundation=AnalyticsFoundationQuery(
            board_id="board-1",
            actor_scope_ref="board:board-1",
            window=AnalyticsUtcWindow(
                datetime(2026, 8, 1, tzinfo=UTC),
                datetime(2026, 8, 22, tzinfo=UTC),
            ),
            as_of=datetime(2026, 8, 22, tzinfo=UTC),
        )
    )

    facts, next_cursor, error = await CommunitySqlAlchemyBoardKgAnalyticsEvidence(
        None  # type: ignore[arg-type]
    )._cognitive_items(query, observed_at=datetime(2026, 8, 22, tzinfo=UTC))

    assert [fact.cognitive_item_id for fact in facts] == ["from", "inside"]
    assert next_cursor is None
    assert error is None
