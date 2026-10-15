"""U2/UV09/22: resource predicates retain their applicability at public edges."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog, Board, DomainEventRow, Spec, SpecHistory, SpecValidationOperationReceiptRow,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.spec_planning import SpecPlanningManifest
from okto_pulse.core.mcp import server
from okto_pulse.core.services.main import SpecService
from okto_pulse.core.services.resource_gate import ResourceGateService
from test_spec_planning_lint_parity import (
    composition_client, db_factory, current_manifest, _validation_payload, _record_lint,
)
from test_spec_validation_native_gate import ACTOR, USER_ID, _decomposition_submit_data

__all__ = ['composition_client', 'db_factory']


async def _state(db_factory, spec_id):
    async with db_factory() as db:
        row = await db.get(Spec, spec_id)
        return (row.status.value, row.version, row.edition, row.validations, row.evaluations,
                [(await db.execute(select(func.count()).select_from(model))).scalar_one()
                 for model in (ActivityLog, DomainEventRow, SpecHistory, SpecValidationOperationReceiptRow)])


async def _promote(client, monkeypatch, board_id, spec_id, surface, quality_enabled, payload):
    if surface == 'rest':
        response = client.post(
            f'/api/v1/specs/{spec_id}/validation' if quality_enabled else f'/api/v1/specs/{spec_id}/move',
            json=payload if quality_enabled else {'status': 'validated'},
        )
        return response.status_code, response.json()
    monkeypatch.setattr(server, '_get_agent_ctx', AsyncMock(return_value=SimpleNamespace(
        agent_id=USER_ID, agent_name='Planning parity reviewer', realm_id='local',
        permissions=['spec.read', 'spec.update', 'spec.entity.read', 'spec.entity.edit_fields',
                     'spec.interact_in.approved', 'spec.validation.submit', 'spec.move.approved_to_validated'],
    )))
    if quality_enabled:
        tool = await server.mcp.get_tool('okto_pulse_submit_spec_validation')
        result = json.loads(await tool.fn(board_id=board_id, spec_id=spec_id, **{
            **payload, 'expected_planning_manifest': SpecPlanningManifest.model_validate(payload['expected_planning_manifest']),
        }))
    else:
        tool = await server.mcp.get_tool('okto_pulse_move_spec')
        result = json.loads(await tool.fn(board_id=board_id, spec_id=spec_id, status='validated'))
    return None, result


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['rest', 'mcp'])
@pytest.mark.parametrize('quality_enabled', [True, False])
@pytest.mark.parametrize('resource', ['architecture_missing', 'architecture_na', 'architecture_off', 'uncovered_mockup'])
async def test_native_resource_readiness_matches_public_promotions(
    composition_client, db_factory, monkeypatch, surface, quality_enabled, resource,
):
    client, spec_id, _ = composition_client
    async with db_factory() as db:
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            spec = await db.get(Spec, spec_id)
            board_id = spec.board_id
            board = await db.get(Board, board_id)
            board.settings = {**board.settings, 'require_spec_validation': quality_enabled,
                'auto_derive_spec_resources_enabled': resource in {'architecture_missing', 'architecture_na'},
                'auto_derive_spec_resource_types': ['architecture'],
                'require_spec_resource_task_coverage': True,
            }
            if resource == 'architecture_na':
                await ResourceGateService(db).mark_not_applicable(
                    board_id, 'spec', spec_id, 'architecture', USER_ID,
                    justification='This isolated mock has no architecture resource requirement.', source_channel='ui',
                )
            elif resource == 'uncovered_mockup':
                spec.screen_mockups = [{
                    'id': 'mockup-native', 'title': 'Mock status screen',
                    'description': 'A local mock for resource allocation parity.',
                    'html_content': '<main>Mock status</main>',
                }]
            await SpecService(db).submit_spec_evaluation(
                spec_id, USER_ID, 'Planning reviewer', _decomposition_submit_data(),
            )
            await uow.commit()
    if quality_enabled:
        await _record_lint(db_factory, board_id, spec_id, findings=())
    before = await _state(db_factory, spec_id)
    blocked = resource == 'architecture_missing' or (resource == 'uncovered_mockup' and quality_enabled)
    key = 'resource_coverage' if resource == 'uncovered_mockup' else 'architecture'
    async with db_factory() as db:
        spec, board = await db.get(Spec, spec_id), await db.get(Board, board_id)
        summary = await SpecService(db).get_planning_readiness(
            spec, board, can_read=lambda _: True, can_act=lambda _: True,
        )
        component = next(item for item in summary.components if item.key == key)
        assert component.state == ('blocked' if blocked else 'ready'), component
        if resource == 'architecture_off' or (resource == 'uncovered_mockup' and not quality_enabled):
            assert component.mode == 'off', component
        payload = {**_validation_payload(), 'expected_spec_version': spec.version,
                   'expected_planning_manifest': await current_manifest(db, spec_id),
                   'idempotency_key': 'resource-parity-conclusion'}
    preview = client.get(f'/api/v1/boards/{board_id}/allowed-transitions', params={
        'entity_type': 'spec', 'entity_id': spec_id,
    })
    assert preview.status_code == 200, preview.text
    transition = next(item for item in preview.json()['allowed_transitions'] if item['to_status'] == 'validated')
    if quality_enabled:
        assert 'spec_validation_required' in transition['blocked_reason'], transition
    elif blocked:
        assert component.message in transition['blocked_reason'], transition
    else:
        assert transition['blocked_reason'] is None, transition
    assert await _state(db_factory, spec_id) == before
    status, response = await _promote(client, monkeypatch, board_id, spec_id, surface, quality_enabled, payload)
    if blocked:
        assert status in {None, 409}, response
        diagnosis = response['detail'] if surface == 'rest' else response
        assert diagnosis.get('message') == component.message, diagnosis
        assert diagnosis.get('code', diagnosis.get('error')) == component.reason_codes[0], diagnosis
        assert await _state(db_factory, spec_id) == before
    else:
        assert status in {None, 201 if quality_enabled else 200}, response
        if surface == 'mcp':
            assert (response.get('spec_status') == 'validated' if quality_enabled else response.get('success') is True), response
        after = await _state(db_factory, spec_id)
        assert after[0] == 'validated' and after[4] == before[4]
        assert len(after[3] or []) == int(quality_enabled)
