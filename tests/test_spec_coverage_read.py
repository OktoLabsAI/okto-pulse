"""Bounded real SQL plus the same authenticated rollup consumed by the gate."""
import asyncio
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, update

from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.community.adapters.sqlalchemy_spec_coverage import CommunitySpecCoverageReader
from okto_pulse.community.adapters.graph_operation_guards import BoardCloseInProgressError
from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.domain.delivery_evidence import evaluate_delivery_coverage
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout, GraphCapabilityUnavailable, GraphUnavailable
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
@pytest.mark.parametrize('phase', ['source_read', 'proof_bound', 'proof_load',
                                  'source_recheck', 'savepoint_cleanup'])
@pytest.mark.parametrize('failure_type', [TimeoutError, GraphQueryTimeout])
async def test_required_timeout_logs_only_sanitized_phase(monkeypatch, caplog, phase, failure_type):
    failure = failure_type('SELECT private_secret FROM /private/path')

    class Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            if phase == 'savepoint_cleanup':
                raise failure

    store = SimpleNamespace(load_rollup_snapshot=AsyncMock(return_value=(None, None)))
    reader = CommunitySpecCoverageReader(SimpleNamespace(begin_nested=Savepoint), delivery_store=store)
    source = {'edition': 1}
    calls = 0

    async def read_source(_):
        nonlocal calls
        calls += 1
        if (phase == 'source_read' and calls == 1) or (phase == 'source_recheck' and calls == 2):
            raise failure
        return source, []

    monkeypatch.setattr(reader, '_source', read_source)
    monkeypatch.setattr(reader, '_proof_bound', AsyncMock(
        side_effect=failure if phase == 'proof_bound' else None))
    if phase == 'proof_load':
        store.load_rollup_snapshot.side_effect = failure
    with pytest.raises(GraphQueryTimeout) as caught:
        await reader.read(replace(QUERY, read_delivery=True), timeout_ms=15000)
    if failure_type is GraphQueryTimeout:
        assert caught.value is failure
    else:
        assert caught.value.__cause__ is failure
    messages = [r.getMessage() for r in caplog.records if 'spec_coverage.read_timeout' in r.getMessage()]
    assert len(messages) == 1 and f'stage={phase}' in messages[0]
    assert 'private' not in caplog.text and 'SELECT' not in caplog.text


@pytest.mark.asyncio
async def test_caller_cancel_is_not_logged_as_required_timeout(monkeypatch, caplog):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def savepoint():
        yield

    reader = CommunitySpecCoverageReader(SimpleNamespace(begin_nested=savepoint), delivery_store=object())
    failure = asyncio.CancelledError()
    monkeypatch.setattr(reader, '_source', AsyncMock(side_effect=failure))
    with pytest.raises(asyncio.CancelledError) as caught:
        await reader.read(QUERY, timeout_ms=15000)
    assert caught.value is failure
    assert 'spec_coverage.read_timeout' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(('failure', 'reason'), [
    (BoardCloseInProgressError('private board', 1), 'board_close_in_progress'),
    (GraphCapabilityUnavailable('private path', details={'reason': 'prepared_reader_unavailable'}),
     'prepared_reader_unavailable'),
    (GraphCapabilityUnavailable('private path', details={'reason': 'query_readers_not_configured'}),
     'query_readers_not_configured'),
    (GraphCapabilityUnavailable('private path', details={'reason': 'secret\nforged-log-entry',
        'backend_error_type': 'secret', 'query': 'private query'}), 'graph_capability_unavailable'),
    (GraphUnavailable('private path', details={'secret': 'private query'}), 'graph_unavailable'),
    (GraphQueryTimeout('private path'), 'graph_query_timeout'),
])
async def test_graph_refusal_diagnostics_preserve_behavior_and_hide_native_content(
        monkeypatch, caplog, failure, reason):
    def fail(*args):
        raise failure
    execution = SimpleNamespace(scope=lambda *args, **kwargs: nullcontext())
    reader = CommunitySpecCoverageReader(None,
        graph_reader=SimpleNamespace(read_spec_coverage_graph=fail), query_execution=execution)
    monkeypatch.setattr(reader, 'verify_source', AsyncMock())
    if isinstance(failure, GraphQueryTimeout):
        with pytest.raises(GraphQueryTimeout) as raised:
            await reader.read_graph(replace(QUERY, read_graph=True), None, 'revision', timeout_ms=15000)
        assert raised.value is failure
    else:
        result = await reader.read_graph(replace(QUERY, read_graph=True), None, 'revision', timeout_ms=15000)
        assert result.state == 'unavailable'
    assert f'board={BOARD} spec={SPEC} stage=graph_observation reason={reason}' in caplog.text
    assert not any(value in caplog.text for value in ('private', 'secret', 'forged-log-entry'))


