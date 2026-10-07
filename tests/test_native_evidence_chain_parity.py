"""KG-24: native immutable Evidence supersedence survives replay and rebuild.

SQL rows are explicit attestation fixtures under the current schema guards;
this tests projection/history, not the authorization or signing ceremony.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select, update

from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Card, Spec, ConsolidationQueue
from test_code_traceability_kg_rebuild_e2e import seed_complete_traceability_source
from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_native_evidence_supersedence_matches_exact_snapshot_rebuild(tmp_path):
    now = datetime.now(timezone.utc)
    tables = Base.metadata.tables
    snapshots = []
    rule = 'supersedes/code_traceability_evidence@v2.0'

    async def extend(session):
        async def one(name):
            return dict((await session.execute(select(tables[name]))).mappings().one())
        request = await one('code_investigation_requests')
        receipt = await one('code_investigation_receipts')
        evidence = await one('code_evidence')
        await session.execute(insert(tables['code_investigation_requests']).values({
            **request, 'id': 'request-2', 'status': 'open', 'consumed_at': None,
            'expected_head_generation': 1, 'challenge_token_hash': 'd' * 64,
            'expected_predecessor_receipt_id': 'receipt-1',
            'idempotency_key': 'request-idempotency-2'}))
        await session.execute(insert(tables['code_investigation_receipts']).values({
            **receipt, 'id': 'receipt-2', 'request_id': 'request-2',
            'generation': 2, 'predecessor_receipt_id': 'receipt-1',
            'idempotency_key': 'receipt-idempotency-2'}))
        await session.execute(update(tables['code_investigation_requests'])
            .where(tables['code_investigation_requests'].c.id == 'request-2')
            .values(status='consumed', consumed_at=now + timedelta(seconds=6)))
        await session.execute(update(tables['code_investigation_heads']).values(
            generation=2, latest_receipt_id='receipt-2', current_receipt_id='receipt-2',
            revision=2, updated_at=now + timedelta(seconds=6)))
        await session.execute(insert(tables['code_evidence']).values({
            **evidence, 'id': 'evidence-2', 'investigation_receipt_id': 'receipt-2',
            'supersedes_evidence_id': 'evidence-1', 'claim': 'A revised native observation.',
            'received_at': now + timedelta(seconds=7), 'idempotency_key': 'evidence-idempotency-2'}))
        await session.execute(update(tables['code_evidence'])
            .where(tables['code_evidence'].c.id == 'evidence-1').values(lifecycle_status='superseded'))

    async def seed(factory, *, final):
        async with factory() as session:
            await session.execute(update(Board).values(created_at=now))
            await session.execute(update(Spec).values(created_at=now, updated_at=now))
            await session.execute(update(Card).values(created_at=now, updated_at=now))
            await seed_complete_traceability_source(session, board_id='board', spec_id='spec',
                card_id='card', requirement_id='fr_one', include_parents=False, now=now, link_spec_version=2)
            await session.execute(update(Spec).values(version=2, updated_at=now))
            if final:
                await extend(session)
            await session.commit()

    async def capture(factory):
        async with factory() as session:
            path = session.bind.url.database
        snapshot = CommunityBoardSourceReader(path).fetch('board')
        assert snapshot.complete
        snapshots.append(snapshot.rows)

    async def churn(factory, graph):
        assert not any(edge[3] == rule for edge in relationship_set(graph))
        async with factory() as session:
            await extend(session)
            await session.commit()
        for index, (kind, identity) in enumerate([
            ('code_investigation_receipt', 'receipt-2'), ('code_evidence', 'evidence-1'),
            ('code_evidence', 'evidence-2'), ('implementation_target', 'target-1'),
            ('code_evidence', 'evidence-2'),
        ]):
            async with factory() as session:
                session.add(ConsolidationQueue(id=f'chain-{index}', board_id='board',
                    artifact_type=kind, artifact_id=identity, source='state_transition'))
                await session.commit()
            assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
            async with factory() as session:
                assert not (await session.scalars(select(ConsolidationQueue.id))).all()
        chain = {edge: count for edge, count in relationship_set(graph).items() if edge[3] == rule}
        assert len(chain) == 1 and list(chain.values()) == [1]
        edge = next(iter(chain))
        assert edge[1:3] == ('code_evidence:evidence-2', 'code_evidence:evidence-1')
        assert edge[4][1] == 'canonical' and edge[5][1] == 'working'
        await capture(factory)

    async def capture_rebuilt(factory, graph):
        await capture(factory)

    incremental = await materialize(tmp_path / 'incremental', incremental=False,
        card_type='normal', seed=lambda factory: seed(factory, final=False),
        exercise=churn, native_schema=True)
    rebuilt = await materialize(tmp_path / 'rebuilt', incremental=False,
        card_type='normal', seed=lambda factory: seed(factory, final=True),
        exercise=capture_rebuilt, native_schema=True)
    assert snapshots[0] == snapshots[1]
    assert incremental == rebuilt
    assert all(count == 1 for count in rebuilt.values())
