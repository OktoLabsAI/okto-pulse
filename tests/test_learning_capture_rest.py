"""REST capture uses the authorized UOW and real signed evidence path."""

from dataclasses import asdict

from fastapi import FastAPI
import httpx
import pytest

from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.community.api.bug_cognitive_closure import router
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.ports.permission_policy import set_permission_flag
import test_learning_capture_writer as writer
from test_learning_capture_writer import uow, actor, BOARD

runtime = writer.runtime


@pytest.fixture
async def api(runtime, monkeypatch):
    factory, assembler, store, request = runtime
    principal = ActorContext('owner', 'rest', board_id=BOARD, permissions=actor().permissions)
    monkeypatch.setattr(RESTAdapterContract, 'actor', staticmethod(lambda *args, **kwargs: principal))
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    async def unit():
        async with factory() as session:
            value = uow(session)
            value.commit = session.commit
            try:
                yield value
            finally:
                await session.rollback()
    app.dependency_overrides[get_unit_of_work] = unit
    app.dependency_overrides[require_user] = lambda: 'owner'
    body = asdict(request)
    del body['bug_id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client, store, principal, body


async def test_rest_context_capture_commit_and_exact_retry(api):
    client, store, _, body = api
    context = await client.get('/api/v1/bugs/bug-context/learning-capture-context', params={'board_id': BOARD})
    assert context.status_code == 200, context.text
    observed = context.json()
    assert observed['contract_version'] == 'learning-capture-context/v1'
    assert observed['scenarios'][0]['authenticated'] is True
    assert await store.enumerate(BOARD) == ()
    body.update(expected_source_digest=observed['source_digest'], expected_source_version=observed['source_policy_version'])
    saved = await client.post('/api/v1/bugs/bug-context/learning-captures', json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['status'] == 'captured_pending_materialization'
    retry = await client.post('/api/v1/bugs/bug-context/learning-captures', json=body)
    assert retry.status_code == 200 and retry.json() == saved.json()
    record, = await store.enumerate(BOARD)
    assert record.payload['author_id'] == 'owner'


async def test_rest_read_authority_does_not_grant_capture_write(api):
    client, store, principal, body = api
    set_permission_flag(principal.permissions, 'kg.session.commit', False)
    read = await client.get('/api/v1/bugs/bug-context/learning-capture-context', params={'board_id': BOARD})
    assert read.status_code == 200
    denied = await client.post('/api/v1/bugs/bug-context/learning-captures', json=body)
    assert denied.status_code == 403, denied.text
    assert await store.enumerate(BOARD) == ()


@pytest.mark.parametrize('change,status', [('author', 422), ('version', 409), ('digest', 409), ('unknown', 422)])
async def test_rest_rejects_untrusted_authority_or_stale_basis_without_commit(api, change, status):
    client, store, _, body = api
    if change == 'author': body['author_id'] = 'impersonated'
    elif change == 'version': body['expected_source_version'] += 1
    elif change == 'digest': body['expected_source_digest'] = 'f' * 64
    else: body['approved'] = True
    result = await client.post('/api/v1/bugs/bug-context/learning-captures', json=body)
    assert result.status_code == status, result.text
    assert await store.enumerate(BOARD) == ()


async def test_rest_missing_bug_is_not_found_and_internal_failures_do_not_leak_details(api, monkeypatch):
    from okto_pulse.core.application.kg_operations import CoreKnowledgeGraphOperations
    client, store, _, _ = api
    missing = await client.get('/api/v1/bugs/missing/learning-capture-context', params={'board_id': BOARD})
    assert missing.status_code == 404 and missing.json()['detail']['code'] == 'bug_not_found'
    async def failed(*args, **kwargs):
        raise RuntimeError('sensitive internal path and provider detail')
    monkeypatch.setattr(CoreKnowledgeGraphOperations, 'get_learning_capture_source', failed)
    result = await client.get('/api/v1/bugs/bug-context/learning-capture-context', params={'board_id': BOARD})
    assert result.status_code == 503
    assert result.json() == {'detail': {'code': 'learning_capture_unavailable'}}
    assert await store.enumerate(BOARD) == ()
