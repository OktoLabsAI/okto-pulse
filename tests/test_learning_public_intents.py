"""Public typed intents use real SQL capture/report writers and signed evidence.

Target literals are seeded fixtures; independent completion gates are mocked by
the existing report fixture. This is not final graph or lifecycle acceptance.
"""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import Card, DomainEventRow
from okto_pulse.core.application.use_cases.base import PermissionDeniedError
from okto_pulse.core.application.use_cases.card_crud import MoveCardCommand, MoveCardUseCase
from okto_pulse.core.domain.learning_closeout import closeout_binding_is_current
from okto_pulse.core.models.schemas import CardMove
from okto_pulse.core.ports.permission_policy import set_permission_flag
from test_learning_capture_rest import api as _api
from test_learning_capture_mcp import mcp_capture as _mcp_capture
from test_learning_capture_intents import target
from test_learning_submission_writer import prepare, author, unit, independent_gates as _independent_gates
from test_learning_capture_writer import BOARD, runtime as _runtime

api = _api
mcp_capture = _mcp_capture
runtime = _runtime
independent_gates = _independent_gates
pytestmark = pytest.mark.asyncio


def intent(record, kind):
    return dict(kind=kind, target_node_id=record.node_id, target_generation=record.generation,
        expected_fingerprint=record.record_fingerprint, reason='Explicit applicability to this Bug',
        **({'scope': 'source_bug'} if kind == 'supersede' else {}))


@pytest.mark.parametrize('kind', ['reuse', 'supersede'])
async def test_rest_explicit_intent_retry_and_history(api, runtime, kind):
    client, store, _, body = api
    existing = await target(runtime)
    body['intent'] = intent(existing, kind)
    first = await client.post('/api/v1/bugs/bug-context/learning-captures', json=body)
    assert first.status_code == 200, first.text
    retry = await client.post('/api/v1/bugs/bug-context/learning-captures', json=body)
    assert retry.status_code == 200 and retry.json() == first.json()
    history = await client.get('/api/v1/bugs/bug-context/learning-captures', params={'board_id': BOARD})
    assert history.status_code == 200, history.text
    item, = history.json()['items']
    assert item['capture']['intent']['kind'] == kind
    if kind == 'supersede':
        assert item['capture']['intent']['scope'] == 'source_bug'
        assert item['lineage']['state'] == 'unverified'
    assert sum('capture_format' in row.payload for row in await store.enumerate(BOARD)) == 1


@pytest.mark.parametrize('kind', ['reuse', 'supersede'])
async def test_mcp_explicit_intent_and_exact_retry(mcp_capture, runtime, kind):
    _, create, body, store, _ = mcp_capture
    existing = await target(runtime)
    body['intent'] = intent(existing, kind)
    first = json.loads(await create(**body))
    assert first['status'] == 'captured_pending_materialization', first
    assert json.loads(await create(**body)) == first
    capture, = [row for row in await store.enumerate(BOARD) if 'capture_format' in row.payload]
    assert capture.payload['intent']['kind'] == kind
    assert capture.payload['author_id'] == 'author'


@pytest.mark.parametrize('transport', ['rest', 'mcp'])
async def test_stale_public_target_returns_current_identity_without_rebasing(api, mcp_capture, runtime, transport):
    client, store, _, body = api
    existing = await target(runtime)
    current = replace(existing, payload={**existing.payload, 'context': 'Later curation'}, record_fingerprint='')
    await store.append(current)
    if transport == 'rest':
        response = await client.post('/api/v1/bugs/bug-context/learning-captures', json={**body, 'intent': intent(existing, 'reuse')})
        assert response.status_code == 409, response.text
        error = response.json()['detail']
    else:
        _, create, mcp_body, _, _ = mcp_capture
        error = json.loads(await create(**{**mcp_body, 'intent': intent(existing, 'reuse')}))
    assert error['code'] == 'learning_capture_target_changed'
    assert error['current_target'] == dict(learning_id=current.node_id, generation=0,
        source_revision=1, fingerprint=current.record_fingerprint)
    assert not any('capture_format' in row.payload for row in await store.enumerate(BOARD))


