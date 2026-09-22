"""Read-only source/graph reconciliation for a private retirement candidate."""

from contextlib import closing
import hashlib
import json
import sqlite3

from okto_grafx import connect
from okto_pulse.core.kg.schema_contract import NODE_TYPES
from okto_pulse.core.ports.consolidation import ExactConsolidationAckReceipt

from .grafx_relationship_layout import PULSE_RELATIONSHIP_LAYOUT
from .graph_backend_binding import CommunityGraphBackendBindingStore
from .relational_recovery_snapshot import _check_time, _readonly, _sidecars_absent

_MAX_NODES = 100_000
_MAX_EDGES = 500_000


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':')).encode('ascii')).hexdigest()


def _relational_evidence(database_path, receipts):
    sessions = {ack.consolidation_session_id: ack for ack in receipts}
    refs, edge_count = {}, 0
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
                if type(row['edges_added']) is not int or row['edges_added'] < 0:
                    raise ValueError('retirement_candidate_graph_audit_invalid')
                edge_count += row['edges_added']
        connection.execute('ROLLBACK')
    if len(refs) != sum(ack.node_ref_count for ack in receipts):
        raise ValueError('retirement_candidate_graph_node_ref_count_changed')
    return refs, edge_count


def _board_graph(binding, expected_refs, expected_edge_count, deadline):
    nodes = {}
    with connect(binding.physical_path, page_size=binding.page_size, read_only=True) as graph:
        for node_type in NODE_TYPES:
            _check_time(deadline)
            rows = graph.execute(f'MATCH (n:{node_type}) RETURN n.id, '
                'n.source_artifact_ref, n.source_session_id, n.created_by_agent').rows
            for row in rows:
                identity = node_type, str(row[0])
                if (identity in nodes or not row[1] or not row[2] or not row[3]
                        or str(row[1]).startswith('sprint:')):
                    raise ValueError('retirement_candidate_graph_node_invalid')
                nodes[identity] = (str(row[1]), str(row[2]), str(row[3]))
                if len(nodes) > _MAX_NODES:
                    raise ValueError('retirement_candidate_graph_limit')
        if set(nodes) != set(expected_refs):
            raise ValueError('retirement_candidate_graph_node_census_changed')
        if any(nodes[identity][1] != session_id
                for identity, session_id in expected_refs.items()):
            raise ValueError('retirement_candidate_graph_node_session_changed')

        allowed_edges = {entry.physical_table: entry
            for entry in PULSE_RELATIONSHIP_LAYOUT.entries}
        rows = graph.execute('MATCH (a)-[r]->(b) RETURN a.id, b.id, type(r), '
            'r.rule_id, r.layer, r.created_by').rows
        if len(rows) > _MAX_EDGES:
            raise ValueError('retirement_candidate_graph_limit')
        node_by_id = {}
        for identity in nodes:
            if identity[1] in node_by_id:
                raise ValueError('retirement_candidate_graph_node_identity_ambiguous')
            node_by_id[identity[1]] = identity[0]
        edges = []
        for row in rows:
            source_id, target_id, physical = str(row[0]), str(row[1]), str(row[2])
            definition = allowed_edges.get(physical)
            if (definition is None or node_by_id.get(source_id) != definition.from_type
                    or node_by_id.get(target_id) != definition.to_type
                    or type(row[3]) is not str or not row[3]
                    or row[4] != 'deterministic'
                    or type(row[5]) is not str or not row[5]):
                raise ValueError('retirement_candidate_graph_edge_invalid')
            edges.append((physical, source_id, target_id, str(row[3]), str(row[4]), str(row[5])))
        if len(edges) != expected_edge_count or len(edges) != len(set(edges)):
            raise ValueError('retirement_candidate_graph_edge_census_changed')

        connected = set()
        for _physical, source_id, target_id, *_metadata in edges:
            connected.add(source_id)
            connected.add(target_id)
        if set(node_by_id) - connected:
            raise ValueError('retirement_candidate_graph_orphan_detected')
    canonical_nodes = sorted((kind, node_id, *values)
        for (kind, node_id), values in nodes.items())
    return {'node_count': len(nodes), 'edge_count': len(edges),
        'node_sha256': _digest(canonical_nodes), 'edge_sha256': _digest(sorted(edges)),
        'zero_orphan_validation': 'passed'}


def verify_candidate_graph_reconciliation(target, boards, *, deadline):
    """Bind every candidate node/edge to exact ACK evidence and reject orphans."""
    if type(boards) is not list:
        raise ValueError('retirement_candidate_graph_boards_invalid')
    bindings = CommunityGraphBackendBindingStore(target / 'kg-artifacts')
    database_path = target / 'database.sqlite3'
    _sidecars_absent(database_path)
    reports = []
    seen_boards = set()
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
        refs, edge_count = _relational_evidence(database_path, receipts)
        board_refs = {identity: session for identity, session in refs.items()
            if next(ack for ack in receipts if ack.consolidation_session_id == session).board_id == board_id}
        report = _board_graph(binding, board_refs, edge_count, deadline)
        reports.append({'board_id': board_id, 'generation': binding.generation,
            'binding_sha256': binding.binding_sha256, **report})
    _sidecars_absent(database_path)
    return {'format': 'retirement-candidate-graph-reconciliation/v1',
        'state': 'source_graph_reconciled', 'boards': reports}
