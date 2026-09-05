"""Embedded GraphTransaction over kg.schema.open_board_connection (spec #06).

Adapter-internal (kg/providers/embedded/): wraps a single BoardConnection as a
staged-write scope. The live Kùzu/Ladybug path auto-commits each statement, so
commit() finalizes by closing the connection and rollback() is best-effort (it
closes but cannot undo auto-committed statements — the documented embedded
limitation, identical to the current direct open_board_connection usage).
"""

from __future__ import annotations

from collections import Counter
import logging
import math
import re
from typing import Any

from okto_pulse.community.adapters.graph_error_mapping import (
    map_graph_error,
)
from okto_pulse.community.adapters.ladybug_writer import (
    DEFAULT_WRITER_TIMEOUT_S,
    LadybugWriterLease,
    acquire_ladybug_writer,
    acquire_ladybug_writer_async,
    activate_ladybug_writer_lease,
)
from okto_pulse.core.services.application_kg import (
    revalidate_board_graph_write_lease,
)
from okto_pulse.core.kg.interfaces.graph_transaction import (
    GraphNodePropertyBeforeImage,
    GraphStatementResult,
    ProjectionActiveSetIntent,
    ProjectionActiveSetReceipt,
    ProjectionActiveSetReconciliationError,
    ProjectionEdgeBeforeImage,
    ProjectionNodeBeforeImage,
    SOURCE_PROJECTION_REMOVED_REASON,
    SpecLineageEdgeSnapshot,
    SpecLineageReconciliationError,
    SpecLineageReconciliationReceipt,
    is_spec_lineage_rule_id,
)
from okto_pulse.core.kg.relational_projection import (
    is_relational_projection_node,
    parse_relational_projection_ref,
    relational_projection_rule_node_type,
)
from okto_pulse.core.kg.schema_contract import (
    EDGE_METADATA_COLUMNS,
    MULTI_REL_TYPES,
    REL_TYPES,
    STABLE_NODE_PROPERTIES,
)
from okto_pulse.community.adapters.cypher_statement_policy import (
    POTENTIALLY_MUTATING_TOKENS,
    PROVEN_READ_ONLY_CALLS,
    statement_is_write,
    statement_kind,
)

logger = logging.getLogger(__name__)


_MUTATING_MATCH_KEYWORDS = ("CREATE", "DELETE", "MERGE", "REMOVE", "SET")
_TOMBSTONE_NODE_PROPERTIES = tuple(
    property_name
    for property_name in STABLE_NODE_PROPERTIES
    if property_name not in {"id", "source_session_id"}
) + ("embedding",)
_TOMBSTONE_EDGE_PROPERTIES = (
    "confidence",
    "created_by_session_id",
    "created_at",
    *(name for name, _data_type in EDGE_METADATA_COLUMNS),
)
_TOMBSTONE_RELATIONSHIP_PAIRS = tuple(
    dict.fromkeys(
        (
            *REL_TYPES,
            *(
                (edge_type, from_type, to_type)
                for edge_type, endpoint_pairs in MULTI_REL_TYPES
                for from_type, to_type in endpoint_pairs
            ),
        )
    )
)
_TOMBSTONE_COMPENSATION_ATTEMPTS = 2
# Kept as aliases: the classification moved to a shared module so Grafx
# answers the same question the same way, and these names are still read here.
_PROVEN_READ_ONLY_CALLS = PROVEN_READ_ONLY_CALLS
_POTENTIALLY_MUTATING_TOKENS = POTENTIALLY_MUTATING_TOKENS


_IncidentEdgeBeforeImage = ProjectionEdgeBeforeImage
_NodeBeforeImage = ProjectionNodeBeforeImage


_SESSION_ID_MUST_BE_A_STRING = "source_session_id must be a string"
_RESERVED_ATTRS_REFUSED = "replacement attrs must exclude id and source_session_id"
_UNKNOWN_ATTRS_REFUSED = "replacement attrs contain unknown node properties"


class NodePayloadReplacementCompensationError(RuntimeError):
    """A verified node swap failed and its before-image could not be restored."""


class TombstoneReplacementCompensationError(NodePayloadReplacementCompensationError):
    """The destructive swap failed and its before-image could not be restored.

    Kept as its own name, and now a subclass, so every existing handler catches exactly what it
    caught before while a caller that only cares that a swap could not be compensated may catch
    the general one.
    """


# The policy is shared; these names stay so every existing caller and test
# keeps importing them from here.
_statement_kind = statement_kind
_statement_is_write = statement_is_write


def _materialize(result: Any) -> GraphStatementResult:
    if result is None:
        return GraphStatementResult()
    columns: tuple[str, ...] = ()
    rows: list[list[Any]] = []
    try:
        get_columns = getattr(result, "get_column_names", None)
        if callable(get_columns):
            columns = tuple(str(item) for item in get_columns())
        has_next = getattr(result, "has_next", None)
        get_next = getattr(result, "get_next", None)
        if callable(has_next) and callable(get_next):
            while has_next():
                rows.append(list(get_next()))
        elif isinstance(result, (list, tuple)):
            rows.extend(
                list(row) if isinstance(row, (list, tuple)) else [row] for row in result
            )
    finally:
        close = getattr(result, "close", None)
        if callable(close):
            close()
    return GraphStatementResult.from_rows(rows, columns=columns)


