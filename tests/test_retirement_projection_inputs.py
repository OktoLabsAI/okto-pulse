"""Final SQL sources -> real Core projection preparation, without graph writes."""

import hashlib
import json
import sqlite3

import pytest

from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters.retirement_projection_inputs import read_retirement_projection_inputs
from okto_pulse.community.adapters import retirement_projection_inputs as inputs
from test_retirement_offline_bootstrap import prepare, resume
from test_retirement_offline_run import MIGRATION
from test_card_context_retirement import dump


@pytest.mark.asyncio
@pytest.mark.parametrize('status,archived', [('cancelled', False), ('done', True)])
async def test_terminal_refinement_from_real_sql_keeps_empty_cleanup_outside_census(tmp_path, status, archived):
    from datetime import datetime, timezone
    from sqlalchemy import insert
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Ideation, Refinement, Spec
    from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
    path = tmp_path / 'terminal-projection.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{path.as_posix()}')
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(insert(Board).values(id='board', name='Board', owner_id='owner'))
            await connection.execute(insert(Ideation).values(id='idea', board_id='board', title='Archived context',
                status='cancelled', created_by='owner'))
            await connection.execute(insert(Refinement).values(id='refinement', board_id='board', ideation_id='idea',
                title='Terminal refinement', status=status, archived=archived, created_by='owner'))
            await connection.execute(insert(Spec).values(id='spec', board_id='board', title='Surviving Spec',
                status='done', created_by='owner'))
        before = dump(path)
        snapshot = CommunityBoardSourceReader(path).fetch('board')
        assert snapshot.complete
        planner = inputs.make_deterministic_projection_planner(inputs.CommunitySqlAlchemyConsolidationPersistence(),
            dependencies=inputs.RetirementProjectionDependencies(path))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            encoded = await planner.prepare_board(session, board_id='board', source_rows=tuple(snapshot.rows),
                cognitive_rows=(), captured_at=datetime.now(timezone.utc))
        document = json.loads(encoded)
        assert document['census']['eligible_count'] == 1 and document['census']['skipped_cancelled_count'] == 2
        plans = {item['source']['artifact_id']: item for item in document['plans']}
        assert set(plans) == {'refinement', 'spec'}
        assert plans['refinement']['projection']['nodes'] == []
        assert plans['refinement']['projection']['relational_projection_active_set_intent'] == {
            'owner_type': 'refinement', 'owner_id': 'refinement', 'namespace': 'rdl',
            'active_refs': [], 'active_edges': [],
        }
        assert dump(path) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_final_projection_preserves_only_required_expired_evidence_chain(tmp_path):
    from datetime import timedelta
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from okto_pulse.community.adapters.sqlalchemy_models import Base
    from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
    from test_code_traceability_kg_rebuild_e2e import (
        NOW, seed_complete_traceability_source, seed_superseded_evidence_chain,
    )
    path = tmp_path / 'projection-chain.sqlite3'
    engine = create_async_engine(f'sqlite+aiosqlite:///{path.as_posix()}')
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await seed_complete_traceability_source(connection)
            await seed_superseded_evidence_chain(connection)
        before = dump(path)
        snapshot = CommunityBoardSourceReader(path).fetch('board-1')
        assert snapshot.complete
        planner = inputs.make_deterministic_projection_planner(inputs.CommunitySqlAlchemyConsolidationPersistence(),
            dependencies=inputs.RetirementProjectionDependencies(path))
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            encoded = await planner.prepare_board(session, board_id='board-1',
                source_rows=tuple(snapshot.rows), cognitive_rows=(), captured_at=NOW + timedelta(days=1))
            await planner.revalidate_board(session, encoded, board_id='board-1',
                source_rows=tuple(snapshot.rows), cognitive_rows=())
            membership = await planner.prepare_execution(session, encoded, board_id='board-1',
                source_rows=tuple(snapshot.rows), cognitive_rows=())
            assert {'evidence-y', 'evidence-z', 'evidence-1'} <= {row['id'] for row in membership}
            assert 'evidence-unrelated' not in {row['id'] for row in membership}
        document = json.loads(encoded)
        assert document['format'] == 'deterministic-board-projection-plan/v3'
        assert {row['id'] for row in document['dependency_closure']} == {'evidence-y', 'evidence-z'}
        plans = {item['source']['artifact_id']: item for item in document['plans']}
        assert {'evidence-y', 'evidence-z', 'evidence-1'} <= plans.keys()
        assert 'evidence-unrelated' not in plans
        for identity in ('evidence-y', 'evidence-z'):
            nodes = plans[identity]['projection']['nodes']
            root = next(node for node in nodes if node['source_artifact_ref'] == f'code_evidence:{identity}')
            assert root['graph_layer'] == 'working' and root['maturity_status'] == 'working_superseded'
        assert dump(path) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_candidate_seed_and_restore_reject_resealed_plan_that_does_not_match_sql(tmp_path):
    from okto_pulse.community.adapters import retirement_graph_candidate as candidate
    runtime, storage, run, source = await prepare(tmp_path)
    try:
        prepared = await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        handle = prepared['projection_inputs']
        artifact = handle.directory / 'run.json'
        original = artifact.read_bytes()
        malformed = json.loads(original)
        board = malformed['boards'][0]
        board['projection']['plans'][0]['projection']['nodes'][0]['title'] = 'Not from the retained SQL'
        board['sha256'] = hashlib.sha256(inputs._encode(board['projection'])).hexdigest()
        changed = inputs._encode(malformed)
        artifact.write_bytes(changed)
        changed_handle = inputs.RetirementProjectionInputs(handle.directory, hashlib.sha256(changed).hexdigest())
        assert inputs.read_retirement_projection_inputs(changed_handle) == malformed
        before = dump(source)
        recovery = tmp_path / 'candidate-backups'
        recovery.mkdir()
        with pytest.raises(ValueError, match='retained_plan_mismatch'):
            await candidate.prepare_retirement_candidate_seed(runtime, storage, (), run, changed_handle,
                migration_builds=MIGRATION, recovery_directory=recovery, seed_directory=tmp_path / 'bad-seed')
        assert not (tmp_path / 'bad-seed').exists() and artifact.read_bytes() == changed
        artifact.write_bytes(original)
        seed = await candidate.prepare_retirement_candidate_seed(runtime, storage, (), run, handle,
            migration_builds=MIGRATION, recovery_directory=recovery, seed_directory=tmp_path / 'seed')
        # Simulate a retained seed with consistent outer/inner hashes. Restore
        # must independently reject the content, not rely on prior preparation.
        artifact.write_bytes(changed)
        seed_path = seed.directory / 'run.json'
        seed_document = json.loads(seed_path.read_bytes())
        seed_document['projection_inputs']['manifest_sha256'] = changed_handle.manifest_sha256
        encoded_seed = inputs._encode(seed_document)
        seed_path.write_bytes(encoded_seed)
        resealed_seed = candidate.RetirementGraphCandidateSeed(seed.directory, hashlib.sha256(encoded_seed).hexdigest())
        with pytest.raises(ValueError, match='retained_plan_mismatch'):
            await candidate.restore_retirement_graph_candidate(runtime, storage, (), run, resealed_seed,
                tmp_path / 'candidate', migration_builds=MIGRATION, confirm_original_offline=True)
        assert not (tmp_path / 'candidate').exists()
        assert artifact.read_bytes() == changed and seed_path.read_bytes() == encoded_seed
        assert dump(source) == before
        async with runtime.engine.connect() as connection:
            assert (await connection.exec_driver_sql('PRAGMA query_only')).scalar_one() == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_revalidation_reads_independent_inventory_under_readonly_sql_reservation(tmp_path, monkeypatch):
    from sqlalchemy import text
    runtime, storage, run, source = await prepare(tmp_path)
    try:
        prepared = await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        document = inputs.read_retirement_projection_inputs(prepared['projection_inputs'])
        before = dump(source)
        factory = inputs.make_deterministic_projection_planner
        def guarded(port, **kwargs):
            planner = factory(port, **kwargs)
            revalidate = planner.revalidate_board
            async def checked(session, *args, **options):
                with sqlite3.connect(source, timeout=0.01) as competitor:
                    with pytest.raises(sqlite3.OperationalError, match='locked'):
                        competitor.execute("UPDATE specs SET title='concurrent' WHERE id='spec-a'")
                with pytest.raises(Exception, match='readonly'):
                    await session.execute(text("UPDATE specs SET title='write denied' WHERE id='spec-a'"))
                return await revalidate(session, *args, **options)
            planner.revalidate_board = checked
            return planner
        monkeypatch.setattr(inputs, 'make_deterministic_projection_planner', guarded)
        async with runtime.engine.connect() as connection:
            await connection.exec_driver_sql('BEGIN IMMEDIATE')
            await inputs.revalidate_retirement_projection_inputs(connection, source, document)
            assert (await connection.exec_driver_sql('PRAGMA query_only')).scalar_one() == 0
            await connection.rollback()
        assert dump(source) == before
        with sqlite3.connect(source) as writer:
            writer.execute("UPDATE specs SET title='New source revision' WHERE id='spec-a'")
        changed = dump(source)
        async with runtime.engine.connect() as connection:
            await connection.exec_driver_sql('BEGIN IMMEDIATE')
            with pytest.raises(ValueError, match='retained_plan_mismatch'):
                await inputs.revalidate_retirement_projection_inputs(connection, source, document)
            assert (await connection.exec_driver_sql('PRAGMA query_only')).scalar_one() == 0
            await connection.rollback()
        assert dump(source) == changed
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_frozen_predecessor_bootstrap_prepares_final_sources_without_writes(tmp_path, monkeypatch):
    runtime, storage, run, path = await prepare(tmp_path)
    try:
        completed = await resume(runtime, storage, run)
        before = dump(path)
        from okto_pulse.core.application.processors import consolidation
        monkeypatch.setattr(consolidation, 'begin_consolidation', lambda *a, **k: pytest.fail('graph session started'))
        factory = inputs.make_deterministic_projection_planner
        def observed(port, **kwargs):
            delegate = factory(port, **kwargs)
            original = delegate.prepare_board
            async def checked(*args, **kwargs):
                with sqlite3.connect(path, timeout=0.01) as competitor:
                    with pytest.raises(sqlite3.OperationalError, match='locked'):
                        competitor.execute("UPDATE specs SET title='concurrent' WHERE id='spec-a'")
                return await original(*args, **kwargs)
            delegate.prepare_board = checked
            return delegate
        monkeypatch.setattr(inputs, 'make_deterministic_projection_planner', observed)
        result = await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        assert result['state'] == 'projection_inputs_prepared'
        assert result['bootstrap'] == completed['bootstrap'] and dump(path) == before
        document = read_retirement_projection_inputs(result['projection_inputs'])
        assert document['offline_run_sha256'] == run.manifest_sha256
        assert len(document['boards']) == 1 and document['graphs'] == []
        board = document['boards'][0]['projection']
        assert board['board_id'] == 'board-a'
        assert {item['source']['artifact_id'] for item in board['plans']} == {'spec-a', 'card-a', 'card-b'}
        assert all(row['artifact_type'] != 'sprint' for row in board['source_rows'])
        refs = {node['source_artifact_ref'] for item in board['plans'] for node in item['projection']['nodes']}
        assert {'spec:spec-a', 'card:card-a', 'card:card-b'} <= refs
        assert not any(ref and ref.startswith('sprint:') for ref in refs)
        with pytest.raises(Exception, match='retirement_cutover_incomplete'):
            await offline.require_retirement_runtime_admission(runtime.engine)
        with pytest.raises(ValueError, match='private_destination'):
            await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
                migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        assert dump(path) == before
        assert json.loads((tmp_path / 'projection/run.json').read_text()) == document
        artifact = tmp_path / 'projection/run.json'
        original = artifact.read_bytes()
        # Model a retained pre-fix v2 plan: valid hashes do not excuse missing
        # cleanup, even when its claimed census omits the terminal source.
        board['source_rows'].append({'artifact_type': 'refinement', 'id': 'cancelled-refinement',
            'status': 'cancelled', 'content_hash': 'a' * 64})
        document['boards'][0]['sha256'] = hashlib.sha256(inputs._encode(board)).hexdigest()
        incomplete = inputs._encode(document)
        artifact.write_bytes(incomplete)
        handle = inputs.RetirementProjectionInputs(artifact.parent, hashlib.sha256(incomplete).hexdigest())
        with pytest.raises(ValueError, match='cleanup_missing'):
            read_retirement_projection_inputs(handle)
        assert artifact.read_bytes() == incomplete
        artifact.write_bytes(original)
        artifact.write_bytes(artifact.read_bytes() + b' ')
        with pytest.raises(ValueError, match='manifest_mismatch'):
            read_retirement_projection_inputs(result['projection_inputs'])
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_output_cannot_enter_board_storage_or_original_backup_before_migration(tmp_path):
    runtime, storage, run, path = await prepare(tmp_path)
    try:
        before = dump(path)
        document, *_ = offline.read_offline_retirement_run(run)
        from pathlib import Path
        for root in (tmp_path / 'uploads', tmp_path / 'kg', Path(document['backup']['directory'])):
            with pytest.raises(ValueError, match='private_destination'):
                await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
                    migration_builds=MIGRATION, projection_directory=root / 'projection')
        assert dump(path) == before
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_projection_read_cannot_write_sql_or_leave_connection_readonly(tmp_path, monkeypatch):
    runtime, storage, run, path = await prepare(tmp_path)
    try:
        await resume(runtime, storage, run)
        before = dump(path)
        from sqlalchemy import text
        from types import SimpleNamespace
        async def writer(session, **kwargs):
            await session.execute(text("UPDATE specs SET title='forbidden' WHERE id='spec-a'"))
            pytest.fail('projection wrote the authoritative source')
        monkeypatch.setattr(inputs, 'make_deterministic_projection_planner', lambda _, **kwargs: SimpleNamespace(prepare_board=writer))
        with pytest.raises(Exception, match='readonly'):
            await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
                migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        assert dump(path) == before and not (tmp_path / 'projection').exists()
        async with runtime.engine.connect() as connection:
            assert (await connection.exec_driver_sql('PRAGMA query_only')).scalar_one() == 0
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_aggregate_board_limit_stops_preparation_without_publishing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    runtime, storage, run, path = await prepare(tmp_path)
    try:
        await resume(runtime, storage, run)
        before = dump(path)
        # Small synthetic plans exercise the aggregate bound without allocating
        # hundreds of megabytes. No third Board may be read after exhaustion.
        census = {f'board-{i}': {'realm_id': 'local', 'metadata': {}, 'sources': (), 'cognitive': ()}
            for i in range(3)}
        encoded = json.dumps({'padding': 'x' * 1_000}).encode()
        planner = SimpleNamespace(prepare_board=AsyncMock(return_value=encoded))
        monkeypatch.setattr(inputs, '_census', lambda *args: census)
        monkeypatch.setattr(inputs, '_LIMIT', len(encoded) + 1)
        monkeypatch.setattr(inputs, 'make_deterministic_projection_planner', lambda _, **kwargs: planner)
        with pytest.raises(ValueError, match='plan_limit'):
            await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
                migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        assert planner.prepare_board.await_count == 2
        assert dump(path) == before and not (tmp_path / 'projection').exists()
    finally:
        await runtime.close()
