"""Closed Pulse 0.5.0 schema manifest for the inactive Grafx adapter.

The Core contract owns logical names and relationship ordering.  The existing
Community DDL remains the authority for physical node-column order.  This
module renders both into the model Grafx needs: one relationship table per
endpoint pair and one embedding space per node table.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable
from okto_pulse.core.kg.schema_contract import (
    NODE_TYPES,
    SCHEMA_VERSION,
    STABLE_NODE_PROPERTIES,
    VECTOR_INDEX_TYPES,
    vector_index_name,
)

from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)
from okto_pulse.community.adapters.graph_ddl import (
    COMMON_NODE_COLUMNS,
    COMMON_REL_COLUMNS,
    NODE_PRIMARY_KEY,
)

EMBEDDING_DIMENSION = 384
EMBEDDING_METRIC = "cosine"
EMBEDDING_NORMALIZED = False
EMBEDDING_STORAGE_DTYPE = "float64"

BOARD_META_COLUMNS: tuple[tuple[str, str], ...] = (
    ("board_id", "STRING"),
    ("schema_version", "STRING"),
    ("bootstrapped_at", "TIMESTAMP"),
    ("embedding_model", "STRING"),
    ("embedding_dimension", "INT64"),
)
BOARD_META_PRIMARY_KEY = "board_id"

_GRAFX_VALUE_TYPES = {
    "BOOLEAN": "BOOL",
    "DOUBLE[384]": "VECTOR_F64",
}


@dataclass(frozen=True, slots=True)
class GrafxColumnManifest:
    """One exact catalog column expected by the Pulse adapter."""

    name: str
    pulse_type: str
    nullable: bool
    vector_space: str | None = None

    @property
    def grafx_value_type(self) -> str:
        return _GRAFX_VALUE_TYPES.get(self.pulse_type, self.pulse_type)

    def ddl(self) -> str:
        if self.pulse_type == "DOUBLE[384]":
            return f"{self.name} VECTOR({self.vector_space})"
        return f"{self.name} {self.pulse_type}"

    def descriptor(self) -> dict[str, object]:
        logical_type = "VECTOR" if self.pulse_type == "DOUBLE[384]" else self.pulse_type
        return {
            "name": self.name,
            "type": logical_type,
            "nullable": self.nullable,
            "space": self.vector_space,
        }


@dataclass(frozen=True, slots=True)
class GrafxSpaceManifest:
    """One physical vector space owned by one logical Pulse node type."""

    node_type: str
    name: str
    dimension: int = EMBEDDING_DIMENSION
    metric: str = EMBEDDING_METRIC
    normalized: bool = EMBEDDING_NORMALIZED
    storage_dtype: str = EMBEDDING_STORAGE_DTYPE

    def ddl(self) -> str:
        normalized = "true" if self.normalized else "false"
        return (
            f"CREATE VECTOR SPACE {self.name} "
            f"{{dimension: {self.dimension}, metric: '{self.metric}', "
            f"normalized: {normalized}, storage_dtype: '{self.storage_dtype}'}}"
        )

    def descriptor(self) -> dict[str, object]:
        return {
            "node_type": self.node_type,
            "name": self.name,
            "dimension": self.dimension,
            "metric": self.metric,
            "normalized": self.normalized,
            "storage_dtype": self.storage_dtype,
            "searchable": self.node_type in VECTOR_INDEX_TYPES,
        }


@dataclass(frozen=True, slots=True)
class GrafxTableManifest:
    """One exact physical table shape and the DDL that creates it."""

    name: str
    kind: str
    columns: tuple[GrafxColumnManifest, ...]
    primary_key: str | None = None
    from_table: str | None = None
    to_table: str | None = None
    logical_relationship: str | None = None

    def ddl(self) -> str:
        if self.kind == "node":
            columns = ", ".join(column.ddl() for column in self.columns)
            primary = f", PRIMARY KEY({self.primary_key})" if self.primary_key else ""
            return f"CREATE NODE TABLE {self.name}({columns}{primary})"
        properties = ", ".join(column.ddl() for column in self.columns[2:])
        suffix = f", {properties}" if properties else ""
        return (
            f"CREATE REL TABLE {self.name}"
            f"(FROM {self.from_table} TO {self.to_table}{suffix})"
        )


@dataclass(frozen=True, slots=True)
class GrafxSchemaManifest:
    """The immutable physical and logical authority used by bootstrap."""

    schema_version: str
    spaces: tuple[GrafxSpaceManifest, ...]
    board_meta: GrafxTableManifest
    nodes: tuple[GrafxTableManifest, ...]
    relationships: tuple[GrafxTableManifest, ...]
    logical_descriptor_json: str
    logical_fingerprint: str

    @property
    def tables(self) -> tuple[GrafxTableManifest, ...]:
        return (self.board_meta, *self.nodes, *self.relationships)

    @property
    def logical_descriptor(self) -> dict[str, object]:
        """Return a detached descriptor so the manifest itself stays immutable."""

        return json.loads(self.logical_descriptor_json)


def _column(
    name: str,
    pulse_type: str,
    *,
    nullable: bool,
    vector_space: str | None = None,
) -> GrafxColumnManifest:
    return GrafxColumnManifest(
        name=name,
        pulse_type=pulse_type,
        nullable=nullable,
        vector_space=vector_space,
    )


def _fail_authority(reason: str, **details: object) -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        "The Pulse schema authorities disagree before Grafx bootstrap.",
        details={
            "backend": "okto_grafx",
            "operation": "grafx_schema_manifest",
            "reason": reason,
            **details,
        },
    )


def _build_manifest() -> GrafxSchemaManifest:
    node_property_names = tuple(name for name, _data_type in COMMON_NODE_COLUMNS)
    expected_property_names = (*STABLE_NODE_PROPERTIES, "embedding")
    if len(node_property_names) != len(set(node_property_names)) or set(
        node_property_names
    ) != set(expected_property_names):
        raise _fail_authority(
            "node_column_authority_mismatch",
            expected=tuple(sorted(expected_property_names)),
            observed=node_property_names,
        )

    spaces = tuple(
        GrafxSpaceManifest(node_type=node_type, name=vector_index_name(node_type))
        for node_type in NODE_TYPES
    )
    space_by_node = {space.node_type: space.name for space in spaces}
    nodes = tuple(
        GrafxTableManifest(
            name=node_type,
            kind="node",
            columns=tuple(
                _column(
                    name,
                    pulse_type,
                    nullable=name != NODE_PRIMARY_KEY,
                    vector_space=space_by_node[node_type]
                    if pulse_type == "DOUBLE[384]"
                    else None,
                )
                for name, pulse_type in COMMON_NODE_COLUMNS
            ),
            primary_key=NODE_PRIMARY_KEY,
        )
        for node_type in NODE_TYPES
    )

    board_meta = GrafxTableManifest(
        name="BoardMeta",
        kind="node",
        columns=tuple(
            _column(
                name,
                pulse_type,
                nullable=name != BOARD_META_PRIMARY_KEY,
            )
            for name, pulse_type in BOARD_META_COLUMNS
        ),
        primary_key=BOARD_META_PRIMARY_KEY,
    )

    endpoint_columns = (
        _column("_from", "INT64", nullable=False),
        _column("_to", "INT64", nullable=False),
    )
    relationship_properties = tuple(
        _column(name, pulse_type, nullable=True)
        for name, pulse_type in COMMON_REL_COLUMNS
    )
    relationships = tuple(
        GrafxTableManifest(
            name=entry.physical_table,
            kind="rel",
            columns=(*endpoint_columns, *relationship_properties),
            from_table=entry.from_type,
            to_table=entry.to_type,
            logical_relationship=entry.logical_type,
        )
        for entry in PULSE_RELATIONSHIP_LAYOUT.entries
    )

    logical_descriptor: dict[str, object] = {
        "contract": "okto-pulse-board-schema",
        "schema_version": SCHEMA_VERSION,
        "nodes": [
            {
                "name": table.name,
                "primary_key": table.primary_key,
                "columns": [column.descriptor() for column in table.columns],
            }
            for table in nodes
        ],
        "board_meta": {
            "name": board_meta.name,
            "primary_key": board_meta.primary_key,
            "columns": [column.descriptor() for column in board_meta.columns],
        },
        "relationships": [
            {
                "name": definition.name,
                "endpoint_pairs": [list(pair) for pair in definition.endpoint_pairs],
                "columns": [column.descriptor() for column in relationship_properties],
            }
            for definition in PULSE_RELATIONSHIP_LAYOUT.logical_definitions
        ],
        "spaces": [space.descriptor() for space in spaces],
    }
    descriptor_json = json.dumps(
        logical_descriptor,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest()
    return GrafxSchemaManifest(
        schema_version=SCHEMA_VERSION,
        spaces=spaces,
        board_meta=board_meta,
        nodes=nodes,
        relationships=relationships,
        logical_descriptor_json=descriptor_json,
        logical_fingerprint=fingerprint,
    )


PULSE_GRAFX_SCHEMA_MANIFEST = _build_manifest()


__all__ = [
    "BOARD_META_COLUMNS",
    "BOARD_META_PRIMARY_KEY",
    "EMBEDDING_DIMENSION",
    "EMBEDDING_METRIC",
    "EMBEDDING_NORMALIZED",
    "EMBEDDING_STORAGE_DTYPE",
    "PULSE_GRAFX_SCHEMA_MANIFEST",
    "GrafxColumnManifest",
    "GrafxSchemaManifest",
    "GrafxSpaceManifest",
    "GrafxTableManifest",
]
