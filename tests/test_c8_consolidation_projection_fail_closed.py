"""Fail-closed C8 contracts for joined relational projection reads."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    QualityAssessmentHeadRow,
    ResearchDecisionHeadRow,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows: list[tuple[object, object | None]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, object | None]]:
        return self._rows


class _Context:
    def __init__(self, *results: _Result) -> None:
        self._results = list(results)
        self.query_count = 0

    async def execute(self, _statement) -> _Result:
        self.query_count += 1
        return self._results.pop(0)


async def test_quality_head_without_receipt_fails_closed_in_one_query() -> None:
    head = QualityAssessmentHeadRow(
        board_id="board-c8",
        subject_type="spec",
        subject_id="spec-c8",
        assessment_kind="ambiguity",
        receipt_id="missing-receipt",
        revision=1,
        updated_at=NOW,
    )
    context = _Context(_Result([(head, None)]))

    with pytest.raises(RuntimeError, match="quality_projection_head_dangling"):
        await CommunitySqlAlchemyConsolidationPersistence().load_projection_inputs(
            context,
            board_id="board-c8",
            artifact_type="spec",
            artifact_id="spec-c8",
        )

    assert context.query_count == 1


async def test_rdl_head_without_current_entry_fails_closed_in_two_queries() -> None:
    head = ResearchDecisionHeadRow(
        ledger_id="ledger-c8",
        board_id="board-c8",
        refinement_id="refinement-c8",
        current_entry_id="missing-entry",
        revision=1,
        refinement_version=2,
        status="resolved",
        updated_by="agent-c8",
        updated_at=NOW,
    )
    context = _Context(_Result([]), _Result([(head, None)]))

    with pytest.raises(
        RuntimeError,
        match="research_decision_projection_head_dangling",
    ):
        await CommunitySqlAlchemyConsolidationPersistence().load_projection_inputs(
            context,
            board_id="board-c8",
            artifact_type="refinement",
            artifact_id="refinement-c8",
        )

    assert context.query_count == 2
