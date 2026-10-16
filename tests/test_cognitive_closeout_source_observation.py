"""BASE T39/T40: source readability, not graph health, governs cognitive closeout."""
import pytest

from okto_pulse.core.kg.cognitive_closeout_gate import CognitiveCloseoutGate
from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItemStore
from okto_pulse.core.kg.interfaces.rebuild_audit_storage import RebuildAuditKey
from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore


@pytest.mark.parametrize('graph_state', ['healthy', 'recovery_needed', 'quarantined', None])
@pytest.mark.parametrize('source_state', ['empty', 'pending', 'corrupt'])
def test_closeout_reads_authoritative_source_without_graph_inference(tmp_path, graph_state, source_state):
    adapter = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    key = RebuildAuditKey(namespace='cognitive_pending', board_id='board', kg_generation_id='generation')
    if source_state != 'empty':
        adapter.write_json_atomic(key, {
            'board_id': 'board', 'kg_generation_id': 'generation',
            'recorded_at': '2026-09-30T00:00:00Z', 'pending_refs': ['spec:spec'],
        })
        path = tmp_path / 'rebuild/audit/cognitive_pending/board/generation.json'
        if source_state == 'corrupt':
            path.write_text('{ incomplete', encoding='utf-8')
    before = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    gate = CognitiveCloseoutGate(store=CognitiveConsolidationItemStore(artifact_store=adapter))
    result = gate.evaluate(board_id='board', entity_type='spec', entity_id='spec',
        target_status='done', graph_state=graph_state)
    assert result.allowed is (source_state == 'empty'), (source_state, graph_state, result)
    if source_state == 'pending':
        assert result.blocking_count == 1 and result.reason == 'cognitive_consolidation_pending'
    elif source_state == 'corrupt':
        assert result.reason == 'cognitive_status_unavailable'
    after = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert after == before


@pytest.mark.parametrize('failure', ['provider', 'missing_generation', 'missing_capability'])
def test_incomplete_source_observation_never_falls_back_to_graph_or_ordinary_reads(tmp_path, monkeypatch, failure):
    adapter = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    if failure == 'provider':
        def unavailable(*args, **kwargs):
            raise PermissionError('source unavailable')
        monkeypatch.setattr(adapter, 'observe_health_json', unavailable)
    elif failure == 'missing_capability':
        monkeypatch.setattr(adapter, 'observe_health_json', None)
    for name in ('read_json', 'list_json'):
        monkeypatch.setattr(adapter, name, lambda *args: pytest.fail('best-effort fallback'))
    gate = CognitiveCloseoutGate(store=CognitiveConsolidationItemStore(artifact_store=adapter))
    result = gate.evaluate(board_id='board', entity_type='spec', entity_id='spec', target_status='done',
        kg_generation_id='lost-generation' if failure == 'missing_generation' else None,
        graph_state='healthy')
    assert not result.allowed and result.reason == 'cognitive_status_unavailable'
