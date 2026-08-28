"""Read one Ladybug database as a bounded, single-snapshot logical graph source.

Two mechanisms carry this module, and both were measured against
``ladybug==0.16.0`` rather than assumed, because assuming either one would
produce a source that looks correct and is not.

the snapshot
    All reads happen inside one ``BEGIN TRANSACTION READ ONLY`` on a dedicated
    connection.  Measured: a writer that commits during the scan does not enter
    the census, and becomes visible only once the transaction ends.  Without
    this, paging would walk a moving target and the export would describe a
    graph that never existed at any instant.

the bound
    Every page is its own keyset query with a ``LIMIT``.  Reading with
    ``has_next``/``get_next`` is NOT enough on its own: measured, an
    unrestricted query allocates the entire result natively inside
    ``execute()`` before the first row is read (+43 MiB with zero rows read on
    200k rows), and row-by-row reading merely drains a buffer that is already
    the size of the table.  Bounding the QUERY is what bounds the memory.

Nodes page by their declared primary key.  Relations cannot: the contract
requires preserving parallel relations with identical endpoints AND identical
properties, so endpoints are not a key.  They page by ``offset(ID(r))``, the
only per-relation identity available, which was measured stable inside the
snapshot -- 5000 indistinguishable relations paged to exactly 5000 unique,
strictly increasing, with a concurrent writer excluded.

Endpoints travel as LOGICAL keys because Cypher can return the endpoint's key
column in the same query.  This source therefore needs no physical-to-logical
endpoint map; that is a Grafx-side concern, not a shared one.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from typing import Any, Final

from okto_pulse.core.kg.logical_transfer import (
    LogicalCounts,
    LogicalNode,
    LogicalNodeType,
    LogicalPropertyDef,
    LogicalRelation,
    LogicalRelationLayout,
    LogicalSchema,
    LogicalSchemaError,
    LogicalTimestamp,
    LogicalValue,
    LogicalVector,
)

from okto_pulse.community.adapters.logical_transfer_values import (
    require_projected_columns,
    scalar_to_logical,
)


DEFAULT_BATCH_SIZE: Final[int] = 500
_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=timezone.utc)


class LadybugSourceError(RuntimeError):
    """A Ladybug read failed. Native exceptions are translated into this."""


def _quote(identifier: str) -> str:
    if not identifier.isidentifier():
        raise LadybugSourceError(f"unsafe identifier {identifier!r}")
    return identifier


def timestamp_to_logical(native: object, *, owner: str) -> LogicalTimestamp:
    """Convert Ladybug's ``datetime`` into whole microseconds.

    Adapter-local on purpose: Grafx wants its own Timestamp, so a shared
    converter would only be unpicked at both ends.
    """

    if not isinstance(native, datetime):
        raise LogicalSchemaError(
            "timestamp column did not return a datetime",
            detail=f"{owner}: {type(native).__name__}",
        )
    moment = native if native.tzinfo else native.replace(tzinfo=timezone.utc)
    return LogicalTimestamp(int((moment - _EPOCH).total_seconds() * 1_000_000))


def vector_to_logical(
    native: object, declared: LogicalPropertyDef, space_dtype: str, *, owner: str
) -> LogicalVector:
    """Convert Ladybug's list of floats into a space-named logical vector."""

    if not isinstance(native, list):
        raise LogicalSchemaError(
            "vector column did not return a list",
            detail=f"{owner}: {type(native).__name__}",
        )
    if declared.vector_space is None:
        raise LogicalSchemaError(
            "vector property declares no space", detail=f"{owner}.{declared.name}"
        )
    return LogicalVector(
        space_name=declared.vector_space,
        dtype=space_dtype,
        components=tuple(float(component) for component in native),
    )


