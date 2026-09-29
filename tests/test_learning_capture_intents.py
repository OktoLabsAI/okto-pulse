"""Internal explicit intent admission against SQL source heads, before projection.

Target literals are seeded source fixtures. These tests do not certify graph
eligibility, materialization or reuse/supersedence completion.
"""
import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow
from okto_pulse.core.application.use_cases.base import ActorContext, PermissionDeniedError
from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
from okto_pulse.core.ports.kg_cognitive_source import CognitiveSourceRecord
from okto_pulse.core.ports.learning_capture import LearningCaptureIntent, LearningCaptureTargetConflict
from okto_pulse.core.ports.permission_policy import set_permission_flag
from test_learning_capture_writer import BOARD, runtime as _runtime, actor, uow

runtime = _runtime
pytestmark = pytest.mark.asyncio


async def target(runtime, identity='target'):
    _, _, store, request = runtime
    record = CognitiveSourceRecord(board_id=BOARD, node_type='Learning', node_id=identity,
        generation=0, evidence_refs=('bug:prior-bug',), payload={
            'content': request.content, 'context': 'Original context and applicability',
            'graph_layer': 'canonical', 'maturity_status': 'canonical_eligible',
            'created_by_agent': 'previous-author', 'created_at': '2026-09-24T00:00:00+00:00',
            'source_artifact_ref': 'bug:prior-bug'})
    await store.append(record)
    return record


def intent(record, kind='reuse'):
    return LearningCaptureIntent(kind, record.node_id, record.generation,
        record.record_fingerprint, 'Explicit applicability to this corrected Bug')


async def submit(factory, request, *, rollback=False):
    async with factory() as session:
        record = await StageLearningCaptureUseCase().execute(request, actor=actor(), uow=uow(session))
        await (session.rollback() if rollback else session.commit())
        return record


@pytest.mark.parametrize('kind', ['reuse', 'supersede'])
async def test_explicit_intent_preserves_target_history_and_retries_exactly(runtime, kind):
    factory, _, store, request = runtime
    existing = await target(runtime)
    original, = await store.enumerate(BOARD)
    request = replace(request, intent=intent(existing, kind))
    capture = await submit(factory, request)
    retry = await submit(factory, request)
    assert retry.record_fingerprint == capture.record_fingerprint
    records = await store.enumerate(BOARD)
    assert len(records) == 2 and original in records
    assert capture.payload['intent']['expected_fingerprint'] == existing.record_fingerprint
    assert (capture.node_id == existing.node_id) == (kind == 'reuse')
    assert capture.source_revision == (1 if kind == 'reuse' else 0)
    async with factory() as session:
        events = list((await session.scalars(select(DomainEventRow))).all())
        assert len(events) == 1
        assert events[0].payload_json['capture']['fingerprint'] == capture.record_fingerprint


@pytest.mark.parametrize('kind', ['reuse', 'supersede'])
async def test_intent_rollback_preserves_target_and_has_no_outbox(runtime, kind):
    factory, _, store, request = runtime
    existing = await target(runtime)
    original = await store.enumerate(BOARD)
    await submit(factory, replace(request, intent=intent(existing, kind)), rollback=True)
    assert await store.enumerate(BOARD) == original
    async with factory() as session:
        assert list((await session.scalars(select(DomainEventRow))).all()) == []


async def test_same_capture_id_cannot_silently_change_target(runtime):
    factory, _, store, request = runtime
    first, second = await target(runtime, 'first'), await target(runtime, 'second')
    await submit(factory, replace(request, intent=intent(first)))
    before = await store.enumerate(BOARD)
    with pytest.raises(ValueError, match='idempotency_conflict'):
        await submit(factory, replace(request, intent=intent(second)))
    assert await store.enumerate(BOARD) == before


