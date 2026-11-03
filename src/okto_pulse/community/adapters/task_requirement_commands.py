"""Relational facts and one transaction for the Core requirement command."""
from sqlalchemy import select
from okto_pulse.community.adapters.sqlalchemy_models import Card, Board, CardDependency, ActivityLog, DomainEventRow
from okto_pulse.community.adapters.sqlalchemy_task_requirement_decisions import CommunityTaskRequirementDecisionStore
from okto_pulse.core.domain.delivery_evidence import DeliveryScope
from okto_pulse.core.domain.delivery_inventory import delivery_digest
from okto_pulse.core.services.task_requirement_decision import (
    TaskRequirementDecisionState, task_requirement_preview, prepare_task_requirement_decision,
    task_requirement_scenario_digest,
)
from okto_pulse.core.domain.task_requirement_decision import resolve_task_requirement


def _value(value):
    return getattr(value, 'value', value)


class CommunityTaskRequirementCommands:
    def __init__(self, delivery):
        self.delivery = delivery
        self.session = delivery.session
        self.store = CommunityTaskRequirementDecisionStore(self.session)

    async def state(self, command):
        spec = await self.delivery._spec(command.board_id, command.spec_id)
        board = await self.session.get(Board, command.board_id, populate_existing=True)
        cards = list((await self.session.scalars(select(Card).where(
            Card.board_id == command.board_id, Card.id.in_(command.card_ids),
        ).order_by(Card.id).execution_options(populate_existing=True))).all())
        if {card.id for card in cards} != set(command.card_ids):
            raise ValueError('task_requirement_card_not_found')
        plan = await self.delivery._execution_plan(spec)
        fields = ('id', 'board_id', 'spec_id', 'card_type', 'status', 'archived',
                  'test_scenario_ids', 'title', 'policy_version')
        facts = tuple({field: _value(getattr(card, field)) for field in fields} for card in cards)
        histories = await self.store.history(board_id=command.board_id, spec_id=spec.id,
            edition=spec.edition, card_ids=command.card_ids)
        needed = {identity for card in facts if card['card_type'] == 'test'
                  for identity in (card['test_scenario_ids'] or ())}
        digests = {scenario['id']: task_requirement_scenario_digest(board_id=spec.board_id,
            spec_id=spec.id, scenario=scenario, acceptance_criteria=spec.acceptance_criteria)
            for scenario in (spec.test_scenarios or ()) if scenario.get('id') in needed}
        dependencies = list((await self.session.execute(select(
            Card.id, Card.title, Card.status, CardDependency.depends_on_id,
        ).join(CardDependency, CardDependency.card_id == Card.id).where(
            Card.board_id == command.board_id, Card.archived.is_(False),
            CardDependency.depends_on_id.in_(command.card_ids),
        ).order_by(Card.id, CardDependency.depends_on_id).limit(5001))).all())
        if len(dependencies) > 5000:
            raise ValueError('task_requirement_scope_unavailable')
        return TaskRequirementDecisionState(command.board_id, spec.id, int(spec.edition), int(spec.version),
            _value(spec.status), plan.inventory, facts, histories, digests,
            tuple(dict(card_id=row.id, title=row.title, status=_value(row.status), prerequisite_id=row.depends_on_id)
                  for row in dependencies), delivery_digest(board.settings or {}))

    async def preview(self, command, *, actor_id, allowed_decisions):
        state = await self.state(command)
        return task_requirement_preview(state, card_ids=command.card_ids,
            actor_id=actor_id, allowed_decisions=allowed_decisions)[0]

    async def history(self, query):
        spec = await self.delivery._spec(query.board_id, query.spec_id)
        card_id = query.card_ids[0]
        card = await self.session.get(Card, card_id, populate_existing=True)
        if card is None or card.board_id != query.board_id:
            raise ValueError('task_requirement_card_not_found')
        edition = query.edition or int(spec.edition)
        if edition > int(spec.edition):
            raise ValueError('task_requirement_edition_not_found')
        history = (await self.store.history(board_id=query.board_id, spec_id=spec.id,
            edition=edition, card_ids=[card_id]))[card_id]
        if history:
            resolve_task_requirement(history[-1].scope, history)
        elif card.spec_id != spec.id:
            raise ValueError('task_requirement_card_not_found')
        rows = [row for row in reversed(history)
            if (query.before_revision is None or row.revision < query.before_revision)
            and (query.record_id is None or row.id == query.record_id)]
        if query.record_id and not rows:
            raise ValueError('task_requirement_decision_not_found')
        page = rows[:query.limit]
        return dict(contract_version='task-requirement-history/v1', board_id=query.board_id,
            spec_id=spec.id, card_id=card_id, edition=edition, current_edition=int(spec.edition),
            total=len(history), next_before_revision=page[-1].revision if len(rows) > len(page) else None,
            items=[dict(id=row.id, revision=row.revision, decision=row.decision,
                previous_decision=row.previous_decision, actor_id=row.actor_id,
                recorded_at=row.recorded_at.isoformat(), scope_sha256=row.scope.digest,
                note=row.user_note if query.record_id else (row.user_note or '')[:500] or None,
                note_truncated=not query.record_id and len(row.user_note or '') > 500)
                for row in page])

    async def record(self, command, *, actor_id, actor_kind):
        spec = await self.delivery._spec(command.board_id, command.spec_id)
        await self.delivery.lock_scope(DeliveryScope(command.board_id, spec.id, int(spec.edition)))
        digest = delivery_digest(command.model_dump(mode='json'))
        existing = await self.store.operation(board_id=command.board_id, spec_id=command.spec_id,
            actor_id=actor_id, idempotency_key=command.idempotency_key)
        if existing is not None:
            if existing.request_sha256 != digest:
                raise ValueError('task_requirement_idempotency_conflict')
            return {**existing.response, 'replayed': True}
        state = await self.state(command)
        records, response = prepare_task_requirement_decision(state, command,
            actor_id=actor_id, actor_kind=actor_kind)
        async with self.session.begin_nested():
            await self.store.append(operation_id=response['operation_id'], board_id=command.board_id,
                spec_id=command.spec_id, edition=state.edition, actor_id=actor_id,
                idempotency_key=command.idempotency_key, request_sha256=digest,
                response=response, records=records)
            details = dict(operation_id=response['operation_id'], spec_id=command.spec_id,
                spec_edition=state.edition, card_ids=list(command.card_ids), decision=command.decision,
                decision_ids=response['decision_ids'])
            self.session.add(ActivityLog(board_id=command.board_id,
                card_id=command.card_ids[0] if len(command.card_ids) == 1 else None,
                actor_id=actor_id, actor_type='user', actor_name=actor_id,
                action='task.requirement_decision_applied', details=details))
            self.session.add(DomainEventRow(board_id=command.board_id, actor_id=actor_id,
                actor_type='user', event_type='task.requirement_decision_applied', payload_json=details))
            await self.session.flush()
        return response
