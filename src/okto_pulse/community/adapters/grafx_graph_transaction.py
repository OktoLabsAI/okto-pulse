"""Transactional Core ``GraphTransaction`` provider backed by Okto Grafx.

The provider owns a Grafx transaction, not the database handle.  Database
lifecycle and board routing remain composition concerns and are injected via a
resolver so one scope cannot accidentally close a handle shared by readers.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, NoReturn, Self

from okto_grafx import Database, Timestamp, Transaction, VectorValue
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
    GraphLockContention,
)
from okto_pulse.core.kg.interfaces.graph_transaction import (
    GraphNodePropertyBeforeImage,
    GraphStatementResult,
    ProjectionActiveSetIntent,
    ProjectionActiveSetReceipt,
    SpecLineageEdgeSnapshot,
    SpecLineageReconciliationError,
    SpecLineageReconciliationReceipt,
    is_spec_lineage_rule_id,
)
from okto_pulse.core.kg.schema_contract import (
    EDGE_METADATA_COLUMNS,
    MULTI_REL_TYPES,
    NODE_TYPES,
    REL_TYPES,
)

from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_IDENTITY_PROPERTIES = frozenset({"id", "source_session_id"})
_SOURCE_DELETED_REQUIRED_PROPERTIES = frozenset(
    {
        "id",
        "source_session_id",
        "title",
        "content",
        "context",
        "justification",
        "source_artifact_ref",
        "graph_layer",
        "maturity_status",
        "created_at",
        "created_by_agent",
        "source_confidence",
        "relevance_score",
        "query_hits",
        "priority_boost",
        "revocation_reason",
        "human_curated",
        "generation",
        "source_span_quote",
        "source_content_hash",
        "embedding",
    }
)
_SPEC_LINEAGE_REQUIRED_PROPERTIES = frozenset(
    {
        "confidence",
        "created_by_session_id",
        "created_at",
        *(name for name, _data_type in EDGE_METADATA_COLUMNS),
    }
)

DatabaseResolver = Callable[[str], Database]
FenceRevalidator = Callable[[str, str], None]
RelationshipTableResolver = Callable[[str, str, str], str]
RelationshipPair = tuple[str, str, str]


def _default_relationship_pairs() -> tuple[RelationshipPair, ...]:
    pairs = list(REL_TYPES)
    for edge_type, endpoint_pairs in MULTI_REL_TYPES:
        pairs.extend(
            (edge_type, from_type, to_type) for from_type, to_type in endpoint_pairs
        )
    return tuple(dict.fromkeys(pairs))


def _default_relationship_table(
    edge_type: str,
    _from_type: str,
    _to_type: str,
) -> str:
    return edge_type


def _identifier(kind: str, value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"invalid graph {kind}: {value!r}")
    return value


def _timestamp_from_iso(value: str | datetime) -> Timestamp:
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


def _normalize_value(value: Any) -> Any:
    if isinstance(value, Timestamp):
        rendered = datetime.fromtimestamp(
            value.micros / 1_000_000,
            tz=UTC,
        ).isoformat(timespec="microseconds")
        return rendered.replace("+00:00", "Z")
    if isinstance(value, VectorValue):
        return [_normalize_value(item) for item in value.values]
    if isinstance(value, tuple):
        return tuple(_normalize_value(item) for item in value)
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    return value


class _GrafxTransactionScope:
    """One staged, fenced Grafx write transaction for a Pulse board."""

    def __init__(
        self,
        board_id: str,
        database: Database,
        transaction: Transaction,
        revalidate_fence: FenceRevalidator,
        *,
        node_types: tuple[str, ...],
        relationship_pairs: tuple[RelationshipPair, ...],
        relationship_table_resolver: RelationshipTableResolver,
    ) -> None:
        self._board_id = board_id
        self._database = database
        self._transaction = transaction
        self._revalidate_fence = revalidate_fence
        self._node_types = node_types
        self._relationship_pairs = relationship_pairs
        self._relationship_table_resolver = relationship_table_resolver
        self._finished = False

    def _require_active(self) -> None:
        if self._finished or not self._transaction.active:
            raise GraphError(
                "Grafx graph transaction scope is already finished.",
                details={"backend": "okto_grafx", "board_id": self._board_id},
            )

    def _fence(self, phase: str) -> None:
        self._require_active()
        self._revalidate_fence(self._board_id, phase)

    def _query(
        self,
        statement: str,
        params: Mapping[str, Any] | None = None,
        *,
        operation: str,
    ):
        self._require_active()
        try:
            return self._transaction.execute(statement, dict(params or {}))
        except Exception as exc:
            mapped = map_grafx_error(exc, operation=operation)
            if mapped is exc:
                raise
            raise mapped from exc

    def _mutation(
        self,
        statement: str,
        params: Mapping[str, Any] | None = None,
        *,
        operation: str,
    ):
        self._fence(operation)
        return self._query(statement, params, operation=operation)

    def _node_definition(self, node_type: str):
        name = _identifier("node type", node_type)
        try:
            definition = self._database.catalog.catalog.table(name)
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="node_schema")
            raise mapped from exc
        if definition.kind != "node":
            raise GraphCapabilityUnavailable(
                f"Grafx table {name!r} is not a node table.",
                details={"backend": "okto_grafx", "table": name},
            )
        return definition

    def _relationship_definition(
        self,
        edge_type: str,
        from_type: str,
        to_type: str,
    ):
        logical = _identifier("relationship type", edge_type)
        source = _identifier("source node type", from_type)
        target = _identifier("target node type", to_type)
        physical = _identifier(
            "relationship table",
            self._relationship_table_resolver(logical, source, target),
        )
        try:
            definition = self._database.catalog.catalog.table(physical)
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="relationship_schema")
            raise mapped from exc
        if (
            definition.kind != "rel"
            or definition.from_table != source
            or definition.to_table != target
        ):
            raise GraphCapabilityUnavailable(
                f"Grafx relationship table {physical!r} does not implement "
                f"{logical}({source}->{target}).",
                details={
                    "backend": "okto_grafx",
                    "edge_type": logical,
                    "physical_table": physical,
                    "from_type": source,
                    "to_type": target,
                },
            )
        return physical, definition

    @staticmethod
    def _column_map(definition: Any) -> dict[str, Any]:
        return {column.name: column for column in definition.columns}

    @staticmethod
    def _coerce_value(column: Any, value: Any) -> Any:
        if value is None:
            return None
        if column.type.name == "TIMESTAMP" and not isinstance(value, Timestamp):
            if not isinstance(value, (str, datetime)):
                raise ValueError(
                    f"timestamp property {column.name!r} requires ISO text or datetime"
                )
            return _timestamp_from_iso(value)
        return value

    def _coerce_properties(
        self,
        definition: Any,
        attrs: Mapping[str, Any],
        *,
        forbidden: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        columns = self._column_map(definition)
        unknown = set(attrs).difference(columns)
        rejected = set(attrs).intersection(forbidden)
        if unknown or rejected:
            raise ValueError(
                "invalid graph properties: "
                f"unknown={sorted(unknown)!r} forbidden={sorted(rejected)!r}"
            )
        return {
            name: self._coerce_value(columns[name], value)
            for name, value in attrs.items()
        }

    @staticmethod
    def _assignments(alias: str, names: tuple[str, ...]) -> str:
        return ", ".join(
            f"{alias}.{name} = $value_{index}" for index, name in enumerate(names)
        )

    @staticmethod
    def _assignment_params(
        names: tuple[str, ...],
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {f"value_{index}": values[name] for index, name in enumerate(names)}

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        """Fail closed until M-PULSE-2 exposes a backend-neutral query subset."""

        del statement, params
        self._require_active()
        raise GraphCapabilityUnavailable(
            "Generic graph statements are unavailable on the Grafx transaction provider.",
            details={
                "backend": "okto_grafx",
                "capability": "generic_execute",
                "milestone": "M-PULSE-2",
            },
        )

    def create_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
        *,
        source_session_id: str,
    ) -> None:
        definition = self._node_definition(node_type)
        values = self._coerce_properties(
            definition,
            attrs,
            forbidden=_IDENTITY_PROPERTIES,
        )
        values = {
            "id": node_id,
            "source_session_id": source_session_id,
            **values,
        }
        bindings = ", ".join(
            f"{name}: $value_{index}" for index, name in enumerate(values)
        )
        params = {
            f"value_{index}": value for index, value in enumerate(values.values())
        }
        self._mutation(
            f"CREATE (n:{definition.name} {{{bindings}}})",
            params,
            operation="create_node",
        )

    def update_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
    ) -> None:
        definition = self._node_definition(node_type)
        values = self._coerce_properties(
            definition,
            attrs,
            forbidden=frozenset({"id"}),
        )
        if not values:
            return
        names = tuple(values)
        params = {"node_id": node_id, **self._assignment_params(names, values)}
        self._mutation(
            f"MATCH (n:{definition.name}) WHERE n.id = $node_id "
            f"SET {self._assignments('n', names)}",
            params,
            operation="update_node",
        )

    def _node_snapshot(self, node_type: str, node_id: str) -> dict[str, Any] | None:
        definition = self._node_definition(node_type)
        names = tuple(column.name for column in definition.columns)
        projection = ", ".join(f"n.{name}" for name in names)
        result = self._query(
            f"MATCH (n:{definition.name}) WHERE n.id = $node_id "
            f"RETURN {projection} LIMIT 1",
            {"node_id": node_id},
            operation="snapshot_node",
        )
        if not result.rows:
            return None
        return {
            name: _normalize_value(result.rows[0][index])
            for index, name in enumerate(names)
        }

    def _incident_edge_snapshot(
        self,
        node_type: str,
        node_id: str,
    ) -> Counter[tuple[Any, ...]]:
        """Read every physical relationship table incident to the typed node.

        The replacement proof cannot trust the configured logical relationship
        pairs: a stale or incomplete mapping must not make an unobserved edge
        disappear from the multiset that is being certified.  The committed
        Grafx catalog is the authority for this safety check.
        """

        snapshots: Counter[tuple[Any, ...]] = Counter()
        for (
            physical,
            from_type,
            to_type,
            definition,
        ) in self._incident_relationship_definitions(node_type):
            properties = tuple(column.name for column in definition.columns[2:])
            projection = ["a.id", "b.id", *(f"r.{name}" for name in properties)]
            predicate = self._incident_predicate(node_type, from_type, to_type)
            result = self._query(
                f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type}) "
                f"WHERE {predicate} RETURN {', '.join(projection)}",
                {"node_id": node_id},
                operation="snapshot_incident_edges",
            )
            for row in result.rows:
                snapshots[
                    (
                        physical,
                        from_type,
                        to_type,
                        *(_normalize_value(value) for value in row),
                    )
                ] += 1
        return snapshots

    def _incident_relationship_definitions(
        self,
        node_type: str,
    ) -> tuple[tuple[str, str, str, Any], ...]:
        wanted = _identifier("node type", node_type)
        try:
            definitions = self._database.catalog.catalog.tables()
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="snapshot_incident_schema")
            raise mapped from exc
        incident: list[tuple[str, str, str, Any]] = []
        for definition in definitions:
            if definition.kind != "rel" or wanted not in {
                definition.from_table,
                definition.to_table,
            }:
                continue
            physical = _identifier("relationship table", definition.name)
            from_type = _identifier("source node type", definition.from_table)
            to_type = _identifier("target node type", definition.to_table)
            incident.append((physical, from_type, to_type, definition))
        return tuple(incident)

    @staticmethod
    def _incident_predicate(
        node_type: str,
        from_type: str,
        to_type: str,
    ) -> str:
        predicates: list[str] = []
        if from_type == node_type:
            predicates.append("a.id = $node_id")
        if to_type == node_type:
            predicates.append("b.id = $node_id")
        if not predicates:
            raise AssertionError("relationship definition is not incident to node type")
        return " OR ".join(predicates)

    def replace_node_payload(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
        *,
        source_session_id: str,
    ) -> bool:
        """Replace every mutable value in one SET and verify node plus edges."""

        # This primitive performs reads to build its proof before the one SET.
        # Authority must therefore be checked before *any* engine access, not
        # merely immediately before the eventual write.
        self._fence("replace_node_payload")
        definition = self._node_definition(node_type)
        supplied = self._coerce_properties(
            definition,
            attrs,
            forbidden=_IDENTITY_PROPERTIES,
        )
        before = self._node_snapshot(node_type, node_id)
        if before is None:
            return False
        before_edges = self._incident_edge_snapshot(node_type, node_id)

        mutable_names = tuple(
            column.name for column in definition.columns if column.name != "id"
        )
        expected_raw: dict[str, Any] = {
            name: None for name in mutable_names if name != "source_session_id"
        }
        expected_raw.update(supplied)
        expected_raw["source_session_id"] = source_session_id
        expected = {
            "id": _normalize_value(node_id),
            **{name: _normalize_value(expected_raw[name]) for name in mutable_names},
        }
        params = {
            "node_id": node_id,
            **self._assignment_params(mutable_names, expected_raw),
        }
        result = self._mutation(
            f"MATCH (n:{definition.name}) WHERE n.id = $node_id "
            f"SET {self._assignments('n', mutable_names)} RETURN n.id",
            params,
            operation="replace_node_payload",
        )
        if not result.rows:
            raise GraphError(
                "Grafx node disappeared during atomic payload replacement.",
                details={"backend": "okto_grafx", "node_type": node_type},
            )
        after = self._node_snapshot(node_type, node_id)
        after_edges = self._incident_edge_snapshot(node_type, node_id)
        if after != expected or after_edges != before_edges:
            raise GraphError(
                "Grafx could not confirm the atomic node payload replacement.",
                details={
                    "backend": "okto_grafx",
                    "node_type": node_type,
                    "payload_confirmed": after == expected,
                    "edges_confirmed": after_edges == before_edges,
                },
            )
        return True

    def replace_with_source_deleted_tombstone(
        self,
        node_type: str,
        node_id: str,
        *,
        graph_layer: str,
        maturity_status: str,
        revocation_reason: str,
        relevance_score: float,
    ) -> bool:
        """Atomically erase semantic payload and every incident relationship."""

        self._fence("replace_with_source_deleted_tombstone")
        definition = self._node_definition(node_type)
        columns = self._column_map(definition)
        missing = _SOURCE_DELETED_REQUIRED_PROPERTIES.difference(columns)
        if missing:
            raise GraphCapabilityUnavailable(
                "Grafx node schema cannot prove source-deleted payload erasure.",
                details={
                    "backend": "okto_grafx",
                    "node_type": node_type,
                    "missing_properties": tuple(sorted(missing)),
                },
            )
        before = self._node_snapshot(node_type, node_id)
        if before is None:
            return False

        mutable_names = tuple(
            column.name for column in definition.columns if column.name != "id"
        )
        desired: dict[str, Any] = {
            "source_session_id": str(
                before.get("source_session_id") or "source-deletion-tombstone"
            ),
            "title": "",
            "content": "",
            "context": "",
            "justification": "",
            "source_artifact_ref": str(before.get("source_artifact_ref") or ""),
            "graph_layer": graph_layer,
            "maturity_status": maturity_status,
            "created_by_agent": str(
                before.get("created_by_agent") or "system:source-deletion"
            ),
            "source_confidence": 0.0,
            "relevance_score": relevance_score,
            "query_hits": 0,
            "priority_boost": 0.0,
            "revocation_reason": revocation_reason,
            "human_curated": False,
            "generation": int(before.get("generation") or 0),
            "source_span_quote": "",
        }
        if before.get("created_at") is not None:
            desired["created_at"] = before["created_at"]
        expected_raw: dict[str, Any] = {name: None for name in mutable_names}
        expected_raw.update(
            {
                name: self._coerce_value(columns[name], value)
                for name, value in desired.items()
                if name != "id"
            }
        )
        replacement_raw = {"id": node_id, **expected_raw}
        replacement_names = tuple(replacement_raw)
        bindings = ", ".join(
            f"{name}: $value_{index}" for index, name in enumerate(replacement_names)
        )
        params = {
            "node_id": node_id,
            **self._assignment_params(replacement_names, replacement_raw),
        }
        expected = {
            "id": _normalize_value(node_id),
            **{name: _normalize_value(expected_raw[name]) for name in mutable_names},
        }
        statement = (
            f"MATCH (n:{definition.name}) WHERE n.id = $node_id "
            f"DETACH DELETE n CREATE (t:{definition.name} {{{bindings}}}) RETURN t.id"
        )

        # Revalidate immediately before entering the one statement that can stage effects.  From
        # this point through both confirmations, any refusal poisons and rolls back the complete
        # scope: the Core reconciler catches per-node errors, so leaving staged residue here would
        # otherwise let its later context-manager commit publish a partial erasure.
        self._fence("replace_with_source_deleted_tombstone")
        try:
            result = self._query(
                statement,
                params,
                operation="replace_with_source_deleted_tombstone",
            )
            if result.rows != ((_normalize_value(node_id),),):
                raise GraphError(
                    "Grafx node swap was not confirmed by the mutation statement.",
                    details={"backend": "okto_grafx", "node_type": node_type},
                )
            after = self._node_snapshot(node_type, node_id)
            after_edges = self._incident_edge_snapshot(node_type, node_id)
            if after != expected or after_edges:
                raise GraphError(
                    "Grafx could not confirm the source-deleted tombstone.",
                    details={
                        "backend": "okto_grafx",
                        "node_type": node_type,
                        "payload_confirmed": after == expected,
                        "edges_removed": not after_edges,
                    },
                )
        except BaseException as primary_error:
            cleanup_error = self._abort_after_staged_failure(
                operation="replace_with_source_deleted_tombstone"
            )
            if cleanup_error is not None:
                try:
                    primary_error.add_note(
                        "Grafx rollback also failed while discarding a staged "
                        "source-deleted tombstone."
                    )
                except BaseException:  # noqa: BLE001, S110 - diagnostic only
                    pass
                raise primary_error from cleanup_error
            raise
        return True

    def _abort_after_staged_failure(
        self,
        *,
        operation: str,
    ) -> BaseException | None:
        """Poison the scope, discard staged effects and report cleanup failure."""

        self._finished = True
        try:
            self._transaction.rollback()
        except BaseException as cleanup_error:  # noqa: BLE001 - preserve primary
            return cleanup_error
        if self._transaction.active:
            return GraphError(
                "Grafx transaction remained active after rollback returned.",
                details={
                    "backend": "okto_grafx",
                    "board_id": self._board_id,
                    "operation": operation,
                    "scope_poisoned": True,
                    "cleanup_confirmed": False,
                },
            )
        return None

    def snapshot_node_properties(
        self,
        node_type: str,
        node_id: str,
        property_names: tuple[str, ...],
    ) -> GraphNodePropertyBeforeImage | None:
        definition = self._node_definition(node_type)
        columns = self._column_map(definition)
        names = tuple(_identifier("property name", name) for name in property_names)
        unknown = set(names).difference(columns)
        if unknown:
            raise ValueError(f"unknown graph properties: {sorted(unknown)!r}")
        if not names:
            exists = self._query(
                f"MATCH (n:{definition.name}) WHERE n.id = $node_id RETURN n.id LIMIT 1",
                {"node_id": node_id},
                operation="snapshot_node_properties",
            )
            attrs: dict[str, Any] = {}
            found = bool(exists.rows)
        else:
            projection = ", ".join(f"n.{name}" for name in names)
            result = self._query(
                f"MATCH (n:{definition.name}) WHERE n.id = $node_id "
                f"RETURN {projection} LIMIT 1",
                {"node_id": node_id},
                operation="snapshot_node_properties",
            )
            found = bool(result.rows)
            attrs = (
                {
                    name: _normalize_value(result.rows[0][index])
                    for index, name in enumerate(names)
                }
                if found
                else {}
            )
        if not found:
            return None
        return GraphNodePropertyBeforeImage(
            node_type=node_type,
            node_id=node_id,
            attrs=attrs,
        )

    def restore_node_properties(
        self,
        before_image: GraphNodePropertyBeforeImage,
    ) -> None:
        definition = self._node_definition(before_image.node_type)
        values = self._coerce_properties(
            definition,
            before_image.attrs,
            forbidden=frozenset({"id"}),
        )
        if not values:
            return
        names = tuple(values)
        result = self._mutation(
            f"MATCH (n:{definition.name}) WHERE n.id = $node_id "
            f"SET {self._assignments('n', names)} RETURN n.id",
            {
                "node_id": before_image.node_id,
                **self._assignment_params(names, values),
            },
            operation="restore_node_properties",
        )
        if not result.rows:
            raise LookupError("graph node missing during property before-image restore")

    def mark_superseded(
        self,
        node_type: str,
        node_id: str,
        *,
        superseded_by: str,
        superseded_at: str,
        revocation_reason: str,
    ) -> None:
        self.update_node(
            node_type,
            node_id,
            {
                "superseded_by": superseded_by,
                "superseded_at": superseded_at,
                "revocation_reason": revocation_reason,
            },
        )

    def edge_exists(
        self,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
        rule_id: str | None = None,
    ) -> bool:
        physical, definition = self._relationship_definition(
            edge_type,
            from_type,
            to_type,
        )
        predicate = "a.id = $from_id AND b.id = $to_id"
        params: dict[str, Any] = {"from_id": from_id, "to_id": to_id}
        if rule_id is not None:
            if "rule_id" not in self._column_map(definition):
                raise GraphCapabilityUnavailable(
                    f"Grafx relationship table {physical!r} has no rule_id property."
                )
            predicate += " AND r.rule_id = $rule_id"
            params["rule_id"] = rule_id
        result = self._query(
            f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type}) "
            f"WHERE {predicate} RETURN a.id LIMIT 1",
            params,
            operation="edge_exists",
        )
        return bool(result.rows)

    def create_edge(
        self,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
        attrs: dict[str, Any],
    ) -> bool:
        physical, definition = self._relationship_definition(
            edge_type,
            from_type,
            to_type,
        )
        values = self._coerce_properties(
            definition,
            attrs,
            forbidden=frozenset({"_from", "_to"}),
        )
        bindings = ", ".join(
            f"{name}: $value_{index}" for index, name in enumerate(values)
        )
        properties = f" {{{bindings}}}" if bindings else ""
        params = {
            "from_id": from_id,
            "to_id": to_id,
            **{f"value_{index}": value for index, value in enumerate(values.values())},
        }
        result = self._mutation(
            f"MATCH (a:{from_type}), (b:{to_type}) "
            "WHERE a.id = $from_id AND b.id = $to_id "
            f"CREATE (a)-[:{physical}{properties}]->(b) RETURN a.id, b.id",
            params,
            operation="create_edge",
        )
        return bool(result.rows)

    @staticmethod
    def _unsupported(capability: str) -> NoReturn:
        raise GraphCapabilityUnavailable(
            f"Grafx graph transaction capability {capability!r} is not implemented yet.",
            details={
                "backend": "okto_grafx",
                "capability": capability,
                "milestone": "M-PULSE-1",
            },
        )

    def reconcile_spec_lineage_parent(
        self,
        source_id: str,
        target_id: str,
        attrs: dict[str, Any],
    ) -> SpecLineageReconciliationReceipt:
        """Stage one exclusive deterministic parent or leave no staged effect."""

        self._fence("reconcile_spec_lineage_parent")
        rule_id = str(attrs.get("rule_id") or "")
        if not is_spec_lineage_rule_id(rule_id):
            raise SpecLineageReconciliationError(
                "spec_lineage_rule_out_of_scope",
                f"Rule {rule_id!r} is outside the exclusive Spec-parent family.",
            )
        physical, definition, properties = self._spec_lineage_schema()
        write_attrs, normalized_target_attrs = self._materialize_spec_lineage_attrs(
            definition,
            properties,
            attrs,
        )
        endpoints = self._query(
            "MATCH (source:Entity), (target:Entity) "
            "WHERE source.id = $source_id AND target.id = $target_id "
            "RETURN source.id, target.id LIMIT 1",
            {"source_id": source_id, "target_id": target_id},
            operation="reconcile_spec_lineage_endpoints",
        )
        if not endpoints.rows:
            raise SpecLineageReconciliationError(
                "spec_lineage_endpoint_not_found",
                "Both the Spec source and its new parent must exist as Entity "
                "nodes before lineage reconciliation.",
            )

        existing = self._spec_lineage_edges(
            source_id,
            physical=physical,
            properties=properties,
        )
        exact_exists = any(
            edge.target_id == target_id and edge.rule_id == rule_id for edge in existing
        )
        old_edges = tuple(
            edge
            for edge in existing
            if is_spec_lineage_rule_id(edge.rule_id)
            and not (edge.target_id == target_id and edge.rule_id == rule_id)
        )
        ambiguous_legacy_edges = self._ambiguous_spec_lineage_edges(existing)
        receipt = SpecLineageReconciliationReceipt(
            source_id=source_id,
            target_id=target_id,
            target_rule_id=rule_id,
            target_attrs=dict(attrs),
            new_edge_created=False,
            removed_edges=old_edges,
            ambiguous_legacy_edges=ambiguous_legacy_edges,
        )
        mutation_started = False
        try:
            if not exact_exists:
                self._fence("create_spec_lineage_parent")
                mutation_started = True
                created = self._create_spec_lineage_edge(
                    source_id,
                    target_id,
                    write_attrs,
                    physical=physical,
                )
                receipt = SpecLineageReconciliationReceipt(
                    source_id=source_id,
                    target_id=target_id,
                    target_rule_id=rule_id,
                    target_attrs=dict(attrs),
                    new_edge_created=created,
                    removed_edges=old_edges,
                    ambiguous_legacy_edges=ambiguous_legacy_edges,
                )
                expected = SpecLineageEdgeSnapshot(
                    source_id=source_id,
                    target_id=target_id,
                    rule_id=rule_id,
                    attrs=normalized_target_attrs,
                )
                replacement = tuple(
                    edge
                    for edge in self._spec_lineage_edges(
                        source_id,
                        physical=physical,
                        properties=properties,
                    )
                    if edge.target_id == target_id and edge.rule_id == rule_id
                )
                if (
                    not created
                    or len(replacement) != 1
                    or self._spec_lineage_edge_signature(replacement[0])
                    != self._spec_lineage_edge_signature(expected)
                ):
                    raise SpecLineageReconciliationError(
                        "spec_lineage_new_parent_create_failed",
                        "The new Spec-parent edge could not be confirmed exactly; "
                        "the Grafx transaction was discarded.",
                        receipt=receipt,
                    )

            for snapshot in old_edges:
                self._fence("delete_spec_lineage_edge")
                mutation_started = True
                self._delete_spec_lineage_edge(
                    snapshot,
                    physical=physical,
                    properties=properties,
                )

            remaining_old = tuple(
                edge
                for edge in self._spec_lineage_edges(
                    source_id,
                    physical=physical,
                    properties=properties,
                )
                if is_spec_lineage_rule_id(edge.rule_id)
                and not (edge.target_id == target_id and edge.rule_id == rule_id)
            )
            if remaining_old:
                raise SpecLineageReconciliationError(
                    "spec_lineage_old_parent_cleanup_failed",
                    "Grafx could not confirm removal of every old Spec parent; "
                    "the transaction was discarded.",
                    receipt=receipt,
                )
        except BaseException as primary_error:
            if mutation_started:
                self._abort_spec_lineage_failure(
                    primary_error,
                    receipt=receipt,
                    operation="reconcile_spec_lineage_parent",
                )
            raise
        return receipt

    def compensate_spec_lineage_parent(
        self,
        receipt: SpecLineageReconciliationReceipt,
    ) -> None:
        """Restore every before-image before removing this attempt's replacement."""

        self._fence("compensate_spec_lineage_parent")
        physical, definition, properties = self._spec_lineage_schema()
        normalized_removed = tuple(
            self._normalize_spec_lineage_snapshot(
                snapshot,
                definition=definition,
                properties=properties,
            )
            for snapshot in receipt.removed_edges
        )
        identities = [self._spec_lineage_identity(edge) for edge in normalized_removed]
        if (
            any(edge.source_id != receipt.source_id for edge in normalized_removed)
            or any(
                not is_spec_lineage_rule_id(edge.rule_id) for edge in normalized_removed
            )
            or len(identities) != len(set(identities))
        ):
            raise SpecLineageReconciliationError(
                "spec_lineage_edge_metadata_inconsistent",
                "The Spec-lineage receipt contains an ambiguous before-image.",
                receipt=receipt,
            )

        replacement: SpecLineageEdgeSnapshot | None = None
        if receipt.new_edge_created:
            if receipt.target_id is None or receipt.target_rule_id is None:
                raise SpecLineageReconciliationError(
                    "spec_lineage_edge_metadata_inconsistent",
                    "The Spec-lineage receipt omits its created replacement identity.",
                    receipt=receipt,
                )
            replacement = self._normalize_spec_lineage_snapshot(
                SpecLineageEdgeSnapshot(
                    source_id=receipt.source_id,
                    target_id=receipt.target_id,
                    rule_id=receipt.target_rule_id,
                    attrs=dict(receipt.target_attrs),
                ),
                definition=definition,
                properties=properties,
            )
            if not is_spec_lineage_rule_id(
                replacement.rule_id
            ) or self._spec_lineage_identity(replacement) in set(identities):
                raise SpecLineageReconciliationError(
                    "spec_lineage_edge_metadata_inconsistent",
                    "The Spec-lineage replacement identity conflicts with its "
                    "before-image.",
                    receipt=receipt,
                )

        current = self._spec_lineage_edges(
            receipt.source_id,
            physical=physical,
            properties=properties,
        )
        current_by_identity = {
            self._spec_lineage_identity(edge): edge for edge in current
        }
        missing: list[SpecLineageEdgeSnapshot] = []
        for snapshot in normalized_removed:
            existing = current_by_identity.get(self._spec_lineage_identity(snapshot))
            if existing is None:
                missing.append(snapshot)
            elif self._spec_lineage_edge_signature(existing) != (
                self._spec_lineage_edge_signature(snapshot)
            ):
                raise SpecLineageReconciliationError(
                    "spec_lineage_edge_metadata_inconsistent",
                    "An old Spec parent exists with different metadata; "
                    "automatic compensation was refused.",
                    receipt=receipt,
                )

        replacement_exists = False
        if replacement is not None:
            existing_replacement = current_by_identity.get(
                self._spec_lineage_identity(replacement)
            )
            if existing_replacement is not None:
                if self._spec_lineage_edge_signature(existing_replacement) != (
                    self._spec_lineage_edge_signature(replacement)
                ):
                    raise SpecLineageReconciliationError(
                        "spec_lineage_edge_metadata_inconsistent",
                        "The replacement Spec parent exists with different metadata; "
                        "automatic compensation was refused.",
                        receipt=receipt,
                    )
                replacement_exists = True

        for node_id in dict.fromkeys(
            node_id
            for snapshot in missing
            for node_id in (snapshot.source_id, snapshot.target_id)
        ):
            if not self._entity_exists(node_id):
                raise SpecLineageReconciliationError(
                    "spec_lineage_old_parent_restore_failed",
                    f"Entity {node_id!r} required by the before-image is missing.",
                    receipt=receipt,
                )

        mutation_started = False
        try:
            for snapshot in missing:
                write_attrs, _normalized = self._materialize_spec_lineage_attrs(
                    definition,
                    properties,
                    snapshot.attrs,
                )
                self._fence("restore_spec_lineage_edge")
                mutation_started = True
                created = self._create_spec_lineage_edge(
                    snapshot.source_id,
                    snapshot.target_id,
                    write_attrs,
                    physical=physical,
                )
                restored = tuple(
                    edge
                    for edge in self._spec_lineage_edges(
                        snapshot.source_id,
                        physical=physical,
                        properties=properties,
                    )
                    if self._spec_lineage_identity(edge)
                    == self._spec_lineage_identity(snapshot)
                )
                if (
                    not created
                    or len(restored) != 1
                    or self._spec_lineage_edge_signature(restored[0])
                    != self._spec_lineage_edge_signature(snapshot)
                ):
                    raise SpecLineageReconciliationError(
                        "spec_lineage_old_parent_restore_failed",
                        "An old Spec parent could not be restored exactly; the "
                        "Grafx transaction was discarded.",
                        receipt=receipt,
                    )

            if replacement is not None and replacement_exists:
                self._fence("remove_compensated_spec_lineage_replacement")
                mutation_started = True
                try:
                    self._delete_spec_lineage_edge(
                        replacement,
                        physical=physical,
                        properties=properties,
                    )
                except SpecLineageReconciliationError as exc:
                    raise SpecLineageReconciliationError(
                        "spec_lineage_replacement_remove_failed",
                        "The replacement Spec parent could not be removed exactly; "
                        "the Grafx transaction was discarded.",
                        receipt=receipt,
                    ) from exc
        except BaseException as primary_error:
            if mutation_started:
                self._abort_spec_lineage_failure(
                    primary_error,
                    receipt=receipt,
                    operation="compensate_spec_lineage_parent",
                )
            raise

    def clear_spec_lineage_parent(
        self,
        source_id: str,
    ) -> SpecLineageReconciliationReceipt:
        """Stage removal of explicit deterministic parents, preserving all others."""

        self._fence("clear_spec_lineage_parent")
        physical, _definition, properties = self._spec_lineage_schema()
        source = self._query(
            "MATCH (source:Entity) WHERE source.id = $source_id "
            "RETURN source.id LIMIT 1",
            {"source_id": source_id},
            operation="clear_spec_lineage_source",
        )
        if not source.rows:
            raise SpecLineageReconciliationError(
                "spec_lineage_source_not_found",
                "The Spec source must exist as an Entity node before lineage "
                "can be cleared.",
            )

        existing = self._spec_lineage_edges(
            source_id,
            physical=physical,
            properties=properties,
        )
        old_edges = tuple(
            edge for edge in existing if is_spec_lineage_rule_id(edge.rule_id)
        )
        receipt = SpecLineageReconciliationReceipt(
            source_id=source_id,
            target_id=None,
            target_rule_id=None,
            target_attrs={},
            new_edge_created=False,
            removed_edges=old_edges,
            ambiguous_legacy_edges=self._ambiguous_spec_lineage_edges(existing),
        )
        mutation_started = False
        try:
            for snapshot in old_edges:
                self._fence("delete_spec_lineage_edge")
                mutation_started = True
                self._delete_spec_lineage_edge(
                    snapshot,
                    physical=physical,
                    properties=properties,
                )
            if any(
                is_spec_lineage_rule_id(edge.rule_id)
                for edge in self._spec_lineage_edges(
                    source_id,
                    physical=physical,
                    properties=properties,
                )
            ):
                raise SpecLineageReconciliationError(
                    "spec_lineage_clear_failed",
                    "Grafx could not confirm removal of every deterministic "
                    "Spec parent; the transaction was discarded.",
                    receipt=receipt,
                )
        except BaseException as primary_error:
            if mutation_started:
                self._abort_spec_lineage_failure(
                    primary_error,
                    receipt=receipt,
                    operation="clear_spec_lineage_parent",
                )
            raise
        return receipt

    def _spec_lineage_schema(self) -> tuple[str, Any, tuple[str, ...]]:
        physical, definition = self._relationship_definition(
            "belongs_to",
            "Entity",
            "Entity",
        )
        properties = tuple(column.name for column in definition.columns[2:])
        missing = _SPEC_LINEAGE_REQUIRED_PROPERTIES.difference(properties)
        if missing:
            raise GraphCapabilityUnavailable(
                f"Grafx relationship table {physical!r} lacks required Spec-lineage "
                f"properties: {sorted(missing)!r}."
            )
        return physical, definition, properties

    def _materialize_spec_lineage_attrs(
        self,
        definition: Any,
        properties: tuple[str, ...],
        attrs: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        write_attrs = self._coerce_properties(
            definition,
            attrs,
            forbidden=frozenset({"_from", "_to"}),
        )
        normalized = {
            name: _normalize_value(write_attrs.get(name)) for name in properties
        }
        return write_attrs, normalized

    def _normalize_spec_lineage_snapshot(
        self,
        snapshot: SpecLineageEdgeSnapshot,
        *,
        definition: Any,
        properties: tuple[str, ...],
    ) -> SpecLineageEdgeSnapshot:
        _write_attrs, normalized = self._materialize_spec_lineage_attrs(
            definition,
            properties,
            snapshot.attrs,
        )
        if str(normalized.get("rule_id") or "") != snapshot.rule_id:
            raise SpecLineageReconciliationError(
                "spec_lineage_edge_metadata_inconsistent",
                "A Spec-lineage before-image disagrees with its rule identity.",
            )
        return SpecLineageEdgeSnapshot(
            source_id=snapshot.source_id,
            target_id=snapshot.target_id,
            rule_id=snapshot.rule_id,
            attrs=normalized,
        )

    @staticmethod
    def _ambiguous_spec_lineage_edges(
        edges: tuple[SpecLineageEdgeSnapshot, ...],
    ) -> int:
        return sum(
            1
            for edge in edges
            if str(edge.attrs.get("layer") or "") == "legacy"
            or edge.rule_id in {"", "legacy_pre_v2"}
        )

    def _entity_exists(self, node_id: str) -> bool:
        result = self._query(
            "MATCH (node:Entity) WHERE node.id = $node_id RETURN node.id LIMIT 1",
            {"node_id": node_id},
            operation="read_spec_lineage_endpoint",
        )
        return bool(result.rows)

    def _create_spec_lineage_edge(
        self,
        source_id: str,
        target_id: str,
        attrs: Mapping[str, Any],
        *,
        physical: str,
    ) -> bool:
        bindings = ", ".join(
            f"{name}: $value_{index}" for index, name in enumerate(attrs)
        )
        properties = f" {{{bindings}}}" if bindings else ""
        params = {
            "source_id": source_id,
            "target_id": target_id,
            **{f"value_{index}": value for index, value in enumerate(attrs.values())},
        }
        result = self._query(
            "MATCH (source:Entity), (target:Entity) "
            "WHERE source.id = $source_id AND target.id = $target_id "
            f"CREATE (source)-[:{physical}{properties}]->(target) "
            "RETURN source.id, target.id",
            params,
            operation="create_spec_lineage_edge",
        )
        return bool(result.rows)

    def _read_spec_lineage_edges(
        self,
        source_id: str,
        *,
        physical: str,
        properties: tuple[str, ...],
    ) -> tuple[SpecLineageEdgeSnapshot, ...]:
        projection = ["target.id", *(f"r.{name}" for name in properties)]
        result = self._query(
            f"MATCH (source:Entity)-[r:{physical}]->(target:Entity) "
            "WHERE source.id = $source_id "
            f"RETURN {', '.join(projection)}",
            {"source_id": source_id},
            operation="read_spec_lineage_edges",
        )
        snapshots: list[SpecLineageEdgeSnapshot] = []
        for row in result.rows:
            values = {
                name: _normalize_value(row[index + 1])
                for index, name in enumerate(properties)
            }
            rule_id = str(values.get("rule_id") or "")
            snapshots.append(
                SpecLineageEdgeSnapshot(
                    source_id=source_id,
                    target_id=str(row[0]),
                    rule_id=rule_id,
                    attrs=values,
                )
            )
        return tuple(snapshots)

    def _spec_lineage_edges(
        self,
        source_id: str,
        *,
        physical: str,
        properties: tuple[str, ...],
    ) -> tuple[SpecLineageEdgeSnapshot, ...]:
        edges = self._read_spec_lineage_edges(
            source_id,
            physical=physical,
            properties=properties,
        )
        identities = Counter(
            (edge.source_id, edge.target_id, edge.rule_id)
            for edge in edges
            if is_spec_lineage_rule_id(edge.rule_id)
        )
        if any(count != 1 for count in identities.values()):
            raise SpecLineageReconciliationError(
                "spec_lineage_edge_metadata_inconsistent",
                "The canonical Spec-parent scan exposes more than one edge for "
                "a deterministic lineage identity; graph repair is required.",
            )
        return edges

    @staticmethod
    def _spec_lineage_identity(
        snapshot: SpecLineageEdgeSnapshot,
    ) -> tuple[str, str, str]:
        return snapshot.source_id, snapshot.target_id, snapshot.rule_id

    @staticmethod
    def _spec_lineage_edge_signature(
        snapshot: SpecLineageEdgeSnapshot,
    ) -> tuple[tuple[str, str, str], ...]:
        values = {
            "source_id": snapshot.source_id,
            "target_id": snapshot.target_id,
            **snapshot.attrs,
        }
        return tuple(
            (name, type(value).__name__, repr(_normalize_value(value)))
            for name, value in sorted(values.items())
        )

    def _delete_spec_lineage_edge(
        self,
        snapshot: SpecLineageEdgeSnapshot,
        *,
        physical: str,
        properties: tuple[str, ...],
    ) -> None:
        self._query(
            f"MATCH (source:Entity)-[r:{physical}]->(target:Entity) "
            "WHERE source.id = $source_id AND target.id = $target_id "
            "AND r.rule_id = $rule_id DELETE r",
            {
                "source_id": snapshot.source_id,
                "target_id": snapshot.target_id,
                "rule_id": snapshot.rule_id,
            },
            operation="delete_spec_lineage_edge",
        )
        if any(
            self._spec_lineage_identity(edge) == self._spec_lineage_identity(snapshot)
            for edge in self._spec_lineage_edges(
                snapshot.source_id,
                physical=physical,
                properties=properties,
            )
        ):
            raise SpecLineageReconciliationError(
                "spec_lineage_edge_delete_unconfirmed",
                "The exact Spec-parent relationship remained visible after "
                "DELETE; its replacement was preserved for bounded recovery.",
            )

    def _abort_spec_lineage_failure(
        self,
        primary_error: BaseException,
        *,
        receipt: SpecLineageReconciliationReceipt,
        operation: str,
    ) -> NoReturn:
        cleanup_error = self._abort_after_staged_failure(operation=operation)
        if cleanup_error is None:
            if isinstance(primary_error, SpecLineageReconciliationError):
                if primary_error.receipt is None:
                    primary_error.receipt = receipt
                primary_error.compensation_applied = True
                primary_error.preserve_progress = False
                primary_error.details.update(
                    {
                        "backend": "okto_grafx",
                        "scope_poisoned": True,
                        "transaction_rolled_back": True,
                    }
                )
            raise primary_error
        try:
            primary_error.add_note(
                "Grafx rollback also failed while discarding staged Spec-lineage "
                "effects."
            )
        except BaseException:  # noqa: BLE001, S110 - diagnostic only
            pass
        raise primary_error from cleanup_error

    def reconcile_projection_active_set(
        self,
        intent: ProjectionActiveSetIntent,
    ) -> ProjectionActiveSetReceipt:
        del intent
        self._unsupported("projection_active_set_reconciliation")

    def compensate_projection_active_set(
        self,
        receipt: ProjectionActiveSetReceipt,
    ) -> None:
        del receipt
        self._unsupported("projection_active_set_compensation")

    def find_node_types(self, node_id: str) -> tuple[str, ...]:
        found: list[str] = []
        for node_type in self._node_types:
            result = self._query(
                f"MATCH (n:{node_type}) WHERE n.id = $node_id RETURN n.id LIMIT 1",
                {"node_id": node_id},
                operation="find_node_types",
            )
            if result.rows:
                found.append(node_type)
        return tuple(found)

    def delete_edges_by_session(self, session_id: str) -> None:
        for edge_type, from_type, to_type in self._relationship_pairs:
            physical, definition = self._relationship_definition(
                edge_type,
                from_type,
                to_type,
            )
            if "created_by_session_id" not in self._column_map(definition):
                raise GraphCapabilityUnavailable(
                    f"Grafx relationship table {physical!r} lacks session ownership."
                )
            self._mutation(
                f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type}) "
                "WHERE r.created_by_session_id = $session_id DELETE r",
                {"session_id": session_id},
                operation="delete_edges_by_session",
            )

    def delete_edges_by_session_preserving_spec_lineage(
        self,
        session_id: str,
        preserved_edges: tuple[SpecLineageEdgeSnapshot, ...],
    ) -> None:
        for edge_type, from_type, to_type in self._relationship_pairs:
            physical, definition = self._relationship_definition(
                edge_type,
                from_type,
                to_type,
            )
            columns = self._column_map(definition)
            if "created_by_session_id" not in columns:
                raise GraphCapabilityUnavailable(
                    f"Grafx relationship table {physical!r} lacks session ownership."
                )
            params: dict[str, Any] = {"session_id": session_id}
            preservation = ""
            if (
                edge_type == "belongs_to"
                and from_type == "Entity"
                and to_type == "Entity"
                and preserved_edges
            ):
                if "rule_id" not in columns:
                    raise GraphCapabilityUnavailable(
                        f"Grafx relationship table {physical!r} lacks lineage identity."
                    )
                predicates: list[str] = []
                for index, snapshot in enumerate(preserved_edges):
                    params[f"source_{index}"] = snapshot.source_id
                    params[f"target_{index}"] = snapshot.target_id
                    params[f"rule_{index}"] = snapshot.rule_id
                    predicates.append(
                        f"(a.id = $source_{index} AND b.id = $target_{index} "
                        f"AND r.rule_id = $rule_{index})"
                    )
                preservation = " AND NOT (" + " OR ".join(predicates) + ")"
            self._mutation(
                f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type}) "
                "WHERE r.created_by_session_id = $session_id"
                f"{preservation} DELETE r",
                params,
                operation="delete_edges_by_session_preserving_spec_lineage",
            )

    def delete_nodes_by_session(
        self,
        session_id: str,
        node_types: tuple[str, ...],
    ) -> tuple[str, ...]:
        failed: list[str] = []
        for node_type in node_types:
            try:
                definition = self._node_definition(node_type)
                self._mutation(
                    f"MATCH (n:{definition.name}) "
                    "WHERE n.source_session_id = $session_id DETACH DELETE n",
                    {"session_id": session_id},
                    operation="delete_nodes_by_session",
                )
            except GraphLockContention:
                # Authority loss is not a per-label cleanup failure.  Returning
                # it as a failed type would let callers continue after fencing.
                raise
            except Exception:  # noqa: BLE001 - contract reports failures per requested type
                failed.append(node_type)
        return tuple(failed)

    def increment_attestation(
        self,
        node_type: str,
        node_id: str,
        *,
        attested_at: str,
    ) -> None:
        # The count snapshot is part of the mutating primitive.  Fence before
        # it so a stale owner cannot even base a write on protected state.
        self._fence("increment_attestation")
        before = self.snapshot_node_properties(
            node_type,
            node_id,
            ("attestation_count",),
        )
        if before is None:
            return
        current = before.attrs["attestation_count"]
        next_count = (1 if current is None else int(current)) + 1
        self.update_node(
            node_type,
            node_id,
            {
                "attestation_count": next_count,
                "last_attested_at": attested_at,
            },
        )

    async def commit(self) -> None:
        if self._finished:
            return
        self._fence("commit")
        try:
            self._transaction.commit()
        except Exception as exc:
            self._finished = not self._transaction.active
            mapped = map_grafx_error(exc, operation="commit")
            report = self._transaction.report
            if report is not None and report.durable:
                # Grafx can report a post-barrier apply/publication failure
                # after the wrapper has already recorded a durable commit.
                # Preserve that outcome explicitly and forbid blind retry.
                mapped.details.update(
                    {
                        "commit_durable": True,
                        "write_may_be_applied": bool(report.wrote),
                        "commit_csn": report.csn,
                    }
                )
                mapped.retryable = False
            if mapped is exc:
                raise
            raise mapped from exc
        self._finished = True

    async def rollback(self) -> None:
        if self._finished:
            return
        try:
            self._transaction.rollback()
        except Exception as exc:
            self._finished = not self._transaction.active
            mapped = map_grafx_error(exc, operation="rollback")
            if mapped is exc:
                raise
            raise mapped from exc
        self._finished = True

    async def __aenter__(self) -> Self:
        self._require_active()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if exc and exc[0] is not None:
            await self.rollback()
            return
        try:
            await self.commit()
        except BaseException as commit_error:
            try:
                await self.rollback()
            except BaseException as rollback_error:
                commit_error.add_note(
                    "Grafx rollback also failed while cleaning up a failed commit."
                )
                raise commit_error from rollback_error
            raise


