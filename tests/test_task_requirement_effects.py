from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import update, select, func
from okto_pulse.community.adapters.sqlalchemy_models import Card, TaskRequirementDecisionRow
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.task_requirement_decision import GetTaskRequirementOptionsUseCase, RecordTaskRequirementDecisionUseCase
from okto_pulse.core.models.task_requirement_decision import TaskRequirementOptionsQuery, TaskRequirementDecisionCommand
from okto_pulse.core.domain.delivery_evidence import DeliveryScope, evaluate_delivery_coverage
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, assess_test_delivery, assess_delivery_gate, UnitState
import test_execution_contract_integration as contract

ledger = contract.ledger


async def test_current_waiver_changes_applicability_without_forging_proof_or_lifecycle_and_restore_reblocks(ledger, tmp_path, monkeypatch):
    session, store, _ = ledger
    await contract.adopt_fixture(session, tmp_path, monkeypatch)
    await session.execute(update(Card).where(Card.id == 'task').values(status='in_progress'))
    await session.commit()
    board_id, spec_id = contract.signed.BOARD_ID, contract.signed.SPEC_ID
    actor = ActorContext(actor_id='owner', actor_kind='user', source='system', board_id=board_id,
        permissions=['spec.entity.read', 'card.entity.read', 'code_traceability.evidence.read',
                     'code_traceability.waiver.create', 'code_traceability.waiver.clear'])
    uow = SimpleNamespace(services=SimpleNamespace(delivery_evidence=store), commit=AsyncMock(side_effect=session.commit))
    query = TaskRequirementOptionsQuery(board_id=board_id, spec_id=spec_id, card_ids=['task'])
    scope = DeliveryScope(board_id, spec_id, 1)
    before = assess_obligation_delivery(await store.load_snapshot(scope))
    assert not assess_delivery_gate(before).allowed
    preview = await GetTaskRequirementOptionsUseCase().execute(query, actor=actor, uow=uow)
    request = TaskRequirementDecisionCommand(board_id=board_id, spec_id=spec_id,
        contract_version='task-requirement-decision/v1', card_ids=['task'], decision='waived',
        basis=preview['basis'], note=None, idempotency_key='waive')
    await RecordTaskRequirementDecisionUseCase().execute(request, actor=actor, uow=uow)
    snapshot = await store.load_snapshot(scope)
    assessment = assess_obligation_delivery(snapshot)
    assert assessment.completeness.scope_resolved
    assert all(row.implementation.state == row.verification.state == UnitState.NOT_APPLICABLE for row in assessment.obligations)
    assert all(row.implementation.proof_satisfied is not True and row.verification.proof_satisfied is not True for row in assessment.obligations)
    assert assess_delivery_gate(assessment).allowed
    assert not snapshot.implementations and not snapshot.tests
    assert (await session.get(Card, 'task')).status == 'in_progress'
    assert (await store.task_requirement_states(board_id, spec_id))['task']['decision'] == 'waived'
    legacy = evaluate_delivery_coverage(snapshot)
    assert legacy.allowed and all(row.not_required and not row.implementation_satisfied for row in legacy.rows)
    preview = await GetTaskRequirementOptionsUseCase().execute(query, actor=actor, uow=uow)
    request = request.model_copy(update={'basis': preview['basis'], 'decision': 'required', 'idempotency_key': 'restore'})
    await RecordTaskRequirementDecisionUseCase().execute(request, actor=actor, uow=uow)
    restored = await store.load_snapshot(scope)
    assert not assess_delivery_gate(assess_obligation_delivery(restored)).allowed
    assert not restored.implementations and not restored.tests
    assert await session.scalar(select(func.count()).select_from(TaskRequirementDecisionRow)) == 2


async def test_waiving_one_test_card_does_not_remove_required_spec_verification(ledger, tmp_path, monkeypatch):
    session, store, _ = ledger
    await contract.adopt_fixture(session, tmp_path, monkeypatch)
    await session.execute(update(Card).where(Card.id == 'test').values(status='in_progress',
        test_scenario_ids=[contract.signed.SCENARIO_ID]))
    await session.commit()
    board_id, spec_id = contract.signed.BOARD_ID, contract.signed.SPEC_ID
    actor = ActorContext(actor_id='owner', actor_kind='human', source='system', board_id=board_id,
        permissions=['spec.entity.read', 'card.entity.read', 'code_traceability.evidence.read',
                     'code_traceability.waiver.create'])
    unit = SimpleNamespace(services=SimpleNamespace(delivery_evidence=store), commit=AsyncMock(side_effect=session.commit))
    query = TaskRequirementOptionsQuery(board_id=board_id, spec_id=spec_id, card_ids=['test'])
    preview = await GetTaskRequirementOptionsUseCase().execute(query, actor=actor, uow=unit)
    assert preview['effects']['test_scenarios_remain_governed']
    await RecordTaskRequirementDecisionUseCase().execute(TaskRequirementDecisionCommand(
        board_id=board_id, spec_id=spec_id, contract_version='task-requirement-decision/v1',
        card_ids=['test'], decision='waived', basis=preview['basis'], idempotency_key='test-waiver'),
        actor=actor, uow=unit)
    snapshot = await store.load_snapshot(DeliveryScope(board_id, spec_id, 1))
    test_scope = assess_test_delivery(snapshot, card_id='test', scenario_ids=(contract.signed.SCENARIO_ID,))
    assert test_scope.scenarios and all(row.verification.state == UnitState.NOT_APPLICABLE for row in test_scope.scenarios)
    assert all(row.verification.proof_satisfied is not True for row in test_scope.scenarios)
    assert assess_delivery_gate(test_scope).allowed
    global_scope = assess_obligation_delivery(snapshot)
    assert not assess_delivery_gate(global_scope).allowed
    assert any(row.verification.state != UnitState.NOT_APPLICABLE for row in global_scope.obligations)
    assert not snapshot.tests and (await session.get(Card, 'test')).status == 'in_progress'
