"""Restored candidates execute privately; failed attempts never touch originals."""

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from datetime import datetime, timezone

import pytest
from okto_grafx import connect
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core import get_settings
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import retirement_graph_candidate as candidate
from okto_pulse.community.adapters import retirement_candidate_execution as execution
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from test_retirement_offline_bootstrap import prepare
from test_retirement_offline_run import MIGRATION
from test_card_context_retirement import dump


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_failed_private_execution_can_retry_from_seed_and_keeps_original_providers(tmp_path, monkeypatch):
    runtime, storage, run, source = await prepare(tmp_path)
    ambient = get_settings()
    settings = CommunitySettings(kg_embedding_mode='stub', kg_embedding_dim=384)
    try:
        projection = await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        recovery = tmp_path / 'candidate-backups'
        recovery.mkdir()
        seed = await candidate.prepare_retirement_candidate_seed(runtime, storage, (), run, projection['projection_inputs'],
            migration_builds=MIGRATION, recovery_directory=recovery, seed_directory=tmp_path / 'seed')
        before = dump(source)
        target = tmp_path / 'projected'
        arguments = (runtime, storage, (), run, seed, target)
        reserve = execution.reserve_offline_consolidation
        committed = []
        @contextmanager
        def interrupt_after_ack(**kwargs):
            with reserve(**kwargs) as owner:
                class Interrupted:
                    is_authorized = owner.is_authorized
                    async def process_next(self):
                        result = await owner.process_next()
                        assert result.acked_count == 1
                        committed.append(result)
                        raise RuntimeError('interrupted after real candidate ACK')
                yield Interrupted()
        with monkeypatch.context() as scoped:
            scoped.setattr(execution, 'reserve_offline_consolidation', interrupt_after_ack)
            with pytest.raises(RuntimeError, match='interrupted after real candidate ACK'):
                await candidate.build_projected_retirement_graph_candidate(*arguments, migration_builds=MIGRATION,
                    settings=settings, confirm_original_offline=True, max_seconds=300)
        assert len(committed) == 1
        assert get_settings() is ambient and dump(source) == before
        assert not target.exists() and not list(tmp_path.glob('.projected.*.restore'))
        result = await candidate.build_projected_retirement_graph_candidate(*arguments, migration_builds=MIGRATION,
            settings=settings, confirm_original_offline=True, max_seconds=300)
        assert result['state'] == 'projected_not_reconciled'
        with pytest.raises(ValueError, match='checkpoint_target_missing'):
            await candidate.build_projected_retirement_graph_candidate(runtime, storage, (), run, seed,
                tmp_path / 'missing-projected', migration_builds=MIGRATION, settings=settings,
                confirm_original_offline=True, expected_receipt_sha256=result['receipt_sha256'])
        assert get_settings() is ambient and dump(source) == before
        document = json.loads((target / 'candidate-receipt/run.json').read_bytes())
        receipt_bytes = (target / 'projection-receipt/run.json').read_bytes()
        assert hashlib.sha256(receipt_bytes).hexdigest() == document['projection_receipt_sha256']
        receipt = json.loads(receipt_bytes)
        assert receipt['seed_sha256'] == seed.manifest_sha256
        assert receipt['before_sql'] != receipt['after_sql']
        assert len(receipt['boards']) == 1 and len(receipt['boards'][0]['acks']) == 3
        assert receipt['graph_reconciliation']['state'] == 'source_graph_reconciled'
        assert receipt['graph_reconciliation']['boards'][0]['node_count'] == 4
        assert receipt['graph_reconciliation']['boards'][0]['edge_count'] == 5
        assert receipt['graph_reconciliation']['boards'][0]['zero_orphan_validation'] == 'passed'
        assert receipt['graph_reconciliation']['boards'][0]['source_metadata_validation'] == 'passed'
        assert receipt['graph_reconciliation']['boards'][0]['source_metadata_root_count'] == 3
        assert receipt['graph_reconciliation']['boards'][0]['source_partition_validation'] == 'passed'
        assert receipt['graph_reconciliation']['boards'][0]['edge_session_validation'] == 'passed'
        assert receipt['graph_reconciliation']['boards'][0]['edge_session_count'] == 3
        assert receipt['historical_observations']['state'] == 'observed_not_classified'
        delta = receipt['historical_observations']['graphs'][0]['delta']
        # The full physical census also retains the BoardMeta schema record.
        assert sorted(node['node_type'] for node in delta['introduced_nodes']) == ['BoardMeta'] + ['Entity'] * 4
        assert len(delta['introduced_edges']) == 5
        assert not delta['removed_nodes'] and not delta['removed_edges'] and not delta['changed_nodes']
        from okto_pulse.core.ports.projection_effects import ProjectionPropertyEffects
        with closing(sqlite3.connect(target / 'database.sqlite3')) as sql:
            effects = [ProjectionPropertyEffects.from_payload(payload['projection_property_effects'])
                for (raw,) in sql.execute('SELECT payload FROM global_update_outbox')
                if 'projection_property_effects' in (payload := json.loads(raw))]
            assert effects  # Later sessions reuse nodes created by earlier sessions.
            for effect in effects:
                created = set(sql.execute('SELECT kuzu_node_type, kuzu_node_id FROM kuzu_node_refs WHERE session_id=?',
                    (effect.session_id,)))
                assert effect.board_id == 'board-a'
                assert not created & {(node.node_type, node.node_id) for node in effect.nodes}
        # ACK membership keeps the census task reference; graph roots use card.
        assert {ack['membership_source_ref'] for ack in receipt['boards'][0]['acks']} == {'spec:spec-a', 'task:card-a', 'task:card-b'}
        bindings = CommunityGraphBackendBindingStore(target / 'kg-artifacts')
        binding = bindings.inspect_board_binding('board-a')
        assert binding.generation == document['routes'][0]['generation']
        with connect(binding.physical_path, page_size=binding.page_size, read_only=True) as graph:
            refs = {row[0] for row in graph.execute('MATCH (n:Entity) RETURN n.source_artifact_ref').rows}
            assert {'spec:spec-a', 'card:card-a', 'card:card-b'} <= refs
            with closing(sqlite3.connect(target / 'database.sqlite3')) as sql:
                expected_dates = {
                    f'{kind}:{identity}': (created, updated, status)
                    for kind, table in (('spec', 'specs'), ('card', 'cards'))
                    for identity, created, updated, status in sql.execute(
                        f'SELECT id, created_at, updated_at, status FROM {table}')
                }
            for ref, created, updated, status in graph.execute(
                'MATCH (n:Entity) RETURN n.source_artifact_ref, '
                'n.source_created_at, n.source_updated_at, n.source_status'
            ).rows:
                if ref not in expected_dates:
                    # The Board reference cannot acquire its child's dates.
                    assert created is None and updated is None and status is None
                    continue
                sql_created, sql_updated, sql_status = expected_dates[ref]
                def micros(value):
                    stamp = datetime.fromisoformat(value)
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    return round(stamp.timestamp() * 1_000_000)
                assert created.micros == micros(sql_created)
                assert updated.micros == micros(sql_updated)
                assert status == sql_status
        candidate_engine = create_async_engine(f'sqlite+aiosqlite:///{target / "database.sqlite3"}')
        try:
            with pytest.raises(Exception, match='retirement_cutover_incomplete'):
                await offline.require_retirement_runtime_admission(candidate_engine)
        finally:
            await candidate_engine.dispose()
        with pytest.raises(ValueError, match='projected_replay_requires_checkpoint'):
            await candidate.build_projected_retirement_graph_candidate(*arguments, migration_builds=MIGRATION,
                settings=settings, confirm_original_offline=True)
        with pytest.raises(ValueError, match='replay_offline_required'):
            await candidate.build_projected_retirement_graph_candidate(*arguments, migration_builds=MIGRATION,
                settings=settings, confirm_original_offline=True,
                expected_receipt_sha256=result['receipt_sha256'])
        with pytest.raises(ValueError, match='checkpoint_receipt_mismatch'):
            await candidate.build_projected_retirement_graph_candidate(*arguments, migration_builds=MIGRATION,
                settings=settings, confirm_original_offline=True, confirm_candidate_offline=True,
                expected_receipt_sha256='0' * 64)
        replay = await candidate.build_projected_retirement_graph_candidate(*arguments, migration_builds=MIGRATION,
            settings=settings, confirm_original_offline=True, confirm_candidate_offline=True,
            expected_receipt_sha256=result['receipt_sha256'])
        assert replay == result
        from okto_pulse.core.ports.consolidation import ExactConsolidationAckReceipt
        from okto_pulse.community.adapters.retirement_candidate_sql_delta import verify_candidate_sql_delta
        from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline

        changed_sql = tmp_path / 'changed-candidate.sqlite3'
        with closing(sqlite3.connect(target / 'database.sqlite3')) as original, closing(sqlite3.connect(changed_sql)) as altered:
            original.backup(altered)
            with altered:
                altered.execute("UPDATE boards SET name='Unowned change' WHERE id='board-a'")
        seed_document = candidate.read_retirement_candidate_seed(seed)[0]
        acknowledgements = tuple(ExactConsolidationAckReceipt.from_payload(ack)
            for board in receipt['boards'] for ack in board['acks'])
        verified_delta = verify_candidate_sql_delta(
            Path(seed_document['snapshot']['directory']) / 'relational/database.sqlite3',
            target / 'database.sqlite3', acknowledgements, deadline=_deadline(60))
        assert verified_delta['state'] == 'receipt_owned_projection_effects'
        assert verified_delta['source_revision_delta'] == verified_delta['source_revision_expected_delta'] == 25
        with pytest.raises(ValueError, match='sql_delta_unclassified:boards'):
            verify_candidate_sql_delta(
                Path(seed_document['snapshot']['directory']) / 'relational/database.sqlite3',
                changed_sql, acknowledgements, deadline=_deadline(60))
        sidecar = Path(str(changed_sql) + '-wal')
        sidecar.write_bytes(b'uncheckpointed')
        with pytest.raises(ValueError, match='relational_snapshot_unexpected_sidecar'):
            verify_candidate_sql_delta(
                Path(seed_document['snapshot']['directory']) / 'relational/database.sqlite3',
                changed_sql, acknowledgements, deadline=_deadline(60))
        sidecar.unlink()
        changed_revision = tmp_path / 'changed-revision.sqlite3'
        with closing(sqlite3.connect(target / 'database.sqlite3')) as original, closing(
                sqlite3.connect(changed_revision)) as altered:
            original.backup(altered)
            with altered:
                altered.execute("UPDATE global_discovery_source_revision "
                    "SET revision=revision+1, mutation_nonce=lower(hex(randomblob(32)))")
        with pytest.raises(ValueError, match='revision_delta_unowned'):
            verify_candidate_sql_delta(
                Path(seed_document['snapshot']['directory']) / 'relational/database.sqlite3',
                changed_revision, acknowledgements, deadline=_deadline(60))
        from okto_pulse.community.adapters.retirement_candidate_graph_reconciliation import (
            verify_candidate_graph_reconciliation,
        )
        from okto_pulse.community.adapters.retirement_projection_inputs import read_retirement_projection_inputs
        source_plan = read_retirement_projection_inputs(projection['projection_inputs'])

        changed_graph_evidence = tmp_path / 'changed-graph-evidence'
        shutil.copytree(target, changed_graph_evidence)
        with closing(sqlite3.connect(changed_graph_evidence / 'database.sqlite3')) as altered:
            with altered:
                altered.execute("UPDATE kuzu_node_refs SET kuzu_node_id='unowned-node' "
                    "WHERE id=(SELECT id FROM kuzu_node_refs ORDER BY id LIMIT 1)")
        with pytest.raises(ValueError, match='graph_node_census_changed'):
            verify_candidate_graph_reconciliation(
                changed_graph_evidence, receipt['boards'], projection=source_plan, deadline=_deadline(60))
        changed_temporal = tmp_path / 'changed-temporal'
        shutil.copytree(target, changed_temporal)
        changed_binding = CommunityGraphBackendBindingStore(changed_temporal / 'kg-artifacts').inspect_board_binding('board-a')
        with connect(changed_binding.physical_path, page_size=changed_binding.page_size) as graph:
            original_date = graph.execute("MATCH (n:Entity) WHERE n.source_artifact_ref='card:card-a' "
                "RETURN n.source_created_at").rows[0][0]
            with graph.begin('write') as writer:
                writer.execute("MATCH (n:Entity) WHERE n.source_artifact_ref='card:card-a' "
                    "SET n.source_created_at=NULL")
            graph.checkpoint()
        with pytest.raises(ValueError, match='source_metadata_changed:source_created_at'):
            verify_candidate_graph_reconciliation(changed_temporal, receipt['boards'],
                projection=source_plan, deadline=_deadline(60))
        with connect(changed_binding.physical_path, page_size=changed_binding.page_size) as graph:
            with graph.begin('write') as writer:
                writer.execute("MATCH (n:Entity) WHERE n.source_artifact_ref='card:card-a' "
                    "SET n.source_created_at=$stamp", {'stamp': original_date})
                writer.execute("MATCH (n:Entity) WHERE n.source_artifact_ref='board:board-a' "
                    "SET n.source_created_at=$stamp", {'stamp': original_date})
            graph.checkpoint()
        with pytest.raises(ValueError, match='source_metadata_changed:source_created_at'):
            verify_candidate_graph_reconciliation(changed_temporal, receipt['boards'],
                projection=source_plan, deadline=_deadline(60))
        marker = target / 'unexpected-payload'
        marker.write_text('candidate changed after checkpoint')
        with pytest.raises(ValueError, match='checkpoint_content_changed'):
            await candidate.build_projected_retirement_graph_candidate(*arguments, migration_builds=MIGRATION,
                settings=settings, confirm_original_offline=True, confirm_candidate_offline=True,
                expected_receipt_sha256=result['receipt_sha256'])
        marker.unlink()
        assert (target / 'projection-receipt/run.json').read_bytes() == receipt_bytes
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.timeout(480)
async def test_bug_candidate_uses_latest_source_done_event(tmp_path, monkeypatch):
    import test_retirement_offline_bootstrap as fixture

    restore = fixture.restore_source

    def bug_source(directory):
        path = restore(directory)
        # Modify only the disposable predecessor before its signed snapshot.
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute("UPDATE cards SET card_type='bug', status='done', "
                    "severity='critical', origin_task_id='card-b' WHERE id='card-a'")
                for identity, day, old, new in (
                    ('first', 2, 'in_progress', 'done'),
                    ('reopened', 3, 'done', 'in_progress'),
                    ('last', 4, 'in_progress', 'done'),
                ):
                    connection.execute("INSERT INTO domain_events "
                        "(id,event_type,board_id,payload_json,occurred_at) VALUES (?,?,?,?,?)",
                        (identity, 'card.moved', 'board-a', json.dumps({
                            'card_id': 'card-a', 'from_status': old, 'to_status': new,
                        }), f'2001-01-{day:02d} 00:00:00'))
        return path

    monkeypatch.setattr(fixture, 'restore_source', bug_source)
    runtime, storage, run, source = await prepare(tmp_path)
    try:
        projection = await offline.prepare_offline_retirement_projection_inputs(runtime, storage, (), run,
            migration_builds=MIGRATION, projection_directory=tmp_path / 'projection')
        recovery = tmp_path / 'candidate-backups'
        recovery.mkdir()
        seed = await candidate.prepare_retirement_candidate_seed(runtime, storage, (), run,
            projection['projection_inputs'], migration_builds=MIGRATION,
            recovery_directory=recovery, seed_directory=tmp_path / 'seed')
        target = tmp_path / 'projected'
        before = dump(source)
        await candidate.build_projected_retirement_graph_candidate(runtime, storage, (), run, seed,
            target, migration_builds=MIGRATION,
            settings=CommunitySettings(kg_embedding_mode='stub', kg_embedding_dim=384),
            confirm_original_offline=True, max_seconds=300)
        binding = CommunityGraphBackendBindingStore(target / 'kg-artifacts').inspect_board_binding('board-a')
        with connect(binding.physical_path, page_size=binding.page_size, read_only=True) as graph:
            rows = graph.execute("MATCH (n:Bug) WHERE n.source_artifact_ref='card:card-a' "
                "RETURN n.resolved_at, n.source_status, n.severity, n.created_at").rows
            assert len(rows) == 1
            resolved, status, severity, created = rows[0]
            assert resolved.micros == int(datetime(2001, 1, 4, tzinfo=timezone.utc).timestamp() * 1_000_000)
            assert (status, severity) == ('done', 'critical')
            assert created.micros != resolved.micros
        assert dump(source) == before
    finally:
        await runtime.close()


@pytest.mark.parametrize('lose_at,committed', [(1, False), (3, False), (4, True)])
def test_enqueue_rechecks_authority_before_and_after_commit(tmp_path, lose_at, committed):
    from test_f06_community_rebuild_effects import _queue_db
    from okto_pulse.community.adapters.board_rebuild_ingestion import CommunityBoardRebuildIngestionAdapter
    source = _queue_db(tmp_path)
    calls = []
    def authority():
        calls.append(len(calls) + 1)
        return len(calls) < lose_at
    with pytest.raises(RuntimeError, match='rebuild_enqueue_authority_lost'):
        CommunityBoardRebuildIngestionAdapter(db_path=source).enqueue_sources(board_id='board', run_id='run',
            sources=({'artifact_type': 'story', 'id': 'story', 'source_ref': 'story:story',
                'source_version': '1', 'content_hash': 'a' * 64},), mutation_guard=authority)
    with sqlite3.connect(source) as connection:
        assert connection.execute('SELECT count(*) FROM consolidation_queue').fetchone()[0] == int(committed)
