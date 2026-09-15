"""Community-owned exact source-reference indexes for provenance/identity reads.

The generic Core query and Grafx's existing exact-index protocol are unchanged.
Activation is additive, fenced and idempotent; a conflicting incumbent is never
replaced implicitly. This helper must run under the normal schema lifecycle.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from okto_grafx import Database
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable

from okto_pulse.community.adapters.grafx_schema_manifest import (
    PULSE_GRAFX_SCHEMA_MANIFEST, GrafxSchemaManifest,
)


@dataclass(frozen=True, slots=True)
class GrafxSourceIndexResult:
    created: tuple[str, ...]
    existing: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.created)


def pulse_source_index_name(table_name: str) -> str:
    return f"pulse_source_{table_name.lower()}"


def validate_pulse_grafx_source_index(index: object, *, table_name: str) -> None:
    """Prove the persisted access-path contract, without imposing hash sizing."""
    expected = {
        "name": pulse_source_index_name(table_name),
        "table": table_name,
        "columns": ("source_artifact_ref",),
        "layout": "hash",
        "visibility": "exact",
        "key_derivation": "columns",
        "generation_state": "active",
        "stale": False,
        "automatic": False,
    }
    layout = getattr(index, "layout", None)
    visibility = getattr(index, "visibility", None)
    observed = {
        "name": getattr(index, "name", None),
        "table": getattr(index, "table_name", None),
        "columns": tuple(getattr(index, "columns", ())),
        "layout": getattr(layout, "value", layout),
        "visibility": getattr(visibility, "value", visibility),
        "key_derivation": getattr(index, "key_derivation", None),
        "generation_state": getattr(index, "generation_state", None),
        "stale": getattr(index, "stale", None),
        "automatic": getattr(index, "automatic", None),
    }
    if observed != expected:
        raise GraphCapabilityUnavailable(
            "The persisted Grafx source-reference index conflicts with Pulse policy.",
            details={"backend": "okto_grafx", "operation": "ensure_source_indexes",
                     "reason": "source_index_mismatch", "expected": expected,
                     "observed": observed},
        )


def ensure_pulse_grafx_source_indexes(
    database: Database,
    *,
    manifest: GrafxSchemaManifest = PULSE_GRAFX_SCHEMA_MANIFEST,
    revalidate_fence: Callable[[str], None] | None = None,
) -> GrafxSourceIndexResult:
    """Build exact source indexes, accepting only equivalent concurrent winners."""
    indexes = {i.name.casefold(): i for i in database.indexes.indexes()}
    created: list[str] = []
    existing: list[str] = []
    for table in manifest.nodes:
        name = pulse_source_index_name(table.name)
        incumbent = indexes.get(name.casefold())
        if incumbent is not None:
            validate_pulse_grafx_source_index(incumbent, table_name=table.name)
            existing.append(name)
            continue
        if revalidate_fence is not None:
            revalidate_fence(f"source_index:{table.name}")
        try:
            activated = database.create_index(name, table.name, ("source_artifact_ref",))
        except Exception:
            concurrent = {
                i.name.casefold(): i for i in database.indexes.indexes()
            }.get(name.casefold())
            if concurrent is None:
                raise
            validate_pulse_grafx_source_index(concurrent, table_name=table.name)
            existing.append(name)
            indexes[name.casefold()] = concurrent
            continue
        validate_pulse_grafx_source_index(activated, table_name=table.name)
        created.append(name)
        indexes[name.casefold()] = activated
    return GrafxSourceIndexResult(tuple(created), tuple(existing))
