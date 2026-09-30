"""Observe private, offline candidate records against authenticated history.

The caller holds the retirement fences and seals the returned evidence with the
candidate checkpoint. Observations never waive source, active-set or orphan
reconciliation, and do not authorize historical removal or runtime admission.
"""

from contextlib import closing
from dataclasses import asdict
import hashlib
import json

from okto_grafx import connect
from okto_pulse.core.kg.logical_transfer import LOGICAL_NULL, encode_value, schema_digest
from okto_pulse.core.ports.projection_effects import (
    ProjectionPropertyEffects, reconcile_projection_node_effects, observe_bug_projection_supersedence,
)

from .logical_transfer_factories import make_grafx_logical_source
from .recovery_graph_inventory import read_recovery_graph_inventory
from .relational_recovery_snapshot import _check_time, _deadline, _readonly, _sidecars_absent
from .retirement_historical_graph_census import (
    compare_graph_record_censuses, read_graph_record_census,
    read_retirement_historical_graph_census,
)
from .sprint_retirement_archive import _encode
from .retirement_schema_baseline import read_retirement_schema_baseline

_LIMIT = 64 * 1024 * 1024


def observe_candidate_history(target, snapshot, *, max_seconds=180, property_effects=(), schema_evolutions=(), learning_phase=None):
    """Read all scopes; effect envelopes must come from complete SQL/ACK validation."""
    deadline = _deadline(max_seconds)
    from .retirement_learning_execution import VerifiedLearningPhase
    from .logical_graph_transfer import LogicalGraphFileSnapshotSource
    if learning_phase is not None and type(learning_phase) is not VerifiedLearningPhase:
        raise TypeError('retirement_candidate_verified_learning_phase_required')
    initial_graphs = learning_phase.initial_graphs if learning_phase is not None else {}
    if (type(property_effects) is not tuple
            or any(type(effect) is not ProjectionPropertyEffects for effect in property_effects)):
        raise ValueError('retirement_candidate_property_effects_invalid')
    wanted = {(effect.board_id, node.node_type, node.node_id)
        for effect in property_effects for node in effect.nodes}
    old_nodes, matched, proofs, retained_bytes = {}, set(), [], [0]
    bug_nodes, supersedence = {}, []
    if (type(schema_evolutions) is not tuple or any(type(item) is not dict
            or type(item.get('board_id')) is not str for item in schema_evolutions)):
        raise ValueError('retirement_candidate_schema_evolutions_invalid')
    evolved_boards = {item.get('board_id') for item in schema_evolutions}

    def before_node(scope, board, schema, node):
        key = board, node.type_name, node.key
        if scope != 'board' or key not in wanted:
            return
        retained_bytes[0] += len(_encode({name: encode_value(value) for name, value in node.properties.items()}))
        if retained_bytes[0] > _LIMIT:
            raise ValueError('retirement_candidate_property_effects_limit')
        old_nodes[key] = schema, node

    def after_node(board, schema, node):
        key = board, node.type_name, node.key
        if learning_phase is not None and node.type_name == 'Bug':
            retained_bytes[0] += len(_encode({name: encode_value(value) for name, value in node.properties.items()}))
            if retained_bytes[0] > _LIMIT or key in bug_nodes:
                raise ValueError('retirement_candidate_supersedence_inventory_invalid')
            bug_nodes[key] = node
        if key not in old_nodes:
            return  # A later session can reuse an ACK-owned newly created node.
        old_schema, old = old_nodes[key]
        if schema_digest(old_schema) != schema_digest(schema) or key in matched:
            raise ValueError('retirement_candidate_property_effects_schema_or_identity_changed')
        effects = tuple(effect for effect in property_effects if effect.board_id == board)
        proof = reconcile_projection_node_effects(schema=schema, board_id=board,
            before=old, after=node, effects=effects)
        proofs.append({'board_id': board, **asdict(proof)})
        matched.add(key)

    def after_relation(board, schema, relation):
        if learning_phase is None or (relation.layout_name, relation.source_type, relation.target_type) != (
                'supersedes', 'Bug', 'Bug'):
            return
        target_key = board, 'Bug', relation.target_key
        old = old_nodes.get(target_key)
        if old is None or old[1].properties.get('superseded_by') not in (LOGICAL_NULL, ''):
            return  # An unchanged historical trail is not a newly observed transition.
        current = bug_nodes.get(target_key)
        following = bug_nodes.get((board, 'Bug', relation.source_key))
        if current is None or following is None or current.properties.get('superseded_by') != following.key:
            return  # The reconciliation gate will reject an unproved new edge.
        # Session ownership comes from the authenticated property trace, not
        # the relation's self-declared actor or layer.
        owners = [effect.session_id for effect in property_effects if effect.board_id == board
            for node in effect.nodes if (node.node_type, node.node_id) == ('Bug', relation.target_key)
            and 'superseded_by' in json.loads(node.after_json)]
        if len(owners) != 1:
            raise ValueError('retirement_candidate_supersedence_owner_ambiguous')
        proof = observe_bug_projection_supersedence(schema=schema, board_id=board, session_id=owners[0],
            before=old[1], after=current, successor=following, relation=relation,
            effects=tuple(effect for effect in property_effects if effect.board_id == board))
        payload = asdict(proof)
        retained_bytes[0] += len(_encode(payload))
        if retained_bytes[0] > _LIMIT:
            raise ValueError('retirement_candidate_supersedence_inventory_invalid')
        supersedence.append(payload)

    def original_node(scope, board, schema, node):
        if board not in evolved_boards:
            before_node(scope, board, schema, node)

    previous, original_digest = read_retirement_historical_graph_census(snapshot,
        max_seconds=max_seconds, node_observer=original_node)
    previous, baseline_digest = read_retirement_schema_baseline(snapshot, previous, original_digest,
        schema_evolutions, node_observer=before_node, max_seconds=max_seconds)
    sql = target / 'database.sqlite3'
    kg = target / 'kg-artifacts'
    _sidecars_absent(sql)
    graphs, terminal_graphs, observed_initial, budget = [], [], set(), [0]
    with closing(_readonly(sql, immutable=True)) as connection:
        connection.execute('BEGIN')
        inventory = read_recovery_graph_inventory(connection, kg)
        inventory.require_resolved_routes()
        for route in inventory.routes:
            _check_time(deadline)
            if route.state != 'bound':
                continue
            with connect(route.physical_path, page_size=route.page_size, read_only=True) as database:
                initial = initial_graphs.get(route.board_id) if route.scope == 'board' else None
                reader = make_grafx_logical_source(database, scope=route.scope).open_snapshot()
                try:
                    _, records = read_graph_record_census(reader, deadline=deadline, budget=budget,
                        node_observer=(lambda schema, node: after_node(route.board_id, schema, node))
                            if route.scope == 'board' and initial is None else None,
                        relation_observer=(lambda schema, relation: after_relation(route.board_id, schema, relation))
                            if route.scope == 'board' and initial is None else None)
                finally:
                    reader.close()
                envelope = {'scope': route.scope, 'board_id': route.board_id,
                    'generation': route.generation, 'binding_sha256': route.binding_sha256,
                    'database_uuid': database.identity.database_uuid.hex()}
                terminal_graphs.append({**envelope, **records})
                if initial is not None:
                    observed_initial.add(route.board_id)
                    reader = LogicalGraphFileSnapshotSource(initial).open_snapshot()
                    try:
                        _, records = read_graph_record_census(reader, deadline=deadline, budget=budget,
                            node_observer=lambda schema, node: after_node(route.board_id, schema, node),
                            relation_observer=lambda schema, relation: after_relation(route.board_id, schema, relation))
                        if not reader.manifest_verified:
                            raise ValueError('retirement_candidate_learning_initial_graph_unverified')
                    finally:
                        reader.close()
                graphs.append({**envelope, **records})
        if read_recovery_graph_inventory(connection, kg) != inventory:
            raise ValueError('retirement_candidate_history_routes_changed')
        connection.execute('ROLLBACK')
    _sidecars_absent(sql)
    if observed_initial != set(initial_graphs):
        raise ValueError('retirement_candidate_learning_initial_scope_changed')
    current = {'format': 'retirement-candidate-record-census/v1', 'graphs': graphs}
    encoded = _encode(current)
    if len(encoded) > _LIMIT:
        raise ValueError('retirement_candidate_history_limit')
    observations = compare_graph_record_censuses(previous['graphs'], graphs, deadline=deadline)
    if set(old_nodes) != matched:
        raise ValueError('retirement_candidate_property_effect_node_removed')
    result = {'format': 'retirement-candidate-history-observations/v4',
        'state': 'observed_not_classified', 'snapshot_sha256': snapshot.manifest_sha256,
        'before_census_sha256': original_digest, 'projection_baseline_sha256': baseline_digest,
        'candidate_census_sha256': hashlib.sha256(encoded).hexdigest(), 'graphs': observations,
        'property_composition': sorted(proofs, key=lambda row: (row['board_id'], row['before']['node_type'], row['before']['node_id']))}
    if learning_phase is not None:
        result['format'] = 'retirement-candidate-history-observations/v5'
        result['state'] = 'observed_before_learning_not_classified'
        result['before_learning_census_sha256'] = result.pop('candidate_census_sha256')
        result['candidate_census_sha256'] = hashlib.sha256(_encode(
            {'format': 'retirement-candidate-record-census/v1', 'graphs': terminal_graphs})).hexdigest()
        result['terminal_graphs'] = compare_graph_record_censuses(previous['graphs'], terminal_graphs, deadline=deadline)
        result['supersedence_effects'] = sorted(supersedence,
            key=lambda row: (row['board_id'], row['predecessor']['before']['node_id'], row['successor']['node_id']))
    if len(_encode(result)) > _LIMIT:
        raise ValueError('retirement_candidate_history_limit')
    _check_time(deadline)
    return result
