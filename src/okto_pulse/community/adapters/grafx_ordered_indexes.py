"""Pulse-owned ordered index policy for Grafx knowledge-graph pages.

The Core query remains backend neutral.  Community installs the physical capability that lets
Grafx execute its stable ``(created_at DESC, id DESC)`` cursor contract without a graph-wide
sort.  Existing definitions are never replaced implicitly: a conflicting durable authority is
reported fail-closed for operator review.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from okto_grafx import Database
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable

from okto_pulse.community.adapters.grafx_schema_manifest import (
    PULSE_GRAFX_SCHEMA_MANIFEST,
    GrafxSchemaManifest,
)

_OPERATION = "ensure_pulse_grafx_ordered_page_indexes"
_COLUMNS = ("created_at", "id")
_LAYOUT = "ordered"
_VISIBILITY = "exact"
_KEY_DERIVATION = "ordered_timestamp_string_v1"
_GENERATION_STATE = "active"

IndexFence = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class GrafxOrderedIndexResult:
    """Outcome of one idempotent ordered-index activation pass."""

    created: tuple[str, ...]
    existing: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.created)


def pulse_ordered_page_index_name(table_name: str) -> str:
    """Return the stable Community-owned name for one node table's page index."""

    return f"pulse_page_{table_name.lower()}"


def _value(value: object) -> object:
    return getattr(value, "value", value)


def _invalid_index(name: str, *, expected: dict[str, object], observed: object) -> None:
    raise GraphCapabilityUnavailable(
        "The persisted Grafx ordered page index conflicts with the Pulse policy.",
        details={
            "backend": "okto_grafx",
            "operation": _OPERATION,
            "reason": "ordered_page_index_mismatch",
            "index": name,
            "expected": expected,
            "observed": observed,
        },
    )


def _validate_index(index: object, *, name: str, table_name: str) -> None:
    expected = {
        "table": table_name,
        "columns": _COLUMNS,
        "layout": _LAYOUT,
        "visibility": _VISIBILITY,
        "key_derivation": _KEY_DERIVATION,
        "generation_state": _GENERATION_STATE,
        "stale": False,
    }
    observed = {
        "table": getattr(index, "table_name", None),
        "columns": tuple(getattr(index, "columns", ())),
        "layout": _value(getattr(index, "layout", None)),
        "visibility": _value(getattr(index, "visibility", None)),
        "key_derivation": getattr(index, "key_derivation", None),
        "generation_state": getattr(index, "generation_state", None),
        "stale": getattr(index, "stale", None),
    }
    if observed != expected:
        _invalid_index(name, expected=expected, observed=observed)


def _indexes_by_name(database: Database) -> dict[str, object]:
    return {index.name.casefold(): index for index in database.indexes.indexes()}


def ensure_pulse_grafx_ordered_page_indexes(
    database: Database,
    *,
    manifest: GrafxSchemaManifest = PULSE_GRAFX_SCHEMA_MANIFEST,
    revalidate_fence: IndexFence | None = None,
) -> GrafxOrderedIndexResult:
    """Create every exact Pulse page index once and validate durable incumbents.

    A concurrent creator is accepted only after a fresh registry snapshot proves that the exact
    expected ACTIVE definition won.  Every other creation failure is propagated unchanged.
    """

    indexes = _indexes_by_name(database)
    created: list[str] = []
    existing: list[str] = []
    for table in manifest.nodes:
        name = pulse_ordered_page_index_name(table.name)
        incumbent = indexes.get(name.casefold())
        if incumbent is not None:
            _validate_index(incumbent, name=name, table_name=table.name)
            existing.append(name)
            continue

        if revalidate_fence is not None:
            revalidate_fence(f"ordered_index:{table.name}")
        try:
            activated = database.create_index(
                name,
                table.name,
                _COLUMNS,
                layout=_LAYOUT,
            )
        except Exception:
            # A second process may have published the same named authority after our snapshot.
            # Success is idempotent only when a fresh public snapshot proves exact equivalence.
            concurrent = _indexes_by_name(database).get(name.casefold())
            if concurrent is None:
                raise
            _validate_index(concurrent, name=name, table_name=table.name)
            existing.append(name)
            indexes[name.casefold()] = concurrent
            continue
        _validate_index(activated, name=name, table_name=table.name)
        created.append(name)
        indexes[name.casefold()] = activated

    return GrafxOrderedIndexResult(tuple(created), tuple(existing))


__all__ = [
    "GrafxOrderedIndexResult",
    "IndexFence",
    "ensure_pulse_grafx_ordered_page_indexes",
    "pulse_ordered_page_index_name",
]
