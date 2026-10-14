"""P06 Policy Compliance uses real read-only native authority for Card closure."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import setup, read
from test_delivery_readiness_batch import statements
from test_skb3_semantic_guideline_persistence import _native_guideline_binding
from okto_pulse.community.adapters.sqlalchemy_models import Card, PermissionPreset
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import CommunitySqlAlchemySemanticGuidelineAssessment
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.domain.guideline_policy import (
    GuidelineRevision, GuidelineMetric, GuidelineMetricDirection, PolicyEntityType,
)
from okto_pulse.core.ports.permission_policy import registered_permission_flags
from okto_pulse.core.services.delivery_readiness import EvaluatedDeliveryReadiness

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


async def test_native_policy_blocks_without_writes_or_score_penalty_and_redacts_details(ledger):
    session, store, _ = ledger
    await setup(session)
    await session.execute(update(Card).where(Card.id == 'task').values(status='in_progress'))
    revision = GuidelineRevision(revision_id='readiness-policy-revision',
        guideline_id='readiness-policy', revision_number=1, semantic_version='1.0.0',
        title='Private policy title', content='Private policy content',
        metrics=(GuidelineMetric(metric_id='boundary', code='boundary', title='Private metric title',
            description='Inspect responsibility boundaries.', evaluation_rubric='Score isolation from 0 to 100.',
            target_entity_types=(PolicyEntityType.CARD,), direction=GuidelineMetricDirection.MINIMUM,
            default_threshold=70),), created_by='owner', created_at=datetime.now(timezone.utc))
    await _native_guideline_binding(session, board_id=delivery.BOARD_ID, revision=revision)
    await session.commit()
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    with statements(session.bind) as unconfigured_sql:
        unconfigured = await read(session, card_id='task')
    assert unconfigured.native_lifecycle.check_status == 'not_evaluated'
    assert unconfigured.native_lifecycle.reason_codes == ('policy_assessment_unavailable',)
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in unconfigured_sql)
    # This fixture seeded the Card directly. Supply the native semantic-version
    # registration that the application writer normally performs; reads never do it.
    await CommunitySqlAlchemySemanticGuidelineAssessment(session).record_semantic_subject_mutation(
        board_id=delivery.BOARD_ID, entity_type=PolicyEntityType.CARD, subject_id='task', actor_id='owner',
        idempotency_key='readiness-policy-subject', request_digest=canonical_sha256({'subject': 'task'}),
        changed_at=datetime.now(timezone.utc))
    await session.commit()
    with statements(session.bind) as sql:
        batch = await read(session, card_id='task')
        repeated = await read(session, card_id='task')
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert batch.snapshot_id == repeated.snapshot_id
    assert batch.native_lifecycle.blocking_codes == ('policy_compliance_receipt_missing',)
    assert batch.native_lifecycle.basis_digest
    projected = EvaluatedDeliveryReadiness(batch, actor_id=batch.access.actor_id)
    context = projected.context('task')
    assert context.implementation.percent == context.verification.percent == 100
    assert context.delivery_gate.status == 'allowed'
    assert context.lifecycle.check_status == 'blocked'
    assert context.lifecycle.qualification_is_current is False
    issue = next(c for c in projected.diagnostics('task').graph.causes if c.code == 'lifecycle_gate_pending')
    assert not issue.blocks_proof and issue.blocks_target_transition
    assert 'Private' not in repr(batch.native_lifecycle)
    assert not session.new and not session.dirty and not session.deleted

    flags = registered_permission_flags()
    flags['guidelines']['assessments']['read'] = False
    await session.execute(update(PermissionPreset).where(PermissionPreset.id == 'readiness-base').values(flags=flags))
    await session.commit()
    hidden = await read(session, card_id='task')
    assert hidden.facts == batch.facts and hidden.snapshot_id != batch.snapshot_id
    assert hidden.native_lifecycle.blocking_codes == ('policy_compliance_blocked',)
    assert hidden.native_lifecycle.basis_digest == batch.native_lifecycle.basis_digest
