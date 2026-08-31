"""Fail-closed, idempotent bootstrap of the current Pulse schema in Grafx.

This adapter intentionally has no path resolver and activates no provider.  A
composition that already owns an open :class:`okto_grafx.Database` supplies it;
the helper validates the whole current manifest before creating only missing
objects and stamps BoardMeta only after the committed catalog validates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from okto_grafx import Database, Timestamp
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
)

from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_schema_manifest import (
    EMBEDDING_DIMENSION,
    PULSE_GRAFX_SCHEMA_MANIFEST,
    GrafxSchemaManifest,
    GrafxSpaceManifest,
    GrafxTableManifest,
)

_OPERATION = "ensure_current_grafx_board_schema"
_VALIDATE_OPERATION = "validate_current_grafx_schema"

BootstrapFence = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class GrafxSchemaBootstrapResult:
    """Stable result of one bootstrap attempt."""

    schema_version: str
    logical_fingerprint: str
    changed: bool


@dataclass(frozen=True, slots=True)
class _CatalogPreflight:
    missing_spaces: tuple[GrafxSpaceManifest, ...]
    missing_tables: tuple[GrafxTableManifest, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_spaces and not self.missing_tables


@dataclass(frozen=True, slots=True)
class _BoardMeta:
    board_id: str
    schema_version: str
    bootstrapped_at: Timestamp
    embedding_model: str | None
    embedding_dimension: int | None


def _divergence(
    reason: str,
    *,
    operation: str = _OPERATION,
    **details: object,
) -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        "The Grafx catalog does not match the current Pulse schema.",
        details={
            "backend": "okto_grafx",
            "operation": operation,
            "reason": reason,
            **details,
        },
    )


def _invalid_argument(field: str, value: object) -> GraphCapabilityUnavailable:
    return GraphCapabilityUnavailable(
        "Grafx schema bootstrap received an invalid argument.",
        details={
            "backend": "okto_grafx",
            "operation": _OPERATION,
            "reason": "invalid_bootstrap_argument",
            "field": field,
            "value_type": type(value).__name__,
        },
    )


def _validate_arguments(
    *,
    board_id: object,
    bootstrapped_at: object,
    embedding_model: object,
    embedding_dimension: object,
) -> tuple[str, Timestamp, str | None, int | None]:
    if type(board_id) is not str or not board_id:
        raise _invalid_argument("board_id", board_id)
    if type(bootstrapped_at) is not Timestamp:
        raise _invalid_argument("bootstrapped_at", bootstrapped_at)
    if embedding_model is None:
        if embedding_dimension is not None:
            raise _invalid_argument("embedding_dimension", embedding_dimension)
        return board_id, bootstrapped_at, None, None
    if type(embedding_model) is not str or not embedding_model:
        raise _invalid_argument("embedding_model", embedding_model)
    if (
        type(embedding_dimension) is not int
        or embedding_dimension != EMBEDDING_DIMENSION
    ):
        raise _invalid_argument("embedding_dimension", embedding_dimension)
    return board_id, bootstrapped_at, embedding_model, embedding_dimension


def _observed_column_signature(column: object) -> tuple[object, ...]:
    column_type = column.type
    return (
        column.name,
        column_type.name,
        column.nullable,
        column.vector_space,
    )


def _expected_column_signature(column: object) -> tuple[object, ...]:
    return (
        column.name,
        column.grafx_value_type,
        column.nullable,
        column.vector_space,
    )


def _validate_table(
    expected: GrafxTableManifest,
    observed: object,
    *,
    operation: str,
) -> None:
    observed_shape = {
        "kind": observed.kind,
        "primary_key": observed.primary_key,
        "from_table": observed.from_table,
        "to_table": observed.to_table,
        "columns": tuple(
            _observed_column_signature(column) for column in tuple(observed.columns)
        ),
    }
    expected_shape = {
        "kind": expected.kind,
        "primary_key": expected.primary_key,
        "from_table": expected.from_table,
        "to_table": expected.to_table,
        "columns": tuple(
            _expected_column_signature(column) for column in expected.columns
        ),
    }
    if observed_shape != expected_shape:
        raise _divergence(
            "table_shape_mismatch",
            operation=operation,
            table=expected.name,
            expected=expected_shape,
            observed=observed_shape,
        )


def _validate_space(
    expected: GrafxSpaceManifest,
    observed: object,
    *,
    operation: str,
) -> None:
    metric = observed.metric.value
    observed_shape = {
        "dimension": observed.dimension,
        "metric": metric,
        "normalized": observed.normalized,
        "storage_dtype": observed.storage_dtype,
        "state": observed.state,
    }
    expected_shape = {
        "dimension": expected.dimension,
        "metric": expected.metric,
        "normalized": expected.normalized,
        "storage_dtype": expected.storage_dtype,
        "state": "active",
    }
    if observed_shape != expected_shape:
        raise _divergence(
            "space_shape_mismatch",
            operation=operation,
            space=expected.name,
            expected=expected_shape,
            observed=observed_shape,
        )


def _catalog_preflight(
    database: Database,
    *,
    catalog: object | None = None,
    manifest: GrafxSchemaManifest = PULSE_GRAFX_SCHEMA_MANIFEST,
    operation: str = _OPERATION,
) -> _CatalogPreflight:
    snapshot = database.catalog if catalog is None else catalog
    logical_catalog = snapshot.catalog
    observed_tables = tuple(logical_catalog.tables())
    observed_spaces = tuple(logical_catalog.spaces())
    tables_by_name = {table.name: table for table in observed_tables}
    spaces_by_name = {space.name: space for space in observed_spaces}

    expected_table_names = {table.name for table in manifest.tables}
    expected_space_names = {space.name for space in manifest.spaces}
    unexpected_tables = tuple(
        table.name
        for table in observed_tables
        if table.name not in expected_table_names
    )
    unexpected_spaces = tuple(
        space.name
        for space in observed_spaces
        if space.name not in expected_space_names
    )
    if unexpected_tables or unexpected_spaces:
        raise _divergence(
            "unexpected_schema_object",
            operation=operation,
            tables=unexpected_tables,
            spaces=unexpected_spaces,
        )

    missing_spaces: list[GrafxSpaceManifest] = []
    for expected in manifest.spaces:
        observed = spaces_by_name.get(expected.name)
        if observed is None:
            missing_spaces.append(expected)
        else:
            _validate_space(expected, observed, operation=operation)

    missing_tables: list[GrafxTableManifest] = []
    for expected in manifest.tables:
        observed = tables_by_name.get(expected.name)
        if observed is None:
            missing_tables.append(expected)
        else:
            _validate_table(expected, observed, operation=operation)

    return _CatalogPreflight(tuple(missing_spaces), tuple(missing_tables))


def _read_board_meta(database: Database, *, table_exists: bool) -> _BoardMeta | None:
    if not table_exists:
        return None
    result = database.execute(
        "MATCH (m:BoardMeta) "
        "RETURN m.board_id, m.schema_version, m.bootstrapped_at, "
        "m.embedding_model, m.embedding_dimension"
    )
    if len(result.rows) > 1:
        raise _divergence("board_meta_not_singleton", row_count=len(result.rows))
    if not result.rows:
        return None
    row = result.rows[0]
    if len(row) != 5:
        raise _divergence("board_meta_projection_mismatch", arity=len(row))
    board_id, schema_version, bootstrapped_at, model, dimension = row
    if (
        type(board_id) is not str
        or type(schema_version) is not str
        or type(bootstrapped_at) is not Timestamp
    ):
        raise _divergence(
            "board_meta_value_type_mismatch",
            board_id_type=type(board_id).__name__,
            schema_version_type=type(schema_version).__name__,
            bootstrapped_at_type=type(bootstrapped_at).__name__,
        )
    if model is None and dimension is None:
        pass
    elif (
        type(model) is not str
        or not model
        or type(dimension) is not int
        or dimension != EMBEDDING_DIMENSION
    ):
        raise _divergence(
            "board_meta_embedding_mismatch",
            embedding_model_type=type(model).__name__,
            embedding_dimension=dimension,
        )
    return _BoardMeta(
        board_id=board_id,
        schema_version=schema_version,
        bootstrapped_at=bootstrapped_at,
        embedding_model=model,
        embedding_dimension=dimension,
    )


def _validate_board_meta(
    observed: _BoardMeta,
    *,
    board_id: str,
    embedding_model: str | None,
    embedding_dimension: int | None,
) -> bool:
    if observed.board_id != board_id:
        raise _divergence(
            "board_meta_board_mismatch",
            expected_board_id=board_id,
            observed_board_id=observed.board_id,
        )
    if observed.schema_version != PULSE_GRAFX_SCHEMA_MANIFEST.schema_version:
        raise _divergence(
            "board_meta_version_mismatch",
            expected_version=PULSE_GRAFX_SCHEMA_MANIFEST.schema_version,
            observed_version=observed.schema_version,
        )
    requested = (embedding_model, embedding_dimension)
    persisted = (observed.embedding_model, observed.embedding_dimension)
    if requested == (None, None) or requested == persisted:
        return False
    if persisted == (None, None):
        return True
    raise _divergence(
        "board_meta_embedding_mismatch",
        expected_embedding_model=embedding_model,
        expected_embedding_dimension=embedding_dimension,
        observed_embedding_model=observed.embedding_model,
        observed_embedding_dimension=observed.embedding_dimension,
    )


def _commit_statements(
    database: Database,
    statements: tuple[tuple[str, dict], ...],
    *,
    revalidate_fence: BootstrapFence | None = None,
) -> None:
    transaction = database.begin("write")
    try:
        for text, parameters in statements:
            if revalidate_fence is not None:
                revalidate_fence("bootstrap")
            transaction.execute(text, parameters)
        if revalidate_fence is not None:
            revalidate_fence("commit")
        report = transaction.commit()
    except BaseException as failure:
        if transaction.active:
            try:
                transaction.rollback()
            except BaseException as cleanup_failure:  # noqa: BLE001
                failure.add_note(
                    "Grafx bootstrap rollback also failed: "
                    f"{type(cleanup_failure).__name__}"
                )
        raise
    if not report.durable or not report.wrote:
        raise _divergence(
            "bootstrap_commit_not_durable",
            durable=report.durable,
            wrote=report.wrote,
        )


def _create_missing_schema(
    database: Database,
    preflight: _CatalogPreflight,
    *,
    revalidate_fence: BootstrapFence | None = None,
) -> None:
    missing_space_names = {space.name for space in preflight.missing_spaces}
    missing_table_names = {table.name for table in preflight.missing_tables}
    manifest = PULSE_GRAFX_SCHEMA_MANIFEST
    statements = tuple(
        (space.ddl(), {})
        for space in manifest.spaces
        if space.name in missing_space_names
    ) + tuple(
        (table.ddl(), {})
        for table in manifest.tables
        if table.name in missing_table_names
    )
    if statements:
        _commit_statements(
            database,
            statements,
            revalidate_fence=revalidate_fence,
        )


def _stamp_board_meta(
    database: Database,
    *,
    board_id: str,
    bootstrapped_at: Timestamp,
    embedding_model: str | None,
    embedding_dimension: int | None,
    enrich: bool,
    revalidate_fence: BootstrapFence | None = None,
) -> None:
    if enrich:
        statement = (
            "MATCH (m:BoardMeta {board_id: $board_id}) "
            "SET m.embedding_model = $embedding_model, "
            "m.embedding_dimension = $embedding_dimension "
            "RETURN m.board_id"
        )
        parameters = {
            "board_id": board_id,
            "embedding_model": embedding_model,
            "embedding_dimension": embedding_dimension,
        }
    else:
        statement = (
            "CREATE (m:BoardMeta {board_id: $board_id, schema_version: $schema_version, "
            "bootstrapped_at: $bootstrapped_at, embedding_model: $embedding_model, "
            "embedding_dimension: $embedding_dimension}) RETURN m.board_id"
        )
        parameters = {
            "board_id": board_id,
            "schema_version": PULSE_GRAFX_SCHEMA_MANIFEST.schema_version,
            "bootstrapped_at": bootstrapped_at,
            "embedding_model": embedding_model,
            "embedding_dimension": embedding_dimension,
        }
    _commit_statements(
        database,
        ((statement, parameters),),
        revalidate_fence=revalidate_fence,
    )


def read_current_grafx_schema_version(
    database: Database,
    *,
    catalog: object | None = None,
) -> str | None:
    """Return the persisted BoardMeta version without mutating the catalog."""

    try:
        preflight = _catalog_preflight(
            database,
            catalog=catalog,
            operation=_VALIDATE_OPERATION,
        )
        table_exists = all(
            table.name != "BoardMeta" for table in preflight.missing_tables
        )
        observed = _read_board_meta(database, table_exists=table_exists)
        return None if observed is None else observed.schema_version
    except GraphError:
        raise
    except Exception as exc:
        mapped = map_grafx_error(exc, operation=_VALIDATE_OPERATION)
        raise mapped from exc


def validate_current_grafx_schema(
    database: Database,
    *,
    catalog: object | None = None,
) -> str:
    """Validate the complete logical schema and return its stable fingerprint."""

    try:
        preflight = _catalog_preflight(
            database,
            catalog=catalog,
            operation=_VALIDATE_OPERATION,
        )
        if not preflight.complete:
            raise _divergence(
                "schema_incomplete",
                operation=_VALIDATE_OPERATION,
                missing_spaces=tuple(space.name for space in preflight.missing_spaces),
                missing_tables=tuple(table.name for table in preflight.missing_tables),
            )
        return PULSE_GRAFX_SCHEMA_MANIFEST.logical_fingerprint
    except GraphError:
        raise
    except Exception as exc:
        mapped = map_grafx_error(exc, operation=_VALIDATE_OPERATION)
        raise mapped from exc


def ensure_current_grafx_board_schema(
    database: Database,
    *,
    board_id: str,
    bootstrapped_at: Timestamp,
    embedding_model: str | None = None,
    embedding_dimension: int | None = None,
    revalidate_fence: BootstrapFence | None = None,
    catalog: object | None = None,
) -> GrafxSchemaBootstrapResult:
    """Ensure exactly the current schema and BoardMeta singleton, or fail closed."""

    board_id, bootstrapped_at, embedding_model, embedding_dimension = (
        _validate_arguments(
            board_id=board_id,
            bootstrapped_at=bootstrapped_at,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
        )
    )
    changed = False
    try:
        catalog_snapshot = database.catalog if catalog is None else catalog
        preflight = _catalog_preflight(database, catalog=catalog_snapshot)
        board_meta_exists = all(
            table.name != "BoardMeta" for table in preflight.missing_tables
        )
        observed_meta = _read_board_meta(database, table_exists=board_meta_exists)
        if observed_meta is not None and not preflight.complete:
            raise _divergence(
                "versioned_partial_schema",
                missing_spaces=tuple(space.name for space in preflight.missing_spaces),
                missing_tables=tuple(table.name for table in preflight.missing_tables),
            )
        enrich = (
            _validate_board_meta(
                observed_meta,
                board_id=board_id,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
            )
            if observed_meta is not None
            else False
        )

        if not preflight.complete:
            _create_missing_schema(
                database,
                preflight,
                revalidate_fence=revalidate_fence,
            )
            changed = True

        # This is intentionally a fresh public snapshot after schema commit.  BoardMeta is not
        # stamped until the committed catalog has proved the complete current shape.
        validation_catalog = None if changed else catalog_snapshot
        fingerprint = validate_current_grafx_schema(
            database,
            catalog=validation_catalog,
        )
        if changed:
            observed_meta = _read_board_meta(database, table_exists=True)
        if observed_meta is None:
            _stamp_board_meta(
                database,
                board_id=board_id,
                bootstrapped_at=bootstrapped_at,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
                enrich=False,
                revalidate_fence=revalidate_fence,
            )
            changed = True
        else:
            enrich = _validate_board_meta(
                observed_meta,
                board_id=board_id,
                embedding_model=embedding_model,
                embedding_dimension=embedding_dimension,
            )
            if enrich:
                _stamp_board_meta(
                    database,
                    board_id=board_id,
                    bootstrapped_at=bootstrapped_at,
                    embedding_model=embedding_model,
                    embedding_dimension=embedding_dimension,
                    enrich=True,
                    revalidate_fence=revalidate_fence,
                )
                changed = True

        final_meta = (
            _read_board_meta(database, table_exists=True)
            if changed
            else observed_meta
        )
        if final_meta is None:
            raise _divergence("board_meta_missing_after_bootstrap")
        if _validate_board_meta(
            final_meta,
            board_id=board_id,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
        ):
            raise _divergence("board_meta_not_stamped_after_bootstrap")
        return GrafxSchemaBootstrapResult(
            schema_version=PULSE_GRAFX_SCHEMA_MANIFEST.schema_version,
            logical_fingerprint=fingerprint,
            changed=changed,
        )
    except GraphError:
        raise
    except Exception as exc:
        mapped = map_grafx_error(exc, operation=_OPERATION)
        raise mapped from exc


__all__ = [
    "BootstrapFence",
    "GrafxSchemaBootstrapResult",
    "ensure_current_grafx_board_schema",
    "read_current_grafx_schema_version",
    "validate_current_grafx_schema",
]
