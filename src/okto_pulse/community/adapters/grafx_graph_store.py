"""Grafx implementation of the Core :class:`SemanticGraphStore` boundary.

The provider resolves one already-owned database handle per call.  It never
owns paths or handle lifetime, and every write is fenced immediately before
the statement and again before commit.  Physical relationship-table names and
Grafx values are normalized at this boundary so neither can leak into Core.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn, TypeVar

from okto_grafx import Database, Timestamp, VectorValue
from okto_grafx.errors import GrafxError
from okto_pulse.core.domain.code_traceability_kg import (
    CODE_TRACEABILITY_KG_SUBTYPES,
)
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
)
from okto_pulse.core.kg.interfaces.graph_store import GraphCapabilities, QueryFilters
from okto_pulse.core.kg.schema_contract import (
    NODE_TYPES,
    SCHEMA_VERSION,
    VECTOR_INDEX_TYPES,
    resolve_relationship_endpoint_pair,
    stable_rel_type_entries,
    vector_index_name,
)

from okto_pulse.community.adapters.grafx_board_vector_search import (
    CommunityGrafxBoardVectorSearch,
)
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
    RelationshipLayoutEntry,
    introspect_logical_relationships,
    resolve_relationship_table,
)
from okto_pulse.community.adapters.grafx_schema_bootstrap import (
    ensure_current_grafx_board_schema,
    validate_current_grafx_schema,
)
from okto_pulse.community.adapters.grafx_schema_introspection import (
    list_node_properties as introspect_node_properties,
)
from okto_pulse.community.adapters.grafx_schema_manifest import (
    EMBEDDING_DIMENSION,
    PULSE_GRAFX_SCHEMA_MANIFEST,
)

DatabaseResolver = Callable[[str], Database]
FenceRevalidator = Callable[[str, str], None]
_T = TypeVar("_T")

_LAYERS = frozenset({"canonical", "working", "all"})
_IDENTITY_NODE_PROPERTIES = frozenset({"id"})
_IDENTITY_REL_PROPERTIES = frozenset({"_from", "_to"})


def _invalid_board_id(board_id: object) -> str:
    if type(board_id) is not str or not board_id:
        raise ValueError("board_id must be non-empty text")
    return board_id


def _node_type(node_type: object) -> str:
    if type(node_type) is not str or node_type not in NODE_TYPES:
        raise ValueError(f"invalid_node_type: {node_type!r} (allowed: {NODE_TYPES})")
    return node_type


def _graph_layer(graph_layer: object) -> str:
    if type(graph_layer) is not str or graph_layer not in _LAYERS:
        raise ValueError("invalid_graph_layer")
    return graph_layer


def _timestamp_from_value(value: str | datetime) -> Timestamp:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    else:
        parsed = parsed.astimezone(UTC)
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    micros = (
        delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    )
    return Timestamp(micros=micros)


def _iso_timestamp(value: Timestamp | datetime) -> str:
    if isinstance(value, Timestamp):
        rendered = datetime.fromtimestamp(value.micros / 1_000_000, tz=UTC)
    else:
        rendered = value
        if rendered.tzinfo is None or rendered.utcoffset() is None:
            rendered = rendered.replace(tzinfo=UTC)
        else:
            rendered = rendered.astimezone(UTC)
    return rendered.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_value(value: Any) -> Any:
    if isinstance(value, (Timestamp, datetime)):
        return _iso_timestamp(value)
    if isinstance(value, VectorValue):
        return [_normalize_value(item) for item in value.values]
    if isinstance(value, (tuple, list)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    return value


def _rows(result: object) -> list[list[Any]]:
    source = getattr(result, "rows", None)
    if not isinstance(source, (tuple, list)):
        raise ValueError("Grafx returned an invalid row collection")
    normalized: list[list[Any]] = []
    for row in source:
        if not isinstance(row, (tuple, list)):
            raise ValueError("Grafx returned an invalid result row")
        normalized.append([_normalize_value(value) for value in row])
    return normalized


def _raise_mapped(exc: Exception, *, operation: str) -> NoReturn:
    mapped = map_grafx_error(exc, operation=operation)
    if mapped is exc:
        raise exc
    raise mapped from exc


@dataclass(frozen=True, slots=True)
class _NodeView:
    node_id: str
    node_type: str
    title: Any
    source_artifact_ref: Any
    source_confidence: Any
    graph_layer: Any
    superseded_by: Any
    revocation_reason: Any
    kind_of: Any


def _node_view(row: Sequence[Any], *, node_type: str | None = None) -> _NodeView:
    expected = 8 if node_type is not None else 9
    if len(row) != expected:
        raise ValueError("Grafx returned an unexpected neighborhood row shape")
    offset = 0
    observed_type = node_type
    if observed_type is None:
        observed_type = row[1]
        offset = 1
    if type(row[0]) is not str or type(observed_type) is not str:
        raise ValueError("Grafx returned an invalid node identity")
    return _NodeView(
        node_id=row[0],
        node_type=observed_type,
        title=row[1 + offset],
        source_artifact_ref=row[2 + offset],
        source_confidence=row[3 + offset],
        graph_layer=row[4 + offset],
        superseded_by=row[5 + offset],
        revocation_reason=row[6 + offset],
        kind_of=row[7 + offset],
    )


class _FencedWriter:
    """Small synchronous write scope used by the synchronous store port."""

    def __init__(self, store: CommunityGrafxGraphStore, board_id: str, txn: Any):
        self._store = store
        self._board_id = board_id
        self._txn = txn
        self.write_attempted = False

    def query(
        self,
        statement: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        operation: str,
    ) -> object:
        try:
            return self._txn.execute(statement, dict(parameters or {}))
        except Exception as exc:
            _raise_mapped(exc, operation=operation)

    def write(
        self,
        statement: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        operation: str,
    ) -> object:
        self._store._fence(self._board_id, operation)
        self.write_attempted = True
        return self.query(statement, parameters, operation=operation)


class CommunityGrafxGraphStore:
    """Complete board-scoped ``SemanticGraphStore`` backed by Okto Grafx."""

    def __init__(
        self,
        database_resolver: DatabaseResolver,
        revalidate_fence: FenceRevalidator,
    ) -> None:
        if not callable(database_resolver):
            raise ValueError("database_resolver must be callable")
        if not callable(revalidate_fence):
            raise ValueError("revalidate_fence must be callable")
        self._database_resolver = database_resolver
        self._revalidate_fence = revalidate_fence
        self._vector_provider = CommunityGrafxBoardVectorSearch(database_resolver)

    def _resolve(self, board_id: str, *, operation: str) -> Database:
        wanted = _invalid_board_id(board_id)
        try:
            return self._database_resolver(wanted)
        except Exception as exc:
            _raise_mapped(exc, operation=operation)

    def _fence(self, board_id: str, phase: str) -> None:
        try:
            self._revalidate_fence(board_id, phase)
        except Exception as exc:
            _raise_mapped(exc, operation=f"fence:{phase}")

    def _read(
        self,
        board_id: str,
        *,
        operation: str,
        callback: Callable[[Any], _T],
    ) -> _T:
        database = self._resolve(board_id, operation=operation)
        try:
            with database.begin("read") as reader:
                return callback(reader)
        except GraphError:
            raise
        except Exception as exc:
            _raise_mapped(exc, operation=operation)

    def _write(
        self,
        board_id: str,
        *,
        operation: str,
        callback: Callable[[_FencedWriter], _T],
        database: Database | None = None,
    ) -> _T:
        if database is None:
            database = self._resolve(board_id, operation=operation)
        try:
            transaction = database.begin("write")
        except Exception as exc:
            _raise_mapped(exc, operation=operation)

        writer = _FencedWriter(self, board_id, transaction)
        try:
            answer = callback(writer)
            if not writer.write_attempted:
                try:
                    transaction.rollback()
                except Exception as exc:
                    _raise_mapped(exc, operation=f"{operation}:rollback")
                return answer
            self._fence(board_id, "commit")
            try:
                transaction.commit()
            except Exception as exc:
                mapped = map_grafx_error(exc, operation=f"{operation}:commit")
                report = getattr(transaction, "report", None)
                if report is not None and bool(getattr(report, "durable", False)):
                    mapped.details.update(
                        {
                            "commit_durable": True,
                            "write_may_be_applied": bool(
                                getattr(report, "wrote", False)
                            ),
                            "commit_csn": getattr(report, "csn", None),
                        }
                    )
                    mapped.retryable = False
                if mapped is exc:
                    raise
                raise mapped from exc
            return answer
        except BaseException as failure:
            cleanup_error: BaseException | None = None
            if getattr(transaction, "active", False):
                try:
                    transaction.rollback()
                except BaseException as exc:  # noqa: BLE001 - preserve primary
                    cleanup_error = exc
            if cleanup_error is not None:
                try:
                    failure.add_note(
                        "Grafx rollback also failed while discarding a graph-store write."
                    )
                except BaseException:  # noqa: BLE001, S110 - diagnostic only
                    pass
                raise failure from cleanup_error
            raise

    @staticmethod
    def _definition(database: Database, node_type: str) -> Any:
        try:
            definition = database.catalog.catalog.table(node_type)
        except Exception as exc:
            _raise_mapped(exc, operation="node_schema")
        if definition.kind != "node":
            raise GraphCapabilityUnavailable(
                f"Grafx table {node_type!r} is not a node table.",
                details={"backend": "okto_grafx", "node_type": node_type},
            )
        return definition

    @staticmethod
    def _relationship_definition(
        database: Database,
        edge_type: str,
        from_type: str,
        to_type: str,
    ) -> tuple[str, Any]:
        physical = resolve_relationship_table(edge_type, from_type, to_type)
        try:
            definition = database.catalog.catalog.table(physical)
        except Exception as exc:
            _raise_mapped(exc, operation="relationship_schema")
        if (
            definition.kind != "rel"
            or definition.from_table != from_type
            or definition.to_table != to_type
        ):
            raise GraphCapabilityUnavailable(
                "The Grafx relationship table does not match the Pulse endpoint pair.",
                details={
                    "backend": "okto_grafx",
                    "edge_type": edge_type,
                    "from_type": from_type,
                    "to_type": to_type,
                    "physical_table": physical,
                },
            )
        return physical, definition

    @staticmethod
    def _coerce_value(database: Database, column: Any, value: Any) -> Any:
        if value is None:
            return None
        if column.type.name == "TIMESTAMP" and not isinstance(value, Timestamp):
            if not isinstance(value, (str, datetime)):
                raise ValueError(
                    f"timestamp property {column.name!r} requires ISO text or datetime"
                )
            try:
                return _timestamp_from_value(value)
            except GrafxError as exc:
                _raise_mapped(exc, operation="node_property_coercion")
        if column.is_vector and not isinstance(value, VectorValue):
            if isinstance(value, (str, bytes, bytearray)) or not isinstance(
                value, (tuple, list)
            ):
                raise ValueError(
                    f"vector property {column.name!r} requires a numeric sequence"
                )
            try:
                space = database.catalog.catalog.space(str(column.vector_space))
            except GrafxError as exc:
                _raise_mapped(exc, operation="vector_property_schema")
            components = tuple(float(item) for item in value)
            if len(components) != space.dimension or not all(
                math.isfinite(item) for item in components
            ):
                raise ValueError(
                    f"vector property {column.name!r} requires {space.dimension} "
                    "finite components"
                )
            try:
                return VectorValue(
                    values=components,
                    space_ref=space.space_id,
                    dtype=space.storage_dtype,
                )
            except GrafxError as exc:
                _raise_mapped(exc, operation="vector_property_coercion")
        return value

    @classmethod
    def _properties(
        cls,
        database: Database,
        definition: Any,
        attrs: Mapping[str, Any],
        *,
        forbidden: frozenset[str],
    ) -> dict[str, Any]:
        columns = {column.name: column for column in definition.columns}
        unknown = set(attrs).difference(columns)
        rejected = set(attrs).intersection(forbidden)
        if unknown or rejected:
            raise ValueError(
                "invalid graph properties: "
                f"unknown={sorted(unknown)!r} forbidden={sorted(rejected)!r}"
            )
        return {
            name: cls._coerce_value(database, columns[name], value)
            for name, value in attrs.items()
        }

    # ------------------------------------------------------------------
    # Bootstrap and writes
    # ------------------------------------------------------------------

    def bootstrap(self, board_id: str) -> None:
        wanted = _invalid_board_id(board_id)
        database = self._resolve(wanted, operation="bootstrap")
        stamp = Timestamp(micros=time.time_ns() // 1_000)
        try:
            ensure_current_grafx_board_schema(
                database,
                board_id=wanted,
                bootstrapped_at=stamp,
                revalidate_fence=lambda phase: self._fence(wanted, phase),
            )
        except GraphError:
            raise
        except Exception as exc:
            _raise_mapped(exc, operation="bootstrap")

    def create_node(
        self,
        board_id: str,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
    ) -> None:
        wanted_type = _node_type(node_type)
        if type(node_id) is not str or not node_id:
            raise ValueError("node_id must be non-empty text")
        database = self._resolve(board_id, operation="create_node")
        definition = self._definition(database, wanted_type)
        values = self._properties(
            database,
            definition,
            attrs,
            forbidden=_IDENTITY_NODE_PROPERTIES,
        )
        values = {"id": node_id, **values}
        names = tuple(values)
        bindings = ", ".join(
            f"{name}: $value_{index}" for index, name in enumerate(names)
        )
        parameters = {
            f"value_{index}": values[name] for index, name in enumerate(names)
        }
        self._write(
            board_id,
            operation="create_node",
            database=database,
            callback=lambda writer: writer.write(
                f"CREATE (n:{wanted_type} {{{bindings}}})",
                parameters,
                operation="create_node",
            ),
        )

    def create_edge(
        self,
        board_id: str,
        edge_type: str,
        from_id: str,
        to_id: str,
        attrs: dict[str, Any] | None = None,
        *,
        from_type: str | None = None,
        to_type: str | None = None,
    ) -> None:
        source, target = resolve_relationship_endpoint_pair(
            edge_type,
            from_type=from_type,
            to_type=to_type,
        )
        database = self._resolve(board_id, operation="create_edge")
        physical, definition = self._relationship_definition(
            database, edge_type, source, target
        )
        raw = dict(attrs or {})
        raw.setdefault("confidence", 0.7)
        raw.setdefault("layer", "cognitive")
        raw.setdefault("rule_id", "")
        raw.setdefault("created_by", raw.get("created_by_session_id", ""))
        raw.setdefault("fallback_reason", "")
        values = self._properties(
            database,
            definition,
            raw,
            forbidden=_IDENTITY_REL_PROPERTIES,
        )
        bindings = ", ".join(
            f"{name}: $value_{index}" for index, name in enumerate(values)
        )
        properties = f" {{{bindings}}}" if bindings else ""
        parameters = {
            "from_id": from_id,
            "to_id": to_id,
            **{f"value_{index}": value for index, value in enumerate(values.values())},
        }
        self._write(
            board_id,
            operation="create_edge",
            database=database,
            callback=lambda writer: writer.write(
                f"MATCH (a:{source}), (b:{target}) "
                "WHERE a.id = $from_id AND b.id = $to_id "
                f"CREATE (a)-[:{physical}{properties}]->(b)",
                parameters,
                operation="create_edge",
            ),
        )

    def _update_node(
        self,
        board_id: str,
        node_type: str,
        node_id: str,
        attrs: Mapping[str, Any],
        *,
        operation: str,
    ) -> None:
        wanted_type = _node_type(node_type)
        database = self._resolve(board_id, operation=operation)
        definition = self._definition(database, wanted_type)
        values = self._properties(
            database,
            definition,
            attrs,
            forbidden=_IDENTITY_NODE_PROPERTIES,
        )
        if not values:
            return
        names = tuple(values)
        assignments = ", ".join(
            f"n.{name} = $value_{index}" for index, name in enumerate(names)
        )
        parameters = {
            "node_id": node_id,
            **{f"value_{index}": values[name] for index, name in enumerate(names)},
        }
        self._write(
            board_id,
            operation=operation,
            database=database,
            callback=lambda writer: writer.write(
                f"MATCH (n:{wanted_type}) WHERE n.id = $node_id SET {assignments}",
                parameters,
                operation=operation,
            ),
        )

    def update_node(
        self,
        board_id: str,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
    ) -> None:
        self._update_node(
            board_id,
            node_type,
            node_id,
            {key: value for key, value in attrs.items() if key != "id"},
            operation="update_node",
        )

    def mark_superseded(
        self,
        board_id: str,
        node_type: str,
        node_id: str,
        *,
        superseded_by: str,
        superseded_at: str,
        revocation_reason: str,
    ) -> None:
        self._update_node(
            board_id,
            node_type,
            node_id,
            {
                "superseded_by": superseded_by,
                "superseded_at": superseded_at,
                "revocation_reason": revocation_reason,
            },
            operation="mark_superseded",
        )

    def edge_exists(
        self,
        board_id: str,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
        rule_id: str | None = None,
    ) -> bool:
        resolve_relationship_endpoint_pair(
            edge_type, from_type=from_type, to_type=to_type
        )
        physical = resolve_relationship_table(edge_type, from_type, to_type)
        rule = " AND r.rule_id = $rule_id" if rule_id is not None else ""
        parameters: dict[str, Any] = {"from_id": from_id, "to_id": to_id}
        if rule_id is not None:
            parameters["rule_id"] = rule_id
        rows = self._read(
            board_id,
            operation="edge_exists",
            callback=lambda reader: _rows(
                reader.execute(
                    f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type}) "
                    "WHERE a.id = $from_id AND b.id = $to_id"
                    f"{rule} RETURN a.id LIMIT 1",
                    parameters,
                )
            ),
        )
        return bool(rows)

    def find_node_types(self, board_id: str, node_id: str) -> tuple[str, ...]:
        def find(reader: Any) -> tuple[str, ...]:
            found: list[str] = []
            for candidate in NODE_TYPES:
                result = reader.execute(
                    f"MATCH (n:{candidate}) WHERE n.id = $node_id "
                    "RETURN n.id LIMIT 1",
                    {"node_id": node_id},
                )
                if _rows(result):
                    found.append(candidate)
            return tuple(found)

        return self._read(board_id, operation="find_node_types", callback=find)

    def increment_attestation(
        self,
        board_id: str,
        node_type: str,
        node_id: str,
        *,
        attested_at: str,
    ) -> None:
        wanted_type = _node_type(node_type)
        stamp = _timestamp_from_value(attested_at)
        self._write(
            board_id,
            operation="increment_attestation",
            callback=lambda writer: writer.write(
                f"MATCH (n:{wanted_type}) WHERE n.id = $node_id "
                "SET n.attestation_count = coalesce(n.attestation_count, 1) + 1, "
                "n.last_attested_at = $attested_at",
                {"node_id": node_id, "attested_at": stamp},
                operation="increment_attestation",
            ),
        )

    @staticmethod
    def _count(result: object) -> int:
        rows = getattr(result, "rows", ())
        if (
            not isinstance(rows, (tuple, list))
            or len(rows) != 1
            or not isinstance(rows[0], (tuple, list))
            or len(rows[0]) != 1
            or type(rows[0][0]) is not int
            or rows[0][0] < 0
        ):
            raise GraphError(
                "Grafx returned an invalid mutation count.",
                details={"backend": "okto_grafx"},
            )
        return rows[0][0]

    def delete_nodes_by_session(self, board_id: str, session_id: str) -> int:
        def remove(writer: _FencedWriter) -> int:
            count = 0
            for candidate in NODE_TYPES:
                matched = self._count(
                    writer.query(
                        f"MATCH (n:{candidate}) "
                        "WHERE n.source_session_id = $session_id RETURN count(n)",
                        {"session_id": session_id},
                        operation="delete_nodes_by_session_count",
                    )
                )
                count += matched
                if matched:
                    writer.write(
                        f"MATCH (n:{candidate}) "
                        "WHERE n.source_session_id = $session_id DETACH DELETE n",
                        {"session_id": session_id},
                        operation="delete_nodes_by_session",
                    )
            return count

        return self._write(
            board_id,
            operation="delete_nodes_by_session",
            callback=remove,
        )

    def delete_edges_by_session(self, board_id: str, session_id: str) -> int:
        def remove(writer: _FencedWriter) -> int:
            count = 0
            for entry in PULSE_RELATIONSHIP_LAYOUT.entries:
                matched = self._count(
                    writer.query(
                        f"MATCH (a:{entry.from_type})-[r:{entry.physical_table}]->"
                        f"(b:{entry.to_type}) "
                        "WHERE r.created_by_session_id = $session_id RETURN count(r)",
                        {"session_id": session_id},
                        operation="delete_edges_by_session_count",
                    )
                )
                count += matched
                if matched:
                    writer.write(
                        f"MATCH (a:{entry.from_type})-[r:{entry.physical_table}]->"
                        f"(b:{entry.to_type}) "
                        "WHERE r.created_by_session_id = $session_id DELETE r",
                        {"session_id": session_id},
                        operation="delete_edges_by_session",
                    )
            return count

        return self._write(
            board_id,
            operation="delete_edges_by_session",
            callback=remove,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def find_by_topic(
        self,
        board_id: str,
        node_type: str,
        topic: str,
        filters: QueryFilters,
    ) -> list[list]:
        from okto_pulse.core.kg.scoring import (
            DECAY_REORDER_POOL_MULTIPLIER,
            apply_decay_reorder,
        )

        wanted_type = _node_type(node_type)
        if filters.max_rows <= 0:
            return []
        pool_size = max(
            filters.max_rows,
            filters.max_rows * DECAY_REORDER_POOL_MULTIPLIER,
        )
        statement = (
            f"MATCH (n:{wanted_type}) WHERE n.title CONTAINS $topic "
            "AND n.source_confidence >= $min_confidence "
            "AND n.relevance_score >= $min_relevance "
            f"AND {tpl.superseded_filter_clause('n')} "
            f"AND {tpl.active_read_filter_clause('n')} "
            "RETURN n.id, n.title, n.content, n.created_at, n.source_confidence, "
            "n.relevance_score, n.superseded_by, n.query_hits, n.last_queried_at, "
            "n.attestation_count, n.source_artifact_ref "
            "ORDER BY n.relevance_score DESC, n.created_at DESC LIMIT $max_rows"
        )
        rows = self._read(
            board_id,
            operation="find_by_topic",
            callback=lambda reader: _rows(
                reader.execute(
                    statement,
                    {
                        "topic": topic,
                        "min_confidence": filters.min_confidence,
                        "min_relevance": filters.min_relevance,
                        "max_rows": pool_size,
                        "include_superseded": filters.include_superseded,
                    },
                )
            ),
        )
        enriched = [
            {
                "node_id": row[0],
                "title": row[1],
                "content": row[2],
                "created_at": row[3],
                "source_confidence": row[4],
                "relevance_score": row[5],
                "superseded_by": row[6],
                "query_hits": row[7] or 0,
                "last_queried_at": row[8],
                "attestation_count": row[9],
                "source_artifact_ref": row[10],
            }
            for row in rows
        ]
        reordered = apply_decay_reorder(enriched, filters.max_rows)
        return [
            [
                row["node_id"],
                row["title"],
                row["content"],
                row["created_at"],
                row["source_confidence"],
                row["relevance_score"],
                row["superseded_by"],
                row["source_artifact_ref"],
            ]
            for row in reordered
        ]

    @staticmethod
    def _visible_neighbour(
        node: _NodeView,
        *,
        graph_layer: str,
        include_superseded: bool,
        include_code_traceability: bool,
    ) -> bool:
        return (
            (graph_layer == "all" or node.graph_layer == graph_layer)
            and (include_superseded or node.superseded_by is None)
            and tpl.is_visible_in_active_reads(node.revocation_reason)
            and (
                include_code_traceability
                or str(node.kind_of or "") not in CODE_TRACEABILITY_KG_SUBTYPES
            )
        )

    @staticmethod
    def _incident_entries(node_type: str) -> tuple[RelationshipLayoutEntry, ...]:
        return tuple(
            entry
            for entry in PULSE_RELATIONSHIP_LAYOUT.entries
            if node_type in {entry.from_type, entry.to_type}
        )

    def _adjacent(
        self,
        reader: Any,
        node: _NodeView,
    ) -> list[tuple[_NodeView, str]]:
        projection = (
            "neighbor.id, neighbor.title, neighbor.source_artifact_ref, "
            "neighbor.source_confidence, neighbor.graph_layer, "
            "neighbor.superseded_by, neighbor.revocation_reason, neighbor.kind_of"
        )
        adjacent: list[tuple[_NodeView, str]] = []
        for entry in self._incident_entries(node.node_type):
            if entry.from_type == node.node_type:
                result = reader.execute(
                    f"MATCH (center:{entry.from_type})-[r:{entry.physical_table}]->"
                    f"(neighbor:{entry.to_type}) WHERE center.id = $node_id "
                    f"RETURN {projection}",
                    {"node_id": node.node_id},
                )
                adjacent.extend(
                    (_node_view(row, node_type=entry.to_type), entry.logical_type)
                    for row in _rows(result)
                )
            if entry.to_type == node.node_type:
                result = reader.execute(
                    f"MATCH (neighbor:{entry.from_type})-[r:{entry.physical_table}]->"
                    f"(center:{entry.to_type}) WHERE center.id = $node_id "
                    f"RETURN {projection}",
                    {"node_id": node.node_id},
                )
                adjacent.extend(
                    (_node_view(row, node_type=entry.from_type), entry.logical_type)
                    for row in _rows(result)
                )
        return adjacent

    def find_by_artifact(
        self,
        board_id: str,
        artifact_id: str,
        filters: QueryFilters,
        *,
        graph_layer: str = "all",
        include_code_traceability: bool = True,
    ) -> list[list]:
        layer = _graph_layer(graph_layer)
        if filters.max_rows <= 0:
            return []

        def find(reader: Any) -> list[list]:
            center_rows = _rows(
                reader.execute(
                    "MATCH (center) WHERE center.source_artifact_ref = $artifact_id "
                    "AND center.source_confidence >= $min_confidence "
                    f"AND {tpl.active_read_filter_clause('center')} "
                    f"AND {tpl.code_traceability_visibility_clause('center')} "
                    "RETURN center.id, label(center), center.title, "
                    "center.source_artifact_ref, center.source_confidence, "
                    "center.graph_layer, center.superseded_by, "
                    "center.revocation_reason, center.kind_of LIMIT $max_rows",
                    {
                        "artifact_id": artifact_id,
                        "min_confidence": filters.min_confidence,
                        "include_code_traceability": include_code_traceability,
                        "max_rows": filters.max_rows,
                    },
                )
            )
            centers = [_node_view(row) for row in center_rows]
            cache: dict[tuple[str, str], list[tuple[_NodeView, str]]] = {}

            def neighbours(node: _NodeView) -> list[tuple[_NodeView, str]]:
                key = (node.node_type, node.node_id)
                if key not in cache:
                    cache[key] = self._adjacent(reader, node)
                return cache[key]

            answer: list[list] = []
            for center in centers:
                for hop1, rel1 in neighbours(center):
                    if not self._visible_neighbour(
                        hop1,
                        graph_layer=layer,
                        include_superseded=filters.include_superseded,
                        include_code_traceability=include_code_traceability,
                    ):
                        continue
                    second = [
                        (hop2, rel2)
                        for hop2, rel2 in neighbours(hop1)
                        if self._visible_neighbour(
                            hop2,
                            graph_layer=layer,
                            include_superseded=filters.include_superseded,
                            include_code_traceability=include_code_traceability,
                        )
                    ]
                    if not second:
                        answer.append(
                            [
                                center.node_id,
                                center.title,
                                hop1.node_id,
                                hop1.title,
                                None,
                                None,
                                rel1,
                                None,
                            ]
                        )
                    else:
                        answer.extend(
                            [
                                center.node_id,
                                center.title,
                                hop1.node_id,
                                hop1.title,
                                hop2.node_id,
                                hop2.title,
                                rel1,
                                rel2,
                            ]
                            for hop2, rel2 in second
                        )
                    if len(answer) >= filters.max_rows:
                        return answer[: filters.max_rows]
            return answer

        return self._read(board_id, operation="find_by_artifact", callback=find)

    def traverse_supersedence(
        self,
        board_id: str,
        decision_id: str,
        max_depth: int = 10,
        node_type: str = "Decision",
    ) -> list[list]:
        wanted_type = _node_type(node_type)
        if type(max_depth) is not int or isinstance(max_depth, bool):
            raise ValueError("max_depth must be an integer")
        if max_depth <= 0:
            return []
        physical = resolve_relationship_table("supersedes", wanted_type, wanted_type)

        def traverse(reader: Any) -> list[list]:
            answer: list[list] = []
            frontier = [decision_id]
            seen = {decision_id}
            for _depth in range(max_depth):
                following: list[str] = []
                for current_id in frontier:
                    rows = _rows(
                        reader.execute(
                            f"MATCH (current:{wanted_type})-[r:{physical}]->"
                            f"(next:{wanted_type}) WHERE current.id = $current_id "
                            f"AND {tpl.active_read_filter_clause('current')} "
                            f"AND {tpl.active_read_filter_clause('next')} "
                            "RETURN next.id, next.title, next.created_at, "
                            "next.superseded_by, next.superseded_at",
                            {"current_id": current_id},
                        )
                    )
                    for row in rows:
                        node_id = row[0]
                        if type(node_id) is not str or node_id in seen:
                            continue
                        seen.add(node_id)
                        following.append(node_id)
                        answer.append(row)
                if not following:
                    break
                frontier = following
            return answer

        return self._read(
            board_id,
            operation="traverse_supersedence",
            callback=traverse,
        )

    def find_contradictions(
        self,
        board_id: str,
        node_id: str | None,
        limit: int,
    ) -> list[list]:
        if limit <= 0:
            return []
        physical = resolve_relationship_table("contradicts", "Decision", "Decision")
        predicate = "(a.id = $node_id OR b.id = $node_id) AND " if node_id else ""
        parameters: dict[str, Any] = {"max_rows": limit}
        if node_id:
            parameters["node_id"] = node_id
        return self._read(
            board_id,
            operation="find_contradictions",
            callback=lambda reader: _rows(
                reader.execute(
                    f"MATCH (a:Decision)-[r:{physical}]->(b:Decision) WHERE "
                    f"{predicate}{tpl.active_read_filter_clause('a')} AND "
                    f"{tpl.active_read_filter_clause('b')} "
                    "RETURN a.id, a.title, b.id, b.title, r.confidence "
                    "LIMIT $max_rows",
                    parameters,
                )
            ),
        )

    def vector_search(
        self,
        board_id: str,
        node_type: str,
        query_vec: list[float],
        top_k: int,
        min_similarity: float,
        *,
        include_superseded: bool = False,
        graph_layer: str = "all",
    ) -> list[dict]:
        return self._vector_provider.vector_search(
            board_id,
            node_type,
            query_vec,
            top_k,
            min_similarity,
            include_superseded=include_superseded,
            graph_layer=graph_layer,
        )

    def find_active_by_source_ref(
        self,
        board_id: str,
        node_type: str,
        source_artifact_ref: str,
    ) -> dict[str, Any] | None:
        wanted_type = _node_type(node_type)
        rows = self._read(
            board_id,
            operation="find_active_by_source_ref",
            callback=lambda reader: _rows(
                reader.execute(
                    f"MATCH (n:{wanted_type}) "
                    "WHERE n.source_artifact_ref = $source_artifact_ref "
                    "AND n.superseded_by IS NULL "
                    f"AND {tpl.active_read_filter_clause('n')} "
                    "RETURN n.id, n.title, n.source_artifact_ref, n.content, "
                    "n.context, n.justification, coalesce(n.generation, 0) "
                    "ORDER BY coalesce(n.generation, 0) DESC, n.id DESC LIMIT 1",
                    {"source_artifact_ref": source_artifact_ref},
                )
            ),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "node_id": row[0],
            "node_type": wanted_type,
            "title": row[1] or "",
            "source_artifact_ref": row[2],
            "content": row[3],
            "context": row[4],
            "justification": row[5],
            "generation": int(row[6] or 0),
        }

    def get_constraint_detail(
        self,
        board_id: str,
        constraint_id: str,
    ) -> tuple[list[list], list[list], list[list]]:
        violation_table = resolve_relationship_table("violates", "Bug", "Constraint")

        def detail(reader: Any) -> tuple[list[list], list[list], list[list]]:
            main = _rows(
                reader.execute(
                    "MATCH (c:Constraint) WHERE c.id = $constraint_id "
                    f"AND {tpl.active_read_filter_clause('c')} "
                    "RETURN c.id, c.title, c.content, c.justification, "
                    "c.source_artifact_ref, c.source_confidence",
                    {"constraint_id": constraint_id},
                )
            )
            # The closed Pulse relationship authority has no
            # Decision->Constraint ``derives_from`` pair.  Returning the empty
            # logical origin set is the only non-fabricated answer.
            origins: list[list] = []
            violations = _rows(
                reader.execute(
                    f"MATCH (bug:Bug)-[r:{violation_table}]->(c:Constraint) "
                    "WHERE c.id = $constraint_id "
                    f"AND {tpl.active_read_filter_clause('c')} "
                    f"AND {tpl.active_read_filter_clause('bug')} "
                    "RETURN bug.id, bug.title",
                    {"constraint_id": constraint_id},
                )
            )
            return main, origins, violations

        return self._read(board_id, operation="get_constraint_detail", callback=detail)

    def get_alternatives(
        self,
        board_id: str,
        decision_id: str,
        limit: int,
    ) -> list[list]:
        if limit <= 0:
            return []
        physical = resolve_relationship_table("relates_to", "Decision", "Alternative")
        return self._read(
            board_id,
            operation="get_alternatives",
            callback=lambda reader: _rows(
                reader.execute(
                    f"MATCH (d:Decision)-[r:{physical}]->(alt:Alternative) "
                    "WHERE d.id = $decision_id "
                    f"AND {tpl.active_read_filter_clause('d')} "
                    f"AND {tpl.active_read_filter_clause('alt')} "
                    "RETURN alt.id, alt.title, alt.content, alt.justification, "
                    "alt.source_confidence, alt.source_artifact_ref "
                    "ORDER BY alt.source_confidence DESC LIMIT $max_rows",
                    {"decision_id": decision_id, "max_rows": limit},
                )
            ),
        )

    def get_learnings_for_area(
        self,
        board_id: str,
        area: str,
        filters: QueryFilters,
    ) -> list[list]:
        if filters.max_rows <= 0:
            return []
        physical = resolve_relationship_table("validates", "Learning", "Bug")
        return self._read(
            board_id,
            operation="get_learnings_for_area",
            callback=lambda reader: _rows(
                reader.execute(
                    f"MATCH (l:Learning)-[r:{physical}]->(b:Bug) "
                    "WHERE l.source_confidence >= $min_confidence "
                    "AND l.relevance_score >= $min_relevance "
                    "AND (b.title CONTAINS $area OR b.content CONTAINS $area) "
                    f"AND {tpl.active_read_filter_clause('l')} "
                    f"AND {tpl.active_read_filter_clause('b')} "
                    "RETURN l.id, l.title, l.content, l.justification, "
                    "l.source_confidence, b.id, b.title "
                    "ORDER BY l.relevance_score DESC, l.source_confidence DESC "
                    "LIMIT $max_rows",
                    {
                        "area": area,
                        "min_confidence": filters.min_confidence,
                        "min_relevance": filters.min_relevance,
                        "max_rows": filters.max_rows,
                    },
                )
            ),
        )

    # ------------------------------------------------------------------
    # Schema and capabilities
    # ------------------------------------------------------------------

    def get_schema_version(self, board_id: str) -> str | None:
        rows = self._read(
            board_id,
            operation="get_schema_version",
            callback=lambda reader: _rows(
                reader.execute(
                    "MATCH (m:BoardMeta) WHERE m.board_id = $board_id "
                    "RETURN m.schema_version",
                    {"board_id": board_id},
                )
            ),
        )
        if not rows:
            return None
        value = rows[0][0]
        if value is not None and type(value) is not str:
            raise GraphError(
                "Grafx returned an invalid schema version.",
                details={"backend": "okto_grafx"},
            )
        return value

    def get_schema_info(self, board_id: str, *, include_internal: bool = False) -> dict:
        del board_id
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "stable_node_types": [
                {"name": node_type, "stable": True} for node_type in NODE_TYPES
            ],
            "stable_rel_types": stable_rel_type_entries(),
            "vector_indexes": [
                {
                    "node_type": node_type,
                    "attribute": "embedding",
                    "dimension": EMBEDDING_DIMENSION,
                    "similarity_metric": "cosine",
                    "index_name": vector_index_name(node_type),
                }
                for node_type in VECTOR_INDEX_TYPES
            ],
        }
        if include_internal:
            result["internal_node_types"] = [{"name": "BoardMeta", "stable": False}]
            result["internal_rel_types"] = []
        return result

    def list_schema_objects(self, board_id: str) -> tuple[str, ...]:
        database = self._resolve(board_id, operation="list_schema_objects")
        try:
            validate_current_grafx_schema(database)
            logical_relationships = introspect_logical_relationships(database)
        except GraphError:
            raise
        except Exception as exc:
            _raise_mapped(exc, operation="list_schema_objects")
        names = {
            "BoardMeta",
            *(table.name for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes),
            *(definition.name for definition in logical_relationships),
        }
        return tuple(sorted(names))

    def list_node_properties(
        self,
        board_id: str,
        node_type: str,
    ) -> tuple[str, ...]:
        if node_type not in NODE_TYPES:
            return ()
        database = self._resolve(board_id, operation="list_node_properties")
        return introspect_node_properties(database, node_type)

    def capabilities(self) -> GraphCapabilities:
        return GraphCapabilities(
            indexed_similarity=True,
            schema_introspection=True,
            mutable_indexed_attributes=True,
        )


__all__ = ["CommunityGrafxGraphStore", "DatabaseResolver", "FenceRevalidator"]
