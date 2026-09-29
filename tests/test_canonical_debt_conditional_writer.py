"""Atomic preservation of restrictions added after maintenance read a debt."""
from dataclasses import replace

import pytest

from okto_pulse.community.adapters.sqlalchemy_canonical_debt import CommunitySqlAlchemyCanonicalDebtStore
from okto_pulse.core.ports.canonical_debt import CanonicalDebtRecord, ConditionalCanonicalDebtWriter
from test_bug_cognitive_context_adapter import _runtime, _seed_full_context


@pytest.fixture
async def debt_runtime(tmp_path):
    engine, factory = await _runtime(tmp_path / 'debt.db')
    await _seed_full_context(factory)
    store = CommunitySqlAlchemyCanonicalDebtStore()
    record = CanonicalDebtRecord(board_id='board-bug-context', artifact_type='bug',
        artifact_id='bug-context', source_ref='bug:bug-context', content_hash='hash',
        target_status='canonical_learning_partition_integrity', canonical_state='pending',
        failure_reason='canonical_learning_historical_working_only_bug_evidence_debt')
    async with factory() as session:
        await store.save(session, record)
        await session.commit()
    try:
        yield factory, store, record.id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [
    ('canonical_state', 'blocked'), ('failure_reason', 'authority_denied'),
    ('target_status', 'quarantine'), ('dlq_ref', 'dlq:integrity'),
    ('source_version', 'v2'), ('last_error', 'Content requires review'),
])
async def test_concurrent_restriction_cannot_be_overwritten(debt_runtime, field, value):
    factory, store, identity = debt_runtime
    assert isinstance(store, ConditionalCanonicalDebtWriter)
    async with factory() as session:
        expected = await store.get(session, debt_id=identity)
    changed = replace(expected, **{field: value})
    async with factory() as session:
        await store.save(session, changed)
        await session.commit()
    async with factory() as session:
        assert not await store.replace_if_current(session, expected=expected,
            replacement=replace(expected, canonical_state='committed', evidence_ref='kg:learning'))
        await session.commit()
    async with factory() as session:
        actual = await store.get(session, debt_id=identity)
    assert getattr(actual, field) == value
    assert actual.evidence_ref is None
    assert actual.canonical_state == changed.canonical_state


@pytest.mark.asyncio
async def test_exact_conditional_transition_obeys_caller_transaction(debt_runtime):
    factory, store, identity = debt_runtime
    async with factory() as session:
        expected = await store.get(session, debt_id=identity)
        assert await store.replace_if_current(session, expected=expected,
            replacement=replace(expected, canonical_state='committed', evidence_ref='kg:learning'))
        await session.rollback()
    async with factory() as session:
        assert (await store.get(session, debt_id=identity)).canonical_state == 'pending'
        expected = await store.get(session, debt_id=identity)
        assert await store.replace_if_current(session, expected=expected,
            replacement=replace(expected, canonical_state='committed', evidence_ref='kg:learning'))
        await session.commit()
    async with factory() as session:
        actual = await store.get(session, debt_id=identity)
        assert actual.canonical_state == 'committed' and actual.evidence_ref == 'kg:learning'
