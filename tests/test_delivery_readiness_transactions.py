"""P06 native persistence checks at the delivery gate transaction boundary."""
from datetime import datetime, timezone
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import select, func

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import setup, read
from test_delivery_readiness_batch import statements
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_code_traceability import CommunitySqlAlchemyCodeTraceabilityStore
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, Spec, CodeInvestigationReceiptRevocationRow,
    CardDeliveryEvidenceRecordRow, DomainEventRow,
)
from okto_pulse.core.services.delivery_readiness import EvaluatedDeliveryReadiness
from okto_pulse.core.application.delivery_errors import DeliveryGateBlocked
from okto_pulse.core.services.delivery_evidence import require_spec_delivery

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


@pytest.mark.parametrize('change', ['target_revision', 'receipt_revocation'])
async def test_G10_native_fenced_gate_reloads_changed_proof_after_readiness(ledger, change):
    session, store, _ = ledger
    await setup(session)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    before = await read(session)
    context = EvaluatedDeliveryReadiness(before, actor_id=before.access.actor_id).context()
    assert context.implementation.percent == context.verification.percent == 100
    assert context.delivery_gate.status == 'allowed'
    spec = await session.get(Spec, delivery.SPEC_ID)
    board = await session.get(Board, delivery.BOARD_ID)
    event_count = await session.scalar(select(func.count()).select_from(DomainEventRow))
    record_count = await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow))
    await session.commit()

    # A different connection changes source authority after the informational read.
    async with build_community_session_factory(session.bind)() as writer:
        if change == 'target_revision':
            targets = CommunitySqlAlchemyCodeTraceabilityStore(writer)
            target = await targets.get_target(board_id=delivery.BOARD_ID, target_id='target')
            await targets.update_target(target=replace(target, revision=target.revision + 1,
                last_change_reason_sha256=sha256(b'Changed target path').hexdigest(),
                relative_path_hint='src/revised.py', updated_at=datetime.now(timezone.utc)),
                expected_revision=target.revision)
        else:
            writer.add(CodeInvestigationReceiptRevocationRow(id='readiness-revocation',
                board_id=delivery.BOARD_ID, receipt_id='receipt-1', reason_code='invalid',
                justification='Source observation withdrawn', revoked_by='owner',
                revoked_at=datetime.now(timezone.utc)))
        await writer.commit()

    with statements(session.bind) as sql, pytest.raises(DeliveryGateBlocked) as rejected:
        await require_spec_delivery(session, spec, board=board, for_update=True)
    assert sql[0].lstrip().upper().startswith('UPDATE BOARDS')
    assert rejected.value.gate.status == 'blocked'
    assert any(b.phase == 'implementation' for b in rejected.value.gate.blockers)
    await session.rollback()
    # No captured readiness object or old score authorizes the later writer.
    after = await read(session)
    assert after.snapshot_id != before.snapshot_id
    assert context.delivery_gate.status == 'allowed'  # Immutable historical observation.
    current = EvaluatedDeliveryReadiness(after, actor_id=after.access.actor_id).context()
    assert current.delivery_gate.status == 'blocked'
    assert current.implementation.percent == 0
    assert (await session.get(Spec, delivery.SPEC_ID)).status.value == 'in_progress'
    assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == event_count
    assert await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow)) == record_count


async def test_deferred_materialization_retains_captured_authority_and_never_queries_after_rollback(ledger, tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession
    from test_delivery_readiness_batch import reader
    from okto_pulse.community.adapters.sqlalchemy_models import CodeInvestigationReceiptRow
    session, store, _ = ledger
    await setup(session)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    source = (await session.scalars(select(CodeInvestigationReceiptRow))).one().source_ref
    await session.commit()
    value = reader(session)
    async with session.bind.connect() as connection:
        await connection.exec_driver_sql('BEGIN')
        async with AsyncSession(connection, expire_on_commit=False, autoflush=False) as pinned:
            rows = await value._load_transaction(pinned, delivery.BOARD_ID, delivery.SPEC_ID,
                None, {'queries': 0, 'facts': 0}, defer_facts=True)
        await connection.rollback()
    captured = rows.native_verification
    assert captured.complete and captured.runs[0].outcome == 'passed'
    await delivery.issue_runner_verification(session, tmp_path, source_ref=source, revision='a' * 40,
        outcome='failed', executed_at=datetime.now(timezone.utc).isoformat())
    with statements(session.bind) as sql:
        before = await value._snapshot_from_rows(rows)
    assert sql == []
    assert before.facts.effective_context.native_verification == captured
    assert before.facts.tests[0].current_verified_run
    current = await read(session)
    assert current.facts.effective_context.native_verification.runs[0].outcome == 'failed'
    assert current.facts.effective_context.native_verification.source_revision > captured.source_revision
    assert current.snapshot_id != before.snapshot_id
    assert not any(test.current_verified_run for test in current.facts.tests)
