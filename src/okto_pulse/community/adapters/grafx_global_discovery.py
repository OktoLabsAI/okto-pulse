"""Inactive Grafx vector support for the Global Discovery binding.

M-PULSE-4 owns only the closed Global schema, its vector-index health view,
the existing DecisionDigest search operation and the two writes that receive
replacement embeddings.  Provider composition, lifecycle and cutover remain
owned by M-PULSE-6 and M-PULSE-7.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Iterable

from okto_grafx import Database, VectorValue
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
    GraphIndexUnavailable,
)

from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_schema_manifest import (
    EMBEDDING_DIMENSION,
    EMBEDDING_STORAGE_DTYPE,
    GrafxColumnManifest,
    GrafxSpaceManifest,
    GrafxTableManifest,
)

_BOOTSTRAP_OPERATION = "ensure_current_grafx_global_schema"
_SEARCH_OPERATION = "search_grafx_decision_digests"
_STATUS_OPERATION = "certify_grafx_global_vector_indexes"
_WRITE_OPERATION = "write_grafx_global_vector"
_LAYERS = frozenset(("canonical", "working", "all"))
_SCORE_ABS_TOL = 1e-9
_SCORE_REL_TOL = 1e-9

MutationFence = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class GrafxGlobalSchemaManifest:
    """Exact physical schema of the separate Global Discovery database."""

    spaces: tuple[GrafxSpaceManifest, ...]
    nodes: tuple[GrafxTableManifest, ...]
    relationships: tuple[GrafxTableManifest, ...]
    logical_descriptor_json: str
    logical_fingerprint: str

    @property
    def tables(self) -> tuple[GrafxTableManifest, ...]:
        return (*self.nodes, *self.relationships)

    @property
    def logical_descriptor(self) -> dict[str, object]:
        return json.loads(self.logical_descriptor_json)


@dataclass(frozen=True, slots=True)
class GrafxGlobalBootstrapResult:
    """Stable result of one idempotent Global schema bootstrap."""

    logical_fingerprint: str
    changed: bool


@dataclass(frozen=True, slots=True)
class GrafxVectorIndexStatus:
    """Backend-neutral evidence for one certified Pulse vector index."""

    table: str
    column: str
    space: str
    index: str
    dimension: int
    metric: str
    normalized: bool
    storage_dtype: str
    stale: bool
    stale_reason: str | None
    built_through_lsn: int


def _column(
    name: str,
    pulse_type: str,
    *,
    nullable: bool,
    vector_space: str | None = None,
) -> GrafxColumnManifest:
    return GrafxColumnManifest(
        name=name,
        pulse_type=pulse_type,
        nullable=nullable,
        vector_space=vector_space,
    )


def _node(
    name: str,
    primary_key: str,
    columns: Iterable[tuple[str, str, str | None]],
) -> GrafxTableManifest:
    return GrafxTableManifest(
        name=name,
        kind="node",
        columns=tuple(
            _column(
                column_name,
                column_type,
                nullable=column_name != primary_key,
                vector_space=vector_space,
            )
            for column_name, column_type, vector_space in columns
        ),
        primary_key=primary_key,
    )


def _relationship(
    name: str,
    from_table: str,
    to_table: str,
    *properties: tuple[str, str],
) -> GrafxTableManifest:
    return GrafxTableManifest(
        name=name,
        kind="rel",
        columns=(
            _column("_from", "INT64", nullable=False),
            _column("_to", "INT64", nullable=False),
            *(
                _column(prop_name, prop_type, nullable=True)
                for prop_name, prop_type in properties
            ),
        ),
        from_table=from_table,
        to_table=to_table,
        logical_relationship=name,
    )


def _build_manifest() -> GrafxGlobalSchemaManifest:
    spaces = (
        GrafxSpaceManifest("Board", "board_summary_idx"),
        GrafxSpaceManifest("Topic", "topic_centroid_idx"),
        GrafxSpaceManifest("Entity", "entity_embedding_idx"),
        GrafxSpaceManifest("DecisionDigest", "digest_embedding_idx"),
    )
    nodes = (
        _node(
            "Board",
            "board_id",
            (
                ("board_id", "STRING", None),
                ("name", "STRING", None),
                ("summary", "STRING", None),
                ("summary_embedding", "DOUBLE[384]", "board_summary_idx"),
                ("topic_count", "INT64", None),
                ("entity_count", "INT64", None),
                ("decision_count", "INT64", None),
                ("last_sync_at", "TIMESTAMP", None),
            ),
        ),
        _node(
            "Topic",
            "id",
            (
                ("id", "STRING", None),
                ("name", "STRING", None),
                ("centroid_embedding", "DOUBLE[384]", "topic_centroid_idx"),
                ("member_count", "INT64", None),
                ("created_at", "TIMESTAMP", None),
                ("updated_at", "TIMESTAMP", None),
            ),
        ),
        _node(
            "Entity",
            "id",
            (
                ("id", "STRING", None),
                ("canonical_name", "STRING", None),
                ("aliases", "STRING", None),
                ("embedding", "DOUBLE[384]", "entity_embedding_idx"),
                ("mention_count", "INT64", None),
                ("last_seen", "TIMESTAMP", None),
            ),
        ),
        _node(
            "DecisionDigest",
            "id",
            (
                ("id", "STRING", None),
                ("board_id", "STRING", None),
                ("original_node_id", "STRING", None),
                ("title", "STRING", None),
                ("one_line_summary", "STRING", None),
                ("node_type", "STRING", None),
                ("graph_layer", "STRING", None),
                ("source_revoked", "BOOLEAN", None),
                ("embedding", "DOUBLE[384]", "digest_embedding_idx"),
                ("created_at", "TIMESTAMP", None),
            ),
        ),
    )
    relationships = (
        _relationship("HAS_TOPIC", "Board", "Topic"),
        _relationship("MENTIONS_ENTITY", "Board", "Entity"),
        _relationship("CONTAINS_DECISION", "Board", "DecisionDigest"),
        _relationship("TOPIC_RELATES_TO", "Topic", "Topic", ("weight", "DOUBLE")),
        _relationship("ENTITY_RELATES_TO", "Entity", "Entity", ("weight", "DOUBLE")),
        _relationship("DECISION_MENTIONS_ENTITY", "DecisionDigest", "Entity"),
        _relationship("DECISION_DERIVES_FROM", "DecisionDigest", "DecisionDigest"),
    )
    descriptor: dict[str, object] = {
        "contract": "okto-pulse-global-discovery-schema",
        "spaces": [
            {
                "table": space.node_type,
                "name": space.name,
                "dimension": space.dimension,
                "metric": space.metric,
                "normalized": space.normalized,
                "storage_dtype": space.storage_dtype,
            }
            for space in spaces
        ],
        "tables": [
            {
                "name": table.name,
                "kind": table.kind,
                "primary_key": table.primary_key,
                "from_table": table.from_table,
                "to_table": table.to_table,
                "columns": [column.descriptor() for column in table.columns],
            }
            for table in (*nodes, *relationships)
        ],
    }
    descriptor_json = json.dumps(
        descriptor,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return GrafxGlobalSchemaManifest(
        spaces=spaces,
        nodes=nodes,
        relationships=relationships,
        logical_descriptor_json=descriptor_json,
        logical_fingerprint=hashlib.sha256(descriptor_json.encode("utf-8")).hexdigest(),
    )


PULSE_GRAFX_GLOBAL_SCHEMA = _build_manifest()


def _failure(
    reason: str,
    *,
    operation: str,
    index: bool = False,
    **details: object,
) -> GraphError:
    error_type = GraphIndexUnavailable if index else GraphCapabilityUnavailable
    return error_type(
        "The Grafx Global Discovery vector contract is not satisfied.",
        details={
            "backend": "okto_grafx",
            "operation": operation,
            "reason": reason,
            **details,
        },
    )


def _column_signature(column: object) -> tuple[object, ...]:
    column_type = column.type
    return (
        column.name,
        column_type.name,
        column.nullable,
        column.vector_space,
    )


def _expected_column_signature(column: GrafxColumnManifest) -> tuple[object, ...]:
    return (
        column.name,
        column.grafx_value_type,
        column.nullable,
        column.vector_space,
    )


def _validate_table(expected: GrafxTableManifest, observed: object) -> None:
    observed_shape = (
        observed.kind,
        observed.primary_key,
        observed.from_table,
        observed.to_table,
        tuple(_column_signature(column) for column in observed.columns),
    )
    expected_shape = (
        expected.kind,
        expected.primary_key,
        expected.from_table,
        expected.to_table,
        tuple(_expected_column_signature(column) for column in expected.columns),
    )
    if observed_shape != expected_shape:
        raise _failure(
            "table_shape_mismatch",
            operation=_BOOTSTRAP_OPERATION,
            table=expected.name,
            expected=expected_shape,
            observed=observed_shape,
        )


def _validate_space(expected: GrafxSpaceManifest, observed: object) -> None:
    observed_shape = (
        observed.dimension,
        observed.metric.value,
        observed.normalized,
        observed.storage_dtype,
        observed.state,
    )
    expected_shape = (
        expected.dimension,
        expected.metric,
        expected.normalized,
        expected.storage_dtype,
        "active",
    )
    if observed_shape != expected_shape:
        raise _failure(
            "space_shape_mismatch",
            operation=_BOOTSTRAP_OPERATION,
            space=expected.name,
            expected=expected_shape,
            observed=observed_shape,
        )


@dataclass(frozen=True, slots=True)
class _Preflight:
    missing_spaces: tuple[GrafxSpaceManifest, ...]
    missing_tables: tuple[GrafxTableManifest, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_spaces and not self.missing_tables


def _preflight(
    database: Database,
    manifest: GrafxGlobalSchemaManifest,
) -> _Preflight:
    catalog = database.catalog.catalog
    observed_tables = tuple(catalog.tables())
    observed_spaces = tuple(catalog.spaces())
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
        raise _failure(
            "unexpected_schema_object",
            operation=_BOOTSTRAP_OPERATION,
            tables=unexpected_tables,
            spaces=unexpected_spaces,
        )

    tables_by_name = {table.name: table for table in observed_tables}
    spaces_by_name = {space.name: space for space in observed_spaces}
    missing_spaces: list[GrafxSpaceManifest] = []
    missing_tables: list[GrafxTableManifest] = []
    for expected in manifest.spaces:
        observed = spaces_by_name.get(expected.name)
        if observed is None:
            missing_spaces.append(expected)
        else:
            _validate_space(expected, observed)
    for expected in manifest.tables:
        observed = tables_by_name.get(expected.name)
        if observed is None:
            missing_tables.append(expected)
        else:
            _validate_table(expected, observed)
    return _Preflight(tuple(missing_spaces), tuple(missing_tables))


def validate_current_grafx_global_schema(
    database: Database,
    *,
    manifest: GrafxGlobalSchemaManifest = PULSE_GRAFX_GLOBAL_SCHEMA,
) -> str:
    """Validate the complete Global catalog and return its logical fingerprint."""

    try:
        preflight = _preflight(database, manifest)
        if not preflight.complete:
            raise _failure(
                "missing_schema_object",
                operation=_BOOTSTRAP_OPERATION,
                spaces=tuple(space.name for space in preflight.missing_spaces),
                tables=tuple(table.name for table in preflight.missing_tables),
            )
        return manifest.logical_fingerprint
    except GraphError:
        raise
    except Exception as exc:
        mapped = map_grafx_error(exc, operation=_BOOTSTRAP_OPERATION)
        raise mapped from exc


def ensure_current_grafx_global_schema(
    database: Database,
    *,
    manifest: GrafxGlobalSchemaManifest = PULSE_GRAFX_GLOBAL_SCHEMA,
    revalidate_fence: MutationFence | None = None,
) -> GrafxGlobalBootstrapResult:
    """Create only missing Global objects in one transaction and validate cold truth."""

    try:
        preflight = _preflight(database, manifest)
        if preflight.complete:
            return GrafxGlobalBootstrapResult(
                logical_fingerprint=manifest.logical_fingerprint,
                changed=False,
            )
        transaction = database.begin("write")
        try:
            for space in preflight.missing_spaces:
                if revalidate_fence is not None:
                    revalidate_fence("global_schema")
                transaction.execute(space.ddl())
            missing_names = {table.name for table in preflight.missing_tables}
            for table in manifest.nodes:
                if table.name in missing_names:
                    if revalidate_fence is not None:
                        revalidate_fence("global_schema")
                    transaction.execute(table.ddl())
            for table in manifest.relationships:
                if table.name in missing_names:
                    if revalidate_fence is not None:
                        revalidate_fence("global_schema")
                    transaction.execute(table.ddl())
            if revalidate_fence is not None:
                revalidate_fence("commit")
            report = transaction.commit()
        except BaseException:
            if transaction.active:
                transaction.rollback()
            raise
        if not report.durable or not report.wrote:
            raise _failure(
                "schema_commit_not_published",
                operation=_BOOTSTRAP_OPERATION,
            )
        validate_current_grafx_global_schema(database, manifest=manifest)
        return GrafxGlobalBootstrapResult(
            logical_fingerprint=manifest.logical_fingerprint,
            changed=True,
        )
    except GraphError:
        raise
    except Exception as exc:
        mapped = map_grafx_error(exc, operation=_BOOTSTRAP_OPERATION)
        raise mapped from exc


def _vector_targets(
    manifest: GrafxGlobalSchemaManifest,
) -> tuple[tuple[str, str, GrafxSpaceManifest], ...]:
    targets: list[tuple[str, str, GrafxSpaceManifest]] = []
    spaces = {space.name: space for space in manifest.spaces}
    for table in manifest.nodes:
        for column in table.columns:
            if column.vector_space is not None:
                targets.append((table.name, column.name, spaces[column.vector_space]))
    return tuple(targets)


def certify_grafx_global_vector_indexes(
    database: Database,
    *,
    manifest: GrafxGlobalSchemaManifest = PULSE_GRAFX_GLOBAL_SCHEMA,
) -> tuple[GrafxVectorIndexStatus, ...]:
    """Cold-verify and correlate all four public vector/index views."""

    try:
        validate_current_grafx_global_schema(database, manifest=manifest)
        report = database.verify("all")
        if report.findings:
            raise _failure(
                "verification_findings",
                operation=_STATUS_OPERATION,
                index=True,
                finding_count=len(report.findings),
            )
        vectors = database.vectors
        indexes = database.indexes
        published_lsn = database.transactions.published_lsn()
        catalog = database.catalog.catalog
        statuses: list[GrafxVectorIndexStatus] = []
        for table_name, column_name, expected_space in _vector_targets(manifest):
            space = vectors.space(expected_space.name)
            vector_index = vectors.index(expected_space.name)
            generic_index = indexes.index(vector_index.name)
            table = catalog.table(table_name)
            position = next(
                index
                for index, column in enumerate(table.columns)
                if column.name == column_name
            )
            observed = {
                "dimension": space.dimension,
                "metric": space.metric.value,
                "normalized": space.normalized,
                "storage_dtype": space.storage_dtype,
                "state": space.state,
                "index_space_id": vector_index.space_id,
                "space_id": space.space_id,
                "index_table": generic_index.definition.table_name,
                "index_positions": generic_index.definition.positions,
                "index_stale": vector_index.stale,
                "generic_stale": generic_index.stale,
                "index_stale_reason": vector_index.stale_reason,
                "generic_stale_reason": generic_index.stale_reason,
                "index_lsn": vector_index.built_through_lsn,
                "generic_lsn": generic_index.built_through_lsn,
                "published_lsn": published_lsn,
            }
            expected = {
                "dimension": expected_space.dimension,
                "metric": expected_space.metric,
                "normalized": expected_space.normalized,
                "storage_dtype": expected_space.storage_dtype,
                "state": "active",
                "index_space_id": space.space_id,
                "space_id": space.space_id,
                "index_table": table_name,
                "index_positions": (position,),
                "index_stale": False,
                "generic_stale": False,
                "index_stale_reason": None,
                "generic_stale_reason": None,
                "index_lsn": published_lsn,
                "generic_lsn": published_lsn,
                "published_lsn": published_lsn,
            }
            if observed != expected:
                raise _failure(
                    "index_status_mismatch",
                    operation=_STATUS_OPERATION,
                    index=True,
                    space=expected_space.name,
                    expected=expected,
                    observed=observed,
                )
            statuses.append(
                GrafxVectorIndexStatus(
                    table=table_name,
                    column=column_name,
                    space=expected_space.name,
                    index=vector_index.name,
                    dimension=space.dimension,
                    metric=space.metric.value,
                    normalized=space.normalized,
                    storage_dtype=space.storage_dtype,
                    stale=False,
                    stale_reason=None,
                    built_through_lsn=published_lsn,
                )
            )
        return tuple(statuses)
    except GraphError:
        raise
    except Exception as exc:
        mapped = map_grafx_error(exc, operation=_STATUS_OPERATION)
        raise mapped from exc


def _invalid_argument(field: str, value: object, *, operation: str) -> GraphError:
    return _failure(
        "invalid_argument",
        operation=operation,
        field=field,
        value_type=type(value).__name__,
    )


def _validated_vector(
    database: Database,
    values: object,
    *,
    space: str,
    operation: str,
) -> VectorValue:
    if not isinstance(values, (list, tuple)) or len(values) != EMBEDDING_DIMENSION:
        raise _invalid_argument("embedding", values, operation=operation)
    components: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _invalid_argument("embedding", values, operation=operation)
        component = float(value)
        if not math.isfinite(component):
            raise _invalid_argument("embedding", values, operation=operation)
        components.append(component)
    space_id = database.catalog.catalog.space(space).space_id
    return VectorValue(
        values=tuple(components),
        space_ref=space_id,
        dtype=EMBEDDING_STORAGE_DTYPE,
    )


def _validated_search(
    query_vector: object,
    *,
    graph_layer: object,
    top_k: object,
    min_similarity: object,
) -> tuple[tuple[float, ...], str, int, float]:
    if graph_layer not in _LAYERS:
        raise _invalid_argument("graph_layer", graph_layer, operation=_SEARCH_OPERATION)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise _invalid_argument("top_k", top_k, operation=_SEARCH_OPERATION)
    if (
        isinstance(min_similarity, bool)
        or not isinstance(min_similarity, (int, float))
        or not math.isfinite(float(min_similarity))
        or not 0.0 <= float(min_similarity) <= 1.0
    ):
        raise _invalid_argument(
            "min_similarity", min_similarity, operation=_SEARCH_OPERATION
        )
    if (
        not isinstance(query_vector, (list, tuple))
        or len(query_vector) != EMBEDDING_DIMENSION
    ):
        raise _invalid_argument(
            "query_vector", query_vector, operation=_SEARCH_OPERATION
        )
    components: list[float] = []
    for value in query_vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _invalid_argument(
                "query_vector", query_vector, operation=_SEARCH_OPERATION
            )
        component = float(value)
        if not math.isfinite(component):
            raise _invalid_argument(
                "query_vector", query_vector, operation=_SEARCH_OPERATION
            )
        components.append(component)
    return tuple(components), str(graph_layer), top_k, float(min_similarity)


def _layer_clause(alias: str, graph_layer: str) -> str:
    if graph_layer == "all":
        return ""
    return f"AND {alias}.graph_layer = $graph_layer "


def _search_statement(*, graph_layer: str, bounded: bool) -> str:
    statement = (
        "MATCH (b:Board)-[:CONTAINS_DECISION]->(d:DecisionDigest) "
        "WHERE b.board_id IN $boards AND d.embedding IS NOT NULL "
        "AND (d.source_revoked IS NULL OR d.source_revoked = false) "
        f"{_layer_clause('d', graph_layer)}"
    )
    if bounded:
        statement += (
            "AND similarity(d.embedding, $query, "
            "space => 'digest_embedding_idx') >= -1.0 "
        )
    statement += (
        "RETURN b.board_id, d.id, d.original_node_id, d.title, "
        "d.one_line_summary, d.node_type, "
        "coalesce(d.graph_layer, 'legacy_unknown'), "
    )
    if bounded:
        return statement + (
            "similarity_score() AS raw_similarity "
            "ORDER BY raw_similarity DESC LIMIT $fetch_k"
        )
    return statement + "d.embedding"


def _components(value: object) -> tuple[float, ...] | None:
    if type(value) is VectorValue:
        return tuple(float(component) for component in value.values)
    if isinstance(value, (list, tuple)):
        return tuple(float(component) for component in value)
    return None


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    left_scale = max((abs(value) for value in left), default=0.0)
    right_scale = max((abs(value) for value in right), default=0.0)
    if left_scale == 0.0 or right_scale == 0.0:
        return 0.0
    scaled_left = tuple(value / left_scale for value in left)
    scaled_right = tuple(value / right_scale for value in right)
    left_norm = math.hypot(*scaled_left)
    right_norm = math.hypot(*scaled_right)
    return math.fsum(a * b for a, b in zip(scaled_left, scaled_right)) / (
        left_norm * right_norm
    )


def _project_rows(
    rows: Iterable[tuple[object, ...]],
    *,
    query: tuple[float, ...],
    exact: bool,
    min_similarity: float,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if exact:
            embedding = _components(row[7])
            if embedding is None or len(embedding) != len(query):
                continue
            raw_similarity = _cosine(query, embedding)
        else:
            raw_similarity = float(row[7])
        similarity = max(0.0, min(1.0, raw_similarity))
        if similarity < min_similarity:
            continue
        identity = (str(row[0]), str(row[1]))
        if identity in seen:
            continue
        seen.add(identity)
        projected.append(
            {
                "board_id": identity[0],
                "digest_id": identity[1],
                "id": str(row[2]),
                "title": row[3],
                "summary": row[4],
                "node_type": row[5],
                "graph_layer": row[6],
                "similarity": similarity,
            }
        )
    projected.sort(
        key=lambda item: (
            -float(item["similarity"]),
            str(item["board_id"]),
            str(item["digest_id"]),
        )
    )
    return projected


def search_grafx_decision_digests(
    database: Database,
    query_vector: list[float],
    *,
    board_ids: tuple[str, ...],
    graph_layer: str,
    top_k: int,
    min_similarity: float,
    exhaustive: bool = False,
) -> list[dict[str, Any]]:
    """Search the existing Global DecisionDigest port over a separate Grafx DB."""

    if not board_ids:
        return []
    query, layer, wanted_k, threshold = _validated_search(
        query_vector,
        graph_layer=graph_layer,
        top_k=top_k,
        min_similarity=min_similarity,
    )
    if any(type(board_id) is not str or not board_id for board_id in board_ids):
        raise _invalid_argument("board_ids", board_ids, operation=_SEARCH_OPERATION)
    boards = sorted(set(board_ids))
    reader = None
    try:
        reader = database.begin("read")
        parameters: dict[str, object] = {
            "boards": boards,
            "graph_layer": layer,
        }
        if exhaustive:
            rows = reader.execute(
                _search_statement(graph_layer=layer, bounded=False),
                parameters,
            ).rows
            outcome = _project_rows(
                rows,
                query=query,
                exact=True,
                min_similarity=threshold,
            )[:wanted_k]
        else:
            parameters.update({"query": query, "fetch_k": wanted_k + 1})
            rows = reader.execute(
                _search_statement(graph_layer=layer, bounded=True),
                parameters,
            ).rows
            approximate = _project_rows(
                rows,
                query=query,
                exact=False,
                min_similarity=threshold,
            )
            # The bounded result needs one *unique eligible* witness beyond the
            # public cutoff.  A full physical page can still collapse to exactly
            # ``top_k`` identities after relationship-join deduplication or
            # thresholding; accepting it would leave the logical-id tie winner
            # unproved.
            falls_back = len(rows) < wanted_k + 1 or len(approximate) <= wanted_k
            if len(approximate) > wanted_k:
                falls_back = falls_back or math.isclose(
                    float(approximate[wanted_k - 1]["similarity"]),
                    float(approximate[wanted_k]["similarity"]),
                    abs_tol=_SCORE_ABS_TOL,
                    rel_tol=_SCORE_REL_TOL,
                )
            if falls_back:
                exact_rows = reader.execute(
                    _search_statement(graph_layer=layer, bounded=False),
                    {
                        "boards": boards,
                        "graph_layer": layer,
                    },
                ).rows
                outcome = _project_rows(
                    exact_rows,
                    query=query,
                    exact=True,
                    min_similarity=threshold,
                )[:wanted_k]
            else:
                outcome = approximate[:wanted_k]
        reader.rollback()
        reader = None
        return outcome
    except Exception as exc:
        if reader is not None and reader.active:
            try:
                reader.rollback()
            except Exception as cleanup_failure:
                exc.add_note(
                    "Grafx read-transaction rollback also failed with "
                    f"{type(cleanup_failure).__name__}."
                )
        if isinstance(exc, GraphError):
            raise
        mapped = map_grafx_error(exc, operation=_SEARCH_OPERATION)
        raise mapped from exc


def upsert_grafx_board_summary_vector(
    database: Database,
    *,
    board_id: str,
    name: str,
    summary: str,
    summary_embedding: list[float],
    decision_count: int,
    synced_at: str,
    revalidate_fence: MutationFence | None = None,
) -> None:
    """Upsert Board summary data, including every replacement embedding."""

    try:
        embedding = _validated_vector(
            database,
            summary_embedding,
            space="board_summary_idx",
            operation=_WRITE_OPERATION,
        )
        transaction = database.begin("write")
        try:
            if revalidate_fence is not None:
                revalidate_fence("upsert_board_summary")
            exists = transaction.execute(
                "MATCH (b:Board {board_id: $board_id}) RETURN b.board_id",
                {"board_id": board_id},
            ).rows
            values = {
                "board_id": board_id,
                "name": name,
                "summary": summary,
                "embedding": embedding,
                "decision_count": decision_count,
                "synced_at": synced_at,
            }
            if exists:
                if revalidate_fence is not None:
                    revalidate_fence("upsert_board_summary")
                transaction.execute(
                    "MATCH (b:Board {board_id: $board_id}) "
                    "SET b.name = $name, b.summary = $summary, "
                    "b.summary_embedding = $embedding, "
                    "b.decision_count = $decision_count, "
                    "b.last_sync_at = timestamp($synced_at)",
                    values,
                )
            else:
                if revalidate_fence is not None:
                    revalidate_fence("upsert_board_summary")
                transaction.execute(
                    "CREATE (:Board {board_id: $board_id, name: $name, "
                    "summary: $summary, summary_embedding: $embedding, "
                    "topic_count: 0, entity_count: 0, "
                    "decision_count: $decision_count, "
                    "last_sync_at: timestamp($synced_at)})",
                    values,
                )
            if revalidate_fence is not None:
                revalidate_fence("commit")
            report = transaction.commit()
        except BaseException:
            if transaction.active:
                transaction.rollback()
            raise
        if not report.durable or not report.wrote:
            raise _failure(
                "board_summary_commit_not_published",
                operation=_WRITE_OPERATION,
            )
    except GraphError:
        raise
    except Exception as exc:
        mapped = map_grafx_error(exc, operation=_WRITE_OPERATION)
        raise mapped from exc


def upsert_grafx_decision_digest_vector(
    database: Database,
    *,
    digest_id: str,
    board_id: str,
    original_node_id: str,
    title: str,
    summary: str,
    node_type: str,
    graph_layer: str,
    embedding: list[float],
    created_at: str,
    revalidate_fence: MutationFence | None = None,
) -> str:
    """Upsert one healthy digest identity and replace its embedding atomically."""

    if graph_layer not in {"canonical", "working"}:
        raise _invalid_argument("graph_layer", graph_layer, operation=_WRITE_OPERATION)
    try:
        vector = _validated_vector(
            database,
            embedding,
            space="digest_embedding_idx",
            operation=_WRITE_OPERATION,
        )
        transaction = database.begin("write")
        try:
            if revalidate_fence is not None:
                revalidate_fence("upsert_decision_digest")
            by_source = transaction.execute(
                "MATCH (d:DecisionDigest) "
                "WHERE d.board_id = $board_id "
                "AND d.original_node_id = $original_node_id RETURN d.id",
                {
                    "board_id": board_id,
                    "original_node_id": original_node_id,
                },
            ).rows
            if by_source and tuple(str(row[0]) for row in by_source) != (digest_id,):
                raise _failure(
                    "digest_identity_collision",
                    operation=_WRITE_OPERATION,
                    board_id=board_id,
                    original_node_id=original_node_id,
                )
            if revalidate_fence is not None:
                revalidate_fence("upsert_decision_digest")
            by_id = transaction.execute(
                "MATCH (d:DecisionDigest {id: $digest_id}) "
                "RETURN d.board_id, d.original_node_id",
                {"digest_id": digest_id},
            ).rows
            if by_id and (
                str(by_id[0][0]) != board_id or str(by_id[0][1]) != original_node_id
            ):
                raise _failure(
                    "digest_primary_key_collision",
                    operation=_WRITE_OPERATION,
                    digest_id=digest_id,
                )
            values = {
                "digest_id": digest_id,
                "board_id": board_id,
                "original_node_id": original_node_id,
                "title": title,
                "summary": summary,
                "node_type": node_type,
                "graph_layer": graph_layer,
                "embedding": vector,
                "created_at": created_at,
            }
            if by_id:
                if revalidate_fence is not None:
                    revalidate_fence("upsert_decision_digest")
                transaction.execute(
                    "MATCH (d:DecisionDigest {id: $digest_id}) "
                    "SET d.board_id = $board_id, "
                    "d.original_node_id = $original_node_id, "
                    "d.title = $title, d.one_line_summary = $summary, "
                    "d.node_type = $node_type, d.graph_layer = $graph_layer, "
                    "d.source_revoked = false, d.embedding = $embedding, "
                    "d.created_at = timestamp($created_at)",
                    values,
                )
                outcome = "updated"
            else:
                if revalidate_fence is not None:
                    revalidate_fence("upsert_decision_digest")
                transaction.execute(
                    "CREATE (:DecisionDigest {id: $digest_id, "
                    "board_id: $board_id, original_node_id: $original_node_id, "
                    "title: $title, one_line_summary: $summary, "
                    "node_type: $node_type, graph_layer: $graph_layer, "
                    "source_revoked: false, embedding: $embedding, "
                    "created_at: timestamp($created_at)})",
                    values,
                )
                outcome = "created"
            if revalidate_fence is not None:
                revalidate_fence("commit")
            report = transaction.commit()
        except BaseException:
            if transaction.active:
                transaction.rollback()
            raise
        if not report.durable or not report.wrote:
            raise _failure(
                "decision_digest_commit_not_published",
                operation=_WRITE_OPERATION,
            )
        return outcome
    except GraphError:
        raise
    except Exception as exc:
        mapped = map_grafx_error(exc, operation=_WRITE_OPERATION)
        raise mapped from exc


__all__ = [
    "PULSE_GRAFX_GLOBAL_SCHEMA",
    "GrafxGlobalBootstrapResult",
    "GrafxGlobalSchemaManifest",
    "GrafxVectorIndexStatus",
    "MutationFence",
    "certify_grafx_global_vector_indexes",
    "ensure_current_grafx_global_schema",
    "search_grafx_decision_digests",
    "upsert_grafx_board_summary_vector",
    "upsert_grafx_decision_digest_vector",
    "validate_current_grafx_global_schema",
]