@pytest.mark.parametrize('transport', ['rest', 'mcp'])
async def test_explicit_read_denial_precedes_target_conflict_details(api, mcp_capture, runtime, transport):
    client, store, principal, body = api
    existing = await target(runtime)
    selection = {**intent(existing, 'reuse'), 'expected_fingerprint': 'f' * 64}
    if transport == 'rest':
        set_permission_flag(principal.permissions, 'kg.query.learning_from_bugs', False)
        response = await client.post('/api/v1/bugs/bug-context/learning-captures', json={**body, 'intent': selection})
        assert response.status_code == 403, response.text
        error = response.json()
    else:
        _, create, mcp_body, _, flags = mcp_capture
        set_permission_flag(flags, 'kg.query.learning_from_bugs', False)
        error = json.loads(await create(**{**mcp_body, 'intent': selection}))
        assert error['code'] == 'permission_denied'
    assert 'current_target' not in json.dumps(error)
    assert not any('capture_format' in row.payload for row in await store.enumerate(BOARD))


@pytest.mark.parametrize('kind', ['reuse', 'supersede'])
@pytest.mark.parametrize('status', ['validation', 'done'])
async def test_compound_intent_is_bound_atomically_and_retries_exactly(runtime, independent_gates, kind, status):
    factory, assembler, store, _ = runtime
    bug_id, draft = await prepare(runtime, status, learning_policy='blocking')
    existing = await target(runtime)
    payload = draft.model_dump()
    payload['learning_submission']['intent'] = intent(existing, kind)
    move = CardMove.model_validate(payload)
    principal = author()
    set_permission_flag(principal.permissions, 'kg.query.learning_from_bugs', True)
    async with factory() as session:
        case = MoveCardUseCase()
        await case.execute(MoveCardCommand(bug_id, move), actor=principal, uow=unit(session))
        before = await store.enumerate(BOARD)
        await case.execute(MoveCardCommand(bug_id, move), actor=principal, uow=unit(session))
        assert await store.enumerate(BOARD) == before
        bug = await session.get(Card, bug_id)
        assert len(bug.conclusions) == 1 and bug.status.value == status
        capture, = [row for row in before if 'capture_format' in row.payload]
        assert capture.payload['intent']['kind'] == kind
        if status == 'done':
            source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=bug_id)
            assert closeout_binding_is_current(bug.learning_closeout_bindings[0], source)


async def test_compound_target_read_denial_has_no_report_or_capture(runtime, independent_gates):
    factory, _, store, _ = runtime
    bug_id, draft = await prepare(runtime, 'validation')
    existing = await target(runtime)
    payload = draft.model_dump()
    payload['learning_submission']['intent'] = intent(existing, 'reuse')
    principal = author()
    set_permission_flag(principal.permissions, 'kg.query.learning_from_bugs', False)
    before = await store.enumerate(BOARD)
    async with factory() as session:
        with pytest.raises(PermissionDeniedError):
            await MoveCardUseCase().execute(MoveCardCommand(bug_id, CardMove.model_validate(payload)), actor=principal, uow=unit(session))
        await session.commit()
        bug = await session.get(Card, bug_id)
        assert not bug.conclusions and bug.status.value == 'in_progress'
        assert list((await session.scalars(select(DomainEventRow))).all()) == []
    assert await store.enumerate(BOARD) == before


@pytest.mark.parametrize('kind', ['reuse', 'supersede'])
@pytest.mark.parametrize('failure', ['stale_target', 'late_binding'])
async def test_compound_intent_failure_rolls_back_report_capture_and_binding(runtime, independent_gates, monkeypatch, kind, failure):
    factory, _, store, _ = runtime
    bug_id, draft = await prepare(runtime, 'done', learning_policy='blocking')
    existing = await target(runtime)
    payload = draft.model_dump()
    payload['learning_submission']['intent'] = intent(existing, kind)
    if failure == 'stale_target':
        await store.append(replace(existing, payload={**existing.payload, 'context': 'Later target'}, record_fingerprint=''))
    else:
        from okto_pulse.core.domain import learning_closeout
        def fail(**kwargs): raise RuntimeError('late_intent_binding_failure')
        monkeypatch.setattr(learning_closeout, 'bind_learning_capture_to_closed_source', fail)
    before = await store.enumerate(BOARD)
    principal = author()
    set_permission_flag(principal.permissions, 'kg.query.learning_from_bugs', True)
    async with factory() as session:
        expected = 'learning_capture_target_changed' if failure == 'stale_target' else 'late_intent_binding_failure'
        uow = unit(session)
        with pytest.raises((ValueError, RuntimeError), match=expected):
            await MoveCardUseCase().execute(MoveCardCommand(bug_id, CardMove.model_validate(payload)), actor=principal, uow=uow)
        uow.rollback.assert_awaited_once()
        await session.commit()
        bug = await session.get(Card, bug_id)
        assert bug.status.value == 'validation' and not bug.conclusions and not bug.learning_closeout_bindings
        assert list((await session.scalars(select(DomainEventRow))).all()) == []
    assert await store.enumerate(BOARD) == before
