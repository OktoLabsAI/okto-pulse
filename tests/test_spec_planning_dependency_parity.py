"""UV17: predecessor completion gates execution, not planning approval."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog, Card, CardType, DomainEventRow, Spec, SpecHistory, SpecStatus,
)
from okto_pulse.community.adapters.sqlalchemy_spec_dependency import CommunitySqlAlchemySpecDependency
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.spec_planning import SpecPlanningManifest
from okto_pulse.core.mcp import server
from okto_pulse.core.services.main import SpecService
from okto_pulse.core.services.spec_dependency import SpecDependencyService
from test_spec_planning_lint_parity import (
    composition_client, db_factory, current_manifest, _validation_payload, _record_lint,
)
from test_spec_validation_lifecycle_gate import _new_spec
from test_spec_validation_native_gate import ACTOR, USER_ID, _decomposition_submit_data

__all__ = ['composition_client', 'db_factory']


@pytest.fixture
def verification_capability(tmp_path):
    from okto_pulse.community.adapters.sqlalchemy_architecture_persistence import CommunitySqlAlchemyArchitecturePersistence
    from okto_pulse.community.adapters.sqlalchemy_structured_spec import CommunitySqlAlchemyStructuredSpecStore
    from okto_pulse.core.ports.architecture_persistence import register_architecture_persistence_port
    from okto_pulse.core.ports.structured_spec import register_structured_spec_store
    from okto_pulse.community.adapters.test_evidence import (
        CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier,
    )
    from okto_pulse.core.ports.test_evidence import (
        register_test_evidence_write_verifier, resolve_test_evidence_write_verifier,
        reset_test_evidence_write_verifier_for_tests,
    )
    previous = resolve_test_evidence_write_verifier()
    register_architecture_persistence_port(CommunitySqlAlchemyArchitecturePersistence())
    register_structured_spec_store(CommunitySqlAlchemyStructuredSpecStore())
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(
        ledger=CommunityEvidenceLedger(evidence_root=tmp_path / 'evidence'),
    ))
    try:
        yield
    finally:
        if previous is None:
            reset_test_evidence_write_verifier_for_tests()
        else:
            register_test_evidence_write_verifier(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['rest', 'mcp'])
async def test_planning_accepts_pending_predecessor_but_start_waits_for_done(
    composition_client, db_factory, monkeypatch, surface, verification_capability,
):
    client, spec_id, _ = composition_client
    predecessor_id = 'planning-predecessor'
    async with db_factory() as db:
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            spec = await db.get(Spec, spec_id)
            board_id = spec.board_id
            spec.functional_requirements = [{
                'id': 'fr-mock', 'title': 'Expose mock status', 'text': 'Return the mock status.',
                'linked_task_ids': ['mock-task'],
                'verification': {'mode': 'explicit', 'required_profiles': ['functional']},
                'implementation_plan': {'contributions': [
                    {'card_id': 'mock-task', 'scope': 'whole_requirement'},
                ]},
            }]
            spec.acceptance_criteria = [{
                'id': 'ac-mock', 'text': 'A mock status request returns ready.',
                'verification_profile': 'functional', 'linked_task_ids': ['mock-task'],
                'requirement_links': [
                    {'requirement_type': 'functional_requirement', 'requirement_id': 'fr-mock'},
                    {'requirement_type': 'business_rule', 'requirement_id': 'br-mock'},
                ],
            }]
            spec.business_rules = [{
                'id': 'br-mock', 'title': 'Ready mock status', 'rule': 'A running mock reports ready.',
                'when': 'The running mock status is requested', 'then': 'Report ready',
                'linked_requirements': ['fr-mock'], 'linked_task_ids': ['mock-task'],
                'verification': {'mode': 'explicit', 'required_profiles': ['functional']},
                'implementation_plan': {'contributions': [
                    {'card_id': 'mock-task', 'scope': 'whole_requirement'},
                ]},
            }]
            spec.test_scenarios = [{
                'id': 'scenario-mock', 'title': 'Mock status returns ready',
                'given': 'A running mock', 'when': 'Status is requested', 'then': 'Ready is returned',
                'scenario_type': 'e2e', 'status': 'ready',
                'verification_method': 'automated_test', 'linked_criteria': ['ac-mock'],
                'linked_task_ids': ['mock-test'],
            }]
            db.add_all([
                Card(id='mock-task', board_id=board_id, spec_id=spec_id,
                     title='Implement mock status', created_by=USER_ID, card_type=CardType.NORMAL),
                Card(id='mock-test', board_id=board_id, spec_id=spec_id,
                     title='Verify mock status', created_by=USER_ID, card_type=CardType.TEST,
                     test_scenario_ids=['scenario-mock']),
            ])
            db.add(_new_spec(
                id=predecessor_id, board_id=board_id, title='Mock predecessor', created_by=USER_ID,
                status=SpecStatus.IN_PROGRESS, version=1, edition=1,
                functional_requirements=[], business_rules=[], technical_requirements=[],
                acceptance_criteria=[], test_scenarios=[], api_contracts=[],
                integration_requirements=[], observability_requirements=[], decisions=[],
            ))
            await db.flush()
            await SpecDependencyService(CommunitySqlAlchemySpecDependency(db), db).add_dependency(
                board_id=board_id, source_spec_id=spec_id, target_spec_id=predecessor_id,
                expected_spec_version=spec.version, expected_spec_edition=spec.edition,
                idempotency_key='planning-dependency', actor_id=USER_ID,
                actor_type='user', actor_name='Planning owner',
            )
            await SpecService(db).submit_spec_evaluation(
                spec_id, USER_ID, 'Planning reviewer', _decomposition_submit_data(),
            )
            await uow.commit()
    await _record_lint(db_factory, board_id, spec_id, findings=())
    async with db_factory() as db:
        spec = await db.get(Spec, spec_id)
        from okto_pulse.core.ports.delivery_inventory import default_delivery_inventory_policy
        from okto_pulse.core.ports.test_evidence import supported_test_verification_methods
        fields = ('id', 'board_id', 'spec_id', 'card_type', 'status', 'archived',
                  'test_scenario_ids', 'title', 'description', 'details')
        cards = (await db.execute(select(Card).where(Card.spec_id == spec_id))).scalars().all()
        plan = default_delivery_inventory_policy().execution_plan(
            spec=spec, cards=[{field: getattr(card, field) for field in fields} for card in cards],
            admitted_methods=supported_test_verification_methods(),
        )
        if not plan.complete:
            raise AssertionError(json.dumps({
                'qualification': plan.qualification,
                'inventory_blockers': [(row.binding.obligation_ref, row.blockers) for row in plan.inventory.rows],
            }, default=str))
        payload = {**_validation_payload(), 'expected_spec_version': spec.version,
                   'expected_planning_manifest': await current_manifest(db, spec_id),
                   'idempotency_key': 'planning-with-pending-predecessor'}

    monkeypatch.setattr(server, '_get_agent_ctx', AsyncMock(return_value=SimpleNamespace(
        agent_id=USER_ID, agent_name='Dependency parity reviewer', realm_id='local',
        permissions=['spec.read', 'spec.update', 'spec.entity.read', 'spec.entity.edit_fields',
                     'spec.interact_in.approved', 'spec.validation.submit',
                     'spec.move.approved_to_validated', 'spec.move.validated_to_in_progress'],
    )))
    if surface == 'rest':
        validation = client.post(f'/api/v1/specs/{spec_id}/validation', json=payload)
        assert validation.status_code == 201, validation.text
        result = validation.json()
    else:
        tool = await server.mcp.get_tool('okto_pulse_submit_spec_validation')
        result = json.loads(await tool.fn(board_id=board_id, spec_id=spec_id, **{
            **payload, 'expected_planning_manifest': SpecPlanningManifest.model_validate(payload['expected_planning_manifest']),
        }))
    assert result['spec_status'] == 'validated' and result['planning_result'] == 'ready', result

    async def state():
        async with db_factory() as db:
            spec = await db.get(Spec, spec_id)
            return (spec.status, spec.version, spec.edition, spec.current_validation_id,
                    spec.validations, spec.evaluations,
                    [(await db.execute(select(func.count()).select_from(model))).scalar_one()
                     for model in (ActivityLog, DomainEventRow, SpecHistory)])

    async def start():
        if surface == 'rest':
            response = client.post(f'/api/v1/specs/{spec_id}/move', json={'status': 'in_progress'})
            return response.status_code, response.json()
        tool = await server.mcp.get_tool('okto_pulse_move_spec')
        return None, json.loads(await tool.fn(board_id=board_id, spec_id=spec_id, status='in_progress'))

    before = await state()
    preview = client.get(f'/api/v1/boards/{board_id}/allowed-transitions', params={
        'entity_type': 'spec', 'entity_id': spec_id,
    })
    assert preview.status_code == 200, preview.text
    transition = next(item for item in preview.json()['allowed_transitions'] if item['to_status'] == 'in_progress')
    assert 'spec_dependencies_incomplete' in transition['blocked_reason'], transition
    status, denied = await start()
    assert status in {None, 409}, denied
    diagnosis = denied['detail'] if surface == 'rest' else denied
    assert diagnosis['code'] == 'spec_dependencies_incomplete', diagnosis
    assert predecessor_id in json.dumps(diagnosis), diagnosis
    assert await state() == before

    # Model a separately committed prerequisite completion in the disposable
    # fixture. This is not a claim to exercise the predecessor's own closeout;
    # full lifecycle/evidence closeout belongs to U4.
    async with db_factory() as db:
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            predecessor = await db.get(Spec, predecessor_id)
            predecessor.status = SpecStatus.DONE
            await uow.commit()
    after_predecessor = await state()
    assert after_predecessor[:6] == before[:6]
    status, started = await start()
    assert status in {None, 200}, started
    if surface == 'mcp':
        assert started['success'] and started['to_status'] == 'in_progress', started
    after = await state()
    assert after[0] == SpecStatus.IN_PROGRESS
    assert after[2:6] == before[2:6]  # same edition, head and independent reviews
