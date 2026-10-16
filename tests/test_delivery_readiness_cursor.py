"""P05 native HMAC envelope and real signed delivery projection pagination."""
import base64
from dataclasses import asdict
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_verification import prepare_reports
from okto_pulse.community.adapters.delivery_readiness_cursor import CommunityReadinessCursorCodec
from okto_pulse.community.adapters.sqlalchemy_delivery_readiness import CommunityDeliveryReadinessReader, CommunityReadinessClock
from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, aggregate_delivery_metrics
from okto_pulse.core.models.delivery_readiness import DeliveryReadinessContext, ReadinessCursorBinding, ReadinessReadQuery
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError
from okto_pulse.core.services.delivery_readiness import PreparedDeliveryReadiness, serialize_readiness_response
from okto_pulse.core.services.delivery_readiness_diagnostics import compose_delivery_diagnostics

KEY = b'readiness-test-only-installation-key-32'
ledger = delivery.ledger


def binding(**changes):
    return ReadinessCursorBinding.model_validate(dict(actor_id='actor', board_id='board', spec_id='spec',
        card_id=None, spec_edition=2, snapshot_id='a' * 64, projection_id='b' * 64,
        phase='all', issue_id=None, limit=20) | changes)


def sign_payload(payload):
    encoded = payload.encode('utf-8')
    key = hashlib.sha256(b'okto-pulse-delivery-readiness-cursor-v1\x00' + KEY).digest()
    signature = hmac.new(key, encoded, hashlib.sha256).digest()
    return '.'.join(base64.urlsafe_b64encode(v).decode('ascii').rstrip('=') for v in (encoded, signature))


def payload(token):
    encoded = token.split('.')[0]
    return json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))


def test_native_stable_setting_survives_new_codec_and_keeps_scope_ids_out_of_token():
    settings = SimpleNamespace(guideline_policy_cursor_signing_key=SecretStr(KEY.decode()))
    first = CommunityReadinessCursorCodec.from_settings(settings)
    second = CommunityReadinessCursorCodec.from_settings(settings)
    token = first.encode(binding(), 12)
    assert second.decode(token, binding()) == 12
    assert token == second.encode(binding(), 12)
    assert set(payload(token)) == {'kind', 'version', 'binding', 'snapshot_id', 'projection_id', 'spec_edition', 'offset'}
    assert KEY.decode() not in token


@pytest.mark.parametrize('changes', [{'actor_id': 'other'}, {'board_id': 'other'}, {'spec_id': 'other'},
    {'card_id': 'card'}, {'phase': 'verification'}, {'issue_id': 'cause'}, {'limit': 10}])
def test_S08_actor_scope_filter_issue_and_limit_cannot_reuse_cursor(changes):
    codec = CommunityReadinessCursorCodec(KEY)
    token = codec.encode(binding(), 2)
    with pytest.raises(ReadinessReadError, match='readiness_query_invalid'):
        codec.decode(token, binding(**changes))


def test_S08_view_is_bound_even_with_a_valid_signature():
    codec = CommunityReadinessCursorCodec(KEY)
    token = codec.encode(binding().model_copy(update={'view': 'ledger'}), 2)
    with pytest.raises(ReadinessReadError, match='readiness_query_invalid'):
        codec.decode(token, binding())


@pytest.mark.parametrize('changes', [{'snapshot_id': 'c' * 64}, {'spec_edition': 3}, {'projection_id': 'd' * 64}])
def test_S09_fact_edition_or_visibility_change_is_stale(changes):
    codec = CommunityReadinessCursorCodec(KEY)
    with pytest.raises(ReadinessReadError, match='readiness_cursor_stale'):
        codec.decode(codec.encode(binding(), 2), binding(**changes))


@pytest.mark.parametrize('change', [{'offset': True}, {'offset': -1}, {'offset': 2.0}, {'version': True},
    {'extra': 'field'}, {'spec_edition': 0}, {'snapshot_id': []}, {'projection_id': 'wrong'}, {'kind': 'ledger'}])
def test_even_authenticated_malformed_shapes_are_refused(change):
    codec = CommunityReadinessCursorCodec(KEY)
    value = payload(codec.encode(binding(), 2)) | change
    token = sign_payload(json.dumps(value, sort_keys=True, separators=(',', ':')))
    with pytest.raises(ReadinessReadError, match='readiness_query_invalid'):
        codec.decode(token, binding())


