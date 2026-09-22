"""Real worker effects reconcile a reused historical root without admitting it."""

from contextlib import closing
import json
import sqlite3

import pytest
from okto_grafx import connect
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.kg.logical_transfer import LOGICAL_NULL, LogicalNode, LogicalTimestamp
from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import retirement_graph_candidate as candidate
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.joint_recovery_snapshot import RecoveryGraph
from okto_pulse.community.adapters.logical_transfer_schema import board_logical_schema
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.config import CommunitySettings
from logical_transfer_matrix_support import Corpus, seed_generation
from test_retirement_offline_run import SOURCE, MIGRATION
from test_retirement_v034_source import restore_source
from test_card_context_retirement import dump


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_authenticated_effects_on_reused_root_preserve_identity_and_remain_history_pending(tmp_path):
    source = restore_source(tmp_path)
    for name in ('uploads', 'kg', 'backups', 'candidate-backups'):
        (tmp_path / name).mkdir()
    with closing(sqlite3.connect(source)) as connection:
        title = connection.execute("SELECT title FROM specs WHERE id='spec-a'").fetchone()[0]
    engine = create_async_engine(f'sqlite+aiosqlite:///{source}')
    runtime = db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine))
    storage = CommunityFileSystemStorage(str(tmp_path / 'uploads'))
    bindings = CommunityGraphBackendBindingStore(tmp_path / 'kg')
    path = bindings.board_grafx_path('board-a', 'original')
    path.parent.mkdir(parents=True)
    schema = board_logical_schema()
    props = {prop.name: LOGICAL_NULL for prop in schema.node_type('Entity').properties}
    props.update(id='old-spec-root', title=title, content='prior projection content',
        source_artifact_ref='spec:spec-a', source_session_id='historical-session',
        created_by_agent='system:historical_consolidation', graph_layer='deterministic',
        human_curated=False, generation=0, created_at=LogicalTimestamp(0), attestation_count=1)
    seed_generation('grafx', path, Corpus(schema, (LogicalNode('Entity', 'old-spec-root', props),), ()))
    graph = connect(path, page_size=8192)
    graphs = (RecoveryGraph(graph, 'board', 'board-a'),)
    try:
        bindings.initialize_board_binding(board_id='board-a', backend='grafx', generation='original',
            physical_path=path, page_size=8192, database=graph)
        run = await offline.prepare_offline_retirement_run(runtime, storage, graphs, tmp_path / 'backups', tmp_path / 'run',
            snapshot_id='original', plan=ContextDispositionPlan(migration_id='reused-history-fixture',
                decision_reference='frozen fixture without substantive Sprint context', decisions=()),
            source_builds=SOURCE, migration_builds=MIGRATION, runtime_directories=(tmp_path, tmp_path / 'kg'),
            kg_base_dir=tmp_path / 'kg', max_seconds=180)
        prepared = await offline.prepare_offline_retirement_projection_inputs(runtime, storage, graphs, run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        seed = await candidate.prepare_retirement_candidate_seed(runtime, storage, graphs, run, prepared['projection_inputs'],
            migration_builds=MIGRATION, recovery_directory=tmp_path / 'candidate-backups', seed_directory=tmp_path / 'seed')
        before = dump(source)
        graph.close()
        target = tmp_path / 'projected'
        settings = CommunitySettings(kg_embedding_mode='stub', kg_embedding_dim=384)
        arguments = (runtime, storage, graphs, run, seed, target)
        result = await candidate.build_projected_retirement_graph_candidate(*arguments,
            migration_builds=MIGRATION, settings=settings, confirm_original_offline=True, max_seconds=300)
        receipt = json.loads((target / 'projection-receipt/run.json').read_bytes())
        assert result['state'] == 'projected_not_reconciled' and dump(source) == before
        report = receipt['graph_reconciliation']['boards'][0]
        assert report['historical_property_change_count'] == 1
        assert report['historical_node_count'] == 1 and report['history_classification'] == 'pending'
        assert report['zero_orphan_validation'] == 'passed'
        proof = receipt['historical_observations']['property_composition'][0]
        assert proof['before']['node_id'] == proof['after']['node_id'] == 'old-spec-root'
        with closing(sqlite3.connect(target / 'database.sqlite3')) as connection:
            assert connection.execute("SELECT count(*) FROM kuzu_node_refs WHERE kuzu_node_id='old-spec-root'").fetchone()[0] == 0
        replay = await candidate.build_projected_retirement_graph_candidate(*arguments,
            migration_builds=MIGRATION, settings=settings, confirm_original_offline=True,
            confirm_candidate_offline=True, expected_receipt_sha256=result['receipt_sha256'], max_seconds=300)
        assert replay == result
        candidate_engine = create_async_engine(f'sqlite+aiosqlite:///{target / "database.sqlite3"}')
        try:
            with pytest.raises(Exception, match='retirement_cutover_incomplete'):
                await offline.require_retirement_runtime_admission(candidate_engine)
        finally:
            await candidate_engine.dispose()
    finally:
        graph.close()
        await engine.dispose()
