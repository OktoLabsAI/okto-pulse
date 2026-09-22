"""Read authenticated graph history inventory without classifying or editing it.

The retained logical artifact supplies every property, including old generations.
This census is evidence for reconciliation, never authority to discard a record,
inherit a parent's dates, accept historical debt or admit the candidate runtime.
"""

from collections import Counter
from dataclasses import asdict
import hashlib

from okto_pulse.core.kg.logical_transfer import LogicalFingerprintAccumulator, encode_value
from okto_pulse.core.ports.projection_history import (
    ProjectionEdgeFingerprint, ProjectionNodeFingerprint, compare_projection_history,
)

from .joint_recovery_snapshot import verify_joint_recovery_snapshot
from .logical_graph_transfer import LogicalGraphFileSnapshotSource
from .relational_recovery_snapshot import _check_time, _deadline, _digest
from .sprint_retirement_archive import _encode

_LIMIT = 64 * 1024 * 1024
_MAX_NODES = 100_000
_MAX_EDGES = 500_000
_FIELDS = (
    'source_artifact_ref', 'source_session_id', 'created_by_agent', 'graph_layer',
    'maturity_status', 'human_curated', 'generation', 'superseded_by', 'superseded_at',
    'created_at', 'source_created_at', 'source_updated_at', 'source_status', 'severity',
    'resolved_at',
)


def read_retirement_historical_graph_census(snapshot, *, max_seconds=180):
    """Bind a bounded per-record inventory to a verified joint recovery snapshot."""
    deadline = _deadline(max_seconds)
    manifest = verify_joint_recovery_snapshot(snapshot, max_seconds=max_seconds)
    graphs, total_bytes = [], 0
    for graph in manifest['graphs']:
        _check_time(deadline)
        path = snapshot.directory / graph['file']
        reader = LogicalGraphFileSnapshotSource(path).open_snapshot()
        nodes, relations, identities = [], Counter(), set()
        try:
            measured = LogicalFingerprintAccumulator.for_schema(reader.schema())
            counts = reader.counts()
            if counts.nodes > _MAX_NODES or counts.relations > _MAX_EDGES:
                raise ValueError('retirement_historical_census_limit')
            for batch in reader.iter_nodes(batch_size=500):
                _check_time(deadline)
                for node in batch:
                    identity = node.type_name, node.key
                    if identity in identities:
                        raise ValueError('retirement_historical_census_duplicate_node')
                    identities.add(identity)
                    measured.add_node(node)
                    single = LogicalFingerprintAccumulator(measured.schema_hex)
                    single.add_node(node)
                    record = {'type': node.type_name, 'id': node.key, 'sha256': single.digest(),
                        'fields': {name: encode_value(node.properties[name]) for name in _FIELDS
                            if name in node.properties}}
                    total_bytes += len(_encode(record))
                    if len(nodes) >= _MAX_NODES or total_bytes > _LIMIT:
                        raise ValueError('retirement_historical_census_limit')
                    nodes.append(record)
            for batch in reader.iter_relations(batch_size=500):
                _check_time(deadline)
                for relation in batch:
                    measured.add_relation(relation)
                    if measured.relation_count > _MAX_EDGES:
                        raise ValueError('retirement_historical_census_limit')
                    single = LogicalFingerprintAccumulator(measured.schema_hex)
                    single.add_relation(relation)
                    # Multiplicity is evidence: identical parallel edges cannot
                    # disappear into a set or an XOR fingerprint.
                    key = (relation.layout_name, relation.source_type, relation.source_key,
                        relation.target_type, relation.target_key, single.digest())
                    if key not in relations:
                        total_bytes += len(_encode(key)) + 32
                    if total_bytes > _LIMIT:
                        raise ValueError('retirement_historical_census_limit')
                    relations[key] += 1
            certificate = graph['certificate']
            if (not reader.manifest_verified or measured.schema_hex != certificate['schema_digest']
                    or measured.digest() != certificate['fingerprint']
                    or asdict(measured.counts()) != certificate['counts']
                    or _digest(path) != graph['sha256']):
                raise ValueError('retirement_historical_census_snapshot_changed')
        finally:
            reader.close()
        graphs.append({'scope': graph['scope'], 'board_id': graph['board_id'],
            'source_sha256': graph['sha256'], 'schema_digest': measured.schema_hex,
            'fingerprint': measured.digest(), 'counts': asdict(measured.counts()),
            'nodes': sorted(nodes, key=lambda row: (row['type'], row['id'])),
            'relations': [{'name': key[0], 'source_type': key[1], 'source_id': key[2],
                'target_type': key[3], 'target_id': key[4], 'sha256': key[5], 'count': count}
                for key, count in sorted(relations.items())]})
    result = {'format': 'retirement-historical-graph-census/v1',
        'state': 'captured_not_classified', 'snapshot_sha256': snapshot.manifest_sha256, 'graphs': graphs}
    encoded = _encode(result)
    if len(encoded) > _LIMIT:
        raise ValueError('retirement_historical_census_limit')
    _check_time(deadline)
    return result, hashlib.sha256(encoded).hexdigest()


def compare_retirement_historical_graph_censuses(before_snapshot, after_snapshot, *, max_seconds=180):
    """Observe two authenticated snapshots; no delta is implicitly authorized."""
    deadline = _deadline(max_seconds)
    before, before_digest = read_retirement_historical_graph_census(before_snapshot, max_seconds=max_seconds)
    after, after_digest = read_retirement_historical_graph_census(after_snapshot, max_seconds=max_seconds)
    old = {(row['scope'], row['board_id']): row for row in before['graphs']}
    new = {(row['scope'], row['board_id']): row for row in after['graphs']}

    def nodes(graph):
        return tuple(ProjectionNodeFingerprint(row['type'], row['id'], row['sha256'])
            for row in graph.get('nodes', ()))

    def edges(graph):
        return tuple(ProjectionEdgeFingerprint(row['name'], row['source_type'], row['source_id'],
            row['target_type'], row['target_id'], row['sha256'], row['count'])
            for row in graph.get('relations', ()))

    results = []
    for scope, board in sorted(old.keys() | new.keys(), key=lambda key: (key[0], key[1] or '')):
        _check_time(deadline)
        previous, current = old.get((scope, board), {}), new.get((scope, board), {})
        delta = compare_projection_history(before_nodes=nodes(previous), after_nodes=nodes(current),
            before_edges=edges(previous), after_edges=edges(current))
        results.append({'scope': scope, 'board_id': board, 'delta': asdict(delta)})
    result = {'format': 'retirement-historical-graph-observations/v1', 'state': 'observed_not_classified',
        'before_snapshot_sha256': before_snapshot.manifest_sha256,
        'after_snapshot_sha256': after_snapshot.manifest_sha256,
        'before_census_sha256': before_digest, 'after_census_sha256': after_digest, 'graphs': results}
    if len(_encode(result)) > _LIMIT:
        raise ValueError('retirement_historical_census_limit')
    _check_time(deadline)
    return result