class _KuzuTransactionScope:
    def __init__(
        self,
        board_id: str,
        *,
        writer_lease: LadybugWriterLease | None = None,
    ) -> None:
        self._board_id = board_id
        if writer_lease is None:
            writer_lease = acquire_ladybug_writer(
                scope=board_id,
                phase="graph_transaction",
            )
        self._writer_lease = writer_lease
        self._finished = False
        self._close_failure: BaseException | None = None
        try:
            from okto_pulse.community.adapters.kg_runtime import open_board_connection

            with activate_ladybug_writer_lease(writer_lease):
                self._connection = open_board_connection(board_id)
            self._db = self._connection.db
            self._conn = self._connection.conn
        except BaseException as exc:
            writer_lease.release()
            logger.warning(
                "kg.graph_transaction.open_failed board=%s phase=open "
                "statement_kind=none error_type=%s",
                board_id,
                type(exc).__name__,
                extra={
                    "event": "kg.graph_transaction.open_failed",
                    "board_id": board_id,
                    "phase": "open",
                    "statement_kind": "none",
                    "error_type": type(exc).__name__,
                },
            )
            raise

    @staticmethod
    def relationship_table_name(
        logical_type: str,
        _from_type: str,
        _to_type: str,
    ) -> str:
        """Ladybug stores every endpoint pair under the logical rel name."""

        return logical_type

    def execute(
        self,
        cypher: str,
        params: dict[str, Any] | None = None,
    ) -> GraphStatementResult:
        if self._finished:
            raise RuntimeError("graph_transaction_scope_finished")
        if _statement_is_write(cypher):
            revalidate_board_graph_write_lease(
                self._board_id,
                failure_phase="graph_statement_precommit",
            )
        try:
            if params:
                return _materialize(self._conn.execute(cypher, params))
            return _materialize(self._conn.execute(cypher))
        except Exception as exc:
            mapped = map_graph_error(exc, operation="graph_statement")
            kind = _statement_kind(cypher)
            mapped.details.setdefault("phase", "execute")
            mapped.details.setdefault("statement_kind", kind)
            mapped.details.setdefault("error_code", mapped.code)
            mapped.details.setdefault("retryable", mapped.retryable)
            logger.warning(
                "kg.graph_transaction.statement_failed board=%s "
                "phase=execute statement_kind=%s error_code=%s",
                self._board_id,
                kind,
                mapped.code,
                extra={
                    "event": "kg.graph_transaction.statement_failed",
                    "board_id": self._board_id,
                    "phase": "execute",
                    "statement_kind": kind,
                    "error_code": mapped.code,
                },
            )
            raise mapped from exc

    @staticmethod
    def _property_binding(name: str, value: Any) -> str:
        if (
            name in {"created_at", "last_attested_at", "superseded_at"}
            and isinstance(value, str)
            and value
        ):
            return f"{name}: timestamp(${name})"
        return f"{name}: ${name}"

    def create_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
        *,
        source_session_id: str | None,
    ) -> None:
        params = dict(attrs)
        params["id"] = node_id
        if source_session_id is not None:
            params["source_session_id"] = source_session_id
        columns = ", ".join(
            self._property_binding(key, value) for key, value in params.items()
        )
        self.execute(f"CREATE (n:{node_type} {{{columns}}})", params)

    def update_node(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
    ) -> None:
        values = {key: value for key, value in attrs.items() if key != "id"}
        if not values:
            return
        assignments = ", ".join(f"n.{key} = ${key}" for key in values)
        self.execute(
            f"MATCH (n:{node_type} {{id: $id}}) SET {assignments}",
            {"id": node_id, **values},
        )

    def snapshot_node_properties(
        self,
        node_type: str,
        node_id: str,
        property_names: tuple[str, ...],
    ) -> GraphNodePropertyBeforeImage | None:
        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(name))
            for name in property_names
        ):
            raise ValueError("invalid graph property name")
        projection = ", ".join(f"n.{name}" for name in property_names)
        current = self.execute(
            f"MATCH (n:{node_type} {{id: $node_id}}) RETURN {projection} LIMIT 1",
            {"node_id": node_id},
        )
        if not current.rows:
            return None
        row = current.rows[0]
        return GraphNodePropertyBeforeImage(
            node_type=node_type,
            node_id=node_id,
            attrs={name: row[index] for index, name in enumerate(property_names)},
        )

    def restore_node_properties(
        self,
        before_image: GraphNodePropertyBeforeImage,
    ) -> None:
        if not before_image.attrs:
            return
        assignments = ", ".join(f"n.{name} = ${name}" for name in before_image.attrs)
        restored = self.execute(
            f"MATCH (n:{before_image.node_type} {{id: $node_id}}) "
            f"SET {assignments} RETURN n.id",
            {"node_id": before_image.node_id, **before_image.attrs},
        )
        if not restored.rows:
            raise LookupError("graph node missing during property before-image restore")

    @staticmethod
    def _canonical_snapshot_value(value: Any) -> Any:
        to_list = getattr(value, "tolist", None)
        if callable(to_list):
            value = to_list()
        if isinstance(value, dict):
            return tuple(
                sorted(
                    (
                        str(key),
                        _KuzuTransactionScope._canonical_snapshot_value(item),
                    )
                    for key, item in value.items()
                )
            )
        if isinstance(value, (list, tuple)):
            return tuple(
                _KuzuTransactionScope._canonical_snapshot_value(item) for item in value
            )
        try:
            hash(value)
        except TypeError:
            return repr(value)
        return value

    @classmethod
    def _node_state_signature(cls, snapshot: _NodeBeforeImage) -> tuple[Any, ...]:
        return (
            snapshot.node_type,
            snapshot.node_id,
            cls._canonical_snapshot_value(snapshot.source_session_id),
            *(
                cls._canonical_snapshot_value(snapshot.attrs[property_name])
                for property_name in _TOMBSTONE_NODE_PROPERTIES
            ),
        )

    @classmethod
    def _edge_state_signature(
        cls,
        snapshot: _IncidentEdgeBeforeImage,
    ) -> tuple[Any, ...]:
        return (
            snapshot.edge_type,
            snapshot.from_type,
            snapshot.to_type,
            snapshot.from_id,
            snapshot.to_id,
            *(
                cls._canonical_snapshot_value(snapshot.attrs[property_name])
                for property_name in _TOMBSTONE_EDGE_PROPERTIES
            ),
        )

    def _snapshot_incident_edges(
        self,
        node_type: str,
        node_id: str,
    ) -> tuple[_IncidentEdgeBeforeImage, ...]:
        snapshots: list[_IncidentEdgeBeforeImage] = []
        edge_return = ", ".join(
            f"r.{property_name}" for property_name in _TOMBSTONE_EDGE_PROPERTIES
        )
        for edge_type, from_type, to_type in _TOMBSTONE_RELATIONSHIP_PAIRS:
            if from_type == node_type:
                outgoing = self.execute(
                    f"MATCH (n:{node_type} {{id: $node_id}})"
                    f"-[r:{edge_type}]->(other:{to_type}) "
                    f"RETURN other.id, {edge_return}",
                    {"node_id": node_id},
                )
                snapshots.extend(
                    _IncidentEdgeBeforeImage(
                        edge_type=edge_type,
                        from_type=from_type,
                        to_type=to_type,
                        from_id=node_id,
                        to_id=str(row[0]),
                        attrs={
                            property_name: row[index + 1]
                            for index, property_name in enumerate(
                                _TOMBSTONE_EDGE_PROPERTIES
                            )
                        },
                    )
                    for row in outgoing.rows
                )

            if to_type != node_type:
                continue
            exclude_self_loop = (
                "WHERE other.id <> $node_id " if from_type == node_type else ""
            )
            incoming = self.execute(
                f"MATCH (other:{from_type})-[r:{edge_type}]->"
                f"(n:{node_type} {{id: $node_id}}) "
                f"{exclude_self_loop}RETURN other.id, {edge_return}",
                {"node_id": node_id},
            )
            snapshots.extend(
                _IncidentEdgeBeforeImage(
                    edge_type=edge_type,
                    from_type=from_type,
                    to_type=to_type,
                    from_id=str(row[0]),
                    to_id=node_id,
                    attrs={
                        property_name: row[index + 1]
                        for index, property_name in enumerate(
                            _TOMBSTONE_EDGE_PROPERTIES
                        )
                    },
                )
                for row in incoming.rows
            )
        return tuple(snapshots)

    def _snapshot_node_before_image(
        self,
        node_type: str,
        node_id: str,
        *,
        include_incident_edges: bool,
    ) -> _NodeBeforeImage | None:
        node_return = ", ".join(
            (
                "n.source_session_id",
                *(f"n.{property_name}" for property_name in _TOMBSTONE_NODE_PROPERTIES),
            )
        )
        current = self.execute(
            f"MATCH (n:{node_type} {{id: $node_id}}) RETURN {node_return} LIMIT 1",
            {"node_id": node_id},
        )
        if not current.rows:
            return None
        row = current.rows[0]
        return _NodeBeforeImage(
            node_type=node_type,
            node_id=node_id,
            source_session_id=row[0],
            attrs={
                property_name: row[index + 1]
                for index, property_name in enumerate(_TOMBSTONE_NODE_PROPERTIES)
            },
            incident_edges=(
                self._snapshot_incident_edges(node_type, node_id)
                if include_incident_edges
                else ()
            ),
        )

    def _restore_incident_edges(
        self,
        before_image: _NodeBeforeImage,
    ) -> None:
        desired = Counter(
            self._edge_state_signature(edge) for edge in before_image.incident_edges
        )
        last_error: BaseException | None = None
        for _attempt in range(_TOMBSTONE_COMPENSATION_ATTEMPTS):
            current = Counter(
                self._edge_state_signature(edge)
                for edge in self._snapshot_incident_edges(
                    before_image.node_type,
                    before_image.node_id,
                )
            )
            if current == desired:
                return

            available = current.copy()
            missing: list[_IncidentEdgeBeforeImage] = []
            for edge in before_image.incident_edges:
                signature = self._edge_state_signature(edge)
                if available[signature] > 0:
                    available[signature] -= 1
                else:
                    missing.append(edge)
            for edge in missing:
                try:
                    self.create_edge(
                        edge.edge_type,
                        edge.from_type,
                        edge.to_type,
                        edge.from_id,
                        edge.to_id,
                        {
                            key: value
                            for key, value in edge.attrs.items()
                            if value is not None
                        },
                    )
                except BaseException as exc:
                    last_error = exc

        restored = Counter(
            self._edge_state_signature(edge)
            for edge in self._snapshot_incident_edges(
                before_image.node_type,
                before_image.node_id,
            )
        )
        if restored != desired:
            error = RuntimeError("source_deleted_tombstone_edge_restore_incomplete")
            if last_error is not None:
                raise error from last_error
            raise error

    def _restore_node_before_image(
        self,
        before_image: _NodeBeforeImage,
    ) -> None:
        current = self._snapshot_node_before_image(
            before_image.node_type,
            before_image.node_id,
            include_incident_edges=False,
        )
        original_is_present = current is not None and self._node_state_signature(
            current
        ) == self._node_state_signature(before_image)
        if not original_is_present:
            if current is not None:
                try:
                    self.execute(
                        f"MATCH (n:{before_image.node_type} "
                        "{id: $node_id}) DETACH DELETE n",
                        {"node_id": before_image.node_id},
                    )
                except BaseException:
                    current = self._snapshot_node_before_image(
                        before_image.node_type,
                        before_image.node_id,
                        include_incident_edges=False,
                    )
                    if current is not None:
                        raise

            restore_attrs = {
                key: value
                for key, value in before_image.attrs.items()
                if value is not None
            }
            try:
                self.create_node(
                    before_image.node_type,
                    before_image.node_id,
                    restore_attrs,
                    source_session_id=before_image.source_session_id,
                )
            except BaseException:
                current = self._snapshot_node_before_image(
                    before_image.node_type,
                    before_image.node_id,
                    include_incident_edges=False,
                )
                if current is None or self._node_state_signature(
                    current
                ) != self._node_state_signature(before_image):
                    raise

        restored_node = self._snapshot_node_before_image(
            before_image.node_type,
            before_image.node_id,
            include_incident_edges=False,
        )
        if restored_node is None or self._node_state_signature(
            restored_node
        ) != self._node_state_signature(before_image):
            raise RuntimeError("source_deleted_tombstone_node_restore_incomplete")
        self._restore_incident_edges(before_image)

    def replace_node_payload(
        self,
        node_type: str,
        node_id: str,
        attrs: dict[str, Any],
        *,
        source_session_id: str,
    ) -> bool:
        """Atomically replace a complete node payload, preserving identity and every edge.

        The alternative this exists to prevent is a read-modify-write: between the read and the
        write the node is half replaced, and anything observing it there sees a node that never
        existed. Kuzu cannot SET an indexed vector property to null and the primary key forbids
        a create-first replacement, so the supported operation is the same native swap the
        tombstone path already uses -- with the incident edges restored rather than dropped.

        ``attrs`` is the complete replacement payload. ``id`` and ``source_session_id`` are the
        operation's to set, and any key that is not a declared node property is refused, which is
        what keeps a structural name like ``_type`` out of a payload: in Kuzu the label IS the
        type, so it is not a property a payload may carry at all.
        """

        if type(source_session_id) is not str:
            raise TypeError(_SESSION_ID_MUST_BE_A_STRING)
        reserved = {"id", "source_session_id"}.intersection(attrs)
        if reserved:
            # Built first and raised second: the diagnostic is the point -- a caller needs to be
            # told WHICH key it smuggled -- and a long literal inside the raise is what the lint
            # objects to, not the information it carries.
            message = f"{_RESERVED_ATTRS_REFUSED}; got {sorted(reserved)}"
            raise ValueError(message)
        unknown = set(attrs).difference(_TOMBSTONE_NODE_PROPERTIES)
        if unknown:
            message = f"{_UNKNOWN_ATTRS_REFUSED}: {sorted(unknown)}"
            raise ValueError(message)
        before_image = self._snapshot_node_before_image(
            node_type,
            node_id,
            include_incident_edges=True,
        )
        if before_image is None:
            return False
        return self._replace_node_payload_from_before_image(
            before_image,
            dict(attrs),
            source_session_id=source_session_id,
            incident_edges=before_image.incident_edges,
            error_prefix="node_payload",
            event_prefix="kg.node_payload_replacement",
            cleanup_phase="node_payload_transaction_cleanup_unconfirmed",
            compensation_error_type=NodePayloadReplacementCompensationError,
        )

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
        """Replace an indexed semantic node with a payload-free tombstone.

        Ladybug/Kuzu does not allow an indexed vector property to be SET to
        null, and the node primary key prevents a create-first replacement.
        The supported operation is therefore an explicit native transaction:
        capture the complete node and every incident edge, atomically swap and
        verify, then commit only while the board lease is still valid.  A lost
        lease rolls the uncommitted delete back without bypassing fencing.  The
        complete before-image remains a fallback for a post-commit driver
        failure while the same lease is held.

        The swap itself is shared with :meth:`replace_node_payload`; what differs is that a
        tombstone expects NO incident edges afterwards, and says so by asking for none.
        """

        before_image = self._snapshot_node_before_image(
            node_type,
            node_id,
            include_incident_edges=True,
        )
        if before_image is None:
            return False
        source_ref = str(before_image.attrs["source_artifact_ref"] or "")
        source_session_id = str(
            before_image.source_session_id or "source-deletion-tombstone"
        )
        attrs: dict[str, Any] = {
            "title": "",
            "content": "",
            "context": "",
            "justification": "",
            "source_artifact_ref": source_ref,
            "graph_layer": graph_layer,
            "maturity_status": maturity_status,
            "created_by_agent": str(
                before_image.attrs["created_by_agent"] or "system:source-deletion"
            ),
            "source_confidence": 0.0,
            "relevance_score": relevance_score,
            "query_hits": 0,
            "priority_boost": 0.0,
            "revocation_reason": revocation_reason,
            "human_curated": False,
            "generation": int(before_image.attrs["generation"] or 0),
            "source_span_quote": "",
        }
        if before_image.attrs["created_at"] is not None:
            attrs["created_at"] = before_image.attrs["created_at"]
        return self._replace_node_payload_from_before_image(
            before_image,
            attrs,
            source_session_id=source_session_id,
            incident_edges=(),
            error_prefix="source_deleted_tombstone",
            event_prefix="kg.source_deleted_tombstone",
            cleanup_phase="tombstone_transaction_cleanup_unconfirmed",
            compensation_error_type=TombstoneReplacementCompensationError,
        )

    def _replace_node_payload_from_before_image(
        self,
        before_image: _NodeBeforeImage,
        attrs: dict[str, Any],
        *,
        source_session_id: str,
        incident_edges: tuple[_IncidentEdgeBeforeImage, ...],
        error_prefix: str,
        event_prefix: str,
        cleanup_phase: str,
        compensation_error_type: type[NodePayloadReplacementCompensationError],
    ) -> bool:
        """Perform one verified native node swap under the active write fence.

        ``incident_edges`` is what the caller expects to hold AFTERWARDS, which is the only
        difference between replacing a payload and tombstoning a node: the first asks for its
        edges back and the second asks for none. Everything else -- the atomic unit, the
        verification, the rollback and the compensation -- is one routine, because two copies of
        a fail-atomic swap are two chances to drift on the path nobody exercises.
        """

        node_type = before_image.node_type
        node_id = before_image.node_id
        expected_attrs = {
            property_name: None for property_name in _TOMBSTONE_NODE_PROPERTIES
        }
        expected_attrs.update(attrs)
        expected = _NodeBeforeImage(
            node_type=node_type,
            node_id=node_id,
            source_session_id=source_session_id,
            attrs=expected_attrs,
            incident_edges=incident_edges,
        )
        expected_edges = Counter(
            self._edge_state_signature(edge) for edge in incident_edges
        )
        if (
            self._node_state_signature(before_image)
            == self._node_state_signature(expected)
            and Counter(
                self._edge_state_signature(edge) for edge in before_image.incident_edges
            )
            == expected_edges
        ):
            return True

        transaction_open = False
        try:
            # The indexed primary-key replacement must be one native atomic
            # unit.  In particular, a Core lease can expire after DELETE and
            # before CREATE; the next statement must fail its fence check, but
            # that must not expose a committed missing-node interval.
            # Set the flag first: the driver can accept BEGIN and then fail
            # while materializing/closing its result.
            transaction_open = True
            self.execute("BEGIN TRANSACTION")
            self.execute(
                f"MATCH (n:{node_type} {{id: $node_id}}) DETACH DELETE n",
                {"node_id": node_id},
            )
            self.create_node(
                node_type,
                node_id,
                attrs,
                source_session_id=source_session_id,
            )
            if incident_edges:
                # DETACH DELETE took them with the node, so preserving them means putting them
                # back INSIDE the same native unit -- a caller must never observe the node
                # without the edges that belong to it.
                self._restore_incident_edges(expected)
            created = self._snapshot_node_before_image(
                node_type,
                node_id,
                include_incident_edges=True,
            )
            if (
                created is None
                or self._node_state_signature(created)
                != self._node_state_signature(expected)
                or Counter(
                    self._edge_state_signature(edge) for edge in created.incident_edges
                )
                != expected_edges
            ):
                raise RuntimeError(f"{error_prefix}_replacement_unconfirmed")
            # execute() revalidates the active Core lease immediately before
            # the native commit.  A lost lease therefore reaches the exception
            # path while the destructive swap is still rollback-safe.
            self.execute("COMMIT")
            transaction_open = False
        except BaseException as replacement_error:
            rollback_error: BaseException | None = None
            if transaction_open:
                rollback_confirmed = False
                # ROLLBACK is cleanup, not a stale-writer mutation.  It only
                # discards this connection's uncommitted changes, so it must
                # remain available after the lease check that rejected
                # CREATE/COMMIT. Retry once because a driver/result wrapper can
                # raise either immediately before or immediately after native
                # execution.
                for _attempt in range(2):
                    try:
                        _materialize(self._conn.execute("ROLLBACK"))
                    except BaseException as exc:
                        rollback_error = exc
                        if "NO ACTIVE TRANSACTION" in str(exc).upper():
                            rollback_confirmed = True
                            break
                    else:
                        rollback_confirmed = True
                        break
                if not rollback_confirmed:
                    # Snapshot equality cannot prove that a native transaction
                    # is closed. Poison the scope so the reconciler cannot
                    # continue issuing writes that would later disappear on
                    # connection close.
                    self._close(phase=cleanup_phase)
                    raise compensation_error_type(
                        f"{error_prefix}_transaction_cleanup_unconfirmed"
                    ) from rollback_error
                transaction_open = False
                restored = self._snapshot_node_before_image(
                    node_type,
                    node_id,
                    include_incident_edges=True,
                )
                if (
                    restored is not None
                    and self._node_state_signature(restored)
                    == self._node_state_signature(before_image)
                    and Counter(
                        self._edge_state_signature(edge)
                        for edge in restored.incident_edges
                    )
                    == Counter(
                        self._edge_state_signature(edge)
                        for edge in before_image.incident_edges
                    )
                ):
                    logger.warning(
                        "%s.replacement_failed_rolled_back "
                        "board=%s node_type=%s error_type=%s",
                        event_prefix,
                        self._board_id,
                        node_type,
                        type(replacement_error).__name__,
                        extra={
                            "event": f"{event_prefix}.replacement_failed_rolled_back",
                            "board_id": self._board_id,
                            "node_type": node_type,
                            "error_type": type(replacement_error).__name__,
                        },
                    )
                    raise
                if rollback_error is None:
                    rollback_error = RuntimeError(
                        f"{error_prefix}_rollback_unconfirmed"
                    )
            try:
                self._restore_node_before_image(before_image)
            except BaseException as restore_error:
                logger.error(
                    "%s.compensation_failed "
                    "board=%s node_type=%s replacement_error_type=%s "
                    "restore_error_type=%s",
                    event_prefix,
                    self._board_id,
                    node_type,
                    type(replacement_error).__name__,
                    type(restore_error).__name__,
                    extra={
                        "event": f"{event_prefix}.compensation_failed",
                        "board_id": self._board_id,
                        "node_type": node_type,
                        "replacement_error_type": type(replacement_error).__name__,
                        "restore_error_type": type(restore_error).__name__,
                        "rollback_error_type": (
                            type(rollback_error).__name__
                            if rollback_error is not None
                            else None
                        ),
                    },
                )
                raise compensation_error_type(
                    f"{error_prefix}_compensation_failed"
                ) from restore_error
            logger.warning(
                "%s.replacement_failed_restored board=%s node_type=%s error_type=%s",
                event_prefix,
                self._board_id,
                node_type,
                type(replacement_error).__name__,
                extra={
                    "event": f"{event_prefix}.replacement_failed_restored",
                    "board_id": self._board_id,
                    "node_type": node_type,
                    "error_type": type(replacement_error).__name__,
                },
            )
            raise
        return True

    def mark_superseded(
        self,
        node_type: str,
        node_id: str,
        *,
        superseded_by: str,
        superseded_at: str,
        revocation_reason: str,
    ) -> None:
        self.execute(
            f"MATCH (n:{node_type} {{id: $node_id}}) "
            "SET n.superseded_by = $superseded_by, "
            "n.superseded_at = timestamp($superseded_at), "
            "n.revocation_reason = $revocation_reason",
            {
                "node_id": node_id,
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
        rule_filter = " WHERE r.rule_id = $rule_id" if rule_id is not None else ""
        result = self.execute(
            f"MATCH (a:{from_type} {{id: $from_id}})-[r:{edge_type}]->"
            f"(b:{to_type} {{id: $to_id}}){rule_filter} RETURN r LIMIT 1",
            {
                "from_id": from_id,
                "to_id": to_id,
                **({"rule_id": rule_id} if rule_id is not None else {}),
            },
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
        columns = ", ".join(
            self._property_binding(key, value) for key, value in attrs.items()
        )
        result = self.execute(
            f"MATCH (a:{from_type} {{id: $from_id}}), "
            f"(b:{to_type} {{id: $to_id}}) "
            f"CREATE (a)-[r:{edge_type} {{{columns}}}]->(b) RETURN r",
            {"from_id": from_id, "to_id": to_id, **attrs},
        )
        return bool(result.rows)

    def _spec_lineage_edges(
        self,
        source_id: str,
    ) -> tuple[SpecLineageEdgeSnapshot, ...]:
        source_view = self._read_spec_lineage_edges(source_id)
        self._assert_spec_lineage_source_consistent(source_view)
        return source_view

    def _read_spec_lineage_edges(
        self,
        source_id: str,
    ) -> tuple[SpecLineageEdgeSnapshot, ...]:
        result = self.execute(
            "MATCH (source:Entity {id: $source_id})"
            "-[r:belongs_to]->(target:Entity) "
            "RETURN target.id, r.confidence, r.created_by_session_id, "
            "r.created_at, r.layer, r.rule_id, r.created_by, "
            "r.fallback_reason",
            {"source_id": source_id},
        )
        snapshots: list[SpecLineageEdgeSnapshot] = []
        for row in result.rows:
            rule_id = str(row[5] or "")
            snapshots.append(
                SpecLineageEdgeSnapshot(
                    source_id=source_id,
                    target_id=str(row[0]),
                    rule_id=rule_id,
                    attrs={
                        "confidence": row[1],
                        "created_by_session_id": row[2],
                        "created_at": row[3],
                        "layer": row[4],
                        "rule_id": rule_id,
                        "created_by": row[6],
                        "fallback_reason": row[7],
                    },
                )
            )
        return tuple(snapshots)

    @staticmethod
    def _spec_lineage_edge_signature(
        snapshot: SpecLineageEdgeSnapshot,
    ) -> tuple[tuple[str, str], ...]:
        values = (
            snapshot.source_id,
            snapshot.target_id,
            snapshot.rule_id,
            snapshot.attrs.get("confidence"),
            snapshot.attrs.get("created_by_session_id"),
            snapshot.attrs.get("created_at"),
            snapshot.attrs.get("layer"),
            snapshot.attrs.get("rule_id"),
            snapshot.attrs.get("created_by"),
            snapshot.attrs.get("fallback_reason"),
        )
        return tuple((type(value).__name__, str(value)) for value in values)

    @staticmethod
    def _assert_spec_lineage_source_consistent(
        source_view: tuple[SpecLineageEdgeSnapshot, ...],
    ) -> None:
        """Fail closed on ambiguous identities in one canonical source scan.

        Ladybug 0.16.1 can expose different relationship-property rows for
        source-anchored and source+target-anchored plans on a long-lived
        ``belongs_to`` table. Issuing the second plan as an integrity probe
        both produced false drift and sent the subsequent compensation down a
        native-crash path on Windows. The writer lease already excludes a
        concurrent mutation, so one source-anchored scan is the authoritative
        before-image. A deterministic lineage identity must occur at most
        once; otherwise no DELETE is authorized.
        """

        lineage_identities = Counter(
            (edge.source_id, edge.target_id, edge.rule_id)
            for edge in source_view
            if is_spec_lineage_rule_id(edge.rule_id)
        )
        if any(count != 1 for count in lineage_identities.values()):
            raise SpecLineageReconciliationError(
                "spec_lineage_edge_metadata_inconsistent",
                "The canonical Spec-parent relationship scan exposes more "
                "than one edge for a deterministic lineage identity; graph "
                "rebuild is required before lineage can be mutated.",
            )

    def _delete_spec_lineage_edge(
        self,
        snapshot: SpecLineageEdgeSnapshot,
    ) -> None:
        self.execute(
            "MATCH (source:Entity {id: $source_id})"
            "-[r:belongs_to]->(target:Entity) "
            "WHERE target.id = $target_id AND r.rule_id = $rule_id DELETE r",
            {
                "source_id": snapshot.source_id,
                "target_id": snapshot.target_id,
                "rule_id": snapshot.rule_id,
            },
        )
        canonical_view = self._read_spec_lineage_edges(snapshot.source_id)
        self._assert_spec_lineage_source_consistent(canonical_view)
        source_view = tuple(
            edge for edge in canonical_view if edge.target_id == snapshot.target_id
        )
        deleted_signature = self._spec_lineage_edge_signature(snapshot)
        if any(
            self._spec_lineage_edge_signature(edge) == deleted_signature
            for edge in source_view
        ):
            raise SpecLineageReconciliationError(
                "spec_lineage_edge_delete_unconfirmed",
                "The exact Spec-parent relationship remained visible after "
                "DELETE; its replacement was preserved for bounded recovery.",
            )

    def reconcile_spec_lineage_parent(
        self,
        source_id: str,
        target_id: str,
        attrs: dict[str, Any],
    ) -> SpecLineageReconciliationReceipt:
        """Atomically-safe saga for the exclusive deterministic Spec parent.

        The embedded backend auto-commits each statement.  This method
        therefore confirms/creates the replacement first and only then removes
        old edges in the narrowly identified rule family.  A mid-cleanup
        failure can temporarily leave two parents, but never zero; retrying
        converges by observing the already-created replacement.
        """

        rule_id = str(attrs.get("rule_id") or "")
        if not is_spec_lineage_rule_id(rule_id):
            raise SpecLineageReconciliationError(
                "spec_lineage_rule_out_of_scope",
                f"Rule {rule_id!r} is outside the exclusive Spec-parent family.",
            )

        endpoints = self.execute(
            "MATCH (source:Entity {id: $source_id}), (target:Entity) "
            "WHERE target.id = $target_id "
            "RETURN source.id, target.id LIMIT 1",
            {"source_id": source_id, "target_id": target_id},
        )
        if not endpoints.rows:
            raise SpecLineageReconciliationError(
                "spec_lineage_endpoint_not_found",
                "Both the Spec source and its new parent must exist as Entity "
                "nodes before lineage reconciliation.",
            )

        existing = self._spec_lineage_edges(source_id)
        exact_exists = any(
            edge.target_id == target_id and edge.rule_id == rule_id for edge in existing
        )
        old_edges = tuple(
            edge
            for edge in existing
            if is_spec_lineage_rule_id(edge.rule_id)
            and not (edge.target_id == target_id and edge.rule_id == rule_id)
        )
        ambiguous_legacy_edges = sum(
            1
            for edge in existing
            if str(edge.attrs.get("layer") or "") == "legacy"
            or edge.rule_id in {"", "legacy_pre_v2"}
        )

        new_edge_created = False
        if not exact_exists:
            new_edge_created = self.create_edge(
                "belongs_to",
                "Entity",
                "Entity",
                source_id,
                target_id,
                dict(attrs),
            )
            replacement_exists = any(
                edge.target_id == target_id and edge.rule_id == rule_id
                for edge in self._spec_lineage_edges(source_id)
            )
            if not new_edge_created or not replacement_exists:
                raise SpecLineageReconciliationError(
                    "spec_lineage_new_parent_create_failed",
                    "The new Spec-parent edge could not be confirmed; old "
                    "parents were preserved.",
                )

        receipt = SpecLineageReconciliationReceipt(
            source_id=source_id,
            target_id=target_id,
            target_rule_id=rule_id,
            target_attrs=dict(attrs),
            new_edge_created=new_edge_created,
            removed_edges=old_edges,
            ambiguous_legacy_edges=ambiguous_legacy_edges,
        )
        try:
            for snapshot in old_edges:
                self._delete_spec_lineage_edge(snapshot)

            remaining_old = tuple(
                edge
                for edge in self._spec_lineage_edges(source_id)
                if is_spec_lineage_rule_id(edge.rule_id)
                and not (edge.target_id == target_id and edge.rule_id == rule_id)
            )
            if remaining_old:
                raise SpecLineageReconciliationError(
                    "spec_lineage_old_parent_cleanup_incomplete",
                    "The replacement parent exists, but one or more old Spec "
                    "parents remain; retry reconciliation to converge.",
                    receipt=receipt,
                )
        except SpecLineageReconciliationError as exc:
            if exc.code in {
                "spec_lineage_edge_delete_unconfirmed",
                "spec_lineage_edge_metadata_inconsistent",
                "spec_lineage_old_parent_cleanup_incomplete",
            }:
                raise SpecLineageReconciliationError(
                    exc.code,
                    str(exc),
                    receipt=receipt,
                ) from exc
            try:
                self.compensate_spec_lineage_parent(receipt)
            except Exception as restore_exc:
                raise SpecLineageReconciliationError(
                    "spec_lineage_partial_cleanup_restore_failed",
                    "Old-parent cleanup and restore-first compensation both "
                    "failed; the replacement edge was preserved.",
                    receipt=receipt,
                ) from restore_exc
            raise SpecLineageReconciliationError(
                "spec_lineage_old_parent_cleanup_failed",
                "Old-parent cleanup failed and was restored before the "
                "replacement edge was removed.",
                receipt=receipt,
            ) from exc
        except Exception as exc:
            try:
                self.compensate_spec_lineage_parent(receipt)
            except Exception as restore_exc:
                raise SpecLineageReconciliationError(
                    "spec_lineage_partial_cleanup_restore_failed",
                    "Old-parent cleanup and restore-first compensation both "
                    "failed; the replacement edge was preserved.",
                    receipt=receipt,
                ) from restore_exc
            raise SpecLineageReconciliationError(
                "spec_lineage_old_parent_cleanup_failed",
                "Old-parent cleanup failed and was restored before the "
                "replacement edge was removed.",
                receipt=receipt,
            ) from exc
        return receipt

    def clear_spec_lineage_parent(
        self,
        source_id: str,
    ) -> SpecLineageReconciliationReceipt:
        """Clear only explicit deterministic Spec-parent edges."""

        source = self.execute(
            "MATCH (source:Entity {id: $source_id}) RETURN source.id LIMIT 1",
            {"source_id": source_id},
        )
        if not source.rows:
            raise SpecLineageReconciliationError(
                "spec_lineage_source_not_found",
                "The Spec source must exist as an Entity node before lineage "
                "can be cleared.",
            )

        existing = self._spec_lineage_edges(source_id)
        old_edges = tuple(
            edge for edge in existing if is_spec_lineage_rule_id(edge.rule_id)
        )
        ambiguous_legacy_edges = sum(
            1
            for edge in existing
            if str(edge.attrs.get("layer") or "") == "legacy"
            or edge.rule_id in {"", "legacy_pre_v2"}
        )
        receipt = SpecLineageReconciliationReceipt(
            source_id=source_id,
            target_id=None,
            target_rule_id=None,
            target_attrs={},
            new_edge_created=False,
            removed_edges=old_edges,
            ambiguous_legacy_edges=ambiguous_legacy_edges,
        )
        try:
            for snapshot in old_edges:
                self._delete_spec_lineage_edge(snapshot)
            if any(
                is_spec_lineage_rule_id(edge.rule_id)
                for edge in self._spec_lineage_edges(source_id)
            ):
                raise SpecLineageReconciliationError(
                    "spec_lineage_clear_incomplete",
                    "One or more deterministic Spec parents remain; retry "
                    "the explicit clear to converge.",
                    receipt=receipt,
                )
        except SpecLineageReconciliationError as exc:
            if exc.code in {
                "spec_lineage_edge_delete_unconfirmed",
                "spec_lineage_edge_metadata_inconsistent",
                "spec_lineage_clear_incomplete",
            }:
                raise SpecLineageReconciliationError(
                    exc.code,
                    str(exc),
                    receipt=receipt,
                ) from exc
            try:
                self.compensate_spec_lineage_parent(receipt)
            except Exception as restore_exc:
                raise SpecLineageReconciliationError(
                    "spec_lineage_clear_restore_failed",
                    "Spec-parent clear failed and its before-image could not "
                    "be fully restored.",
                    receipt=receipt,
                ) from restore_exc
            raise SpecLineageReconciliationError(
                "spec_lineage_clear_failed",
                "Spec-parent clear failed and its before-image was restored.",
                receipt=receipt,
            ) from exc
        except Exception as exc:
            try:
                self.compensate_spec_lineage_parent(receipt)
            except Exception as restore_exc:
                raise SpecLineageReconciliationError(
                    "spec_lineage_clear_restore_failed",
                    "Spec-parent clear failed and its before-image could not "
                    "be fully restored.",
                    receipt=receipt,
                ) from restore_exc
            raise SpecLineageReconciliationError(
                "spec_lineage_clear_failed",
                "Spec-parent clear failed and its before-image was restored.",
                receipt=receipt,
            ) from exc
        return receipt

    def compensate_spec_lineage_parent(
        self,
        receipt: SpecLineageReconciliationReceipt,
    ) -> None:
        """Restore the before-image before removing this attempt's new edge."""

        for snapshot in receipt.removed_edges:
            restored = any(
                edge.target_id == snapshot.target_id
                and edge.rule_id == snapshot.rule_id
                for edge in self._spec_lineage_edges(snapshot.source_id)
            )
            if restored:
                continue
            created = self.create_edge(
                "belongs_to",
                "Entity",
                "Entity",
                snapshot.source_id,
                snapshot.target_id,
                dict(snapshot.attrs),
            )
            restored = any(
                edge.target_id == snapshot.target_id
                and edge.rule_id == snapshot.rule_id
                for edge in self._spec_lineage_edges(snapshot.source_id)
            )
            if not created or not restored:
                raise SpecLineageReconciliationError(
                    "spec_lineage_old_parent_restore_failed",
                    "An old Spec parent could not be restored; the replacement "
                    "edge was preserved.",
                )

        if (
            receipt.new_edge_created
            and receipt.target_id is not None
            and receipt.target_rule_id is not None
        ):
            self._delete_spec_lineage_edge(
                SpecLineageEdgeSnapshot(
                    source_id=receipt.source_id,
                    target_id=receipt.target_id,
                    rule_id=receipt.target_rule_id,
                    attrs=dict(receipt.target_attrs),
                )
            )

    def _projection_owned_nodes(
        self,
        intent: ProjectionActiveSetIntent,
    ) -> tuple[tuple[str, str, str, str], ...]:
        """Read two bounded node tables, then apply the exact Core parser."""

        owned: list[tuple[str, str, str, str]] = []
        for node_type in ("Decision", "Alternative"):
            exact_owner_node_ids = {
                str(row[0] or "")
                for row in self.execute(
                    f"MATCH (n:{node_type})-[r:belongs_to]->"
                    "(owner:Entity) "
                    "RETURN n.id, owner.id, owner.source_artifact_ref, "
                    "r.rule_id"
                ).rows
                if (
                    (
                        intent.owner_node_id is None
                        or str(row[1] or "") == intent.owner_node_id
                    )
                    and str(row[2] or "") == f"refinement:{intent.owner_id}"
                    and relational_projection_rule_node_type(str(row[3] or ""))
                    == node_type
                )
            }
            rows = self.execute(
                f"MATCH (n:{node_type}) "
                "RETURN n.id, n.source_artifact_ref, "
                "n.created_by_agent, n.revocation_reason"
            ).rows
            for row in rows:
                node_id = str(row[0] or "")
                source_ref = str(row[1] or "")
                created_by_agent = str(row[2] or "")
                if not is_relational_projection_node(
                    node_type=node_type,
                    source_artifact_ref=source_ref,
                    created_by_agent=created_by_agent,
                    owner_type=intent.owner_type,
                    owner_id=intent.owner_id,
                    namespace=intent.namespace,
                ):
                    continue
                reason = str(row[3] or "")
                if (
                    node_id not in exact_owner_node_ids
                    and reason != SOURCE_PROJECTION_REMOVED_REASON
                ):
                    continue
                owned.append(
                    (
                        node_type,
                        node_id,
                        source_ref,
                        reason,
                    )
                )
        return tuple(owned)

    def _delete_projection_incident_edges(
        self,
        before_image: ProjectionNodeBeforeImage,
    ) -> None:
        seen: set[tuple[str, str, str, str, str]] = set()
        for edge in before_image.incident_edges:
            identity = (
                edge.edge_type,
                edge.from_type,
                edge.to_type,
                edge.from_id,
                edge.to_id,
            )
            if identity in seen:
                continue
            seen.add(identity)
            self.execute(
                f"MATCH (a:{edge.from_type} {{id: $from_id}})"
                f"-[r:{edge.edge_type}]->"
                f"(b:{edge.to_type} {{id: $to_id}}) DELETE r",
                {"from_id": edge.from_id, "to_id": edge.to_id},
            )

    def _projection_edge_multiset(
        self,
        *,
        edge_type: str,
        from_type: str,
        to_type: str,
        from_id: str,
        to_id: str,
        dependency_rule_id: str | None,
    ) -> Counter[Any]:
        return Counter(
            self._edge_state_signature(item)
            for item in self._snapshot_incident_edges(from_type, from_id)
            if item.edge_type == edge_type
            and item.from_type == from_type
            and item.to_type == to_type
            and item.from_id == from_id
            and item.to_id == to_id
            and (
                dependency_rule_id is None
                or str(item.attrs.get("rule_id") or "") == dependency_rule_id
            )
        )

    def _restore_projection_edges(
        self,
        edges: tuple[ProjectionEdgeBeforeImage, ...],
    ) -> None:
        wanted: dict[tuple[str, str, str, str, str, str | None], Counter[Any]] = {}
        templates: dict[
            tuple[str, str, str, str, str, str | None],
            dict[Any, ProjectionEdgeBeforeImage],
        ] = {}
        for edge in edges:
            dependency_rule_id = (
                str(edge.attrs.get("rule_id") or "")
                if edge.edge_type == "precedes"
                and edge.from_type == "Entity"
                and edge.to_type == "Entity"
                and str(edge.attrs.get("rule_id") or "").startswith(
                    "precedes/spec_dependency/"
                )
                else None
            )
            identity = (
                edge.edge_type,
                edge.from_type,
                edge.to_type,
                edge.from_id,
                edge.to_id,
                dependency_rule_id,
            )
            signature = self._edge_state_signature(edge)
            wanted.setdefault(identity, Counter())[signature] += 1
            templates.setdefault(identity, {})[signature] = edge

        for identity, desired in wanted.items():
            edge_type, from_type, to_type, from_id, to_id, dependency_rule_id = identity
            current = self._projection_edge_multiset(
                edge_type=edge_type,
                from_type=from_type,
                to_type=to_type,
                from_id=from_id,
                to_id=to_id,
                dependency_rule_id=dependency_rule_id,
            )
            if current - desired:
                raise RuntimeError("projection_edge_restore_identity_conflict")
            missing = desired - current
            for signature, count in missing.items():
                edge = templates[identity][signature]
                for _copy in range(count):
                    self.create_edge(
                        edge.edge_type,
                        edge.from_type,
                        edge.to_type,
                        edge.from_id,
                        edge.to_id,
                        {
                            key: value
                            for key, value in edge.attrs.items()
                            if value is not None
                        },
                    )
            if (
                self._projection_edge_multiset(
                    edge_type=edge_type,
                    from_type=from_type,
                    to_type=to_type,
                    from_id=from_id,
                    to_id=to_id,
                    dependency_rule_id=dependency_rule_id,
                )
                != desired
            ):
                raise RuntimeError("projection_edge_restore_incomplete")

    def _reconcile_spec_dependency_edges(
        self,
        intent: ProjectionActiveSetIntent,
    ) -> ProjectionActiveSetReceipt:
        if intent.active_nodes or intent.owner_node_id is None:
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_member_invalid",
                "The Spec dependency projection owns edges and requires its root.",
            )
        desired: set[tuple[str, str, str, str, str, str]] = set()
        for edge in intent.active_edges:
            identity = (
                edge.edge_type,
                edge.from_type,
                edge.to_type,
                edge.from_id,
                edge.to_id,
                edge.rule_id,
            )
            if (
                edge.edge_type != "precedes"
                or edge.from_type != "Entity"
                or edge.to_type != "Entity"
                or edge.to_id != intent.owner_node_id
                or not edge.rule_id.startswith("precedes/spec_dependency/")
                or identity in desired
            ):
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_member_invalid",
                    "A Spec dependency edge is outside the exact projection scope.",
                )
            desired.add(identity)

        edge_return = ", ".join(
            f"r.{property_name}" for property_name in _TOMBSTONE_EDGE_PROPERTIES
        )
        rows = self.execute(
            "MATCH (prerequisite:Entity)-[r:precedes]->"
            "(owner:Entity {id: $owner_id}) "
            f"RETURN prerequisite.id, owner.id, {edge_return}",
            {"owner_id": intent.owner_node_id},
        ).rows
        owned: list[
            tuple[
                tuple[str, str, str, str, str, str],
                ProjectionEdgeBeforeImage,
            ]
        ] = []
        for row in rows:
            attrs = {
                property_name: row[index + 2]
                for index, property_name in enumerate(_TOMBSTONE_EDGE_PROPERTIES)
            }
            if not str(attrs.get("rule_id") or "").startswith(
                "precedes/spec_dependency/"
            ):
                continue
            identity = (
                "precedes",
                "Entity",
                "Entity",
                str(row[0]),
                str(row[1]),
                str(attrs.get("rule_id") or ""),
            )
            owned.append(
                (
                    identity,
                    ProjectionEdgeBeforeImage(
                        edge_type="precedes",
                        from_type="Entity",
                        to_type="Entity",
                        from_id=str(row[0]),
                        to_id=str(row[1]),
                        attrs=attrs,
                    ),
                )
            )

        owned_identities = [identity for identity, _edge in owned]
        if len(owned_identities) != len(set(owned_identities)):
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_source_ref_ambiguous",
                "A Spec dependency identity resolves to multiple edges.",
            )
        if desired.difference(owned_identities):
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_member_missing",
                "An active Spec dependency edge is missing or untrusted.",
            )
        stale = tuple(edge for identity, edge in owned if identity not in desired)
        receipt = ProjectionActiveSetReceipt(
            intent=intent,
            edge_before_images=stale,
        )
        if not stale:
            return receipt

        transaction_open = False
        try:
            transaction_open = True
            self.execute("BEGIN TRANSACTION")
            for edge in stale:
                self.execute(
                    "MATCH (prerequisite:Entity {id: $from_id})"
                    "-[r:precedes]->(owner:Entity {id: $to_id}) "
                    "WHERE r.rule_id = $rule_id DELETE r",
                    {
                        "from_id": edge.from_id,
                        "to_id": edge.to_id,
                        "rule_id": str(edge.attrs.get("rule_id") or ""),
                    },
                )
            remaining = self.execute(
                "MATCH (prerequisite:Entity)-[r:precedes]->"
                "(owner:Entity {id: $owner_id}) "
                "RETURN prerequisite.id, r.rule_id",
                {"owner_id": intent.owner_node_id},
            ).rows
            stale_pairs = {
                (edge.from_id, str(edge.attrs.get("rule_id") or "")) for edge in stale
            }
            if any(
                (str(row[0]), str(row[1] or "")) in stale_pairs for row in remaining
            ):
                raise RuntimeError("projection_stale_edge_cleanup_unconfirmed")
            self.execute("COMMIT")
            transaction_open = False
        except BaseException as apply_error:
            if transaction_open:
                try:
                    self.execute("ROLLBACK")
                except BaseException as rollback_error:
                    self._close(phase="projection_edge_cleanup_unconfirmed")
                    raise ProjectionActiveSetReconciliationError(
                        "projection_active_set_transaction_cleanup_unconfirmed",
                        "The native projection transaction could not be proven closed.",
                        receipt=receipt,
                    ) from rollback_error
            try:
                self._restore_projection_edges(stale)
            except BaseException as restore_error:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_apply_and_restore_failed",
                    "Projection edge reconciliation failed and could not be restored.",
                    receipt=receipt,
                ) from restore_error
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_apply_failed",
                "Projection edge reconciliation failed and was restored.",
                receipt=receipt,
            ) from apply_error
        return receipt

    def reconcile_projection_active_set(
        self,
        intent: ProjectionActiveSetIntent,
    ) -> ProjectionActiveSetReceipt:
        """Atomically replace one exact relational active set."""

        if intent.owner_type == "spec" and intent.namespace == "dependencies":
            return self._reconcile_spec_dependency_edges(intent)

        if intent.owner_type != "refinement" or intent.namespace != "rdl":
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_scope_invalid",
                "Only the exact refinement/RDL relational projection is supported.",
            )
        if intent.active_edges:
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_member_invalid",
                "The refinement/RDL projection cannot own operational edges.",
            )

        active_by_ref: dict[str, tuple[str, str]] = {}
        for ref in intent.active_nodes:
            identity = parse_relational_projection_ref(ref.source_artifact_ref)
            if (
                identity is None
                or identity.owner_type != intent.owner_type
                or identity.owner_id != intent.owner_id
                or identity.namespace != intent.namespace
                or identity.node_type != ref.node_type
            ):
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_member_invalid",
                    "An active member is outside the exact projection scope.",
                )
            if ref.source_artifact_ref in active_by_ref:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_member_duplicate",
                    "The active projection contains a duplicate source reference.",
                )
            active_by_ref[ref.source_artifact_ref] = (ref.node_type, ref.node_id)

        owned = self._projection_owned_nodes(intent)
        owned_by_ref: dict[str, tuple[str, str, str]] = {}
        for node_type, node_id, source_ref, reason in owned:
            if source_ref in owned_by_ref:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_source_ref_ambiguous",
                    "A relational source reference resolves to multiple graph nodes.",
                )
            owned_by_ref[source_ref] = (node_type, node_id, reason)
        for source_ref, expected in active_by_ref.items():
            current = owned_by_ref.get(source_ref)
            if current is None:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_member_missing",
                    "An active relational projection member is missing or has "
                    "untrusted provenance.",
                )
            if current[:2] != expected:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_identity_conflict",
                    "An active relational source reference resolves to a "
                    "different graph identity.",
                )

        active_refs = frozenset(active_by_ref)
        before_images: list[ProjectionNodeBeforeImage] = []
        for node_type, node_id, source_ref, reason in owned:
            is_active = source_ref in active_refs
            if is_active:
                needs_change = reason == SOURCE_PROJECTION_REMOVED_REASON
            elif reason not in {"", SOURCE_PROJECTION_REMOVED_REASON}:
                # Never overwrite deletion/cancellation/supersedence provenance.
                needs_change = False
            else:
                snapshot_without_edges = self._snapshot_node_before_image(
                    node_type,
                    node_id,
                    include_incident_edges=False,
                )
                if snapshot_without_edges is None:
                    raise ProjectionActiveSetReconciliationError(
                        "projection_active_set_snapshot_failed",
                        "A projection member disappeared while its before-image "
                        "was being captured.",
                    )
                needs_change = reason != SOURCE_PROJECTION_REMOVED_REASON or bool(
                    self._snapshot_incident_edges(node_type, node_id)
                )
            if not needs_change:
                continue
            snapshot = self._snapshot_node_before_image(
                node_type,
                node_id,
                include_incident_edges=True,
            )
            if snapshot is None:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_snapshot_failed",
                    "A projection member disappeared while its complete "
                    "before-image was being captured.",
                )
            before_images.append(snapshot)

        receipt = ProjectionActiveSetReceipt(
            intent=intent,
            before_images=tuple(before_images),
        )
        if not before_images:
            return receipt

        transaction_open = False
        try:
            transaction_open = True
            self.execute("BEGIN TRANSACTION")
            for before_image in before_images:
                source_ref = str(before_image.attrs.get("source_artifact_ref") or "")
                if source_ref in active_refs:
                    restored = self.execute(
                        f"MATCH (n:{before_image.node_type} "
                        "{id: $node_id}) "
                        "WHERE n.revocation_reason = $reason "
                        "SET n.revocation_reason = '' RETURN n.id",
                        {
                            "node_id": before_image.node_id,
                            "reason": SOURCE_PROJECTION_REMOVED_REASON,
                        },
                    )
                    if not restored.rows:
                        raise RuntimeError(
                            "projection_active_member_restore_unconfirmed"
                        )
                    continue
                self._delete_projection_incident_edges(before_image)
                removed = self.execute(
                    f"MATCH (n:{before_image.node_type} "
                    "{id: $node_id}) "
                    "WHERE coalesce(n.revocation_reason, '') IN "
                    "['', $reason] "
                    "SET n.revocation_reason = $reason RETURN n.id",
                    {
                        "node_id": before_image.node_id,
                        "reason": SOURCE_PROJECTION_REMOVED_REASON,
                    },
                )
                if not removed.rows:
                    raise RuntimeError("projection_stale_member_tombstone_unconfirmed")
                current = self._snapshot_node_before_image(
                    before_image.node_type,
                    before_image.node_id,
                    include_incident_edges=True,
                )
                if (
                    current is None
                    or str(current.attrs.get("revocation_reason") or "")
                    != SOURCE_PROJECTION_REMOVED_REASON
                    or current.incident_edges
                ):
                    raise RuntimeError("projection_stale_member_cleanup_unconfirmed")
            self.execute("COMMIT")
            transaction_open = False
        except BaseException as apply_error:
            rollback_error: BaseException | None = None
            if transaction_open:
                rollback_confirmed = False
                for _attempt in range(2):
                    try:
                        _materialize(self._conn.execute("ROLLBACK"))
                    except BaseException as exc:
                        rollback_error = exc
                        if "NO ACTIVE TRANSACTION" in str(exc).upper():
                            rollback_confirmed = True
                            break
                    else:
                        rollback_confirmed = True
                        break
                if not rollback_confirmed:
                    self._close(phase="projection_active_set_cleanup_unconfirmed")
                    raise ProjectionActiveSetReconciliationError(
                        "projection_active_set_transaction_cleanup_unconfirmed",
                        "The native projection transaction could not be proven "
                        "closed; the writer scope was poisoned.",
                        receipt=receipt,
                    ) from rollback_error
                transaction_open = False
            try:
                self.compensate_projection_active_set(receipt)
            except BaseException as restore_error:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_apply_and_restore_failed",
                    "Projection reconciliation failed and its complete "
                    "before-image could not be restored.",
                    receipt=receipt,
                ) from restore_error
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_apply_failed",
                "Projection reconciliation failed and was restored.",
                receipt=receipt,
            ) from apply_error
        return receipt

    def compensate_projection_active_set(
        self,
        receipt: ProjectionActiveSetReceipt,
    ) -> None:
        """Restore every projection node and all incident edges exactly."""

        for before_image in receipt.before_images:
            self._restore_node_before_image(before_image)
        self._restore_projection_edges(receipt.edge_before_images)

    def find_node_types(self, node_id: str) -> tuple[str, ...]:
        from okto_pulse.community.adapters.kg_runtime import NODE_TYPES

        found: list[str] = []
        for node_type in NODE_TYPES:
            result = self.execute(
                f"MATCH (n:{node_type} {{id: $node_id}}) RETURN n.id LIMIT 1",
                {"node_id": node_id},
            )
            if result.rows:
                found.append(node_type)
        return tuple(found)

    def delete_edges_by_session(self, session_id: str) -> None:
        from okto_pulse.community.adapters.kg_runtime import (
            MULTI_REL_TYPES,
            REL_TYPES,
        )

        pairs = list(REL_TYPES)
        for rel_name, endpoint_pairs in MULTI_REL_TYPES:
            pairs.extend(
                (rel_name, from_type, to_type) for from_type, to_type in endpoint_pairs
            )
        for rel_name, from_type, to_type in pairs:
            self.execute(
                f"MATCH (a:{from_type})-[r:{rel_name}]->(b:{to_type}) "
                "WHERE r.created_by_session_id = $session_id DELETE r",
                {"session_id": session_id},
            )

    def delete_edges_by_session_preserving_spec_lineage(
        self,
        session_id: str,
        preserved_edges: tuple[SpecLineageEdgeSnapshot, ...],
        *,
        preserved_projection_edges: tuple[ProjectionEdgeBeforeImage, ...] = (),
    ) -> None:
        """Compensate a session without erasing restored before-images."""

        from okto_pulse.community.adapters.kg_runtime import (
            MULTI_REL_TYPES,
            REL_TYPES,
        )

        pairs = list(REL_TYPES)
        for rel_name, endpoint_pairs in MULTI_REL_TYPES:
            pairs.extend(
                (rel_name, from_type, to_type) for from_type, to_type in endpoint_pairs
            )

        projection_by_pair: dict[
            tuple[str, str, str], list[ProjectionEdgeBeforeImage]
        ] = {}
        projection_by_identity: dict[
            tuple[str, str, str, str, str, str | None],
            list[ProjectionEdgeBeforeImage],
        ] = {}
        configured_pairs = set(pairs)
        for edge in preserved_projection_edges:
            pair = (edge.edge_type, edge.from_type, edge.to_type)
            if pair not in configured_pairs:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_cleanup_preservation_inconsistent",
                    "A restored projection edge has no configured relationship "
                    "mapping for session cleanup.",
                )
            if any(
                isinstance(value, float) and not math.isfinite(value)
                for value in edge.attrs.values()
            ):
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_cleanup_preservation_inconsistent",
                    "A restored projection edge contains a non-finite numeric "
                    "value that cannot be matched safely during session cleanup.",
                )
            if set(edge.attrs) != set(_TOMBSTONE_EDGE_PROPERTIES):
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_cleanup_preservation_inconsistent",
                    "A restored projection edge is not a complete relationship "
                    "before-image.",
                )
            dependency_rule_id = (
                str(edge.attrs.get("rule_id") or "")
                if edge.edge_type == "precedes"
                and edge.from_type == "Entity"
                and edge.to_type == "Entity"
                and str(edge.attrs.get("rule_id") or "").startswith(
                    "precedes/spec_dependency/"
                )
                else None
            )
            projection_by_pair.setdefault(pair, []).append(edge)
            projection_by_identity.setdefault(
                (
                    edge.edge_type,
                    edge.from_type,
                    edge.to_type,
                    edge.from_id,
                    edge.to_id,
                    dependency_rule_id,
                ),
                [],
            ).append(edge)

        # Fail before the first DELETE if an exact predicate would preserve an
        # unowned payload or multiplicity sharing a receipt identity.
        for identity, identity_edges in projection_by_identity.items():
            edge_type, from_type, to_type, from_id, to_id, dependency_rule_id = identity
            desired = Counter(
                self._edge_state_signature(edge) for edge in identity_edges
            )
            actual = Counter(
                self._edge_state_signature(edge)
                for edge in self._snapshot_incident_edges(from_type, from_id)
                if edge.edge_type == edge_type
                and edge.from_type == from_type
                and edge.to_type == to_type
                and edge.from_id == from_id
                and edge.to_id == to_id
                and (
                    dependency_rule_id is None
                    or str(edge.attrs.get("rule_id") or "") == dependency_rule_id
                )
            )
            if actual - desired:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_cleanup_preservation_inconsistent",
                    "The restored projection edge multiset exceeds the exact "
                    "before-images owned by the compensation receipts.",
                )

        for rel_name, from_type, to_type in pairs:
            params: dict[str, Any] = {"session_id": session_id}
            predicates: list[str] = []
            if (
                rel_name == "belongs_to"
                and from_type == "Entity"
                and to_type == "Entity"
            ):
                for index, snapshot in enumerate(preserved_edges):
                    params[f"preserved_source_{index}"] = snapshot.source_id
                    params[f"preserved_target_{index}"] = snapshot.target_id
                    params[f"preserved_rule_{index}"] = snapshot.rule_id
                    predicates.append(
                        "(a.id = $preserved_source_"
                        f"{index} AND b.id = $preserved_target_{index} "
                        f"AND r.rule_id = $preserved_rule_{index})"
                    )

            for index, edge in enumerate(
                projection_by_pair.get((rel_name, from_type, to_type), ())
            ):
                params[f"projection_source_{index}"] = edge.from_id
                params[f"projection_target_{index}"] = edge.to_id
                clauses = [
                    f"a.id = $projection_source_{index}",
                    f"b.id = $projection_target_{index}",
                ]
                for property_index, property_name in enumerate(
                    _TOMBSTONE_EDGE_PROPERTIES
                ):
                    value = edge.attrs[property_name]
                    if value is None:
                        clauses.append(f"r.{property_name} IS NULL")
                        continue
                    parameter = f"projection_{index}_{property_index}"
                    params[parameter] = value
                    expression = f"${parameter}"
                    if (
                        property_name == "created_at"
                        and isinstance(value, str)
                        and value
                    ):
                        expression = f"timestamp(${parameter})"
                    clauses.append(f"r.{property_name} = {expression}")
                predicates.append("(" + " AND ".join(clauses) + ")")

            preservation = (
                " AND NOT (" + " OR ".join(predicates) + ")" if predicates else ""
            )
            self.execute(
                f"MATCH (a:{from_type})-[r:{rel_name}]->(b:{to_type}) "
                "WHERE r.created_by_session_id = $session_id"
                f"{preservation} DELETE r",
                params,
            )

    def delete_nodes_by_session(
        self,
        session_id: str,
        node_types: tuple[str, ...],
    ) -> tuple[str, ...]:
        failed: list[str] = []
        for node_type in node_types:
            try:
                self.execute(
                    f"MATCH (n:{node_type}) "
                    "WHERE n.source_session_id = $session_id DETACH DELETE n",
                    {"session_id": session_id},
                )
            except Exception:
                failed.append(node_type)
        return tuple(failed)

    def increment_attestation(
        self,
        node_type: str,
        node_id: str,
        *,
        attested_at: str,
    ) -> None:
        self.execute(
            f"MATCH (n:{node_type} {{id: $node_id}}) "
            "SET n.attestation_count = coalesce(n.attestation_count, 1) + 1, "
            "n.last_attested_at = timestamp($attested_at)",
            {"node_id": node_id, "attested_at": attested_at},
        )

    async def commit(self) -> None:
        self._close(phase="commit")

    async def rollback(self) -> None:
        if not self._finished:
            logger.warning(
                "kg.graph_transaction.rollback_best_effort board=%s — embedded "
                "Kùzu auto-commits per statement; staged writes are not undone.",
                self._board_id,
            )
        self._close(phase="rollback")

    def _close(self, *, phase: str) -> None:
        if self._finished:
            if self._close_failure is not None:
                raise RuntimeError(
                    "graph_transaction_scope_close_failed"
                ) from self._close_failure
            return
        self._finished = True
        release_writer = True
        try:
            strict_close = getattr(self._connection, "close_strict", None)
            if callable(strict_close):
                strict_close()
            else:
                self._connection.close()
        except BaseException as exc:
            self._close_failure = exc
            release_writer = False
            logger.error(
                "kg.graph_transaction.close_failed board=%s phase=%s "
                "statement_kind=none writer_retained=true error_type=%s",
                self._board_id,
                phase,
                type(exc).__name__,
                extra={
                    "event": "kg.graph_transaction.close_failed",
                    "board_id": self._board_id,
                    "phase": phase,
                    "statement_kind": "none",
                    "writer_retained": True,
                    "error_type": type(exc).__name__,
                },
            )
            raise
        finally:
            if release_writer:
                self._writer_lease.release()
                logger.debug(
                    "kg.graph_transaction.writer_released board=%s phase=%s "
                    "statement_kind=none wait_ms=%d",
                    self._board_id,
                    phase,
                    self._writer_lease.wait_ms,
                    extra={
                        "event": "kg.graph_transaction.writer_released",
                        "board_id": self._board_id,
                        "phase": phase,
                        "statement_kind": "none",
                        "wait_ms": self._writer_lease.wait_ms,
                    },
                )

    async def __aenter__(self) -> "_KuzuTransactionScope":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            await self.rollback()
        else:
            await self.commit()


class CommunityKuzuGraphTransaction:
    """GraphTransaction adapter: begin(board_id) opens a BoardConnection scope."""

    def __init__(
        self,
        *,
        writer_lock_timeout_s: float = DEFAULT_WRITER_TIMEOUT_S,
    ) -> None:
        if writer_lock_timeout_s <= 0:
            raise ValueError("writer_lock_timeout_s must be positive")
        self._writer_lock_timeout_s = float(writer_lock_timeout_s)

    async def begin(self, board_id: str) -> _KuzuTransactionScope:
        writer_lease = await acquire_ladybug_writer_async(
            scope=board_id,
            phase="graph_transaction",
            timeout_s=self._writer_lock_timeout_s,
        )
        return _KuzuTransactionScope(
            board_id,
            writer_lease=writer_lease,
        )


__all__ = [
    "CommunityKuzuGraphTransaction",
    "_materialize",
    "_statement_is_write",
    "_statement_kind",
]
