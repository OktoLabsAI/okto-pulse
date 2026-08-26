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
    SpecLineageReconciliationReceipt,
)
from okto_pulse.core.kg.schema_contract import MULTI_REL_TYPES, NODE_TYPES, REL_TYPES

from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_IDENTITY_PROPERTIES = frozenset({"id", "source_session_id"})

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
        try:
            definitions = self._database.catalog.catalog.tables()
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="snapshot_incident_schema")
            raise mapped from exc
        for definition in definitions:
            if definition.kind != "rel" or node_type not in {
                definition.from_table,
                definition.to_table,
            }:
                continue
            physical = _identifier("relationship table", definition.name)
            from_type = _identifier("source node type", definition.from_table)
            to_type = _identifier("target node type", definition.to_table)
            properties = tuple(column.name for column in definition.columns[2:])
            projection = ["a.id", "b.id", *(f"r.{name}" for name in properties)]
            predicates: list[str] = []
            if from_type == node_type:
                predicates.append("a.id = $node_id")
            if to_type == node_type:
                predicates.append("b.id = $node_id")
            result = self._query(
                f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type}) "
                f"WHERE {' OR '.join(predicates)} RETURN {', '.join(projection)}",
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
        del source_id, target_id, attrs
        self._unsupported("spec_lineage_reconciliation")

    def compensate_spec_lineage_parent(
        self,
        receipt: SpecLineageReconciliationReceipt,
    ) -> None:
        del receipt
        self._unsupported("spec_lineage_compensation")

    def clear_spec_lineage_parent(
        self,
        source_id: str,
    ) -> SpecLineageReconciliationReceipt:
        del source_id
        self._unsupported("spec_lineage_clear")

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
