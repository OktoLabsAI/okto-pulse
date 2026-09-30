"""Retained Learning execution inside a caller-owned private recovery window.

The enclosing coordinator owns original/candidate offline fences, runtime
composition and whole-stage cleanup on failure. No standalone maintenance
entrypoint or completion authority is provided here.
"""
import asyncio
from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import time

from okto_pulse.core.kg.logical_transfer import count_graph, decode_artifact, LogicalFingerprintAccumulator, schema_digest
from okto_pulse.core.ports.learning_reconciliation import (
    LearningReconciliationExecution, execute_learning_reconciliation,
    qualify_learning_reconciliation_graph_delta, plan_learning_reconciliation_execution,
)

from .logical_graph_file import publish_logical_graph_file
from .relational_recovery_snapshot import (
    SqliteRecoverySnapshot, _path, _digest, _readonly, _deadline, _check_time,
    create_sqlite_recovery_snapshot, verify_sqlite_recovery_snapshot,
)
from .retirement_candidate_checkpoint import _read_sealed
from .retirement_candidate_sql_delta import _changed_rows
from .retirement_learning_history import CandidateLearningHistory
from .retirement_learning_sql_delta import _history, verify_learning_sql_delta
from .retirement_offline_run import _seal
from .sprint_retirement_archive import _encode


async def _blocking(function, *args, **kwargs):
    # Backup/publication must become terminal before the enclosing coordinator
    # can remove its stage on cancellation. Never abandon a filesystem worker.
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            task.result()
        except BaseException as failure:
            cancelled.add_note(f'retained worker terminated with {type(failure).__name__}')
        raise cancelled


def _remaining(deadline):
    _check_time(deadline)
    return deadline - time.monotonic()


def _capture(database, root, name, read_graph, require_live, deadline):
    def live(*_):
        _check_time(deadline)
        if require_live() is not True:
            raise ValueError('retirement_learning_execution_fence_lost')
    live()
    graph_path = _path(root / (name + '.graph.jsonl'))
    if graph_path.exists():
        raise FileExistsError('retirement_learning_frame_exists')
    schema, nodes, relations = read_graph()
    if (schema.scope != 'board' or type(nodes) is not tuple or type(relations) is not tuple
            or len(nodes) > 100_000 or len(relations) > 500_000):
        raise ValueError('retirement_learning_graph_limit')
    certificate = publish_logical_graph_file(graph_path, schema, nodes, relations, counts=count_graph(nodes, relations))
    if graph_path.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('retirement_learning_graph_file_limit')
    live()
    snapshot = create_sqlite_recovery_snapshot(database, root, snapshot_id=name,
        max_seconds=_remaining(deadline), progress=live)
    live()
    return {'sql': {'directory': name, 'manifest_sha256': snapshot.manifest_sha256,
        'database_sha256': snapshot.database_sha256},
        'graph': {'file': graph_path.name, 'sha256': _digest(graph_path),
            'certificate': json.loads(_encode(asdict(certificate)))}}


def _read_frame(root, frame, name, deadline):
    if (type(frame) is not dict or set(frame) != {'sql', 'graph'}
            or type(frame['sql']) is not dict or set(frame['sql']) != {'directory', 'manifest_sha256', 'database_sha256'}
            or type(frame['graph']) is not dict or set(frame['graph']) != {'file', 'sha256', 'certificate'}
            or frame['sql']['directory'] != name or frame['graph']['file'] != name + '.graph.jsonl'):
        raise ValueError('retirement_learning_frame_invalid')
    snapshot = SqliteRecoverySnapshot(_path(root / name), frame['sql']['manifest_sha256'], frame['sql']['database_sha256'])
    verify_sqlite_recovery_snapshot(snapshot, max_seconds=_remaining(deadline))
    graph_path = _path(root / frame['graph']['file'])
    if graph_path.stat().st_size > 128 * 1024 * 1024 or _digest(graph_path) != frame['graph']['sha256']:
        raise ValueError('retirement_learning_graph_file_changed')
    def lines():
        with graph_path.open('r', encoding='utf-8', newline='') as stream:
            for line in stream:
                _check_time(deadline)
                yield line.removesuffix('\n')
    graph = decode_artifact(lines())
    certificate = {'scope': graph.header.scope, 'counts': asdict(graph.manifest.counts),
        'schema_digest': graph.header.schema_digest, 'fingerprint': graph.manifest.fingerprint,
        'stream_checksum': graph.manifest.stream_checksum}
    if (certificate != frame['graph']['certificate'] or graph.header.scope != 'board'
            or len(graph.nodes) > 100_000 or len(graph.relations) > 500_000):
        raise ValueError('retirement_learning_graph_certificate_changed')
    return snapshot.directory / 'database.sqlite3', graph


