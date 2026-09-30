"""Exact SQL effects of one governed Learning execution in a private candidate.

Inputs are quiescent snapshots retained inside the candidate's recovery fence.
This read-only proof complements, but never replaces, graph/evidence checks.
"""
from contextlib import closing
from datetime import datetime
import json
import sqlite3
from types import SimpleNamespace

from okto_pulse.core.ports.canonical_debt import CanonicalDebtRecord
from okto_pulse.core.ports.learning_reconciliation import (
    LearningReconciliationExecution, learning_reconciliation_source_basis, qualify_learning_reconciliation_debt_change,
)

from .global_discovery_recovery import CommunityRelationalRecoverySnapshotFingerprint
from .materialization_health import materialization_generation_key
from .relational_recovery_snapshot import _readonly, _sidecars_absent, _check_time
from .retirement_candidate_sql_delta import _changed_rows, _rows
from .retirement_learning_history import CandidateLearningHistory
from .sqlalchemy_kg_cognitive_source import _base_record, _revision_record


def _all_rows(connection, table, deadline):
    columns, rows = _rows(connection, table, deadline)
    return [dict(zip(columns, row, strict=True)) for row in rows.elements()]


def _source_row(row):
    row = dict(row)
    for key in ('payload', 'evidence_refs'):
        row[key] = json.loads(row[key])
    row['committed_at'] = datetime.fromisoformat(row['committed_at']) if row['committed_at'] else None
    return SimpleNamespace(**row)


def _history(connection, deadline):
    parents = {row['id']: _source_row(row) for row in _all_rows(connection, 'kg_cognitive_sources', deadline)}
    records = [_base_record(row) for row in parents.values()]
    for row in _all_rows(connection, 'kg_cognitive_source_revisions', deadline):
        if row['cognitive_source_id'] not in parents:
            raise ValueError('retirement_learning_sql_source_orphan')
        records.append(_revision_record(parents[row['cognitive_source_id']], _source_row(row)))
    return parents, tuple(records)


def _debt(row):
    row = dict(row)
    if set(row) != set(CanonicalDebtRecord.__dataclass_fields__):
        raise ValueError('retirement_learning_sql_debt_schema_changed')
    for key in ('created_at', 'updated_at', 'next_retry_at', 'last_attempt_at'):
        row[key] = datetime.fromisoformat(row[key]) if row[key] is not None else None
    return CanonicalDebtRecord(**row)


def _single_insert(changes, table):
    added, removed = changes.get(table, ([], []))
    if len(added) != 1 or removed:
        raise ValueError('retirement_learning_sql_insert_unowned:' + table)
    return added[0]


