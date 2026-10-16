"""BASE T11 uses Community semantic sessions, not Core's frozen ORM fixtures."""
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import test_adopted_delivery_report as adopted
from okto_pulse.community.adapters.sqlalchemy_models import ActivityLog, Card, DomainEventRow, Spec
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import CommunitySemanticSession
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.schemas import CardMove, CardUpdate
from okto_pulse.core.services.main import CardOperationError, CardService

ledger = adopted.ledger


@pytest.mark.asyncio
async def test_review_rechecks_human_policy_changed_after_board_read(ledger, tmp_path, monkeypatch):
    """ADV-17: a completed human policy update cannot leave an in-flight grant alive."""
    from copy import deepcopy
    from okto_pulse.community.adapters.sqlalchemy_models import Board
    from okto_pulse.core.models.schemas import BoardUpdate
    from okto_pulse.core.services import main

    seed, _, _ = await adopted.setup(ledger, tmp_path, monkeypatch)
    factory = async_sessionmaker(seed.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={'realm_scope': RealmScope.local()})
    try:
        board = await seed.get(Board, adopted.BOARD)
        board.settings = {**board.settings, 'reviewer_separation_mode': 'off'}
        card = await seed.get(Card, 'task')
        card.status, card.created_by, card.assignee_id = 'validation', 'owner', 'owner'
        await seed.commit()
        version = card.policy_version
        await seed.close()
        original_get = main._application_get
        changed = False
        counts = None

        async with factory() as reviewer:
            async def interleave(context, entity, identity, **kwargs):
                nonlocal changed, counts
                result = await original_get(context, entity, identity, **kwargs)
                if context is reviewer and entity == 'board' and not changed:
                    changed = True
                    assert result.settings['reviewer_separation_mode'] == 'off'
                    async with factory() as human:
                        updated = await main.BoardService(human).update_board(adopted.BOARD, 'owner',
                            BoardUpdate(settings={'reviewer_separation_mode': 'enforce'}))
                        assert updated.settings['reviewer_separation_mode'] == 'enforce'
                        await human.commit()
                        counts = [await human.scalar(select(func.count()).select_from(model))
                                  for model in (ActivityLog, DomainEventRow)]
                    # The already-read request snapshot remains stale.
                    assert result.settings['reviewer_separation_mode'] == 'off'
                return result

            monkeypatch.setattr(main, '_application_get', interleave)
            before = deepcopy((await reviewer.get(Card, 'task')).conclusions)
            with pytest.raises(CardOperationError) as denied:
                await CardService(reviewer).submit_task_validation('task', 'owner', 'Owner', {
                    'expected_subject_version': version, 'idempotency_key': 'policy-race',
                    'confidence': 95, 'confidence_justification': 'Inspected work',
                    'estimated_completeness': 100, 'completeness_justification': 'Complete scope',
                    'estimated_drift': 0, 'drift_justification': 'Within scope',
                    'general_justification': 'Self-review submitted before the policy change',
                    'recommendation': 'reject',
                })
            assert changed and denied.value.code == 'reviewer_separation_required'
            await reviewer.commit()
        async with factory() as observer:
            current = await observer.get(Card, 'task')
            assert current.status == 'validation' and current.policy_version == version
            assert not current.validations and not current.rejection_records
            assert current.conclusions == before
            assert (await observer.get(Board, adopted.BOARD)).settings['reviewer_separation_mode'] == 'enforce'
            assert [await observer.scalar(select(func.count()).select_from(model))
                    for model in (ActivityLog, DomainEventRow)] == counts
    finally:
        await seed.close()


@pytest.mark.asyncio
async def test_review_cannot_approve_subject_edited_in_another_semantic_session(ledger, tmp_path, monkeypatch):
    seed, _, _ = await adopted.setup(ledger, tmp_path, monkeypatch)
    factory = async_sessionmaker(seed.bind, sync_session_class=CommunitySemanticSession,
        expire_on_commit=False, info={'realm_scope': RealmScope.local()})
    try:
        card = await seed.get(Card, 'task')
        card.status = 'validation'
        await seed.commit()
        async with factory() as reader:
            reviewed_version = (await reader.get(Card, 'task')).policy_version
        async with factory() as editor:
            await CardService(editor).update_card('task', 'owner',
                CardUpdate(title='Changed after the reviewer read the subject'))
            await editor.commit()
            current = await editor.get(Card, 'task')
            assert current.policy_version > reviewed_version
            version = current.policy_version
            counts = [await editor.scalar(select(func.count()).select_from(model))
                      for model in (ActivityLog, DomainEventRow)]
        async with factory() as reviewer:
            with pytest.raises(CardOperationError) as conflict:
                await CardService(reviewer).submit_task_validation('task', 'owner', 'Owner', {
                    'expected_subject_version': reviewed_version, 'idempotency_key': 'stale-review',
                    'confidence': 95, 'confidence_justification': 'Inspected prior content',
                    'estimated_completeness': 100, 'completeness_justification': 'Prior scope complete',
                    'estimated_drift': 0, 'drift_justification': 'Prior scope unchanged',
                    'general_justification': 'Assessment prepared before concurrent edit',
                    'recommendation': 'approve',
                })
            assert conflict.value.code == 'task_validation_subject_version_conflict'
            await reviewer.commit()
        async with factory() as observer:
            current = await observer.get(Card, 'task')
            assert current.status == 'validation' and current.policy_version == version
            assert current.title == 'Changed after the reviewer read the subject'
            assert not current.validations and not current.rejection_records
            assert [await observer.scalar(select(func.count()).select_from(model))
                    for model in (ActivityLog, DomainEventRow)] == counts
    finally:
        await seed.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['normal', 'bug'])
@pytest.mark.parametrize('status', ['in_progress', 'validation'])
async def test_direct_done_preserves_required_task_validation(ledger, tmp_path, monkeypatch, kind, status):
    """BASE T08: normal/Bug cannot replace required review with a move."""
    session, uow, _ = await adopted.setup(ledger, tmp_path, monkeypatch)
    try:
        card = await session.get(Card, 'task')
        card.card_type, card.status = kind, status
        spec = await session.get(Spec, adopted.SPEC)
        spec.require_task_validation = True
        await session.commit()
        before = card.policy_version
        with pytest.raises(ValueError, match='submit_task_validation'):
            await uow.services.cards.move_card('task', 'owner', CardMove(
                status='done', conclusion='Attempt direct completion', completeness=100,
                completeness_justification='All implementation present', drift=0,
                drift_justification='Within scope'))
        await session.commit()
        await session.refresh(card)
        assert card.status == status and card.policy_version == before
        assert not card.validations and not card.conclusions
    finally:
        await session.close()
