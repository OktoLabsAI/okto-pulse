from datetime import datetime, timezone
from dataclasses import replace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.exc import IntegrityError
from okto_pulse.community.adapters.sqlalchemy_models import (
    TaskRequirementDecisionOperationRow as Operation, TaskRequirementDecisionRow as Decision,
)
from okto_pulse.community.adapters.sqlalchemy_task_requirement_decisions import CommunityTaskRequirementDecisionStore
from okto_pulse.core.domain.task_requirement_decision import TaskRequirementScope, TaskRequirementDecisionRecord


def decision(identity='a', **changes):
    return TaskRequirementDecisionRecord(**(dict(id='decision-' + identity, operation_id='op1',
        scope=TaskRequirementScope('board', 'spec', 1, identity, 'normal', (('fr:shared', 'a' * 64, 'b' * 64),)),
        revision=1, decision='waived', previous_decision='required', supersedes=None,
        actor_id='user', recorded_at=datetime.now(timezone.utc), user_note=None) | changes))


@pytest.fixture
async def db(tmp_path):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'decisions.db'))
    async with engine.begin() as connection:
        # Exercise an additive upgrade without rewriting existing ledger data.
        await connection.execute(text('CREATE TABLE card_delivery_evidence_records (id TEXT PRIMARY KEY, payload TEXT NOT NULL)'))
        await connection.execute(text("INSERT INTO card_delivery_evidence_records VALUES ('old', 'immutable legacy bytes')"))
        for _ in range(2):
            await connection.run_sync(lambda sync: Operation.metadata.create_all(sync, tables=[Operation.__table__, Decision.__table__]))
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session, CommunityTaskRequirementDecisionStore(session)
    await engine.dispose()


async def append(store, records, **changes):
    args = dict(operation_id='op1', board_id='board', spec_id='spec', edition=1, actor_id='user',
        idempotency_key='key', request_sha256='f' * 64, response={'operation_id': 'op1'}, records=records)
    await store.append(**(args | changes))


async def test_round_trip_is_scoped_and_does_not_touch_legacy_ledger(db):
    session, store = db
    first, other = decision(), decision('b')
    await append(store, [first, other])
    await session.commit()
    history = await store.history(board_id='board', spec_id='spec', edition=1, card_ids=['a', 'b'])
    assert history == {'a': (first,), 'b': (other,)}
    assert await store.history(board_id='foreign', spec_id='spec', edition=1, card_ids=['a']) == {'a': ()}
    assert await store.history(board_id='board', spec_id='spec', edition=2, card_ids=['a']) == {'a': ()}
    operation = await store.operation(board_id='board', spec_id='spec', actor_id='user', idempotency_key='key')
    assert operation.request_sha256 == 'f' * 64
    assert await store.operation(board_id='board', spec_id='spec', actor_id='other', idempotency_key='key') is None
    assert await session.scalar(text("SELECT payload FROM card_delivery_evidence_records WHERE id='old'")) == 'immutable legacy bytes'


async def test_failure_halfway_rolls_back_the_complete_batch_even_if_caller_commits(db):
    session, store = db
    async with session.begin_nested():
        await session.execute(text("CREATE TRIGGER fail_second_decision BEFORE INSERT ON task_requirement_decisions WHEN NEW.card_id='b' BEGIN SELECT RAISE(ABORT, 'injected_failure'); END"))
    await session.commit()
    with pytest.raises(IntegrityError, match='injected_failure'):
        await append(store, [decision(), decision('b')])
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Operation)) == 0
    assert await session.scalar(select(func.count()).select_from(Decision)) == 0


@pytest.mark.parametrize('table', ['task_requirement_decisions', 'task_requirement_decision_operations'])
async def test_audit_rows_reject_update_and_delete(db, table):
    session, store = db
    await append(store, [decision()])
    await session.commit()
    for statement in [f'UPDATE {table} SET id=id', f'DELETE FROM {table}']:
        with pytest.raises(IntegrityError, match='task_requirement_decision_immutable'):
            async with session.begin_nested():
                await session.execute(text(statement))
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Decision)) == 1


async def test_foreign_actor_or_scope_cannot_be_mixed_into_one_operation(db):
    session, store = db
    with pytest.raises(ValueError, match='batch_invalid'):
        await append(store, [decision(), decision('b', actor_id='other')])
    await session.commit()
    assert await session.scalar(select(func.count()).select_from(Operation)) == 0
