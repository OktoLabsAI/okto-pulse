"""Shared, non-test support for the frozen M-PULSE-5 physical matrix.

The matrix deliberately reaches the adapters only through the four composition
factories.  Those imports stay inside the wrapper functions because the factory
change is merged independently from this test-only commit.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from okto_pulse.core.kg.logical_transfer import (
    LOGICAL_NULL,
    LogicalCounts,
    LogicalFingerprintAccumulator,
    LogicalNode,
    LogicalRelation,
    LogicalSchema,
    LogicalTimestamp,
    LogicalVector,
    count_graph,
    fingerprint_graph,
    transfer_logical_graph,
)

from okto_pulse.community.adapters.logical_transfer_schema import (
    board_logical_schema,
    global_logical_schema,
)


BACKENDS = ("ladybug", "grafx")
SCOPES = ("board", "global_discovery")
SAMPLE_MICROS = 1_787_878_923_456_789


@dataclass(frozen=True, slots=True)
class Corpus:
    schema: LogicalSchema
    nodes: tuple[LogicalNode, ...]
    relations: tuple[LogicalRelation, ...]

    @property
    def counts(self) -> LogicalCounts:
        return count_graph(self.nodes, self.relations)

    @property
    def fingerprint(self) -> str:
        return fingerprint_graph(self.schema, self.nodes, self.relations)


def schema_for(scope: str) -> LogicalSchema:
    if scope == "board":
        return board_logical_schema()
    if scope == "global_discovery":
        return global_logical_schema()
    raise AssertionError(f"unsupported matrix scope: {scope!r}")


def sample_value(
    prop: Any,
    schema: LogicalSchema,
    seed: int,
    *,
    empty_string: bool = False,
) -> Any:
    if prop.type == "string":
        return "" if empty_string else f"value-{seed}"
    if prop.type == "int64":
        return seed
    if prop.type == "float64":
        return float(seed) + 0.25
    if prop.type == "bool":
        return seed % 2 == 0
    if prop.type == "timestamp_us":
        return LogicalTimestamp(SAMPLE_MICROS + seed)
    if prop.type == "vector":
        space = schema.vector_space(prop.vector_space or "")
        components = [0.0] * space.dimension
        components[seed % space.dimension] = 1.0
        return LogicalVector(
            space_name=space.name,
            dtype=space.storage_dtype,
            components=tuple(components),
        )
    raise AssertionError(f"unsupported logical property type: {prop.type!r}")


def complete_node(
    schema: LogicalSchema,
    type_name: str,
    key: str,
    seed: int,
    *,
    null_nullable: bool = False,
) -> LogicalNode:
    node_type = schema.node_type(type_name)
    properties: dict[str, Any] = {}
    empty_written = False
    for position, prop in enumerate(node_type.properties):
        if prop.name == node_type.key:
            value: Any = key
        elif null_nullable and prop.nullable:
            value = LOGICAL_NULL
        else:
            write_empty = prop.type == "string" and not empty_written
            value = sample_value(
                prop,
                schema,
                seed + position,
                empty_string=write_empty,
            )
            empty_written = empty_written or write_empty
        properties[prop.name] = value
    return LogicalNode(type_name, key, properties)


def complete_relation(
    schema: LogicalSchema,
    layout: Any,
    source_key: str,
    target_key: str,
    seed: int,
) -> LogicalRelation:
    properties: dict[str, Any] = {}
    empty_written = False
    for position, prop in enumerate(layout.properties):
        write_empty = prop.type == "string" and not empty_written
        properties[prop.name] = sample_value(
            prop,
            schema,
            seed + position,
            empty_string=write_empty,
        )
        empty_written = empty_written or write_empty
    return LogicalRelation(
        layout.name,
        layout.source_type,
        layout.target_type,
        source_key,
        target_key,
        properties,
    )


def canonical_corpus(scope: str) -> Corpus:
    """Two complete rows/type, every layout, a self-loop and exact parallels."""

    schema = schema_for(scope)
    keys: dict[tuple[str, int], str] = {}
    nodes: list[LogicalNode] = []
    for type_position, node_type in enumerate(schema.node_types):
        for row in range(2):
            key = f"{node_type.name.lower()}-{row}"
            keys[(node_type.name, row)] = key
            nodes.append(
                complete_node(
                    schema,
                    node_type.name,
                    key,
                    type_position * 100 + row * 10,
                    null_nullable=row == 1,
                )
            )

    relations: list[LogicalRelation] = []
    self_relation: LogicalRelation | None = None
    for position, layout in enumerate(schema.relation_layouts):
        relation = complete_relation(
            schema,
            layout,
            keys[(layout.source_type, 0)],
            keys[(layout.target_type, 0)],
            1_000 + position * 10,
        )
        relations.append(relation)
        if layout.source_type == layout.target_type and self_relation is None:
            self_relation = relation
    if self_relation is None:
        raise AssertionError(f"{scope} has no self layout for the frozen corpus")
    relations.append(self_relation)

    corpus = Corpus(schema, tuple(nodes), tuple(relations))
    expected = {
        "board": LogicalCounts(24, 70, 1_468, 11),
        "global_discovery": LogicalCounts(8, 8, 63, 4),
    }[scope]
    if corpus.counts != expected:
        raise AssertionError(
            f"canonical {scope} corpus drifted: "
            f"{corpus.counts.as_mapping()} != {expected.as_mapping()}"
        )
    return corpus


def one_node_corpus(scope: str, *, key: str = "baseline") -> Corpus:
    schema = schema_for(scope)
    type_name = "Decision" if scope == "board" else "Topic"
    return Corpus(
        schema,
        (complete_node(schema, type_name, key, 7),),
        (),
    )


def dense_global_corpus(size: int = 25) -> Corpus:
    schema = global_logical_schema()
    nodes = tuple(
        complete_node(schema, "Topic", f"topic-{position:03d}", position)
        for position in range(size)
    )
    layout = schema.relation_layout("TOPIC_RELATES_TO", "Topic", "Topic")
    relations = tuple(
        complete_relation(
            schema,
            layout,
            "topic-000",
            "topic-001",
            2_000 + position,
        )
        for position in range(size)
    )
    return Corpus(schema, nodes, relations)


def portable_file_corpus() -> Corpus:
    from okto_pulse.core.kg.logical_transfer import (
        LogicalNodeType,
        LogicalPropertyDef,
        LogicalRelationLayout,
    )

    schema = LogicalSchema(
        scope="board",
        node_types=(
            LogicalNodeType(
                "Card",
                "id",
                (
                    LogicalPropertyDef("id", "string", nullable=False),
                    LogicalPropertyDef("title", "string"),
                ),
            ),
        ),
        relation_layouts=(LogicalRelationLayout("blocks", "Card", "Card"),),
    )
    nodes = (
        LogicalNode("Card", "c1", {"id": "c1", "title": ""}),
        LogicalNode("Card", "c2", {"id": "c2", "title": "second"}),
    )
    relations = (
        LogicalRelation("blocks", "Card", "Card", "c1", "c2"),
        LogicalRelation("blocks", "Card", "Card", "c1", "c2"),
        LogicalRelation("blocks", "Card", "Card", "c1", "c1"),
    )
    return Corpus(schema, nodes, relations)


class MaterializedSnapshot:
    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self.close_calls = 0

    def schema(self) -> LogicalSchema:
        return self.corpus.schema

    def counts(self) -> LogicalCounts:
        return self.corpus.counts

    def iter_nodes(self, *, batch_size: int) -> Iterator[Sequence[LogicalNode]]:
        yield from batches(self.corpus.nodes, batch_size)

    def iter_relations(self, *, batch_size: int) -> Iterator[Sequence[LogicalRelation]]:
        yield from batches(self.corpus.relations, batch_size)

    def close(self) -> None:
        self.close_calls += 1


class MaterializedSource:
    def __init__(self, corpus: Corpus) -> None:
        self.snapshot = MaterializedSnapshot(corpus)
        self.open_calls = 0

    def open_snapshot(self) -> MaterializedSnapshot:
        self.open_calls += 1
        return self.snapshot


def batches(records: tuple[Any, ...], batch_size: int) -> Iterator[tuple[Any, ...]]:
    for start in range(0, len(records), batch_size):
        yield records[start : start + batch_size]


def make_physical_source(
    backend: str,
    database: Any,
    *,
    scope: str,
    scan_batch_size: int = 500,
    temporary_parent: Path | None = None,
) -> Any:
    from okto_pulse.community.adapters import logical_transfer_factories

    if backend == "ladybug":
        return logical_transfer_factories.make_ladybug_logical_source(
            database, scope=scope
        )
    if backend == "grafx":
        return logical_transfer_factories.make_grafx_logical_source(
            database,
            scope=scope,
            scan_batch_size=scan_batch_size,
            temporary_parent=temporary_parent,
        )
    raise AssertionError(f"unsupported matrix backend: {backend!r}")


def make_physical_sink(
    backend: str,
    candidate_path: Path,
    *,
    scope: str,
    max_batch_size: int = 500,
    temporary_parent: Path | None = None,
) -> Any:
    from okto_pulse.community.adapters import logical_transfer_factories

    if backend == "ladybug":
        return logical_transfer_factories.make_ladybug_logical_sink(
            candidate_path, scope=scope
        )
    if backend == "grafx":
        return logical_transfer_factories.make_grafx_logical_sink(
            candidate_path,
            scope=scope,
            max_batch_size=max_batch_size,
            connect_options={"page_size": 8192},
            temporary_parent=temporary_parent,
        )
    raise AssertionError(f"unsupported matrix backend: {backend!r}")


def seed_generation(
    backend: str,
    candidate_path: Path,
    corpus: Corpus,
    *,
    batch_size: int = 500,
    temporary_parent: Path | None = None,
) -> Any:
    sink = make_physical_sink(
        backend,
        candidate_path,
        scope=corpus.schema.scope,
        max_batch_size=batch_size,
        temporary_parent=temporary_parent,
    )
    report = transfer_logical_graph(
        MaterializedSource(corpus), sink, batch_size=batch_size
    )
    if report.counts != corpus.counts or report.fingerprint != corpus.fingerprint:
        raise AssertionError("physical seed did not certify the requested corpus")
    return sink


def expected_ladybug_filename(scope: str) -> str:
    if scope == "board":
        from okto_pulse.community.adapters.kg_runtime import GRAPH_DB_FILENAME

        return GRAPH_DB_FILENAME
    from okto_pulse.community.adapters.global_discovery_runtime import (
        GLOBAL_DISCOVERY_FILENAME,
    )

    return GLOBAL_DISCOVERY_FILENAME


def generation_database_path(backend: str, generation: Path, scope: str) -> Path:
    if backend == "ladybug":
        return generation / expected_ladybug_filename(scope)
    return generation


def open_generation_database(
    backend: str,
    generation: Path,
    scope: str,
    *,
    read_only: bool,
) -> Any:
    if backend == "ladybug":
        import ladybug

        database = ladybug.Database(
            str(generation_database_path(backend, generation, scope)),
            read_only=read_only,
        )
        if not read_only:
            from okto_pulse.community.adapters.kg_runtime import (
                load_vector_extension,
            )

            connection = ladybug.Connection(database)
            try:
                load_vector_extension(connection, install=False)
            finally:
                connection.close()
        return database

    from okto_grafx import connect

    return connect(generation, page_size=8192, read_only=read_only)


def close_database(backend: str, database: Any) -> None:
    database.close()
    if backend == "grafx" and database.close_complete is not True:
        raise AssertionError("Grafx database did not finish closing")


def export_database(
    backend: str,
    database: Any,
    *,
    scope: str,
    batch_size: int = 7,
    temporary_parent: Path | None = None,
) -> Corpus:
    source = make_physical_source(
        backend,
        database,
        scope=scope,
        scan_batch_size=batch_size,
        temporary_parent=temporary_parent,
    )
    snapshot = source.open_snapshot()
    try:
        schema = snapshot.schema()
        declared = snapshot.counts()
        nodes = tuple(
            node
            for batch in snapshot.iter_nodes(batch_size=batch_size)
            for node in batch
        )
        relations = tuple(
            relation
            for batch in snapshot.iter_relations(batch_size=batch_size)
            for relation in batch
        )
    finally:
        snapshot.close()
    corpus = Corpus(schema, nodes, relations)
    if corpus.counts != declared:
        raise AssertionError("physical source census differs from its stream")
    return corpus


def export_generation(
    backend: str,
    generation: Path,
    *,
    scope: str,
    batch_size: int = 7,
    temporary_parent: Path | None = None,
) -> Corpus:
    database = open_generation_database(backend, generation, scope, read_only=True)
    try:
        return export_database(
            backend,
            database,
            scope=scope,
            batch_size=batch_size,
            temporary_parent=temporary_parent,
        )
    finally:
        close_database(backend, database)


def native_insert_node(
    backend: str,
    database: Any,
    schema: LogicalSchema,
    node: LogicalNode,
    *,
    property_names: Sequence[str] | None = None,
) -> None:
    node_type = schema.node_type(node.type_name)
    names = tuple(property_names or node.properties)
    declared = {prop.name: prop for prop in node_type.properties}
    if backend == "ladybug":
        import ladybug

        from okto_pulse.community.adapters.kg_runtime import load_vector_extension
        from okto_pulse.community.adapters.ladybug_logical_sink import (
            logical_to_native,
        )

        connection = ladybug.Connection(database)
        try:
            load_vector_extension(connection, install=False)
            assignments = ", ".join(
                f"{name}: $p{position}" for position, name in enumerate(names)
            )
            parameters = {
                f"p{position}": logical_to_native(node.properties[name], declared[name])
                for position, name in enumerate(names)
            }
            connection.execute(
                f"CREATE (:{node.type_name} {{{assignments}}})", parameters
            )
        finally:
            connection.close()
        return

    from okto_pulse.community.adapters.grafx_logical_sink import _native_value

    assignments = ", ".join(
        f"{name}: $p{position}" for position, name in enumerate(names)
    )
    parameters = {
        f"p{position}": _native_value(node.properties[name], declared[name], database)
        for position, name in enumerate(names)
    }
    transaction = database.begin("write")
    try:
        transaction.execute(
            f"CREATE (n:{node.type_name} {{{assignments}}}) "
            f"RETURN n.{node_type.key}",
            parameters,
        )
        transaction.commit()
    except BaseException:
        if transaction.active:
            transaction.rollback()
        raise


def insert_omitted_and_null_rows(
    backend: str,
    database: Any,
    schema: LogicalSchema,
) -> tuple[str, str, str]:
    node_type = schema.node_type("Topic")
    nullable = next(prop for prop in node_type.properties if prop.nullable)
    omitted = LogicalNode(
        node_type.name,
        "omitted",
        {node_type.key: "omitted"},
    )
    explicit = LogicalNode(
        node_type.name,
        "explicit",
        {node_type.key: "explicit", nullable.name: LOGICAL_NULL},
    )
    native_insert_node(
        backend,
        database,
        schema,
        omitted,
        property_names=(node_type.key,),
    )
    native_insert_node(
        backend,
        database,
        schema,
        explicit,
        property_names=(node_type.key, nullable.name),
    )
    return node_type.name, node_type.key, nullable.name


class ObservedSnapshot:
    def __init__(self, delegate: Any, *, fail_after_first_node_batch: bool = False):
        self.delegate = delegate
        self.fail_after_first_node_batch = fail_after_first_node_batch
        self.node_batch_sizes: list[int] = []
        self.relation_batch_sizes: list[int] = []
        self.close_calls = 0

    def schema(self) -> LogicalSchema:
        return self.delegate.schema()

    def counts(self) -> LogicalCounts:
        return self.delegate.counts()

    def iter_nodes(self, *, batch_size: int) -> Iterator[Sequence[LogicalNode]]:
        produced = False
        for batch in self.delegate.iter_nodes(batch_size=batch_size):
            self.node_batch_sizes.append(len(batch))
            yield batch
            if self.fail_after_first_node_batch and not produced:
                produced = True
                raise OSError("injected physical source write failure")

    def iter_relations(self, *, batch_size: int) -> Iterator[Sequence[LogicalRelation]]:
        for batch in self.delegate.iter_relations(batch_size=batch_size):
            self.relation_batch_sizes.append(len(batch))
            yield batch

    def close(self) -> None:
        self.close_calls += 1
        self.delegate.close()


class ObservedSource:
    def __init__(self, delegate: Any, *, fail_after_first_node_batch: bool = False):
        self.delegate = delegate
        self.fail_after_first_node_batch = fail_after_first_node_batch
        self.open_calls = 0
        self.snapshot: ObservedSnapshot | None = None

    def open_snapshot(self) -> ObservedSnapshot:
        self.open_calls += 1
        self.snapshot = ObservedSnapshot(
            self.delegate.open_snapshot(),
            fail_after_first_node_batch=self.fail_after_first_node_batch,
        )
        return self.snapshot


class ObservedSink:
    def __init__(self, delegate: Any, *, fail_on: str | None = None) -> None:
        self.delegate = delegate
        self.fail_on = fail_on
        self.abort_calls = 0
        self.finalize_calls = 0
        self.node_batch_sizes: list[int] = []
        self.relation_batch_sizes: list[int] = []

    @property
    def candidate_path(self) -> Path:
        return self.delegate.candidate_path

    @property
    def database_path(self) -> Path:
        return self.delegate.database_path

    def begin_candidate(self, schema: LogicalSchema) -> None:
        self.delegate.begin_candidate(schema)

    def write_nodes(self, nodes: Sequence[LogicalNode]) -> None:
        self.node_batch_sizes.append(len(nodes))
        self.delegate.write_nodes(nodes)
        if self.fail_on == "import":
            raise OSError("injected physical sink import failure")

    def write_relations(self, relations: Sequence[LogicalRelation]) -> None:
        self.relation_batch_sizes.append(len(relations))
        self.delegate.write_relations(relations)
        if self.fail_on == "import":
            raise OSError("injected physical sink import failure")

    def checkpoint(self) -> None:
        self.delegate.checkpoint()
        if self.fail_on == "checkpoint":
            raise OSError("injected physical sink checkpoint failure")

    def certify(self) -> Any:
        certificate = self.delegate.certify()
        if self.fail_on == "reopen":
            raise OSError("injected physical sink reopen failure")
        return certificate

    def finalize(self) -> None:
        self.finalize_calls += 1
        self.delegate.finalize()

    def abort(self) -> None:
        self.abort_calls += 1
        self.delegate.abort()


def contains_logical_record(value: Any, *, _seen: set[int] | None = None) -> bool:
    if isinstance(value, (LogicalNode, LogicalRelation)):
        return True
    if _seen is None:
        _seen = set()
    identity = id(value)
    if identity in _seen:
        return False
    _seen.add(identity)
    if isinstance(value, dict):
        return any(
            contains_logical_record(item, _seen=_seen)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_logical_record(item, _seen=_seen) for item in value)
    return False


def sink_retains_logical_records(sink: Any) -> bool:
    return any(contains_logical_record(value) for value in vars(sink).values())


def tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for entry in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
        relative = entry.relative_to(path).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        if entry.is_file():
            digest.update(entry.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def accumulate_snapshot(snapshot: Any, *, batch_size: int) -> tuple[LogicalCounts, str]:
    accumulator = LogicalFingerprintAccumulator.for_schema(snapshot.schema())
    for batch in snapshot.iter_nodes(batch_size=batch_size):
        for node in batch:
            accumulator.add_node(node)
    for batch in snapshot.iter_relations(batch_size=batch_size):
        for relation in batch:
            accumulator.add_relation(relation)
    return accumulator.counts(), accumulator.digest()


def endpoint_schema() -> LogicalSchema:
    from okto_pulse.core.kg.logical_transfer import (
        LogicalNodeType,
        LogicalPropertyDef,
        LogicalRelationLayout,
    )

    return LogicalSchema(
        scope="board",
        node_types=(
            LogicalNodeType(
                "A",
                "id",
                (
                    LogicalPropertyDef("id", "string", nullable=False),
                    LogicalPropertyDef("note", "string"),
                ),
            ),
            LogicalNodeType(
                "B",
                "id",
                (LogicalPropertyDef("id", "string", nullable=False),),
            ),
        ),
        relation_layouts=(
            LogicalRelationLayout(
                "links",
                "A",
                "B",
                (LogicalPropertyDef("note", "string"),),
            ),
        ),
    )


def endpoint_database(path: Path) -> Any:
    from okto_grafx import connect

    database = connect(path, page_size=512)
    with database.begin("write") as schema:
        schema.execute("CREATE NODE TABLE A(id STRING, note STRING, PRIMARY KEY(id))")
        schema.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
        schema.execute("CREATE REL TABLE A_links_B(FROM A TO B, note STRING)")
    with database.begin("write") as writer:
        writer.execute("CREATE (:A {id: 'a1', note: NULL})")
        writer.execute("CREATE (:B {id: 'b1'})")
        writer.execute(
            "MATCH (a:A {id: 'a1'}), (b:B {id: 'b1'}) "
            "CREATE (a)-[:A_links_B {note: ''}]->(b)"
        )
    return database


def failing_file_nodes() -> Iterator[LogicalNode]:
    yield portable_file_corpus().nodes[0]
    raise OSError("injected write failure")


def corrupt_artifact_payload(path: Path) -> None:
    """Change one same-length node string while leaving every JSON line valid."""

    payload = path.read_bytes()
    marker = b'"value-'
    if marker not in payload:
        raise AssertionError("portable artifact contains no corruption marker")
    path.write_bytes(payload.replace(marker, b'"xalue-', 1))
