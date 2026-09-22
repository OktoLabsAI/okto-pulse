"""Recovery captures every SQL debt page without changing persisted records."""

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_canonical_debt import CommunitySqlAlchemyCanonicalDebtStore
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, CanonicalDebt
from okto_pulse.core.composition import isolated_runtime_provider_scope
from okto_pulse.core.kg.canonical_learning_partition import HISTORICAL_DEBT_REASON
from okto_pulse.core.ports.canonical_debt import register_canonical_debt_store
from okto_pulse.core.ports.global_discovery_recovery_control import GlobalDiscoveryRecoveryBoardSeedInputService


@pytest.mark.asyncio
async def test_recovery_seed_preserves_all_201_sql_exclusions_and_board_scope(tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "debt.sqlite3"}')
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: Base.metadata.create_all(sync,
                tables=[Board.__table__, CanonicalDebt.__table__]))
            await connection.execute(insert(Board), [{'id': owner, 'name': owner, 'owner_id': 'agent',
                'realm_id': 'local'} for owner in ('board', 'other')])
            await connection.execute(insert(CanonicalDebt), [{'id': f'debt-{index:03}',
                'board_id': 'board' if index < 201 else 'other', 'artifact_type': 'spec',
                'artifact_id': f'source-{index}', 'source_ref': f'spec:source-{index}',
                'content_hash': 'a' * 64, 'target_status': 'canonical_learning_partition_integrity',
                'canonical_state': 'pending', 'failure_reason': HISTORICAL_DEBT_REASON}
                for index in range(202)])
        with isolated_runtime_provider_scope(inherit=False):
            register_canonical_debt_store(CommunitySqlAlchemyCanonicalDebtStore())
            async with AsyncSession(engine) as session:
                before = (await session.execute(select(CanonicalDebt.__table__).order_by(CanonicalDebt.id))).all()
                captured = await GlobalDiscoveryRecoveryBoardSeedInputService().capture_board_seed_input(
                    session, board_id='board', board_name='Board', board_summary='',
                    captured_cognitive_pending_exclusions={})
                after = (await session.execute(select(CanonicalDebt.__table__).order_by(CanonicalDebt.id))).all()
                assert before == after
                assert dict(captured.overlay_exclusions) == {
                    f'spec:source-{index}': HISTORICAL_DEBT_REASON for index in range(201)}
                await session.rollback()
    finally:
        await engine.dispose()