class CommunityGrafxGraphTransaction:
    """Resolve a board database and open one real Grafx write transaction."""

    def __init__(
        self,
        database_resolver: DatabaseResolver,
        revalidate_fence: FenceRevalidator,
        *,
        node_types: tuple[str, ...] = tuple(NODE_TYPES),
        relationship_pairs: tuple[RelationshipPair, ...] | None = None,
        relationship_table_resolver: RelationshipTableResolver = (
            _default_relationship_table
        ),
    ) -> None:
        self._database_resolver = database_resolver
        self._revalidate_fence = revalidate_fence
        self._node_types = tuple(
            dict.fromkeys(_identifier("node type", item) for item in node_types)
        )
        raw_pairs = (
            _default_relationship_pairs()
            if relationship_pairs is None
            else relationship_pairs
        )
        self._relationship_pairs = tuple(
            dict.fromkeys(
                (
                    _identifier("relationship type", edge_type),
                    _identifier("source node type", from_type),
                    _identifier("target node type", to_type),
                )
                for edge_type, from_type, to_type in raw_pairs
            )
        )
        self._relationship_table_resolver = relationship_table_resolver

    async def begin(self, board_id: str) -> _GrafxTransactionScope:
        if type(board_id) is not str or not board_id:
            raise ValueError("board_id must be non-empty text")
        self._revalidate_fence(board_id, "begin")
        try:
            database = self._database_resolver(board_id)
            transaction = database.begin("write")
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="begin")
            if mapped is exc:
                raise
            raise mapped from exc
        return _GrafxTransactionScope(
            board_id,
            database,
            transaction,
            self._revalidate_fence,
            node_types=self._node_types,
            relationship_pairs=self._relationship_pairs,
            relationship_table_resolver=self._relationship_table_resolver,
        )


__all__ = ["CommunityGrafxGraphTransaction"]
