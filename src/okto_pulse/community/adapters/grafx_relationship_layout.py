"""Logical Pulse relationships materialized as single-pair Grafx tables.

Grafx deliberately gives one relationship table exactly one source and target
node table.  Pulse gives one logical relationship name several valid endpoint
pairs.  This module is the adapter-owned bijection between those two models;
neither the Grafx catalog format nor the Core schema contract needs to lie.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from okto_grafx import Database
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
)
from okto_pulse.core.kg.schema_contract import MULTI_REL_TYPES, REL_TYPES

from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_GRAFX_IDENTIFIER_BYTES = 128
_OPERATION = "logical_relationship_layout"


@dataclass(frozen=True, slots=True)
class RelationshipLayoutEntry:
    """One logical endpoint pair and its exact physical Grafx table."""

    logical_type: str
    from_type: str
    to_type: str
    physical_table: str


@dataclass(frozen=True, slots=True)
class LogicalRelationshipDefinition:
    """Backend-neutral introspection for one logical Pulse relationship."""

    name: str
    endpoint_pairs: tuple[tuple[str, str], ...]


class RelationshipLayout:
    """An immutable, collision-checked relationship layout manifest."""

    __slots__ = ("_by_key", "_entries", "_logical_definitions")

    def __init__(self, pairs: Iterable[tuple[str, str, str]]) -> None:
        entries: list[RelationshipLayoutEntry] = []
        by_key: dict[tuple[str, str, str], RelationshipLayoutEntry] = {}
        by_physical: dict[str, tuple[str, str, str]] = {}
        logical_pairs: dict[str, list[tuple[str, str]]] = {}

        for raw_logical, raw_source, raw_target in pairs:
            logical = _require_identifier("logical_type", raw_logical)
            source = _require_identifier("from_type", raw_source)
            target = _require_identifier("to_type", raw_target)
            key = (logical, source, target)
            if key in by_key:
                continue

            physical = f"{logical}__{source}__{target}"
            if len(physical.encode("ascii")) > _MAX_GRAFX_IDENTIFIER_BYTES:
                raise _layout_failure(
                    "A physical relationship table exceeds the Grafx identifier limit.",
                    reason="identifier_too_long",
                    logical_type=logical,
                    from_type=source,
                    to_type=target,
                    physical_table=physical,
                )
            previous = by_physical.get(physical)
            if previous is not None and previous != key:
                raise _layout_failure(
                    "Two logical endpoint pairs resolve to the same physical table.",
                    reason="physical_name_collision",
                    physical_table=physical,
                    first=previous,
                    second=key,
                )

            entry = RelationshipLayoutEntry(logical, source, target, physical)
            entries.append(entry)
            by_key[key] = entry
            by_physical[physical] = key
            logical_pairs.setdefault(logical, []).append((source, target))

        self._entries = tuple(entries)
        self._by_key = by_key
        self._logical_definitions = tuple(
            LogicalRelationshipDefinition(name, tuple(endpoint_pairs))
            for name, endpoint_pairs in logical_pairs.items()
        )

    @property
    def entries(self) -> tuple[RelationshipLayoutEntry, ...]:
        """Return the immutable physical manifest in Core authority order."""

        return self._entries

    @property
    def logical_definitions(self) -> tuple[LogicalRelationshipDefinition, ...]:
        """Return grouped definitions with no physical storage names."""

        return self._logical_definitions

    def resolve(self, logical_type: str, from_type: str, to_type: str) -> str:
        """Resolve one exact logical endpoint pair or fail closed."""

        logical = _require_identifier("logical_type", logical_type)
        source = _require_identifier("from_type", from_type)
        target = _require_identifier("to_type", to_type)
        entry = self._by_key.get((logical, source, target))
        if entry is None:
            raise _layout_failure(
                "The Pulse relationship authority does not declare this endpoint pair.",
                reason="unknown_endpoint_pair",
                logical_type=logical,
                from_type=source,
                to_type=target,
            )
        return entry.physical_table


def _require_identifier(field: str, value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise _layout_failure(
            "A logical relationship layout name is not a safe identifier.",
            reason="invalid_identifier",
            field=field,
            value=repr(value),
        )
    return value


def _layout_failure(
    message: str,
    *,
    reason: str,
    **details: object,
) -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        message,
        details={
            "backend": "okto_grafx",
            "operation": _OPERATION,
            "reason": reason,
            **details,
        },
    )


def _pulse_relationship_pairs() -> tuple[tuple[str, str, str], ...]:
    pairs = list(REL_TYPES)
    for logical_type, endpoint_pairs in MULTI_REL_TYPES:
        pairs.extend(
            (logical_type, from_type, to_type) for from_type, to_type in endpoint_pairs
        )
    return tuple(pairs)


PULSE_RELATIONSHIP_LAYOUT = RelationshipLayout(_pulse_relationship_pairs())


def resolve_relationship_table(
    logical_type: str,
    from_type: str,
    to_type: str,
) -> str:
    """Resolve through the closed Pulse manifest used by production composition."""

    return PULSE_RELATIONSHIP_LAYOUT.resolve(logical_type, from_type, to_type)


def introspect_logical_relationships(
    database: Database,
    *,
    layout: RelationshipLayout = PULSE_RELATIONSHIP_LAYOUT,
) -> tuple[LogicalRelationshipDefinition, ...]:
    """Validate every physical table and return only logical definitions.

    Missing tables, wrong kinds, and endpoint drift are schema divergence.  The
    returned values intentionally contain no physical name, record identity, or
    backend object.
    """

    try:
        catalog = database.catalog.catalog
    except Exception as exc:
        mapped = map_grafx_error(exc, operation=_OPERATION)
        raise mapped from exc

    for entry in layout.entries:
        try:
            definition = catalog.table(entry.physical_table)
        except Exception as exc:
            mapped = map_grafx_error(exc, operation=_OPERATION)
            mapped.details.update(
                {
                    "logical_type": entry.logical_type,
                    "from_type": entry.from_type,
                    "to_type": entry.to_type,
                    "physical_table": entry.physical_table,
                }
            )
            raise mapped from exc

        if (
            definition.kind != "rel"
            or definition.from_table != entry.from_type
            or definition.to_table != entry.to_type
        ):
            raise _layout_failure(
                "A physical Grafx table does not implement its declared logical pair.",
                reason="physical_schema_mismatch",
                logical_type=entry.logical_type,
                from_type=entry.from_type,
                to_type=entry.to_type,
                physical_table=entry.physical_table,
                observed_kind=definition.kind,
                observed_from=definition.from_table,
                observed_to=definition.to_table,
            )

    return layout.logical_definitions


__all__ = [
    "LogicalRelationshipDefinition",
    "PULSE_RELATIONSHIP_LAYOUT",
    "RelationshipLayout",
    "RelationshipLayoutEntry",
    "introspect_logical_relationships",
    "resolve_relationship_table",
]
