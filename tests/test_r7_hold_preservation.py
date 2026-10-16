"""KG-37: a fresh technical R7 observation cannot overwrite owned history."""
import pytest

from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
from okto_pulse.core.kg.connectivity_guard import CANONICAL_LEARNING_WORKING_ONLY_REASON
from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItemStore, record_cognitive_working_only_hold


@pytest.mark.parametrize('status', ['pending', 'in_progress', 'skipped', 'consolidated'])
def test_technical_hold_does_not_replace_existing_substantive_or_terminal_state(tmp_path, status):
    artifacts = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    store = CognitiveConsolidationItemStore(artifact_store=artifacts)
    store.materialize_from_marker(board_id='board', kg_generation_id='generation',
        event_ref='historical', source_set=[{'source_ref': 'bug:source', 'artifact_type': 'bug'}])
    item, = store.list_items('board', 'generation')
    previous = store.update_item(board_id='board', kg_generation_id='generation', item_id=item.item_id,
        new_status=status, updated_by_agent_id='human-reviewer', reason_code='authority_denied',
        reason='Preserve the independent authority review', justification='A substantive restriction',
        actor='human:reviewer', evidence_refs=['review:original'], consolidation_session_id='prior-session')
    result = record_cognitive_working_only_hold(board_id='board', actor_id='projection-worker',
        artifact_store=artifacts, hold_payload={'source_ref': 'bug:source', 'artifact_type': 'bug',
            'session_id': 'technical-session', 'reason_code': CANONICAL_LEARNING_WORKING_ONLY_REASON})
    actual, = store.list_items('board', 'generation')
    assert actual == previous
    assert result is None


def test_fresh_technical_hold_is_atomic_and_replay_preserves_the_original_session(tmp_path):
    artifacts = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    payload = {'source_ref': 'bug:source', 'artifact_type': 'bug', 'session_id': 'first-session',
        'reason_code': CANONICAL_LEARNING_WORKING_ONLY_REASON}
    first = record_cognitive_working_only_hold(board_id='board', actor_id='worker',
        artifact_store=artifacts, hold_payload=payload)
    assert first is not None
    store = CognitiveConsolidationItemStore(artifact_store=artifacts)
    before = store.load_record('board', first['generation_id'])
    second = record_cognitive_working_only_hold(board_id='board', actor_id='different-worker',
        artifact_store=artifacts, hold_payload={**payload, 'session_id': 'later-session'})
    assert second is None
    assert store.load_record('board', first['generation_id']) == before
    third = record_cognitive_working_only_hold(board_id='board', actor_id='worker',
        artifact_store=artifacts, hold_payload={**payload, 'source_ref': 'bug:another'})
    assert third is not None
    after = store.load_record('board', first['generation_id'])
    assert before['items'][0] in after['items']
    assert after['pending_count'] == 2


def test_concurrent_substantive_write_wins_before_the_atomic_hold_transform(tmp_path, monkeypatch):
    artifacts = CommunityFileSystemRebuildAuditArtifactStore(tmp_path)
    original = CommunityFileSystemRebuildAuditArtifactStore.replace_json_with_revision
    inserted = []

    def insert_review(self, *, key, transform, revision_key, revision_transition):
        if key.namespace == 'cognitive_pending' and not inserted:
            inserted.append(None)
            store = CognitiveConsolidationItemStore(artifact_store=artifacts)
            store.materialize_from_marker(board_id=key.board_id, kg_generation_id=key.kg_generation_id,
                event_ref='concurrent-review', source_set=[{'source_ref': 'bug:source', 'artifact_type': 'bug'}])
            row, = store.list_items(key.board_id, key.kg_generation_id)
            inserted[0] = store.update_item(board_id=key.board_id, kg_generation_id=key.kg_generation_id,
                item_id=row.item_id, new_status='pending', updated_by_agent_id='human-reviewer',
                reason_code='authority_denied', reason='Concurrent substantive review')
        return original(self, key=key, transform=transform,
            revision_key=revision_key, revision_transition=revision_transition)

    monkeypatch.setattr(CommunityFileSystemRebuildAuditArtifactStore, 'replace_json_with_revision', insert_review)
    result = record_cognitive_working_only_hold(board_id='board', actor_id='worker',
        artifact_store=artifacts, hold_payload={'source_ref': 'bug:source', 'artifact_type': 'bug',
            'session_id': 'technical-session', 'reason_code': CANONICAL_LEARNING_WORKING_ONLY_REASON})
    assert result is None
    store = CognitiveConsolidationItemStore(artifact_store=artifacts)
    assert store.list_items('board', store.latest_generation('board')) == inserted
