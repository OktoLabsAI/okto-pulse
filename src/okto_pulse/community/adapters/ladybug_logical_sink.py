"""Write a logical graph into a NEW Ladybug generation, and certify it cold.

The sink writes out of place only.  It creates the candidate directory itself
and refuses one that already exists, so it never adopts a directory somebody
else owns -- and therefore never deletes one either.  Nothing here binds,
activates or retires a generation.

Three rules shape it, and each exists because of a way an import can look
finished while being wrong:

nothing is retained
    Writing accumulates a fingerprint and counters, never the records.  A sink
    that kept the graph in order to check itself later would hold the whole
    import in memory, which is exactly the bound the transfer promises not to
    break.

certify from cold
    ``certify`` closes the writing handles, reopens the candidate from scratch,
    and streams it back through an accumulator.  A certificate answered from
    the warm writer would prove the process still remembers what it wrote,
    which is not the question.  If the handles cannot be closed, nothing is
    certified: an unflushed writer would make the reopen a lie.

absent is refused, never softened
    A fixed-schema table has a slot for every declared column and no state
    meaning "never set", so writing NULL for an omitted property would promote
    "never set" into "set to null".  The import refuses, the candidate is
    abandoned, and finalize never runs.

Cleanup can fail, and when it does it says so.  ``abort`` records itself as
done only once the handles closed and the tree was actually removed, so a
caller can retry instead of inheriting an orphaned directory it was told was
gone.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from okto_pulse.core.kg.logical_transfer import (
    CandidateCertificate,
    LogicalFingerprintAccumulator,
    LogicalNode,
    LogicalNodeType,
    LogicalNull,
    LogicalPropertyDef,
    LogicalRelation,
    LogicalRelationLayout,
    LogicalSchema,
    LogicalSchemaError,
    LogicalTimestamp,
    LogicalValue,
    LogicalVector,
)

from okto_pulse.community.adapters.filesystem_erasure import remove_contained_tree
from okto_pulse.community.adapters.logical_transfer_values import require_representable

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=timezone.utc)

_LOGICAL_TO_PULSE: Final[dict[str, str]] = {
    "string": "STRING",
    "int64": "INT64",
    "float64": "DOUBLE",
    "timestamp_us": "TIMESTAMP",
    "bool": "BOOLEAN",
}


class LadybugSinkError(RuntimeError):
    """A Ladybug write failed. Native exceptions are translated into this."""


def _quote(identifier: str) -> str:
    if not identifier.isidentifier():
        raise LadybugSinkError(f"unsafe identifier {identifier!r}")
    return identifier


def logical_to_native(value: LogicalValue, declared: LogicalPropertyDef) -> Any:
    """Convert one logical value into the native Ladybug parameter."""

    if isinstance(value, LogicalNull):
        return None
    if isinstance(value, LogicalTimestamp):
        return _EPOCH + timedelta(microseconds=value.micros)
    if isinstance(value, LogicalVector):
        if value.space_name != declared.vector_space:
            raise LogicalSchemaError(
                "vector belongs to a different space than its property declares",
                detail=f"{declared.name}: {value.space_name}",
            )
        return [float(component) for component in value.components]
    return value


def schema_ddl(schema: LogicalSchema) -> list[str]:
    """Render the DDL for a scope, node tables before relation tables."""

    dimensions = {space.name: space.dimension for space in schema.vector_spaces}
    statements: list[str] = []
    for node_type in schema.node_types:
        columns = ", ".join(
            f"{_quote(prop.name)} {_physical_type(prop, dimensions)}"
            for prop in node_type.properties
        )
        statements.append(
            f"CREATE NODE TABLE {_quote(node_type.name)} "
            f"({columns}, PRIMARY KEY({_quote(node_type.key)}))"
        )
    # One physical table per relation NAME, carrying every endpoint pair that
    # name spans. Board has 69 concrete layouts over only 16 names, so emitting
    # one table per layout would try to create `supersedes` eleven times.
    by_name: dict[str, list[LogicalRelationLayout]] = {}
    for layout in schema.relation_layouts:
        by_name.setdefault(layout.name, []).append(layout)
    for name, layouts in by_name.items():
        first = layouts[0]
        for other in layouts[1:]:
            if [p.name for p in other.properties] != [p.name for p in first.properties]:
                raise LogicalSchemaError(
                    "layouts sharing a name declare different properties",
                    detail=name,
                )
        pairs = ", ".join(
            f"FROM {_quote(layout.source_type)} TO {_quote(layout.target_type)}"
            for layout in layouts
        )
        columns = "".join(
            f", {_quote(prop.name)} {_physical_type(prop, dimensions)}"
            for prop in first.properties
        )
        statements.append(f"CREATE REL TABLE {_quote(name)} ({pairs}{columns})")
    return statements


def _physical_type(prop: LogicalPropertyDef, dimensions: dict[str, int]) -> str:
    if prop.type == "vector":
        space = prop.vector_space or ""
        if space not in dimensions:
            raise LogicalSchemaError(
                "vector property names an undeclared space", detail=prop.name
            )
        return f"DOUBLE[{dimensions[space]}]"
    physical = _LOGICAL_TO_PULSE.get(prop.type)
    if physical is None:
        raise LogicalSchemaError(
            "property type has no physical mapping", detail=prop.type
        )
    return physical


def expected_vector_indexes(
    schema: LogicalSchema,
) -> tuple[tuple[str, str, str, str], ...]:
    """Return (table, index_name, column, metric) for every vector property.

    Derived from the schema's own property->space mapping, so the indexes that
    must exist are exactly the ones the scope declares -- not a count.
    """

    metrics = {space.name: space.metric for space in schema.vector_spaces}
    entries: list[tuple[str, str, str, str]] = []
    for node_type in schema.node_types:
        for prop in node_type.properties:
            if prop.type != "vector":
                continue
            space = prop.vector_space or ""
            if space not in metrics:
                raise LogicalSchemaError(
                    "vector property names an undeclared space", detail=prop.name
                )
            entries.append((node_type.name, space, prop.name, metrics[space]))
    return tuple(entries)


def _vector_index_already_exists(failure: BaseException) -> bool:
    normalized = str(failure).lower()
    return "already exists" in normalized and "index" in normalized


def _close(handle: Any, what: str) -> None:
    """Close one handle, reporting failure instead of hiding it."""

    if handle is None:
        return
    closer = getattr(handle, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception as failure:
        raise LadybugSinkError(f"closing the {what} failed: {failure}") from failure


def _vector_indexes_present(database: Any, schema: LogicalSchema) -> bool:
    """Ask the REOPENED database which vector indexes it actually has."""

    expected = {
        (table, index)
        for table, index, _column, _metric in expected_vector_indexes(schema)
    }
    if not expected:
        return True
    import ladybug

    connection = ladybug.Connection(database)
    try:
        # No INSTALL and no LOAD here. kg_runtime records that both are engine
        # writes in Ladybug 0.16, so loading would mutate and warm the very
        # candidate this is supposed to inspect cold. SHOW_INDEXES reads the
        # catalog and needs neither.
        result = connection.execute("CALL SHOW_INDEXES() RETURN *")
        observed: set[tuple[str, str]] = set()
        while result.has_next():
            row = result.get_next()
            if len(row) >= 2:
                observed.add((str(row[0]), str(row[1])))
    except Exception as failure:
        raise LadybugSinkError(
            f"reading the candidate indexes failed: {failure}"
        ) from failure
    finally:
        _close(connection, "index probe connection")
    return expected <= observed


class LadybugLogicalCandidateSink:
    """Accepts a logical graph into a new, empty, unbound Ladybug generation."""

    def __init__(
        self, candidate_path: str | Path, expected_schema: LogicalSchema
    ) -> None:
        self._path = Path(candidate_path)
        self._base = self._path.parent
        self._expected_schema = expected_schema
        self._database: Any = None
        self._connection: Any = None
        self._schema: LogicalSchema | None = None
        self._written = LogicalFingerprintAccumulator.for_schema(expected_schema)
        self._owns_path = False
        self._released = False
        self._finalized = False
        self._certificate: CandidateCertificate | None = None

    # -- lifecycle ---------------------------------------------------------

    def begin_candidate(self, schema: LogicalSchema) -> None:
        """Validate the schema, create the candidate, apply DDL before any data."""

        if self._database is not None:
            raise LadybugSinkError("the candidate is already open")
        # Checked BEFORE anything exists on disk: a schema this sink was not
        # built for must not leave a directory behind.
        if schema != self._expected_schema:
            raise LadybugSinkError(
                "candidate schema does not match the expected schema"
            )
        if self._path.exists():
            # Out of place means this sink creates the directory. Adopting one
            # that already exists would make abort delete somebody else's tree.
            raise LadybugSinkError(f"candidate path already exists: {self._path}")
        import ladybug

        try:
            # parents=False: the candidate is created, its surroundings are
            # somebody else's and must already exist.
            self._path.mkdir(parents=False, exist_ok=False)
        except OSError as failure:
            raise LadybugSinkError(
                f"creating the candidate directory failed: {failure}"
            ) from failure
        # Ownership only after the directory is ours, so abort never removes a
        # tree this sink did not create.
        self._owns_path = True
        try:
            self._database = ladybug.Database(str(self._path / "db"))
            self._connection = ladybug.Connection(self._database)
            for statement in schema_ddl(schema):
                self._connection.execute(statement)
            # A DOUBLE[n] column is storage, not a searchable space. Without
            # the index the candidate would certify while answering no vector
            # query, and the cosine semantics would exist only on paper.
            from okto_pulse.community.adapters.kg_runtime import (
                load_vector_extension,
            )

            load_vector_extension(self._connection, install=True)
            for table, index, column, metric in expected_vector_indexes(schema):
                try:
                    self._connection.execute(
                        f"CALL CREATE_VECTOR_INDEX("
                        f"'{table}', '{index}', '{column}', "
                        f"metric := '{metric}')"
                    )
                except Exception as failure:
                    # Only a proven already-exists outcome may pass; anything
                    # else must reach the caller so the candidate is abandoned.
                    if not _vector_index_already_exists(failure):
                        raise
        except LogicalSchemaError:
            raise
        except Exception as failure:
            raise LadybugSinkError(
                f"creating the candidate failed: {failure}"
            ) from failure
        self._schema = schema

    def write_nodes(self, nodes: Sequence[LogicalNode]) -> None:
        schema = self._require_open()
        for node in nodes:
            node_type = schema.node_type(node.type_name)
            require_representable(
                node_type.properties,
                node.properties,
                owner=f"{node.type_name}:{node.key}",
            )
            self._create_node(node_type, node)
            # Accumulated, not retained: the sink never holds the graph.
            self._written.add_node(node)

    def write_relations(self, relations: Sequence[LogicalRelation]) -> None:
        schema = self._require_open()
        for relation in relations:
            layout = schema.relation_layout(*relation.layout_identity)
            require_representable(
                layout.properties, relation.properties, owner=layout.name
            )
            self._create_relation(schema, layout, relation)
            self._written.add_relation(relation)

    def checkpoint(self) -> None:
        """Make everything written so far durable."""

        self._require_open()
        self._run("CHECKPOINT", {})

    def certify(self) -> CandidateCertificate:
        """Close the writer, reopen the candidate COLD, and stream it back."""

        schema = self._require_open()
        # A close that fails means the reopen would not be cold, so nothing is
        # certified: this raises rather than reporting on a half-flushed file.
        self._close_handles()
        from okto_pulse.community.adapters.ladybug_logical_source import (
            LadybugLogicalSnapshotSource,
        )

        import ladybug

        try:
            cold = ladybug.Database(str(self._path / "db"))
        except Exception as failure:
            raise LadybugSinkError(f"cold reopen failed: {failure}") from failure
        observed = LogicalFingerprintAccumulator.for_schema(schema)
        indexes_present = False
        try:
            indexes_present = _vector_indexes_present(cold, schema)
            snapshot = LadybugLogicalSnapshotSource(cold, schema).open_snapshot()
            try:
                counts = snapshot.counts()
                for batch in snapshot.iter_nodes(batch_size=500):
                    for node in batch:
                        observed.add_node(node)
                for batch in snapshot.iter_relations(batch_size=500):
                    for relation in batch:
                        observed.add_relation(relation)
            finally:
                snapshot.close()
        finally:
            _close(cold, "cold database")
        fingerprint = observed.digest()
        # ONE verdict, used for both the certificate and the finalize gate. Two
        # expressions drift: the earlier gate omitted the census, so a cold
        # count that disagreed produced verify_succeeded=False and still let
        # finalize through.
        verified = (
            indexes_present
            and fingerprint == self._written.digest()
            and counts == observed.counts()
            and counts == self._written.counts()
        )
        certificate = CandidateCertificate(
            cold_reopen_completed=True,
            verify_succeeded=verified,
            schema=schema,
            counts=counts,
            vector_spaces=tuple(space.name for space in schema.vector_spaces),
            fingerprint=fingerprint,
        )
        # The accepted certificate itself is the gate, so finalize cannot pass
        # on a weaker fact than the one the caller was shown.
        self._certificate = certificate if verified else None
        return certificate

    def finalize(self) -> None:
        """Accept the candidate. Only ever called after certification passed."""

        if self._finalized:
            return
        if self._certificate is None or not self._certificate.verify_succeeded:
            # Accepting without the passing certificate would make the whole
            # cold-reopen check advisory.
            raise LadybugSinkError(
                "the candidate was not certified; refusing to finalize"
            )
        self._close_handles()
        self._finalized = True
        self._released = True

    def abort(self) -> None:
        """Abandon the candidate, and only claim to have done so if it worked.

        A cleanup that failed must stay retryable.  Swallowing the error and
        recording success would hand the caller an orphaned directory it has
        been told is gone.
        """

        if self._released:
            return
        self._close_handles()
        if self._owns_path and self._path.exists():
            try:
                remove_contained_tree(self._path, base_dir=self._base)
            except Exception as failure:
                raise LadybugSinkError(
                    f"abandoning the candidate failed: {failure}"
                ) from failure
        self._owns_path = False
        self._released = True

    # -- internals ---------------------------------------------------------

    def _create_node(self, node_type: LogicalNodeType, node: LogicalNode) -> None:
        assignments = ", ".join(
            f"{_quote(prop.name)}: $p{index}"
            for index, prop in enumerate(node_type.properties)
        )
        parameters = {
            f"p{index}": logical_to_native(node.properties[prop.name], prop)
            for index, prop in enumerate(node_type.properties)
        }
        self._run(f"CREATE (:{_quote(node_type.name)} {{{assignments}}})", parameters)

    def _create_relation(
        self,
        schema: LogicalSchema,
        layout: LogicalRelationLayout,
        relation: LogicalRelation,
    ) -> None:
        source_key = _quote(schema.node_type(layout.source_type).key)
        target_key = _quote(schema.node_type(layout.target_type).key)
        parameters: dict[str, Any] = {
            "src": relation.source_key,
            "dst": relation.target_key,
        }
        assignments = ""
        if layout.properties:
            pairs = []
            for index, prop in enumerate(layout.properties):
                parameters[f"p{index}"] = logical_to_native(
                    relation.properties[prop.name], prop
                )
                pairs.append(f"{_quote(prop.name)}: $p{index}")
            assignments = " {" + ", ".join(pairs) + "}"
        self._run(
            f"MATCH (a:{_quote(layout.source_type)} {{{source_key}: $src}}), "
            f"(b:{_quote(layout.target_type)} {{{target_key}: $dst}}) "
            f"CREATE (a)-[:{_quote(layout.name)}{assignments}]->(b)",
            parameters,
        )

    def _run(self, query: str, parameters: dict[str, Any]) -> None:
        try:
            if parameters:
                self._connection.execute(query, parameters)
            else:
                self._connection.execute(query)
        except Exception as failure:
            raise LadybugSinkError(f"write failed: {failure}") from failure

    def _require_open(self) -> LogicalSchema:
        if self._schema is None or self._connection is None:
            raise LadybugSinkError("the candidate is not open")
        return self._schema

    def _close_handles(self) -> None:
        """Close both handles, keeping whichever one is still open.

        A reference is cleared only after that handle actually closed. Zeroing
        both first meant a connection that failed to close was forgotten, the
        database was never attempted, and a retry had nothing left to try.
        """

        failures: list[str] = []
        for attribute, what in (
            ("_connection", "candidate connection"),
            ("_database", "candidate database"),
        ):
            handle = getattr(self, attribute)
            if handle is None:
                continue
            try:
                _close(handle, what)
            except LadybugSinkError as failure:
                failures.append(str(failure))
                continue
            setattr(self, attribute, None)
        if failures:
            raise LadybugSinkError("; ".join(failures))


__all__ = [
    "LadybugLogicalCandidateSink",
    "LadybugSinkError",
    "logical_to_native",
    "schema_ddl",
]
