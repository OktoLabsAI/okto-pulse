"""Opt-in installed predecessor -> retirement bootstrap -> predecessor rollback.

Requires the frozen pair's wheels, checkouts and isolated interpreter, plus a
fresh byte-parity report for the candidate pair. No published release or runtime
promotion is inferred: layout recomposition below belongs only to this fixture.
"""

import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest
from okto_grafx import connect
from sqlalchemy.ext.asyncio import create_async_engine

from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
from okto_pulse.community.adapters import joint_recovery_snapshot as recovery
from okto_pulse.community.adapters import retirement_offline_run as offline
from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
from test_retirement_v034_source import restore_source, FIXTURES


def pair(packages, *, current=False):
    def wheel(edition):
        return packages[edition]['wheel']['sha256'] if current else packages[edition]['wheel_sha256']
    return recovery.RecoveryBuildPair(packages['core']['commit'], packages['community']['commit'],
        wheel('core'), wheel('community'))


def predecessor(mode, root, output):
    environment = {key: value for key, value in os.environ.items() if key.upper() != 'PYTHONPATH'}
    environment.update(PULSE_PREDECESSOR_FIXTURE=str(FIXTURES / 'f2_v034_source.json'),
        DATABASE_URL=f'sqlite+aiosqlite:///{root / "source.sqlite3"}', DATA_DIR=str(root),
        KG_BASE_DIR=str(root / 'kg'), KG_EMBEDDING_MODE='stub')
    result = subprocess.run([environment['PULSE_PREDECESSOR_PYTHON'],
        str(Path(__file__).with_name('retirement_predecessor_probe.py')), mode, str(root), str(output)],
        cwd=root, env=environment, text=True, capture_output=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(output.read_text(encoding='utf-8'))


@pytest.mark.skipif(not os.environ.get('PULSE_PREDECESSOR_PYTHON'), reason='requires isolated frozen predecessor pair')
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_real_predecessor_reopens_original_policy_and_native_history_after_bootstrap(tmp_path):
    live = tmp_path / 'live'
    live.mkdir()
    source = restore_source(live)
    for name in ('uploads', 'kg', 'backups'):
        (live / name).mkdir()
    before = predecessor('seed', live, tmp_path / 'before.json')
    assert [card['validation']['min_confidence'] for card in before['cards']] == [90, 60]
    expected = json.loads((FIXTURES / 'f2_v034_source.json').read_text())['source_builds']
    proof = json.loads(Path(os.environ['PULSE_CANDIDATE_PROVENANCE']).read_text())['packages']
    assert all(proof[edition]['byte_identical'] for edition in ('core', 'community'))
    original_pair, candidate_pair = pair(expected), pair(proof, current=True)
    engine = create_async_engine(f'sqlite+aiosqlite:///{source}')
    runtime = db.CommunityDatabaseRuntime(engine, db.build_community_session_factory(engine))
    storage = CommunityFileSystemStorage(str(live / 'uploads'))
    bindings = CommunityGraphBackendBindingStore(live / 'kg')
    binding = bindings.inspect_board_binding('board-a')
    graph = connect(binding.physical_path, page_size=binding.page_size)
    graphs = (recovery.RecoveryGraph(graph, 'board', 'board-a'),)
    try:
        run = await offline.prepare_offline_retirement_run(runtime, storage, graphs, live / 'backups', live / 'run',
            snapshot_id='predecessor', plan=ContextDispositionPlan(migration_id='predecessor-rollback',
                decision_reference='disposable frozen fixture without substantive Sprint context', decisions=()),
            source_builds=original_pair, migration_builds=candidate_pair,
            runtime_directories=(live, live / 'kg'), kg_base_dir=live / 'kg', max_seconds=180)
        result = await offline.resume_offline_retirement_bootstrap(runtime, storage, graphs, run,
            migration_builds=candidate_pair)
        assert result['state'] == 'bootstrap_complete'
        with sqlite3.connect(source) as sql:
            assert sql.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name='sprints'").fetchone() is None
        with pytest.raises(Exception, match='retirement_cutover_incomplete'):
            await offline.require_retirement_runtime_admission(engine)
        _, _, _, _, snapshot, _ = offline.read_offline_retirement_run(run)
    finally:
        graph.close()
        await runtime.close()
    restored = recovery.restore_joint_recovery_snapshot(snapshot, tmp_path / 'restored', builds=original_pair,
        current_storage_root=live / 'uploads', confirm_original_offline=True, max_seconds=180)
    # Fixture-only publication. Never touch the source roots or reuse a binding.
    def move(source, destination):
        assert source.resolve().is_relative_to(restored.resolve())
        assert destination.resolve().is_relative_to(restored.resolve()) and not destination.exists()
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
    move(restored / 'database.sqlite3', restored / 'source.sqlite3')
    move(restored / 'kg-artifacts', restored / 'kg')
    restored_bindings = CommunityGraphBackendBindingStore(restored / 'kg')
    destination = restored_bindings.board_grafx_path('board-a', 'original')
    move(restored / 'graph-0000', destination)
    with connect(destination, page_size=8192, read_only=True) as cold:
        restored_bindings.initialize_board_binding(board_id='board-a', backend='grafx', generation='original',
            physical_path=destination, page_size=8192, database=cold)
    after = predecessor('reopen', restored, tmp_path / 'after.json')
    assert after == before
