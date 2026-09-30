"""Native storage mechanics for the closed Core Card scenario ownership rule."""
from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetReceipt, ProjectionActiveSetReconciliationError, ProjectionEdgeBeforeImage,
)
from okto_pulse.core.ports.card_projection import owns_card_scenario_endpoints, is_card_scenario_writer
from okto_pulse.community.adapters.grafx_query_values import normalize_query_value


def _refuse(message):
    raise ProjectionActiveSetReconciliationError('projection_active_set_member_invalid', message)


def reconcile_card_scenarios(scope, intent):
    if intent.active_nodes or (intent.active_edges and not intent.owner_node_id):
        _refuse('Card scenario projection owns edges and requires its root.')
    roots = ([(kind, scope._node_snapshot(kind, intent.owner_node_id)) for kind in ('Entity', 'Bug')]
             if intent.owner_node_id else [])
    roots = [(kind, root) for kind, root in roots if root is not None and root.get('source_artifact_ref') == f'card:{intent.owner_id}']
    if intent.owner_node_id and len(roots) != 1:
        _refuse('Card scenario owner root is absent or ambiguous.')
    desired, endpoints = set(), set()
    for edge in intent.active_edges:
        key = (edge.from_type, edge.from_id, edge.to_id, edge.rule_id)
        target = scope._node_snapshot(edge.to_type, edge.to_id)
        if (edge.edge_type != 'supports' or edge.from_type != roots[0][0]
                or edge.from_id != intent.owner_node_id or target is None
                or (edge.from_type, edge.from_id, edge.to_id) in endpoints
                or not is_card_scenario_writer(rule_id=edge.rule_id, layer='deterministic', created_by='worker_layer1')
                or not owns_card_scenario_endpoints(owner_id=intent.owner_id, source_type=edge.from_type,
                    target_type=edge.to_type, source_ref=roots[0][1].get('source_artifact_ref'),
                    target_ref=target.get('source_artifact_ref'))):
            _refuse('Card scenario endpoint or rule is outside its owner scope.')
        desired.add(key)
        endpoints.add(key[:3])

    def owned():
        found = {}
        for kind in ('Entity', 'Bug'):
            physical, definition = scope._relationship_definition('supports', kind, 'TestScenario')
            fields = scope._projection_edge_properties(definition)
            projection = ', '.join('r.' + field for field in fields)
            rows = scope._query(f'MATCH (a:{kind})-[r:{physical}]->(b:TestScenario) '
                'WHERE a.source_artifact_ref=$owner '
                f'RETURN a.id,b.id,a.source_artifact_ref,b.source_artifact_ref,{projection}',
                {'owner': f'card:{intent.owner_id}'}, operation='projection_card_scenario_read').rows
            for row in rows:
                attrs = {field: normalize_query_value(row[index + 4]) for index, field in enumerate(fields)}
                if (not owns_card_scenario_endpoints(owner_id=intent.owner_id, source_type=kind,
                        target_type='TestScenario', source_ref=row[2], target_ref=row[3])
                        or not is_card_scenario_writer(rule_id=attrs.get('rule_id'), layer=attrs.get('layer'), created_by=attrs.get('created_by'))):
                    continue
                key = kind, str(row[0]), str(row[1]), attrs['rule_id']
                if key in found:
                    _refuse('Card scenario owned edge has duplicate physical copies.')
                found[key] = ProjectionEdgeBeforeImage('supports', kind, 'TestScenario', str(row[0]), str(row[1]), attrs)
        return found

    current = owned()
    if desired - current.keys():
        _refuse('An active Card scenario relation is missing or untrusted.')
    receipt = ProjectionActiveSetReceipt(intent=intent,
        edge_before_images=tuple(edge for key, edge in current.items() if key not in desired))
    try:
        for edge in receipt.edge_before_images:
            physical, _ = scope._relationship_definition(edge.edge_type, edge.from_type, edge.to_type)
            scope._mutation(f'MATCH (a:{edge.from_type})-[r:{physical}]->(b:TestScenario) '
                'WHERE a.id=$source AND b.id=$target AND r.rule_id=$rule '
                'AND r.layer=$layer AND r.created_by=$writer DELETE r',
                {'source': edge.from_id, 'target': edge.to_id, 'rule': edge.attrs['rule_id'],
                 'layer': edge.attrs['layer'], 'writer': edge.attrs['created_by']}, operation='delete_projection_card_scenario_edge')
        if set(owned()) != desired:
            _refuse('Card scenario active set did not converge.')
    except BaseException as error:
        scope._projection_apply_failure(receipt, error, operation='reconcile_card_scenarios')
    return receipt
