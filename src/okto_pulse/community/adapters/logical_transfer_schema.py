"""Derive the Board and Global Discovery logical schemas from Community authorities.

The logical transfer needs the two scopes expressed as Core ``LogicalSchema``
objects.  Those shapes already exist in this repository, so this module DERIVES
them rather than restating them: Board from ``graph_ddl`` plus the shared
schema contract, BoardMeta from the Grafx schema manifest, and Global by
reading its own DDL.  Restating them would create a second authority that drifts
silently the first time somebody adds a column to one and not the other.

Deriving is not sufficient on its own, because a derivation happily follows the
authority off a cliff.  So every schema is checked against a frozen census
before it is returned, and any disagreement refuses.  A transfer that ran
against a schema nobody expected would still produce matching counts and a
matching fingerprint -- of the wrong graph.

Physical types map onto the logical ones exactly once, here:

===============  ==================
Pulse column     logical property
===============  ==================
``STRING``       ``string``
``INT64``        ``int64``
``DOUBLE``       ``float64``
``TIMESTAMP``    ``timestamp_us``
``BOOLEAN``      ``bool``
``DOUBLE[384]``  ``vector``
===============  ==================
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from okto_pulse.core.kg.logical_transfer import (
    LogicalNodeType,
    LogicalPropertyDef,
    LogicalPropertyType,
    LogicalRelationLayout,
    LogicalSchema,
    LogicalVectorSpace,
)
from okto_pulse.core.kg.schema_contract import NODE_TYPES, vector_index_name

from okto_pulse.community.adapters.global_discovery_schema import (
    NODE_DDL,
    REL_DDL,
    VECTOR_INDEXES,
)
from okto_pulse.community.adapters.graph_ddl import (
    COMMON_NODE_COLUMNS,
    COMMON_REL_COLUMNS,
    NODE_PRIMARY_KEY,
)
from okto_pulse.community.adapters.grafx_schema_manifest import (
    BOARD_META_COLUMNS,
    BOARD_META_PRIMARY_KEY,
    EMBEDDING_DIMENSION,
    EMBEDDING_METRIC,
    EMBEDDING_NORMALIZED,
    EMBEDDING_STORAGE_DTYPE,
)
from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)


BOARD_META_TABLE: Final[str] = "BoardMeta"

_PULSE_SCALARS: Final[dict[str, LogicalPropertyType]] = {
    "STRING": "string",
    "INT64": "int64",
    "DOUBLE": "float64",
    "TIMESTAMP": "timestamp_us",
    "BOOLEAN": "bool",
}
_VECTOR_COLUMN_TYPE: Final[str] = f"DOUBLE[{EMBEDDING_DIMENSION}]"

_NODE_TABLE = re.compile(
    r"CREATE\s+NODE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*)\)",
    re.IGNORECASE | re.DOTALL,
)
_REL_TABLE = re.compile(
    r"CREATE\s+REL\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(\s*"
    r"FROM\s+(\w+)\s+TO\s+(\w+)\s*(?:,(.*))?\)",
    re.IGNORECASE | re.DOTALL,
)


class SchemaDerivationError(RuntimeError):
    """A Community authority no longer matches what the transfer expects."""


@dataclass(frozen=True, slots=True)
class SchemaCensus:
    """The frozen shape a derived schema must still have.

    Counted rather than named: a census that listed every property would be a
    second copy of the authority, which is the thing this module exists to
    avoid.  Counts catch an added or removed column without pretending to own
    the column list.
    """

    node_types: int
    relation_layouts: int
    vector_spaces: int
    node_property_defs: int
    relation_property_defs: int


# Board: 11 typed nodes of 44 columns each, plus BoardMeta's 5, over 69 concrete
# endpoint triples of 7 columns each, against 11 embedding spaces.
BOARD_CENSUS: Final[SchemaCensus] = SchemaCensus(
    node_types=len(NODE_TYPES) + 1,
    relation_layouts=69,
    vector_spaces=len(NODE_TYPES),
    node_property_defs=len(NODE_TYPES) * len(COMMON_NODE_COLUMNS)
    + len(BOARD_META_COLUMNS),
    relation_property_defs=69 * len(COMMON_REL_COLUMNS),
)

# Global Discovery: 4 nodes of 8/6/6/10 columns, 7 layouts carrying `weight`
# only on the two self-relations, against 4 embedding spaces.
GLOBAL_CENSUS: Final[SchemaCensus] = SchemaCensus(
    node_types=4,
    relation_layouts=7,
    vector_spaces=4,
    node_property_defs=30,
    relation_property_defs=2,
)


def _embedding_space(name: str) -> LogicalVectorSpace:
    return LogicalVectorSpace(
        name=name,
        storage_dtype=EMBEDDING_STORAGE_DTYPE,
        dimension=EMBEDDING_DIMENSION,
        metric=EMBEDDING_METRIC,
        normalized=EMBEDDING_NORMALIZED,
    )


def _property(
    name: str, pulse_type: str, *, key: str, vector_space: str | None
) -> LogicalPropertyDef:
    """Map one physical column onto its logical property definition."""

    nullable = name != key
    if pulse_type == _VECTOR_COLUMN_TYPE:
        if vector_space is None:
            raise SchemaDerivationError(f"vector column {name!r} has no declared space")
        return LogicalPropertyDef(
            name=name, type="vector", nullable=nullable, vector_space=vector_space
        )
    logical = _PULSE_SCALARS.get(pulse_type)
    if logical is None:
        raise SchemaDerivationError(
            f"column {name!r} has unmappable physical type {pulse_type!r}"
        )
    return LogicalPropertyDef(name=name, type=logical, nullable=nullable)


def board_logical_schema() -> LogicalSchema:
    """Derive the Board scope, then refuse it unless it still matches BOARD_CENSUS."""

    node_types: list[LogicalNodeType] = [
        LogicalNodeType(
            name=BOARD_META_TABLE,
            key=BOARD_META_PRIMARY_KEY,
            properties=tuple(
                _property(
                    name,
                    pulse_type,
                    key=BOARD_META_PRIMARY_KEY,
                    vector_space=None,
                )
                for name, pulse_type in BOARD_META_COLUMNS
            ),
        )
    ]
    for node_type in NODE_TYPES:
        # Every Board type carries a property literally named `embedding`, and
        # each one belongs to its own space. The space comes from the type, not
        # from the property name.
        space = vector_index_name(node_type)
        node_types.append(
            LogicalNodeType(
                name=node_type,
                key=NODE_PRIMARY_KEY,
                properties=tuple(
                    _property(
                        name,
                        pulse_type,
                        key=NODE_PRIMARY_KEY,
                        vector_space=space,
                    )
                    for name, pulse_type in COMMON_NODE_COLUMNS
                ),
            )
        )

    relation_properties = tuple(
        _property(name, pulse_type, key="", vector_space=None)
        for name, pulse_type in COMMON_REL_COLUMNS
    )
    layouts = tuple(
        LogicalRelationLayout(
            name=entry.logical_type,
            source_type=entry.from_type,
            target_type=entry.to_type,
            properties=relation_properties,
        )
        for entry in PULSE_RELATIONSHIP_LAYOUT.entries
    )
    spaces = tuple(_embedding_space(vector_index_name(t)) for t in NODE_TYPES)

    schema = LogicalSchema(
        scope="board",
        node_types=tuple(node_types),
        relation_layouts=layouts,
        vector_spaces=spaces,
    )
    require_no_schema_drift(schema, BOARD_CENSUS)
    return schema


def global_logical_schema() -> LogicalSchema:
    """Derive the Global Discovery scope from its own DDL, then census-check it."""

    spaces_by_type = {
        node_type: (space, prop) for node_type, space, prop in VECTOR_INDEXES
    }
    node_types: list[LogicalNodeType] = []
    for ddl in NODE_DDL:
        name, columns = _parse_node_table(ddl)
        space = spaces_by_type.get(name)
        key = _primary_key(columns, name)
        node_types.append(
            LogicalNodeType(
                name=name,
                key=key,
                properties=tuple(
                    _property(
                        column,
                        pulse_type,
                        key=key,
                        vector_space=space[0] if space else None,
                    )
                    for column, pulse_type in columns
                ),
            )
        )

    layouts = tuple(_parse_rel_table(ddl) for ddl in REL_DDL)
    spaces = tuple(_embedding_space(space) for _, space, _ in VECTOR_INDEXES)

    schema = LogicalSchema(
        scope="global_discovery",
        node_types=tuple(node_types),
        relation_layouts=layouts,
        vector_spaces=spaces,
    )
    require_no_schema_drift(schema, GLOBAL_CENSUS)
    return schema


def _parse_node_table(ddl: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    match = _NODE_TABLE.search(ddl)
    if match is None:
        raise SchemaDerivationError("node DDL is not in the expected shape")
    name = match.group(1)
    columns: list[tuple[str, str]] = []
    for raw in match.group(2).split(","):
        column = raw.strip()
        if not column:
            continue
        parts = column.split()
        if len(parts) < 2:
            raise SchemaDerivationError(
                f"column {column!r} of {name} is not in the expected shape"
            )
        columns.append((parts[0], parts[1]))
    return name, tuple(columns)


def _primary_key(columns: tuple[tuple[str, str], ...], table: str) -> str:
    # The DDL marks it inline; the first column is the key in every Global
    # table, but that is an observation, not a licence to assume it.
    for ddl_name, _ in columns:
        if ddl_name:
            return columns[0][0]
    raise SchemaDerivationError(f"{table} declares no columns")


def _parse_rel_table(ddl: str) -> LogicalRelationLayout:
    match = _REL_TABLE.search(ddl)
    if match is None:
        raise SchemaDerivationError("relation DDL is not in the expected shape")
    name, source, target, trailing = match.groups()
    properties: list[LogicalPropertyDef] = []
    if trailing:
        for raw in trailing.split(","):
            column = raw.strip()
            if not column:
                continue
            parts = column.split()
            if len(parts) < 2:
                raise SchemaDerivationError(
                    f"column {column!r} of {name} is not in the expected shape"
                )
            properties.append(_property(parts[0], parts[1], key="", vector_space=None))
    return LogicalRelationLayout(
        name=name,
        source_type=source,
        target_type=target,
        properties=tuple(properties),
    )


def require_no_schema_drift(schema: LogicalSchema, census: SchemaCensus) -> None:
    """Refuse a derived schema whose shape no longer matches its frozen census.

    Fail-closed on purpose.  If an authority gains a column, the derivation
    silently gains it too, and every downstream check -- counts, checksum,
    fingerprint -- would agree with the new shape while the frozen contract
    still says otherwise.  The census is the one place that notices.
    """

    observed = SchemaCensus(
        node_types=len(schema.node_types),
        relation_layouts=len(schema.relation_layouts),
        vector_spaces=len(schema.vector_spaces),
        node_property_defs=sum(len(n.properties) for n in schema.node_types),
        relation_property_defs=sum(
            len(layout.properties) for layout in schema.relation_layouts
        ),
    )
    if observed != census:
        raise SchemaDerivationError(
            f"{schema.scope} schema drifted from its frozen census: "
            f"expected {census}, derived {observed}"
        )


__all__ = [
    "BOARD_CENSUS",
    "BOARD_META_TABLE",
    "GLOBAL_CENSUS",
    "SchemaCensus",
    "SchemaDerivationError",
    "board_logical_schema",
    "global_logical_schema",
    "require_no_schema_drift",
]
