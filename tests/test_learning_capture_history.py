"""Bounded, authorized capture history survives later non-capture revisions."""

from dataclasses import replace

import pytest
from sqlalchemy import select, update

import test_learning_capture_rest as rest
import test_learning_capture_writer as writer
from okto_pulse.community.adapters.sqlalchemy_models import KGCognitiveSource, KGCognitiveSourceRevision
from okto_pulse.core.application.use_cases.learning_capture import LEARNING_CAPTURE_HISTORY_PERMISSIONS
from okto_pulse.core.ports.kg_cognitive_source import CognitiveSourceError
from okto_pulse.core.ports.permission_policy import set_permission_flag

runtime = writer.runtime
capture_api = rest.api


@pytest.fixture
async def api(capture_api):
    set_permission_flag(capture_api[2].permissions, 'kg.query.learning_from_bugs', True)
    yield capture_api
URL = '/api/v1/bugs/bug-context/learning-captures'


async def test_history_empty_and_paginated_authored_content(api):
    client, store, _, body = api
    params = {'board_id': writer.BOARD, 'limit': 1}
    empty = await client.get(URL, params=params)
    assert empty.status_code == 200 and empty.json()['items'] == []
    for index in range(3):
        saved = await client.post(URL, json={**body, 'capture_id': f'capture-{index}'})
        assert saved.status_code == 200, saved.text
    observed = []
    for _ in range(3):
        page = await client.get(URL, params=params)
        assert page.status_code == 200, page.text
        data = page.json()
        assert len(data['items']) == 1
        item = data['items'][0]
        assert item['capture']['author_id'] == 'owner'
        assert item['capture']['content'] == body['content']
        assert 'status' not in item and 'current' not in item
        observed.append(item['capture']['capture_id'])
        params['cursor'] = data['next_cursor']
    assert params['cursor'] is None and len(set(observed)) == 3
    assert len(await store.enumerate(writer.BOARD)) == 3


@pytest.mark.parametrize('flag', LEARNING_CAPTURE_HISTORY_PERMISSIONS)
async def test_history_each_read_permission_is_required(api, flag):
    client, _, principal, _ = api
    set_permission_flag(principal.permissions, flag, False)
    response = await client.get(URL, params={'board_id': writer.BOARD})
    assert response.status_code == 403, response.text


async def test_history_preserves_capture_after_literal_head_and_detects_older_corruption(api, runtime):
    client, store, _, body = api
    factory, _, _, _ = runtime
    assert (await client.post(URL, json=body)).status_code == 200
    original, = await store.enumerate(writer.BOARD)
    for index in range(2):
        await store.append(replace(original, payload={'content': f'later literal {index}'},
            record_fingerprint=''))
    before = await store.enumerate(writer.BOARD)
    response = await client.get(URL, params={'board_id': writer.BOARD})
    assert response.status_code == 200, response.text
    item, = response.json()['items']
    assert item['capture']['capture_id'] == body['capture_id']
    assert item['source_revision'] == 0
    assert await store.enumerate(writer.BOARD) == before
    async with factory() as session:
        oldest = (await session.execute(select(KGCognitiveSourceRevision).order_by(
            KGCognitiveSourceRevision.source_revision))).scalars().first()
        await session.execute(update(KGCognitiveSourceRevision).where(
            KGCognitiveSourceRevision.id == oldest.id).values(record_fingerprint='f' * 64))
        await session.commit()
    broken = await client.get(URL, params={'board_id': writer.BOARD})
    assert broken.status_code in (409, 503), broken.text
    assert 'items' not in broken.json()


async def test_history_rejects_invalid_page_and_cross_board(api):
    client, store, _, _ = api
    for limit in (0, 51):
        result = await client.get(URL, params={'board_id': writer.BOARD, 'limit': limit})
        assert result.status_code == 422
    missing = await client.get('/api/v1/bugs/missing/learning-captures', params={'board_id': writer.BOARD})
    assert missing.status_code == 404
    foreign = await client.get(URL, params={'board_id': 'foreign-board'})
    assert foreign.status_code in (403, 404)
    assert await store.enumerate(writer.BOARD) == ()


