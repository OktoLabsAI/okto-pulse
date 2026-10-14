"""Consistent SQLite batch reads; proof predicates remain in the native store.

The dedicated connection never joins the writer's UoW and never commits. An
explicit deferred transaction is necessary because SQLite's legacy driver mode
does not start a database snapshot for SELECT merely on Session.begin().
"""
from collections import defaultdict
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
from time import perf_counter

from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.delivery_verification import native_verification_population
from okto_pulse.community.adapters.sqlalchemy_decision_reviews import CommunityDecisionReviews
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, Card, Spec, SpecHistory, DeliveryEvidenceRecordRow as Record,
    CardDeliveryEvidenceRecordRow as CardRecord, ImplementationTargetExecutionRecordRow as Execution,
    ImplementationTargetRow as Target, CodeInvestigationReceiptRow as Receipt,
    CodeInvestigationReceiptRevocationRow as Revocation,
)
from okto_pulse.core.domain.delivery_evidence import (
    CardDeliveryScope, DeliveryEvidenceSnapshot, DeliveryObligation, DeliveryScope,
)
from okto_pulse.core.domain.delivery_progress import DeliveryProgress
from okto_pulse.core.domain.enums import CardType
from okto_pulse.core.domain.execution_contract import execution_contract
from okto_pulse.core.ports.delivery_readiness import (
    ReadinessCard, ReadinessReadError, ReadinessScenarioProcedure, ReadinessSnapshot,
)
from okto_pulse.core.ports.test_evidence import supported_test_verification_methods

logger = logging.getLogger(__name__)
MAX_CARDS = 5000
MAX_HISTORY = 10000
MAX_FACTS = 20000
FINGERPRINT_VERSION = 'delivery-readiness-snapshot/v1'
EVALUATOR_VERSION = 'delivery-readiness-evaluator/v1'


class CommunityReadinessClock:
    def now(self):
        return datetime.now(timezone.utc)


def _json_value(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: json.dumps(item, default=_json_value, sort_keys=True))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json')
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f'Unsupported readiness identity type: {type(value).__name__}')


