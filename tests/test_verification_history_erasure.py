"""Verification evidence is immutable except for scoped governed Board erasure."""

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

import okto_pulse.community.app as _community_app  # noqa: F401
from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_database import build_community_session_factory
from okto_pulse.community.adapters.sqlalchemy_kg_governance import CommunitySqlAlchemyKGGovernanceStore, BoardRelationalErasureError
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board, BoardErasurePermit, VerificationAcceptanceRow as Acceptance,
    VerificationBasisHeadRow as Head, VerificationReservationRow as Reservation,
    VerificationScenarioStateRow as State,
)


MODELS = (Head, Acceptance, Reservation, State)


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'erasure.db'))
    await initialize_current_schema(engine, current_schema_contract())
    async with build_community_session_factory(engine)() as session:
        await session.execute(text('PRAGMA foreign_keys=ON'))
        for board in ('erase', 'keep'):
            session.add(Board(id=board, realm_id='local', name=board, owner_id='owner'))
        await session.flush()
        for board in ('erase', 'keep'):
            scope = dict(board_id=board, spec_id='deleted-spec', spec_edition=1, scenario_id='scenario')
            await session.execute(insert(State).values(**scope, generation=1, last_ordinal=1,
                summary_ordinal=1, revision=1, scenario_sha256='a' * 64,
                verification_method='automated_test', deleted=True))
            await session.execute(insert(Reservation).values(**scope, id='reservation-' + board,
                generation=1, ordinal=1, context={'private': board}, actor_id='owner', actor_kind='human'))
            await session.execute(insert(Acceptance).values(**scope, id='acceptance-' + board,
                reservation_id='reservation-' + board, generation=1, ordinal=1, basis_digest='b' * 64,
                receipt_id='receipt-' + board, result={'private': board}, evidence={'private': board},
                evidence_status='passed', origin='native'))
            await session.execute(insert(Head).values(**scope, generation=1, basis_digest='b' * 64,
                ordinal=1, accepted_result_id='acceptance-' + board))
        await session.commit()
        yield session
    await engine.dispose()


async def snapshot(session, board):
    return {model.__tablename__: [dict(row) for row in
        (await session.execute(select(model.__table__).where(model.board_id == board))).mappings()]
        for model in MODELS}


@pytest.mark.parametrize('model', [Reservation, Acceptance])
@pytest.mark.parametrize('operation', ['update', 'delete'])
@pytest.mark.parametrize('permit', [None, 'keep', 'erase'])
async def test_history_guards_require_exact_board_permit_only_for_delete(session, model, operation, permit):
    if operation == 'delete' and permit == 'erase':
        # Dependency children are removed first even inside authorized erasure.
        await session.execute(delete(Head).where(Head.board_id == 'erase'))
        if model is Reservation:
            session.add(BoardErasurePermit(board_id='erase', permit_token='scoped'))
            await session.flush()
            await session.execute(delete(Acceptance).where(Acceptance.board_id == 'erase'))
        else:
            session.add(BoardErasurePermit(board_id='erase', permit_token='scoped'))
            await session.flush()
        await session.execute(delete(model).where(model.board_id == 'erase'))
        assert (await session.execute(select(model).where(model.board_id == 'erase'))).first() is None
    else:
        if permit:
            session.add(BoardErasurePermit(board_id=permit, permit_token='scoped'))
            await session.flush()
        statement = (update(model).values(ordinal=2) if operation == 'update' else delete(model))
        with pytest.raises(IntegrityError, match='verification_history_immutable'):
            await session.execute(statement.where(model.board_id == 'erase'))
    await session.rollback()
    assert all((await snapshot(session, 'erase')).values())


async def test_governed_erasure_clears_history_preserves_other_board_and_releases_permit(session):
    other = await snapshot(session, 'keep')
    await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(session, board_id='erase')
    await session.execute(delete(Board).where(Board.id == 'erase'))
    await session.commit()
    assert not any((await snapshot(session, 'erase')).values())
    assert await snapshot(session, 'keep') == other
    assert (await session.scalars(select(BoardErasurePermit))).all() == []
    assert (await session.execute(text('PRAGMA foreign_key_check'))).all() == []


async def test_retained_state_blocks_erasure_and_rollback_preserves_history(session):
    original = await snapshot(session, 'erase')
    await session.execute(text("CREATE TRIGGER retain_verification_state BEFORE DELETE ON verification_scenario_states WHEN OLD.board_id='erase' BEGIN SELECT RAISE(IGNORE); END"))
    with pytest.raises(BoardRelationalErasureError, match='verification_scenario_states'):
        await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(session, board_id='erase')
    await session.rollback()
    assert await snapshot(session, 'erase') == original
    assert (await session.scalars(select(BoardErasurePermit))).all() == []
