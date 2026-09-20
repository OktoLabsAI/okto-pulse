"""Internal directed removal of a retained Board projection plan.

Caller owns the verified original recovery set, Board routing, offline writer
window and durable receipt. This is not runtime admission or a global cutover.
"""

from contextlib import ExitStack
from dataclasses import dataclass

from okto_pulse.core.kg.logical_transfer import LogicalNode, LogicalRelation, canonical_bytes, count_graph, encode_value
from okto_pulse.core.ports.retirement_graph import (
    GraphRetirementPlan, graph_retirement_fingerprint, plan_sprint_graph_retirement,
    verify_graph_retirement_selection,
)
from .logical_transfer_factories import logical_transfer_scope, make_grafx_logical_source
from .logical_transfer_grafx import _logical_value, _validate_physical_schema

_MAX_RECORDS = 100_000
_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class GraphRetirementReceipt:
    plan: GraphRetirementPlan


def prepare_sprint_graph_retirement(database, *, board_id, archived_origin_ids):
    source = make_grafx_logical_source(database, scope="board")
    snapshot = source.open_snapshot()
    try:
        return plan_sprint_graph_retirement(snapshot, board_id=board_id, archived_origin_ids=archived_origin_ids)
    finally:
        snapshot.close()


class _TransactionSnapshot:
    """Closed logical view from the write transaction's supported query API.

    Grafx scan_rows_v1 is read-only. Query this same write transaction so the
    second view includes our uncommitted deletes. No graph is reconstructed.
    """

    def __init__(self, database, transaction, *, scope="board"):
        contract = logical_transfer_scope(scope)
        self._schema = contract.schema
        catalog = database.catalog.catalog
        _validate_physical_schema(catalog, contract.schema, contract.relationship_tables)
        self._nodes, self._relations = [], []
        consumed = size = 0

        def properties(native, definitions):
            nonlocal consumed, size
            consumed += 1
            result = {prop.name: _logical_value(native.properties[prop.name], prop, catalog) for prop in definitions}
            size += len(canonical_bytes({key: encode_value(value) for key, value in result.items()}))
            if consumed > _MAX_RECORDS or size > _MAX_BYTES:
                raise ValueError("retirement_graph_record_limit")
            return result

        for node_type in self._schema.node_types:
            # Names come only from the validated closed physical schema.
            rows = transaction.execute(f"MATCH (n:{node_type.name}) RETURN n LIMIT $cap",
                {"cap": _MAX_RECORDS - consumed + 1}).rows
            for (native,) in rows:
                values = properties(native, node_type.properties)
                self._nodes.append(LogicalNode(node_type.name, values[node_type.key], values))
        for layout in self._schema.relation_layouts:
            physical = contract.relationship_tables[layout.identity]
            source_key = self._schema.node_type(layout.source_type).key
            target_key = self._schema.node_type(layout.target_type).key
            rows = transaction.execute(f"MATCH (a:{layout.source_type})-[r:{physical}]->(b:{layout.target_type}) "
                f"RETURN a.{source_key},b.{target_key},r LIMIT $cap", {"cap": _MAX_RECORDS - consumed + 1}).rows
            for source, target, native in rows:
                self._relations.append(LogicalRelation(layout.name, layout.source_type, layout.target_type,
                    source, target, properties(native, layout.properties)))

    def schema(self):
        return self._schema

    def counts(self):
        return count_graph(self._nodes, self._relations)

    def iter_nodes(self, *, batch_size):
        for offset in range(0, len(self._nodes), batch_size):
            yield self._nodes[offset:offset + batch_size]

    def iter_relations(self, *, batch_size):
        for offset in range(0, len(self._relations), batch_size):
            yield self._relations[offset:offset + batch_size]


def _remove(transaction, key):
    # The public plan permits exactly Entity/Criterion table names, never
    # caller-supplied Cypher. Values remain bound parameters.
    node_type, node_id = key
    transaction.execute(f"MATCH (n:{node_type}) WHERE n.id=$identity DETACH DELETE n", {"identity": node_id})


def apply_sprint_graph_retirement(database, plan: GraphRetirementPlan) -> GraphRetirementReceipt:
    """Commit only the planned node/incident-edge difference, or verify replay.

    Before/after checks read this write transaction. Native optimistic conflict
    refusal is propagated; no retry selects a different source population.
    Raw writers are not excluded by owning this transaction. The enclosing
    coordinator still owns exclusion, original backup and durable completion.
    """
    if not isinstance(plan, GraphRetirementPlan):
        raise ValueError("retirement_graph_plan_invalid")
    transaction = database.begin("write")
    with ExitStack() as cleanup:
        cleanup.callback(lambda: transaction.rollback() if transaction.active else None)
        before = _TransactionSnapshot(database, transaction)
        fingerprint = graph_retirement_fingerprint(before)
        if fingerprint == plan.after_sha256:
            return GraphRetirementReceipt(plan)
        if fingerprint != plan.before_sha256:
            raise ValueError("retirement_graph_before_mismatch")
        verify_graph_retirement_selection(before, plan)
        for key in plan.node_keys:
            _remove(transaction, key)
        after = _TransactionSnapshot(database, transaction)
        if graph_retirement_fingerprint(after) != plan.after_sha256:
            raise ValueError("retirement_graph_after_mismatch")
        transaction.commit()
    return GraphRetirementReceipt(plan)
