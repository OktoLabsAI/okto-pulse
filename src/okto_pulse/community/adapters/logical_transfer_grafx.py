"""Bounded logical snapshot source for an already-open Grafx database.

Grafx exposes physical relationship endpoints as table-local record ids.  A
portable artifact needs logical keys, so this adapter builds a temporary
SQLite map keyed by ``(node_table, record_id)`` while it scans the fixed MVCC
snapshot.  The map is disk-backed and every in-memory batch remains bounded.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    LayoutIdentity,
    LogicalCounts,
    LogicalNode,
    LogicalRelation,
    LogicalSchema,
    LogicalSchemaError,
    LogicalSchemaIndex,
    LogicalTimestamp,
    LogicalValue,
    LogicalVector,
)

if TYPE_CHECKING:
    from okto_grafx import Database, ScanPageV1, Transaction


_DEFAULT_SCAN_BATCH_SIZE = 500
_GRAFX_SCALAR_TYPES = {
    "bool": "BOOL",
    "float64": "DOUBLE",
    "int64": "INT64",
    "string": "STRING",
    "timestamp_us": "TIMESTAMP",
}


class CommunityGrafxLogicalSnapshotSource:
    """Open one Grafx read transaction as a Core logical snapshot."""

    def __init__(
        self,
        database: Database,
        *,
        schema: LogicalSchema,
        relationship_tables: Mapping[LayoutIdentity, str],
        scan_batch_size: int = _DEFAULT_SCAN_BATCH_SIZE,
        temporary_parent: Path | None = None,
    ) -> None:
        if type(scan_batch_size) is not int or scan_batch_size < 1:
            raise LogicalSchemaError(
                "Grafx scan_batch_size must be a positive int",
                detail=repr(scan_batch_size),
            )
        copied_relationships = dict(relationship_tables)
        if any(
            type(identity) is not tuple
            or len(identity) != 3
            or any(type(part) is not str or not part for part in identity)
            for identity in copied_relationships
        ):
            raise LogicalSchemaError(
                "Grafx relationship table mapping contains an invalid layout identity"
            )
        expected_layouts = {layout.identity for layout in schema.relation_layouts}
        if set(copied_relationships) != expected_layouts:
            raise LogicalSchemaError(
                "Grafx relationship table mapping does not match the logical schema",
                detail=(
                    f"missing={sorted(expected_layouts - set(copied_relationships))} "
                    f"extra={sorted(set(copied_relationships) - expected_layouts)}"
                ),
            )
        if any(
            type(name) is not str or not name for name in copied_relationships.values()
        ):
            raise LogicalSchemaError(
                "Grafx relationship table mapping contains an invalid table name"
            )
        if len(set(copied_relationships.values())) != len(copied_relationships):
            raise LogicalSchemaError(
                "Grafx relationship table mapping reuses a physical table"
            )
        self._database = database
        self._schema = schema
        self._relationship_tables = MappingProxyType(copied_relationships)
        self._scan_batch_size = scan_batch_size
        self._temporary_parent = temporary_parent

    def open_snapshot(self) -> CommunityGrafxLogicalSnapshot:
        transaction = self._database.begin("read")
        try:
            catalog = self._database.catalog.catalog
        except BaseException as failure:
            _rollback_preserving(transaction, failure)
            raise
        # The snapshot constructor owns the transaction from this point and
        # closes it if preparation fails.  Keeping it outside the try avoids a
        # second rollback that could mask the preparation error.
        return CommunityGrafxLogicalSnapshot(
            transaction,
            catalog=catalog,
            schema=self._schema,
            relationship_tables=self._relationship_tables,
            scan_batch_size=self._scan_batch_size,
            temporary_parent=self._temporary_parent,
        )


class CommunityGrafxLogicalSnapshot:
    """One prepared, fixed Grafx snapshot with a disk-backed endpoint index."""

    def __init__(
        self,
        transaction: Transaction,
        *,
        catalog: Any,
        schema: LogicalSchema,
        relationship_tables: Mapping[LayoutIdentity, str],
        scan_batch_size: int,
        temporary_parent: Path | None,
    ) -> None:
        self._transaction = transaction
        self._catalog = catalog
        self._schema = schema
        self._relationship_tables = relationship_tables
        self._scan_batch_size = scan_batch_size
        self._closed = False
        self._cleanup_complete = False
        self._index = LogicalSchemaIndex.build(schema)
        self._endpoints: _EndpointMap | None = None
        self._counts: LogicalCounts | None = None
        try:
            _validate_physical_schema(catalog, schema, relationship_tables)
            self._endpoints = _EndpointMap(temporary_parent)
            self._counts = self._prepare()
        except BaseException as failure:
            try:
                self.close()
            except BaseException as cleanup_failure:  # noqa: BLE001 - preserve signal
                failure.add_note(
                    "Grafx snapshot preparation cleanup also failed: "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                )
            raise

    def schema(self) -> LogicalSchema:
        self._require_open()
        return self._schema

    def counts(self) -> LogicalCounts:
        self._require_open()
        if self._counts is None:
            raise LogicalSchemaError("Grafx logical snapshot census is unavailable")
        return self._counts

    def iter_nodes(self, *, batch_size: int) -> Iterator[Sequence[LogicalNode]]:
        self._require_batch_size(batch_size)
        for node_type in self._schema.node_types:
            table = self._catalog.table(node_type.name)
            for page in _scan_pages(self._transaction, table.name, batch_size):
                batch = tuple(
                    self._logical_node(table, node_type, row) for row in page.rows
                )
                if batch:
                    yield batch

    def iter_relations(self, *, batch_size: int) -> Iterator[Sequence[LogicalRelation]]:
        self._require_batch_size(batch_size)
        for layout in self._schema.relation_layouts:
            table_name = self._relationship_tables[layout.identity]
            table = self._catalog.table(table_name)
            for page in _scan_pages(self._transaction, table.name, batch_size):
                batch = tuple(
                    self._logical_relation(table, layout, row) for row in page.rows
                )
                if batch:
                    yield batch

    def close(self) -> None:
        if self._cleanup_complete:
            return
        # Closed means unusable; cleanup_complete means every owned resource
        # was actually released.  Keeping those states separate lets a caller
        # retry a transient rollback or filesystem cleanup failure without
        # reopening access to a half-closed snapshot.
        self._closed = True
        primary_failure: BaseException | None = None
        if self._endpoints is not None:
            try:
                self._endpoints.close()
            except BaseException as exc:  # noqa: BLE001 - release on cancellation
                primary_failure = exc
        try:
            if self._transaction.active:
                self._transaction.rollback()
        except BaseException as exc:  # noqa: BLE001 - release on cancellation
            if primary_failure is None:
                primary_failure = exc
            else:
                primary_failure.add_note(
                    f"Grafx snapshot rollback also failed: {type(exc).__name__}: {exc}"
                )
        if primary_failure is not None:
            raise primary_failure
        self._cleanup_complete = True

    def _prepare(self) -> LogicalCounts:
        node_count = 0
        relation_count = 0
        property_count = 0
        vector_count = 0
        endpoints = self._require_endpoints()

        for node_type in self._schema.node_types:
            table = self._catalog.table(node_type.name)
            for page in _scan_pages(
                self._transaction, table.name, self._scan_batch_size
            ):
                entries: list[tuple[str, int, str]] = []
                for row in page.rows:
                    node = self._logical_node(table, node_type, row)
                    self._index.validate_node(node)
                    entries.append((table.name, row.record_id, node.key))
                    node_count += 1
                    property_count += len(node.properties)
                    vector_count += _vector_count(node.properties.values())
                endpoints.add_batch(entries)

        for layout in self._schema.relation_layouts:
            table = self._catalog.table(self._relationship_tables[layout.identity])
            for page in _scan_pages(
                self._transaction, table.name, self._scan_batch_size
            ):
                for row in page.rows:
                    relation = self._logical_relation(table, layout, row)
                    self._index.validate_relation(relation)
                    relation_count += 1
                    property_count += len(relation.properties)
                    vector_count += _vector_count(relation.properties.values())

        return LogicalCounts(
            nodes=node_count,
            relations=relation_count,
            properties=property_count,
            vectors=vector_count,
        )

    def _logical_node(self, table: Any, node_type: Any, row: Any) -> LogicalNode:
        properties = {
            prop.name: _logical_value(
                row.values[table.column_index(prop.name)],
                prop,
                self._catalog,
            )
            for prop in node_type.properties
        }
        key = properties[node_type.key]
        if type(key) is not str:
            raise LogicalSchemaError(
                "Grafx node key is not a string",
                detail=f"{node_type.name}.{node_type.key}: {type(key).__name__}",
            )
        return LogicalNode(node_type.name, key, properties)

    def _logical_relation(self, table: Any, layout: Any, row: Any) -> LogicalRelation:
        if len(row.values) != len(table.columns):
            raise LogicalSchemaError(
                "Grafx relationship row arity differs from its catalog table",
                detail=table.name,
            )
        source_record_id, target_record_id = row.values[:2]
        if type(source_record_id) is not int or type(target_record_id) is not int:
            raise LogicalSchemaError(
                "Grafx relationship endpoint is not a record id",
                detail=table.name,
            )
        endpoints = self._require_endpoints()
        source_key = endpoints.resolve(layout.source_type, source_record_id)
        target_key = endpoints.resolve(layout.target_type, target_record_id)
        properties = {
            prop.name: _logical_value(
                row.values[table.column_index(prop.name)],
                prop,
                self._catalog,
            )
            for prop in layout.properties
        }
        return LogicalRelation(
            layout.name,
            layout.source_type,
            layout.target_type,
            source_key,
            target_key,
            properties,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise LogicalSchemaError("Grafx logical snapshot is closed")

    def _require_batch_size(self, batch_size: int) -> None:
        self._require_open()
        if type(batch_size) is not int or batch_size < 1:
            raise LogicalSchemaError(
                "Grafx logical snapshot batch_size must be a positive int",
                detail=repr(batch_size),
            )

    def _require_endpoints(self) -> _EndpointMap:
        if self._endpoints is None:
            raise LogicalSchemaError("Grafx endpoint map is unavailable")
        return self._endpoints


class _EndpointMap:
    """Temporary SQLite mapping whose memory is independent of node cardinality."""

    def __init__(self, parent: Path | None) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix="okto-pulse-grafx-endpoints-",
            dir=parent,
        )
        self.path = Path(self._temporary.name) / "endpoints.sqlite3"
        self._connection: sqlite3.Connection | None = None
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA synchronous=OFF")
            self._connection.execute("PRAGMA cache_size=-1024")
            self._connection.execute(
                "CREATE TABLE endpoints ("
                "node_table TEXT NOT NULL, "
                "record_id INTEGER NOT NULL, "
                "logical_key TEXT NOT NULL, "
                "PRIMARY KEY (node_table, record_id)"
                ") WITHOUT ROWID"
            )
            self._connection.commit()
        except BaseException as failure:
            if self._connection is not None:
                try:
                    self._connection.close()
                except BaseException as cleanup_failure:  # noqa: BLE001
                    failure.add_note(
                        "endpoint SQLite close also failed: "
                        f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                    )
            try:
                self._temporary.cleanup()
            except BaseException as cleanup_failure:  # noqa: BLE001
                failure.add_note(
                    "endpoint temp cleanup also failed: "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                )
            raise
        self._closed = False
        self._cleanup_complete = False

    def add_batch(self, entries: Sequence[tuple[str, int, str]]) -> None:
        if self._closed:
            raise LogicalSchemaError("Grafx endpoint map is closed")
        if not entries:
            return
        connection = self._require_connection()
        connection.executemany(
            "INSERT INTO endpoints(node_table, record_id, logical_key) VALUES (?, ?, ?)",
            entries,
        )
        connection.commit()

    def resolve(self, node_table: str, record_id: int) -> str:
        if self._closed:
            raise LogicalSchemaError("Grafx endpoint map is closed")
        found = (
            self._require_connection()
            .execute(
                "SELECT logical_key FROM endpoints "
                "WHERE node_table = ? AND record_id = ?",
                (node_table, record_id),
            )
            .fetchone()
        )
        if found is None:
            raise LogicalSchemaError(
                "Grafx relationship references a missing physical endpoint",
                detail=f"{node_table}#{record_id}",
            )
        return str(found[0])

    def close(self) -> None:
        if self._cleanup_complete:
            return
        self._closed = True
        primary_failure: BaseException | None = None
        try:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
        except BaseException as failure:  # noqa: BLE001 - cleanup is signal-safe
            primary_failure = failure
        try:
            self._temporary.cleanup()
        except BaseException as failure:  # noqa: BLE001 - cleanup is signal-safe
            if primary_failure is None:
                primary_failure = failure
            else:
                primary_failure.add_note(
                    "Grafx endpoint temp cleanup also failed: "
                    f"{type(failure).__name__}: {failure}"
                )
        if primary_failure is not None:
            raise primary_failure
        self._cleanup_complete = True

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LogicalSchemaError("Grafx endpoint map connection is unavailable")
        return self._connection


def _scan_pages(
    transaction: Transaction,
    table_name: str,
    limit: int,
) -> Iterator[ScanPageV1]:
    cursor = None
    while True:
        page = transaction.scan_rows_v1(table_name, limit=limit, cursor=cursor)
        if len(page.rows) > limit:
            raise LogicalSchemaError(
                "Grafx physical scan exceeded its requested bound",
                detail=f"{table_name}: limit={limit} got={len(page.rows)}",
            )
        yield page
        cursor = page.next_cursor
        if cursor is None:
            return


def _validate_physical_schema(
    catalog: Any,
    schema: LogicalSchema,
    relationship_tables: Mapping[LayoutIdentity, str],
) -> None:
    expected_table_names = {node.name for node in schema.node_types} | set(
        relationship_tables.values()
    )
    observed_table_names = {table.name for table in catalog.tables()}
    if observed_table_names != expected_table_names:
        raise LogicalSchemaError(
            "Grafx catalog tables differ from the logical transfer scope",
            detail=(
                f"missing={sorted(expected_table_names - observed_table_names)} "
                f"extra={sorted(observed_table_names - expected_table_names)}"
            ),
        )

    expected_space_names = {space.name for space in schema.vector_spaces}
    observed_space_names = {space.name for space in catalog.spaces()}
    if observed_space_names != expected_space_names:
        raise LogicalSchemaError(
            "Grafx catalog vector spaces differ from the logical transfer scope",
            detail=(
                f"missing={sorted(expected_space_names - observed_space_names)} "
                f"extra={sorted(observed_space_names - expected_space_names)}"
            ),
        )

    for node_type in schema.node_types:
        table = catalog.table(node_type.name)
        if table.kind != "node" or table.primary_key != node_type.key:
            raise LogicalSchemaError(
                "Grafx node table identity differs from the logical schema",
                detail=node_type.name,
            )
        _validate_columns(table.columns, node_type.properties, schema, table.name)

    for layout in schema.relation_layouts:
        table = catalog.table(relationship_tables[layout.identity])
        if (
            table.kind != "rel"
            or table.from_table != layout.source_type
            or table.to_table != layout.target_type
        ):
            raise LogicalSchemaError(
                "Grafx relationship table endpoints differ from the logical layout",
                detail=table.name,
            )
        _validate_columns(
            table.property_columns,
            layout.properties,
            schema,
            table.name,
        )

    for logical_space in schema.vector_spaces:
        physical_space = catalog.space(logical_space.name)
        observed_metric = getattr(physical_space.metric, "value", physical_space.metric)
        if (
            physical_space.dimension != logical_space.dimension
            or observed_metric != logical_space.metric
            or physical_space.normalized is not logical_space.normalized
            or physical_space.storage_dtype != logical_space.storage_dtype
        ):
            raise LogicalSchemaError(
                "Grafx vector space geometry differs from the logical schema",
                detail=logical_space.name,
            )


def _validate_columns(
    physical_columns: Sequence[Any],
    logical_properties: Sequence[Any],
    schema: LogicalSchema,
    owner: str,
) -> None:
    if tuple(column.name for column in physical_columns) != tuple(
        prop.name for prop in logical_properties
    ):
        raise LogicalSchemaError(
            "Grafx column order differs from the logical schema",
            detail=owner,
        )
    for column, prop in zip(physical_columns, logical_properties, strict=True):
        expected_type = _expected_grafx_type(prop, schema)
        if column.type.name != expected_type or column.nullable is not prop.nullable:
            raise LogicalSchemaError(
                "Grafx column type/nullability differs from the logical schema",
                detail=f"{owner}.{prop.name}",
            )
        if column.vector_space != prop.vector_space:
            raise LogicalSchemaError(
                "Grafx vector column names a different logical space",
                detail=f"{owner}.{prop.name}",
            )


def _expected_grafx_type(prop: Any, schema: LogicalSchema) -> str:
    if prop.type != "vector":
        return _GRAFX_SCALAR_TYPES[prop.type]
    space = schema.vector_space(prop.vector_space)
    if space.storage_dtype == "float32":
        return "VECTOR_F32"
    if space.storage_dtype == "float64":
        return "VECTOR_F64"
    raise LogicalSchemaError(
        "Grafx adapter does not support the logical vector storage dtype",
        detail=f"{space.name}: {space.storage_dtype}",
    )


def _logical_value(value: object, prop: Any, catalog: Any) -> LogicalValue:
    if value is None:
        return LOGICAL_NULL
    if prop.type == "bool" and type(value) is bool:
        return value
    if prop.type == "int64" and type(value) is int:
        return value
    if prop.type == "float64" and type(value) is float:
        return value
    if prop.type == "string" and type(value) is str:
        return value

    grafx_timestamp, grafx_vector = _grafx_value_classes()
    if prop.type == "timestamp_us" and type(value) is grafx_timestamp:
        return LogicalTimestamp(value.micros)
    if prop.type == "vector" and type(value) is grafx_vector:
        space = catalog.space(prop.vector_space)
        if value.space_ref != space.space_id or value.dtype != space.storage_dtype:
            raise LogicalSchemaError(
                "Grafx vector value disagrees with its catalog space",
                detail=prop.vector_space,
            )
        return LogicalVector(prop.vector_space, value.dtype, value.values)
    raise LogicalSchemaError(
        "Grafx physical value does not match its logical property type",
        detail=f"{prop.name}: expected={prop.type} got={type(value).__name__}",
    )


@cache
def _grafx_value_classes() -> tuple[type, type]:
    from okto_grafx import Timestamp, VectorValue

    return Timestamp, VectorValue


def _vector_count(values: Iterable[LogicalValue]) -> int:
    return sum(type(value) is LogicalVector for value in values)


def _rollback_preserving(transaction: Transaction, failure: BaseException) -> None:
    try:
        if transaction.active:
            transaction.rollback()
    except BaseException as cleanup_failure:  # noqa: BLE001 - preserve signal
        failure.add_note(
            "Grafx snapshot rollback also failed: "
            f"{type(cleanup_failure).__name__}: {cleanup_failure}"
        )


__all__ = [
    "CommunityGrafxLogicalSnapshot",
    "CommunityGrafxLogicalSnapshotSource",
]
