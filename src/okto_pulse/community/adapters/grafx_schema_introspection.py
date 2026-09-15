"""Read-only Pulse node-property introspection over the public Grafx catalog.

This M-PULSE-3A helper deliberately covers only ``list_node_properties``.
Logical relationship objects, schema versions, indexes, and provider composition
remain owned by later M-PULSE-3/M-PULSE-6 slices.
"""

from __future__ import annotations

from okto_grafx import Database
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.schema_contract import NODE_TYPES

from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error


def list_node_properties(database: Database, node_type: str) -> tuple[str, ...]:
    """Return catalog-ordered properties for one stable Pulse node type.

    Types outside Pulse's closed node vocabulary match the existing Kuzu
    adapter by returning an empty tuple without consulting the backend.  A
    known Pulse type whose table is absent or is not a node table is schema
    divergence and therefore fails closed.
    """

    if node_type not in NODE_TYPES:
        return ()

    try:
        catalog = database.catalog.catalog
        definition = catalog.table(node_type)
    except Exception as exc:
        mapped = map_grafx_error(exc, operation="list_node_properties")
        raise mapped from exc

    if definition.kind != "node":
        message = f"Grafx table {node_type!r} is not a node table."
        raise GraphCapabilityUnavailable(
            message,
            details={
                "backend": "okto_grafx",
                "operation": "list_node_properties",
                "node_type": node_type,
                "table_kind": definition.kind,
            },
        )

    return tuple(column.name for column in definition.columns)


__all__ = ["list_node_properties"]
