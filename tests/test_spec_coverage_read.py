"""Bounded real SQL plus the same authenticated rollup consumed by the gate."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, update

from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.community.adapters.sqlalchemy_spec_coverage import CommunitySpecCoverageReader
from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.domain.delivery_evidence import evaluate_delivery_coverage
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout
from okto_pulse.core.ports.spec_coverage_query import SpecCoverageQuery
from okto_pulse.core.models.spec_coverage_query import SpecCoverageResponse
from okto_pulse.core.services.spec_coverage_query import project_spec_coverage
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, aggregate_delivery_metrics
import test_delivery_evidence_integration as delivery_fixture
from test_delivery_readiness_batch import read as read_readiness, reader as readiness_reader

ledger = delivery_fixture.ledger
BOARD = delivery_fixture.BOARD_ID
SPEC = delivery_fixture.SPEC_ID
QUERY = SpecCoverageQuery(BOARD, SPEC, 'authorized-actor')


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
@pytest.mark.parametrize('damage', ['none', 'target_revision', 'revoked'])
async def test_in_progress_coverage_matches_factual_readiness_without_completing_cards(ledger, damage):
    session, store, _ = ledger
    await session.execute(update(Card).values(status='in_progress'))
    await session.commit()
    implementation = await delivery_fixture.record(store, delivery_fixture.command())
    await delivery_fixture.record(store, delivery_fixture.command('test', implementation_ids=[implementation['id']]))
    if damage == 'target_revision':
        from datetime import datetime, timezone
        from hashlib import sha256
        from okto_pulse.community.adapters.sqlalchemy_code_traceability import CommunitySqlAlchemyCodeTraceabilityStore
        targets = CommunitySqlAlchemyCodeTraceabilityStore(session)
        target = await targets.get_target(board_id=BOARD, target_id='target')
        await targets.update_target(target=replace(target, revision=2,
            last_change_reason_sha256=sha256(b'Changed target path').hexdigest(),
            relative_path_hint='src/revised.py', updated_at=datetime.now(timezone.utc)), expected_revision=1)
    elif damage == 'revoked':
        await delivery_fixture.record(store, delivery_fixture.command('revoke',
            card_id='task', record_id=implementation['id']), human=True)
    await session.commit()

    observed = await CommunitySpecCoverageReader(session, delivery_store=store).read(QUERY, timeout_ms=15000)
    batch = await read_readiness(readiness_reader(session))
    # Both native adapters resolve the very same admitted facts, not fabricated
    # percentages. The only distinction is proof progress versus final lifecycle.
    assert observed.delivery == batch.facts
    metrics = aggregate_delivery_metrics(assess_obligation_delivery(batch.facts))
    result = project_spec_coverage(QUERY, observed)
    SpecCoverageResponse.model_validate(result)
    expected = 2 if damage == 'none' else 0
    assert result['delivery']['counts']['implementation_proven'] == metrics.implementation.satisfied == expected
    assert result['delivery']['counts']['verification_proven'] == metrics.verification.satisfied == expected
    assert not evaluate_delivery_coverage(observed.delivery).allowed
    assert {card.status.value for card in await session.scalars(select(Card))} == {'in_progress'}
    assert (await session.get(Spec, SPEC)).version == 1


@pytest.mark.asyncio
async def test_linked_test_card_without_ledger_proof_stays_missing(ledger):
    session, store, _ = ledger
    spec = await session.get(Spec, SPEC)
    # KG-58: a linked Test Card with no current passing scenario or admitted
    # delivery record. Historical receipts are preserved, never promoted.
    scenario = {**spec.test_scenarios[0], 'status': 'ready', 'evidence': None, 'linked_task_ids': ['test']}
    await session.execute(update(Spec).where(Spec.id == SPEC).values(test_scenarios=[scenario]))
    await session.commit()
    observed = await CommunitySpecCoverageReader(session, delivery_store=store).read(QUERY, timeout_ms=15000)
    result = project_spec_coverage(QUERY, observed)
    SpecCoverageResponse.model_validate(result)
    assert result['structure']['complete_for_scope']
    assert result['structure']['summary']['scenario_task_linkage_pct'] == 100
    assert result['delivery']['counts']['verification_proven'] == 0
    assert all(item['verification'] == 'missing' for item in result['items'])
    assert result['projection_freshness']['state'] == 'unknown'


@pytest.mark.asyncio
async def test_signed_proof_matches_gate_rollup_without_write_or_hidden_paths(ledger):
    session, store, _ = ledger
    implementation = await delivery_fixture.record(store, delivery_fixture.command())
    verification = await delivery_fixture.record(store, delivery_fixture.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    expected, _ = await store.load_rollup_snapshot(BOARD, SPEC)
    evaluated = evaluate_delivery_coverage(expected)
    assert evaluated.allowed
    statements = []
    listener = lambda conn, cursor, statement, *args: statements.append(statement)
    event.listen(session.bind.sync_engine, 'before_cursor_execute', listener)
    try:
        observed = await CommunitySpecCoverageReader(session, delivery_store=store).read(QUERY, timeout_ms=15000)
    finally:
        event.remove(session.bind.sync_engine, 'before_cursor_execute', listener)
    result = project_spec_coverage(QUERY, observed)
    assert result['items'][0]['implementation_record_refs'] == [implementation['id']]
    assert result['items'][0]['verification_record_refs'] == [verification['id']]
    assert result['items'][0]['verification'] == 'proven'
    assert result['delivery']['blockers'] == list(evaluated.blockers)
    assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP')) for sql in statements)
    assert 'src/file.py' not in str(result)
    assert (await session.get(Spec, SPEC)).status.value == 'in_progress'


@pytest.mark.asyncio
async def test_restricted_proof_is_not_even_read_or_counted(ledger):
    session, _, _ = ledger
    proof = SimpleNamespace(load_rollup_snapshot=AsyncMock(side_effect=AssertionError('private proof read')))
    statements = []
    listener = lambda conn, cursor, statement, *args: statements.append(statement)
    event.listen(session.bind.sync_engine, 'before_cursor_execute', listener)
    try:
        observed = await CommunitySpecCoverageReader(session, delivery_store=proof).read(replace(QUERY, read_delivery=False), timeout_ms=15000)
    finally:
        event.remove(session.bind.sync_engine, 'before_cursor_execute', listener)
    result = project_spec_coverage(replace(QUERY, read_delivery=False), observed)
    proof.load_rollup_snapshot.assert_not_called()
    assert not any('delivery_evidence_records' in sql for sql in statements)
    assert result['delivery']['state'] == 'restricted'
    assert all(value is None for value in result['delivery']['counts'].values())


@pytest.mark.asyncio
async def test_card_change_without_spec_version_changes_source_identity(ledger):
    session, _, _ = ledger
    query = replace(QUERY, read_delivery=False)
    reader = CommunitySpecCoverageReader(session)
    before = await reader.read(query, timeout_ms=15000)
    await session.execute(update(Card).where(Card.id == 'test').values(status='in_progress'))
    await session.commit()
    after = await reader.read(query, timeout_ms=15000)
    assert before.spec.version == after.spec.version
    assert before.source_revision != after.source_revision


@pytest.mark.asyncio
async def test_foreign_spec_not_found_before_proof(ledger):
    session, _, _ = ledger
    proof = SimpleNamespace(load_rollup_snapshot=AsyncMock())
    with pytest.raises(EntityNotFoundError):
        await CommunitySpecCoverageReader(session, delivery_store=proof).read(replace(QUERY, board_id='foreign'), timeout_ms=15000)
    proof.load_rollup_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_drains_read_scope_before_returning(ledger):
    session, _, _ = ledger
    cleaned = False

    async def slow(*args):
        nonlocal cleaned
        try:
            await asyncio.sleep(5)
        finally:
            await asyncio.sleep(0.01)
            cleaned = True

    proof = SimpleNamespace(load_rollup_snapshot=slow)
    with pytest.raises(GraphQueryTimeout):
        await CommunitySpecCoverageReader(session, delivery_store=proof).read(QUERY, timeout_ms=100)
    assert cleaned
    assert not session.in_nested_transaction()
    assert await session.scalar(select(Card.id).where(Card.id == 'test')) == 'test'
