"""Community SQLAlchemy reads for discovery intent execution."""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Board,
    Card,
    CardDependency,
    Comment,
    Ideation,
    Refinement,
    Spec,
    Sprint,
)
from okto_pulse.core.ports.discovery_execution import (
    DiscoveryActivityFact,
    DiscoveryCardFact,
    DiscoveryDependencyFact,
    DiscoveryDependentCardFact,
    DiscoveryMentionFact,
    DiscoverySpecFact,
    DiscoverySprintFact,
)


def _spec_fact(row: Any) -> DiscoverySpecFact:
    return DiscoverySpecFact(
        id=str(row.id),
        board_id=str(row.board_id),
        title=row.title or "",
        status=row.status,
        version=getattr(row, "version", None),
        functional_requirements=tuple(row.functional_requirements or ()),
        business_rules=tuple(row.business_rules or ()),
        technical_requirements=tuple(row.technical_requirements or ()),
        decisions=tuple(row.decisions or ()),
        acceptance_criteria=tuple(row.acceptance_criteria or ()),
        api_contracts=tuple(row.api_contracts or ()),
        integration_requirements=tuple(row.integration_requirements or ()),
        observability_requirements=tuple(row.observability_requirements or ()),
        test_scenarios=tuple(row.test_scenarios or ()),
        skip_rules_coverage=bool(row.skip_rules_coverage),
        skip_test_coverage=bool(row.skip_test_coverage),
        skip_trs_coverage=bool(row.skip_trs_coverage),
        skip_contract_coverage=bool(row.skip_contract_coverage),
        skip_ir_coverage=bool(row.skip_ir_coverage),
        skip_or_coverage=bool(row.skip_or_coverage),
        skip_decisions_coverage=bool(row.skip_decisions_coverage),
        skip_code_evidence_coverage=bool(
            getattr(row, "skip_code_evidence_coverage", False)
        ),
    )


def _card_fact(row: Any) -> DiscoveryCardFact:
    return DiscoveryCardFact(
        id=str(row.id),
        board_id=str(row.board_id),
        title=row.title or "",
        status=row.status,
        priority=row.priority,
        spec_id=str(row.spec_id) if row.spec_id else None,
        sprint_id=str(row.sprint_id) if row.sprint_id else None,
        archived=bool(row.archived),
        updated_at=row.updated_at,
    )


