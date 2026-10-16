"""KG7.5: authored recovery discharges only its demonstrated technical debt."""
import pytest

from okto_pulse.community.adapters.sqlalchemy_canonical_debt import CommunitySqlAlchemyCanonicalDebtStore
from okto_pulse.core.kg.canonical_learning_partition import (
    HISTORICAL_DEBT_REASON, PARTITION_TARGET_STATUS, _stable_content_hash,
)
from okto_pulse.core.kg.workers.cognitive_closeout import CognitiveCloseoutWorker
from okto_pulse.core.ports.canonical_debt import register_canonical_debt_store
from okto_pulse.core.services.canonical_debt_service import upsert_canonical_debt
from test_learning_materialization_worker import (
    runtime as _runtime, independent_gates as _independent_gates,
    graph_runtime as _graph_runtime, work_store as _work_store, deliver_capture_events,
)
from test_learning_materialization_writer import graph_rows
from test_learning_capture_writer import BOARD

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
work_store = _work_store


@pytest.mark.asyncio
@pytest.mark.parametrize('restriction', [None, 'authority', 'blocked', 'dlq', 'version',
                                        'source_absent', 'other_learning', 'other_target'])
async def test_recovered_authored_projection_closes_its_technical_debt(graph_runtime, work_store, restriction):
    runtime, capture, _, _ = graph_runtime
    factory, _, source_store, _ = runtime
    store, discovery = work_store
    debts = CommunitySqlAlchemyCanonicalDebtStore()
    register_canonical_debt_store(debts)
    await deliver_capture_events(factory)
    worker = CognitiveCloseoutWorker(factory, store=store, pending_work_provider=discovery)
    assert await worker.drain_once() == 1
    history = await source_store.enumerate(BOARD)
    graph_rows('MATCH (n:Learning)-[r:validates]->(b:Bug) WHERE n.id = $id DELETE r',
               {'id': capture.node_id})
    async with factory() as session:
        debt = await upsert_canonical_debt(session, board_id=BOARD, artifact_type='bug',
            artifact_id='bug-context', source_ref='bug:bug-context',
            content_hash=_stable_content_hash('bug:bug-context',
                'other-learning' if restriction == 'other_learning' else capture.node_id),
            target_status='other_target' if restriction == 'other_target' else PARTITION_TARGET_STATUS,
            canonical_state='blocked' if restriction == 'blocked' else 'pending',
            failure_reason=({'authority': 'authority_denied', 'source_absent': 'source_absent'}
                            .get(restriction, HISTORICAL_DEBT_REASON)),
            source_version='unproven-version' if restriction == 'version' else None,
            dlq_ref='dlq:integrity' if restriction == 'dlq' else None)
        await session.commit()
    async with factory() as session:
        original = await debts.get(session, debt_id=debt.id)
        substantive = await upsert_canonical_debt(session, board_id=BOARD, artifact_type='bug',
            artifact_id='bug-context', source_ref='bug:bug-context',
            content_hash=_stable_content_hash('bug:bug-context', 'independent-authority-review'),
            target_status=PARTITION_TARGET_STATUS, canonical_state='blocked',
            failure_reason='authority_denied', source_version='reviewed-source-version',
            dlq_ref='dlq:independent-authority')
        await session.commit()
        substantive_before = await debts.get(session, debt_id=substantive.id)
    assert await worker.drain_once() == 1
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == [
        [capture.node_id, 'canonical-bug']]
    assert await source_store.enumerate(BOARD) == history
    async with factory() as session:
        actual = await debts.get(session, debt_id=debt.id)
        assert await debts.get(session, debt_id=substantive.id) == substantive_before
    if restriction is None:
        assert actual.canonical_state == 'committed'
        assert actual.evidence_ref == capture.node_id
    else:
        assert actual == original


@pytest.mark.asyncio
async def test_debt_reconciliation_rolls_back_with_failed_relational_ack(graph_runtime, monkeypatch):
    from okto_pulse.core.ports.consolidation import get_consolidation_persistence_port
    runtime, capture, selection, persister = graph_runtime
    factory, _, source_store, _ = runtime
    debts = CommunitySqlAlchemyCanonicalDebtStore()
    register_canonical_debt_store(debts)
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    history = await source_store.enumerate(BOARD)
    graph_rows('MATCH (n:Learning)-[r:validates]->(b:Bug) WHERE n.id = $id DELETE r',
               {'id': capture.node_id})
    async with factory() as session:
        debt = await upsert_canonical_debt(session, board_id=BOARD, artifact_type='bug',
            artifact_id='bug-context', source_ref='bug:bug-context',
            content_hash=_stable_content_hash('bug:bug-context', capture.node_id),
            target_status=PARTITION_TARGET_STATUS, canonical_state='pending',
            failure_reason=HISTORICAL_DEBT_REASON)
        await session.commit()

    async def fail_ack(session):
        staged = await debts.get(session, debt_id=debt.id)
        assert staged.canonical_state == 'committed'
        raise RuntimeError('injected relational ack failure after debt CAS')

    monkeypatch.setattr(get_consolidation_persistence_port(), 'commit', fail_ack)
    with pytest.raises(RuntimeError, match='injected relational ack failure after debt CAS'):
        await persister.persist_authored_learning(BOARD, 'bug-context', selection, raise_failures=True)
    async with factory() as session:
        actual = await debts.get(session, debt_id=debt.id)
    assert actual.canonical_state == 'pending' and actual.evidence_ref is None
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == []
    assert await source_store.enumerate(BOARD) == history
