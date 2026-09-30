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


@pytest.fixture
async def coordinator_graph_runtime(runtime, independent_gates, monkeypatch, tmp_path):
    # The source represents a runtime whose existing startup migrations ran
    # before authorship. Base.create_all alone leaves synthetic legacy aliases.
    factory, _, _, _ = runtime
    with monkeypatch.context() as scoped:
        scoped.setattr(relational_schema_steps, 'get_session_factory', lambda: factory)
        await relational_schema_steps._migrate_heal_task_validation_field_names()
    async for value in _graph_runtime.__wrapped__(runtime, independent_gates, monkeypatch, tmp_path):
        yield value


async def enable_fence(factory, monkeypatch):
    with monkeypatch.context() as scoped:
        scoped.setattr(relational_schema_steps, 'get_engine', lambda: factory.kw['bind'])
        await relational_schema_steps._migrate_global_discovery_recovery_control_plane()


async def test_coordinator_retains_learning_phase_and_replays_checkpoint(coordinator_graph_runtime, monkeypatch, tmp_path):
    from okto_grafx import connect
    from okto_pulse.core.ports.context_disposition import ContextDispositionPlan
    from okto_pulse.community.adapters import retirement_offline_run as offline, retirement_graph_candidate as candidate
    from okto_pulse.community.adapters.sqlalchemy_database import CommunityDatabaseRuntime
    from okto_pulse.community.adapters.storage import CommunityFileSystemStorage
    from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
    from okto_pulse.community.adapters.joint_recovery_snapshot import RecoveryGraph, RecoveryBuildPair
    from okto_pulse.community.config import CommunitySettings
    from logical_transfer_matrix_support import Corpus, seed_generation

    # Explicit disposable build anchors; avoid importing another test suite
    # after the graph fixture places Core's homonymous helpers on sys.path.
    source_builds = RecoveryBuildPair('a' * 40, 'b' * 40, 'c' * 64, 'd' * 64)
    migration_builds = RecoveryBuildPair('e' * 40, 'f' * 40, '1' * 64, '2' * 64)

    runtime, _, _, _ = coordinator_graph_runtime
    factory, _, _, _ = runtime
    from legacy_sprint_schema import Base as LegacyBase, RETIRED_TABLES
    # Model an upgrade predecessor, not an already-retired runtime. Retired
    # tables and the nullable Card origin are isolated fixture DDL only.
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(lambda sync: LegacyBase.metadata.create_all(sync,
            tables=[LegacyBase.metadata.tables[name] for name in RETIRED_TABLES]))
        await connection.exec_driver_sql('ALTER TABLE cards ADD COLUMN sprint_id VARCHAR(36) REFERENCES sprints(id) ON DELETE SET NULL')
        await connection.execute(LegacyBase.metadata.tables['sprints'].insert().values(
            id='legacy-empty', board_id=BOARD, spec_id='spec-bug-context', title='Empty historical container', created_by='fixture-owner'))
    await enable_fence(factory, monkeypatch)
    source_runtime = CommunityDatabaseRuntime(factory.kw['bind'], factory)
    storage = CommunityFileSystemStorage(str(tmp_path / 'upgrade-uploads'))
    (tmp_path / 'upgrade-uploads').mkdir()
    (tmp_path / 'upgrade-backups').mkdir()
    (tmp_path / 'upgrade-candidate-backups').mkdir()
    bindings = CommunityGraphBackendBindingStore(tmp_path / 'upgrade-kg')
    path = bindings.board_grafx_path(BOARD, 'original')
    path.parent.mkdir(parents=True)
    seed_generation('grafx', path, Corpus(*read_graph()))
    graph = connect(path, page_size=8192)
    bindings.initialize_board_binding(board_id=BOARD, backend='grafx', generation='original',
        physical_path=path, page_size=8192, database=graph)
    graphs = (RecoveryGraph(graph, 'board', BOARD),)
    try:
        run = await offline.prepare_offline_retirement_run(source_runtime, storage, graphs,
            tmp_path / 'upgrade-backups', tmp_path / 'upgrade-run', snapshot_id='original',
            plan=ContextDispositionPlan(migration_id='learning-upgrade-fixture',
                decision_reference='disposable fixture without Sprint context', decisions=()),
            source_builds=source_builds, migration_builds=migration_builds, runtime_directories=(tmp_path, tmp_path / 'upgrade-kg'),
            kg_base_dir=tmp_path / 'upgrade-kg', evidence_root=tmp_path / 'evidence', max_seconds=180)
        from okto_pulse.community.adapters import retirement_bootstrap
        captured_cards = []
        read_cards = retirement_bootstrap._load_cards
        async def observe_cards(*args, **kwargs):
            rows = await read_cards(*args, **kwargs)
            captured_cards.append(deepcopy(rows))
            return rows
        with monkeypatch.context() as scoped:
            scoped.setattr(retirement_bootstrap, '_load_cards', observe_cards)
            try:
                prepared = await offline.prepare_offline_retirement_projection_inputs(source_runtime, storage, graphs, run,
                    migration_builds=migration_builds, projection_directory=tmp_path / 'upgrade-projection')
            except ValueError:
                if len(captured_cards) >= 2:
                    before_cards = {row['id']: row for row in captured_cards[-2]}
                    delta = [(row['id'], key, before_cards.get(row['id'], {}).get(key), value)
                        for row in captured_cards[-1] for key, value in row.items()
                        if key != 'position' and before_cards.get(row['id'], {}).get(key) != value]
                    (tmp_path / 'bootstrap-card-delta.json').write_text(json.dumps(delta, default=str), encoding='utf-8')
                raise
        seed = await candidate.prepare_retirement_candidate_seed(source_runtime, storage, graphs, run,
            prepared['projection_inputs'], migration_builds=migration_builds,
            recovery_directory=tmp_path / 'upgrade-candidate-backups', seed_directory=tmp_path / 'upgrade-seed')
        before = await sql_cells(factory)
        graph.close()
        target = tmp_path / 'upgrade-candidate'
        settings = CommunitySettings(kg_embedding_mode='stub', kg_embedding_dim=384)
        args = (source_runtime, storage, graphs, run, seed, target)
        try:
            result = await candidate.build_projected_retirement_graph_candidate(*args,
                migration_builds=migration_builds, settings=settings, confirm_original_offline=True, max_seconds=300)
        except ValueError as error:
            # Diagnostic only: preserve the original failure and record the
            # exact disposable edge rejected by the production contract.
            trace = error.__traceback__
            while trace is not None:
                if trace.tb_frame.f_code.co_name == '_board_graph':
                    relation = trace.tb_frame.f_locals.get('relation')
                    if relation is not None:
                        diagnostic = {name: getattr(relation, name) for name in
                            ('layout_name', 'source_type', 'source_key', 'target_type', 'target_key')}
                        diagnostic['properties'] = dict(relation.properties)
                        (tmp_path / 'candidate-edge-diagnostic.json').write_text(
                            json.dumps(diagnostic, default=str, indent=2), encoding='utf-8')
                trace = trace.tb_next
            raise
        report = json.loads((target / 'projection-receipt/run.json').read_bytes())
        assert report['format'] == 'retirement-candidate-projection/v7'
        transition, = report['historical_observations']['supersedence_effects']
        assert transition['predecessor']['before']['node_id'] == 'canonical-bug'
        assert transition['predecessor']['after']['node_id'] == 'canonical-bug'
        assert transition['edge']['edge_type'] == 'supersedes'
        assert transition['edge']['source_id'] == transition['successor']['node_id']
        assert transition['edge']['target_id'] == 'canonical-bug'
        assert report['graph_reconciliation']['state'] == 'learning_reconciliation_pending'
        assert report['graph_reconciliation']['learning_execution_count'] >= 1
        projected = report['graph_reconciliation']['projection_with_before_learning_boards']
        board = next(row for row in projected['boards'] if row['board_id'] == BOARD)
        relations = board['source_relation_comparison']
        assert relations['unresolved_count'] == relations['missing_count'] == 0
        assert relations['matched_count'] == relations['expected_count']
        assert await sql_cells(factory) == before
        assert await candidate.build_projected_retirement_graph_candidate(*args,
            migration_builds=migration_builds, settings=settings, confirm_original_offline=True,
            confirm_candidate_offline=True, expected_receipt_sha256=result['receipt_sha256'], max_seconds=300) == result
        with pytest.raises(ValueError, match='retirement_completion_learning_pending'):
            await candidate.verify_reconciled_retirement_graph_candidate(*args,
                migration_builds=migration_builds, settings=settings, confirm_original_offline=True,
                confirm_candidate_offline=True, expected_receipt_sha256=result['receipt_sha256'], max_seconds=300)
    finally:
        graph.close()


