"""P05: actual native SQLite snapshots, concurrent writers and query counts."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest
from sqlalchemy import event, select, update

import test_delivery_evidence_integration as delivery
import test_decision_reviews as decisions
from test_delivery_readiness_verification import prepare_reports
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.community.adapters.sqlalchemy_code_traceability import CommunitySqlAlchemyCodeTraceabilityStore
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, aggregate_delivery_metrics
from okto_pulse.core.domain.enums import CardType
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError

ledger = delivery.ledger
reviews = decisions.reviews
pytestmark = pytest.mark.asyncio
native = pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)


class Clock:
    value = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)

    def now(self):
        return self.value


def reader(session, *, kind=CommunityDeliveryReadinessReader, clock=None):
    return kind(session.bind, clock=clock or Clock())


async def read(value, **kwargs):
    return await value.load(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, **kwargs)


@contextmanager
def statements(engine):
    result = []
    def collect(conn, cursor, statement, parameters, context, many):
        result.append(statement)
    event.listen(engine.sync_engine, 'before_cursor_execute', collect)
    try:
        yield result
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', collect)


@native
@pytest.mark.parametrize('method,outcome', [('inspection', 'passed'), ('inspection', 'failed'),
    ('static_analysis', 'inconclusive'), ('demonstration', 'aborted')])
async def test_batch_matches_native_facts_including_unbound_negative_results(ledger, tmp_path, method, outcome):
    session, store, _, _, _ = await prepare_reports(ledger, tmp_path, method, [outcome])
    expected, _ = await store.load_rollup_snapshot(delivery.BOARD_ID, delivery.SPEC_ID)
    await session.rollback()
    with statements(session.bind) as sql:
        actual = await read(reader(session), card_id='task')
    assert actual.facts == expected
    assert actual.spec_status == 'in_progress'
    assert actual.facts.effective_context.native_verification.runs[0].outcome == outcome
    assert actual.scenarios[0].given == 'The delivered revision'
    assert actual.scenarios[0].verification_method == method
    assert len([s for s in sql if s.lstrip().upper().startswith('SELECT')]) == 10
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert not session.new and not session.dirty and not session.deleted


@native
async def test_batch_reads_test_proof_owned_by_another_card_and_reuses_snapshot(ledger):
    session, store, _ = ledger
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    result = await read(reader(session), card_id='task')
    metrics = aggregate_delivery_metrics(assess_obligation_delivery(result.facts, card_id='task', card_type=CardType.NORMAL))
    assert metrics.implementation.percent == metrics.verification.percent == 100
    assert result.facts.tests[0].card_id == 'test'
    assert {c.id for c in result.cards} == {'task', 'test'}


@native
async def test_snapshot_identity_excludes_clock_and_changes_with_native_inputs(ledger):
    session, store, _ = ledger
    await delivery.record(store, delivery.command())
    await session.commit()
    clock = Clock()
    value = reader(session, clock=clock)
    before = await read(value)
    clock.value += timedelta(hours=1)
    again = await read(value)
    assert before.snapshot_id == again.snapshot_id
    assert before.evaluated_at != again.evaluated_at
    targets = CommunitySqlAlchemyCodeTraceabilityStore(session)
    target = await targets.get_target(board_id=delivery.BOARD_ID, target_id='target')
    await targets.update_target(target=replace(target, revision=2,
        last_change_reason_sha256=sha256(b'Changed target path').hexdigest(),
        relative_path_hint='src/revised.py', updated_at=datetime.now(timezone.utc)), expected_revision=1)
    await session.commit()
    changed = await read(value)
    assert changed.snapshot_id != before.snapshot_id
    assert not changed.facts.implementations[0].executions[0].current_accepted_execution
    expected, _ = await store.load_rollup_snapshot(delivery.BOARD_ID, delivery.SPEC_ID)
    assert changed.facts == expected


@native
async def test_query_count_does_not_grow_per_card(ledger):
    session, _, _ = ledger
    value = reader(session)
    with statements(session.bind) as first:
        await read(value)
    session.add_all([Card(id=f'additional-{i}', board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        title=f'Card {i}', created_by='owner', card_type='normal', status='in_progress') for i in range(30)])
    await session.commit()
    with statements(session.bind) as second:
        result = await read(value)
    assert len(result.cards) == 32
    assert len(first) == len(second) == 13


@native
@pytest.mark.parametrize('repeat', [False, True])
async def test_concurrent_commit_retries_once_then_refuses_a_second_race(ledger, repeat):
    session, _, _ = ledger
    class RacingReader(CommunityDeliveryReadinessReader):
        attempts = 0
        async def _load_transaction(self, read_session, board_id, spec_id, card_id, counts):
            result = await super()._load_transaction(read_session, board_id, spec_id, card_id, counts)
            self.attempts += 1
            if repeat or self.attempts == 1:
                await session.execute(update(Card).where(Card.id == 'task').values(policy_version=Card.policy_version + 1))
                await session.commit()
                # A real SQLite transaction keeps earlier rows despite the commit.
                observed = await read_session.scalar(select(Card.policy_version).where(Card.id == 'task'))
                assert observed == next(c.version for c in result.cards if c.id == 'task')
            return result
    value = reader(session, kind=RacingReader)
    if repeat:
        with pytest.raises(ReadinessReadError, match='readiness_snapshot_changed'):
            await read(value)
    else:
        result = await read(value)
        assert next(c.version for c in result.cards if c.id == 'task') == 2
    assert value.attempts == 2


@native
@pytest.mark.parametrize('scope', [{'card_id': 'foreign'}, {'board_id': 'foreign'}, {'spec_id': 'foreign'}])
async def test_foreign_or_missing_scope_is_not_empty_success(ledger, scope):
    session, _, _ = ledger
    with pytest.raises(ReadinessReadError, match='readiness_scope_mismatch'):
        await reader(session).load(**({'board_id': delivery.BOARD_ID, 'spec_id': delivery.SPEC_ID} | scope))


@native
async def test_capacity_never_returns_truncated_positive_population(ledger, monkeypatch):
    session, _, _ = ledger
    monkeypatch.setattr('okto_pulse.community.adapters.sqlalchemy_delivery_readiness.MAX_CARDS', 1)
    result = await read(reader(session))
    assert result.unavailable_reason_codes == ('readiness_limit_exceeded', 'readiness_population_limit')
    assert not result.facts.complete and result.facts.facts_complete is False
    assert not result.cards and not result.facts.obligations
    metrics = aggregate_delivery_metrics(assess_obligation_delivery(result.facts))
    assert metrics.implementation.percent is metrics.verification.percent is None


async def test_batch_preserves_native_decision_authority_and_conflict(reviews):
    session, store = reviews
    await decisions.record(store, await decisions.command(store))
    await decisions.record(store, await decisions.command(store, 'failed'))
    await session.commit()
    expected, _ = await store.load_rollup_snapshot('board', 'spec')
    await session.rollback()
    result = await reader(session).load(board_id='board', spec_id='spec')
    assert result.facts == expected
    assert result.facts.effective_context.decision_inspections[0].status == 'conflict'


@native
async def test_no_proof_or_ledger_payload_escapes_as_orm(ledger):
    session, _, _ = ledger
    result = await read(reader(session))
    assert len(result.snapshot_id) == 64
    with pytest.raises((AttributeError, TypeError)):
        result.cards[0].version = 100
    assert not hasattr(result, 'rows')
    assert not hasattr(result, 'session')


@native
async def test_invalid_population_remains_unknown(ledger):
    session, _, _ = ledger
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(functional_requirements=None))
    await session.commit()
    result = await read(reader(session))
    metrics = aggregate_delivery_metrics(assess_obligation_delivery(result.facts))
    assert metrics.implementation.status == metrics.verification.status == 'unknown'
    assert metrics.implementation.percent is metrics.verification.percent is None


@native
async def test_near_native_card_limit_has_fixed_query_count(ledger):
    session, _, _ = ledger
    session.add_all([Card(id=f'near-limit-{i}', board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID,
        title=f'Card {i}', created_by='owner', card_type='normal', status='in_progress') for i in range(4500)])
    await session.commit()
    with statements(session.bind) as sql:
        result = await read(reader(session))
    assert len(result.cards) == 4502
    assert len(sql) == 13


@native
async def test_batch_median_does_not_regress_against_native_rollup(ledger):
    from statistics import median
    from time import perf_counter
    session, store, _ = ledger
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    value = reader(session)
    timings = {'batch': [], 'native': []}
    for index in range(6):
        start = perf_counter()
        await store.load_rollup_snapshot(delivery.BOARD_ID, delivery.SPEC_ID)
        await session.rollback()
        native_elapsed = perf_counter() - start
        start = perf_counter()
        await read(value)
        batch_elapsed = perf_counter() - start
        if index:  # Warm up both paths before comparing the same local fixture.
            timings['native'].append(native_elapsed)
            timings['batch'].append(batch_elapsed)
    measured = {key: median(values) for key, values in timings.items()}
    print('readiness_batch_local_median_seconds', measured)
    assert measured['batch'] <= measured['native'] * 1.2, measured
