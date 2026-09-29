"""Conditional work updates preserve changes from another store instance."""
from uuid import uuid4

from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
from okto_pulse.core.kg.cognitive_closeout_production import open_cognitive_closeout_pending
from okto_pulse.core.kg.rebuild_audit import CognitiveConsolidationItemStore


def test_stale_worker_cannot_overwrite_human_item(tmp_path):
    store = CognitiveConsolidationItemStore(artifact_store=CommunityFileSystemRebuildAuditArtifactStore(tmp_path))
    other = CognitiveConsolidationItemStore(artifact_store=CommunityFileSystemRebuildAuditArtifactStore(tmp_path))
    generation = str(uuid4())
    open_cognitive_closeout_pending(board_id='board', source_ref='bug:bug', artifact_type='bug',
        kg_generation_id=generation, store=store)
    original, = store.list_items('board', generation)
    protected = other.update_item(board_id='board', kg_generation_id=generation, item_id=original.item_id,
        new_status='skipped', updated_by_agent_id='human', reason='Explicit decision')
    assert store.update_item(board_id='board', kg_generation_id=generation, item_id=original.item_id,
        new_status='in_progress', updated_by_agent_id='worker', expected_item=original) is None
    assert store.list_items('board', generation) == [protected]


def test_change_during_atomic_replace_preserves_unrelated_item(tmp_path):
    other = CognitiveConsolidationItemStore(artifact_store=CommunityFileSystemRebuildAuditArtifactStore(tmp_path))
    class Interleaved(CommunityFileSystemRebuildAuditArtifactStore):
        before_replace = None
        def replace_json_with_revision(self, **kwargs):
            callback, self.before_replace = self.before_replace, None
            if callback:
                callback()
            return super().replace_json_with_revision(**kwargs)
    adapter = Interleaved(tmp_path)
    store = CognitiveConsolidationItemStore(artifact_store=adapter)
    generation = str(uuid4())
    for identity in ('one', 'two'):
        open_cognitive_closeout_pending(board_id='board', source_ref='bug:' + identity, artifact_type='bug',
            kg_generation_id=generation, store=store)
    by_ref = {item.source_ref: item for item in store.list_items('board', generation)}
    original, second = by_ref['bug:one'], by_ref['bug:two']
    def intervene():
        other.update_item(board_id='board', kg_generation_id=generation, item_id=second.item_id,
            new_status='skipped', updated_by_agent_id='human', reason='Concurrent decision')
    adapter.before_replace = intervene
    assert store.update_item(board_id='board', kg_generation_id=generation, item_id=original.item_id,
        new_status='in_progress', updated_by_agent_id='worker', expected_item=original) is None
    actual = {item.source_ref: item for item in store.list_items('board', generation)}
    assert actual['bug:one'] == original
    assert actual['bug:two'].status == 'skipped' and actual['bug:two'].reason == 'Concurrent decision'
