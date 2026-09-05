"""Out-of-place Grafx candidate sink for portable logical graph transfers.

The sink owns only a path that did not exist before ``begin_candidate``.  It
never binds that path into a live Pulse runtime: M-PULSE-5 builds and certifies
an unbound generation, while routing and activation remain M-PULSE-6 concerns.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    CandidateCertificate,
    LayoutIdentity,
    LogicalFingerprintAccumulator,
    LogicalNode,
    LogicalRelation,
    LogicalSchema,
    LogicalSchemaError,
    LogicalSchemaIndex,
    LogicalTimestamp,
    LogicalValue,
    LogicalVector,
)

from okto_pulse.community.adapters.filesystem_erasure import remove_contained_tree
from okto_pulse.community.adapters.logical_transfer_grafx import (
    CommunityGrafxLogicalSnapshotSource,
)

if TYPE_CHECKING:
    from okto_grafx import Database, Transaction


_DEFAULT_BATCH_SIZE = 500
_DEFAULT_PAGE_SIZE = 8192
# A logical candidate is a fresh, unbound path created and exclusively owned by
# this sink.  Import finishes with an explicit checkpoint before the writer is
# closed and the candidate is cold-certified, so per-512-record automatic
# checkpoints only add repeated full flushes to bulk backfills.  Keep the
# record-based safety valve finite but well above ordinary candidate batches;
# leave Grafx's independent ``wal_max_bytes`` default untouched.
_CANDIDATE_CHECKPOINT_INTERVAL_RECORDS = 1_000_000
_MINIMUM_PULSE_PAGE_SIZE = 4096
_MAX_GRAFX_IDENTIFIER_LENGTH = 128
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_GRAFX_SCALAR_DDL = {
    "bool": "BOOL",
    "float64": "DOUBLE",
    "int64": "INT64",
    "string": "STRING",
    "timestamp_us": "TIMESTAMP",
}
_GRAFX_VECTOR_METRICS = {"cosine", "dot", "euclidean"}


class CommunityGrafxLogicalCandidateSink:
    """Build and cold-certify one fresh, unbound Grafx generation."""

    def __init__(
        self,
        candidate_path: str | Path,
        *,
        expected_schema: LogicalSchema,
        relationship_tables: Mapping[LayoutIdentity, str],
        max_batch_size: int = _DEFAULT_BATCH_SIZE,
        connect_options: Mapping[str, object] | None = None,
        connect_factory: Callable[..., Any] | None = None,
        temporary_parent: Path | None = None,
    ) -> None:
        path = _candidate_path(candidate_path)
        if not isinstance(expected_schema, LogicalSchema):
            raise LogicalSchemaError(
                "Grafx candidate requires an expected LogicalSchema",
                detail=type(expected_schema).__name__,
            )
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise LogicalSchemaError(
                "Grafx max_batch_size must be a positive int",
                detail=repr(max_batch_size),
            )
        options = dict(connect_options or {})
        if "read_only" in options:
            raise LogicalSchemaError(
                "Grafx candidate connection options cannot set read_only"
            )
        options.setdefault("page_size", _DEFAULT_PAGE_SIZE)
        options.setdefault(
            "checkpoint_interval_records",
            _CANDIDATE_CHECKPOINT_INTERVAL_RECORDS,
        )
        # Generation revalidation is safe only because begin_candidate refuses
        # pre-existing paths and this sink remains the sole owner until its
        # writer and cold verifier are closed.  Explicit caller policy wins.
        options.setdefault("descriptor_revalidation", "generation")
        page_size = options["page_size"]
        if type(page_size) is not int or page_size < _MINIMUM_PULSE_PAGE_SIZE:
            raise LogicalSchemaError(
                "Grafx Pulse candidates require page_size >= 4096",
                detail=repr(page_size),
            )

        self._path = path
        self._base_dir = path.parent
        self._expected_schema = expected_schema
        self._relationship_input = dict(relationship_tables)
        self._max_batch_size = max_batch_size
        self._connect_options = MappingProxyType(options)
        self._connect_factory = connect_factory
        self._temporary_parent = temporary_parent
        self._schema: LogicalSchema | None = None
        self._index: LogicalSchemaIndex | None = None
        self._relationship_tables: Mapping[LayoutIdentity, str] | None = None
        self._database: Database | None = None
        self._cold_database: Database | None = None
        self._owns_path = False
        self._begun = False
        self._checkpointed = False
        self._certificate: CandidateCertificate | None = None
        self._finalized = False
        self._abort_complete = False

    @property
    def candidate_path(self) -> Path:
        """Return the canonical, still-unbound candidate path."""

        return self._path

    def begin_candidate(self, schema: LogicalSchema) -> None:
        if self._begun:
            raise LogicalSchemaError("Grafx logical candidate was already begun")
        if not isinstance(schema, LogicalSchema):
            raise LogicalSchemaError(
                "Grafx logical candidate requires a LogicalSchema",
                detail=type(schema).__name__,
            )
        if schema != self._expected_schema:
            raise LogicalSchemaError(
                "Grafx candidate schema does not match its fixed Pulse schema",
                detail=f"expected={self._expected_schema.scope} got={schema.scope}",
            )
        relationship_tables = _validated_relationship_tables(
            schema, self._relationship_input
        )
        _require_representable_schema(schema)
        if self._path.exists():
            raise LogicalSchemaError(
                "Grafx logical candidate path already exists",
                detail=str(self._path),
            )
        if not self._path.parent.is_dir():
            raise LogicalSchemaError(
                "Grafx logical candidate parent directory does not exist",
                detail=str(self._path.parent),
            )

        # mkdir is the ownership boundary.  From this point onward abort may
        # remove only this exact directory; a pre-existing path is never adopted.
        self._path.mkdir()
        self._owns_path = True
        self._begun = True
        self._schema = schema
        self._index = LogicalSchemaIndex.build(schema)
        self._relationship_tables = relationship_tables
        database = self._connect(read_only=False)
        self._database = database
        if not database.catalog.catalog.is_empty():
            raise LogicalSchemaError(
                "new Grafx logical candidate is not empty",
                detail=str(self._path),
            )
        # This is the only safe point for the one-way catalog activation: the
        # sink has proved the path did not exist, created it itself and proved
        # its catalog empty.  Activate v2 before the first DDL so every PK and
        # relationship endpoint index created below receives persisted catalog
        # authority from birth.  Existing databases never pass this ownership
        # boundary, and any activation failure is handled by transfer abort.
        database.ensure_identity_indexes()
        _create_physical_schema(database, schema, relationship_tables)

    def write_nodes(self, nodes: Sequence[LogicalNode]) -> None:
        self._require_writable_batch(nodes, "nodes")
        if not nodes:
            return
        schema = self._require_schema()
        index = self._require_index()
        database = self._require_database()

        def write(transaction: Transaction) -> None:
            for node in nodes:
                index.validate_node(node)
                node_type = schema.node_type(node.type_name)
                _require_complete_properties(
                    node.properties,
                    tuple(prop.name for prop in node_type.properties),
                    f"node {node.type_name}:{node.key}",
                )
                parameters = {
                    f"p{position}": _native_value(
                        node.properties[prop.name], prop, database
                    )
                    for position, prop in enumerate(node_type.properties)
                }
                statement = _node_insert_statement(node_type)
                result = transaction.execute(statement, parameters)
                if result.rows != ((node.key,),):
                    raise LogicalSchemaError(
                        "Grafx node import did not create exactly its logical key",
                        detail=f"{node.type_name}:{node.key}",
                    )

        _write_batch(database, write)

    def write_relations(self, relations: Sequence[LogicalRelation]) -> None:
        self._require_writable_batch(relations, "relations")
        if not relations:
            return
        schema = self._require_schema()
        index = self._require_index()
        database = self._require_database()
        relationship_tables = self._require_relationship_tables()

        def write(transaction: Transaction) -> None:
            for relation in relations:
                index.validate_relation(relation)
                layout = schema.relation_layout(*relation.layout_identity)
                _require_complete_properties(
                    relation.properties,
                    tuple(prop.name for prop in layout.properties),
                    (
                        f"relation {layout.name}"
                        f"({layout.source_type}->{layout.target_type})"
                    ),
                )
                parameters: dict[str, object] = {
                    "source_key": relation.source_key,
                    "target_key": relation.target_key,
                }
                parameters.update(
                    {
                        f"p{position}": _native_value(
                            relation.properties[prop.name], prop, database
                        )
                        for position, prop in enumerate(layout.properties)
                    }
                )
                statement = _relation_insert_statement(
                    layout,
                    relationship_tables[layout.identity],
                    schema,
                )
                result = transaction.execute(statement, parameters)
                expected = ((relation.source_key, relation.target_key),)
                if result.rows != expected:
                    raise LogicalSchemaError(
                        "Grafx relation import did not resolve exactly two endpoints",
                        detail=(
                            f"{layout.name}:"
                            f"{relation.source_key}->{relation.target_key}"
                        ),
                    )

        _write_batch(database, write)

    def checkpoint(self) -> None:
        self._require_active_candidate()
        if self._checkpointed:
            raise LogicalSchemaError("Grafx logical candidate was already checkpointed")
        self._require_database().checkpoint()
        self._checkpointed = True

    def certify(self) -> CandidateCertificate:
        self._require_active_candidate()
        if self._certificate is not None:
            return self._certificate
        if not self._checkpointed:
            raise LogicalSchemaError(
                "Grafx logical candidate must checkpoint before certification"
            )

        self._close_writer()
        cold = self._connect(read_only=True)
        self._cold_database = cold
        snapshot = None
        primary_failure: BaseException | None = None
        certificate: CandidateCertificate | None = None
        try:
            verification = cold.verify("all")
            if verification.clean is not True:
                raise LogicalSchemaError(
                    "Grafx cold candidate verification was not clean",
                    detail=f"findings={len(verification.findings)}",
                )
            schema = self._require_schema()
            source = CommunityGrafxLogicalSnapshotSource(
                cold,
                schema=schema,
                relationship_tables=self._require_relationship_tables(),
                scan_batch_size=self._max_batch_size,
                temporary_parent=self._temporary_parent,
            )
            snapshot = source.open_snapshot()
            declared = snapshot.counts()
            accumulator = LogicalFingerprintAccumulator.for_schema(snapshot.schema())
            for batch in snapshot.iter_nodes(batch_size=self._max_batch_size):
                for node in batch:
                    accumulator.add_node(node)
            for batch in snapshot.iter_relations(batch_size=self._max_batch_size):
                for relation in batch:
                    accumulator.add_relation(relation)
            observed = accumulator.counts()
            if observed != declared:
                raise LogicalSchemaError(
                    "Grafx cold candidate census changed during certification",
                    detail=(
                        f"declared={declared.as_mapping()} "
                        f"observed={observed.as_mapping()}"
                    ),
                )
            certificate = CandidateCertificate(
                cold_reopen_completed=True,
                verify_succeeded=True,
                schema=snapshot.schema(),
                counts=observed,
                vector_spaces=tuple(
                    sorted(space.name for space in schema.vector_spaces)
                ),
                fingerprint=accumulator.digest(),
            )
        except BaseException as failure:  # noqa: BLE001 - close must preserve signal
            primary_failure = failure
        finally:
            if snapshot is not None:
                try:
                    snapshot.close()
                except BaseException as failure:  # noqa: BLE001
                    primary_failure = _combine_failure(
                        primary_failure,
                        failure,
                        "Grafx cold snapshot close also failed",
                    )
            try:
                self._close_cold()
            except BaseException as failure:  # noqa: BLE001
                primary_failure = _combine_failure(
                    primary_failure,
                    failure,
                    "Grafx cold database close also failed",
                )
        if primary_failure is not None:
            raise primary_failure
        if certificate is None:  # pragma: no cover - total above
            raise AssertionError("Grafx certification produced no certificate")
        self._certificate = certificate
        return certificate

    def finalize(self) -> None:
        self._require_active_candidate()
        if self._certificate is None:
            raise LogicalSchemaError(
                "Grafx logical candidate cannot finalize before certification"
            )
        self._finalized = True

    def abort(self) -> None:
        if self._abort_complete or self._finalized:
            return
        primary_failure: BaseException | None = None
        if self._database is not None:
            try:
                self._close_writer()
            except BaseException as failure:  # noqa: BLE001
                primary_failure = failure
        if self._cold_database is not None:
            try:
                self._close_cold()
            except BaseException as failure:  # noqa: BLE001
                primary_failure = _combine_failure(
                    primary_failure,
                    failure,
                    "Grafx abort cold close also failed",
                )
        if primary_failure is None and self._owns_path:
            try:
                remove_contained_tree(self._path, base_dir=self._base_dir)
                self._owns_path = False
            except BaseException as failure:  # noqa: BLE001
                primary_failure = failure
        if primary_failure is not None:
            raise primary_failure
        self._abort_complete = True

    def _connect(self, *, read_only: bool) -> Database:
        factory = self._connect_factory
        if factory is None:
            factory = _grafx_connect()
        options = dict(self._connect_options)
        options["read_only"] = read_only
        return factory(self._path, **options)

    def _close_writer(self) -> None:
        database = self._database
        if database is None:
            return
        try:
            database.close()
        finally:
            if database.close_complete:
                self._database = None
        if database.close_complete is not True:
            raise LogicalSchemaError("Grafx candidate writer close did not complete")

    def _close_cold(self) -> None:
        database = self._cold_database
        if database is None:
            return
        try:
            database.close()
        finally:
            if database.close_complete:
                self._cold_database = None
        if database.close_complete is not True:
            raise LogicalSchemaError("Grafx cold candidate close did not complete")

    def _require_active_candidate(self) -> None:
        if not self._begun:
            raise LogicalSchemaError("Grafx logical candidate was not begun")
        if self._abort_complete:
            raise LogicalSchemaError("Grafx logical candidate was aborted")
        if self._finalized:
            raise LogicalSchemaError("Grafx logical candidate was finalized")

    def _require_writable_batch(self, batch: Sequence[object], what: str) -> None:
        self._require_active_candidate()
        if self._checkpointed:
            raise LogicalSchemaError(
                "Grafx logical candidate cannot write after checkpoint"
            )
        if len(batch) > self._max_batch_size:
            raise LogicalSchemaError(
                f"Grafx logical candidate {what} batch exceeds its bound",
                detail=f"limit={self._max_batch_size} got={len(batch)}",
            )

    def _require_schema(self) -> LogicalSchema:
        if self._schema is None:
            raise LogicalSchemaError("Grafx logical candidate schema is unavailable")
        return self._schema

    def _require_index(self) -> LogicalSchemaIndex:
        if self._index is None:
            raise LogicalSchemaError("Grafx logical candidate index is unavailable")
        return self._index

    def _require_database(self) -> Database:
        if self._database is None:
            raise LogicalSchemaError("Grafx logical candidate writer is unavailable")
        return self._database

    def _require_relationship_tables(self) -> Mapping[LayoutIdentity, str]:
        if self._relationship_tables is None:
            raise LogicalSchemaError(
                "Grafx logical candidate relationship mapping is unavailable"
            )
        return self._relationship_tables


def _candidate_path(value: str | Path) -> Path:
    if isinstance(value, str) and value == ":memory:":
        raise LogicalSchemaError("Grafx logical candidates must be persistent")
    try:
        path = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as failure:
        raise LogicalSchemaError(
            "Grafx logical candidate path is invalid",
            detail=type(failure).__name__,
        ) from failure
    if not path.name:
        raise LogicalSchemaError("Grafx logical candidate path is too broad")
    return path


def _validated_relationship_tables(
    schema: LogicalSchema,
    relationship_tables: Mapping[LayoutIdentity, str],
) -> Mapping[LayoutIdentity, str]:
    copied = dict(relationship_tables)
    expected = {layout.identity for layout in schema.relation_layouts}
    if set(copied) != expected:
        raise LogicalSchemaError(
            "Grafx relationship table mapping does not match the logical schema",
            detail=(
                f"missing={sorted(expected - set(copied))} "
                f"extra={sorted(set(copied) - expected)}"
            ),
        )
    if len(set(copied.values())) != len(copied):
        raise LogicalSchemaError(
            "Grafx relationship table mapping reuses a physical table"
        )
    for identity, table in copied.items():
        if (
            type(identity) is not tuple
            or len(identity) != 3
            or any(not _valid_identifier(part) for part in identity)
            or not _valid_identifier(table)
        ):
            raise LogicalSchemaError(
                "Grafx relationship table mapping contains an invalid identifier"
            )
    return MappingProxyType(copied)


def _require_representable_schema(schema: LogicalSchema) -> None:
    if not schema.node_types:
        raise LogicalSchemaError("Grafx logical candidate schema has no node types")
    for node_type in schema.node_types:
        _require_identifier(node_type.name)
        _require_identifier(node_type.key)
        for prop in node_type.properties:
            _require_identifier(prop.name)
            if prop.name != node_type.key and prop.nullable is not True:
                raise LogicalSchemaError(
                    "Grafx fixed schema cannot represent a required non-key property",
                    detail=f"{node_type.name}.{prop.name}",
                )
        key = node_type.property_def(node_type.key)
        if key.type != "string" or key.nullable is not False:
            raise LogicalSchemaError(
                "Grafx logical node keys must be non-nullable strings",
                detail=f"{node_type.name}.{node_type.key}",
            )
    for layout in schema.relation_layouts:
        for part in layout.identity:
            _require_identifier(part)
        for prop in layout.properties:
            _require_identifier(prop.name)
            if prop.nullable is not True:
                raise LogicalSchemaError(
                    "Grafx fixed schema cannot represent a required relation property",
                    detail=f"{layout.name}.{prop.name}",
                )
    for space in schema.vector_spaces:
        _require_identifier(space.name)
        if space.metric not in _GRAFX_VECTOR_METRICS:
            raise LogicalSchemaError(
                "Grafx logical vector metric is unsupported",
                detail=f"{space.name}: {space.metric}",
            )
        if space.storage_dtype not in {"float32", "float64"}:
            raise LogicalSchemaError(
                "Grafx logical vector dtype is unsupported",
                detail=f"{space.name}: {space.storage_dtype}",
            )


def _create_physical_schema(
    database: Database,
    schema: LogicalSchema,
    relationship_tables: Mapping[LayoutIdentity, str],
) -> None:
    statements = (
        *(_space_ddl(space) for space in schema.vector_spaces),
        *(_node_ddl(node_type) for node_type in schema.node_types),
        *(
            _relation_ddl(layout, relationship_tables[layout.identity])
            for layout in schema.relation_layouts
        ),
    )

    def write(transaction: Transaction) -> None:
        for statement in statements:
            transaction.execute(statement)

    _write_batch(database, write)


def _space_ddl(space: Any) -> str:
    normalized = "true" if space.normalized else "false"
    return (
        f"CREATE VECTOR SPACE {space.name} "
        f"{{dimension: {space.dimension}, metric: '{space.metric}', "
        f"normalized: {normalized}, storage_dtype: '{space.storage_dtype}'}}"
    )


def _node_ddl(node_type: Any) -> str:
    columns = ", ".join(_property_ddl(prop) for prop in node_type.properties)
    return (
        f"CREATE NODE TABLE {node_type.name}"
        f"({columns}, PRIMARY KEY({node_type.key}))"
    )


def _relation_ddl(layout: Any, physical_table: str) -> str:
    properties = ", ".join(_property_ddl(prop) for prop in layout.properties)
    suffix = f", {properties}" if properties else ""
    return (
        f"CREATE REL TABLE {physical_table}"
        f"(FROM {layout.source_type} TO {layout.target_type}{suffix})"
    )


def _property_ddl(prop: Any) -> str:
    if prop.type == "vector":
        return f"{prop.name} VECTOR({prop.vector_space})"
    return f"{prop.name} {_GRAFX_SCALAR_DDL[prop.type]}"


def _node_insert_statement(node_type: Any) -> str:
    properties = ", ".join(
        f"{prop.name}: $p{position}"
        for position, prop in enumerate(node_type.properties)
    )
    return f"CREATE (n:{node_type.name} {{{properties}}}) " f"RETURN n.{node_type.key}"


def _relation_insert_statement(
    layout: Any,
    physical_table: str,
    schema: LogicalSchema,
) -> str:
    source_key = schema.node_type(layout.source_type).key
    target_key = schema.node_type(layout.target_type).key
    properties = ", ".join(
        f"{prop.name}: $p{position}" for position, prop in enumerate(layout.properties)
    )
    body = f" {{{properties}}}" if properties else ""
    return (
        f"MATCH (source:{layout.source_type} {{{source_key}: $source_key}}), "
        f"(target:{layout.target_type} {{{target_key}: $target_key}}) "
        f"CREATE (source)-[relation:{physical_table}{body}]->(target) "
        f"RETURN source.{source_key}, target.{target_key}"
    )


def _require_complete_properties(
    properties: Mapping[str, LogicalValue],
    expected_names: tuple[str, ...],
    owner: str,
) -> None:
    observed = set(properties)
    expected = set(expected_names)
    if observed != expected:
        raise LogicalSchemaError(
            "Grafx fixed schema cannot represent an absent logical property",
            detail=(
                f"{owner}: missing={sorted(expected - observed)} "
                f"extra={sorted(observed - expected)}"
            ),
        )


def _native_value(value: LogicalValue, prop: Any, database: Database) -> object:
    if value is LOGICAL_NULL:
        return None
    if prop.type in {"bool", "int64", "float64", "string"}:
        return value
    timestamp_class, vector_class = _grafx_value_classes()
    if prop.type == "timestamp_us" and isinstance(value, LogicalTimestamp):
        return timestamp_class(value.micros)
    if prop.type == "vector" and isinstance(value, LogicalVector):
        space = database.catalog.catalog.space(prop.vector_space)
        return vector_class(value.components, space.space_id, value.dtype)
    raise LogicalSchemaError(
        "logical value cannot be converted to Grafx",
        detail=f"{prop.name}: {type(value).__name__}",
    )


def _write_batch(database: Database, write: Callable[[Transaction], None]) -> None:
    transaction = database.begin("write")
    try:
        write(transaction)
        report = transaction.commit()
    except BaseException as failure:
        try:
            if transaction.active:
                transaction.rollback()
        except BaseException as cleanup_failure:  # noqa: BLE001
            failure.add_note(
                "Grafx logical candidate rollback also failed: "
                f"{type(cleanup_failure).__name__}: {cleanup_failure}"
            )
        raise
    if report.durable is not True or report.wrote is not True:
        raise LogicalSchemaError(
            "Grafx logical candidate commit was not durable",
            detail=f"durable={report.durable} wrote={report.wrote}",
        )


def _combine_failure(
    primary: BaseException | None,
    secondary: BaseException,
    label: str,
) -> BaseException:
    if primary is None:
        return secondary
    primary.add_note(f"{label}: {type(secondary).__name__}: {secondary}")
    return primary


def _valid_identifier(value: object) -> bool:
    return (
        type(value) is str
        and len(value) <= _MAX_GRAFX_IDENTIFIER_LENGTH
        and _IDENTIFIER.fullmatch(value) is not None
    )


def _require_identifier(value: object) -> None:
    if not _valid_identifier(value):
        raise LogicalSchemaError(
            "Grafx logical schema contains an unsafe physical identifier",
            detail=repr(value),
        )


def _grafx_connect() -> Callable[..., Database]:
    from okto_grafx import connect

    return connect


def _grafx_value_classes() -> tuple[type, type]:
    from okto_grafx import Timestamp, VectorValue

    return Timestamp, VectorValue


__all__ = ["CommunityGrafxLogicalCandidateSink"]
