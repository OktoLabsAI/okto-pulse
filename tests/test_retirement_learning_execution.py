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


@pytest.mark.parametrize('missing_bug', [False, True])
async def test_retained_board_execution_rederives_cold_and_refuses_report_tampering(
        graph_runtime, monkeypatch, tmp_path, missing_bug):
    runtime, _, _, _ = graph_runtime
    factory, _, _, _ = runtime
    await enable_fence(factory, monkeypatch)
    if missing_bug:
        graph_rows("MATCH (b:Bug) WHERE b.id = 'canonical-bug' DETACH DELETE b")
    source = Path(factory.kw['bind'].url.database)
    root = tmp_path / 'learning-execution'
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