async def test_history_failure_is_unavailable_not_empty(api, monkeypatch):
    client, store, _, _ = api
    async def unavailable(*args, **kwargs):
        raise CognitiveSourceError('learning_capture_history_limit', board_id=writer.BOARD)
    monkeypatch.setattr(store, 'read_capture_history_in_context', unavailable)
    result = await client.get(URL, params={'board_id': writer.BOARD})
    assert result.status_code == 503
    assert 'items' not in result.json()


async def test_history_actual_revision_budget_refuses_truncation(api, runtime):
    client, store, _, body = api
    factory, _, _, _ = runtime
    assert (await client.post(URL, json=body)).status_code == 200
    original, = await store.enumerate(writer.BOARD)
    async with factory() as session:
        base = (await session.execute(select(KGCognitiveSource))).scalar_one()
        for number in range(1, 201):
            record = replace(original, payload={'content': str(number)}, record_fingerprint='', source_revision=number)
            session.add(KGCognitiveSourceRevision(cognitive_source_id=base.id, source_revision=number,
                record_fingerprint=record.record_fingerprint, payload=dict(record.payload),
                evidence_refs=list(record.evidence_refs)))
        await session.commit()
    response = await client.get(URL, params={'board_id': writer.BOARD, 'limit': 1})
    assert response.status_code == 503
    assert 'items' not in response.json()


async def test_history_does_not_expose_other_bug_or_board_capture(api):
    client, store, _, body = api
    assert (await client.post(URL, json=body)).status_code == 200
    original, = await store.enumerate(writer.BOARD)
    for board, bug, node in [(writer.BOARD, 'other-bug', 'other-bug-node'),
                             ('other-board', 'bug-context', 'other-board-node')]:
        payload = {**original.payload, 'source': {**original.payload['source'], 'board_id': board, 'bug_id': bug}}
        await store.append(replace(original, board_id=board, node_id=node, payload=payload, record_fingerprint=''))
    response = await client.get(URL, params={'board_id': writer.BOARD})
    assert response.status_code == 200, response.text
    item, = response.json()['items']
    assert item['learning_id'] == original.node_id
    assert response.json()['next_cursor'] is None


async def test_history_finds_capture_existing_only_in_revision(api):
    client, store, _, body = api
    assert (await client.post(URL, json=body)).status_code == 200
    original, = await store.enumerate(writer.BOARD)
    literal = replace(original, node_id='literal-birth', payload={'content': 'Original literal Learning'},
        record_fingerprint='')
    await store.append(literal)
    await store.append(replace(original, node_id=literal.node_id,
        payload={**original.payload, 'capture_id': 'later-capture'}, record_fingerprint=''))
    response = await client.get(URL, params={'board_id': writer.BOARD})
    assert response.status_code == 200, response.text
    later, = [item for item in response.json()['items'] if item['learning_id'] == literal.node_id]
    assert later['capture']['capture_id'] == 'later-capture'
    assert later['source_revision'] == 1


async def test_history_actual_payload_budget_refuses_partial_success(api):
    client, store, _, body = api
    assert (await client.post(URL, json=body)).status_code == 200
    original, = await store.enumerate(writer.BOARD)
    large = 'é' * 60000
    rows = tuple(replace(original, node_id=f'large-{number:02}', record_fingerprint='',
        payload={**original.payload, 'capture_id': f'large-{number:02}', 'content': large,
            'context': 'x' * 60000, 'applicability': 'y' * 60000}) for number in range(40))
    # Each closed capture is below 256 KiB; their aggregate exceeds 8 MiB.
    await store.append_many(rows)
    response = await client.get(URL, params={'board_id': writer.BOARD, 'limit': 50})
    assert response.status_code == 503, response.text
    assert 'items' not in response.json()
