"""REST/MCP annotation parity with simultaneous technical and cognitive debt."""
from datetime import datetime, timedelta, timezone

import pytest

from okto_pulse.core.kg.cognitive_readiness import compose_readiness
from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItem
from okto_pulse.core.mcp.server import _would_block_done as mcp_annotation
from okto_pulse.community.api.cognitive_action_center import _would_block_done as rest_annotation


@pytest.mark.parametrize('technical', ['dlq', 'debt'])
@pytest.mark.parametrize('expired', [False, True])
@pytest.mark.parametrize('enforced', [False, True])
def test_projection_cannot_hide_expired_skip_in_transport_annotations(technical, expired, enforced):
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    items = [CognitiveConsolidationItem(
        item_id='item', board_id='board', kg_generation_id='generation',
        source_ref='bug:bug', artifact_type='bug', status='skipped',
        reason_code='evidence_insufficient', recorded_at=now.isoformat(),
        revisit_at=(now + timedelta(seconds=-1 if expired else 60)).isoformat(),
    )]
    verdict = compose_readiness(artifact_id='card:bug', technical_dlq=technical == 'dlq',
        canonical_debt_open=technical == 'debt', cognitive_items=items, now=now)
    assert verdict.blocking  # Technical signal remains visible.
    expected = expired and enforced
    assert rest_annotation(verdict, enforced) is expected
    assert mcp_annotation(verdict, enforced) is expected
    assert mcp_annotation(verdict.to_api(), enforced) is expected
