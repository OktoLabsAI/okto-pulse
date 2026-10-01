"""Native reconciliation of the dependent Card's closed precedes active set."""
from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetReceipt, ProjectionActiveSetReconciliationError,
    ProjectionEdgeBeforeImage, ProjectionRemovalOnlyIntent,
)
from okto_pulse.core.ports.card_projection import is_card_dependency_writer, owns_card_dependency_endpoints
from .grafx_query_values import normalize_query_value


def reconcile_card_dependencies(scope, intent):
    def refuse():
        raise ProjectionActiveSetReconciliationError('projection_active_set_member_invalid',
            'Card dependency is outside its exact owner, endpoint or writer scope.')

    def owns(source_type, target_type, source_ref, target_ref, rule):
        return (owns_card_dependency_endpoints(owner_id=intent.owner_id, source_type=source_type,
            target_type=target_type, source_ref=source_ref, target_ref=target_ref)
            and is_card_dependency_writer(rule_id=rule, layer='deterministic', created_by='worker_layer1'))

    removal_only = isinstance(intent, ProjectionRemovalOnlyIntent)
    if intent.active_nodes or (intent.active_edges and not intent.owner_node_id):
        refuse()
    expected = set()
    for edge in intent.expected_edges if removal_only else ():
        key = (edge.from_type, edge.to_type, edge.source_ref, edge.rule_id)
        if (edge.edge_type != 'precedes' or key in expected
                or not owns(edge.from_type, edge.to_type, edge.source_ref, edge.target_ref, edge.rule_id)):
            refuse()
        expected.add(key)
    if removal_only and intent.active_edges:
        refuse()
    roots = [(kind, scope._node_snapshot(kind, intent.owner_node_id)) for kind in ('Entity', 'Bug')] if intent.owner_node_id else []
    roots = [(kind, root) for kind, root in roots if root and root.get('source_artifact_ref') == f'card:{intent.owner_id}']
    if intent.owner_node_id and len(roots) != 1:
        refuse()
    desired, endpoints = set(), set()
    for edge in intent.active_edges:
        source = scope._node_snapshot(edge.from_type, edge.from_id)
        key = (edge.from_type, edge.to_type, edge.from_id, edge.to_id, edge.rule_id)
        if (edge.edge_type != 'precedes' or edge.to_type != roots[0][0]
                or edge.to_id != intent.owner_node_id or source is None or key[:4] in endpoints
                or not owns(edge.from_type, edge.to_type, source.get('source_artifact_ref'),
                    roots[0][1].get('source_artifact_ref'), edge.rule_id)):
            refuse()
        endpoints.add(key[:4])
        desired.add(key)

    logical_keys = {}

    def observed():
        result = {}
        for source_type in ('Entity', 'Bug'):
            for target_type in ('Entity', 'Bug'):
                physical, definition = scope._relationship_definition('precedes', source_type, target_type)
                fields = scope._projection_edge_properties(definition)
                projection = ', '.join('r.' + field for field in fields)
                rows = scope._query(f'MATCH (a:{source_type})-[r:{physical}]->(b:{target_type}) '
                    'WHERE b.source_artifact_ref=$owner '
                    f'RETURN a.id,b.id,a.source_artifact_ref,b.source_artifact_ref,{projection}',
                    {'owner': f'card:{intent.owner_id}'}, operation='projection_card_dependencies_read').rows
                for row in rows:
                    attrs = {field: normalize_query_value(row[index + 4]) for index, field in enumerate(fields)}
                    if (not owns(source_type, target_type, row[2], row[3], attrs.get('rule_id'))
                            or not is_card_dependency_writer(rule_id=attrs.get('rule_id'),
                                layer=attrs.get('layer'), created_by=attrs.get('created_by'))):
                        continue
                    key = source_type, target_type, str(row[0]), str(row[1]), attrs['rule_id']
                    if key in result:
                        refuse()
                    result[key] = ProjectionEdgeBeforeImage('precedes', source_type, target_type,
                        str(row[0]), str(row[1]), attrs)
                    logical_keys[key] = source_type, target_type, str(row[2]), attrs['rule_id']
        return result

    current = observed()
    if removal_only:
        desired = {key for key in current if logical_keys[key] in expected}
    if desired - current.keys():
        refuse()
    receipt = ProjectionActiveSetReceipt(intent=intent,
        edge_before_images=tuple(edge for key, edge in current.items() if key not in desired))
    try:
        for edge in receipt.edge_before_images:
            physical, _ = scope._relationship_definition('precedes', edge.from_type, edge.to_type)
            scope._mutation(f'MATCH (a:{edge.from_type})-[r:{physical}]->(b:{edge.to_type}) '
                'WHERE a.id=$source AND b.id=$target AND r.rule_id=$rule '
                'AND r.layer=$layer AND r.created_by=$writer DELETE r',
                {'source': edge.from_id, 'target': edge.to_id, 'rule': edge.attrs['rule_id'],
                 'layer': edge.attrs['layer'], 'writer': edge.attrs['created_by']},
                operation='delete_projection_card_dependency_edge')
        if set(observed()) != desired:
            refuse()
    except BaseException as error:
        scope._projection_apply_failure(receipt, error, operation='reconcile_card_dependencies')
    return receipt
