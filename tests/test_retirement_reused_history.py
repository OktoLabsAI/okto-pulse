"""Real worker effects reconcile a reused historical root without admitting it."""

from contextlib import closing
import json
import sqlite3

import pytest
from okto_grafx import connect
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.kg.logical_transfer import LOGICAL_NULL, LogicalNode, LogicalTimestamp, schema_digest, transfer_logical_graph
from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.core.ports.global_discovery_recovery_control import CognitivePendingOverlaySnapshotService
from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import retirement_graph_candidate as candidate
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.grafx_recovery_contracts import predecessor_recovery_contract, make_grafx_recovery_logical_sink
from okto_pulse.community.adapters.joint_recovery_snapshot import RecoveryGraph
from okto_pulse.community.adapters.logical_transfer_schema import board_logical_schema
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
from okto_pulse.community.config import CommunitySettings
from logical_transfer_matrix_support import Corpus, MaterializedSource, one_node_corpus, seed_generation
from test_retirement_offline_run import SOURCE, MIGRATION
from test_retirement_v034_source import restore_source
from test_card_context_retirement import dump


@pytest.mark.asyncio
@pytest.mark.timeout(600)
@pytest.mark.parametrize('source_schema', ['0.6.0', '0.5.0'])
async def test_authenticated_effects_on_reused_root_preserve_identity_and_remain_history_pending(tmp_path, source_schema):
    source = restore_source(tmp_path)
    for name in ('uploads', 'kg', 'backups', 'candidate-backups'):
        (tmp_path / name).mkdir()
    if source_schema == '0.6.0':
        overlay_revision = CognitivePendingOverlaySnapshotService(
            CommunityFileSystemRebuildAuditArtifactStore(tmp_path / 'kg')).current_fingerprint()
    with closing(sqlite3.connect(source)) as connection:
        title = connection.execute("SELECT title FROM specs WHERE id='spec-a'").fetchone()[0]
        if source_schema == '0.6.0':
            # Durable knowledge can exist while its graph projection is absent.
            # The candidate must retain this limitation through checkpoint replay.
            connection.execute('INSERT INTO kg_cognitive_sources '
                '(id,board_id,node_id,node_type,generation,payload,evidence_refs,source_session_id,committed_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)', ('durable-source', 'board-a', 'missing-decision', 'Decision', 0,
                    json.dumps({'title': 'sealed historical decision', 'generation': 0,
                        'source_artifact_ref': 'spec:spec-a'}), json.dumps(['spec:spec-a']),
                    'historical-cognitive-session', '2026-01-01T00:00:00.000000'))
            connection.commit()
    engine = create_async_engine(f'sqlite+aiosqlite:///{source}')
    runtime = db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine))
    storage = CommunityFileSystemStorage(str(tmp_path / 'uploads'))
    bindings = CommunityGraphBackendBindingStore(tmp_path / 'kg')
    path = bindings.board_grafx_path('board-a', 'original')
    path.parent.mkdir(parents=True)
    schema = board_logical_schema() if source_schema == '0.6.0' else predecessor_recovery_contract().schema
    props = {prop.name: LOGICAL_NULL for prop in schema.node_type('Entity').properties}
    props.update(id='old-spec-root', title=title, content='prior projection content',
        source_artifact_ref='spec:spec-a', source_session_id='historical-session',
        created_by_agent='system:historical_consolidation', graph_layer='deterministic',
        human_curated=False, generation=0, created_at=LogicalTimestamp(0), attestation_count=1)
    nodes = (LogicalNode('Entity', 'old-spec-root', props),)
    if source_schema == '0.5.0':
        nodes += (LogicalNode('BoardMeta', 'board-a', {'board_id': 'board-a', 'schema_version': '0.5.0',
            'bootstrapped_at': LogicalTimestamp(0), 'embedding_model': 'historical-fixture', 'embedding_dimension': 384}),)
        transfer_logical_graph(MaterializedSource(Corpus(schema, nodes, ())),
            make_grafx_recovery_logical_sink(path, scope='board', expected_schema_digest=schema_digest(schema)))
    else:
        seed_generation('grafx', path, Corpus(schema, nodes, ()))
    graph = connect(path, page_size=8192)
    graphs = (RecoveryGraph(graph, 'board', 'board-a'),)
    original_uuid = graph.identity.database_uuid
    try:
        bindings.initialize_board_binding(board_id='board-a', backend='grafx', generation='original',
            physical_path=path, page_size=8192, database=graph)
        if source_schema == '0.5.0':
            global_path = bindings.global_grafx_path('original')
            global_path.parent.mkdir(parents=True, exist_ok=True)
            seed_generation('grafx', global_path, one_node_corpus('global_discovery'))
            global_graph = connect(global_path, page_size=8192)
            graphs += (RecoveryGraph(global_graph, 'global_discovery'),)
            bindings.initialize_global_binding(backend='grafx', generation='original', physical_path=global_path,
                page_size=8192, database=global_graph)
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
        for entry in graphs:
            entry.database.close()
        target = tmp_path / 'projected'
        settings = CommunitySettings(kg_embedding_mode='stub', kg_embedding_dim=384)
        arguments = (runtime, storage, graphs, run, seed, target)
        result = await candidate.build_projected_retirement_graph_candidate(*arguments,
            migration_builds=MIGRATION, settings=settings, confirm_original_offline=True, max_seconds=300)
        receipt = json.loads((target / 'projection-receipt/run.json').read_bytes())
        assert receipt['format'] == 'retirement-candidate-projection/v5'
        if source_schema == '0.6.0':
            assert receipt['global_source_inputs']['state'] == 'captured_not_reconciled'
            assert receipt['global_source_inputs']['overlay_revision'] == overlay_revision
            assert receipt['global_source_inputs']['boards'][0]['board_id'] == 'board-a'
            global_comparison = receipt['graph_reconciliation']['global_projection_comparison']
            assert global_comparison['state'] == 'matched' and global_comparison['materialized']
            assert global_comparison['missing_nodes'] == 0
            assert receipt['global_materialization']['state'] == 'created'
        else:
            assert receipt['global_source_inputs']['state'] == 'overlay_unavailable'
            assert receipt['graph_reconciliation']['global_projection_comparison']['state'] == 'unavailable'
            assert receipt['global_materialization']['state'] == 'retained'
        assert result['state'] == 'projected_not_reconciled' and dump(source) == before
        report = receipt['graph_reconciliation']['boards'][0]
        assert report['source_partition_validation'] == report['edge_session_validation'] == 'passed'
        assert report['historical_property_change_count'] == 1
        assert report['historical_node_count'] == 1 and report['history_classification'] == 'current_source_reconciled'
        assert report['history_qualification']['current_source_node_count'] == 1
        assert report['history_qualification']['unclassified_node_count'] == 0
        comparison = report['source_relation_comparison']
        assert comparison['expected_count'] == comparison['matched_count'] == 5
        assert comparison['missing_count'] == comparison['unresolved_count'] == comparison['unexpected_new_count'] == 0
        assert receipt['graph_reconciliation']['state'] == 'source_projection_reconciled_history_pending'
        assert report['zero_orphan_validation'] == 'passed'
        if source_schema == '0.5.0':
            assert len(receipt['schema_evolutions']) == 1
            observed = receipt['historical_observations']
            assert observed['before_census_sha256'] != observed['projection_baseline_sha256']
            bound = CommunityGraphBackendBindingStore(target / 'kg-artifacts').inspect_board_binding('board-a')
            with connect(bound.physical_path, page_size=8192, read_only=True) as current:
                assert current.identity.database_uuid != original_uuid
                assert current.execute('MATCH (m:BoardMeta) RETURN m.schema_version').rows == (('0.6.0',),)
            assert (target / 'graph-0000').is_dir()  # Retained, unbound native predecessor.
        else:
            assert receipt['schema_evolutions'] == []
            parity = report['cognitive_source_parity']
            assert len(parity) == 1 and parity[0]['node_id'] == 'missing-decision'
            assert parity[0]['state'] == 'missing_node'
            restoration, = report['cognitive_restoration']
            assert restoration['node_id'] == 'missing-decision'
            assert restoration['state'] == 'connectivity_rejected' and restoration['reasons']
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
        if source_schema == '0.5.0':
            retained = next(file for file in (target / 'graph-0000').rglob('*')
                if file.is_file() and file.stat().st_size > 0)
            original_bytes = retained.read_bytes()
            try:
                retained.write_bytes(bytes([original_bytes[0] ^ 1]) + original_bytes[1:])
                with pytest.raises(ValueError, match='checkpoint_content_changed'):
                    await candidate.build_projected_retirement_graph_candidate(*arguments,
                        migration_builds=MIGRATION, settings=settings, confirm_original_offline=True,
                        confirm_candidate_offline=True, expected_receipt_sha256=result['receipt_sha256'], max_seconds=300)
            finally:
                retained.write_bytes(original_bytes)
    finally:
        for entry in graphs:
            entry.database.close()
        await engine.dispose()
