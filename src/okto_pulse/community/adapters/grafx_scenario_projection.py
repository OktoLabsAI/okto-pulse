"""Grafx mechanics for closed, Core-owned Owned relationship families."""

from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetReceipt, ProjectionActiveSetReconciliationError, ProjectionEdgeBeforeImage,
)
from okto_pulse.core.ports.spec_projection import spec_relationship_family
from okto_pulse.core.ports.code_evidence_projection import traceability_relationship_family
from okto_pulse.community.adapters.grafx_query_values import normalize_query_value


def _refuse(code, message):
    raise ProjectionActiveSetReconciliationError(code, message)


def reconcile_spec_relationships(scope, intent):
    evidence = intent.owner_type in {"code_evidence", "implementation_target"}
    family = traceability_relationship_family(intent.namespace) if evidence else spec_relationship_family(intent.namespace)
    owner_ref = f"{intent.owner_type}:{intent.owner_id}"
    if intent.active_nodes or not intent.owner_node_id:
        _refuse("projection_active_set_member_invalid", "Owned relationship projection owns edges and requires its root.")
    root = scope._node_snapshot("Entity", intent.owner_node_id)
    if root is None or root.get("source_artifact_ref") != owner_ref:
        _refuse("projection_active_set_scope_invalid", "Owned relationship projection owner root does not match its source.")
    desired = set()
    endpoints = set()
    for edge in intent.active_edges:
        pair = (edge.from_type, edge.from_id, edge.to_type, edge.to_id)
        if (edge.edge_type != family.edge_type or edge.rule_id not in family.rules or pair in endpoints):
            _refuse("projection_active_set_member_invalid", "Owned relationship identity is invalid or duplicated.")
        source = scope._node_snapshot(edge.from_type, edge.from_id)
        target = scope._node_snapshot(edge.to_type, edge.to_id)
        if source is None or target is None or not family.owns_endpoints(owner_id=intent.owner_id,
                source_type=edge.from_type, target_type=edge.to_type,
                source_ref=source.get('source_artifact_ref'), target_ref=target.get('source_artifact_ref')):
            _refuse("projection_active_set_member_invalid", "Projection endpoint belongs to another source.")
        desired.add((*pair, edge.rule_id))
        endpoints.add(pair)

    def owned():
        result = {}
        for target_type in dict.fromkeys(kind for kind, _section in family.target_sections):
            physical, definition = scope._relationship_definition(family.edge_type, family.source_type, target_type)
            properties = scope._projection_edge_properties(definition)
            projection = ", ".join(f"r.{name}" for name in properties)
            rows = scope._query(
                f"MATCH (a:{family.source_type})-[r:{physical}]->(b:{target_type}) "
                f"WHERE a.source_artifact_ref {'=' if evidence else 'STARTS WITH'} $prefix "
                f"RETURN a.id, b.id, a.source_artifact_ref, b.source_artifact_ref, {projection}",
                {"prefix": owner_ref if evidence else owner_ref + ":"}, operation="projection_spec_relationship_read",
            ).rows
            for row in rows:
                attrs = {name: normalize_query_value(row[index + 4]) for index, name in enumerate(properties)}
                if (not family.owns_endpoints(owner_id=intent.owner_id, source_type=family.source_type,
                        target_type=target_type, source_ref=row[2], target_ref=row[3])
                        or not family.owns_writer(rule_id=attrs.get('rule_id'), layer=attrs.get('layer'),
                            created_by=attrs.get('created_by'))):
                    continue
                key = (family.source_type, str(row[0]), target_type, str(row[1]), attrs['rule_id'])
                if key in result:
                    _refuse("projection_active_set_source_ref_ambiguous", "An owned edge has multiple physical copies.")
                result[key] = ProjectionEdgeBeforeImage(family.edge_type, family.source_type, target_type,
                    str(row[0]), str(row[1]), attrs)
        return result

    current = owned()
    if desired.difference(current):
        _refuse("projection_active_set_member_missing", "An active Owned relationship is missing or untrusted.")
    stale = tuple(edge for key, edge in current.items() if key not in desired)
    receipt = ProjectionActiveSetReceipt(intent=intent, edge_before_images=stale)
    try:
        for edge in stale:
            physical, _definition = scope._relationship_definition(edge.edge_type, edge.from_type, edge.to_type)
            scope._mutation(
                f"MATCH (a:{edge.from_type})-[r:{physical}]->(b:{edge.to_type}) "
                "WHERE a.id=$source AND b.id=$target AND r.rule_id=$rule "
                "AND r.layer=$layer AND r.created_by=$writer DELETE r",
                {"source": edge.from_id, "target": edge.to_id, "rule": edge.attrs["rule_id"],
                 "layer": edge.attrs["layer"], "writer": edge.attrs["created_by"]},
                operation=("delete_projection_scenario_criterion_edge" if intent.namespace == 'scenario_criteria'
                    else 'delete_projection_decision_requirement_edge' if intent.namespace == 'decision_requirements'
                    else 'delete_projection_spec_relationship_edge'),
            )
        if set(owned()) != desired:
            _refuse("projection_stale_edge_cleanup_unconfirmed", "Owned relationship active set did not converge.")
    except BaseException as error:
        scope._projection_apply_failure(receipt, error, operation="reconcile_spec_relationships")
    return receipt
