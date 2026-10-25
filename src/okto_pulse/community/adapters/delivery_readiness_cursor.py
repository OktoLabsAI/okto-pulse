"""Readiness namespace for the installation's native HMAC cursor mechanism."""
import base64
import hashlib
import hmac
import json

from okto_pulse.core.ports.delivery_readiness import ReadinessReadError

_NAMESPACE = b'okto-pulse-delivery-readiness-cursor-v1\x00'


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _encode(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _decode(value):
    raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    if _encode(raw) != value:
        raise ValueError('noncanonical_cursor')
    return raw


class CommunityReadinessCursorCodec:
    def __init__(self, signing_key: bytes):
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ReadinessReadError('readiness_assessment_unavailable', native_code='readiness_cursor_signing_unavailable')
        self._key = hashlib.sha256(_NAMESPACE + signing_key).digest()

    @classmethod
    def from_settings(cls, settings):
        value = getattr(settings, 'guideline_policy_cursor_signing_key', None)
        reveal = getattr(value, 'get_secret_value', None)
        value = reveal() if callable(reveal) else value
        return cls(value.encode('utf-8') if isinstance(value, str) else value)

    @staticmethod
    def _binding(binding):
        return hashlib.sha256(_canonical(binding.model_dump(mode='json',
            exclude={'snapshot_id', 'spec_edition', 'projection_id'}))).hexdigest()

    def encode(self, binding, offset):
        if type(offset) is not int or offset < 1:
            raise ReadinessReadError('readiness_query_invalid', native_code='readiness_cursor_position_invalid')
        payload = _canonical({'version': 1, 'kind': 'delivery-readiness',
            'binding': self._binding(binding), 'snapshot_id': binding.snapshot_id,
            'spec_edition': binding.spec_edition, 'projection_id': binding.projection_id, 'offset': offset})
        return _encode(payload) + '.' + _encode(hmac.new(self._key, payload, hashlib.sha256).digest())

    def decode(self, token, binding):
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or token.count('.') != 1:
                raise ValueError('invalid_envelope')
            encoded, signature = map(_decode, token.split('.'))
            if not hmac.compare_digest(signature, hmac.new(self._key, encoded, hashlib.sha256).digest()):
                raise ValueError('invalid_signature')
            payload = json.loads(encoded)
            if not isinstance(payload, dict) or not hmac.compare_digest(encoded, _canonical(payload)):
                raise ValueError('noncanonical_payload')
            if set(payload) != {'version', 'kind', 'binding', 'snapshot_id', 'spec_edition', 'projection_id', 'offset'}:
                raise ValueError('invalid_fields')
            if (type(payload['version']) is not int or payload['version'] != 1
                or payload['kind'] != 'delivery-readiness' or type(payload['offset']) is not int
                or payload['offset'] < 1 or type(payload['spec_edition']) is not int or payload['spec_edition'] < 1
                or any(not isinstance(payload[key], str) or len(payload[key]) != 64
                    or any(c not in '0123456789abcdef' for c in payload[key])
                    for key in ('snapshot_id', 'projection_id'))):
                raise ValueError('invalid_shape')
            if not isinstance(payload['binding'], str) or not hmac.compare_digest(payload['binding'], self._binding(binding)):
                raise ValueError('invalid_binding')
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise ReadinessReadError('readiness_query_invalid', native_code='readiness_cursor_invalid') from exc
        if (payload['snapshot_id'], payload['spec_edition'], payload['projection_id']) != (
            binding.snapshot_id, binding.spec_edition, binding.projection_id):
            raise ReadinessReadError('readiness_cursor_stale')
        return payload['offset']

    def encode_projection(self, *, actor_id, board_id, spec_id, edition, card_id,
                          limit, generation_id, offset):
        if not isinstance(generation_id, str) or not generation_id or type(offset) is not int or offset < 1:
            raise ReadinessReadError('readiness_query_invalid')
        binding = _canonical({'actor_id': actor_id, 'board_id': board_id, 'spec_id': spec_id,
            'edition': edition, 'card_id': card_id, 'limit': limit})
        payload = _canonical({'version': 1, 'kind': 'persisted-readiness',
            'binding': hashlib.sha256(binding).hexdigest(), 'generation_id': generation_id,
            'offset': offset})
        return _encode(payload) + '.' + _encode(hmac.new(self._key, payload, hashlib.sha256).digest())

    def decode_projection(self, token, *, actor_id, board_id, spec_id, edition,
                          card_id, limit):
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 8192 or token.count('.') != 1:
                raise ValueError('invalid_envelope')
            encoded, signature = map(_decode, token.split('.'))
            if not hmac.compare_digest(signature, hmac.new(self._key, encoded, hashlib.sha256).digest()):
                raise ValueError('invalid_signature')
            payload = json.loads(encoded)
            if not isinstance(payload, dict) or not hmac.compare_digest(encoded, _canonical(payload)):
                raise ValueError('noncanonical_payload')
            if set(payload) != {'version', 'kind', 'binding', 'generation_id', 'offset'}:
                raise ValueError('invalid_fields')
            binding = _canonical({'actor_id': actor_id, 'board_id': board_id, 'spec_id': spec_id,
                'edition': edition, 'card_id': card_id, 'limit': limit})
            if (type(payload['version']) is not int or payload['version'] != 1
                or payload['kind'] != 'persisted-readiness'
                or not hmac.compare_digest(payload['binding'], hashlib.sha256(binding).hexdigest())
                or not isinstance(payload['generation_id'], str) or len(payload['generation_id']) != 32
                or any(ch not in '0123456789abcdef' for ch in payload['generation_id'])
                or type(payload['offset']) is not int or payload['offset'] < 1):
                raise ValueError('invalid_binding')
            return payload['generation_id'], payload['offset']
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
            raise ReadinessReadError('readiness_query_invalid', native_code='readiness_cursor_invalid') from exc