@pytest.mark.parametrize('same_capture', [False, True])
async def test_concurrent_intents_have_one_winner(runtime, same_capture):
    factory, _, store, request = runtime
    first = await target(runtime, 'first')
    second = await target(runtime, 'second') if same_capture else first
    requests = [replace(request, intent=intent(first)), replace(request,
        capture_id=request.capture_id if same_capture else 'different-intent', intent=intent(second))]
    results = await asyncio.wait_for(asyncio.gather(*(submit(factory, item) for item in requests),
        return_exceptions=True), timeout=30)
    assert sum(isinstance(item, CognitiveSourceRecord) for item in results) == 1
    failure, = [item for item in results if isinstance(item, Exception)]
    assert isinstance(failure, (LearningCaptureTargetConflict, ValueError))
    assert ('idempotency_conflict' if same_capture else 'target_changed') in str(failure)
    records = await store.enumerate(BOARD)
    assert sum('capture_format' in row.payload for row in records) == 1


@pytest.mark.parametrize('damage', ['working', 'revoked', 'superseded', 'author', 'content'])
async def test_target_or_reuse_content_must_be_eligible(runtime, damage):
    factory, _, store, request = runtime
    existing = await target(runtime)
    fields = {'working': {'graph_layer': 'working'}, 'revoked': {'revocation_reason': 'invalid'},
        'superseded': {'superseded_by': 'successor'}, 'author': {'created_by_agent': ''}}
    if damage != 'content':
        existing = replace(existing, payload={**existing.payload, **fields[damage]}, record_fingerprint='')
        await store.append(existing)
    request = replace(request, intent=intent(existing),
        content='Different assertion' if damage == 'content' else request.content)
    before = await store.enumerate(BOARD)
    with pytest.raises(ValueError, match='target_not_eligible|reuse_content_changed'):
        await submit(factory, request)
    assert await store.enumerate(BOARD) == before


async def test_stale_target_reports_current_identity_without_rebasing(runtime):
    factory, _, store, request = runtime
    existing = await target(runtime)
    current = replace(existing, payload={**existing.payload, 'content': 'A later curated assertion'}, record_fingerprint='')
    await store.append(current)
    with pytest.raises(LearningCaptureTargetConflict) as failure:
        await submit(factory, replace(request, intent=intent(existing)))
    assert failure.value.current_target['learning_id'] == existing.node_id
    assert failure.value.current_target['fingerprint'] == current.record_fingerprint
    assert failure.value.current_target['source_revision'] == 1


async def test_target_read_permission_is_required_before_admission(runtime):
    factory, _, store, request = runtime
    existing = await target(runtime)
    principal = actor()
    flags = dict(principal.permissions)
    set_permission_flag(flags, 'kg.query.learning_from_bugs', False)
    principal = ActorContext(principal.actor_id, principal.source, board_id=BOARD, permissions=flags)
    before = await store.enumerate(BOARD)
    async with factory() as session:
        with pytest.raises(PermissionDeniedError):
            await StageLearningCaptureUseCase().execute(replace(request, intent=intent(existing)),
                actor=principal, uow=uow(session))
    assert await store.enumerate(BOARD) == before


@pytest.mark.parametrize('kind', ['reuse', 'supersede'])
async def test_late_target_cas_conflict_reports_observed_head_without_capture(runtime, monkeypatch, kind):
    factory, _, store, request = runtime
    existing = await target(runtime)
    updated = replace(existing, payload={**existing.payload, 'content': 'Intervening target update'}, record_fingerprint='')
    original_append = store.append_many_if_current_in_context

    async def intervening_write(context, records, **kwargs):
        # Fault injection at the real CAS boundary, not a claim of live PG contention.
        await store.append_many_in_context(context, (updated,))
        return await original_append(context, records, **kwargs)

    monkeypatch.setattr(store, 'append_many_if_current_in_context', intervening_write)
    async with factory() as session:
        with pytest.raises(LearningCaptureTargetConflict) as failure:
            await StageLearningCaptureUseCase().execute(replace(request, intent=intent(existing, kind)),
                actor=actor(), uow=uow(session))
        assert failure.value.current_target['fingerprint'] == updated.record_fingerprint
        assert list((await session.scalars(select(DomainEventRow))).all()) == []
        # Commit the injected target edit; the rejected capture must not exist.
        await session.commit()
    assert not any('capture_format' in row.payload for row in await store.enumerate(BOARD))