async def test_private_phase_uses_isolated_candidate_sql_graph_and_signed_evidence(graph_runtime, monkeypatch, tmp_path):
    import shutil
    from okto_grafx import connect
    from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
    from okto_pulse.community.adapters.relational_recovery_snapshot import create_sqlite_recovery_snapshot
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
        verified = await execution.verify_candidate_learning_phase(stage,
            expected_receipt_sha256=result['receipt_sha256'], board_ids=(BOARD,), generation='private-generation')
    boards = verified.boards
    assert verified.document == document
    assert verified.baseline_database == result['directory'] / 'baseline/database.sqlite3'
    assert verified.initial_graphs == {BOARD: result['directory'] / '000000/000000-before.graph.jsonl'}
    assert len(boards) == 1 and boards[0]['steps'][0]['execution']['materialized'] is True
    assert boards[0]['steps'][0]['sql_delta']['source_append_count'] == 1
    report_path = result['directory'] / 'receipt/run.json'
    original = report_path.read_bytes()
    forged = deepcopy(document)
    forged['baseline'] = {**document['terminal'], 'directory': 'baseline'}
    encoded = _encode(forged)
    report_path.write_bytes(encoded)
    try:
        with pytest.raises(ValueError):
            await execution.verify_candidate_learning_phase(stage,
                expected_receipt_sha256=hashlib.sha256(encoded).hexdigest(),
                board_ids=(BOARD,), generation='private-generation')
    finally:
        report_path.write_bytes(original)
    with pytest.raises(ValueError, match='receipt_invalid'):
        await execution.verify_candidate_learning_phase(stage,
            expected_receipt_sha256=result['receipt_sha256'], board_ids=(BOARD,), generation='other-generation')


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
