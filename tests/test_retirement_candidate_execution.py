"""Restored candidates execute privately; failed attempts never touch originals."""

from contextlib import contextmanager
import hashlib
import json
import sqlite3

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
        assert get_settings() is ambient and dump(source) == before
        document = json.loads((target / 'candidate-receipt/run.json').read_bytes())
        receipt_bytes = (target / 'projection-receipt/run.json').read_bytes()
        assert hashlib.sha256(receipt_bytes).hexdigest() == document['projection_receipt_sha256']
        receipt = json.loads(receipt_bytes)
        assert receipt['seed_sha256'] == seed.manifest_sha256
        assert receipt['before_sql'] != receipt['after_sql']
        assert len(receipt['boards']) == 1 and len(receipt['boards'][0]['acks']) == 3
        # ACK membership keeps the census task reference; graph roots use card.
        assert {ack['membership_source_ref'] for ack in receipt['boards'][0]['acks']} == {'spec:spec-a', 'task:card-a', 'task:card-b'}
        bindings = CommunityGraphBackendBindingStore(target / 'kg-artifacts')
        binding = bindings.inspect_board_binding('board-a')
        assert binding.generation == document['routes'][0]['generation']
        with connect(binding.physical_path, page_size=binding.page_size, read_only=True) as graph:
            refs = {row[0] for row in graph.execute('MATCH (n:Entity) RETURN n.source_artifact_ref').rows}
            assert {'spec:spec-a', 'card:card-a', 'card:card-b'} <= refs
        candidate_engine = create_async_engine(f'sqlite+aiosqlite:///{target / "database.sqlite3"}')
        try:
            with pytest.raises(Exception, match='retirement_cutover_incomplete'):
                await offline.require_retirement_runtime_admission(candidate_engine)
        finally:
            await candidate_engine.dispose()
        with pytest.raises(ValueError, match='projected_replay_requires_checkpoint'):
            await candidate.build_projected_retirement_graph_candidate(*arguments, migration_builds=MIGRATION,
                settings=settings, confirm_original_offline=True)
        assert (target / 'projection-receipt/run.json').read_bytes() == receipt_bytes
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
