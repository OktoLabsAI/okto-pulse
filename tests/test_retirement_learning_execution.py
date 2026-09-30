"""Private retained execution and cold rederivation on real SQL and Grafx."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from okto_pulse.community.adapters import retirement_learning_execution as execution
from okto_pulse.community.adapters import relational_schema_steps
from okto_pulse.community.adapters.logical_transfer_schema import board_logical_schema
from okto_pulse.community.adapters.sprint_retirement_archive import _encode
from okto_pulse.core.composition import isolated_runtime_provider_scope
from test_learning_reconciliation_effects import graph_cells, sql_cells
from test_learning_materialization_writer import (
    BOARD, runtime as _runtime, independent_gates as _independent_gates,
    graph_runtime as _graph_runtime, graph_rows,
)

runtime = _runtime
independent_gates = _independent_gates
graph_runtime = _graph_runtime
pytestmark = pytest.mark.asyncio


def read_graph():
    return (board_logical_schema(), *graph_cells())


async def enable_fence(factory, monkeypatch):
    monkeypatch.setattr(relational_schema_steps, 'get_engine', lambda: factory.kw['bind'])
    await relational_schema_steps._migrate_global_discovery_recovery_control_plane()


async def test_private_phase_uses_isolated_candidate_sql_graph_and_signed_evidence(graph_runtime, monkeypatch, tmp_path):
    import shutil
    from okto_grafx import connect
    from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
    from okto_pulse.community.adapters.relational_recovery_snapshot import create_sqlite_recovery_snapshot, _deadline
    from okto_pulse.community.adapters.retirement_candidate_global_reconciliation import _read
    from okto_pulse.community.config import CommunitySettings
    from logical_transfer_matrix_support import Corpus, seed_generation

    runtime, _, _, _ = graph_runtime
    factory, _, store, _ = runtime
    await enable_fence(factory, monkeypatch)
    source = Path(factory.kw['bind'].url.database)
    original_sql, original_graph = await sql_cells(factory), read_graph()
    original_history = await store.enumerate(BOARD)
    stage = create_sqlite_recovery_snapshot(source, tmp_path, snapshot_id='private-stage').directory
    (stage / 'uploads').mkdir()
    # Only disposable fixture evidence is copied; the production coordinator
    # already restores authenticated evidence through its recovery window.
    shutil.copytree(tmp_path / 'evidence', stage / 'evidence')
    bindings = CommunityGraphBackendBindingStore(stage / 'kg-artifacts')
    path = bindings.board_grafx_path(BOARD, 'private-generation')
    path.parent.mkdir(parents=True)
    seed_generation('grafx', path, Corpus(*original_graph))
    with connect(path, page_size=8192) as graph:
        bindings.initialize_board_binding(board_id=BOARD, backend='grafx', generation='private-generation',
            physical_path=path, page_size=8192, database=graph)
    result = await execution.execute_candidate_learning_phase(stage, board_ids=(BOARD,),
        settings=CommunitySettings(kg_embedding_mode='stub', kg_embedding_dim=384),
        generation='private-generation', lifetime_probe=lambda: True, max_seconds=180)
    document = result['document']
    assert document['state'] == 'retained_not_reconciled'
    assert await sql_cells(factory) == original_sql
    assert read_graph() == original_graph
    assert await store.enumerate(BOARD) == original_history
    with isolated_runtime_provider_scope(inherit=False):
        boards = await execution.verify_candidate_learning_chain(result['directory'], document['boards'],
            board_ids=(BOARD,), baseline_database=result['directory'] / 'baseline/database.sqlite3',
            candidate_database=stage / 'database.sqlite3',
            read_graph=lambda board: _read(bindings.inspect_board_binding(board), 'board', _deadline(60)))
    assert len(boards) == 1 and boards[0]['steps'][0]['execution']['materialized'] is True
    assert boards[0]['steps'][0]['sql_delta']['source_append_count'] == 1


@pytest.mark.parametrize('missing_bug', [False, True])
async def test_retained_board_execution_rederives_cold_and_refuses_report_tampering(
        graph_runtime, monkeypatch, tmp_path, missing_bug):
    runtime, _, _, _ = graph_runtime
    factory, _, _, _ = runtime
    await enable_fence(factory, monkeypatch)
    if missing_bug:
        graph_rows("MATCH (b:Bug) WHERE b.id = 'canonical-bug' DETACH DELETE b")
    source = Path(factory.kw['bind'].url.database)
    chain = tmp_path / 'learning-chain'
    chain.mkdir()
    root = chain / '000000'
    result = await execution.execute_candidate_learning_board(source, root, board_id=BOARD,
        relational_scope_factory=factory, read_graph=read_graph, require_live=lambda: True, max_seconds=180)
    before = await sql_cells(factory)
    with isolated_runtime_provider_scope(inherit=False):
        document = await execution.verify_candidate_learning_board(root,
            expected_receipt_sha256=result['receipt_sha256'])
    assert document['state'] == 'retained_not_reconciled' and len(document['steps']) == 1
    step, = document['steps']
    assert step['execution']['materialized'] is (not missing_bug)
    if missing_bug:
        assert step['execution']['consolidation_session_id'] is None
        assert step['sql_delta']['changed_tables'] == []
        assert step['graph_delta'] == {'state': 'unchanged_without_committed_session'}
    else:
        assert step['sql_delta']['source_append_count'] == step['graph_delta']['introduced_nodes'] == 1
    from okto_pulse.community.adapters.relational_recovery_snapshot import create_sqlite_recovery_snapshot
    terminal = create_sqlite_recovery_snapshot(source, tmp_path, snapshot_id='final-candidate')
    entries = [{'board_id': BOARD, 'directory': '000000', 'receipt_sha256': result['receipt_sha256']}]
    graphs = read_graph()
    options = dict(board_ids=(BOARD,), baseline_database=root / '000000-before/database.sqlite3',
        candidate_database=terminal.directory / 'database.sqlite3', read_graph=lambda board: graphs)
    with isolated_runtime_provider_scope(inherit=False):
        assert await execution.verify_candidate_learning_chain(chain, entries, **options) == (document,)
        with pytest.raises(ValueError, match='final_graph_changed'):
            await execution.verify_candidate_learning_chain(chain, entries,
                **{**options, 'read_graph': lambda board: (graphs[0], (), ())})
        with pytest.raises(ValueError, match='scope_invalid'):
            await execution.verify_candidate_learning_chain(chain,
                [{**entries[0], 'directory': '../000000'}], **options)
        if not missing_bug:
            with pytest.raises(ValueError, match='sql_boundary_changed'):
                await execution.verify_candidate_learning_chain(chain, entries,
                    **{**options, 'baseline_database': terminal.directory / 'database.sqlite3'})
            with pytest.raises(ValueError, match='final_sql_changed'):
                await execution.verify_candidate_learning_chain(chain, entries,
                    **{**options, 'candidate_database': options['baseline_database']})
    with pytest.raises(FileExistsError):
        await execution.execute_candidate_learning_board(source, root, board_id=BOARD,
            relational_scope_factory=factory, read_graph=read_graph, require_live=lambda: True)
    assert await sql_cells(factory) == before
    report_path = root / 'receipt/run.json'
    original = report_path.read_bytes()
    forged = deepcopy(document)
    forged['steps'][0]['sql_delta']['nodes_added'] += 1
    encoded = _encode(forged)
    report_path.write_bytes(encoded)
    try:
        # Even a newly supplied external digest cannot replace rederivation.
        with pytest.raises(ValueError, match='proof_changed'):
            await execution.verify_candidate_learning_board(root,
                expected_receipt_sha256=hashlib.sha256(encoded).hexdigest())
    finally:
        report_path.write_bytes(original)
    graph_path = root / document['initial']['graph']['file']
    graph_path.write_bytes(graph_path.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='graph_file_changed'):
        await execution.verify_candidate_learning_board(root, expected_receipt_sha256=result['receipt_sha256'])


async def test_lost_fence_after_real_commit_never_publishes_a_reconciled_receipt(graph_runtime, monkeypatch, tmp_path):
    runtime, _, _, _ = graph_runtime
    factory, _, _, _ = runtime
    await enable_fence(factory, monkeypatch)
    source = Path(factory.kw['bind'].url.database)
    before = await sql_cells(factory)
    live = [True]
    governed = execution.execute_learning_reconciliation
    async def lose_fence(**kwargs):
        result = await governed(**kwargs)
        assert result.materialized and result.consolidation_session_id
        live[0] = False
        return result
    monkeypatch.setattr(execution, 'execute_learning_reconciliation', lose_fence)
    root = tmp_path / 'interrupted-learning'
    with pytest.raises(ValueError, match='fence_lost'):
        await execution.execute_candidate_learning_board(source, root, board_id=BOARD,
            relational_scope_factory=factory, read_graph=read_graph, require_live=lambda: live[0])
    assert await sql_cells(factory) != before
    assert (root / '000000-before/database.sqlite3').is_file()
    assert not (root / 'receipt').exists()
    assert not (root / '000000-after').exists()
    assert json.loads((root / '000000-before/manifest.json').read_bytes())['database_file'] == 'database.sqlite3'


async def test_pending_reuse_precedes_older_origin_and_retains_a_contiguous_chain(graph_runtime, monkeypatch, tmp_path):
    from test_learning_reuse_materialization import add_second_bug, stage_reuse
    from okto_pulse.core.domain.learning_materialization_work import parse_learning_capture_work_ref
    runtime, _, selection, persister = graph_runtime
    factory, _, _, _ = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection, raise_failures=True)
    await add_second_bug(factory)
    await stage_reuse(graph_runtime, bug_id='second-bug')
    await enable_fence(factory, monkeypatch)
    root = tmp_path / 'two-origins'
    result = await execution.execute_candidate_learning_board(Path(factory.kw['bind'].url.database), root,
        board_id=BOARD, relational_scope_factory=factory, read_graph=read_graph, require_live=lambda: True)
    with isolated_runtime_provider_scope(inherit=False):
        document = await execution.verify_candidate_learning_board(root,
            expected_receipt_sha256=result['receipt_sha256'])
    assert [parse_learning_capture_work_ref(step['execution']['work_ref']).bug_id
        for step in document['steps']] == ['second-bug', 'bug-context']
    assert all(step['execution']['materialized'] for step in document['steps'])
    assert [step['sql_delta']['source_append_count'] for step in document['steps']] == [1, 0]
