"""UV11: composed reviews retain both critical-context guards before writes."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.core.domain.spec_planning import SpecPlanningManifest
from okto_pulse.core.mcp import server
from okto_pulse.core.services import main
from test_spec_planning_lint_parity import (
    composition_client, db_factory, current_manifest, _validation_payload, _record_lint,
)
from test_spec_planning_resource_parity import _state
from test_spec_validation_native_gate import USER_ID, _decomposition_submit_data

__all__ = ['composition_client', 'db_factory']


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['rest', 'mcp'])
@pytest.mark.parametrize('unavailable_review', [1, 2], ids=['quality', 'decomposition'])
async def test_composed_conclusion_rejects_each_unavailable_critical_context(
    composition_client, db_factory, monkeypatch, surface, unavailable_review,
):
    client, spec_id, _ = composition_client
    async with db_factory() as db:
        spec = await db.get(Spec, spec_id)
        board_id = spec.board_id
    await _record_lint(db_factory, board_id, spec_id, findings=())
    async with db_factory() as db:
        spec = await db.get(Spec, spec_id)
        payload = {**_validation_payload(), 'expected_spec_version': spec.version,
                   'expected_planning_manifest': await current_manifest(db, spec_id),
                   'idempotency_key': 'critical-context-conclusion',
                   'decomposition': _decomposition_submit_data()}
    before = await _state(db_factory, spec_id)
    native_resolvers = main.build_default_full_context_resolvers
    resolutions = []

    def resolvers(db):
        mapping = native_resolvers(db)
        native_spec = mapping['spec']

        class ReadFault:
            async def resolve_full_context(self, **kwargs):
                # Resolve the real subject first; the fault removes context at
                # the read boundary, without replacing the guard's decision.
                resolved = await native_spec.resolve_full_context(**kwargs)
                resolutions.append(kwargs)
                return {} if len(resolutions) == unavailable_review else resolved

        return {**mapping, 'spec': ReadFault()}

    monkeypatch.setattr(main, 'build_default_full_context_resolvers', resolvers)
    if surface == 'rest':
        response = client.post(f'/api/v1/specs/{spec_id}/validation', json=payload)
        assert response.status_code == 409, response.text
        result = response.json()['detail']
    else:
        monkeypatch.setattr(server, '_get_agent_ctx', AsyncMock(return_value=SimpleNamespace(
            agent_id=USER_ID, agent_name='Planning reviewer', realm_id='local',
            permissions=['spec.read', 'spec.update', 'spec.entity.read', 'spec.entity.edit_fields',
                         'spec.interact_in.approved', 'spec.validation.submit', 'spec.evaluations.submit'],
        )))
        from okto_pulse.core.domain.spec_evaluation import SpecEvaluationInput
        tool = await server.mcp.get_tool('okto_pulse_submit_spec_validation')
        result = json.loads(await tool.fn(board_id=board_id, spec_id=spec_id, **{
            **payload,
            'expected_planning_manifest': SpecPlanningManifest.model_validate(payload['expected_planning_manifest']),
            'decomposition': SpecEvaluationInput.model_validate(payload['decomposition']),
        }))
    assert len(resolutions) == unavailable_review
    assert 'full_context_unavailable' in json.dumps(result), result
    assert await _state(db_factory, spec_id) == before
