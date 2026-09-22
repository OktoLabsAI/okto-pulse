"""Read-only source/graph reconciliation for a private retirement candidate."""

from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from okto_grafx import connect
from okto_pulse.core.kg.schema_contract import NODE_TYPES
from okto_pulse.core.kg.logical_transfer import LOGICAL_NULL, LogicalFingerprintAccumulator
from okto_pulse.core.ports.consolidation import ExactConsolidationAckReceipt
from okto_pulse.core.ports.cognitive_projection import compare_cognitive_projection, validate_cognitive_projection_sources
from okto_pulse.core.ports.projection_connectivity import observe_projection_connectivity
from okto_pulse.core.ports.projection_relations import compare_projection_relations
from okto_pulse.core.ports.projection_qualification import (
    SOURCE_OBSERVATION_FIELDS, ProjectionSourceObservation, qualify_projection_history,
)
from okto_pulse.core.ports.projection_history import (
    ProjectionSourceIdentity, ProjectionSourceRoot, select_projection_source_roots, is_projection_technical_root,
    ProjectionHistoryDelta, ProjectionNodeFingerprint, ProjectionNodeChange, ProjectionEdgeFingerprint,
)

from .logical_transfer_factories import make_grafx_logical_source
from .graph_backend_binding import CommunityGraphBackendBindingStore
from .relational_recovery_snapshot import _check_time, _readonly, _sidecars_absent

_MAX_NODES = 100_000
_MAX_EDGES = 500_000
_SOURCE_FIELDS = SOURCE_OBSERVATION_FIELDS[:5]
_DATE_FIELDS = frozenset({'source_created_at', 'source_updated_at', 'resolved_at'})
_PARTITION_FIELDS = SOURCE_OBSERVATION_FIELDS[5:]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':')).encode('ascii')).hexdigest()