@pytest.mark.asyncio
async def test_initial_fence_timeout_is_distinguished_and_never_enters_graph(monkeypatch, caplog):
    reader = CommunitySpecCoverageReader(None)
    failure = GraphQueryTimeout('private source details')
    monkeypatch.setattr(reader, 'verify_source', AsyncMock(side_effect=failure))
    with pytest.raises(GraphQueryTimeout) as raised:
        await reader.read_graph(replace(QUERY, read_graph=True), None, 'revision', timeout_ms=15000)
    assert raised.value is failure
    assert 'stage=initial_source_fence reason=graph_query_timeout' in caplog.text
    assert 'private' not in caplog.text


@pytest.mark.asyncio
async def test_unrelated_graph_failure_propagates_without_unavailable_diagnostic(monkeypatch, caplog):
    failure = RuntimeError('unexpected implementation failure')
    def fail(*args):
        raise failure
    reader = CommunitySpecCoverageReader(None,
        graph_reader=SimpleNamespace(read_spec_coverage_graph=fail),
        query_execution=SimpleNamespace(scope=lambda *args, **kwargs: nullcontext()))
    monkeypatch.setattr(reader, 'verify_source', AsyncMock())
    with pytest.raises(RuntimeError) as raised:
        await reader.read_graph(replace(QUERY, read_graph=True), None, 'revision', timeout_ms=15000)
    assert raised.value is failure
    assert 'spec_coverage.graph_observation_unavailable' not in caplog.text


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
async def test_coverage_preserves_signed_snapshot_without_unused_card_summaries(ledger, monkeypatch):
    session, store, _ = ledger
    implementation = await delivery_fixture.record(store, delivery_fixture.command())
    await delivery_fixture.record(store, delivery_fixture.command(
        'test', implementation_ids=[implementation['id']]))
    await session.commit()
    expected, per_card = await store.load_rollup_snapshot(BOARD, SPEC)
    assert per_card
    assert evaluate_delivery_coverage(expected).allowed
    # These presentation-only reads must not make required Coverage proof
    # unavailable. The real authenticated rollup and its fences still execute.
    for name in ('_selection_summary', '_accumulated_impact',
                 'report_impact_status', '_progress_summary'):
        monkeypatch.setattr(store, name, AsyncMock(
            side_effect=AssertionError('Coverage must not load discarded card summaries')))
    rollup = AsyncMock(wraps=store.load_rollup_snapshot)
    monkeypatch.setattr(store, 'load_rollup_snapshot', rollup)
    observed = await CommunitySpecCoverageReader(session, delivery_store=store).read(
        QUERY, timeout_ms=15000)
    rollup.assert_awaited_once_with(BOARD, SPEC, include_card_summaries=False)
    assert observed.delivery == expected
    assert evaluate_delivery_coverage(observed.delivery) == evaluate_delivery_coverage(expected)
    assert not session.in_nested_transaction()


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
    def listener(conn, cursor, statement, *args):
        statements.append(statement)
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
    def listener(conn, cursor, statement, *args):
        statements.append(statement)
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

    async def slow(*args, **kwargs):
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


@pytest.mark.asyncio
async def test_expired_absolute_deadline_never_reads_or_starts_savepoint(ledger, monkeypatch):
    import time
    session, _, _ = ledger
    reader = CommunitySpecCoverageReader(session)
    source = AsyncMock(side_effect=AssertionError('expired source read'))
    monkeypatch.setattr(reader, '_source', source)
    for operation in (lambda: reader.read(QUERY, timeout_ms=15000, deadline=time.monotonic() - 1),
                      lambda: reader.verify_source(QUERY, 'old', timeout_ms=15000,
                                                   deadline=time.monotonic() - 1)):
        with pytest.raises(GraphQueryTimeout):
            await operation()
    source.assert_not_called()
    assert not session.in_nested_transaction()


@pytest.mark.asyncio
async def test_mandatory_final_fence_detects_source_change_and_drains_timeout(ledger, monkeypatch):
    session, _, _ = ledger
    query = replace(QUERY, read_delivery=False)
    reader = CommunitySpecCoverageReader(session)
    snapshot = await reader.read(query, timeout_ms=15000)
    await session.execute(update(Card).where(Card.id == 'test').values(status='in_progress'))
    await session.commit()
    with pytest.raises(ValueError, match='source_changed'):
        await reader.verify_source(query, snapshot.source_revision, timeout_ms=15000)
    cleaned = []
    async def slow(_query):
        try:
            await asyncio.sleep(5)
        finally:
            cleaned.append(True)
    monkeypatch.setattr(reader, '_source', slow)
    with pytest.raises(GraphQueryTimeout):
        await reader.verify_source(query, snapshot.source_revision, timeout_ms=10)
    assert cleaned == [True]
