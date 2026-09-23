"""Literal, source-owned node restoration inside an unpublished offline stage.

No edges are inferred. Original graphs, durable source rows and existing nodes
are never overwritten. The enclosing coordinator owns stage cleanup on failure.
"""

from dataclasses import asdict
import json

from okto_grafx import connect
from okto_pulse.core.kg.logical_transfer import LogicalFingerprintAccumulator
from okto_pulse.core.ports.cognitive_projection import (
    cognitive_projection_source_node, compare_cognitive_projection,
    observe_cognitive_restoration, validate_cognitive_projection_sources,
)

from .graph_backend_binding import CommunityGraphBackendBindingStore
from .grafx_graph_store import CommunityGrafxGraphStore
from .grafx_logical_sink import _native_value
from .relational_recovery_snapshot import _deadline, _check_time
from .retirement_candidate_global_reconciliation import _read


def _fingerprint(schema, node):
    measured = LogicalFingerprintAccumulator.for_schema(schema)
    measured.add_node(node)
    return measured.digest()


def _plan(schema, board_id, records, nodes, relations):
    observations = observe_cognitive_restoration(schema=schema, board_id=board_id,
        records=records, nodes=nodes, relations=relations)
    wanted = {(row.node_type, row.node_id): row.literal_fingerprint for row in observations
        if row.state == 'literal_candidate'}
    selected = []
    for record in validate_cognitive_projection_sources(schema=schema, board_id=board_id, records=records):
        key = record['node_type'], record['node_id']
        if key not in wanted:
            continue
        node = cognitive_projection_source_node(schema=schema, board_id=board_id, record=record)
        parity = compare_cognitive_projection(schema=schema, board_id=board_id, record=record, node=node)
        fingerprint = _fingerprint(schema, node)
        if fingerprint != wanted[key] or parity.state != 'matched':
            raise ValueError('retirement_cognitive_literal_plan_changed')
        selected.append((node, {'node_type': node.type_name, 'node_id': node.key,
            'generation': parity.generation, 'source_revision': parity.source_revision,
            'source_fingerprint': parity.source_fingerprint, 'literal_fingerprint': fingerprint}))
    selected.sort(key=lambda item: (item[0].type_name, item[0].key))
    observed = json.loads(json.dumps([asdict(row) for row in observations]))
    return selected, observed


def restore_candidate_cognitive_nodes(target, projection, *, require_live, max_seconds):
    deadline = _deadline(max_seconds)

    def fence(*_):
        _check_time(deadline)
        if require_live() is not True:
            raise ValueError('retirement_cognitive_execution_fence_lost')

    fence()
    bindings, reports = CommunityGraphBackendBindingStore(target / 'kg-artifacts'), []
    for item in projection['boards']:
        plan = item['projection']
        records, board_id = tuple(plan['cognitive_rows']), plan['board_id']
        if not records:
            continue
        fence()
        binding = bindings.inspect_board_binding(board_id)
        schema, nodes, relations = _read(binding, 'board', deadline)
        selected, observations = _plan(schema, board_id, records, nodes, relations)
        if selected:
            with connect(binding.physical_path, page_size=binding.page_size) as database:
                def resolve(wanted):
                    if wanted != board_id:
                        raise ValueError('retirement_cognitive_board_changed')
                    return database

                store = CommunityGrafxGraphStore(resolve, fence)
                for node, _ in selected:
                    fence()
                    definition = schema.node_type(node.type_name)
                    attrs = {name: _native_value(value, definition.property_def(name), database)
                        for name, value in node.properties.items() if name != definition.key}
                    store.create_node(board_id, node.type_name, node.key, attrs)
                fence()
                database.checkpoint()
            current_schema, current, edges = _read(binding, 'board', deadline)
            expected = {(node.type_name, node.key): _fingerprint(schema, node) for node in nodes}
            expected.update({(node.type_name, node.key): row['literal_fingerprint'] for node, row in selected})
            if (relations != edges or len(current) != len(expected)
                    or {(node.type_name, node.key): _fingerprint(current_schema, node) for node in current} != expected):
                raise ValueError('retirement_cognitive_restoration_effects_changed')
        reports.append({'board_id': board_id, 'observations': observations, 'created': [row for _, row in selected]})
    fence()
    report = {'format': 'retirement-cognitive-restoration/v1', 'boards': reports}
    if len(json.dumps(report, ensure_ascii=False).encode('utf-8')) > 64 * 1024 * 1024:
        raise ValueError('retirement_cognitive_restoration_limit')
    return report


def verify_candidate_cognitive_restoration(target, projection, receipt, history, *, max_seconds):
    """Re-derive ownership against source and original-to-candidate census delta."""
    if type(receipt) is not dict or set(receipt) != {'format', 'boards'} or receipt['format'] != 'retirement-cognitive-restoration/v1':
        raise ValueError('retirement_cognitive_restoration_receipt_invalid')
    deadline = _deadline(max_seconds)
    expected_boards = [item['projection'] for item in projection['boards'] if item['projection']['cognitive_rows']]
    if type(receipt['boards']) is not list or len(receipt['boards']) != len(expected_boards):
        raise ValueError('retirement_cognitive_restoration_scope_changed')
    histories = {row['board_id']: row['delta'] for row in history['graphs'] if row['scope'] == 'board'}
    bindings, owned = CommunityGraphBackendBindingStore(target / 'kg-artifacts'), {}
    for plan, report in zip(expected_boards, receipt['boards'], strict=True):
        board_id = plan['board_id']
        if (set(report) != {'board_id', 'observations', 'created'} or report['board_id'] != board_id
                or type(report['created']) is not list):
            raise ValueError('retirement_cognitive_restoration_scope_changed')
        created = {(row['node_type'], row['node_id']): row['literal_fingerprint'] for row in report['created']}
        introduced = {(row['node_type'], row['node_id']): row['fingerprint']
            for row in histories.get(board_id, {}).get('introduced_nodes', ())}
        if len(created) != len(report['created']) or any(introduced.get(key) != value for key, value in created.items()):
            raise ValueError('retirement_cognitive_restoration_prior_identity')
        schema, nodes, relations = _read(bindings.inspect_board_binding(board_id), 'board', deadline)
        if any((edge.source_type, edge.source_key) in created or (edge.target_type, edge.target_key) in created for edge in relations):
            raise ValueError('retirement_cognitive_restoration_unowned_edges')
        before = tuple(node for node in nodes if (node.type_name, node.key) not in created)
        selected, observations = _plan(schema, board_id, tuple(plan['cognitive_rows']), before, relations)
        expected = {'board_id': board_id, 'observations': observations, 'created': [row for _, row in selected]}
        actual = {(node.type_name, node.key): _fingerprint(schema, node) for node in nodes}
        if report != expected or any(actual.get(key) != value for key, value in created.items()):
            raise ValueError('retirement_cognitive_restoration_receipt_changed')
        owned[board_id] = created
    _check_time(deadline)
    return owned
