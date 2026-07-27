"""Community SQLAlchemy reader for bug regression preview facts."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.core.ports.bug_regression_preview import (
    RegressionCardFact,
    RegressionSpecFact,
)


def _card_fact(row: Any) -> RegressionCardFact:
    return RegressionCardFact(
        id=str(row.id),
        board_id=str(row.board_id),
        spec_id=str(row.spec_id) if row.spec_id else None,
        origin_task_id=str(row.origin_task_id) if row.origin_task_id else None,
        card_type=str(getattr(row.card_type, "value", row.card_type or "")),
        test_scenario_ids=tuple(str(value) for value in row.test_scenario_ids or ()),
    )


class CommunitySqlAlchemyBugRegressionPreviewReader:
    async def get_card(
        self,
        context: Any,
        *,
        card_id: str,
    ) -> RegressionCardFact | None:
        row = await context.get(Card, card_id)
        return _card_fact(row) if row is not None else None

    async def get_spec(
        self,
        context: Any,
        *,
        spec_id: str,
    ) -> RegressionSpecFact | None:
        row = await context.get(Spec, spec_id)
        if row is None:
            return None
        return RegressionSpecFact(
            id=str(row.id),
            board_id=str(row.board_id),
            test_scenarios=tuple(
                dict(item) for item in row.test_scenarios or () if isinstance(item, dict)
            ),
        )

    async def candidate_spec_ids(
        self,
        context: Any,
        *,
        board_id: str,
        candidate_scenario_ids: Sequence[str],
    ) -> dict[str, str]:
        candidate_set = set(candidate_scenario_ids)
        rows = (
            await context.execute(
                select(Spec.id, Spec.test_scenarios).where(Spec.board_id == board_id)
            )
        ).all()
        mapping: dict[str, str] = {}
        for spec_id, scenarios in rows:
            for scenario in scenarios or ():
                if not isinstance(scenario, dict) or scenario.get("id") is None:
                    continue
                scenario_id = str(scenario["id"])
                if scenario_id in candidate_set:
                    mapping.setdefault(scenario_id, str(spec_id))
        return mapping


__all__ = ["CommunitySqlAlchemyBugRegressionPreviewReader"]