def _same_graph(left, right):
    return left.header.schema_digest == right.header.schema_digest and left.manifest.fingerprint == right.manifest.fingerprint


def _same_sql(left, right, deadline):
    if _digest(left) == _digest(right):
        return True
    with closing(_readonly(left, immutable=True)) as before, closing(_readonly(right, immutable=True)) as after:
        before.execute('BEGIN')
        after.execute('BEGIN')
        schema = 'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name'
        return before.execute(schema).fetchall() == after.execute(schema).fetchall() and not _changed_rows(before, after, deadline)


def _source_history(database, deadline):
    with closing(_readonly(database, immutable=True)) as connection:
        connection.execute('BEGIN')
        return _history(connection, deadline)[1]


def _select(database, graph, board_id, deadline):
    records = _source_history(database, deadline)
    plan = plan_learning_reconciliation_execution(schema=graph.header.schema, board_id=board_id,
        records=tuple(asdict(row) for row in records if row.board_id == board_id), nodes=graph.nodes)
    return json.loads(_encode([asdict(row) for row in plan.selections])), plan.work_refs


async def _proof(before, after, execution, deadline):
    old_sql, old_graph = before
    new_sql, new_graph = after
    sql = await verify_learning_sql_delta(old_sql, new_sql, execution=execution, deadline=deadline)
    if old_graph.header.schema_digest != new_graph.header.schema_digest:
        raise ValueError('retirement_learning_graph_schema_changed')
    if execution.consolidation_session_id is None:
        if not _same_graph(old_graph, new_graph):
            raise ValueError('retirement_learning_unacknowledged_graph_effects')
        graph = {'state': 'unchanged_without_committed_session'}
    else:
        history = CandidateLearningHistory(_source_history(new_sql, deadline))
        graph = await qualify_learning_reconciliation_graph_delta(None, history,
            schema=new_graph.header.schema, execution=execution, before_nodes=old_graph.nodes,
            before_relations=old_graph.relations, after_nodes=new_graph.nodes, after_relations=new_graph.relations)
        if graph['introduced_nodes'] != sql['nodes_added'] or graph['introduced_edges'] != sql['edges_added']:
            raise ValueError('retirement_learning_audit_graph_count_mismatch')
    _check_time(deadline)
    return sql, graph