class CommunitySqlAlchemyDiscoveryExecutionReader:
    async def get_spec_by_id(
        self, context: Any, *, spec_id: str
    ) -> DiscoverySpecFact | None:
        row = (
            await context.execute(select(Spec).where(Spec.id == spec_id))
        ).scalar_one_or_none()
        return _spec_fact(row) if row is not None else None

    async def get_card_by_id(
        self, context: Any, *, card_id: str
    ) -> DiscoveryCardFact | None:
        row = (
            await context.execute(select(Card).where(Card.id == card_id))
        ).scalar_one_or_none()
        return _card_fact(row) if row is not None else None

    async def list_recent_activity(
        self, context: Any, *, board_id: str, limit: int
    ) -> tuple[DiscoveryActivityFact, ...]:
        rows = (
            (
                await context.execute(
                    select(ActivityLog)
                    .where(ActivityLog.board_id == board_id)
                    .order_by(ActivityLog.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            DiscoveryActivityFact(
                id=str(row.id),
                action=str(row.action),
                details=dict(row.details or {}),
                card_id=str(row.card_id) if row.card_id else None,
                actor_id=str(row.actor_id),
                actor_type=str(row.actor_type),
                actor_name=row.actor_name,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def resolve_entity_titles(
        self,
        context: Any,
        *,
        refs: Sequence[tuple[str, str]],
    ) -> dict[tuple[str, str], str]:
        by_type: dict[str, set[str]] = {}
        for entity_type, entity_id in refs:
            by_type.setdefault(entity_type, set()).add(entity_id)
        models = {
            "card": Card,
            "spec": Spec,
            "ideation": Ideation,
            "refinement": Refinement,
            "sprint": Sprint,
        }
        output: dict[tuple[str, str], str] = {}
        for entity_type, ids in by_type.items():
            model = models.get(entity_type)
            if model is None or not ids:
                continue
            rows = (
                await context.execute(
                    select(model.id, model.title).where(model.id.in_(ids))
                )
            ).all()
            for row_id, title in rows:
                output[(entity_type, str(row_id))] = title or ""
        return output

    async def list_sprints(
        self, context: Any, *, board_id: str
    ) -> tuple[DiscoverySprintFact, ...]:
        rows = (
            (
                await context.execute(
                    select(Sprint).where(
                        Sprint.board_id == board_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            DiscoverySprintFact(
                str(row.id), str(row.board_id), row.title or "", row.status
            )
            for row in rows
        )

    async def list_cards_for_sprints(
        self,
        context: Any,
        *,
        board_id: str,
        sprint_ids: Sequence[str],
    ) -> tuple[DiscoveryCardFact, ...]:
        if not sprint_ids:
            return ()
        rows = (
            (
                await context.execute(
                    select(Card).where(
                        Card.board_id == board_id,
                        Card.archived.is_(False),
                        Card.sprint_id.in_(tuple(sprint_ids)),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_card_fact(row) for row in rows)

    async def list_dependencies_for_cards(
        self, context: Any, *, card_ids: Sequence[str]
    ) -> tuple[DiscoveryDependencyFact, ...]:
        if not card_ids:
            return ()
        rows = (
            (
                await context.execute(
                    select(CardDependency).where(
                        CardDependency.card_id.in_(tuple(card_ids))
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(
            DiscoveryDependencyFact(
                str(row.card_id), str(row.depends_on_id), row.created_at
            )
            for row in rows
        )

    async def list_cards_by_ids(
        self, context: Any, *, card_ids: Sequence[str]
    ) -> tuple[DiscoveryCardFact, ...]:
        if not card_ids:
            return ()
        rows = (
            (await context.execute(select(Card).where(Card.id.in_(tuple(card_ids)))))
            .scalars()
            .all()
        )
        return tuple(_card_fact(row) for row in rows)

    async def list_card_dependents(
        self, context: Any, *, card_id: str
    ) -> tuple[DiscoveryDependentCardFact, ...]:
        rows = (
            await context.execute(
                select(CardDependency, Card)
                .join(Card, Card.id == CardDependency.card_id)
                .where(CardDependency.depends_on_id == card_id)
            )
        ).all()
        return tuple(
            DiscoveryDependentCardFact(
                DiscoveryDependencyFact(
                    str(dependency.card_id),
                    str(dependency.depends_on_id),
                    dependency.created_at,
                ),
                _card_fact(card),
            )
            for dependency, card in rows
        )

    async def list_mentions(
        self,
        context: Any,
        *,
        board_id: str,
        mention_token: str,
        limit: int,
    ) -> tuple[DiscoveryMentionFact, ...]:
        rows = (
            await context.execute(
                select(Comment, Card)
                .join(Card, Card.id == Comment.card_id)
                .where(Card.board_id == board_id)
                .where(Comment.content.contains(mention_token))
                .order_by(Comment.created_at.desc())
                .limit(limit)
            )
        ).all()
        return tuple(
            DiscoveryMentionFact(
                id=str(comment.id),
                content=comment.content or "",
                author_id=str(comment.author_id),
                created_at=comment.created_at,
                card=_card_fact(card),
            )
            for comment, card in rows
        )

    async def list_specs(
        self,
        context: Any,
        *,
        board_id: str,
    ) -> tuple[DiscoverySpecFact, ...]:
        statement = select(Spec).where(Spec.board_id == board_id)
        rows = (await context.execute(statement)).scalars().all()
        return tuple(_spec_fact(row) for row in rows)

    async def get_board_settings(
        self, context: Any, *, board_id: str
    ) -> dict[str, Any]:
        row = await context.get(Board, board_id)
        return dict(row.settings or {}) if row is not None else {}

    async def list_board_cards(
        self, context: Any, *, board_id: str
    ) -> tuple[DiscoveryCardFact, ...]:
        rows = (
            (
                await context.execute(
                    select(Card).where(
                        Card.board_id == board_id,
                        Card.archived.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        return tuple(_card_fact(row) for row in rows)


__all__ = ["CommunitySqlAlchemyDiscoveryExecutionReader"]
