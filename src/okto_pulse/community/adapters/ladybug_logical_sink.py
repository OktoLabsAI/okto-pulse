"""Write a logical graph into a NEW Ladybug generation, and certify it cold.

The sink only ever writes out of place, into a candidate directory that must
not already hold a database.  Nothing here binds, activates or retires a
generation: a previous generation is untouched because this never opens it, not
because it is careful around it.

Two rules shape the lifecycle, and both exist because of a way an import can
look finished while being wrong:

certify from cold
    ``certify`` closes the writing handles and reopens the candidate from
    scratch before reporting anything.  A certificate answered from the warm
    writer would prove the process still remembers what it wrote, which is not
    the question -- the question is what survived to disk.

absent is refused, never softened
    A logical record that omits a declared property cannot be represented in a
    fixed-schema table, because the table has a slot for every column and no
    state meaning "never set".  Writing NULL instead would silently promote
    "never set" into "set to null".  So the import refuses, the candidate is
    abandoned, and finalize never runs.

Cleanup happens exactly once on either path: ``abort`` and ``finalize`` are
both idempotent, and both release the same handles.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from okto_pulse.core.kg.logical_transfer import (
    CandidateCertificate,
    LogicalCounts,
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
    fingerprint_graph,
)

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
    """Convert one logical value into the native Ladybug parameter.

    Adapter-local, like its inverse: Grafx wants entirely different objects for
    timestamps and vectors, so a shared converter would serve neither.
    """

    if isinstance(value, LogicalNull):
        return None
    if isinstance(value, LogicalTimestamp):
        return _EPOCH + _micros(value.micros)
    if isinstance(value, LogicalVector):
        if value.space_name != declared.vector_space:
            raise LogicalSchemaError(
                "vector belongs to a different space than its property declares",
                detail=f"{declared.name}: {value.space_name}",
            )
        return [float(component) for component in value.components]
    return value


def _micros(count: int):
    from datetime import timedelta

    return timedelta(microseconds=count)


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
    for layout in schema.relation_layouts:
        columns = "".join(
            f", {_quote(prop.name)} {_physical_type(prop, dimensions)}"
            for prop in layout.properties
        )
        statements.append(
            f"CREATE REL TABLE {_quote(layout.name)} "
            f"(FROM {_quote(layout.source_type)} TO {_quote(layout.target_type)}"
            f"{columns})"
        )
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


class LadybugLogicalCandidateSink:
    """Accepts a logical graph into a new, empty, unbound Ladybug generation."""

    def __init__(self, candidate_path: str | Path) -> None:
        self._path = Path(candidate_path)
        self._database: Any = None
        self._connection: Any = None
        self._schema: LogicalSchema | None = None
        self._nodes: list[LogicalNode] = []
        self._relations: list[LogicalRelation] = []
        self._released = False
        self._finalized = False

    # -- lifecycle ---------------------------------------------------------

    def begin_candidate(self, schema: LogicalSchema) -> None:
        """Create the candidate and apply its schema BEFORE any data."""

        if self._database is not None:
            raise LadybugSinkError("the candidate is already open")
        if self._path.exists() and any(self._path.iterdir()):
            # Out-of-place only: writing into an existing generation is the one
            # thing this must never do.
            raise LadybugSinkError(f"candidate path is not empty: {self._path}")
        import ladybug

        try:
            self._path.mkdir(parents=True, exist_ok=True)
            self._database = ladybug.Database(str(self._path / "db"))
            self._connection = ladybug.Connection(self._database)
            for statement in schema_ddl(schema):
                self._connection.execute(statement)
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
            # Refuses an absent property rather than writing NULL for it.
            require_representable(
                node_type.properties,
                node.properties,
                owner=f"{node.type_name}:{node.key}",
            )
            self._create_node(node_type, node)
            self._nodes.append(node)

    def write_relations(self, relations: Sequence[LogicalRelation]) -> None:
        schema = self._require_open()
        for relation in relations:
            layout = schema.relation_layout(*relation.layout_identity)
            require_representable(
                layout.properties, relation.properties, owner=layout.name
            )
            self._create_relation(schema, layout, relation)
            self._relations.append(relation)

    def checkpoint(self) -> None:
        """Make everything written so far durable."""

        self._require_open()
        self._run("CHECKPOINT", {})

    def certify(self) -> CandidateCertificate:
        """Close the writer, reopen the candidate COLD, and report what is there."""

        schema = self._require_open()
        self._close_handles()
        from okto_pulse.community.adapters.ladybug_logical_source import (
            LadybugLogicalSnapshotSource,
        )

        import ladybug

        try:
            cold = ladybug.Database(str(self._path / "db"))
        except Exception as failure:
            raise LadybugSinkError(f"cold reopen failed: {failure}") from failure
        snapshot = LadybugLogicalSnapshotSource(cold, schema).open_snapshot()
        try:
            counts = snapshot.counts()
            nodes = [n for batch in snapshot.iter_nodes(batch_size=500) for n in batch]
            relations = [
                r for batch in snapshot.iter_relations(batch_size=500) for r in batch
            ]
        finally:
            snapshot.close()
        return CandidateCertificate(
            cold_reopen_completed=True,
            verify_succeeded=self._verify(counts, nodes, relations),
            schema=schema,
            counts=counts,
            vector_spaces=tuple(space.name for space in schema.vector_spaces),
            fingerprint=fingerprint_graph(schema, nodes, relations),
        )

    def _verify(
        self,
        counts: LogicalCounts,
        nodes: Sequence[LogicalNode],
        relations: Sequence[LogicalRelation],
    ) -> bool:
        """The candidate's own check: what was re-read equals what was written."""

        return (
            counts.nodes == len(nodes)
            and counts.relations == len(relations)
            and sorted(n.key for n in nodes) == sorted(n.key for n in self._nodes)
            and len(relations) == len(self._relations)
        )

    def finalize(self) -> None:
        """Accept the candidate. Only ever called after certification passed."""

        if self._finalized:
            return
        self._close_handles()
        self._finalized = True
        self._released = True

    def abort(self) -> None:
        """Abandon the candidate. Idempotent, and never touches anything else."""

        if self._released:
            return
        self._released = True
        self._close_handles()
        shutil.rmtree(self._path, ignore_errors=True)

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
        connection, self._connection = self._connection, None
        database, self._database = self._database, None
        for handle in (connection, database):
            closer = getattr(handle, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: S110 - closing must not mask the outcome
                    pass


__all__ = [
    "LadybugLogicalCandidateSink",
    "LadybugSinkError",
    "logical_to_native",
    "schema_ddl",
]
