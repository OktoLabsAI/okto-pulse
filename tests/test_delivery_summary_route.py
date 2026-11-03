from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from okto_pulse.community.api import code_traceability
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.delivery_evidence import GetDeliverySummaryUseCase
from okto_pulse.core.models.delivery_summary import DeliverySummaryQuery
from okto_pulse.core.ports.authentication import Principal

PATH = '/boards/board/specs/spec/delivery-summary'


def client():
    app = FastAPI()
    app.include_router(code_traceability.router)
    app.dependency_overrides[require_principal] = lambda: Principal(subject='reader', realm_id='local')
    app.dependency_overrides[get_unit_of_work] = lambda: object()
    return TestClient(app)


def test_route_delegates_typed_query_without_dropping_unknown_counts_or_caching(monkeypatch):
    observed = []
    async def execute(use_case, command, **kwargs):
        assert isinstance(use_case, GetDeliverySummaryUseCase)
        assert kwargs['json_ready'] is True
        observed.append(command)
        return {'complete': False, 'task_count': None, 'groups': []}
    monkeypatch.setattr(code_traceability, '_execute', execute)
    with client() as http:
        response = http.get(PATH,params=dict(group='pending',limit=7,cursor='signed'))
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    assert response.json()['task_count'] is None
    assert observed == [DeliverySummaryQuery(board_id='board',spec_id='spec',group='pending',limit=7,cursor='signed')]


@pytest.mark.parametrize('query', ['limit=0','limit=21','limit=true','group=other','cursor=unsigned',
    'group=pending&cursor=','card_id=foreign','edition=3','limit=5&limit=10','group=pending&group=delivered'])
def test_summary_route_rejects_ambiguous_unknown_or_invalid_query_before_dispatch(monkeypatch, query):
    async def forbidden(*args,**kwargs):
        pytest.fail('Invalid query must not reach the read use case')
    monkeypatch.setattr(code_traceability,'_execute',forbidden)
    with client() as http:
        assert http.get(PATH+'?'+query).status_code == 422
