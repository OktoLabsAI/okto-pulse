"""The human-only contract must survive the HTTP authentication boundary."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from okto_pulse.community.api import task_requirement_decisions as routes
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.domain.realm import LOCAL_REALM_ID


FLAGS = ['spec.entity.read', 'card.entity.read', 'code_traceability.evidence.read',
         'code_traceability.waiver.create', 'code_traceability.waiver.clear']
URL = '/api/v1/boards/board/specs/spec/task-requirement-decisions'
BODY = dict(contract_version='task-requirement-decision/v1', card_ids=['card'],
            decision='waived', basis='a' * 64, idempotency_key='apply', note=None)


def client(*, kind='human', flags=None, error=None):
    store = SimpleNamespace(task_requirement_options=AsyncMock(return_value={'basis': 'a' * 64}),
        task_requirement_history=AsyncMock(return_value={'items': []}),
        record_task_requirement_decision=AsyncMock(return_value={'status': 'applied'}, side_effect=error))
    unit = SimpleNamespace(services=SimpleNamespace(delivery_evidence=store), commit=AsyncMock())
    app = FastAPI()
    app.include_router(routes.router, prefix='/api/v1')
    app.dependency_overrides[require_principal] = lambda: Principal('authenticated',
        realm_id=LOCAL_REALM_ID, actor_kind=kind, claims={'permissions': FLAGS if flags is None else flags})
    app.dependency_overrides[get_unit_of_work] = lambda: unit
    return TestClient(app), store, unit


def test_author_and_scope_are_authenticated_and_get_never_writes():
    http, store, unit = client()
    response = http.get(URL + '/options', params={'card_ids': 'card'})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    args, kwargs = store.task_requirement_options.await_args
    assert args[0].board_id == 'board' and args[0].spec_id == 'spec'
    assert kwargs['actor_id'] == 'authenticated'
    unit.commit.assert_not_awaited()
    response = http.post(URL, json=BODY)
    assert response.status_code == 200
    args, kwargs = store.record_task_requirement_decision.await_args
    assert args[0].board_id == 'board' and args[0].spec_id == 'spec'
    assert kwargs == {'actor_id': 'authenticated', 'actor_kind': 'human'}
    unit.commit.assert_awaited_once()


@pytest.mark.parametrize('kind', ['agent', 'system', 'unknown'])
def test_nonhuman_principal_cannot_write_even_with_every_permission(kind):
    http, store, unit = client(kind=kind)
    assert http.get(URL + '/options', params={'card_ids': 'card'}).status_code == 200
    assert store.task_requirement_options.await_args.kwargs['allowed_decisions'] == ()
    assert http.post(URL, json=BODY).status_code == 403
    store.record_task_requirement_decision.assert_not_awaited()
    unit.commit.assert_not_awaited()


@pytest.mark.parametrize('field,value', [('actor_id', 'owner'), ('recorded_at', '2026-01-01'),
    ('scope_sha256', 'a' * 64), ('edition', 1), ('board_id', 'other')])
def test_caller_cannot_forge_server_owned_facts(field, value):
    http, store, _ = client()
    assert http.post(URL, json={**BODY, field: value}).status_code == 422
    store.record_task_requirement_decision.assert_not_awaited()


@pytest.mark.parametrize('cards', [[], ['card', 'card'], [str(i) for i in range(51)]])
def test_selection_is_nonempty_unique_and_bounded(cards):
    http, store, _ = client()
    assert http.get(URL + '/options', params=[('card_ids', card) for card in cards]).status_code == 422
    assert http.post(URL, json={**BODY, 'card_ids': cards}).status_code == 422
    store.task_requirement_options.assert_not_awaited()
    store.record_task_requirement_decision.assert_not_awaited()


def test_read_and_restore_permissions_are_independent():
    http, store, _ = client(flags=['spec.entity.read', 'card.entity.read', 'code_traceability.evidence.read',
                                 'code_traceability.waiver.create'])
    assert http.get(URL + '/options', params={'card_ids': 'card'}).status_code == 200
    assert store.task_requirement_options.await_args.kwargs['allowed_decisions'] == ('waived', 'optional')
    assert http.post(URL, json={**BODY, 'decision': 'required'}).status_code == 403
    store.record_task_requirement_decision.assert_not_awaited()
    http, store, _ = client(flags=[])
    assert http.get(URL + '/options', params={'card_ids': 'card'}).status_code == 403
    store.task_requirement_options.assert_not_awaited()


def test_history_is_authorized_scoped_bounded_and_read_only():
    http, store, unit = client(kind='agent')
    result = http.get(URL + '/history', params={'card_id': 'card', 'edition': 1, 'limit': 10})
    assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
    args, kwargs = store.task_requirement_history.await_args
    assert args[0].board_id == 'board' and args[0].spec_id == 'spec'
    assert args[0].card_ids == ['card'] and args[0].edition == 1
    assert kwargs['actor_id'] == 'authenticated'
    unit.commit.assert_not_awaited()
    assert http.get(URL + '/history', params={'card_id': 'card', 'limit': 21}).status_code == 422
    http, store, _ = client(flags=[])
    assert http.get(URL + '/history', params={'card_id': 'card'}).status_code == 403
    store.task_requirement_history.assert_not_awaited()


@pytest.mark.parametrize('code,status', [('stale_basis', 409), ('reopen_required', 409),
    ('idempotency_conflict', 409), ('card_not_found', 404), ('scope_unavailable', 503)])
def test_conflicts_never_commit_or_report_success(code, status):
    http, _, unit = client(error=ValueError('task_requirement_' + code))
    response = http.post(URL, json=BODY)
    assert response.status_code == status and response.json()['detail']['code'] == code
    unit.commit.assert_not_awaited()
