"""Read-only certification of current Grafx index definitions and coverage."""
from collections.abc import Callable
from typing import Any
import hashlib
from okto_grafx import Database
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable, GraphError
from .grafx_error_mapping import map_grafx_error
from .grafx_schema_manifest import PULSE_GRAFX_SCHEMA_MANIFEST

def _divergence(reason, *, phase, **details):
    return GraphCapabilityUnavailable("The Grafx index inventory is incompatible.",
        details={"backend": "okto_grafx", "operation": "validate_current_indexes", "phase": phase, "reason": reason, **details})

def _backend_call(action: Callable[[], Any], phase: str) -> Any:
    try:
        return action()
    except GraphError:
        raise
    except Exception as exc:
        raise map_grafx_error(exc, operation="validate_current_indexes") from exc

def _bounded_identifier(value: object) -> str:
    """Return non-secret schema evidence without allowing an unbounded detail value."""
    if type(value) is not str:
        return f"<{type(value).__name__}>"
    if len(value) <= 128:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"<identifier:length={len(value)}:sha256={digest}>"

def require_current_grafx_indexes(
    candidate: Database,
    phase: str,
    *,
    schema_manifest=PULSE_GRAFX_SCHEMA_MANIFEST,
) -> None:
    """Prove the whole index inventory, not merely that nothing was reported stale.

    A catalog and a clean digest can both agree while an index file is missing, so the
    count, the staleness and the per-space definition are each asserted.
    """
    boundary_phase = f"candidate_{phase}_indexes"
    unindexed_tables = _backend_call(lambda: candidate.unindexed_tables, boundary_phase)
    if unindexed_tables != ():
        raise _divergence(
            f"candidate_unindexed_tables_{phase}",
            phase=boundary_phase,
            tables=[
                _bounded_identifier(name)
                for name in sorted(unindexed_tables, key=str)[:8]
            ],
        )
    stale_indexes = _backend_call(lambda: candidate.stale_indexes, boundary_phase)
    if stale_indexes != ():
        raise _divergence(
            f"candidate_stale_indexes_{phase}",
            phase=boundary_phase,
            count=len(stale_indexes),
        )
    registered = _backend_call(lambda: candidate.indexes.indexes(), boundary_phase)
    registered_names = tuple(view.name for view in registered)
    folded_registered_names = tuple(name.casefold() for name in registered_names)
    if len(set(folded_registered_names)) != len(folded_registered_names):
        raise _divergence(
            f"candidate_duplicate_index_name_{phase}", phase=boundary_phase
        )

    catalog_tables = {
        table.name: table
        for table in _backend_call(
            lambda: candidate.catalog.catalog.tables(), boundary_phase
        )
    }
    from okto_pulse.community.adapters.grafx_auxiliary_indexes import (
        certified_base_indexes, certified_index_file,
    )

    # Optional known access paths cannot substitute for missing base indexes.
    # Their definition/coverage is checked before excluding them from the exact
    # base inventory. Unknown extras remain subject to the original refusal.
    registered = certified_base_indexes(registered, catalog_tables, phase=phase)
    registered_names = tuple(view.name for view in registered)
    expected_index_total = (
        len(schema_manifest.nodes)
        + 1
        + 2 * len(schema_manifest.relationships)
        + len(schema_manifest.spaces)
    )
    if len(registered) != expected_index_total:
        raise _divergence(
            f"candidate_index_count_{phase}",
            phase=boundary_phase,
            expected=expected_index_total,
            observed=len(registered),
        )
    expected: dict[str, tuple[str, int, str, tuple[int, ...], str, int, str]] = {}

    def expect(
        *,
        name: str,
        table_name: str,
        positions: tuple[int, ...],
        visibility: str,
        key_derivation: str,
    ) -> None:
        table = catalog_tables[table_name]
        expected[name] = (
            f"index/{name}.idx",
            table.table_id,
            table_name,
            positions,
            visibility,
            64,
            key_derivation,
        )

    keyed_tables = (
        schema_manifest.board_meta,
        *schema_manifest.nodes,
    )
    for manifest_table in keyed_tables:
        primary_key = manifest_table.primary_key
        if primary_key is None:  # pragma: no cover - closed manifest invariant
            raise AssertionError(f"{manifest_table.name} lost its primary key")
        primary_position = next(
            position
            for position, column in enumerate(manifest_table.columns)
            if column.name == primary_key
        )
        expect(
            name=f"pk_{manifest_table.name}",
            table_name=manifest_table.name,
            positions=(primary_position,),
            visibility="exact",
            key_derivation="columns",
        )
    for manifest_table in schema_manifest.relationships:
        expect(
            name=f"ef_{manifest_table.name}",
            table_name=manifest_table.name,
            positions=(0,),
            visibility="exact",
            key_derivation="columns",
        )
        expect(
            name=f"et_{manifest_table.name}",
            table_name=manifest_table.name,
            positions=(1,),
            visibility="exact",
            key_derivation="columns",
        )
    for manifest_table in schema_manifest.nodes:
        vector_column = next(
            (column for column in manifest_table.columns if column.vector_space),
            None,
        )
        if vector_column is None:  # pragma: no cover - closed manifest invariant
            raise AssertionError(f"{manifest_table.name} lost its vector column")
        vector_position = next(
            position
            for position, column in enumerate(manifest_table.columns)
            if column.name == vector_column.name
        )
        vector_name = f"vector_{manifest_table.name}_{vector_column.vector_space}"
        expect(
            name=vector_name,
            table_name=manifest_table.name,
            positions=(vector_position,),
            visibility="proximity",
            key_derivation="vector_digest_v1",
        )

    observed_names = set(registered_names)
    if observed_names != set(expected):
        raise _divergence(
            f"candidate_index_inventory_{phase}",
            phase=boundary_phase,
            missing=[
                _bounded_identifier(name)
                for name in sorted(set(expected) - observed_names)[:8]
            ],
            unexpected=[
                _bounded_identifier(name)
                for name in sorted(observed_names - set(expected))[:8]
            ],
        )
    certified_files = {}
    for view in registered:
        definition = view.definition
        physical_file = certified_index_file(view, phase=phase)
        certified_files[view.name] = physical_file
        wanted = (physical_file, *expected[view.name][1:])
        observed = (
            view.file,
            definition.table_id,
            definition.table_name,
            definition.positions,
            definition.visibility.value,
            definition.bucket_count,
            definition.key_derivation,
        )
        if (
            observed != wanted
            or definition.name != view.name
            or definition.file != view.file
            or view.visibility.value != wanted[4]
        ):
            raise _divergence(
                f"candidate_index_definition_{phase}",
                phase=boundary_phase,
                index=_bounded_identifier(view.name),
            )
        if view.stale or view.stale_reason is not None or view.missing_targets != 0:
            raise _divergence(
                f"candidate_index_coverage_{phase}",
                phase=boundary_phase,
                index=_bounded_identifier(view.name),
                stale=view.stale,
                missing_targets=view.missing_targets,
            )

    vectors = _backend_call(lambda: candidate.vectors.indexes(), boundary_phase)
    if len(vectors) != len(schema_manifest.spaces):
        raise _divergence(
            f"candidate_vector_index_count_{phase}",
            phase=boundary_phase,
            expected=len(schema_manifest.spaces),
            observed=len(vectors),
        )
    vector_spaces = tuple(view.space_name for view in vectors)
    vector_names = tuple(view.name for view in vectors)
    if len({name.casefold() for name in vector_names}) != len(vector_names) or len(
        {name.casefold() for name in vector_spaces}
    ) != len(vector_spaces):
        raise _divergence(
            f"candidate_duplicate_vector_index_{phase}", phase=boundary_phase
        )
    wanted = {space.name: space for space in schema_manifest.spaces}
    if set(vector_spaces) != set(wanted):
        raise _divergence(
            f"candidate_vector_index_inventory_{phase}",
            phase=boundary_phase,
            missing=[
                _bounded_identifier(name)
                for name in sorted(set(wanted) - set(vector_spaces))[:8]
            ],
            unexpected=[
                _bounded_identifier(name)
                for name in sorted(set(vector_spaces) - set(wanted))[:8]
            ],
        )
    catalog_spaces = {
        space.name: space
        for space in _backend_call(
            lambda: candidate.catalog.catalog.spaces(), boundary_phase
        )
    }
    for view in vectors:
        space = wanted[view.space_name]
        expected_name = f"vector_{space.node_type}_{space.name}"
        expected_file = certified_files[expected_name]
        if view.stale or view.stale_reason is not None:
            raise _divergence(
                f"candidate_vector_index_stale_{phase}",
                phase=boundary_phase,
                space=view.space_name,
                stale_reason_present=view.stale_reason is not None,
            )
        if (
            view.name != expected_name
            or view.file != expected_file
            or view.space_id != catalog_spaces[space.name].space_id
            or view.space_name != space.name
            or view.dimension != space.dimension
            or view.metric_of_space.value != space.metric
            or view.storage_dtype != space.storage_dtype
        ):
            raise _divergence(
                f"candidate_vector_index_definition_{phase}",
                phase=boundary_phase,
                space=view.space_name,
            )
