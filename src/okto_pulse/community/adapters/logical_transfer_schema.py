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
from collections.abc import Iterable, Sequence
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
from okto_pulse.core.kg.schema_contract import (
    NODE_TYPES,
    VECTOR_INDEX_TYPES,
    vector_index_name,
)

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


@dataclass(frozen=True, slots=True)
class _Column:
    """One parsed DDL column, including whether it carries the key marker."""

    name: str
    pulse_type: str
    primary_key: bool


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
#
# Written as literals, deliberately. Computing them from the same authorities
# the derivation reads would make expectation and authority move together, so a
# column added upstream would satisfy a census that had silently followed it --
# which is precisely the drift this is here to catch.
BOARD_CENSUS: Final[SchemaCensus] = SchemaCensus(
    node_types=12,
    relation_layouts=69,
    vector_spaces=11,
    node_property_defs=489,
    relation_property_defs=483,
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


@dataclass(frozen=True, slots=True)
class SearchableIndex:
    """One vector index the scope's authority says must physically exist."""

    table: str
    index_name: str
    column: str
    metric: str


# Board declares 11 embedding spaces but only 9 of them are searchable: the
# authority is VECTOR_INDEX_TYPES, and Alternative and Assumption are not in it.
# A space is a column with a geometry; an index is a searchable structure over
# it, and inferring one from the other would silently give two Board types an
# HNSW the product never asked for.
BOARD_SEARCHABLE_TYPES: Final[tuple[str, ...]] = tuple(VECTOR_INDEX_TYPES)


def searchable_indexes(schema: LogicalSchema) -> tuple[SearchableIndex, ...]:
    """Return exactly the indexes this scope's authority requires."""

    metrics = {space.name: space.metric for space in schema.vector_spaces}
    if schema.scope == "board":
        entries = tuple(
            SearchableIndex(
                table=node_type,
                index_name=vector_index_name(node_type),
                column="embedding",
                metric=metrics[vector_index_name(node_type)],
            )
            for node_type in BOARD_SEARCHABLE_TYPES
        )
        if len(entries) != 9:
            raise SchemaDerivationError(
                f"Board expects 9 searchable indexes, derived {len(entries)}"
            )
        return entries
    entries = tuple(
        SearchableIndex(
            table=table, index_name=index, column=column, metric=metrics[index]
        )
        for table, index, column in VECTOR_INDEXES
    )
    if len(entries) != 4:
        raise SchemaDerivationError(
            f"Global expects 4 searchable indexes, derived {len(entries)}"
        )
    return entries


# SHOW_INDEXES row: table_name, index_name, index_type, property_names,
# extension_loaded, index_definition. The definition inlines the metric, which
# is the only topology parameter that belongs to the logical artefact.
_INDEX_METRIC: Final[re.Pattern[str]] = re.compile(r"metric\s*:=\s*'([^']*)'")


def index_gate_failures(
    schema: LogicalSchema, rows: Iterable[Sequence[object]]
) -> tuple[str, ...]:
    """Judge SHOW_INDEXES output against this scope's searchable authority.

    Returns every disagreement rather than the first, so a caller reports what
    is actually wrong with the database instead of one arbitrary symptom.
    Comparing only (table, index_name) would accept an HNSW built over the
    wrong column, or with a metric the schema never declared, under a name that
    happens to match -- so type, indexed property and metric are all checked.
    `extension_loaded` and the HNSW topology knobs (mu, ml, pu, alpha, efc) are
    deliberately ignored: the first is false on a cold database that was never
    loaded, and the rest are engine tuning, not part of the transferred graph.
    """

    expected = {
        (entry.table, entry.index_name): entry for entry in searchable_indexes(schema)
    }
    if not expected:
        return ()
    observed: dict[tuple[str, str], tuple[str, tuple[str, ...], str, bool]] = {}
    for row in rows:
        cells = list(row)
        if len(cells) < 6:
            continue
        names = cells[3]
        properties = (
            tuple(str(name) for name in names)
            if isinstance(names, (list, tuple))
            else (str(names),)
        )
        found = _INDEX_METRIC.search(str(cells[5]))
        observed[(str(cells[0]), str(cells[1]))] = (
            str(cells[2]),
            properties,
            found.group(1) if found else "",
            bool(cells[4]),
        )
    failures: list[str] = []
    # An index the authority never asked for is as wrong as a missing one: it
    # is a searchable structure the scope does not declare, and walking only
    # `expected` would let a tenth Board HNSW ride along unnoticed. Only HNSW
    # rows are judged -- whatever else the engine keeps in its catalog is not
    # part of the transferred artefact.
    for key, (index_type, _properties, _metric, _loaded) in sorted(observed.items()):
        if index_type == "HNSW" and key not in expected:
            failures.append(f"{key[0]}.{key[1]}: index is not declared by this scope")
    for key, entry in sorted(expected.items()):
        seen = observed.get(key)
        if seen is None:
            failures.append(f"{key[0]}.{key[1]}: no such index")
            continue
        index_type, properties, metric, loaded = seen
        if index_type != "HNSW":
            failures.append(
                f"{key[0]}.{key[1]}: type is {index_type!r}, expected 'HNSW'"
            )
        if properties != (entry.column,):
            failures.append(
                f"{key[0]}.{key[1]}: indexes {list(properties)}, expected [{entry.column!r}]"
            )
        if not loaded:
            # Measured on Ladybug 0.16: without the vector extension loaded,
            # SHOW_INDEXES reports extension_loaded False and index_definition
            # as the empty string. The metric is not absent from the database
            # then -- it is merely invisible from this handle, and certifying
            # would claim a geometry nobody read. The flag is checked as well
            # as the metric, so a blank definition can never be mistaken for
            # agreement with a scope that happened to declare no metric.
            failures.append(
                f"{key[0]}.{key[1]}: the vector extension is not loaded on this "
                "handle, so its metric is not proved"
            )
        elif not metric:
            failures.append(f"{key[0]}.{key[1]}: the index definition names no metric")
        elif metric != entry.metric:
            failures.append(
                f"{key[0]}.{key[1]}: metric is {metric!r}, expected {entry.metric!r}"
            )
    return tuple(failures)


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
    if len(spaces_by_type) != len(VECTOR_INDEXES):
        raise SchemaDerivationError("VECTOR_INDEXES declares a node type twice")
    unused = set(spaces_by_type)
    node_types: list[LogicalNodeType] = []
    for ddl in NODE_DDL:
        name, columns = _parse_node_table(ddl)
        declared = spaces_by_type.get(name)
        unused.discard(name)
        key = _primary_key(columns, name)
        # The declared property name is the authority for WHICH column is the
        # vector, not "whichever column happens to be DOUBLE[384]". Ignoring it
        # would silently accept a renamed column, or map a second vector onto
        # the first one's space.
        vector_columns = [
            column.name
            for column in columns
            if column.pulse_type == _VECTOR_COLUMN_TYPE
        ]
        expected = declared[1] if declared else None
        if expected is None:
            if vector_columns:
                raise SchemaDerivationError(
                    f"{name} has vector columns but no VECTOR_INDEXES entry: "
                    f"{','.join(vector_columns)}"
                )
        elif vector_columns != [expected]:
            raise SchemaDerivationError(
                f"{name} declares vector columns {vector_columns} but "
                f"VECTOR_INDEXES names {expected!r}"
            )
        node_types.append(
            LogicalNodeType(
                name=name,
                key=key,
                properties=tuple(
                    _property(
                        column.name,
                        column.pulse_type,
                        key=key,
                        vector_space=declared[0] if declared else None,
                    )
                    for column in columns
                ),
            )
        )
    if unused:
        raise SchemaDerivationError(
            f"VECTOR_INDEXES names node types with no DDL: {','.join(sorted(unused))}"
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


def _parse_node_table(ddl: str) -> tuple[str, tuple[_Column, ...]]:
    """Parse one node DDL, KEEPING the inline PRIMARY KEY marker.

    The marker is the whole point.  Dropping it and taking the first column
    would mean the derived key is "whichever column happens to be written
    first", so an authority that moved or removed its PRIMARY KEY while keeping
    the column count would still satisfy the census and derive a schema keyed
    on the wrong column.
    """

    match = _NODE_TABLE.search(ddl)
    if match is None:
        raise SchemaDerivationError("node DDL is not in the expected shape")
    name = match.group(1)
    columns: list[_Column] = []
    for raw in match.group(2).split(","):
        column = " ".join(raw.split())
        if not column:
            continue
        parts = column.split()
        if len(parts) < 2:
            raise SchemaDerivationError(
                f"column {column!r} of {name} is not in the expected shape"
            )
        modifiers = " ".join(parts[2:]).upper()
        columns.append(_Column(parts[0], parts[1], "PRIMARY KEY" in modifiers))
    return name, tuple(columns)


def _primary_key(columns: tuple[_Column, ...], table: str) -> str:
    """Return the column the DDL actually marks PRIMARY KEY, and only that."""

    if not columns:
        raise SchemaDerivationError(f"{table} declares no columns")
    keys = [column.name for column in columns if column.primary_key]
    if not keys:
        raise SchemaDerivationError(f"{table} declares no PRIMARY KEY")
    if len(keys) > 1:
        raise SchemaDerivationError(
            f"{table} declares more than one PRIMARY KEY: {','.join(keys)}"
        )
    return keys[0]


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
    "BOARD_SEARCHABLE_TYPES",
    "SchemaCensus",
    "SchemaDerivationError",
    "SearchableIndex",
    "index_gate_failures",
    "searchable_indexes",
    "board_logical_schema",
    "global_logical_schema",
    "require_no_schema_drift",
]