class LadybugLogicalSnapshot:
    """One fixed read-only view of a Ladybug database."""

    def __init__(
        self, connection: Any, schema: LogicalSchema, *, owns_transaction: bool = True
    ) -> None:
        self._connection = connection
        self._schema = schema
        self._owns_transaction = owns_transaction
        self._closed = False
        self._space_dtypes = {s.name: s.storage_dtype for s in schema.vector_spaces}

    def schema(self) -> LogicalSchema:
        return self._schema

    def counts(self) -> LogicalCounts:
        """Census the snapshot with aggregate queries, not by walking it."""

        nodes = 0
        relations = 0
        properties = 0
        vectors = 0
        for node_type in self._schema.node_types:
            present = self._scalar(
                f"MATCH (n:{_quote(node_type.name)}) RETURN count(n)"
            )
            nodes += present
            properties += present * len(node_type.properties)
            vectors += self._count_vectors(node_type)
        for layout in self._schema.relation_layouts:
            present = self._count_relations(layout)
            relations += present
            properties += present * len(layout.properties)
        return LogicalCounts(
            nodes=nodes,
            relations=relations,
            properties=properties,
            vectors=vectors,
        )

    def _count_vectors(self, node_type: LogicalNodeType) -> int:
        total = 0
        for prop in node_type.properties:
            if prop.type != "vector":
                continue
            # A NULL vector column is a present property holding LOGICAL_NULL,
            # which is not a vector; only non-null ones count.
            total += self._scalar(
                f"MATCH (n:{_quote(node_type.name)}) "
                f"WHERE n.{_quote(prop.name)} IS NOT NULL RETURN count(n)"
            )
        return total

    def _count_relations(self, layout: LogicalRelationLayout) -> int:
        return self._scalar(
            f"MATCH (:{_quote(layout.source_type)})"
            f"-[r:{_quote(layout.name)}]->"
            f"(:{_quote(layout.target_type)}) RETURN count(r)"
        )

    def iter_nodes(
        self, *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> Iterator[Sequence[LogicalNode]]:
        for node_type in self._schema.node_types:
            yield from self._page_nodes(node_type, batch_size)

    def iter_relations(
        self, *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> Iterator[Sequence[LogicalRelation]]:
        for layout in self._schema.relation_layouts:
            yield from self._page_relations(layout, batch_size)

    def _page_nodes(
        self, node_type: LogicalNodeType, batch_size: int
    ) -> Iterator[Sequence[LogicalNode]]:
        columns = [prop.name for prop in node_type.properties]
        projection = ", ".join(f"n.{_quote(name)}" for name in columns)
        table = _quote(node_type.name)
        key = _quote(node_type.key)
        last: str | None = None
        while True:
            if last is None:
                query = (
                    f"MATCH (n:{table}) RETURN {projection} "
                    f"ORDER BY n.{key} LIMIT {batch_size}"
                )
                parameters: dict[str, Any] = {}
            else:
                query = (
                    f"MATCH (n:{table}) WHERE n.{key} > $last RETURN {projection} "
                    f"ORDER BY n.{key} LIMIT {batch_size}"
                )
                parameters = {"last": last}
            rows = self._rows(query, parameters)
            if not rows:
                return
            batch = [self._node(node_type, columns, row) for row in rows]
            yield batch
            last = batch[-1].key
            if len(rows) < batch_size:
                return

    def _node(
        self, node_type: LogicalNodeType, columns: list[str], row: Sequence[Any]
    ) -> LogicalNode:
        by_name = dict(zip(columns, row))
        key_value = by_name[node_type.key]
        owner = f"{node_type.name}:{key_value}"
        properties = {
            prop.name: self._value(prop, by_name[prop.name], owner=owner)
            for prop in node_type.properties
        }
        # A fixed-schema row HAS every column; projecting fewer would invent
        # `absent` for something the table physically stores.
        require_projected_columns(node_type.properties, properties, owner=owner)
        if not isinstance(key_value, str):
            raise LogicalSchemaError("node key column is not a string", detail=owner)
        return LogicalNode(
            type_name=node_type.name, key=key_value, properties=properties
        )

    def _page_relations(
        self, layout: LogicalRelationLayout, batch_size: int
    ) -> Iterator[Sequence[LogicalRelation]]:
        source_key = _quote(self._schema.node_type(layout.source_type).key)
        target_key = _quote(self._schema.node_type(layout.target_type).key)
        columns = [prop.name for prop in layout.properties]
        projection = "".join(f", r.{_quote(name)}" for name in columns)
        head = (
            f"MATCH (a:{_quote(layout.source_type)})"
            f"-[r:{_quote(layout.name)}]->"
            f"(b:{_quote(layout.target_type)})"
        )
        tail = (
            f" RETURN a.{source_key}, b.{target_key}, offset(ID(r)){projection} "
            f"ORDER BY offset(ID(r)) LIMIT {batch_size}"
        )
        last = -1
        while True:
            # Keyed on the internal offset because endpoints are NOT unique:
            # identical parallel relations are exactly what this must preserve.
            rows = self._rows(
                f"{head} WHERE offset(ID(r)) > $last{tail}", {"last": last}
            )
            if not rows:
                return
            batch = [self._relation(layout, columns, row) for row in rows]
            yield batch
            last = rows[-1][2]
            if len(rows) < batch_size:
                return

    def _relation(
        self, layout: LogicalRelationLayout, columns: list[str], row: Sequence[Any]
    ) -> LogicalRelation:
        source, target = row[0], row[1]
        owner = f"{layout.name}({layout.source_type}->{layout.target_type})"
        properties = {
            prop.name: self._value(prop, value, owner=owner)
            for prop, value in zip(layout.properties, row[3:])
        }
        del columns
        require_projected_columns(layout.properties, properties, owner=owner)
        if not isinstance(source, str) or not isinstance(target, str):
            raise LogicalSchemaError(
                "relation endpoint key is not a string", detail=owner
            )
        return LogicalRelation(
            layout_name=layout.name,
            source_type=layout.source_type,
            target_type=layout.target_type,
            source_key=source,
            target_key=target,
            properties=properties,
        )

    def _value(
        self, declared: LogicalPropertyDef, native: object, *, owner: str
    ) -> LogicalValue:
        if declared.type == "timestamp_us":
            if native is None:
                return scalar_to_logical(
                    LogicalPropertyDef(declared.name, "string"), None
                )
            return timestamp_to_logical(native, owner=f"{owner}.{declared.name}")
        if declared.type == "vector":
            if native is None:
                return scalar_to_logical(
                    LogicalPropertyDef(declared.name, "string"), None
                )
            space = declared.vector_space or ""
            return vector_to_logical(
                native,
                declared,
                self._space_dtypes.get(space, ""),
                owner=owner,
            )
        return scalar_to_logical(declared, native)

    def _rows(self, query: str, parameters: dict[str, Any]) -> list[Sequence[Any]]:
        """Run one bounded page and drain it. Never holds more than one page."""

        self._require_open()
        try:
            result = (
                self._connection.execute(query, parameters)
                if parameters
                else self._connection.execute(query)
            )
        except Exception as failure:
            raise LadybugSourceError(f"query failed: {failure}") from failure
        try:
            rows: list[Sequence[Any]] = []
            while result.has_next():
                rows.append(result.get_next())
            return rows
        except Exception as failure:
            raise LadybugSourceError(f"reading a page failed: {failure}") from failure
        finally:
            with_close = getattr(result, "close", None)
            if callable(with_close):
                with_close()

    def _scalar(self, query: str) -> int:
        rows = self._rows(query, {})
        if not rows:
            raise LadybugSourceError(f"aggregate returned nothing: {query}")
        return int(rows[0][0])

    def _require_open(self) -> None:
        if self._closed:
            raise LadybugSourceError("the snapshot is closed")

    def close(self) -> None:
        """End the read-only transaction exactly once, success or failure."""

        if self._closed:
            return
        self._closed = True
        if not self._owns_transaction:
            return
        try:
            self._connection.execute("COMMIT")
        except Exception as failure:
            raise LadybugSourceError(
                f"ending the snapshot failed: {failure}"
            ) from failure


class LadybugLogicalSnapshotSource:
    """Opens the single read-only snapshot a transfer reads from."""

    def __init__(self, database: Any, schema: LogicalSchema) -> None:
        self._database = database
        self._schema = schema

    def open_snapshot(self) -> LadybugLogicalSnapshot:
        """Open one snapshot. A transfer calls this exactly once."""

        import ladybug

        try:
            connection = ladybug.Connection(self._database)
            connection.execute("BEGIN TRANSACTION READ ONLY")
        except Exception as failure:
            raise LadybugSourceError(
                f"opening the snapshot failed: {failure}"
            ) from failure
        return LadybugLogicalSnapshot(connection, self._schema)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "LadybugLogicalSnapshot",
    "LadybugLogicalSnapshotSource",
    "LadybugSourceError",
    "timestamp_to_logical",
    "vector_to_logical",
]