def _digest(value):
    return hashlib.sha256(json.dumps(value, default=_json_value, sort_keys=True,
        ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class _BatchFacts(CommunityDeliveryEvidenceStore):
    """Only storage access is replaced; admission/selection rules are inherited."""
    def __init__(self, session, rows, *, inventory=None):
        super().__init__(session, inventory=inventory)
        self.rows = rows
        self.index = {model: {r.id: r for r in values} for model, values in rows.items()}
        self.spec = rows[Spec][0]
        self.card_records = defaultdict(list)
        for record in rows[CardRecord]:
            self.card_records[record.card_id].append(record)
        self.heads = {}
        for row in rows[Execution]:
            key = (row.board_id, row.target_id)
            old = self.heads.get(key)
            if old is None or (row.received_at, row.id) > (old.received_at, old.id):
                self.heads[key] = row
        self.revocations = {r.receipt_id: r.id for r in rows[Revocation]}
        self.progress = {}
        self.proofs = {}
        self.obligations = None
        self.by_card = None
        self.names = {}

    async def lock_scope(self, scope):
        raise RuntimeError('readiness_write_forbidden')

    async def _get(self, model, identity):
        return self.index[model].get(identity)

    async def _related_get(self, model, identity):
        return await self._get(model, identity)

    async def _spec(self, board_id, spec_id):
        if (board_id, spec_id) != (self.spec.board_id, self.spec.id):
            raise ReadinessReadError('readiness_scope_mismatch')
        return self.spec

    async def _execution_head(self, board_id, target_id):
        row = self.heads.get((board_id, target_id))
        return row.id if row else None

    async def _receipt_revocation(self, receipt_id):
        return self.revocations.get(receipt_id)

    async def _records(self, scope):
        return [r for r in self.rows[Record] if r.edition == scope.edition]

    async def _active_material_progress(self, scope, card_id):
        if card_id not in self.progress:
            records = self.card_records[card_id]
            revoked = {r.payload.get('record_id') for r in records
                       if r.kind == 'revoke' and r.actor_kind in {'human', 'user'}}
            self.progress[card_id] = tuple((r, value) for r in records
                if r.kind == 'progress' and r.id not in revoked
                and (value := DeliveryProgress.model_validate(r.payload['progress'])).material_change != 'none')
        return self.progress[card_id]

    async def _execution_proof(self, execution_id, scope, card):
        key = (execution_id, scope, card.id)
        if key not in self.proofs:
            self.proofs[key] = await super()._execution_proof(execution_id, scope, card)
        return self.proofs[key]

    async def _execution_plan(self, spec):
        execution_contract(spec)
        fields = ('id', 'board_id', 'spec_id', 'card_type', 'status', 'archived',
                  'test_scenario_ids', 'title', 'description', 'details')
        plan = self.inventory.execution_plan(spec=spec,
            cards=[{field: getattr(c, field, None) for field in fields} for c in self.rows[Card]],
            admitted_methods=supported_test_verification_methods())
        if not plan.inventory.population_complete:
            self.obligations, self.by_card = (), {}
            return plan
        names = {o.binding.obligation_ref: o.title for o in self.inventory.spec_obligations(spec)}
        self.names = names
        self.obligations = tuple(DeliveryObligation(r.binding, names.get(r.binding.obligation_ref,
            r.binding.obligation_ref)) for r in plan.inventory.rows)
        by_binding = {o.binding: o for o in self.obligations}
        self.by_card = {card_id: tuple(by_binding[r.binding] for r in values)
            for card_id, values in plan.inventory.card_obligation_index().items()}
        return plan

    def _planned_obligations(self, spec, plan, card=None):
        if card is None or card.card_type == CardType.TEST:
            return self.obligations
        return tuple(o if o.binding.obligation_ref in self.names else replace(o, title=card.title)
            for o in self.by_card.get(card.id, ()))

    def _with_effective_context(self, snapshot, plan, records, spec, card=None, *, native_verification=None):
        if card is not None:
            # Per-Card admission does not need another complete Spec context.
            return replace(snapshot, complete=snapshot.complete and plan.complete)
        return super()._with_effective_context(snapshot, plan, records, spec,
            native_verification=native_verification)

    async def snapshot(self):
        spec = self.spec
        scope = DeliveryScope(spec.board_id, spec.id, int(spec.edition))
        plan = await self._execution_plan(spec)
        native = native_verification_population(spec)
        if not plan.inventory.population_complete:
            return self._with_effective_context(DeliveryEvidenceSnapshot(scope, (), complete=False,
                facts_complete=False), plan, (), spec, native_verification=native)
        native_runs = defaultdict(list)
        for run in native.runs:
            native_runs[run.scenario_id].append(run)
        implementations, tests, rejections = [], [], []
        complete = facts_complete = True
        active_cards = [c for c in self.rows[Card] if not c.archived]
        for card in active_cards:
            part = await self.load_card_snapshot(CardDeliveryScope(scope.board_id, card.id,
                scope.spec_id, scope.edition), plan=plan, records=self.card_records[card.id],
                native_verification=native, native_runs=native_runs)
            implementations.extend(part.implementations)
            tests.extend(part.tests)
            rejections.extend(part.rejected_records)
            complete = complete and part.complete is True
            facts_complete = facts_complete and part.facts_complete is True
        result = DeliveryEvidenceSnapshot(scope, self.obligations, tuple(implementations), tuple(tests),
            tuple(await self._rollup_waivers(scope)), complete=complete,
            rejected_records=tuple(rejections), facts_complete=facts_complete)
        result = self._with_effective_context(result, plan, self.rows[CardRecord], spec, native_verification=native)
        if result.effective_context and any(r.decision_plan and r.decision_plan.complete
                and r.decision_plan.verification.inspection for r in result.effective_context.inventory.rows):
            _, states, _, _, _ = await CommunityDecisionReviews(self).material(spec, result,
                sources=(self.rows[Board][0], self.rows[Record], self.rows[SpecHistory], active_cards))
            result = replace(result, effective_context=replace(result.effective_context, decision_inspections=states))
        return result


class CommunityDeliveryReadinessReader:
    def __init__(self, engine, *, clock, inventory=None):
        if engine.url.get_backend_name() != 'sqlite':
            raise ValueError('community_readiness_requires_sqlite')
        self.engine, self.clock, self.inventory = engine, clock, inventory

    async def _rows(self, session, statement, limit, counts):
        counts['queries'] += 1
        rows = tuple((await session.scalars(statement.limit(limit + 1))).all())
        if len(rows) > limit:
            raise ReadinessReadError('readiness_limit_exceeded', native_code='readiness_population_limit')
        counts['facts'] += len(rows)
        return rows

    async def _load_transaction(self, session, board_id, spec_id, card_id, counts):
        rows = {}
        rows[Spec] = await self._rows(session, select(Spec).where(Spec.id == spec_id,
            Spec.board_id == board_id, Spec.archived.is_(False)), 1, counts)
        rows[Board] = await self._rows(session, select(Board).where(Board.id == board_id), 1, counts)
        if not rows[Spec] or not rows[Board]:
            raise ReadinessReadError('readiness_scope_mismatch')
        spec = rows[Spec][0]
        if isinstance(spec.test_scenarios, list) and len(spec.test_scenarios) > 5000:
            raise ReadinessReadError('readiness_limit_exceeded', native_code='native_verification_population_unavailable')
        cards = select(Card.id).where(Card.board_id == board_id, Card.spec_id == spec_id)
        rows[Card] = await self._rows(session, select(Card).where(Card.id.in_(cards)).order_by(Card.id), MAX_CARDS, counts)
        if card_id is not None and not any(c.id == card_id and not c.archived for c in rows[Card]):
            raise ReadinessReadError('readiness_scope_mismatch')
        rows[CardRecord] = await self._rows(session, select(CardRecord).where(CardRecord.board_id == board_id,
            CardRecord.spec_id == spec_id, CardRecord.spec_edition == spec.edition,
            CardRecord.card_id.in_(cards)).order_by(CardRecord.created_at, CardRecord.id), MAX_FACTS, counts)
        rows[Record] = await self._rows(session, select(Record).where(Record.board_id == board_id,
            Record.spec_id == spec_id).order_by(Record.created_at, Record.id), MAX_HISTORY, counts)
        rows[SpecHistory] = await self._rows(session, select(SpecHistory).where(SpecHistory.spec_id == spec_id)
            .order_by(SpecHistory.version, SpecHistory.created_at), MAX_HISTORY, counts)
        targets = select(Target.id).where(Target.board_id == board_id, Target.card_id.in_(cards))
        rows[Target] = await self._rows(session, select(Target).where(Target.id.in_(targets)).order_by(Target.id), MAX_FACTS, counts)
        executions = select(Execution.id).where(Execution.board_id == board_id,
            or_(Execution.card_id.in_(cards), Execution.target_id.in_(targets)))
        rows[Execution] = await self._rows(session, select(Execution).where(Execution.id.in_(executions))
            .order_by(Execution.id), MAX_FACTS, counts)
        receipts = select(Execution.result_investigation_receipt_id).where(Execution.id.in_(executions))
        rows[Receipt] = await self._rows(session, select(Receipt).where(Receipt.board_id == board_id,
            Receipt.id.in_(receipts)).order_by(Receipt.id), MAX_FACTS, counts)
        rows[Revocation] = await self._rows(session, select(Revocation).where(Revocation.receipt_id.in_(receipts))
            .order_by(Revocation.id), MAX_FACTS, counts)
        facts = await _BatchFacts(session, rows, inventory=self.inventory).snapshot()
        # Hash actual content as well as identities. No mutable ORM objects,
        # raw ledger payloads, credentials or full Board configuration escape.
        identities = {model.__tablename__: sorted((r.id, _digest({c.key: getattr(r, c.key)
            for c in model.__table__.columns})) for r in values) for model, values in rows.items()}
        identity = _digest({'contract_version': 'delivery-readiness/v1', 'fingerprint_version': FINGERPRINT_VERSION,
            'evaluator_version': EVALUATOR_VERSION,
            'inputs': identities, 'effective_facts': facts})
        evaluated_at = self.clock.now()
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ReadinessReadError('readiness_assessment_unavailable', native_code='readiness_clock_timezone_required')
        def text(row, key):
            value = row.get(key)
            return value if isinstance(value, str) else None
        scenarios = tuple(ReadinessScenarioProcedure(s['id'], *(text(s, key) for key in
            ('title', 'verification_method', 'given', 'when', 'then', 'notes')),
            tuple(v for v in (s.get('linked_criteria') or ()) if isinstance(v, str)))
            for s in spec.test_scenarios if isinstance(s, dict) and isinstance(s.get('id'), str)) if isinstance(spec.test_scenarios, list) else ()
        return ReadinessSnapshot(facts, identity, evaluated_at.astimezone(timezone.utc), int(spec.version), spec.status.value,
            tuple(ReadinessCard(c.id, c.card_type, c.status, c.policy_version,
                tuple(c.test_scenario_ids) if c.test_scenario_ids is not None else None, c.assignee_id)
                for c in rows[Card] if not c.archived), scenarios)

    async def load(self, *, board_id, spec_id, card_id=None):
        started = perf_counter()
        counts = {'queries': 0, 'facts': 0}
        try:
            for attempt in range(2):
                async with self.engine.connect() as connection:
                    counts['queries'] += 3
                    before = (await connection.exec_driver_sql('PRAGMA data_version')).scalar_one()
                    await connection.exec_driver_sql('BEGIN')
                    try:
                        async with AsyncSession(bind=connection, autoflush=False, expire_on_commit=False) as session:
                            result = await self._load_transaction(session, board_id, spec_id, card_id, counts)
                    finally:
                        await connection.rollback()
                    # data_version changes on this connection after ending the
                    # snapshot if another connection committed while we read.
                    after = (await connection.exec_driver_sql('PRAGMA data_version')).scalar_one()
                    if before == after:
                        return result
            raise ReadinessReadError('readiness_snapshot_changed')
        finally:
            logger.debug('readiness_batch_read elapsed_ms=%.3f queries=%d facts=%d',
                (perf_counter() - started) * 1000, counts['queries'], counts['facts'])