async def execute_candidate_learning_board(database_path, recovery_directory, *, board_id,
        relational_scope_factory, read_graph, require_live, max_seconds=180):
    """Execute every selected exact reference, retaining both sides for cold replay."""
    deadline = _deadline(max_seconds)
    database, root = _path(database_path), _path(recovery_directory)
    if (type(board_id) is not str or not board_id or len(board_id) > 256
            or require_live() is not True):
        raise ValueError('retirement_learning_execution_fence_lost')
    # This concrete edition factory must point at the same private SQL file
    # that is being retained. A caller cannot prove effects on another database.
    bound = relational_scope_factory.kw.get('bind')
    if bound is None or _path(Path(bound.url.database)) != database:
        raise ValueError('retirement_learning_execution_database_mismatch')
    root.mkdir(mode=0o700)
    initial = await _blocking(_capture, database, root, '000000-before', read_graph, require_live, deadline)
    before = await _blocking(_read_frame, root, initial, '000000-before', deadline)
    selection, work = _select(*before, board_id, deadline)
    if len(work) > 100_000 or len(set(work)) != len(work):
        raise ValueError('retirement_learning_execution_work_invalid')
    steps, current_frame = [], initial
    receipt_bytes = len(_encode(selection)) + len(_encode(initial)) + 1024
    for index, work_ref in enumerate(work):
        _check_time(deadline)
        if require_live() is not True:
            raise ValueError('retirement_learning_execution_fence_lost')
        if index:
            current_frame = await _blocking(_capture, database, root, f'{index:06d}-before', read_graph, require_live, deadline)
            current = await _blocking(_read_frame, root, current_frame, f'{index:06d}-before', deadline)
            if not _same_sql(before[0], current[0], deadline) or not _same_graph(before[1], current[1]):
                raise ValueError('retirement_learning_execution_between_steps_changed')
            before = current
        execution = await execute_learning_reconciliation(board_id=board_id, work_ref=work_ref,
            relational_scope_factory=relational_scope_factory)
        after_frame = await _blocking(_capture, database, root, f'{index:06d}-after', read_graph, require_live, deadline)
        after = await _blocking(_read_frame, root, after_frame, f'{index:06d}-after', deadline)
        sql, graph = await _proof(before, after, execution, deadline)
        step = {'execution': asdict(execution), 'before': current_frame, 'after': after_frame,
            'sql_delta': sql, 'graph_delta': graph}
        steps.append(step)
        before = after
        receipt_bytes += len(_encode(step)) + 1
        if receipt_bytes > 64 * 1024 * 1024:
            raise ValueError('retirement_learning_execution_receipt_limit')
    if require_live() is not True:
        raise ValueError('retirement_learning_execution_fence_lost')
    document = {'format': 'retirement-learning-execution/v1', 'board_id': board_id,
        'state': 'retained_not_reconciled', 'selection': selection, 'initial': initial, 'steps': steps}
    sealed = _seal(root / 'receipt', document)
    return {'directory': root, 'receipt_sha256': sealed.manifest_sha256}


async def verify_candidate_learning_board(recovery_directory, *, expected_receipt_sha256, max_seconds=180):
    """Cold, read-only rederivation; a sealed report never substitutes its inputs."""
    deadline, root = _deadline(max_seconds), _path(recovery_directory)
    document = _read_sealed(root / 'receipt', expected_receipt_sha256)
    if (set(document) != {'format', 'board_id', 'state', 'selection', 'initial', 'steps'}
            or document['format'] != 'retirement-learning-execution/v1'
            or document['state'] != 'retained_not_reconciled' or type(document['steps']) is not list
            or len(document['steps']) > 100_000):
        raise ValueError('retirement_learning_execution_receipt_invalid')
    initial = await _blocking(_read_frame, root, document['initial'], '000000-before', deadline)
    selection, work = _select(*initial, document['board_id'], deadline)
    if selection != document['selection'] or len(work) != len(document['steps']) or len(set(work)) != len(work):
        raise ValueError('retirement_learning_execution_selection_changed')
    previous, sessions = initial, set()
    for index, step in enumerate(document['steps']):
        if type(step) is not dict or set(step) != {'execution', 'before', 'after', 'sql_delta', 'graph_delta'}:
            raise ValueError('retirement_learning_execution_step_invalid')
        execution = LearningReconciliationExecution(**step['execution'])
        if (execution.board_id != document['board_id'] or execution.work_ref != work[index]
                or (execution.consolidation_session_id is not None and execution.consolidation_session_id in sessions)):
            raise ValueError('retirement_learning_execution_identity_changed')
        sessions.add(execution.consolidation_session_id)
        before = await _blocking(_read_frame, root, step['before'], f'{index:06d}-before', deadline)
        after = await _blocking(_read_frame, root, step['after'], f'{index:06d}-after', deadline)
        if not _same_sql(previous[0], before[0], deadline) or not _same_graph(previous[1], before[1]):
            raise ValueError('retirement_learning_execution_chain_changed')
        sql, graph = await _proof(before, after, execution, deadline)
        if sql != step['sql_delta'] or graph != step['graph_delta']:
            raise ValueError('retirement_learning_execution_proof_changed')
        previous = after
    return document


