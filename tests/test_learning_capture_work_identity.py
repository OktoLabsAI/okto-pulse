"""At-least-once capture delivery preserves distinct work and human history."""
import pytest

from okto_pulse.core.events.handlers.learning_capture import LearningCaptureMaterializationEnqueuer
from okto_pulse.core.events.types import LearningCaptureAdmitted
from test_learning_materialization_worker import work_store as _work_store

work_store = _work_store
pytestmark = pytest.mark.asyncio


def capture(letter):
    return LearningCaptureAdmitted(board_id='work-board', bug_id='same-bug', capture_author_id='author',
        capture={'learning_id': 'same-learning', 'generation': 1, 'fingerprint': letter * 64})


@pytest.mark.parametrize('status', ['pending', 'in_progress', 'failed', 'consolidated', 'skipped'])
async def test_new_capture_is_independent_and_replay_preserves_all_old_decisions(work_store, status):
    store, _ = work_store
    first, second = capture('a'), capture('b')
    handler = LearningCaptureMaterializationEnqueuer()
    await handler.handle(first, None)
    generation = store.latest_generation(first.board_id)
    original, = store.list_items(first.board_id, generation)
    held = store.update_item(board_id=first.board_id, kg_generation_id=generation, item_id=original.item_id,
        new_status=status, updated_by_agent_id='human-reviewer', reason='Preserve existing decision',
        reason_code='awaiting_evidence', justification='Review remains required', actor='human-reviewer',
        evidence_refs=['existing:evidence'])
    await handler.handle(first, None)
    assert store.list_items(first.board_id, generation) == [held]
    await handler.handle(second, None)
    items = store.list_items(first.board_id, generation)
    assert len(items) == 2 and held in items
    new, = [item for item in items if item.content_hash == second.capture.fingerprint]
    assert new.status == 'pending' and new.item_id != held.item_id
    assert new.artifact_id == held.artifact_id
    assert ':capture-v2:' in new.source_ref
    # Out-of-order redelivery cannot erase either item or the older hold.
    await handler.handle(first, None)
    await handler.handle(second, None)
    assert store.list_items(first.board_id, generation) == items
