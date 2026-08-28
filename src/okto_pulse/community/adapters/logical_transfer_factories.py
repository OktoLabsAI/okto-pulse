"""The four ways a Pulse scope becomes a logical transfer endpoint.

A transfer needs the two ends to agree about far more than a schema: which
physical table stores each relationship layout, and what the database file on
disk is called.  Until now every caller assembled that agreement itself, from
whichever authority happened to be in scope -- which is exactly how a Board
source and a Board sink come to disagree about one of sixty-nine tables and
find out during a cutover.

So the agreement is named once, per scope, and both backends are handed the
same one.  There are exactly two scopes and no way to invent a third: an
unknown name is refused rather than defaulted, and a scope whose map has
drifted from its schema is refused too, because a partial map does not produce
a partial transfer -- it produces a confident, wrong one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from okto_pulse.core.kg.logical_transfer import (
    LayoutIdentity,
    LogicalSchema,
    LogicalSchemaError,
)

from okto_pulse.community.adapters.grafx_global_discovery import (
    PULSE_GRAFX_GLOBAL_SCHEMA,
)
from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)
from okto_pulse.community.adapters.logical_transfer_schema import (
    board_logical_schema,
    global_logical_schema,
)

SCOPE_BOARD: Final[str] = "board"
SCOPE_GLOBAL_DISCOVERY: Final[str] = "global_discovery"
SCOPES: Final[tuple[str, ...]] = (SCOPE_BOARD, SCOPE_GLOBAL_DISCOVERY)

# Frozen, and deliberately not derived from the maps they police: computing the
# expectation from the authority would make the two move together, so a layout
# added upstream would satisfy a count that had silently followed it.
BOARD_RELATIONSHIP_TABLES: Final[int] = 69
GLOBAL_RELATIONSHIP_TABLES: Final[int] = 7

_DEFAULT_SCAN_BATCH_SIZE: Final[int] = 500
_DEFAULT_MAX_BATCH_SIZE: Final[int] = 500


@dataclass(frozen=True, slots=True)
class LogicalTransferScope:
    """One Pulse scope, and everything both backends must agree about it."""

    name: str
    schema: LogicalSchema
    relationship_tables: Mapping[LayoutIdentity, str]
    ladybug_filename: str


def logical_transfer_scope(scope: str) -> LogicalTransferScope:
    """Resolve one scope's contract, or refuse.

    Two scopes exist.  A name that is neither is refused instead of falling
    back to one of them, because a transfer that quietly ran against the wrong
    scope would succeed, certify, and be wrong.
    """

    if not isinstance(scope, str) or scope not in SCOPES:
        raise LogicalSchemaError(
            "unknown logical transfer scope",
            detail=f"{scope!r} is not one of {SCOPES}",
        )
    if scope == SCOPE_BOARD:
        from okto_pulse.community.adapters.kg_runtime import GRAPH_DB_FILENAME

        schema = board_logical_schema()
        tables: dict[LayoutIdentity, str] = {
            (entry.logical_type, entry.from_type, entry.to_type): entry.physical_table
            for entry in PULSE_RELATIONSHIP_LAYOUT.entries
        }
        expected = BOARD_RELATIONSHIP_TABLES
        filename = GRAPH_DB_FILENAME
    else:
        from okto_pulse.community.adapters.global_discovery_runtime import (
            GLOBAL_DISCOVERY_FILENAME,
        )

        schema = global_logical_schema()
        tables = {
            (
                relation.logical_relationship,
                relation.from_table,
                relation.to_table,
            ): relation.name
            for relation in PULSE_GRAFX_GLOBAL_SCHEMA.relationships
        }
        expected = GLOBAL_RELATIONSHIP_TABLES
        filename = GLOBAL_DISCOVERY_FILENAME

    _require_no_layout_drift(scope, schema, tables, expected)
    return LogicalTransferScope(
        name=scope,
        schema=schema,
        relationship_tables=tables,
        ladybug_filename=filename,
    )


def _require_no_layout_drift(
    scope: str,
    schema: LogicalSchema,
    tables: Mapping[LayoutIdentity, str],
    expected: int,
) -> None:
    """Refuse a map that is not exactly the schema's set of layouts.

    Both directions matter.  A layout with no table is a relation the transfer
    could not store; a table with no layout is storage the transfer would never
    read, and either one turns a complete-looking transfer into a lossy one.
    """

    if len(tables) != expected:
        raise LogicalSchemaError(
            "relationship table map has drifted from the frozen census",
            detail=f"{scope}: {len(tables)} tables, expected {expected}",
        )
    declared = {layout.identity for layout in schema.relation_layouts}
    mapped = set(tables)
    if mapped != declared:
        raise LogicalSchemaError(
            "relationship table map does not match the schema's layouts",
            detail=(
                f"{scope}: unmapped={sorted(declared - mapped)} "
                f"unknown={sorted(mapped - declared)}"
            ),
        )
    empty = sorted(identity for identity, table in tables.items() if not table)
    if empty:
        raise LogicalSchemaError(
            "relationship table map names an empty physical table",
            detail=f"{scope}: {empty}",
        )


def make_ladybug_logical_source(database: Any, *, scope: str) -> Any:
    """Read one Ladybug database as this scope's logical graph."""

    from okto_pulse.community.adapters.ladybug_logical_source import (
        LadybugLogicalSnapshotSource,
    )

    contract = logical_transfer_scope(scope)
    return LadybugLogicalSnapshotSource(database, contract.schema)


