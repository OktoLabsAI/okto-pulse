"""Transactional persistence for Core-owned human requirement decisions."""
from sqlalchemy import select
from okto_pulse.core.domain.task_requirement_decision import TaskRequirementDecisionRecord
from okto_pulse.community.adapters.sqlalchemy_models import (
    TaskRequirementDecisionOperationRow as Operation,
    TaskRequirementDecisionRow as Decision,
)


class CommunityTaskRequirementDecisionStore:
    def __init__(self, session):
        self.session = session

    async def operation(self, *, board_id, spec_id, actor_id, idempotency_key):
        return (await self.session.scalars(select(Operation).where(
            Operation.board_id == board_id, Operation.spec_id == spec_id,
            Operation.actor_id == actor_id, Operation.idempotency_key == idempotency_key,
        ))).one_or_none()

    async def history(self, *, board_id, spec_id, edition, card_ids):
        if not 1 <= len(card_ids) <= 50 or len(set(card_ids)) != len(card_ids):
            raise ValueError('task_requirement_selection_invalid')
        rows = list((await self.session.scalars(select(Decision).where(
            Decision.board_id == board_id, Decision.spec_id == spec_id,
            Decision.spec_edition == edition, Decision.card_id.in_(card_ids),
        ).order_by(Decision.card_id, Decision.revision).limit(50001))).all())
        if len(rows) > 50000:
            raise ValueError('task_requirement_history_unavailable')
        result = {card_id: [] for card_id in card_ids}
        for row in rows:
            fact = TaskRequirementDecisionRecord.from_dict(row.payload)
            if (fact.id, fact.operation_id, fact.scope.board_id, fact.scope.spec_id,
                fact.scope.edition, fact.scope.card_id, fact.revision, fact.decision, fact.scope.digest) != (
                row.id, row.operation_id, board_id, spec_id, edition, row.card_id,
                row.revision, row.decision, row.scope_sha256):
                raise ValueError('task_requirement_history_invalid')
            result[row.card_id].append(fact)
        return {key: tuple(value) for key, value in result.items()}

    async def append(self, *, operation_id, board_id, spec_id, edition, actor_id,
                     idempotency_key, request_sha256, response, records):
        records = tuple(records)
        if (not 1 <= len(records) <= 50 or len({row.scope.card_id for row in records}) != len(records)
                or any((row.operation_id, row.scope.board_id, row.scope.spec_id, row.scope.edition, row.actor_id) !=
                       (operation_id, board_id, spec_id, edition, actor_id) for row in records)):
            raise ValueError('task_requirement_batch_invalid')
        # No commit here: decision, activity/event and derived-state invalidation
        # belong to the caller's same outer transaction. A caught flush failure
        # still rolls this entire batch back through the savepoint.
        async with self.session.begin_nested():
            self.session.add(Operation(id=operation_id, board_id=board_id, spec_id=spec_id,
                spec_edition=edition, actor_id=actor_id, idempotency_key=idempotency_key,
                request_sha256=request_sha256, response=response, recorded_at=records[0].recorded_at))
            await self.session.flush()
            for record in records:
                self.session.add(Decision(id=record.id, operation_id=operation_id, board_id=board_id,
                    spec_id=spec_id, spec_edition=edition, card_id=record.scope.card_id,
                    revision=record.revision, decision=record.decision,
                    scope_sha256=record.scope.digest, payload=record.to_dict()))
            await self.session.flush()
