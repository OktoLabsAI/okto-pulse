"""Consistent SQLite batch reads; proof predicates remain in the native store.

The dedicated connection never joins the writer's UoW and never commits. An
explicit deferred transaction is necessary because SQLite's legacy driver mode
does not start a database snapshot for SELECT merely on Session.begin().
"""
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
import hashlib
import json
import logging
from time import perf_counter, process_time
from types import SimpleNamespace

from pydantic import BaseModel
from sqlalchemy import and_, event, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from okto_pulse.community.adapters.delivery_verification import native_verification_population
from okto_pulse.community.adapters.sqlalchemy_decision_reviews import CommunityDecisionReviews
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.core.application.service_catalog import build_application_service_catalog
from okto_pulse.community.adapters.sqlalchemy_models import (
    Agent, AgentBoard, Board, BoardShare, PermissionPreset, Card, Spec, SpecHistory, DeliveryEvidenceRecordRow as Record,
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
from okto_pulse.core.domain.realm import LOCAL_REALM_ID, RealmScope
from okto_pulse.core.ports.delivery_readiness import (
    ReadinessAccessDenied, ReadinessActor, ReadinessCard, ReadinessReadError,
    ReadinessScenarioProcedure, ReadinessSnapshot,
    resolve_readiness_access, resolve_delivery_gate_policy,
)
from okto_pulse.core.ports.permission_policy import (
    PermissionPresetLineageNode, board_membership_allows_read, resolve_agent_permission_facts,
)
from okto_pulse.core.ports.test_evidence import supported_test_verification_methods
from okto_pulse.core.services.persisted_delivery_readiness import EVALUATOR_VERSION

logger = logging.getLogger(__name__)
MAX_CARDS = 5000
MAX_HISTORY = 10000
MAX_FACTS = 20000
FINGERPRINT_VERSION = 'delivery-readiness-snapshot/v2'


@dataclass(frozen=True, slots=True)
class ReadinessSourceDelta:
    """Last invalidation observed in the same SQLite snapshot as its revision."""

    source_revision: int
    kind: str
    card_id: str | None


class CommunityReadinessClock:
    def now(self):
        return datetime.now(timezone.utc)


@contextmanager
def _count_queries(connection, counts):
    # Observe this read's connection, including queries issued by native ports.
    # Neither SQL text nor parameters are retained or logged.
    def count(*_args):
        counts['queries'] += 1
    event.listen(connection.sync_connection, 'before_cursor_execute', count)
    try:
        yield
    finally:
        event.remove(connection.sync_connection, 'before_cursor_execute', count)


@lru_cache(maxsize=128)
def _dataclass_field_names(model):
    return tuple(field.name for field in fields(model))


def _json_value(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: json.dumps(item, default=_json_value, sort_keys=True))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json')
    if is_dataclass(value) and not isinstance(value, type):
        # The JSON encoder visits nested dataclasses itself. Avoid recursively
        # copying the entire facts graph before encoding the same values.
        return {name: getattr(value, name) for name in _dataclass_field_names(type(value))}
    raise TypeError(f'Unsupported readiness identity type: {type(value).__name__}')


def _digest(value):
    return hashlib.sha256(json.dumps(value, default=_json_value, sort_keys=True,
        ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _resolve_readiness_policy(*, actor, board_id, realm_id, owner_id, share,
                              agent_flags, preset_id, presets, board_overrides):
    permissions = resolve_agent_permission_facts(
        agent_flags=agent_flags, preset_id=preset_id, presets=presets,
        board_overrides=board_overrides)
    spec_write_access = board_membership_allows_read(
        owner_id=owner_id, actor_id=actor.actor_id,
        share_permission=share,
        verified_agent_board_access=actor.actor_kind == 'agent',
        allowed_share_permissions={'editor', 'admin'})
    access = resolve_readiness_access(actor=actor, board_id=board_id,
        realm_id=realm_id, permissions=permissions,
        spec_write_access=spec_write_access)
    access.require('code_traceability.evidence.read')
    access.require('spec.entity.read')
    return access


def _policy_fact_key(value):
    """Canonicalize freshly queried policy facts, never an actor's claims."""
    return json.dumps(value, default=_json_value, sort_keys=True,
        ensure_ascii=False, separators=(',', ':'), allow_nan=False)


@lru_cache(maxsize=256)
def _resolve_persisted_policy_facts(actor, board_id, realm_id, owner_id, share,
                                    preset_id, agent_flags_key, presets_key,
                                    board_overrides_key):
    # Only the pure policy resolution is reused. Every read still queries Board,
    # membership, Spec/Card and current policy facts before and after projection.
    preset_rows = json.loads(presets_key)
    return _resolve_readiness_policy(actor=actor, board_id=board_id,
        realm_id=realm_id, owner_id=owner_id, share=share,
        agent_flags=json.loads(agent_flags_key), preset_id=preset_id,
        presets=tuple(PermissionPresetLineageNode(row['id'], row['flags'],
            row['base_preset_id']) for row in preset_rows),
        board_overrides=json.loads(board_overrides_key))


def _resolve_persisted_policy(*, actor, board_id, realm_id, owner_id, share,
                              agent_flags, preset_id, presets, board_overrides):
    return _resolve_persisted_policy_facts(actor, board_id, realm_id, owner_id,
        share, preset_id, _policy_fact_key(agent_flags),
        _policy_fact_key(tuple({'id': p.id, 'flags': p.flags,
            'base_preset_id': p.base_preset_id} for p in presets)),
        _policy_fact_key(board_overrides))


class _BatchFacts(CommunityDeliveryEvidenceStore):
    """Only storage access is replaced; admission/selection rules are inherited."""
    def __init__(self, session, rows, *, inventory=None, cached_plan=None):
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
        self.plan = None
        self.cached_plan = cached_plan

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
            # Readiness paginates the complete detail after evaluation. The
            # legacy summary cap must not discard checkpoint identities here.
            self.proofs[key] = await super()._execution_proof(execution_id, scope, card, progress_limit=None)
        return self.proofs[key]

    async def _execution_plan(self, spec):
        execution_contract(spec)
        if self.cached_plan is not None:
            self.plan, self.names, self.obligations, self.by_card = self.cached_plan
            return self.plan
        fields = ('id', 'board_id', 'spec_id', 'card_type', 'status', 'archived',
                  'test_scenario_ids', 'title', 'description', 'details')
        plan = self.inventory.execution_plan(spec=spec,
            cards=[{field: getattr(c, field, None) for field in fields} for c in self.rows[Card]],
            admitted_methods=supported_test_verification_methods())
        self.plan = plan
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

    async def snapshot(self, *, phase_metrics=None):
        spec = self.spec
        scope = DeliveryScope(spec.board_id, spec.id, int(spec.edition))
        phase_started = perf_counter()
        plan = await self._execution_plan(spec)
        native = native_verification_population(spec)
        plan_ms = (perf_counter() - phase_started) * 1000
        if not plan.inventory.population_complete:
            return self._with_effective_context(DeliveryEvidenceSnapshot(scope, (), complete=False,
                facts_complete=False), plan, (), spec, native_verification=native)
        native_runs = defaultdict(list)
        for run in native.runs:
            native_runs[run.scenario_id].append(run)
        implementations, tests, rejections = [], [], []
        complete = facts_complete = True
        active_cards = [c for c in self.rows[Card] if not c.archived]
        phase_started = perf_counter()
        for card in active_cards:
            part = await self.load_card_snapshot(CardDeliveryScope(scope.board_id, card.id,
                scope.spec_id, scope.edition), plan=plan, records=self.card_records[card.id],
                native_verification=native, native_runs=native_runs)
            implementations.extend(part.implementations)
            tests.extend(part.tests)
            rejections.extend(part.rejected_records)
            complete = complete and part.complete is True
            facts_complete = facts_complete and part.facts_complete is True
        cards_ms = (perf_counter() - phase_started) * 1000
        phase_started = perf_counter()
        result = DeliveryEvidenceSnapshot(scope, self.obligations, tuple(implementations), tuple(tests),
            tuple(await self._rollup_waivers(scope)), complete=complete,
            rejected_records=tuple(rejections), facts_complete=facts_complete)
        result = self._with_effective_context(result, plan, self.rows[CardRecord], spec, native_verification=native)
        context_ms = (perf_counter() - phase_started) * 1000
        phase_started = perf_counter()
        if result.effective_context and any(r.decision_plan and r.decision_plan.complete
                and r.decision_plan.verification.inspection for r in result.effective_context.inventory.rows):
            _, states, _, _, _ = await CommunityDecisionReviews(self).material(spec, result,
                sources=(self.rows[Board][0], self.rows[Record], self.rows[SpecHistory], active_cards))
            result = replace(result, effective_context=replace(result.effective_context, decision_inspections=states))
        if phase_metrics is not None:
            phase_metrics.update({
                'readiness_materialize_facts_plan_ms': plan_ms,
                'readiness_materialize_facts_cards_ms': cards_ms,
                'readiness_materialize_facts_context_ms': context_ms,
                'readiness_materialize_facts_decisions_ms':
                    (perf_counter() - phase_started) * 1000,
            })
        return result


class CommunityDeliveryReadinessReader:
    def __init__(self, engine, *, clock, inventory=None):
        if engine.url.get_backend_name() != 'sqlite':
            raise ValueError('community_readiness_requires_sqlite')
        self.engine, self.clock, self.inventory = engine, clock, inventory
        self._projection_plan_cache = OrderedDict()

    async def _rows(self, session, statement, limit, counts):
        started = perf_counter() if 'query_ms' in counts else None
        rows = tuple((await session.scalars(statement.limit(limit + 1))).all())
        if started is not None:
            entity = statement.column_descriptions[0].get('entity')
            name = entity.__tablename__ if entity is not None else 'unknown'
            counts['query_ms'][name] = (perf_counter() - started) * 1000
        if len(rows) > limit:
            raise ReadinessReadError('readiness_limit_exceeded', native_code='readiness_population_limit')
        counts['facts'] += len(rows)
        return rows

    async def _projection_record_rows(self, session, statement, limit, counts):
        """Keep the projection's large ledger batch out of the ORM identity map."""
        started = perf_counter()
        cpu_started = process_time()
        result = await session.execute(statement.limit(limit + 1))
        execute_ms = (perf_counter() - started) * 1000
        fetch_started = perf_counter()
        values = result.all()
        fetch_ms = (perf_counter() - fetch_started) * 1000
        if 'query_ms' in counts:
            counts['query_ms'][CardRecord.__tablename__] = (perf_counter() - started) * 1000
        if len(values) > limit:
            raise ReadinessReadError('readiness_limit_exceeded', native_code='readiness_population_limit')
        counts['facts'] += len(values)
        wrap_started = perf_counter()
        rows = tuple(SimpleNamespace(**row._mapping) for row in values)
        if 'query_ms' in counts:
            counts['record_query_detail_ms'] = {
                'execute': execute_ms,
                'fetch': fetch_ms,
                'wrap': (perf_counter() - wrap_started) * 1000,
                'cpu': (process_time() - cpu_started) * 1000,
            }
        return rows

    async def _access(self, session, actor, board_id, counts):
        """Load native identity/ACL/preset facts before reading delivery content."""
        if (not isinstance(actor, ReadinessActor) or not actor.actor_id
            or actor.actor_kind not in {'human', 'user', 'agent'}
            or actor.board_id is not None and actor.board_id != board_id):
            raise ReadinessReadError('readiness_scope_mismatch')
        boards = await self._rows(session, select(Board).where(Board.id == board_id), 1, counts)
        realm_id = actor.realm_id or LOCAL_REALM_ID
        if not boards or (boards[0].realm_id or LOCAL_REALM_ID) != realm_id:
            raise ReadinessReadError('readiness_scope_mismatch')
        board = boards[0]
        binding, share = None, None
        if actor.actor_kind == 'agent':
            agents = await self._rows(session, select(Agent).where(Agent.id == actor.actor_id,
                Agent.is_active.is_(True)), 1, counts)
            bindings = await self._rows(session, select(AgentBoard).where(
                AgentBoard.agent_id == actor.actor_id, AgentBoard.board_id == board_id), 1, counts)
            if not agents or not bindings:
                raise ReadinessReadError('readiness_scope_mismatch')
            agent, binding = agents[0], bindings[0]
            member = board_membership_allows_read(owner_id=board.owner_id, actor_id=actor.actor_id,
                verified_agent_board_access=True)
        else:
            shares = await self._rows(session, select(BoardShare).where(
                BoardShare.board_id == board_id, BoardShare.user_id == actor.actor_id,
                BoardShare.realm_id == realm_id), 1, counts)
            share = shares[0].permission if shares else None
            member = board_membership_allows_read(owner_id=board.owner_id, actor_id=actor.actor_id,
                share_permission=share)
            if not member:
                raise ReadinessReadError('readiness_scope_mismatch')
            # Match the existing REST resolve_user_permissions lookup exactly:
            # user policy follows its first created Agent, otherwise defaults.
            agent = (await session.scalars(select(Agent).where(Agent.created_by == actor.actor_id).limit(1))).first()
            counts['facts'] += int(agent is not None)
            if agent is not None:
                bindings = await self._rows(session, select(AgentBoard).where(
                    AgentBoard.agent_id == agent.id, AgentBoard.board_id == board_id), 1, counts)
                binding = bindings[0] if bindings else None
        if not member:
            raise ReadinessReadError('readiness_scope_mismatch')
        presets = await self._rows(session, select(PermissionPreset).order_by(PermissionPreset.id), 5000, counts
            ) if agent is not None and agent.preset_id else ()
        return _resolve_readiness_policy(actor=actor, board_id=board_id,
            realm_id=realm_id, owner_id=board.owner_id, share=share,
            agent_flags=agent.permission_flags if agent is not None else None,
            preset_id=agent.preset_id if agent is not None else None,
            presets=tuple(PermissionPresetLineageNode(p.id, p.flags, p.base_preset_id)
                for p in presets),
            board_overrides=binding.permission_overrides if binding is not None else None)

    async def _access_persisted(self, session, actor, board_id):
        """Resolve current authority with only the columns used by the policy.

        The source evaluator keeps its existing batch reader. This path is
        called before and after an indexed generation read, so loading full
        Board/Agent ORM rows twice would inflate the hot request.
        """
        if (not isinstance(actor, ReadinessActor) or not actor.actor_id
            or actor.actor_kind not in {'human', 'user', 'agent'}
            or actor.board_id is not None and actor.board_id != board_id):
            raise ReadinessReadError('readiness_scope_mismatch')
        realm_id = actor.realm_id or LOCAL_REALM_ID
        identity_started = perf_counter()
        if actor.actor_kind == 'agent':
            identity = (await session.execute(select(
                Board.owner_id, Board.realm_id, Agent.id.label('agent_id'),
                Agent.permission_flags, Agent.preset_id,
                AgentBoard.permission_overrides,
            ).select_from(Board).join(Agent, and_(
                Agent.id == actor.actor_id, Agent.is_active.is_(True)))
                .join(AgentBoard, and_(AgentBoard.agent_id == Agent.id,
                    AgentBoard.board_id == Board.id))
                .where(Board.id == board_id))).first()
            share = None
            member = identity is not None and board_membership_allows_read(
                owner_id=identity.owner_id, actor_id=actor.actor_id,
                verified_agent_board_access=True)
        else:
            first_agent_id = select(Agent.id).where(
                Agent.created_by == actor.actor_id).limit(1).scalar_subquery()
            identity = (await session.execute(select(
                Board.owner_id, Board.realm_id, BoardShare.permission,
                Agent.id.label('agent_id'), Agent.permission_flags,
                Agent.preset_id, AgentBoard.permission_overrides,
            ).select_from(Board).outerjoin(BoardShare, and_(
                BoardShare.board_id == Board.id,
                BoardShare.user_id == actor.actor_id,
                BoardShare.realm_id == realm_id))
                .outerjoin(Agent, Agent.id == first_agent_id)
                .outerjoin(AgentBoard, and_(AgentBoard.agent_id == Agent.id,
                    AgentBoard.board_id == Board.id))
                .where(Board.id == board_id))).first()
            share = identity.permission if identity is not None else None
            member = identity is not None and board_membership_allows_read(
                owner_id=identity.owner_id, actor_id=actor.actor_id,
                share_permission=share)
        identity_done = perf_counter()
        if (identity is None or (identity.realm_id or LOCAL_REALM_ID) != realm_id
            or not member):
            raise ReadinessReadError('readiness_scope_mismatch')
        presets = ()
        if identity.agent_id is not None and identity.preset_id:
            presets = tuple((await session.execute(select(
                PermissionPreset.id, PermissionPreset.flags,
                PermissionPreset.base_preset_id,
            ).order_by(PermissionPreset.id).limit(5001))).all())
            if len(presets) > 5000:
                raise ReadinessReadError('readiness_limit_exceeded',
                    native_code='readiness_population_limit')
        presets_done = perf_counter()
        access = _resolve_persisted_policy(actor=actor, board_id=board_id,
            realm_id=realm_id, owner_id=identity.owner_id, share=share,
            agent_flags=identity.permission_flags if identity.agent_id is not None else None,
            preset_id=identity.preset_id if identity.agent_id is not None else None,
            presets=tuple(PermissionPresetLineageNode(p.id, p.flags, p.base_preset_id)
                for p in presets),
            board_overrides=identity.permission_overrides if identity.agent_id is not None else None)
        logger.debug('readiness.access_phases', extra={
            'readiness_identity_ms': (identity_done - identity_started) * 1000,
            'readiness_presets_ms': (presets_done - identity_done) * 1000,
            'readiness_policy_ms': (perf_counter() - presets_done) * 1000,
        })
        return access

    async def _load_transaction(self, session, board_id, spec_id, card_id, counts,
                                *, defer_facts=False):
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
        record_filter = (CardRecord.board_id == board_id,
            CardRecord.spec_id == spec_id, CardRecord.spec_edition == spec.edition,
            CardRecord.card_id.in_(cards))
        record_order = (CardRecord.created_at, CardRecord.id)
        if defer_facts:
            rows[CardRecord] = await self._projection_record_rows(session,
                select(*CardRecord.__table__.columns).where(*record_filter)
                .order_by(*record_order), MAX_FACTS, counts)
        else:
            rows[CardRecord] = await self._rows(session,
                select(CardRecord).where(*record_filter).order_by(*record_order),
                MAX_FACTS, counts)
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
        if defer_facts:
            return rows
        return await self._snapshot_from_rows(rows)

    async def _snapshot_from_rows(self, rows, *, phase_metrics=None,
                                  cached_plan=None, return_plan=False):
        """Build facts from an immutable row set; no database call is allowed."""
        spec = rows[Spec][0]
        phase_started = perf_counter()
        facts_phases = {}
        batch = _BatchFacts(None, rows, inventory=self.inventory, cached_plan=cached_plan)
        facts = await batch.snapshot(
            phase_metrics=facts_phases)
        facts_ms = (perf_counter() - phase_started) * 1000
        phase_started = perf_counter()
        # Hash actual content as well as identities. No mutable ORM objects,
        # raw ledger payloads, credentials or full Board configuration escape.
        identity_values = {model.__tablename__: tuple((r.id,
            {c.key: getattr(r, c.key) for c in model.__table__.columns}) for r in values)
            for model, values in rows.items()}
        identity_rows_ms = (perf_counter() - phase_started) * 1000
        def calculate_identity():
            # One canonical serialization retains every column while avoiding
            # thousands of separate JSON encodes and intermediate row hashes.
            identities = {name: sorted(values, key=lambda row: row[0])
                for name, values in identity_values.items()}
            return _digest({'contract_version': 'delivery-readiness/v1',
                'fingerprint_version': FINGERPRINT_VERSION,
                'evaluator_version': EVALUATOR_VERSION,
                'inputs': identities, 'effective_facts': facts})
        phase_started = perf_counter()
        identity = calculate_identity()
        identity_digest_ms = (perf_counter() - phase_started) * 1000
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
        if phase_metrics is not None:
            phase_metrics.update({
                'readiness_materialize_facts_ms': facts_ms,
                'readiness_materialize_identity_rows_ms': identity_rows_ms,
                'readiness_materialize_identity_digest_ms': identity_digest_ms,
                'readiness_materialize_facts_plan_cache_hit': float(cached_plan is not None),
                **facts_phases,
            })
        result = ReadinessSnapshot(facts, identity, evaluated_at.astimezone(timezone.utc), int(spec.version), spec.status.value,
            tuple(ReadinessCard(c.id, c.card_type, c.status, c.policy_version,
                tuple(c.test_scenario_ids) if c.test_scenario_ids is not None else None, c.assignee_id)
                for c in rows[Card] if not c.archived), scenarios,
            delivery_gate_mode=resolve_delivery_gate_policy(rows[Board][0].settings),
            skip_delivery_evidence=spec.skip_delivery_evidence is True)
        if return_plan:
            return result, (batch.plan, batch.names, batch.obligations, batch.by_card)
        return result

    async def load(self, *, board_id, spec_id, card_id=None, actor=None):
        # Actor-less reads are internal fact-loader compatibility only. Public
        # composition requires the access facts produced by an authorized read.
        started = perf_counter()
        counts = {'queries': 0, 'facts': 0}
        try:
            for attempt in range(2):
                async with self.engine.connect() as connection:
                    with _count_queries(connection, counts):
                        before = (await connection.exec_driver_sql('PRAGMA data_version')).scalar_one()
                        await connection.exec_driver_sql('BEGIN')
                        try:
                            async with AsyncSession(bind=connection, autoflush=False, expire_on_commit=False) as session:
                                access = await self._access(session, actor, board_id, counts) if actor is not None else None
                                if access is not None:
                                    # Native persistence uses the realm verified by Board/actor access.
                                    session.info['realm_scope'] = (RealmScope.local() if access.realm_id == LOCAL_REALM_ID
                                        else RealmScope.tenant(access.realm_id))
                                if card_id is not None and access is not None:
                                    access.require('card.entity.read')
                                try:
                                    result = await self._load_transaction(session, board_id, spec_id, card_id, counts)
                                except ReadinessReadError as exc:
                                    if exc.code != 'readiness_limit_exceeded':
                                        raise
                                    result = await self._capacity_snapshot(session, board_id, spec_id, card_id, counts, exc)
                                if access is not None:
                                    result = replace(result, access=access)
                                    native = await build_application_service_catalog(session).delivery_readiness_lifecycle.evaluate(
                                        result, card_id=card_id)
                                    result = replace(result, native_lifecycle=native,
                                        snapshot_id=_digest({'facts': result.snapshot_id, 'access': access,
                                            'native_lifecycle': native}))
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

    async def load_for_projection(self, *, board_id, spec_id, blocking_execution,
                                  phase_metrics=None, include_source_delta=False):
        """Capture one source revision and immutable facts without actor state.

        The worker releases this connection before calling the CPU evaluator.
        Later writes may advance the source; publication fences that revision
        instead of retrying an unrelated global ``PRAGMA data_version`` change.
        """
        from okto_pulse.community.adapters.sqlalchemy_models import (
            ReadinessScopeRow, ReadinessSourceDeltaRow,
        )

        counts = {'queries': 0, 'facts': 0, 'query_ms': {}}
        started = perf_counter()
        async with self.engine.connect() as connection:
            checkout_ms = (perf_counter() - started) * 1000
            phase_started = perf_counter()
            await connection.exec_driver_sql('BEGIN')
            begin_ms = (perf_counter() - phase_started) * 1000
            try:
                phase_started = perf_counter()
                async with AsyncSession(bind=connection, autoflush=False, expire_on_commit=False) as session:
                    scope = (await session.scalars(select(ReadinessScopeRow).where(
                        ReadinessScopeRow.board_id == board_id,
                        ReadinessScopeRow.spec_id == spec_id,
                        ReadinessScopeRow.tombstone.is_(False)).order_by(
                        ReadinessScopeRow.edition.desc()).limit(1))).first()
                    if scope is None:
                        raise ReadinessReadError('readiness_scope_mismatch')
                    delta_row = (await session.get(ReadinessSourceDeltaRow, scope.id)
                        if include_source_delta else None)
                    delta = (ReadinessSourceDelta(delta_row.source_revision,
                        delta_row.kind, delta_row.card_id) if delta_row is not None else None)
                    scope_ms = (perf_counter() - phase_started) * 1000
                    phase_started = perf_counter()
                    try:
                        rows = await self._load_transaction(
                            session, board_id, spec_id, None, counts, defer_facts=True)
                    except ReadinessReadError as exc:
                        if exc.code != 'readiness_limit_exceeded':
                            raise
                        rows = None
                        overflow = await self._capacity_snapshot(session, board_id, spec_id, None, counts, exc)
                    captured_revision = scope.source_revision
                    edition = scope.edition
                    source_ms = (perf_counter() - phase_started) * 1000
            finally:
                phase_started = perf_counter()
                await connection.rollback()
                rollback_ms = (perf_counter() - phase_started) * 1000
        read_rows_ms = (perf_counter() - started) * 1000 - checkout_ms
        materialize_started = perf_counter()
        materialize_phases = {}
        if rows is None:
            snapshot = overflow
        else:
            import asyncio
            cache_key = (board_id, spec_id, edition)
            previous = self._projection_plan_cache.get(cache_key)
            cached_plan = (previous[1] if previous is not None
                and previous[0] == captured_revision - 1
                and delta is not None and delta.source_revision == captured_revision
                and delta.kind == 'card_progress_insert' and delta.card_id else None)
            snapshot, plan = await blocking_execution.run(
                lambda: asyncio.run(self._snapshot_from_rows(rows,
                    phase_metrics=materialize_phases, cached_plan=cached_plan,
                    return_plan=True)))
            self._projection_plan_cache[cache_key] = (captured_revision, plan)
            self._projection_plan_cache.move_to_end(cache_key)
            if len(self._projection_plan_cache) > 16:
                self._projection_plan_cache.popitem(last=False)
        metrics = {
            'readiness_checkout_ms': checkout_ms,
            'readiness_begin_ms': begin_ms,
            'readiness_scope_ms': scope_ms,
            'readiness_source_ms': source_ms,
            'readiness_rollback_ms': rollback_ms,
            'readiness_query_ms': counts['query_ms'],
            'readiness_record_query_detail_ms': counts.get('record_query_detail_ms', {}),
            'readiness_read_rows_ms': read_rows_ms,
            'readiness_materialize_ms': (perf_counter() - materialize_started) * 1000,
            **materialize_phases,
        }
        logger.debug('readiness.projection_snapshot_phases', extra=metrics)
        if phase_metrics is not None:
            phase_metrics.update(metrics)
        if snapshot.facts.scope.edition != edition:
            raise ReadinessReadError('readiness_snapshot_changed')
        if include_source_delta:
            return snapshot, captured_revision, delta
        return snapshot, captured_revision

    async def load_persisted(self, *, board_id, spec_id, actor, card_id=None,
                             unit_limit=0, cursor=None, include_diagnostics=False):
        """Authorize current actor around a bounded projection read.

        No proof facts, historical versions or diagnostics are loaded. A commit
        during the read triggers fresh authorization; changed ACL/Spec/Card
        facts are retried once, then fail closed.
        """
        from okto_pulse.community.adapters.sqlalchemy_persisted_readiness import (
            CommunityPersistedReadinessStore,
        )

        async def authorize(connection, *, check_source_revision=False):
            await connection.exec_driver_sql('BEGIN')
            try:
                async with AsyncSession(bind=connection, autoflush=False) as session:
                    access = await self._access_persisted(session, actor, board_id)
                    # The unscoped projection contains Card identities and
                    # metrics as well as the Spec summary. Authorize that
                    # population before reading or returning any row.
                    access.require('card.entity.read')
                    spec = (await session.execute(select(Spec.edition, Spec.version).where(
                        Spec.id == spec_id, Spec.board_id == board_id,
                        Spec.archived.is_(False)))).first()
                    if spec is None:
                        raise ReadinessReadError('readiness_scope_mismatch')
                    card_policy_version = None
                    if card_id is not None:
                        card_policy_version = (await session.execute(select(Card.policy_version).where(
                            Card.id == card_id,
                            Card.board_id == board_id, Card.spec_id == spec_id,
                            Card.archived.is_(False)))).scalar_one_or_none()
                        if card_policy_version is None:
                            raise ReadinessReadError('readiness_scope_mismatch')
                    source_revision = None
                    if check_source_revision:
                        source_revision = (await connection.exec_driver_sql(
                            'SELECT source_revision FROM readiness_scope WHERE board_id=? '
                            'AND spec_id=? AND edition=? AND tombstone=0 LIMIT 1',
                            (board_id, spec_id, spec.edition))).scalar_one_or_none()
                    return access, spec.edition, spec.version, card_policy_version, source_revision
            finally:
                await connection.rollback()

        store = CommunityPersistedReadinessStore(self.engine)
        async def read_projected(before, authority_connection):
            generation_id = None
            unit_offset = 0
            if cursor is not None:
                if not unit_limit and not include_diagnostics:
                    raise ReadinessReadError('readiness_query_invalid')
                from okto_pulse.core import get_settings
                from .delivery_readiness_cursor import CommunityReadinessCursorCodec
                codec = CommunityReadinessCursorCodec.from_settings(get_settings())
                if include_diagnostics:
                    generation_id = codec.peek_detail_generation(cursor)
                else:
                    generation_id, unit_offset = codec.decode_projection(cursor,
                        actor_id=actor.actor_id, board_id=board_id, spec_id=spec_id,
                        edition=before[1], card_id=card_id, limit=unit_limit)
            projection = await store.read(board_id=board_id, spec_id=spec_id,
                edition=before[1], card_id=card_id, unit_limit=unit_limit,
                unit_offset=unit_offset, generation_id_override=generation_id,
                include_diagnostics=include_diagnostics, connection=authority_connection)
            return projection, unit_offset

        for _ in range(2):
            started = perf_counter()
            async with self.engine.connect() as authority_connection:
                checkout_ms = (perf_counter() - started) * 1000
                # data_version is comparable only on the same SQLite connection.
                # Release each read transaction before beginning the next one;
                # reuse the connection so a concurrent commit remains detectable.
                observed_version = (await authority_connection.exec_driver_sql(
                    'PRAGMA data_version')).scalar_one()
                before = await authorize(authority_connection)
                authorized_at = perf_counter()
                projection, unit_offset = await read_projected(before, authority_connection)
                projected_at = perf_counter()
                current_version = (await authority_connection.exec_driver_sql(
                    'PRAGMA data_version')).scalar_one()
                if current_version == observed_version:
                    after = before
                else:
                    after = await authorize(authority_connection, check_source_revision=True)
                reauthorized_at = perf_counter()
            if before[:4] == after[:4]:
                if current_version != observed_version:
                    latest_revision = after[4]
                    # The authorization and target revision are one SQLite
                    # snapshot. A source edit may follow the projection read;
                    # the immutable generation is then still safe to show as
                    # updating, with its captured revision kept intact.
                    if (projection is None) != (latest_revision is None):
                        continue
                    if projection is not None and latest_revision != projection.source_revision:
                        projection = replace(projection, source_revision=latest_revision,
                            freshness=('updating' if projection.freshness == 'current'
                                else projection.freshness))
                logger.debug('readiness.persisted_read_phases', extra={
                    'readiness_checkout_ms': checkout_ms,
                    'readiness_auth_before_ms': (authorized_at - started) * 1000,
                    'readiness_projection_ms': (projected_at - authorized_at) * 1000,
                    'readiness_auth_after_ms': (reauthorized_at - projected_at) * 1000,
                })
                has_more = bool(projection and unit_limit
                    and unit_offset + len(projection.units) < projection.unit_total)
                next_cursor = None
                if has_more:
                    from okto_pulse.core import get_settings
                    from .delivery_readiness_cursor import CommunityReadinessCursorCodec
                    next_cursor = CommunityReadinessCursorCodec.from_settings(
                        get_settings()).encode_projection(actor_id=actor.actor_id,
                        board_id=board_id, spec_id=spec_id, edition=before[1],
                        card_id=card_id, limit=unit_limit,
                        generation_id=projection.generation_id,
                        offset=unit_offset + len(projection.units))
                result = (projection, before[1], before[2], {'offset': unit_offset,
                    'limit': unit_limit, 'total': projection.unit_total if projection else 0,
                    'has_more': has_more, 'next_cursor': next_cursor})
                return (*result, after[0]) if include_diagnostics else result
        raise ReadinessReadError('readiness_snapshot_changed')

    async def requeue_terminal(self, *, board_id, spec_id, actor,
                               edition, expected_revision, idempotency_key, reason):
        from .sqlalchemy_persisted_readiness import CommunityPersistedReadinessStore

        counts = {'queries': 0, 'facts': 0}
        async with AsyncSession(self.engine, autoflush=False) as session:
            access = await self._access(session, actor, board_id, counts)
            access.require('readiness.requeue')
            if actor.actor_kind in {'human', 'user'}:
                board = await session.get(Board, board_id)
                if board is None:
                    raise ReadinessReadError('readiness_scope_mismatch')
                if board.owner_id != actor.actor_id:
                    share = (await session.scalars(select(BoardShare.permission).where(
                        BoardShare.board_id == board_id,
                        BoardShare.user_id == actor.actor_id,
                        BoardShare.realm_id == (actor.realm_id or LOCAL_REALM_ID)))).first()
                    if share != 'admin':
                        raise ReadinessAccessDenied('readiness.requeue')
            spec = await session.get(Spec, spec_id)
            if spec is None or spec.board_id != board_id or spec.archived:
                raise ReadinessReadError('readiness_scope_mismatch')
            if spec.edition != edition:
                raise ReadinessReadError('readiness_requeue_conflict')
            scope_id = f'{spec_id}:{spec.edition}'
        return await CommunityPersistedReadinessStore(self.engine).requeue_terminal(
            scope_id=scope_id, board_id=board_id,
            actor_id=actor.actor_id, actor_kind=actor.actor_kind,
            expected_revision=expected_revision, idempotency_key=idempotency_key,
            reason=reason)

    async def _capacity_snapshot(self, session, board_id, spec_id, card_id, counts, error):
        """Keep an authorized scope header, never the prefix of overflowing facts.

        Authority has already been resolved by load. Validate a requested Card
        separately: it may be beyond the capped batch or belong to another Spec.
        Permission-population overflow is deliberately outside this catch path.
        """
        from okto_pulse.core.domain.delivery_evidence import DeliveryEvidenceSnapshot, DeliveryScope

        specs = await self._rows(session, select(Spec).where(Spec.id == spec_id,
            Spec.board_id == board_id, Spec.archived.is_(False)), 1, counts)
        boards = await self._rows(session, select(Board).where(Board.id == board_id), 1, counts)
        if not specs or not boards:
            raise ReadinessReadError('readiness_scope_mismatch')
        spec = specs[0]
        cards = ()
        if card_id is not None:
            selected = await self._rows(session, select(Card).where(Card.id == card_id,
                Card.board_id == board_id, Card.spec_id == spec_id, Card.archived.is_(False)), 1, counts)
            if not selected:
                raise ReadinessReadError('readiness_scope_mismatch')
            card = selected[0]
            # Scenario population was not loaded; do not present assignments as
            # a complete evaluable universe even when the header contains IDs.
            cards = (ReadinessCard(card.id, card.card_type, card.status, card.policy_version, None, card.assignee_id),)
        facts = DeliveryEvidenceSnapshot(DeliveryScope(board_id, spec_id, spec.edition), (), facts_complete=False)
        codes = tuple(dict.fromkeys((error.code, error.native_code or 'readiness_population_limit')))
        identity = _digest({'contract_version': 'delivery-readiness/v1', 'fingerprint_version': FINGERPRINT_VERSION,
            'evaluator_version': EVALUATOR_VERSION, 'scope': facts.scope, 'spec_version': spec.version,
            'spec_status': spec.status, 'cards': cards, 'unavailable_reason_codes': codes,
            'delivery_gate_mode': resolve_delivery_gate_policy(boards[0].settings),
            'skip_delivery_evidence': spec.skip_delivery_evidence is True})
        evaluated_at = self.clock.now()
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ReadinessReadError('readiness_assessment_unavailable', native_code='readiness_clock_timezone_required')
        return ReadinessSnapshot(facts, identity, evaluated_at.astimezone(timezone.utc), int(spec.version),
            spec.status.value, cards, unavailable_reason_codes=codes,
            delivery_gate_mode=resolve_delivery_gate_policy(boards[0].settings),
            skip_delivery_evidence=spec.skip_delivery_evidence is True)
