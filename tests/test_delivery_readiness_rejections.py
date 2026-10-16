"""Rejected selected inputs stay observable without erasing sufficient proof."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256

import pytest
from sqlalchemy import select, update

from okto_pulse.community.adapters.sqlalchemy_models import (
    CardDeliveryEvidenceRecordRow as Record,
    Card,
    Spec,
)
from okto_pulse.core.domain.delivery_evidence import CardDeliveryScope, evaluate_delivery_coverage
from okto_pulse.core.domain.delivery_selection import seal_delivery_selection
import test_delivery_evidence_integration as delivery
from test_delivery_evidence_integration import command, record
from test_evidence_v2_adapter import BOARD_ID, SPEC_ID


SCOPE = CardDeliveryScope(BOARD_ID, 'task', SPEC_ID, 1)
ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


async def append_invalid(session, original, payload):
    """Append a corrupt input fixture; never rewrite an admitted historical row."""
    row = Record(id='invalid-selected', board_id=original.board_id, card_id=original.card_id,
        spec_id=original.spec_id, spec_edition=original.spec_edition, kind=original.kind,
        actor_id=original.actor_id, actor_kind=original.actor_kind,
        idempotency_key='invalid-selected', payload_sha256='f' * 64,
        payload=payload, created_at=datetime.now(timezone.utc))
    session.add(row)
    await session.commit()
    return row


@pytest.mark.parametrize(('mutation', 'reason'), [
    ('not_object', 'delivery_record_payload_invalid'),
    ('foreign_card', 'delivery_record_card_scope_mismatch'),
    ('bindings_missing', 'delivery_binding_payload_invalid'),
    ('bindings_wrong_type', 'delivery_binding_payload_invalid'),
    ('bindings_duplicate', 'delivery_binding_payload_invalid'),
    ('binding_malformed', 'delivery_binding_payload_invalid'),
    ('contribution_missing', 'delivery_implementation_payload_invalid'),
    ('scope_missing', 'delivery_contribution_scope_payload_invalid'),
])
async def test_invalid_selected_record_retains_reason_without_poisoning_valid_proof(ledger, mutation, reason):
    session, store, _ = ledger
    good = await record(store, command())
    await record(store, command('test', implementation_ids=[good['id']]))
    original = await session.get(Record, good['id'])
    original_payload = deepcopy(original.payload)
    payload = deepcopy(original.payload)
    if mutation == 'not_object':
        payload = ['not', 'an', 'object']
    elif mutation == 'foreign_card':
        payload['card_id'] = 'private-foreign-card'
    elif mutation == 'bindings_missing':
        payload.pop('bindings')
    elif mutation == 'bindings_wrong_type':
        payload['bindings'] = 'invalid'
    elif mutation == 'bindings_duplicate':
        payload['bindings'].append(payload['bindings'][0])
    elif mutation == 'binding_malformed':
        payload['bindings'][0]['obligation_ref'] = None
    elif mutation == 'contribution_missing':
        payload.pop('contributions')
    elif mutation == 'scope_missing':
        payload.pop('contribution_scopes')
    bad = await append_invalid(session, original, payload)
    before = deepcopy([(r.id, r.payload) for r in (await session.scalars(select(Record))).all()])
    snapshot, _ = await store.load_rollup_snapshot(BOARD_ID, SPEC_ID)
    result = evaluate_delivery_coverage(snapshot)
    assert result.allowed
    assert [r.record_id for r in result.rejected_records] == [bad.id]
    assert result.rejected_records[0].reason_codes == (reason,)
    assert result.rejected_records[0].card_id == 'task'
    assert 'private-foreign-card' not in repr(result.rejected_records)
    assert all(row.implementation_ids == (good['id'],) for row in result.rows)
    assert original.payload == original_payload
    assert [(r.id, r.payload) for r in (await session.scalars(select(Record))).all()] == before
    projection = await store.projection(BOARD_ID, SPEC_ID)
    assert projection['allowed']
    assert projection['rejected_records'][0]['reason_codes'] == (reason,)


async def test_native_selection_excludes_rejected_history_before_classification(ledger):
    session, store, _ = ledger
    good = await record(store, command())
    original = await session.get(Record, good['id'])
    bad = await append_invalid(session, original, {**original.payload, 'contribution_scopes': []})
    snapshot = await store.load_card_snapshot(SCOPE)
    assert snapshot.rejected_records[0].record_id == bad.id
    manifest = seal_delivery_selection(scope=SCOPE, card_version=1, revision=2,
        records=[dict(id=original.id, kind=original.kind, sha256=store._selection_record_hash(original))],
        obligations=snapshot.obligations, impact=None)
    selected = await store.load_card_snapshot(SCOPE, prospective_report={'delivery_manifest': manifest})
    assert selected.complete
    assert not selected.rejected_records
    assert [fact.id for fact in selected.implementations] == [original.id]
    assert await session.get(Record, bad.id) is bad


async def test_revoked_selected_record_retains_reason_without_receiving_credit(ledger):
    session, store, _ = ledger
    good = await record(store, command())
    await record(store, command('revoke', card_id='task', record_id=good['id']), human=True)
    snapshot = await store.load_card_snapshot(SCOPE)
    assert not snapshot.implementations
    assert snapshot.rejected_records[0].record_id == good['id']
    assert snapshot.rejected_records[0].reason_codes == ('delivery_record_revoked',)
    assert snapshot.rejected_records[0].bindings
    assert not evaluate_delivery_coverage(snapshot).allowed

    from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery
    result = assess_obligation_delivery(snapshot, card_id='task', card_type='normal')
    assert all(row.implementation.state == 'stale' for row in result.obligations)


async def test_target_revision_invalidation_keeps_exact_cause(ledger):
    from okto_pulse.community.adapters.sqlalchemy_code_traceability import CommunitySqlAlchemyCodeTraceabilityStore
    session, store, _ = ledger
    good = await record(store, command())
    targets = CommunitySqlAlchemyCodeTraceabilityStore(session)
    target = await targets.get_target(board_id=BOARD_ID, target_id='target')
    await targets.update_target(target=replace(target, revision=2,
        last_change_reason_sha256=sha256(b'Changed target path').hexdigest(),
        relative_path_hint='src/revised.py', updated_at=datetime.now(timezone.utc)), expected_revision=1)
    await session.commit()
    snapshot = await store.load_card_snapshot(SCOPE)
    assert 'delivery_target_revision_changed' in snapshot.rejected_records[0].reason_codes
    assert snapshot.rejected_records[0].record_id == good['id']
    assert not evaluate_delivery_coverage(snapshot).allowed


@pytest.mark.parametrize('ambiguous', [False, True])
async def test_selected_test_preserves_unresolved_or_ambiguous_scenario(ledger, ambiguous):
    session, store, _ = ledger
    impl = await record(store, command())
    test = await record(store, command('test', implementation_ids=[impl['id']]))
    scope = replace(SCOPE, card_id='test')
    snapshot = await store.load_card_snapshot(scope)
    native = snapshot.effective_context.native_verification
    if ambiguous:
        population = replace(native, runs=(*native.runs, *native.runs))
        result = await store.load_card_snapshot(scope, native_verification=population)
    else:
        await session.execute(update(Spec).where(Spec.id == SPEC_ID).values(test_scenarios=[]))
        await session.commit()
        result = await store.load_card_snapshot(scope)
    assert not result.tests
    assert result.rejected_records[0].record_id == test['id']
    expected = 'delivery_scenario_ambiguous' if ambiguous else 'delivery_scenario_unresolved'
    assert result.rejected_records[0].reason_codes == (expected,)


async def test_actual_snapshot_produces_independent_proof_metrics_before_done(ledger):
    from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, aggregate_delivery_metrics
    session, store, _ = ledger
    await session.execute(update(Card).values(status='in_progress'))
    await session.commit()
    impl = await record(store, command())
    implemented, _ = await store.load_rollup_snapshot(BOARD_ID, SPEC_ID)
    first = aggregate_delivery_metrics(assess_obligation_delivery(implemented))
    assert first.implementation.percent == 100 and first.verification.percent == 0
    await record(store, command('test', implementation_ids=[impl['id']]))
    snapshot, _ = await store.load_rollup_snapshot(BOARD_ID, SPEC_ID)
    assessment = assess_obligation_delivery(snapshot, card_id='task', card_type='normal')
    metrics = aggregate_delivery_metrics(assessment)
    assert metrics.implementation.percent == metrics.verification.percent == 100
    assert all(not row.verification.lifecycle_satisfied for row in assessment.obligations)
    assert not evaluate_delivery_coverage(snapshot).allowed
    assert (await session.get(Card, 'task')).status == 'in_progress'
    assert (await session.get(Card, 'test')).status == 'in_progress'


@pytest.mark.parametrize('scenario_id', [[], {}, None, ''])
async def test_invalid_scenario_identifier_is_rejected_without_crashing_rollup(ledger, scenario_id):
    session, store, _ = ledger
    impl = await record(store, command())
    good = await record(store, command('test', implementation_ids=[impl['id']]))
    original = await session.get(Record, good['id'])
    bad = await append_invalid(session, original, {**original.payload, 'scenario_id': scenario_id})
    projection = await store.projection(BOARD_ID, SPEC_ID)
    assert projection['allowed']
    assert projection['rejected_records'][0]['record_id'] == bad.id
    assert projection['rejected_records'][0]['reason_codes'] == ('delivery_test_payload_invalid',)
