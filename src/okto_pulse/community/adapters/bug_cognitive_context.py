"""Community assembly of the canonical bug-cognitive closeout context."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision,
    Card,
    Comment,
    Spec,
)
from okto_pulse.core.ports.bug_cognitive_context import (
    BugCognitiveContext,
    BugLinkedTestTask,
    CanonicalBugNodeReadPort,
    freeze_mapping_sequence,
)

logger = logging.getLogger("okto_pulse.community.bug_cognitive_context")


def _enum_value(value: Any) -> str | None:
    raw = value.value if hasattr(value, "value") else value
    return str(raw) if raw is not None else None


def _mapping_rows(values: Any) -> tuple[Mapping[str, object], ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    return freeze_mapping_sequence(
        [value for value in values if isinstance(value, Mapping)]
    )


class CommunityCanonicalBugNodeReader:
    """Canonical Bug lookup through the configured graph read interface."""

    def __init__(self, cypher_executor: Any | None = None) -> None:
        self._cypher_executor = cypher_executor

    async def exists(self, *, board_id: str, bug_id: str) -> bool:
        cypher_executor = self._cypher_executor
        if cypher_executor is None:
            from okto_pulse.core.services.application_kg import (
                get_current_provider_registry,
            )

            cypher_executor = get_current_provider_registry().cypher_executor
        result = cypher_executor.execute_read_only(
            board_id,
            "MATCH (b:Bug) WHERE b.graph_layer = 'canonical' "
            "AND (b.id = $bug_id OR b.source_artifact_ref = $bug_ref "
            "OR b.source_artifact_ref = $card_ref "
            "OR b.source_artifact_ref = $card_bug_ref) RETURN count(b)",
            {
                "bug_id": bug_id,
                "bug_ref": f"bug:{bug_id}",
                "card_ref": f"card:{bug_id}",
                "card_bug_ref": f"card:bug:{bug_id}",
            },
            max_rows=1,
        )
        rows = result.get("rows") or ()
        return bool(rows and int(rows[0][0] or 0) > 0)


class CommunityBugCognitiveContextAssembler:
    """Loads one immutable closeout snapshot from Community persistence.

    This is the single producer used by REST, MCP and the worker.  Graph probe
    failures are retained as explicit load errors so policy can fail closed and
    the operator can distinguish an absent node from an unavailable backend.
    """

    def __init__(
        self,
        canonical_bug_reader: CanonicalBugNodeReadPort | None = None,
    ) -> None:
        self._canonical_bug_reader = (
            canonical_bug_reader or CommunityCanonicalBugNodeReader()
        )

    async def assemble(
        self,
        context: Any,
        *,
        board_id: str,
        bug_id: str,
    ) -> BugCognitiveContext:
        card = await context.get(Card, bug_id)
        if card is None or str(card.board_id) != str(board_id):
            return BugCognitiveContext(
                board_id=board_id,
                bug_id=bug_id,
                card_exists=False,
                provenance_refs=(f"sql:cards/{bug_id}",),
            )

        comments_result = await context.execute(
            select(Comment)
            .where(Comment.card_id == bug_id)
            .order_by(Comment.created_at.asc(), Comment.id.asc())
        )
        comments = tuple(
            {
                "id": row.id,
                "content": row.content,
                "author_id": row.author_id,
                "comment_type": row.comment_type,
                "created_at": row.created_at,
            }
            for row in comments_result.scalars().all()
        )

        spec = await context.get(Spec, card.spec_id) if card.spec_id else None
        if spec is not None and str(spec.board_id) != str(board_id):
            spec = None
        all_scenarios = _mapping_rows(
            spec.test_scenarios if spec is not None else None
        )
        acceptance_criteria = tuple(
            dict(value) if isinstance(value, Mapping) else value
            for value in (
                (spec.acceptance_criteria or ()) if spec is not None else ()
            )
        )

        linked_ids = tuple(
            str(value)
            for value in (card.linked_test_task_ids or ())
            if value is not None and str(value).strip()
        )
        linked_tasks: tuple[BugLinkedTestTask, ...] = ()
        rows_by_id: dict[str, Card] = {}
        if linked_ids:
            linked_result = await context.execute(
                select(Card).where(
                    Card.board_id == board_id,
                    Card.id.in_(linked_ids),
                )
            )
            rows_by_id = {str(row.id): row for row in linked_result.scalars().all()}
            linked_tasks = tuple(
                BugLinkedTestTask(
                    card_id=linked_id,
                    status=_enum_value(rows_by_id[linked_id].status),
                    card_type=_enum_value(rows_by_id[linked_id].card_type),
                    conclusions=_mapping_rows(rows_by_id[linked_id].conclusions),
                    validations=_mapping_rows(rows_by_id[linked_id].validations),
                )
                for linked_id in linked_ids
                if linked_id in rows_by_id
            )

        lineage_result = await context.execute(
            select(AmendmentHotfixRevision)
            .where(
                AmendmentHotfixRevision.board_id == board_id,
                AmendmentHotfixRevision.origin_bug_id == bug_id,
            )
            .order_by(AmendmentHotfixRevision.created_at.asc())
        )
        lineage_rows = tuple(
            {
                "id": row.id,
                "original_spec_id": row.original_spec_id,
                "origin_bug_id": row.origin_bug_id,
                "origin_task_ids": list(row.origin_task_ids or ()),
                "affected_task_ids": list(row.affected_task_ids or ()),
                "revision_spec_id": row.revision_spec_id,
                "regression_scenario_ids": list(row.regression_scenario_ids or ()),
                "regression_test_task_ids": list(
                    row.regression_test_task_ids or ()
                ),
                "automated_regression_refs": list(
                    row.automated_regression_refs or ()
                ),
                "status": _enum_value(row.status),
                "lineage_state": _enum_value(row.lineage_state),
                "validation_metadata": dict(row.validation_metadata or {}),
            }
            for row in lineage_result.scalars().all()
        )

        explicit_scenario_ids = {
            str(value)
            for value in (card.test_scenario_ids or ())
            if value is not None and str(value).strip()
        }
        for linked_row in rows_by_id.values():
            explicit_scenario_ids.update(
                str(value)
                for value in (linked_row.test_scenario_ids or ())
                if value is not None and str(value).strip()
            )
        for lineage in lineage_rows:
            explicit_scenario_ids.update(
                str(value)
                for value in (lineage.get("regression_scenario_ids") or ())
                if value is not None and str(value).strip()
            )
        relevant_task_ids = {
            bug_id,
            *(linked_ids or ()),
            *((card.origin_task_id,) if card.origin_task_id else ()),
        }
        scenarios = tuple(
            scenario
            for scenario in all_scenarios
            if str(scenario.get("id") or "") in explicit_scenario_ids
            or bool(
                relevant_task_ids.intersection(
                    str(value)
                    for value in (scenario.get("linked_task_ids") or ())
                    if value is not None
                )
            )
        )

        load_errors: list[str] = []
        canonical_bug_present: bool | None
        try:
            canonical_bug_present = await self._canonical_bug_reader.exists(
                board_id=board_id,
                bug_id=bug_id,
            )
        except Exception as exc:  # the policy receives an explicit unknown state
            canonical_bug_present = None
            load_errors.append("canonical_bug_probe_failed")
            logger.warning(
                "bug_cognitive_context.canonical_probe_failed board=%s bug=%s error=%s",
                board_id,
                bug_id,
                type(exc).__name__,
                extra={
                    "event": "bug_cognitive_context.canonical_probe_failed",
                    "board_id": board_id,
                    "bug_id": bug_id,
                    "error_type": type(exc).__name__,
                },
            )

        provenance = [f"sql:cards/{bug_id}"]
        if card.spec_id:
            provenance.append(f"sql:specs/{card.spec_id}")
        provenance.extend(f"sql:comments/{row['id']}" for row in comments)
        provenance.extend(f"sql:cards/{row.card_id}" for row in linked_tasks)
        provenance.extend(
            f"sql:amendment_hotfix_revisions/{row['id']}" for row in lineage_rows
        )
        if canonical_bug_present is not None:
            provenance.append(f"kg:canonical/bug/{bug_id}")

        return BugCognitiveContext(
            board_id=board_id,
            bug_id=bug_id,
            card_exists=True,
            card_type=_enum_value(card.card_type),
            status=_enum_value(card.status),
            title=card.title,
            description=card.description,
            expected_behavior=card.expected_behavior,
            observed_behavior=card.observed_behavior,
            steps_to_reproduce=card.steps_to_reproduce,
            action_plan=card.action_plan,
            severity=_enum_value(card.severity),
            spec_id=card.spec_id,
            origin_task_id=card.origin_task_id,
            linked_test_task_ids=linked_ids,
            conclusions=_mapping_rows(card.conclusions),
            validations=_mapping_rows(card.validations),
            comments=comments,
            acceptance_criteria=acceptance_criteria,
            test_scenarios=scenarios,
            linked_test_tasks=linked_tasks,
            lineage=lineage_rows,
            canonical_bug_present=canonical_bug_present,
            provenance_refs=tuple(provenance),
            load_errors=tuple(load_errors),
        )


__all__ = [
    "CommunityBugCognitiveContextAssembler",
    "CommunityCanonicalBugNodeReader",
]
