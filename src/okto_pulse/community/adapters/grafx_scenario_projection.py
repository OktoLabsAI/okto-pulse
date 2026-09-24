"""Grafx mechanics for one owned scenario-to-criterion relationship set."""

from okto_pulse.core.kg.interfaces.graph_transaction import (
    ProjectionActiveSetReceipt, ProjectionActiveSetReconciliationError, ProjectionEdgeBeforeImage,
)
from okto_pulse.core.ports.spec_projection import (
    SCENARIO_CRITERIA_RULES, is_spec_child_reference, owns_scenario_criterion_edge,
)
from okto_pulse.community.adapters.grafx_query_values import normalize_query_value


def _refuse(code, message):
    raise ProjectionActiveSetReconciliationError(code, message)


def reconcile_scenario_criteria(scope, intent):
    if intent.active_nodes or not intent.owner_node_id:
        _refuse("projection_active_set_member_invalid", "Scenario projection owns edges and requires its root.")
    root = scope._node_snapshot("Entity", intent.owner_node_id)
    if root is None or root.get("source_artifact_ref") != f"spec:{intent.owner_id}":
        _refuse("projection_active_set_scope_invalid", "Scenario projection owner root does not match its Spec.")
    desired = set()
    endpoints = set()
    for edge in intent.active_edges:
        if (edge.edge_type != "tests" or edge.from_type != "TestScenario" or edge.to_type != "Criterion"
                or edge.rule_id not in SCENARIO_CRITERIA_RULES or (edge.from_id, edge.to_id) in endpoints):
            _refuse("projection_active_set_member_invalid", "Scenario criterion identity is invalid or duplicated.")
        for node_type, node_id, section in (("TestScenario", edge.from_id, "test_scenario"), ("Criterion", edge.to_id, "ac")):
            node = scope._node_snapshot(node_type, node_id)
            if node is None or not is_spec_child_reference(node.get("source_artifact_ref"), owner_id=intent.owner_id, section=section):
                _refuse("projection_active_set_member_invalid", "Scenario criterion endpoint belongs to another source.")
        desired.add((edge.from_id, edge.to_id, edge.rule_id))
        endpoints.add((edge.from_id, edge.to_id))

    physical, definition = scope._relationship_definition("tests", "TestScenario", "Criterion")
    properties = scope._projection_edge_properties(definition)
    projection = ", ".join(f"r.{name}" for name in properties)

    def owned():
        rows = scope._query(
            f"MATCH (a:TestScenario)-[r:{physical}]->(b:Criterion) "
            "WHERE a.source_artifact_ref STARTS WITH $prefix "
            f"RETURN a.id, b.id, a.source_artifact_ref, b.source_artifact_ref, {projection}",
            {"prefix": f"spec:{intent.owner_id}:test_scenario:"}, operation="projection_scenario_criteria_read",
        ).rows
        result = {}
        for row in rows:
            attrs = {name: normalize_query_value(row[index + 4]) for index, name in enumerate(properties)}
            if not owns_scenario_criterion_edge(owner_id=intent.owner_id, source_ref=row[2], target_ref=row[3],
                    rule_id=attrs.get("rule_id"), layer=attrs.get("layer"), created_by=attrs.get("created_by")):
                continue
            key = (str(row[0]), str(row[1]), attrs["rule_id"])
            if key in result:
                _refuse("projection_active_set_source_ref_ambiguous", "An owned scenario edge has multiple physical copies.")
            result[key] = ProjectionEdgeBeforeImage("tests", "TestScenario", "Criterion", key[0], key[1], attrs)
        return result

    current = owned()
    if desired.difference(current):
        _refuse("projection_active_set_member_missing", "An active scenario edge is missing or untrusted.")
    stale = tuple(edge for key, edge in current.items() if key not in desired)
    receipt = ProjectionActiveSetReceipt(intent=intent, edge_before_images=stale)
    try:
        for edge in stale:
            scope._mutation(
                f"MATCH (a:TestScenario)-[r:{physical}]->(b:Criterion) "
                "WHERE a.id=$source AND b.id=$target AND r.rule_id=$rule "
                "AND r.layer=$layer AND r.created_by=$writer DELETE r",
                {"source": edge.from_id, "target": edge.to_id, "rule": edge.attrs["rule_id"],
                 "layer": edge.attrs["layer"], "writer": edge.attrs["created_by"]},
                operation="delete_projection_scenario_criterion_edge",
            )
        if set(owned()) != desired:
            _refuse("projection_stale_edge_cleanup_unconfirmed", "Scenario criterion active set did not converge.")
    except BaseException as error:
        scope._projection_apply_failure(receipt, error, operation="reconcile_scenario_criteria")
    return receipt
