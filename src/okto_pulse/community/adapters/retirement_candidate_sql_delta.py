"""Read-only, receipt-bound SQL delta audit for a private retirement candidate.

This proves row ownership and detects unclassified changes. It also reconciles
the source-revision increments produced by the exact rebuild protocol; this
module never authorizes runtime admission.
"""

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import json
import re
import sqlite3
from types import SimpleNamespace

from okto_pulse.core.ports.consolidation import ExactConsolidationAckReceipt
from okto_pulse.core.ports.projection_effects import ProjectionPropertyEffects, validated_projection_effect_extension

from .global_discovery_recovery import CommunityRelationalRecoverySnapshotFingerprint
from .materialization_health import materialization_generation_key
from .relational_recovery_snapshot import _readonly, _sidecars_absent, _check_time
from .sqlalchemy_consolidation import _canonical_node_refs_sha256

_MAX_ROWS = 100_000
_MAX_BYTES = 64 * 1024 * 1024
_INSERT_TABLES = {
    'consolidation_audit': ('session_id', 'consolidation_session_id'),
    'domain_events': ('id', 'generation_event_id'),
    'exact_rebuild_consolidation_ack_journal': ('queue_id', 'queue_id'),
    'global_update_outbox': ('event_id', 'outbox_event_id'),
}
# One successful exact ACK owns four queue mutations: enqueue/adopt, claim,
# the serialized unfenced-claim CAS, and ACK delete/live-intent restore. It
# also owns one audit insert, one outbox insert and one generation-head write.
# Each created graph node adds one separately counted node-ref mutation.
_SOURCE_REVISION_MUTATIONS_PER_ACK = 7


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _tables(connection):
    return tuple(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"))


def _rows(connection, table, deadline):
    columns = tuple(row[1] for row in connection.execute(f'PRAGMA table_xinfo({_quote(table)})'))
    if not columns:
        raise ValueError('retirement_candidate_sql_delta_schema_invalid')
    rows, total, count = Counter(), 0, 0
    for row in connection.execute(f'SELECT * FROM {_quote(table)}'):
        _check_time(deadline)
        values = tuple(row)
        count += 1
        total += sum(len(value) if type(value) is bytes else len(str(value)) for value in values if value is not None)
        if count > _MAX_ROWS or total > _MAX_BYTES:
            raise ValueError('retirement_candidate_sql_delta_limit')
        rows[values] += 1
    return columns, rows


def _changed_rows(before, after, deadline):
    if _tables(before) != _tables(after):
        raise ValueError('retirement_candidate_sql_delta_schema_changed')
    changes = {}
    for table in _tables(before):
        _check_time(deadline)
        old_columns, old_rows = _rows(before, table, deadline)
        new_columns, new_rows = _rows(after, table, deadline)
        if old_columns != new_columns:
            raise ValueError('retirement_candidate_sql_delta_schema_changed')
        if old_rows != new_rows:
            added = [dict(zip(old_columns, row, strict=True)) for row in (new_rows - old_rows).elements()]
            removed = [dict(zip(old_columns, row, strict=True)) for row in (old_rows - new_rows).elements()]
            changes[table] = added, removed
    return changes


def _inserted(changes, table, field, receipts, receipt_field):
    added, removed = changes.get(table, ([], []))
    expected = {getattr(ack, receipt_field) for ack in receipts}
    if (removed or len(added) != len(receipts)
            or {row[field] for row in added} != expected):
        raise ValueError(f'retirement_candidate_sql_delta_{table}_unowned')
    return {row[field]: row for row in added}


def verify_candidate_sql_delta(baseline_path, candidate_path, receipts, *, deadline, effects_out=None):
    """Check complete table bags and classify only rows owned by exact ACKs.

    This certifies only receipt-owned relational projection effects. Callers
    must keep the candidate incomplete until graph/source reconciliation.
    """
    if (type(receipts) is not tuple or any(type(ack) is not ExactConsolidationAckReceipt
            for ack in receipts) or len({ack.receipt_sha256 for ack in receipts}) != len(receipts)):
        raise ValueError('retirement_candidate_sql_delta_receipts_invalid')
    # These files are retained, quiescent snapshots. Immutable reads avoid
    # manufacturing transient WAL/SHM files that would poison the byte seal.
    # Reject sidecars first: immutable SQLite would otherwise ignore live WAL.
    _sidecars_absent(baseline_path)
    _sidecars_absent(candidate_path)
    with closing(_readonly(baseline_path, immutable=True)) as before, closing(_readonly(candidate_path, immutable=True)) as after:
        before.execute('BEGIN')
        after.execute('BEGIN')
        changes = _changed_rows(before, after, deadline)
        allowed = set(_INSERT_TABLES) | {'app_settings', 'kuzu_node_refs', 'global_discovery_source_revision'}
        unexpected = set(changes) - allowed
        if unexpected:
            raise ValueError('retirement_candidate_sql_delta_unclassified:' + ','.join(sorted(unexpected)))
        rows = {table: _inserted(changes, table, field, receipts, receipt_field)
            for table, (field, receipt_field) in _INSERT_TABLES.items()}
        audit = rows['consolidation_audit']
        events = rows['domain_events']
        journal = rows['exact_rebuild_consolidation_ack_journal']
        outbox = rows['global_update_outbox']
        for ack in receipts:
            logged = journal[ack.queue_id]
            if any(logged.get(field) != getattr(ack, field) for field in ack.__dataclass_fields__):
                raise ValueError('retirement_candidate_sql_delta_ack_changed')
            audited = audit[ack.consolidation_session_id]
            if (audited['board_id'] != ack.board_id or audited['artifact_type'] != ack.artifact_type
                    or audited['artifact_id'] != ack.artifact_id
                    or audited['content_hash'] != ack.audit_content_hash
                    or audited['undo_status'] != 'none' or audited['undone_at'] is not None):
                raise ValueError('retirement_candidate_sql_delta_audit_changed')
            event = events[ack.generation_event_id]
            payload = json.loads(event['payload_json'])
            if (event['board_id'] != ack.board_id
                    or event['event_type'] != 'kg.materialization_generation_advanced'
                    or payload != {'correlation_id': ack.consolidation_session_id,
                        'previous_materialization_generation': ack.previous_materialization_generation,
                        'materialization_generation': ack.materialization_generation}):
                raise ValueError('retirement_candidate_sql_delta_event_changed')
            queued = outbox[ack.outbox_event_id]
            observed_outbox = json.loads(queued['payload'])
            effects = validated_projection_effect_extension(observed_outbox,
                board_id=ack.board_id, session_id=ack.consolidation_session_id)
            if effects and audited['agent_id'] != 'system:historical_consolidation':
                raise ValueError('retirement_candidate_sql_delta_outbox_changed')
            if (queued['board_id'] != ack.board_id or queued['session_id'] != ack.consolidation_session_id
                    or queued['event_type'] != 'consolidation_committed'
                    or queued['processed_at'] is not None or queued['retry_count'] != 0
                    or queued['last_error'] is not None
                    or observed_outbox != {
                        'artifact_id': ack.artifact_id, 'session_id': ack.consolidation_session_id,
                        'nodes_added': audited['nodes_added'], 'nodes_updated': audited['nodes_updated'],
                        'nodes_superseded': audited['nodes_superseded'], 'edges_added': audited['edges_added'], **effects}):
                raise ValueError('retirement_candidate_sql_delta_outbox_changed')
        refs, removed = changes.get('kuzu_node_refs', ([], []))
        if removed or len(refs) != sum(ack.node_ref_count for ack in receipts):
            raise ValueError('retirement_candidate_sql_delta_refs_changed')
        by_session = defaultdict(list)
        for ref in refs:
            by_session[ref['session_id']].append(ref)
        if set(by_session) - set(audit):
            raise ValueError('retirement_candidate_sql_delta_refs_unowned')
        for ack in receipts:
            owned = by_session[ack.consolidation_session_id]
            if (len(owned) != ack.node_ref_count
                    or any(ref['board_id'] != ack.board_id or ref['operation'] != 'add' for ref in owned)):
                raise ValueError('retirement_candidate_sql_delta_refs_changed')
            audited = dict(audit[ack.consolidation_session_id])
            for field in ('started_at', 'committed_at'):
                audited[field] = datetime.fromisoformat(audited[field])
            digest = _canonical_node_refs_sha256(audit=SimpleNamespace(**audited),
                refs=[SimpleNamespace(**ref) for ref in owned],
                projection_effects_sha256=json.loads(outbox[ack.outbox_event_id]['payload'])
                    .get('projection_property_effects', {}).get('sha256'))
            if digest != ack.node_refs_sha256:
                raise ValueError('retirement_candidate_sql_delta_refs_hash_changed')
        settings_added, settings_removed = changes.get('app_settings', ([], []))
        first, last = {}, {}
        for ack in receipts:
            if (ack.board_id in last and ack.previous_materialization_generation
                    != last[ack.board_id].materialization_generation):
                raise ValueError('retirement_candidate_sql_delta_ack_order_changed')
            first.setdefault(ack.board_id, ack)
            last[ack.board_id] = ack
        keys = {materialization_generation_key(board_id): board_id for board_id in first}
        if ({row['key'] for row in settings_added} != set(keys)
                or {row['key'] for row in settings_removed} - set(keys)):
            raise ValueError('retirement_candidate_sql_delta_generation_head_unowned')
        previous = {row['key']: row['value'] for row in settings_removed}
        for row in settings_added:
            board_id = keys[row['key']]
            if (row['value'] != last[board_id].materialization_generation
                    or (row['key'] in previous
                        and previous[row['key']] != first[board_id].previous_materialization_generation)):
                raise ValueError('retirement_candidate_sql_delta_generation_head_changed')
        revision_added, revision_removed = changes.get('global_discovery_source_revision', ([], []))
        if receipts:
            if len(revision_added) != 1 or len(revision_removed) != 1:
                raise ValueError('retirement_candidate_sql_delta_revision_unclassified')
            old, new = revision_removed[0], revision_added[0]
            with closing(_readonly(baseline_path, immutable=True)) as source_revision, closing(_readonly(candidate_path, immutable=True)) as final_revision:
                source_revision.execute('BEGIN')
                final_revision.execute('BEGIN')
                for connection, expected in ((source_revision, old), (final_revision, new)):
                    connection.row_factory = sqlite3.Row
                    fence = CommunityRelationalRecoverySnapshotFingerprint.read_fence_from_connection(connection)
                    if any(getattr(fence, key) != expected[key] for key in (
                            'scope_id', 'fence_version', 'trigger_manifest_version', 'incarnation_id',
                            'revision', 'mutation_nonce')):
                        raise ValueError('retirement_candidate_sql_delta_revision_invalid')
            if (any(old[key] != new[key] for key in ('scope_id', 'fence_version',
                    'trigger_manifest_version', 'incarnation_id'))
                    or type(old['revision']) is not int or type(new['revision']) is not int
                    or new['revision'] <= old['revision']
                    or type(new['mutation_nonce']) is not str
                    or re.fullmatch(r'[0-9a-f]{64}', new['mutation_nonce']) is None
                    or new['mutation_nonce'] == old['mutation_nonce']):
                raise ValueError('retirement_candidate_sql_delta_revision_invalid')
            revision_delta = new['revision'] - old['revision']
            expected_revision_delta = (
                _SOURCE_REVISION_MUTATIONS_PER_ACK * len(receipts) + len(refs)
            )
            if revision_delta != expected_revision_delta:
                raise ValueError(
                    'retirement_candidate_sql_delta_revision_delta_unowned'
                )
        else:
            if revision_added or revision_removed:
                raise ValueError('retirement_candidate_sql_delta_revision_unowned')
            revision_delta = 0
            expected_revision_delta = 0
        _sidecars_absent(baseline_path)
        _sidecars_absent(candidate_path)
        if effects_out is not None:
            # Export only after every audit/ref/ACK and complete SQL delta passes.
            # Order is the sealed execution order, checked by generation chaining.
            effects_out.extend(ProjectionPropertyEffects.from_payload(value)
                for ack in receipts
                if (value := json.loads(outbox[ack.outbox_event_id]['payload'])
                    .get('projection_property_effects')) is not None)
        return {'format': 'retirement-candidate-sql-delta/v1',
            'state': 'receipt_owned_projection_effects' if receipts else 'no_projection_effects',
            'ack_count': len(receipts), 'node_ref_count': len(refs),
            'source_revision_delta': revision_delta,
            'source_revision_expected_delta': expected_revision_delta,
            'changed_tables': sorted(changes)}
