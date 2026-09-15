"""Closed certification of optional Pulse/Grafx access paths.

These indexes never replace a required base index. Unknown names remain in the
base inventory and are refused by its exact-name/count proof. Every recognized
extra is certified before it is separated from that inventory.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from okto_grafx.domain.index.catalog import identity_index_name
from okto_grafx.domain.index.definition import index_generation_file
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable

from okto_pulse.community.adapters.grafx_ordered_indexes import pulse_ordered_page_index_name
from okto_pulse.community.adapters.grafx_schema_manifest import PULSE_GRAFX_SCHEMA_MANIFEST
from okto_pulse.community.adapters.grafx_source_indexes import pulse_source_index_name


def certified_index_file(view: object, *, phase: str) -> str:
    """Bind legacy names or active nonced files to the exact published definition."""
    nonce = getattr(view.definition, "artifact_nonce", 0)
    valid = type(nonce) is int and 0 <= nonce <= 0xFFFFFFFFFFFFFFFF
    if valid and nonce:
        valid = (
            type(getattr(view, "active_nonce", None)) is int
            and view.active_nonce == nonce
            and getattr(view, "generation_state", None) == "active"
        )
    if not valid:
        raise GraphCapabilityUnavailable(
            "The Grafx index generation does not match its published definition.",
            details={"reason": f"candidate_index_generation_{phase}", "index": view.name},
        )
    expected = index_generation_file(nonce) if nonce else f"index/{view.name}.idx"
    if view.file != expected or view.definition.file != expected:
        raise GraphCapabilityUnavailable(
            "The Grafx index file does not match its exact physical generation.",
            details={"reason": f"candidate_index_file_{phase}", "index": view.name},
        )
    return expected


def certified_base_indexes(
    registered: Sequence[object], catalog_tables: Mapping[str, object], *, phase: str,
) -> tuple[object, ...]:
    """Separate only exact, active known auxiliary definitions with valid coverage."""
    policies = {}
    for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes:
        table_id = catalog_tables[table.name].table_id
        column_positions = {column.name: i for i, column in enumerate(table.columns)}
        for name, columns, layout, derivation, automatic in (
            (pulse_ordered_page_index_name(table.name), ("created_at", "id"),
             "ordered", "ordered_timestamp_string_v1", False),
            (pulse_source_index_name(table.name), ("source_artifact_ref",),
             "hash", "columns", False),
            (identity_index_name(table_id), (), "hash", "record_id_u64_v1", True),
        ):
            policies[name] = (table_id, table.name, columns,
                              tuple(column_positions[c] for c in columns),
                              layout, derivation, automatic)

    base = []
    for view in registered:
        policy = policies.get(view.name)
        if policy is None:
            base.append(view)
            continue
        table_id, table_name, columns, positions, layout, derivation, automatic = policy
        definition = view.definition
        expected = (table_id, table_name, positions, "exact", layout, derivation)
        observed = (definition.table_id, definition.table_name, tuple(definition.positions),
                    definition.visibility.value, definition.layout.value, definition.key_derivation)
        if (
            observed != expected
            or definition.name != view.name
            or definition.file != view.file
            or not isinstance(view.file, str) or not view.file
            or view.table_name != table_name
            or tuple(view.columns) != columns
            or view.visibility.value != "exact"
            or view.layout.value != layout
            or view.key_derivation != derivation
            or view.automatic is not automatic
            or view.generation_state != "active"
            or type(view.active_nonce) is not int or view.active_nonce <= 0
        ):
            raise GraphCapabilityUnavailable(
                "A known Grafx auxiliary index has a conflicting physical definition.",
                details={"reason": f"candidate_auxiliary_index_definition_{phase}",
                         "index": view.name, "phase": f"candidate_{phase}_indexes"},
            )
        if view.stale or view.stale_reason is not None or view.missing_targets != 0:
            raise GraphCapabilityUnavailable(
                "A known Grafx auxiliary index failed its coverage proof.",
                details={"reason": f"candidate_auxiliary_index_coverage_{phase}",
                         "index": view.name, "phase": f"candidate_{phase}_indexes"},
            )
        certified_index_file(view, phase=phase)
    return tuple(base)
