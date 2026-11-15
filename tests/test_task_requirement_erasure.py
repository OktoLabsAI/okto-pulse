"""Task decisions remain immutable until the existing scoped Board erasure."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
import okto_pulse.community.app as _community_app  # noqa: F401

from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_models import Board, BoardErasurePermit, TaskRequirementDecisionRow as Decision, TaskRequirementDecisionOperationRow as Operation
from okto_pulse.community.adapters.sqlalchemy_task_requirement_decisions import CommunityTaskRequirementDecisionStore
from okto_pulse.community.adapters.sqlalchemy_kg_governance import CommunitySqlAlchemyKGGovernanceStore, BoardRelationalErasureError
from okto_pulse.core.domain.task_requirement_decision import TaskRequirementDecisionRecord, TaskRequirementScope


@pytest.fixture
async def session(tmp_path):
    engine=create_async_engine('sqlite+aiosqlite:///' + str(tmp_path/'erase.db'))
    await initialize_current_schema(engine,current_schema_contract())
    async with build_community_session_factory(engine)() as session:
        for board in ['erase','keep']:
            session.add(Board(id=board,realm_id='local',name=board,owner_id='owner'))
        await session.commit()
        store=CommunityTaskRequirementDecisionStore(session)
        for board in ['erase','keep']:
            record=TaskRequirementDecisionRecord(id='decision-'+board,operation_id='op-'+board,
                scope=TaskRequirementScope(board,'spec',1,'task','normal',(('fr:scope','a'*64,'b'*64),)),
                revision=1,decision='waived',previous_decision='required',supersedes=None,
                actor_id='owner',recorded_at=datetime.now(timezone.utc),user_note='Private note for '+board)
            await store.append(operation_id=record.operation_id,board_id=board,spec_id='spec',edition=1,
                actor_id='owner',idempotency_key='key',request_sha256='c'*64,response={'decision_ids':[record.id]},records=[record])
        await session.commit()
        yield session
    await engine.dispose()


async def test_governed_erasure_removes_only_target_board_history_and_releases_permit(session):
    with pytest.raises(IntegrityError,match='immutable'):
        await session.execute(delete(Decision).where(Decision.board_id=='erase'))
    await session.rollback()
    original=(await session.get(Decision,'decision-keep')).payload.copy()
    await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(session,board_id='erase')
    await session.execute(delete(Board).where(Board.id=='erase'))
    await session.commit()
    assert await session.get(Decision,'decision-erase') is None
    assert await session.get(Operation,'op-erase') is None
    assert (await session.get(Decision,'decision-keep')).payload==original
    assert await session.get(Operation,'op-keep') is not None
    assert (await session.scalars(select(BoardErasurePermit))).all()==[]
    assert (await session.execute(text('PRAGMA foreign_key_check'))).all()==[]


async def test_foreign_permit_does_not_allow_task_history_mutation(session):
    session.add(BoardErasurePermit(board_id='keep',permit_token='keep-only'))
    await session.flush()
    with pytest.raises(IntegrityError,match='immutable'):
        await session.execute(delete(Decision).where(Decision.board_id=='erase'))
    await session.rollback()
    assert await session.get(Decision,'decision-erase') is not None
    assert await session.get(Decision,'decision-keep') is not None


async def test_silently_retained_operation_blocks_erasure_and_rollback_preserves_history(session):
    await session.execute(text("CREATE TRIGGER retain_one_operation BEFORE DELETE ON task_requirement_decision_operations WHEN OLD.board_id='erase' BEGIN SELECT RAISE(IGNORE); END"))
    with pytest.raises(BoardRelationalErasureError,match='task_requirement_decision_operations'):
        await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(session,board_id='erase')
    await session.rollback()
    for board in ['erase','keep']:
        assert await session.get(Decision,'decision-'+board) is not None
        assert await session.get(Operation,'op-'+board) is not None
    assert (await session.scalars(select(BoardErasurePermit))).all()==[]