@pytest.mark.parametrize('mode', ['duplicate', 'whitespace', 'signature', 'unsigned', 'length', 'foreign_key'])
def test_noncanonical_forged_unsigned_and_foreign_tokens_are_rejected(mode):
    codec = CommunityReadinessCursorCodec(KEY)
    token = codec.encode(binding(), 2)
    if mode in {'duplicate', 'whitespace'}:
        value = json.dumps(payload(token), sort_keys=True, separators=(',', ':'))
        value = value.replace('"offset":2', '"offset":2,"offset":2') if mode == 'duplicate' else ' ' + value
        token = sign_payload(value)
    elif mode == 'signature':
        head, signature = token.split('.')
        token = head + '.' + ('a' if signature[0] != 'a' else 'b') + signature[1:]
    elif mode == 'unsigned':
        token = token.split('.')[0]
    elif mode == 'length':
        token = 'x' * 8193
    else:
        token = CommunityReadinessCursorCodec(b'another-installation-signing-key-32').encode(binding(), 2)
    with pytest.raises(ReadinessReadError, match='readiness_query_invalid'):
        codec.decode(token, binding())


@pytest.mark.parametrize('key', [None, b'', b'short', 'not-bytes'])
def test_missing_or_weak_key_fails_without_random_fallback(key):
    with pytest.raises(ReadinessReadError, match='readiness_assessment_unavailable'):
        CommunityReadinessCursorCodec(key)


def prepared(snapshot, codec):
    assessment = assess_obligation_delivery(snapshot.facts)
    metrics = aggregate_delivery_metrics(assessment)
    projection = compose_delivery_diagnostics(assessment, snapshot.facts,
        permissions={'spec.tests.execute': True}, card_versions={c.id: c.version for c in snapshot.cards})
    scope = snapshot.facts.scope
    # P06 will supply native transition flags. This fixture explicitly does
    # not claim an unevaluated gate is allowed or blocked.
    context = DeliveryReadinessContext.model_validate(dict(scope=dict(board_id=scope.board_id,
        spec_id=scope.spec_id, card_id=None, card_type=None, spec_edition=scope.edition,
        spec_version=snapshot.spec_version, card_version=None), snapshot_id=snapshot.snapshot_id,
        evaluated_at=snapshot.evaluated_at, scope_complete=assessment.completeness.scope_resolved,
        evaluation_complete=assessment.completeness.evaluation_complete,
        implementation=asdict(metrics.implementation), verification=asdict(metrics.verification),
        delivery_state='verification_pending', proof_complete=False,
        delivery_gate=dict(status='unknown', mode='blocking', exception_applied=False, exception_refs=[]),
        lifecycle=dict(stored_status=snapshot.spec_status, check_status='not_evaluated',
            other_blocker_count=None, qualification_is_current=None),
        summary=dict(message_code='pending', message='Existing result awaits association',
            unresolved_units=metrics.verification.unresolved, root_causes=len(projection.graph.causes),
            lifecycle_pending=None, exceptions=0)))
    return PreparedDeliveryReadiness(context, projection, cursor=codec)


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
async def test_native_snapshots_share_metrics_and_authenticated_cursor_then_refuse_changed_base(ledger, tmp_path):
    session, _, _, _, _ = await prepare_reports(ledger, tmp_path, 'inspection', ['passed', 'passed'])
    reader = CommunityDeliveryReadinessReader(session.bind, clock=CommunityReadinessClock())
    codec = CommunityReadinessCursorCodec(KEY)
    snapshot = await reader.load(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID)
    projection = prepared(snapshot, codec)
    query = ReadinessReadQuery(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, limit=1)
    first = projection.page(query, actor_id='agent-1')
    assert first.page.has_more and first.implementation.percent == 100 and first.verification.percent == 0
    assert first.issues[0].code == 'verification_association_missing'
    continuation = query.model_copy(update={'cursor': first.page.next_cursor})
    again = await reader.load(board_id=query.board_id, spec_id=query.spec_id)
    second = prepared(again, CommunityReadinessCursorCodec(KEY)).page(continuation, actor_id='agent-1')
    assert second.implementation == first.implementation and second.verification == first.verification
    assert second.issues[0].id != first.issues[0].id
    assert len(serialize_readiness_response(second)) <= 32768
    detail_query = ReadinessReadQuery.model_validate(first.issues[0].detail_query.model_dump())
    detail = projection.page(detail_query, actor_id='agent-1')
    assert detail.implementation == first.implementation and detail.verification == first.verification
    assert any(e.type == 'action' and e.value.kind == 'associate_verification' for e in detail.entries)
    await session.execute(update(Card).where(Card.id == 'task').values(policy_version=Card.policy_version + 1))
    await session.commit()
    changed = await reader.load(board_id=query.board_id, spec_id=query.spec_id)
    with pytest.raises(ReadinessReadError, match='readiness_cursor_stale'):
        prepared(changed, codec).page(continuation, actor_id='agent-1')
