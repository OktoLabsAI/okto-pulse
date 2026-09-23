"""Real worker effects reconcile a reused historical root without admitting it."""

from contextlib import closing
import asyncio
import json
import sqlite3
import subprocess
import sys

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
@pytest.mark.parametrize('source_schema,with_cognitive,complete_overlay', [
    ('0.6.0', True, True), ('0.6.0', False, True), ('0.5.0', False, False), ('0.5.0', False, True)],
    ids=['current-cognitive-pending', 'current-complete', 'predecessor-global-pending', 'predecessor-complete'])
async def test_authenticated_effects_on_reused_root_preserve_identity_and_require_complete_evidence(tmp_path, source_schema, with_cognitive, complete_overlay, monkeypatch):
    source = restore_source(tmp_path)
    for name in ('uploads', 'kg', 'backups', 'candidate-backups'):
        (tmp_path / name).mkdir()
    if complete_overlay:
        overlay_revision = CognitivePendingOverlaySnapshotService(
            CommunityFileSystemRebuildAuditArtifactStore(tmp_path / 'kg')).current_fingerprint()
    with closing(sqlite3.connect(source)) as connection:
        title = connection.execute("SELECT title FROM specs WHERE id='spec-a'").fetchone()[0]
        if with_cognitive:
            # Durable knowledge can exist while its graph projection is absent.
            # The candidate must retain this limitation through checkpoint replay.
            connection.execute('INSERT INTO kg_cognitive_sources '
                '(id,board_id,node_id,node_type,generation,payload,evidence_refs,source_session_id,committed_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)', ('durable-source', 'board-a', 'missing-decision', 'Decision', 0,
                    json.dumps({'title': 'sealed historical decision', 'generation': 0,
                        'source_artifact_ref': 'spec:spec-a'}), json.dumps(['spec:spec-a']),
                    'historical-cognitive-session', '2026-01-01T00:00:00.000000'))
            connection.execute('INSERT INTO kg_cognitive_sources '
                '(id,board_id,node_id,node_type,generation,payload,evidence_refs,source_session_id,committed_at) '
                'VALUES (?,?,?,?,?,?,?,?,?)', ('durable-report', 'board-a', 'restored-report', 'Decision', 0,
                    json.dumps({'title': 'literal historical report', 'generation': 0,
                        'source_artifact_ref': 'final_report:historical', 'created_by_agent': 'agent-a',
                        'graph_layer': 'canonical', 'maturity_status': 'canonical_eligible',
                        'created_at': '2026-01-01T00:00:00.123456Z'}),
                    json.dumps(['final_report:historical']), 'kgses_historical', '2026-01-01T00:00:00.000000'))
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
        if source_schema == '0.5.0' and not complete_overlay:
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
        assert receipt['format'] == 'retirement-candidate-projection/v6'
        if complete_overlay:
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
        complete = complete_overlay and not with_cognitive
        assert receipt['graph_reconciliation']['state'] == (
            'source_graph_reconciled' if complete else 'source_projection_reconciled_history_pending')
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
        if with_cognitive:
            parity = {row['node_id']: row for row in report['cognitive_source_parity']}
            assert set(parity) == {'missing-decision', 'restored-report'}
            assert parity['missing-decision']['state'] == 'missing_node'
            assert parity['restored-report']['state'] == 'matched'
            assert report['restored_cognitive_node_count'] == 1
            created, = receipt['cognitive_restoration']['boards'][0]['created']
            assert created['node_id'] == 'restored-report' and created['literal_fingerprint']
            restoration, = report['cognitive_restoration']
            assert restoration['node_id'] == 'missing-decision'
            assert restoration['state'] == 'connectivity_rejected' and restoration['reasons']
        proof = receipt['historical_observations']['property_composition'][0]
        assert proof['before']['node_id'] == proof['after']['node_id'] == 'old-spec-root'
        with closing(sqlite3.connect(target / 'database.sqlite3')) as connection:
            assert connection.execute("SELECT count(*) FROM kuzu_node_refs WHERE kuzu_node_id='old-spec-root'").fetchone()[0] == 0
            assert connection.execute("SELECT count(*) FROM kuzu_node_refs WHERE kuzu_node_id='restored-report'").fetchone()[0] == 0
        replay = await candidate.build_projected_retirement_graph_candidate(*arguments,
            migration_builds=MIGRATION, settings=settings, confirm_original_offline=True,
            confirm_candidate_offline=True, expected_receipt_sha256=result['receipt_sha256'], max_seconds=300)
        assert replay == result
        completion_arguments = dict(migration_builds=MIGRATION, settings=settings,
            confirm_original_offline=True, confirm_candidate_offline=True,
            expected_receipt_sha256=result['receipt_sha256'], max_seconds=300)
        if complete:
            assert await candidate.verify_reconciled_retirement_graph_candidate(*arguments, **completion_arguments) == result
            from okto_pulse.community.adapters import retirement_activation_installation as installation
            with monkeypatch.context() as scoped:
                def interrupt_publication(*_):
                    raise RuntimeError('interrupted before activation publication')
                scoped.setattr(installation, '_publish', interrupt_publication)
                with pytest.raises(RuntimeError, match='interrupted before activation'):
                    await candidate.activate_retirement_graph_candidate(*arguments, tmp_path / 'installation',
                        **completion_arguments)
            assert not (tmp_path / 'installation').exists()
            assert not list(tmp_path.glob('.installation.*.activation')) and dump(source) == before
            activated = await candidate.activate_retirement_graph_candidate(*arguments, tmp_path / 'installation',
                **completion_arguments)
            assert activated['state'] == 'activated' and dump(source) == before
            assert await installation.resume_retirement_activation(activated['directory'],
                expected_candidate_receipt_sha256=result['receipt_sha256'], confirm_installation_offline=True) == activated
            with pytest.raises(ValueError, match='offline_required'):
                await installation.resume_retirement_activation(activated['directory'],
                    expected_candidate_receipt_sha256=result['receipt_sha256'])
            with pytest.raises(ValueError, match='receipt_mismatch'):
                await installation.resume_retirement_activation(activated['directory'],
                    expected_candidate_receipt_sha256='0' * 64, confirm_installation_offline=True)
            installed_engine = create_async_engine(f'sqlite+aiosqlite:///{activated["database"]}')
            try:
                await offline.require_retirement_runtime_admission(installed_engine)
                script = '''
import asyncio, sys, site, sysconfig
from pathlib import Path
# The validation venv inherits third-party dependencies from user-site. Keep
# -I (no checkout/PYTHONPATH injection), append that dependency path explicitly,
# and independently require both application packages from this venv's wheels.
sys.path.append(site.getusersitepackages())
import okto_pulse.core, okto_pulse.community
installed = Path(sysconfig.get_path('purelib')).resolve()
assert Path(okto_pulse.core.__file__).resolve().is_relative_to(installed)
assert Path(okto_pulse.community.__file__).resolve().is_relative_to(installed)
from okto_pulse.core import configure_settings, configure_storage
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.relational_schema_lifecycle import register_community_relational_schema_lifecycle
async def main():
    root = Path(sys.argv[1])
    settings = CommunitySettings(database_url=f'sqlite+aiosqlite:///{root / "database.sqlite3"}',
        data_dir=str(root), kg_base_dir=str(root / 'kg-artifacts'), upload_dir=str(root / 'uploads'),
        kg_embedding_mode='stub', kg_embedding_dim=384)
    configure_settings(settings)
    configure_storage(CommunityFileSystemStorage(settings.upload_dir))
    runtime = db.configure_community_database(settings.database_url)
    register_community_relational_schema_lifecycle()
    try:
        await db.init_db()
        print('activated runtime ready')
    finally:
        await runtime.close()
asyncio.run(main())
'''
                booted = await asyncio.to_thread(subprocess.run,
                    [sys.executable, '-I', '-c', script, str(activated['directory'])],
                    capture_output=True, text=True, timeout=180)
                assert booted.returncode == 0, booted.stderr
                assert booted.stdout.strip().endswith('activated runtime ready')
                async with installed_engine.begin() as connection:
                    await connection.exec_driver_sql("UPDATE cards SET title=title || ' after activation'")
                # Admission checks retained proof, never the old hash of mutable user data.
                await offline.require_retirement_runtime_admission(installed_engine)
                with pytest.raises(Exception, match='retirement_cutover_incomplete'):
                    await offline.require_retirement_not_started(installed_engine)
                from okto_pulse.community.adapters.retirement_activation import require_retirement_activation_roots
                installed_settings = settings.model_copy(update={'database_url': str(installed_engine.url),
                    'data_dir': str(activated['directory']), 'kg_base_dir': str(activated['kg']),
                    'upload_dir': str(activated['storage'])})
                require_retirement_activation_roots(installed_settings)
                with pytest.raises(ValueError, match='runtime_roots_mismatch'):
                    require_retirement_activation_roots(installed_settings.model_copy(update={'kg_base_dir': str(tmp_path / 'kg')}))
                proof_path = activated['directory'] / 'retirement-activation/run.json'
                proof_bytes = proof_path.read_bytes()
                try:
                    proof_path.write_bytes(proof_bytes + b' ')
                    with pytest.raises(Exception, match='retirement_cutover_incomplete'):
                        await offline.require_retirement_runtime_admission(installed_engine)
                finally:
                    proof_path.write_bytes(proof_bytes)
                await offline.require_retirement_runtime_admission(installed_engine)
            finally:
                await installed_engine.dispose()
            mutated = dump(activated['database'])
            assert await installation.resume_retirement_activation(activated['directory'],
                expected_candidate_receipt_sha256=result['receipt_sha256'], confirm_installation_offline=True) == activated
            assert dump(activated['database']) == mutated
        else:
            with pytest.raises(ValueError, match='completion_.*pending'):
                await candidate.verify_reconciled_retirement_graph_candidate(*arguments, **completion_arguments)
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
