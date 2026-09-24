"""Re-derive a schema-adjusted baseline without changing authenticated history."""

from dataclasses import asdict
import hashlib

from .joint_recovery_snapshot import verify_joint_recovery_snapshot
from .relational_recovery_snapshot import _deadline, _check_time
from .retirement_historical_graph_census import read_graph_record_census
from .retirement_schema_evolution import evolution_snapshot_source, schema_evolution_receipt, evolution_target_version
from .sprint_retirement_archive import _encode


def read_retirement_schema_baseline(snapshot, original, original_digest, evolutions, *, node_observer=None, max_seconds=180):
    """Compare sealed evolution receipts against the one frozen transformation."""
    deadline = _deadline(max_seconds)
    if type(evolutions) is not tuple or len(evolutions) > 100_000 or any(type(item) is not dict for item in evolutions):
        raise ValueError('retirement_schema_evolutions_invalid')
    if not evolutions:
        return original, original_digest
    manifest = verify_joint_recovery_snapshot(snapshot, max_seconds=max_seconds)
    if 'native_graphs' not in manifest:
        raise ValueError('retirement_schema_native_history_backup_required')
    graphs = {(graph['scope'], graph['board_id']): graph for graph in original['graphs']}
    seen, verified, budget = set(), [], [len(_encode(original))]
    for receipt in evolutions:
        _check_time(deadline)
        board = receipt.get('board_id')
        if type(board) is not str or not board or board in seen:
            raise ValueError('retirement_schema_evolution_scope_invalid')
        seen.add(board)
        matches = [(index, graph) for index, graph in enumerate(manifest['graphs'])
            if graph['scope'] == 'board' and graph['board_id'] == board]
        if len(matches) != 1 or ('board', board) not in graphs:
            raise ValueError('retirement_schema_evolution_scope_invalid')
        index, graph = matches[0]
        target_version = evolution_target_version(receipt)
        reader = evolution_snapshot_source(snapshot, graph, deadline=deadline, target_version=target_version).open_snapshot()
        try:
            measured, records = read_graph_record_census(reader, deadline=deadline, budget=budget,
                node_observer=(lambda schema, node: node_observer('board', board, schema, node))
                    if node_observer is not None else None)
        finally:
            reader.close()
        expected = schema_evolution_receipt(snapshot, manifest, index, scope='board', counts=asdict(measured.counts()),
            fingerprint=measured.digest(), schema_digest=measured.schema_hex, target_version=target_version)
        if _encode(expected) != _encode(receipt):
            raise ValueError('retirement_schema_evolution_receipt_changed')
        verified.append(expected)
        graphs[('board', board)] = {**graphs[('board', board)], **records}
    baseline = {'format': 'retirement-schema-baseline/v1', 'snapshot_sha256': snapshot.manifest_sha256,
        'original_census_sha256': original_digest, 'schema_evolutions': verified,
        'graphs': [graphs[(graph['scope'], graph['board_id'])] for graph in original['graphs']]}
    encoded = _encode(baseline)
    if len(encoded) > 64 * 1024 * 1024:
        raise ValueError('retirement_schema_baseline_limit')
    return baseline, hashlib.sha256(encoded).hexdigest()