def _relational_evidence(database_path, receipts):
    sessions = {ack.consolidation_session_id: ack for ack in receipts}
    refs, edge_counts = {}, {}
    with closing(_readonly(database_path, immutable=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        for row in connection.execute('SELECT board_id, session_id, kuzu_node_type, '
                'kuzu_node_id, operation FROM kuzu_node_refs'):
            session_id = str(row['session_id'])
            if session_id not in sessions:
                continue
            identity = str(row['kuzu_node_type']), str(row['kuzu_node_id'])
            if (identity in refs or row['board_id'] != sessions[session_id].board_id
                    or row['operation'] != 'add'):
                raise ValueError('retirement_candidate_graph_node_ref_invalid')
            refs[identity] = session_id
            if len(refs) > _MAX_NODES:
                raise ValueError('retirement_candidate_graph_limit')
        for row in connection.execute('SELECT session_id, edges_added FROM consolidation_audit'):
            session_id = str(row['session_id'])
            if session_id in sessions:
                if session_id in edge_counts:
                    raise ValueError('retirement_candidate_graph_audit_duplicate')
                if type(row['edges_added']) is not int or row['edges_added'] < 0:
                    raise ValueError('retirement_candidate_graph_audit_invalid')
                edge_counts[session_id] = row['edges_added']
        connection.execute('ROLLBACK')
    if len(refs) != sum(ack.node_ref_count for ack in receipts):
        raise ValueError('retirement_candidate_graph_node_ref_count_changed')
    if set(edge_counts) != set(sessions):
        raise ValueError('retirement_candidate_graph_audit_missing')
    return refs, edge_counts


def _source_expectations(board_plan):
    expected = {}
    for plan in board_plan['plans']:
        projection = plan['projection']
        if projection is None:
            continue
        metadata = projection.get('source_metadata')
        if type(metadata) is not dict:
            raise ValueError('retirement_candidate_source_metadata_missing')
        root_ref = plan['source']['artifact_type'] + ':' + plan['source']['artifact_id']
        root_nodes = [node for node in projection['nodes'] if node['source_artifact_ref'] == root_ref]
        if set(metadata) != ({root_ref} if root_nodes else set()):
            raise ValueError('retirement_candidate_source_metadata_scope')
        for ref, values in metadata.items():
            if (type(values) is not dict or set(values) != set(_SOURCE_FIELDS)
                    or any(value is not None and type(value) is not str for value in values.values())):
                raise ValueError('retirement_candidate_source_metadata_invalid')
            for node in root_nodes:
                root = ProjectionSourceRoot(node['node_type'], ref)
                if root in expected:
                    raise ValueError('retirement_candidate_source_metadata_invalid')
                expected[root] = values
    return expected


def _source_value(name, value):
    if value is None or name not in _DATE_FIELDS:
        return value
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError('retirement_candidate_source_metadata_naive')
    delta = stamp.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _partition_expectations(board_plan):
    # The Core planner has already classified the fenced source snapshot. Do
    # not infer canonical eligibility from a graph status or from its age here.
    expected = {}
    for plan in board_plan['plans']:
        projection = plan['projection']
        if projection is None:
            continue
        for node in projection['nodes']:
            root = ProjectionSourceRoot(node['node_type'], node['source_artifact_ref'])
            partition = tuple(node.get(name) for name in _PARTITION_FIELDS)
            if any(type(item) is not str or not item for item in partition):
                raise ValueError('retirement_candidate_source_partition_invalid')
            if root in expected and expected[root] != partition:
                raise ValueError('retirement_candidate_source_partition_conflict')
            expected[root] = partition
    return expected


def _edge_identity(row):
    return (row['edge_type'], row['source_type'], row['source_id'],
        row['target_type'], row['target_id'], row['fingerprint'])


def _history_delta(payload):
    return ProjectionHistoryDelta(
        **{name: tuple(ProjectionNodeFingerprint(**row) for row in payload.get(name, ()))
            for name in ('unchanged_nodes', 'introduced_nodes', 'removed_nodes')},
        changed_nodes=tuple(ProjectionNodeChange(ProjectionNodeFingerprint(**row['before']),
            ProjectionNodeFingerprint(**row['after'])) for row in payload.get('changed_nodes', ())),
        **{name: tuple(ProjectionEdgeFingerprint(**row) for row in payload.get(name, ()))
            for name in ('retained_edges', 'introduced_edges', 'removed_edges')})


def _cognitive_parity(schema, board_id, record, node):
    value = asdict(compare_cognitive_projection(schema=schema, board_id=board_id, record=record, node=node))
    return {**value, 'differing_fields': list(value['differing_fields']),
        'usage_differences': list(value['usage_differences'])}


def _board_graph(binding, expected_refs, expected_edge_count, expected_metadata, deadline, history=None,
        *, board_id=None, cognitive_rows=(), expected_partitions=None, expected_edge_sessions=None, planned_document=None):
    # Prior records qualify by a full fingerprint, preserved or independently
    # reconciled against the authenticated property trace. Both stay unclassified.
    delta = history['delta'] if history is not None else {}
    prior_nodes = {(row['node_type'], row['node_id']): row['fingerprint']
        for row in delta.get('unchanged_nodes', ())}
    unchanged_nodes = set(prior_nodes)
    prior_nodes.update({(row['after']['node_type'], row['after']['node_id']): row['after']['fingerprint']
        for row in delta.get('changed_nodes', ())})
    prior_edges = Counter({_edge_identity(row): row['count'] for row in delta.get('retained_edges', ())})
    nodes, metadata, identities, hashes, partitions = {}, {}, [], {}, {}
    edges, new_edges, connected, technical_roots = [], [], set(), set()
    edge_sessions = Counter()
    roots = tuple(sorted(expected_metadata))
    partition_roots = tuple(sorted(expected_partitions or {}))
    wanted_roots = set(roots) | set(partition_roots)
    wanted_source_keys = {(root.node_type, root.source_artifact_ref) for root in wanted_roots}
    historical_inventory_nodes, historical_inventory_edges = [], []
    if type(cognitive_rows) is not tuple or len(cognitive_rows) > _MAX_NODES:
        raise ValueError('retirement_candidate_cognitive_source_limit')
    cognitive_sources, cognitive_matches, cognitive_seen = {}, [], set()

    def value(properties, name):
        item = properties.get(name)
        return None if item is LOGICAL_NULL else item

    with connect(binding.physical_path, page_size=binding.page_size, read_only=True) as graph:
        reader = make_grafx_logical_source(graph, scope='board').open_snapshot()
        try:
            for record in validate_cognitive_projection_sources(
                    schema=reader.schema(), board_id=board_id, records=cognitive_rows):
                cognitive_sources.setdefault((record['node_type'], record['node_id']), []).append(record)
            schema = LogicalFingerprintAccumulator.for_schema(reader.schema()).schema_hex
            counts = reader.counts()
            if counts.nodes > _MAX_NODES or counts.relations > _MAX_EDGES:
                raise ValueError('retirement_candidate_graph_limit')
            for batch in reader.iter_nodes(batch_size=500):
                _check_time(deadline)
                for node in batch:
                    if prior_nodes or planned_document is not None:
                        historical_inventory_nodes.append(node)
                    if node.type_name not in NODE_TYPES:
                        continue  # BoardMeta is authenticated by the complete cold census.
                    identity = node.type_name, node.key
                    for record in cognitive_sources.get(identity, ()):
                        cognitive_matches.append(_cognitive_parity(reader.schema(), board_id, record, node))
                    cognitive_seen.add(identity)
                    single = LogicalFingerprintAccumulator(schema)
                    single.add_node(node)
                    hashes[identity] = single.digest()
                    preserved = identity in prior_nodes
                    if preserved and hashes[identity] != prior_nodes[identity]:
                        raise ValueError('retirement_candidate_prior_node_changed')
                    fields = tuple(value(node.properties, name) for name in
                        ('source_artifact_ref', 'source_session_id', 'created_by_agent'))
                    if identity in nodes or (not preserved and (
                            any(type(field) is not str or not field for field in fields)
                            or fields[0].startswith('sprint:'))):
                        raise ValueError('retirement_candidate_graph_node_invalid')
                    nodes[identity] = fields
                    if is_projection_technical_root(node_type=node.type_name, source_artifact_ref=fields[0],
                            created_by_agent=fields[2], source_session_id=fields[1]):
                        technical_roots.add(identity)
                    metadata[identity] = tuple(value(node.properties, name) for name in _SOURCE_FIELDS)
                    partitions[identity] = tuple(value(node.properties, name) for name in _PARTITION_FIELDS)
                    root = (ProjectionSourceRoot(node.type_name, fields[0])
                        if type(fields[0]) is str and fields[0] and
                        (not preserved or (node.type_name, fields[0]) in wanted_source_keys) else None)
                    if not preserved or root in wanted_roots:
                        identities.append(ProjectionSourceIdentity(node.type_name, node.key, fields[0],
                            value(node.properties, 'generation'), value(node.properties, 'superseded_by')))
            selected = select_projection_source_roots(roots=roots, nodes=tuple(identities))
            partition_selected = select_projection_source_roots(roots=partition_roots, nodes=tuple(identities))
            for root, node in zip(partition_roots, partition_selected, strict=True):
                if partitions[(node.node_type, node.node_id)] != expected_partitions[root]:
                    raise ValueError('retirement_candidate_source_partition_changed')
            expected_by_identity = {(node.node_type, node.node_id): expected_metadata[root]
                for root, node in zip(roots, selected, strict=True)}
            for identity, values in metadata.items():
                if identity in prior_nodes and identity not in expected_by_identity:
                    continue  # No current-source chronology is assigned to preserved history.
                expected = expected_by_identity.get(identity, {})
                for name, actual in zip(_SOURCE_FIELDS, values, strict=True):
                    wanted = _source_value(name, expected.get(name))
                    observed = (actual.micros if actual is not None and name in _DATE_FIELDS else actual)
                    if observed != wanted:
                        raise ValueError('retirement_candidate_source_metadata_changed:' + name)
            preserved_nodes = {(kind, key) for kind, key in prior_nodes if kind in NODE_TYPES}
            if (set(nodes) != set(expected_refs) | preserved_nodes
                    or set(expected_refs) & preserved_nodes):
                raise ValueError('retirement_candidate_graph_node_census_changed')
            if any(nodes[identity][1] != session_id for identity, session_id in expected_refs.items()):
                raise ValueError('retirement_candidate_graph_node_session_changed')
            for batch in reader.iter_relations(batch_size=500):
                _check_time(deadline)
                for relation in batch:
                    if prior_nodes or planned_document is not None:
                        historical_inventory_edges.append(relation)
                    source = relation.source_type, relation.source_key
                    target = relation.target_type, relation.target_key
                    if source not in nodes or target not in nodes:
                        raise ValueError('retirement_candidate_graph_edge_invalid')
                    single = LogicalFingerprintAccumulator(schema)
                    single.add_relation(relation)
                    key = (relation.layout_name, *source, *target, single.digest())
                    edges.append(key)
                    connected.update((source, target))
                    if prior_edges[key]:
                        prior_edges[key] -= 1
                        continue
                    rule = value(relation.properties, 'rule_id')
                    layer = value(relation.properties, 'layer')
                    actor = value(relation.properties, 'created_by')
                    if type(rule) is not str or not rule or layer != 'deterministic' or type(actor) is not str or not actor:
                        raise ValueError('retirement_candidate_graph_edge_invalid')
                    if expected_edge_sessions is not None:
                        session_id = value(relation.properties, 'created_by_session_id')
                        if type(session_id) is not str or session_id not in expected_edge_sessions:
                            raise ValueError('retirement_candidate_graph_edge_session_changed')
                        edge_sessions[session_id] += 1
                    new_edges.append((relation.layout_name, *source, *target, rule, layer, actor))
            if any(prior_edges.values()):
                raise ValueError('retirement_candidate_prior_edge_changed')
            if len(new_edges) != expected_edge_count or len(new_edges) != len(set(new_edges)):
                raise ValueError('retirement_candidate_graph_edge_census_changed')
            if expected_edge_sessions is not None and edge_sessions != Counter(expected_edge_sessions):
                raise ValueError('retirement_candidate_graph_edge_session_count_changed')
            orphans = set(nodes) - connected - technical_roots
            current_nodes = set(expected_by_identity) | {(node.node_type, node.node_id) for node in partition_selected}
            if orphans - unchanged_nodes or orphans & current_nodes:
                raise ValueError('retirement_candidate_graph_orphan_detected')
            for identity in sorted(set(cognitive_sources) - cognitive_seen):
                for record in cognitive_sources[identity]:
                    cognitive_matches.append(_cognitive_parity(reader.schema(), board_id, record, None))
            connectivity = observe_projection_connectivity(schema=reader.schema(), board_id=board_id,
                nodes=tuple(historical_inventory_nodes), relations=tuple(historical_inventory_edges),
                selected=tuple(sorted(preserved_nodes))) if preserved_nodes else ()
            relation_comparison = (compare_projection_relations(document=planned_document, schema=reader.schema(),
                nodes=tuple(historical_inventory_nodes), relations=tuple(historical_inventory_edges),
                new_sessions=tuple(sorted(expected_edge_sessions or {}))) if planned_document is not None else None)
            qualification = None
            if relation_comparison is not None and expected_partitions is not None and expected_edge_sessions is not None:
                source_observations = []
                for root, node in zip(partition_roots, partition_selected, strict=True):
                    identity = node.node_type, node.node_id
                    expected_values = expected_metadata.get(root, {})
                    expected_fields = tuple(_source_value(name, expected_values.get(name)) for name in _SOURCE_FIELDS)
                    observed_fields = tuple(item.micros if item is not None and name in _DATE_FIELDS else item
                        for name, item in zip(_SOURCE_FIELDS, metadata[identity], strict=True))
                    source_observations.append(ProjectionSourceObservation(node,
                        expected_fields + expected_partitions[root], observed_fields + partitions[identity]))
                qualification = qualify_projection_history(history=_history_delta(delta),
                    sources=tuple(source_observations), relations=relation_comparison)
        finally:
            reader.close()
    return {'node_count': len(nodes), 'edge_count': len(edges),
        'edge_session_validation': 'passed' if expected_edge_sessions is not None else 'not_checked',
        'edge_session_count': len(expected_edge_sessions or {}),
        'edge_session_sha256': _digest(sorted((expected_edge_sessions or {}).items())),
        'source_relation_comparison': ({**asdict(relation_comparison), 'issues': list(relation_comparison.issues)}
            if relation_comparison is not None else None),
        'node_sha256': _digest(sorted((kind, key, digest) for (kind, key), digest in hashes.items())),
        'edge_sha256': _digest(sorted(edges)),
        'source_metadata_sha256': _digest([(root.node_type, root.source_artifact_ref, expected_metadata[root])
            for root in roots]),
        'source_metadata_root_count': len(expected_metadata), 'source_metadata_validation': 'passed',
        'source_partition_count': len(partition_roots),
        'source_partition_validation': 'passed' if expected_partitions is not None else 'not_checked',
        'source_partition_sha256': _digest([(root.node_type, root.source_artifact_ref, expected_partitions[root])
            for root in partition_roots]),
        'historical_node_count': len(preserved_nodes),
        'historical_property_change_count': len(delta.get('changed_nodes', ())),
        'historical_edge_count': sum(row['count'] for row in delta.get('retained_edges', ())),
        'historical_orphan_count': len(orphans),
        'allowlisted_technical_root_count': len(technical_roots - connected),
        'historical_connectivity': [{**asdict(item), 'reasons': list(item.reasons),
            'advisories': list(item.advisories)} for item in connectivity],
        'cognitive_source_parity': sorted(cognitive_matches,
            key=lambda row: (row['node_type'], row['node_id'], row['generation'], row['source_revision'])),
        'history_qualification': ({**asdict(qualification), 'reasons': list(qualification.reasons)}
            if qualification is not None else None),
        'history_classification': (qualification.state if qualification is not None else
            'pending' if preserved_nodes or delta.get('retained_edges') else 'not_applicable'),
        'zero_orphan_validation': 'pending_history_classification' if orphans else 'passed'}


def verify_candidate_graph_reconciliation(target, boards, *, projection, deadline, historical_observations=None,
        global_comparison=None, global_materialization=None):
    """Verify new projection effects; preserved history never receives implicit approval.

    historical_observations must be freshly derived under the caller's offline
    fences from the retained snapshot. No receipt supplied by a client suffices.
    """
    histories, global_history = {}, 'no_prior_records'
    if historical_observations is not None:
        if historical_observations.get('format') != 'retirement-candidate-history-observations/v4':
            raise ValueError('retirement_candidate_history_observations_invalid')
        proofs = historical_observations['property_composition']
        for item in historical_observations['graphs']:
            delta = item['delta']
            if delta['removed_nodes'] or delta['removed_edges']:
                raise ValueError('retirement_candidate_prior_changes_unclassified')
            for change in delta['changed_nodes']:
                if (item['scope'] != 'board' or sum(proof == {'board_id': item['board_id'], **change}
                        for proof in proofs) != 1):
                    raise ValueError('retirement_candidate_prior_changes_unclassified')
            if item['scope'] == 'global_discovery':
                global_history = item['history_state']
                if item['delta']['introduced_nodes'] or item['delta']['introduced_edges']:
                    if (global_materialization is None or global_materialization.get('state') != 'created'
                            or global_comparison is None or global_comparison['state'] != 'matched'
                            or global_materialization['expected_sha256'] != global_comparison['expected_sha256']
                            or item['history_state'] != 'no_prior_records'
                            or len(delta['introduced_nodes']) != global_comparison['expected_nodes']):
                        raise ValueError('retirement_candidate_global_effects_unowned')
            else:
                if item['board_id'] in histories:
                    raise ValueError('retirement_candidate_history_observations_invalid')
                histories[item['board_id']] = item
    if type(boards) is not list:
        raise ValueError('retirement_candidate_graph_boards_invalid')
    bindings = CommunityGraphBackendBindingStore(target / 'kg-artifacts')
    database_path = target / 'database.sqlite3'
    _sidecars_absent(database_path)
    reports = []
    seen_boards = set()
    planned = {item['projection']['board_id']: item['projection'] for item in projection['boards']}
    if len(planned) != len(projection['boards']) or len(planned) != len(boards):
        raise ValueError('retirement_candidate_graph_boards_invalid')
    for board in boards:
        _check_time(deadline)
        if type(board) is not dict or type(board.get('acks')) is not list:
            raise ValueError('retirement_candidate_graph_board_invalid')
        receipts = tuple(ExactConsolidationAckReceipt.from_payload(value)
            for value in board['acks'])
        board_ids = {ack.board_id for ack in receipts}
        board_id = (next(iter(board_ids)) if board_ids else board.get('binding', {}).get('board_id'))
        if (type(board_id) is not str or not board_id or board_id in seen_boards
                or board_ids not in ({board_id}, set())):
            raise ValueError('retirement_candidate_graph_board_invalid')
        seen_boards.add(board_id)
        binding = bindings.inspect_board_binding(board_id)
        if (binding.backend != 'grafx' or binding.generation != board['binding'].get('generation')):
            raise ValueError('retirement_candidate_graph_binding_changed')
        refs, edge_sessions = _relational_evidence(database_path, receipts)
        board_refs = {identity: session for identity, session in refs.items()
            if next(ack for ack in receipts if ack.consolidation_session_id == session).board_id == board_id}
        if board_id not in planned:
            raise ValueError('retirement_candidate_graph_boards_invalid')
        report = _board_graph(binding, board_refs, sum(edge_sessions.values()), _source_expectations(planned[board_id]), deadline,
            histories.get(board_id), board_id=board_id, cognitive_rows=tuple(planned[board_id]['cognitive_rows']),
            expected_partitions=_partition_expectations(planned[board_id]), expected_edge_sessions=edge_sessions,
            planned_document=json.dumps(planned[board_id], ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode('utf-8'))
        reports.append({'board_id': board_id, 'generation': binding.generation,
            'binding_sha256': binding.binding_sha256, **report})
    _sidecars_absent(database_path)
    if set(histories) - seen_boards:
        raise ValueError('retirement_candidate_history_scope_unplanned')
    if global_comparison is not None and global_comparison['state'] == 'matched' and global_history != 'no_prior_records':
        global_history = 'current_source_reconciled'
    pending = (any(report['history_classification'] == 'pending' for report in reports)
        or any(item['state'] != 'matched' for report in reports for item in report['cognitive_source_parity'])
        or (global_comparison['state'] != 'matched' if global_comparison is not None else global_history != 'no_prior_records'))
    mismatch = any(any(report['source_relation_comparison'][field] for field in
        ('missing_count', 'unresolved_count', 'unexpected_new_count')) for report in reports)
    return {'format': 'retirement-candidate-graph-reconciliation/v14',
        'state': ('source_projection_mismatch' if mismatch else
            'source_projection_reconciled_history_pending' if pending else 'source_graph_reconciled'),
        'global_history_state': global_history, 'global_projection_comparison': global_comparison, 'boards': reports}
