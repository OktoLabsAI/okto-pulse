from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from okto_pulse.community.api import analytics
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import ActorContext, EntityNotFoundError
from okto_pulse.core.domain.delivery_evidence import DeliveryScope, DeliveryEvidenceSnapshot, DeliveryObligation, DeliveryBinding
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.effective_delivery_coverage import EffectiveDeliveryContext
from okto_pulse.core.domain.effective_delivery_inventory import EffectiveDeliveryInventory, EffectiveDeliveryObligation
from okto_pulse.core.domain.implementation_responsibility import ImplementationResponsibilityPlan, RequirementContribution
from okto_pulse.core.kg.interfaces.graph_errors import GraphCorruption, GraphQueryTimeout
from okto_pulse.core.ports.spec_coverage_query import SpecCoverageSnapshot
from okto_pulse.core.services.spec_coverage_query import project_spec_coverage

URL = '/boards/board/specs/spec/coverage'
FLAGS = ['board.read', 'spec.entity.read', 'card.entity.read', 'spec.tests.read',
    'spec.integration_requirements.read', 'spec.observability_requirements.read']


@pytest_asyncio.fixture
async def api(monkeypatch):
    actor = ActorContext('agent', 'mcp', actor_kind='agent', realm_id='local', board_id='board',
        permissions=[*FLAGS, 'code_traceability.evidence.read'])
    monkeypatch.setattr(RESTAdapterContract, 'actor', lambda *args, **kwargs: actor)
    board = SimpleNamespace(id='board', owner_id='owner', realm_id='local', settings={'kg_query_timeout_ms': 700})
    calls = []
    async def aggregate(query, **options):
        calls.append((query, options))
        scope = DeliveryScope(query.board_id, query.spec_id, 1)
        spec = SimpleNamespace(id=query.spec_id, board_id=query.board_id, edition=1, test_scenarios=[],
            **{field: [] for _, field in COLLECTIONS})
        proof = DeliveryEvidenceSnapshot(scope, tuple(DeliveryObligation(DeliveryBinding(f'fr:{i}', 'a' * 64), f'Requirement {i}')
            for i in range(2)), complete=True)
        contribution = RequirementContribution(
            "task", "direct", "selected_criteria", ("ac",), None, (), "b" * 64,
        )
        inventory = EffectiveDeliveryInventory(
            tuple(EffectiveDeliveryObligation(row.binding, "fr", (contribution,), ())
                  for row in proof.obligations),
            ImplementationResponsibilityPlan((), True, ()), True, (),
        )
        proof = replace(proof, effective_context=EffectiveDeliveryContext(
            inventory, (), (), frozenset({"automated_test"}),
        ))
        facts = SpecCoverageSnapshot(scope, query.actor_scope_ref, 'revision', datetime.now(timezone.utc), spec, (), True, proof)
        if not query.read_delivery:
            facts = replace(facts, delivery=None, delivery_state='restricted')
        return project_spec_coverage(query, facts)
    operation = AsyncMock(side_effect=aggregate)
    uow = SimpleNamespace(boards=SimpleNamespace(get=AsyncMock(return_value=board)),
        services=SimpleNamespace(analytics=SimpleNamespace(spec_coverage=operation)))
    app = FastAPI(); app.include_router(analytics.router)
    app.dependency_overrides[require_user] = lambda: 'user'
    app.dependency_overrides[get_unit_of_work] = lambda: uow
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield SimpleNamespace(client=client, actor=actor, board=board, operation=operation, calls=calls)


@pytest.mark.asyncio
async def test_shared_authority_typed_response_pagination_and_policy(api):
    response = await api.client.get(URL, params={'limit': 1, 'timeout_ms': 30000})
    assert response.status_code == 200, response.text
    first = response.json()
    assert api.calls[0][1] == {'timeout_ms': 700}
    assert first['authority'] == 'informational'
    assert first['delivery']['counts']['verification_proven'] == 0
    second = await api.client.get(URL, params={'limit': 1, 'cursor': first['next_cursor']})
    assert second.status_code == 200, second.text
    assert second.json()['delivery']['counts']['obligations'] == 2
    assert second.json()['items'][0]['obligation_ref'] != first['items'][0]['obligation_ref']


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', FLAGS)
async def test_each_source_grant_precedes_aggregation(api, flag):
    api.actor.permissions.remove(flag)
    assert (await api.client.get(URL)).status_code == 403
    api.operation.assert_not_called()


@pytest.mark.asyncio
async def test_proof_grant_is_optional_and_cannot_be_enabled_by_query(api):
    api.actor.permissions.remove('code_traceability.evidence.read')
    result = (await api.client.get(URL, params={'read_delivery': True})).json()
    assert result['delivery']['state'] == 'restricted'
    assert all(value is None for value in result['delivery']['counts'].values())


@pytest.mark.asyncio
@pytest.mark.parametrize(('error','status','code'), [
    (ValueError('spec_coverage_cursor_invalid'),400,'spec_coverage_cursor_invalid'),
    (ValueError('spec_coverage_cursor_stale'),409,'spec_coverage_cursor_stale'),
    (ValueError('spec_coverage_source_changed'),409,'spec_coverage_source_changed'),
    (ValueError('spec_coverage_graph_scope_mismatch'),503,'spec_coverage_unavailable'),
    (GraphCorruption('secret path'),503,'spec_coverage_unavailable'),
    (GraphQueryTimeout('secret query'),504,'graph_query_timeout'),
    (EntityNotFoundError('spec','secret'),404,'spec_not_found'),
])
async def test_failures_never_expose_native_details_or_become_empty_success(api,error,status,code):
    api.operation.side_effect = error
    response = await api.client.get(URL)
    assert response.status_code == status
    assert response.json()['detail']['code'] == code
    assert 'secret' not in response.text
