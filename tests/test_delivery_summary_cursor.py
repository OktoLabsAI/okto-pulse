import json

import pytest

from okto_pulse.core.models.delivery_summary import DeliverySummaryCursorBinding
from okto_pulse.core.ports.delivery_readiness import ReadinessReadError
from okto_pulse.community.adapters.delivery_readiness_cursor import CommunityReadinessCursorCodec
from test_delivery_readiness_cursor import KEY, payload, sign_payload, binding as readiness_binding


def binding(**changes):
    return DeliverySummaryCursorBinding(**(dict(actor_id='reader', actor_kind='human', realm_id='local',
        board_id='board', spec_id='spec', spec_edition=2, population_id='a'*64, group='pending', limit=10) | changes))


def test_summary_cursor_survives_codec_restart_and_hides_actor_and_scope():
    token = CommunityReadinessCursorCodec(KEY).encode_summary(binding(), 5)
    assert CommunityReadinessCursorCodec(KEY).decode_summary(token, binding()) == 5
    assert set(payload(token)) == {'kind', 'version', 'binding', 'spec_edition', 'population_id', 'offset'}
    assert 'reader' not in json.dumps(payload(token)) and KEY.decode() not in token


@pytest.mark.parametrize('changes', [dict(actor_id='other'), dict(actor_kind='agent'), dict(realm_id='other'),
    dict(board_id='other'), dict(spec_id='other'), dict(group='delivered'), dict(limit=5)])
def test_summary_cursor_rejects_other_actor_scope_filter_or_limit(changes):
    codec = CommunityReadinessCursorCodec(KEY)
    with pytest.raises(ReadinessReadError, match='readiness_query_invalid'):
        codec.decode_summary(codec.encode_summary(binding(), 5), binding(**changes))


@pytest.mark.parametrize('changes', [dict(spec_edition=3), dict(population_id='b'*64)])
def test_summary_cursor_refuses_changed_edition_or_population(changes):
    codec = CommunityReadinessCursorCodec(KEY)
    with pytest.raises(ReadinessReadError, match='readiness_cursor_stale'):
        codec.decode_summary(codec.encode_summary(binding(), 5), binding(**changes))


@pytest.mark.parametrize('changes', [dict(offset=True), dict(offset=0), dict(offset=1.5),
    dict(version=True), dict(version=2), dict(extra='field'), dict(kind='persisted-readiness'),
    dict(population_id=[]), dict(population_id='invalid'), dict(spec_edition=True), dict(binding=[])] )
def test_even_signed_malformed_summary_cursors_are_rejected(changes):
    codec = CommunityReadinessCursorCodec(KEY)
    value = payload(codec.encode_summary(binding(), 5)) | changes
    with pytest.raises(ReadinessReadError, match='readiness_query_invalid'):
        codec.decode_summary(sign_payload(json.dumps(value,sort_keys=True,separators=(',',':'))),binding())


@pytest.mark.parametrize('mode', ['forged', 'unsigned', 'foreign_key', 'other_namespace', 'noncanonical'])
def test_summary_cursor_rejects_forgery_and_existing_readiness_namespaces(mode):
    codec = CommunityReadinessCursorCodec(KEY)
    token = codec.encode_summary(binding(), 5)
    if mode == 'forged':
        head, signature = token.split('.')
        token = head + '.' + ('a' if signature[0] != 'a' else 'b') + signature[1:]
    elif mode == 'unsigned':
        token = token.split('.')[0]
    elif mode == 'foreign_key':
        token = CommunityReadinessCursorCodec(b'other-installation-signing-key-000000').encode_summary(binding(), 5)
    elif mode == 'other_namespace':
        token = codec.encode(readiness_binding(), 5)
    else:
        token = sign_payload(' '+json.dumps(payload(token),sort_keys=True,separators=(',',':')))
    with pytest.raises(ReadinessReadError, match='readiness_query_invalid'):
        codec.decode_summary(token, binding())