async def verify_learning_sql_delta(baseline_path, candidate_path, *, execution, deadline):
    """Prove whole-table preservation and classify only this execution's effects."""
    _check_time(deadline)
    if type(execution) is not LearningReconciliationExecution or type(execution.materialized) is not bool:
        raise ValueError('retirement_learning_sql_execution_invalid')
    _sidecars_absent(baseline_path)
    _sidecars_absent(candidate_path)
    with closing(_readonly(baseline_path, immutable=True)) as before, closing(_readonly(candidate_path, immutable=True)) as after:
        before.execute('BEGIN')
        after.execute('BEGIN')
        # A new index or altered non-source trigger does not change table bags
        # or the source-fence counter. No DDL belongs to this execution phase.
        schema_query = 'SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name'
        if before.execute(schema_query).fetchall() != after.execute(schema_query).fetchall():
            raise ValueError('retirement_learning_sql_schema_changed')
        changes = _changed_rows(before, after, deadline)
        if execution.consolidation_session_id is None:
            if execution.materialized or changes:
                raise ValueError('retirement_learning_sql_unacknowledged_effects')
            return {'format': 'retirement-learning-sql-delta/v1', 'board_id': execution.board_id,
                'work_ref': execution.work_ref, 'session_id': None, 'changed_tables': [],
                'source_revision_delta': 0, 'node_ref_count': 0, 'source_append_count': 0,
                'technical_debt_count': 0, 'nodes_added': 0, 'nodes_updated': 0, 'nodes_superseded': 0, 'edges_added': 0}
        allowed = {'consolidation_audit', 'kuzu_node_refs', 'global_update_outbox', 'domain_events',
            'app_settings', 'global_discovery_source_revision', 'kg_cognitive_source_revisions', 'canonical_debt'}
        if set(changes) - allowed:
            raise ValueError('retirement_learning_sql_unclassified:' + ','.join(sorted(set(changes) - allowed)))
        parents, initial_records = _history(before, deadline)
        initial = CandidateLearningHistory(initial_records)
        basis = await learning_reconciliation_source_basis(None, initial, execution=execution)
        audit = _single_insert(changes, 'consolidation_audit')
        session, board = execution.consolidation_session_id, execution.board_id
        if (audit['session_id'] != session or audit['board_id'] != board
                or audit['agent_id'] != 'cognitive_closeout_worker' or audit['artifact_type'] != 'bug'
                or audit['artifact_id'] != basis.bug_id or audit['content_hash'] != basis.audit_content_hash
                or audit['undo_status'] != 'none' or audit['undone_at'] is not None):
            raise ValueError('retirement_learning_sql_audit_unowned')
        counts = {key: audit[key] for key in ('nodes_added', 'nodes_updated', 'nodes_superseded', 'edges_added')}
        if (any(type(value) is not int or value < 0 for value in counts.values())
                or counts['nodes_added'] > 1 or counts['nodes_updated'] != 0
                or counts['nodes_superseded'] != 0 or counts['edges_added'] > 1):
            raise ValueError('retirement_learning_sql_audit_shape_invalid')
        refs, removed = changes.get('kuzu_node_refs', ([], []))
        if (removed or len(refs) != counts['nodes_added']
                or any(row['session_id'] != session or row['board_id'] != board for row in refs)):
            raise ValueError('retirement_learning_sql_refs_unowned')
        expected_refs = ([('add', basis.learning_id)] if counts['nodes_added'] else [])
        if sorted((row['operation'], row['kuzu_node_id']) for row in refs) != sorted(expected_refs):
            raise ValueError('retirement_learning_sql_refs_unowned')
        outbox = _single_insert(changes, 'global_update_outbox')
        if (outbox['board_id'] != board or outbox['session_id'] != session
                or outbox['event_type'] != 'consolidation_committed' or outbox['processed_at'] is not None
                or outbox['retry_count'] != 0 or outbox['last_error'] is not None
                or json.loads(outbox['payload']) != {'artifact_id': basis.bug_id, 'session_id': session, **counts}):
            raise ValueError('retirement_learning_sql_outbox_unowned')
        event = _single_insert(changes, 'domain_events')
        heads, removed_heads = changes.get('app_settings', ([], []))
        key = materialization_generation_key(board)
        if (len(heads) != 1 or heads[0]['key'] != key or len(removed_heads) > 1
                or any(row['key'] != key for row in removed_heads)):
            raise ValueError('retirement_learning_sql_head_unowned')
        previous = removed_heads[0]['value'] if removed_heads else 'unmaterialized-v1'
        if (event['board_id'] != board or event['event_type'] != 'kg.materialization_generation_advanced'
                or json.loads(event['payload_json']) != {'correlation_id': session,
                    'previous_materialization_generation': previous, 'materialization_generation': heads[0]['value']}):
            raise ValueError('retirement_learning_sql_event_unowned')
        revisions, removed = changes.get('kg_cognitive_source_revisions', ([], []))
        if removed or any(row['cognitive_source_id'] not in parents for row in revisions):
            raise ValueError('retirement_learning_sql_source_unowned')
        appended = tuple(_revision_record(parents[row['cognitive_source_id']], _source_row(row)) for row in revisions)
        if appended:
            await initial.verify_append(appended=appended, execution=execution)
        added_debt, removed_debt = changes.get('canonical_debt', ([], []))
        old_debt = {row['id']: row for row in removed_debt}
        if len(added_debt) != len(old_debt) or {row['id'] for row in added_debt} != set(old_debt):
            raise ValueError('retirement_learning_sql_debt_unowned')
        for row in added_debt:
            if not qualify_learning_reconciliation_debt_change(before=_debt(old_debt[row['id']]),
                    after=_debt(row), execution=execution):
                raise ValueError('retirement_learning_sql_debt_unowned')
        for connection in (before, after):
            connection.row_factory = sqlite3.Row
        old_fence = CommunityRelationalRecoverySnapshotFingerprint.read_fence_from_connection(before)
        new_fence = CommunityRelationalRecoverySnapshotFingerprint.read_fence_from_connection(after)
        # Existing triggers count audit, outbox and generation-head once each,
        # plus each node-ref, source revision and technical-debt CAS.
        expected_delta = 3 + len(refs) + len(revisions) + len(added_debt)
        if (any(getattr(old_fence, field) != getattr(new_fence, field) for field in (
                'scope_id', 'fence_version', 'trigger_manifest_version', 'incarnation_id'))
                or new_fence.revision - old_fence.revision != expected_delta
                or new_fence.mutation_nonce == old_fence.mutation_nonce):
            raise ValueError('retirement_learning_sql_fence_unowned')
        _check_time(deadline)
        _sidecars_absent(baseline_path)
        _sidecars_absent(candidate_path)
        return {'format': 'retirement-learning-sql-delta/v1', 'board_id': board,
            'work_ref': execution.work_ref, 'session_id': session, 'changed_tables': sorted(changes),
            'source_revision_delta': expected_delta, 'node_ref_count': len(refs),
            'source_append_count': len(revisions), 'technical_debt_count': len(added_debt), **counts}
