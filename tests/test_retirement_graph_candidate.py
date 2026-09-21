"""Private native candidates retain SQL policy and temporal graph identity."""

from dataclasses import replace
import json
import sqlite3

import pytest
from okto_grafx import connect
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import retirement_graph_candidate as candidate
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.joint_recovery_snapshot import RecoveryGraph
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from test_retirement_offline_run import SOURCE, MIGRATION
from test_retirement_v034_source import restore_source
from test_card_context_retirement import dump
from test_joint_recovery_native_history import history
from logical_transfer_matrix_support import one_node_corpus, seed_generation
import test_retirement_bootstrap_convergence as convergence

database = convergence.database


@pytest.mark.asyncio
async def test_native_candidate_is_private_and_preserves_history_after_failed_composition(tmp_path, monkeypatch):
    source = restore_source(tmp_path)
    for name in ('uploads', 'kg', 'backups', 'candidate-backups'):
        (tmp_path / name).mkdir()
    engine = create_async_engine(f'sqlite+aiosqlite:///{source}')
    runtime = db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine))
    storage = CommunityFileSystemStorage(str(tmp_path / 'uploads'))
    bindings = CommunityGraphBackendBindingStore(tmp_path / 'kg')
    path = bindings.board_grafx_path('board-a', 'original')
    path.parent.mkdir(parents=True)
    corpus = one_node_corpus('board', key='baseline')
    node = corpus.nodes[0]
    corpus = replace(corpus, nodes=(replace(node, properties={**node.properties,
        'source_artifact_ref': 'spec:spec-a', 'created_by_agent': 'system:historical_consolidation'}),))
    seed_generation('grafx', path, corpus)
    graph = connect(path, page_size=8192)
    graphs = (RecoveryGraph(graph, 'board', 'board-a'),)
    try:
        original_binding = bindings.initialize_board_binding(board_id='board-a', backend='grafx', generation='original',
            physical_path=path, page_size=8192, database=graph)
        reader = history(graph)
        reader.activate('board-a', ('Decision',), (), reason='candidate history fixture')
        with graph.begin('write') as writer:
            writer.execute("MATCH (n:Decision {id: 'baseline'}) SET n.title='Historical title'")
        cursor = reader.commits('board-a')['entries'][-1]['commit']
        past = reader.as_of('board-a', cursor, ('Decision',), ())
        with graph.begin('write') as writer:
            writer.execute("MATCH (n:Decision {id: 'baseline'}) SET n.title='Current title'")
        commits = reader.commits('board-a')
        original_uuid = graph.identity.database_uuid
        run = await offline.prepare_offline_retirement_run(runtime, storage, graphs, tmp_path / 'backups', tmp_path / 'run',
            snapshot_id='original', plan=ContextDispositionPlan(migration_id='candidate-fixture',
                decision_reference='frozen fixture without substantive Sprint context', decisions=()),
            source_builds=SOURCE, migration_builds=MIGRATION, runtime_directories=(tmp_path, tmp_path / 'kg'),
            kg_base_dir=tmp_path / 'kg', max_seconds=180)
        prepared = await offline.prepare_offline_retirement_projection_inputs(runtime, storage, graphs, run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        before = dump(source)
        seed = await candidate.prepare_retirement_candidate_seed(runtime, storage, graphs, run, prepared['projection_inputs'],
            migration_builds=MIGRATION, recovery_directory=tmp_path / 'candidate-backups', seed_directory=tmp_path / 'seed')
        target = tmp_path / 'candidate'
        args = (runtime, storage, graphs, run, seed, target)
        with pytest.raises(ValueError, match='original_offline_required'):
            await candidate.restore_retirement_graph_candidate(*args, migration_builds=MIGRATION)
        with pytest.raises(ValueError, match='original_handles_open'):
            await candidate.restore_retirement_graph_candidate(*args, migration_builds=MIGRATION, confirm_original_offline=True)
        assert not target.exists()
        graph.close()
        initialize = CommunityGraphBackendBindingStore.initialize_board_binding
        def fail(*args, **kwargs):
            initialize(*args, **kwargs)
            raise RuntimeError('candidate composition interrupted')
        with monkeypatch.context() as scoped:
            scoped.setattr(CommunityGraphBackendBindingStore, 'initialize_board_binding', fail)
            with pytest.raises(RuntimeError, match='composition interrupted'):
                await candidate.restore_retirement_graph_candidate(*args, migration_builds=MIGRATION, confirm_original_offline=True)
        assert not target.exists() and not list(tmp_path.glob('.candidate.*.restore'))
        result = await candidate.restore_retirement_graph_candidate(*args, migration_builds=MIGRATION, confirm_original_offline=True)
        assert result['state'] == 'restored_not_materialized'
        assert bindings.inspect_board_binding('board-a') == original_binding
        restored_binding = CommunityGraphBackendBindingStore(target / 'kg-artifacts').inspect_board_binding('board-a')
        assert restored_binding.generation != original_binding.generation
        with connect(restored_binding.physical_path, page_size=8192, read_only=True) as cold:
            assert cold.identity.database_uuid == original_uuid
            assert history(cold).commits('board-a') == commits
            assert history(cold).as_of('board-a', cursor, ('Decision',), ()) == past
        assert dump(source) == before
        assert candidate._sql_snapshot(target / 'database.sqlite3') == candidate._sql_snapshot(source)
        receipt = json.loads((target / 'candidate-receipt/run.json').read_text())
        assert receipt['seed_sha256'] == seed.manifest_sha256
        restored_engine = create_async_engine(f'sqlite+aiosqlite:///{target / "database.sqlite3"}')
        try:
            with pytest.raises(Exception, match='retirement_cutover_incomplete'):
                await offline.require_retirement_runtime_admission(restored_engine)
        finally:
            await restored_engine.dispose()
    finally:
        graph.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_candidate_refuses_source_drift_before_creating_output(tmp_path):
    from test_retirement_offline_bootstrap import prepare
    runtime, storage, run, source = await prepare(tmp_path)
    try:
        projection = await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        recovery = tmp_path / 'candidate-backups'
        recovery.mkdir()
        seed = await candidate.prepare_retirement_candidate_seed(runtime, storage, (), run, projection['projection_inputs'],
            migration_builds=MIGRATION, recovery_directory=recovery, seed_directory=tmp_path / 'seed')
        with sqlite3.connect(source) as connection:
            connection.execute("UPDATE specs SET title='changed after seed' WHERE id='spec-a'")
        with pytest.raises(ValueError, match='live_source_changed'):
            await candidate.restore_retirement_graph_candidate(runtime, storage, (), run, seed, tmp_path / 'candidate',
                migration_builds=MIGRATION, confirm_original_offline=True)
        assert not (tmp_path / 'candidate').exists()
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.timeout(420)
async def test_private_layout_recomposes_two_boards_and_global_discovery(database, tmp_path):
    from test_retirement_offline_materialization import setup, prepare
    engine, _ = database
    args, graphs = await setup(engine, tmp_path, global_present=True)
    try:
        run = await prepare(args, graphs)
        projection = await offline.prepare_offline_retirement_projection_inputs(args[0], args[1], tuple(graphs), run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        recovery = tmp_path / 'candidate-backups'
        recovery.mkdir()
        seed = await candidate.prepare_retirement_candidate_seed(args[0], args[1], tuple(graphs), run,
            projection['projection_inputs'], migration_builds=MIGRATION,
            recovery_directory=recovery, seed_directory=tmp_path / 'seed', max_seconds=300)
        expected = {(item.scope, item.board_id): (item.database.identity.database_uuid,
            item.database.transactions.published_lsn()) for item in graphs}
        for item in graphs:
            item.database.close()
        target = tmp_path / 'candidate'
        result = await candidate.restore_retirement_graph_candidate(args[0], args[1], tuple(graphs), run, seed, target,
            migration_builds=MIGRATION, confirm_original_offline=True, max_seconds=300)
        assert result['state'] == 'restored_not_materialized'
        bindings = CommunityGraphBackendBindingStore(target / 'kg-artifacts')
        for (scope, board_id), (identity, lsn) in expected.items():
            binding = (bindings.inspect_board_binding(board_id) if scope == 'board' else bindings.inspect_global_binding())
            assert binding.generation != 'g1'
            with connect(binding.physical_path, page_size=binding.page_size, read_only=True) as cold:
                assert cold.identity.database_uuid == identity
                assert cold.transactions.published_lsn() == lsn
        assert len(expected) == 3
    finally:
        for item in graphs:
            item.database.close()
