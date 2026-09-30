"""Recovery captures every SQL debt page without changing persisted records."""

import json

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from okto_pulse.community.adapters.sqlalchemy_canonical_debt import CommunitySqlAlchemyCanonicalDebtStore
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, CanonicalDebt
from okto_pulse.community.adapters.retirement_candidate_global_sources import (
    capture_candidate_global_source_inputs, initialize_candidate_global_overlay,
)
from okto_pulse.core.composition import isolated_runtime_provider_scope
from okto_pulse.core.kg.canonical_learning_partition import HISTORICAL_DEBT_REASON
from okto_pulse.core.ports.canonical_debt import register_canonical_debt_store
from okto_pulse.core.ports.global_discovery_recovery_control import (
    CognitivePendingOverlaySnapshotError, GlobalDiscoveryRecoveryBoardSeedInputService,
)


@pytest.mark.asyncio
async def test_recovery_seed_preserves_all_201_sql_exclusions_and_board_scope(tmp_path):
    engine = create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "database.sqlite3"}')
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
        fingerprint = initialize_candidate_global_overlay(tmp_path, require_live=lambda: True, max_seconds=60)
        orphan = tmp_path / 'kg-artifacts/rebuild/global_discovery_recovery/.cognitive_pending_overlay_revision.json.left.tmp'
        orphan.write_text('retained evidence', encoding='utf-8')
        before_files = {path.relative_to(tmp_path).as_posix(): path.read_bytes()
            for path in tmp_path.rglob('*') if path.is_file()}
        projection = {'boards': [{'metadata': {'board_id': 'board', 'board_name': 'Board', 'board_summary': ''}}]}
        result = await capture_candidate_global_source_inputs(tmp_path, projection, max_seconds=60)
        assert result['state'] == 'captured_not_reconciled'
        assert result['overlay_revision'] == fingerprint
        assert dict(result['boards'][0]['overlay_exclusions']) == dict(captured.overlay_exclusions)
        assert await capture_candidate_global_source_inputs(tmp_path, projection, max_seconds=60) == result
        assert {path.relative_to(tmp_path).as_posix(): path.read_bytes()
            for path in tmp_path.rglob('*') if path.is_file()} == before_files
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_candidate_missing_overlay_is_unavailable_without_creating_files(tmp_path):
    projection = {'boards': [{'metadata': {'board_id': 'board', 'board_name': 'Board', 'board_summary': ''}}]}
    result = await capture_candidate_global_source_inputs(tmp_path, projection, max_seconds=10)
    assert result['state'] == 'overlay_unavailable' and result['boards'] == []
    assert list(tmp_path.iterdir()) == []


def test_private_initialization_is_idempotent_and_preserves_all_existing_artifacts(tmp_path):
    ledger = tmp_path / 'kg-artifacts/rebuild/audit/cognitive_pending/board/history.json'
    ledger.parent.mkdir(parents=True)
    ledger.write_bytes(b'{"historical":"retained literally"}')
    fingerprint = initialize_candidate_global_overlay(tmp_path, require_live=lambda: True, max_seconds=10)
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert ledger.read_bytes() == b'{"historical":"retained literally"}'
    assert initialize_candidate_global_overlay(tmp_path, require_live=lambda: True, max_seconds=10) == fingerprint
    assert {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


@pytest.mark.parametrize('payload,code', [(b'{broken', 'json'),
    (b'{}', 'retirement_global_overlay_existing_revision_invalid'),
    (b'{"state":"mutating"}', 'retirement_global_overlay_existing_revision_invalid'),
    (b'{"state":"unfenced"}', 'cognitive_overlay_revision_unfenced')])
def test_private_initialization_never_repairs_existing_unstable_revision(tmp_path, payload, code):
    path = tmp_path / 'kg-artifacts/rebuild/global_discovery_recovery/cognitive_pending_overlay_revision.json'
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises((ValueError, CognitivePendingOverlaySnapshotError)) as caught:
        initialize_candidate_global_overlay(tmp_path, require_live=lambda: True, max_seconds=10)
    if code == 'json':
        assert isinstance(caught.value, json.JSONDecodeError)
    else:
        assert getattr(caught.value, 'code', str(caught.value)) == code
    assert {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


def test_private_initialization_requires_live_fence_before_creating_any_file(tmp_path):
    with pytest.raises(ValueError, match='retirement_global_overlay_fence_required'):
        initialize_candidate_global_overlay(tmp_path, require_live=lambda: False, max_seconds=10)
    assert list(tmp_path.iterdir()) == []
