"""Observe private, offline candidate records against authenticated history.

The caller holds the retirement fences and seals the returned evidence with the
candidate checkpoint. Observations never waive source, active-set or orphan
reconciliation, and do not authorize historical removal or runtime admission.
"""

from contextlib import closing
from dataclasses import asdict
import hashlib

from okto_grafx import connect
from okto_pulse.core.kg.logical_transfer import encode_value, schema_digest
from okto_pulse.core.ports.projection_effects import ProjectionPropertyEffects, reconcile_projection_node_effects

from .logical_transfer_factories import make_grafx_logical_source
from .recovery_graph_inventory import read_recovery_graph_inventory
from .relational_recovery_snapshot import _check_time, _deadline, _readonly, _sidecars_absent
from .retirement_historical_graph_census import (
    compare_graph_record_censuses, read_graph_record_census,
    read_retirement_historical_graph_census,
)
from .sprint_retirement_archive import _encode

_LIMIT = 64 * 1024 * 1024


def observe_candidate_history(target, snapshot, *, max_seconds=180, property_effects=()):
    """Read all scopes; effect envelopes must come from complete SQL/ACK validation."""
    deadline = _deadline(max_seconds)
    if (type(property_effects) is not tuple
            or any(type(effect) is not ProjectionPropertyEffects for effect in property_effects)):
        raise ValueError('retirement_candidate_property_effects_invalid')
    wanted = {(effect.board_id, node.node_type, node.node_id)
        for effect in property_effects for node in effect.nodes}
    old_nodes, matched, proofs, retained_bytes = {}, set(), [], [0]

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

    previous, previous_digest = read_retirement_historical_graph_census(snapshot,
        max_seconds=max_seconds, node_observer=before_node)
    sql = target / 'database.sqlite3'
    kg = target / 'kg-artifacts'
    _sidecars_absent(sql)
    graphs, budget = [], [0]
    with closing(_readonly(sql, immutable=True)) as connection:
        connection.execute('BEGIN')
        inventory = read_recovery_graph_inventory(connection, kg)
        inventory.require_resolved_routes()
        for route in inventory.routes:
            _check_time(deadline)
            if route.state != 'bound':
                continue
            with connect(route.physical_path, page_size=route.page_size, read_only=True) as database:
                reader = make_grafx_logical_source(database, scope=route.scope).open_snapshot()
                try:
                    _, records = read_graph_record_census(reader, deadline=deadline, budget=budget,
                        node_observer=(lambda schema, node: after_node(route.board_id, schema, node))
                            if route.scope == 'board' else None)
                finally:
                    reader.close()
                graphs.append({'scope': route.scope, 'board_id': route.board_id,
                    'generation': route.generation, 'binding_sha256': route.binding_sha256,
                    'database_uuid': database.identity.database_uuid.hex(), **records})
        if read_recovery_graph_inventory(connection, kg) != inventory:
            raise ValueError('retirement_candidate_history_routes_changed')
        connection.execute('ROLLBACK')
    _sidecars_absent(sql)
    current = {'format': 'retirement-candidate-record-census/v1', 'graphs': graphs}
    encoded = _encode(current)
    if len(encoded) > _LIMIT:
        raise ValueError('retirement_candidate_history_limit')
    observations = compare_graph_record_censuses(previous['graphs'], graphs, deadline=deadline)
    if set(old_nodes) != matched:
        raise ValueError('retirement_candidate_property_effect_node_removed')
    result = {'format': 'retirement-candidate-history-observations/v3',
        'state': 'observed_not_classified', 'snapshot_sha256': snapshot.manifest_sha256,
        'before_census_sha256': previous_digest,
        'candidate_census_sha256': hashlib.sha256(encoded).hexdigest(), 'graphs': observations,
        'property_composition': sorted(proofs, key=lambda row: (row['board_id'], row['before']['node_type'], row['before']['node_id']))}
    if len(_encode(result)) > _LIMIT:
        raise ValueError('retirement_candidate_history_limit')
    _check_time(deadline)
    return result
