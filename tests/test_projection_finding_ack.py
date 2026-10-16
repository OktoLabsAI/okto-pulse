"""New diagnostic receipts bind contents while preserving historical ACK digests."""
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.sqlalchemy_consolidation import _canonical_node_refs_sha256, _EXACT_NODE_REFS_DIGEST_DOMAIN
from okto_pulse.core.ports.projection_findings import ProjectionFindingSnapshot, ProjectionReferenceFinding


def audit():
    return SimpleNamespace(agent_id='system:historical_consolidation', artifact_id='card',
        artifact_type='card', content_hash='c' * 64, board_id='board', session_id='session',
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc), committed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        nodes_added=0, nodes_updated=0, nodes_superseded=0, edges_added=0, summary_text=None)


def snapshot():
    return ProjectionFindingSnapshot('board', 'card', 'card', 'card_scenarios', 'a' * 64,
        (ProjectionReferenceFinding('board', 'card', 'card', 'card_scenarios',
         'card:card:test_scenario_ids', 'spec:spec:test_scenario:missing', 'target_absent'),))


@pytest.mark.parametrize('effects', [None, 'b' * 64])
def test_absent_snapshot_preserves_exact_historical_wire(effects):
    row = audit()
    payload = dict(agent_id=row.agent_id, artifact_id='card', artifact_type='card',
        audit_content_hash='c' * 64, board_id='board', committed_at=row.committed_at.isoformat(),
        edges_added=0, nodes_added=0, nodes_superseded=0, nodes_updated=0, refs=[],
        schema='exact_consolidation_node_refs.v1', session_id='session',
        started_at=row.started_at.isoformat(), summary_text=None)
    if effects is not None:
        payload.update(schema='exact_consolidation_node_refs.v2', projection_property_effects_sha256=effects)
    rendered = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    expected = hashlib.sha256(_EXACT_NODE_REFS_DIGEST_DOMAIN + rendered).hexdigest()
    assert _canonical_node_refs_sha256(audit=row, refs=[], projection_effects_sha256=effects) == expected
    row.reference_findings = None
    assert _canonical_node_refs_sha256(audit=row, refs=[], projection_effects_sha256=effects) == expected


def test_digest_binds_reason_even_when_finding_identity_is_stable():
    row = audit()
    old = _canonical_node_refs_sha256(audit=row, refs=[])
    row.reference_findings = snapshot().to_payload()
    original = _canonical_node_refs_sha256(audit=row, refs=[])
    assert original != old
    identity = row.reference_findings['findings'][0]['finding_id']
    row.reference_findings['findings'][0]['reason_code'] = 'parent_absent'
    assert row.reference_findings['findings'][0]['finding_id'] == identity
    assert _canonical_node_refs_sha256(audit=row, refs=[]) != original
    row.agent_id = 'cognitive_closeout_worker'
    with pytest.raises(ValueError, match='audit_scope_invalid'):
        _canonical_node_refs_sha256(audit=row, refs=[])
