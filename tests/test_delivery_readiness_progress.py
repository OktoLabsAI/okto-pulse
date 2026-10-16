"""R01/R02: narrative context and actual material invalidators stay distinct."""
from copy import deepcopy

import pytest
from sqlalchemy import select, update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_batch import read, reader, statements
from okto_pulse.community.adapters.sqlalchemy_models import Card, CardDeliveryEvidenceRecordRow as Record
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, aggregate_delivery_metrics
from okto_pulse.core.services.delivery_readiness_diagnostics import compose_delivery_diagnostics

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


@pytest.mark.parametrize('material,count', [('none', 1), ('targets', 1), ('targets', 25)], ids=['R01', 'R02', 'R02-S07'])
async def test_R01_R02_checkpoints_keep_truthful_scores_and_recoverable_native_identities(ledger, material, count):
    session, store, _ = ledger
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.execute(update(Card).where(Card.id == 'task').values(status='in_progress'))
    await session.commit()
    checkpoint_ids = set()
    for index in range(count):
        saved = await delivery.record(store, delivery.command('progress', card_id='task',
            obligation_refs=[], idempotency_key=f'checkpoint-{index}', progress={
                'material_change': material,
                'target_ids': ['target'] if material == 'targets' else [],
                'remaining': 'Earlier narrative: implementation and tests still pending',
                'source_state': {'workspace_state': 'unknown', 'recoverability': 'unknown'},
            }))
        checkpoint_ids.add(saved['id'])
    await session.commit()
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(Record))}
    with statements(session.bind) as sql:
        batch = await read(reader(session))
    assessment = assess_obligation_delivery(batch.facts)
    metrics = aggregate_delivery_metrics(assessment)
    diagnostics = compose_delivery_diagnostics(assessment, batch.facts)
    causes = {cause.id for cause in diagnostics.graph.causes if cause.code == 'material_progress_unresolved'}
    if material == 'none':
        assert metrics.implementation.percent == metrics.verification.percent == 100
        assert not causes
    else:
        assert metrics.implementation.percent == 0
        details = [detail for detail in diagnostics.details if detail.issue_id in causes]
        found = {fact.record_id for detail in details for fact in detail.observed
            if fact.target_id == 'target' and fact.execution_id == 'execution'}
        assert checkpoint_ids <= found
        assert checkpoint_ids <= {ref.id for detail in details for ref in detail.evidence_refs if ref.kind == 'record'}
        proof = next(p for row in batch.facts.implementations for p in row.executions if p.execution_id == 'execution')
        assert set(proof.blocking_progress_ids) == checkpoint_ids
        assert not proof.blocking_progress_truncated
    assert all(statement.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for statement in sql)
    assert {row.id: row.payload for row in await session.scalars(select(Record))} == history