def make_ladybug_logical_sink(candidate_root: str | Path, *, scope: str) -> Any:
    """Write this scope's logical graph into a new Ladybug candidate.

    The filename comes from the scope, not from this call: a Board runtime
    resolves graph.lbug and a Global generation discovery.lbug, and a candidate
    under any other name is one no runtime would ever find.
    """

    from okto_pulse.community.adapters.ladybug_logical_sink import (
        LadybugLogicalCandidateSink,
    )

    contract = logical_transfer_scope(scope)
    return LadybugLogicalCandidateSink(
        candidate_root,
        contract.schema,
        database_filename=contract.ladybug_filename,
    )


def make_grafx_logical_source(
    database: Any,
    *,
    scope: str,
    scan_batch_size: int = _DEFAULT_SCAN_BATCH_SIZE,
    temporary_parent: Path | None = None,
) -> Any:
    """Read one Grafx database as this scope's logical graph."""

    from okto_pulse.community.adapters.logical_transfer_grafx import (
        CommunityGrafxLogicalSnapshotSource,
    )

    contract = logical_transfer_scope(scope)
    return CommunityGrafxLogicalSnapshotSource(
        database,
        schema=contract.schema,
        relationship_tables=contract.relationship_tables,
        scan_batch_size=scan_batch_size,
        temporary_parent=temporary_parent,
    )


def make_grafx_logical_sink(
    candidate_path: str | Path,
    *,
    scope: str,
    max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
    connect_options: Mapping[str, object] | None = None,
    temporary_parent: Path | None = None,
) -> Any:
    """Write this scope's logical graph into a new Grafx candidate."""

    from okto_pulse.community.adapters.grafx_logical_sink import (
        CommunityGrafxLogicalCandidateSink,
    )

    contract = logical_transfer_scope(scope)
    return CommunityGrafxLogicalCandidateSink(
        candidate_path,
        expected_schema=contract.schema,
        relationship_tables=contract.relationship_tables,
        max_batch_size=max_batch_size,
        connect_options=connect_options,
        temporary_parent=temporary_parent,
    )


__all__ = [
    "BOARD_RELATIONSHIP_TABLES",
    "GLOBAL_RELATIONSHIP_TABLES",
    "SCOPES",
    "SCOPE_BOARD",
    "SCOPE_GLOBAL_DISCOVERY",
    "LogicalTransferScope",
    "logical_transfer_scope",
    "make_grafx_logical_sink",
    "make_grafx_logical_source",
    "make_ladybug_logical_sink",
    "make_ladybug_logical_source",
]