async def verify_candidate_learning_chain(recovery_directory, entries, *, board_ids,
        baseline_database, candidate_database, read_graph, max_seconds=180):
    """Bind retained per-Board proofs to the coordinator's exact phase boundaries.

    Both SQL paths must be quiescent snapshots under the caller's offline
    fences. ``read_graph(board_id)`` supplies the final native Board inventory,
    not a graph copied from the receipt. This does not grant completion.
    """
    deadline, root = _deadline(max_seconds), _path(recovery_directory)
    if (type(board_ids) is not tuple or any(type(item) is not str or not item for item in board_ids)
            or len(set(board_ids)) != len(board_ids) or type(entries) is not list
            or len(entries) != len(board_ids) or len(entries) > 100_000):
        raise ValueError('retirement_learning_chain_scope_invalid')
    previous = _path(baseline_database)
    candidate = _path(candidate_database)
    from .relational_recovery_snapshot import _sidecars_absent
    _sidecars_absent(previous)
    _sidecars_absent(candidate)
    documents, retained_bytes = [], 0
    for index, (board_id, entry) in enumerate(zip(board_ids, entries, strict=True)):
        if (type(entry) is not dict or set(entry) != {'board_id', 'directory', 'receipt_sha256'}
                or entry['board_id'] != board_id or entry['directory'] != f'{index:06d}'):
            raise ValueError('retirement_learning_chain_scope_invalid')
        directory = _path(root / entry['directory'])
        document = await verify_candidate_learning_board(directory,
            expected_receipt_sha256=entry['receipt_sha256'], max_seconds=_remaining(deadline))
        retained_bytes += len(_encode(document))
        if retained_bytes > 64 * 1024 * 1024:
            raise ValueError('retirement_learning_execution_receipt_limit')
        if document['board_id'] != board_id:
            raise ValueError('retirement_learning_chain_board_changed')
        initial = await _blocking(_read_frame, directory, document['initial'], '000000-before', deadline)
        if not _same_sql(previous, initial[0], deadline):
            raise ValueError('retirement_learning_chain_sql_boundary_changed')
        if document['steps']:
            final = await _blocking(_read_frame, directory, document['steps'][-1]['after'],
                f"{len(document['steps']) - 1:06d}-after", deadline)
        else:
            final = initial
        schema, nodes, relations = await _blocking(read_graph, board_id)
        if (schema.scope != 'board' or type(nodes) is not tuple or type(relations) is not tuple
                or len(nodes) > 100_000 or len(relations) > 500_000):
            raise ValueError('retirement_learning_graph_limit')
        fingerprint = LogicalFingerprintAccumulator.for_schema(schema)
        for node in nodes:
            _check_time(deadline)
            fingerprint.add_node(node)
        for relation in relations:
            _check_time(deadline)
            fingerprint.add_relation(relation)
        if (schema_digest(schema) != final[1].header.schema_digest
                or count_graph(nodes, relations) != final[1].manifest.counts
                or fingerprint.digest() != final[1].manifest.fingerprint):
            raise ValueError('retirement_learning_chain_final_graph_changed')
        previous = final[0]
        documents.append(document)
    if not _same_sql(previous, candidate, deadline):
        raise ValueError('retirement_learning_chain_final_sql_changed')
    _sidecars_absent(candidate)
    _check_time(deadline)
    return tuple(documents)
