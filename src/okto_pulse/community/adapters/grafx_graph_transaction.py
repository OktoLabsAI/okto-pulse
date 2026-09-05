"""Transactional Core ``GraphTransaction`` provider backed by Okto Grafx.

The provider owns a Grafx transaction, not the database handle.  Database
lifecycle and board routing remain composition concerns and are injected via a
resolver so one scope cannot accidentally close a handle shared by readers.
"""

from __future__ import annotations

import logging
import math
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
    SOURCE_PROJECTION_REMOVED_REASON,
    GraphNodePropertyBeforeImage,
    GraphStatementResult,
    ProjectionActiveSetIntent,
    ProjectionActiveSetReceipt,
    ProjectionActiveSetReconciliationError,
    ProjectionEdgeBeforeImage,
    ProjectionNodeBeforeImage,
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
    NODE_TYPES,
    REL_TYPES,
)

from okto_pulse.community.adapters.cypher_statement_policy import (
    strip_comments_and_literals,
)
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_relationship_layout import (
    resolve_relationship_table,
)

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_LOGICAL_RELATIONSHIP_PROPERTY_SCAN = re.compile(
    r"\A\s*MATCH\s+\([A-Za-z_][A-Za-z0-9_]*\)\s*-\s*"
    r"\[(?P<relationship_alias>[A-Za-z_][A-Za-z0-9_]*):"
    r"(?P<logical_type>[A-Za-z_][A-Za-z0-9_]*)\]\s*->\s*"
    r"\([A-Za-z_][A-Za-z0-9_]*\)\s*"
    r"(?:WHERE\b.*?)?\s*RETURN\s+"
    r"(?P=relationship_alias)\.layer\s*,\s*"
    r"(?P=relationship_alias)\.rule_id\s*;?\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
_NODE_ALIAS_LABEL = re.compile(
    r"\(\s*(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
    r"(?P<label>[A-Za-z_][A-Za-z0-9_]*)\b"
)
_TYPED_LOGICAL_RELATIONSHIP = re.compile(
    r"\(\s*(?P<left_alias>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*:\s*(?P<left_label>[A-Za-z_][A-Za-z0-9_]*))?[^()]*\)\s*"
    r"(?P<left_arrow><-|-)[ \t\r\n]*\[[ \t\r\n]*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*[ \t\r\n]*)?:[ \t\r\n]*"
    r"(?P<logical_type>[A-Za-z_][A-Za-z0-9_]*)\b[^\]]*\][ \t\r\n]*"
    r"(?P<right_arrow>->|-)[ \t\r\n]*"
    r"\(\s*(?P<right_alias>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*:\s*(?P<right_label>[A-Za-z_][A-Za-z0-9_]*))?[^()]*\)"
)
_IDENTITY_PROPERTIES = frozenset({"id", "source_session_id"})
# The relational projection materializes exactly these two node tables; naming them keeps the
# owned-set discovery bounded to a read the provider can actually justify, rather than a sweep
# of every node table on the board.
_RELATIONAL_PROJECTION_NODE_TYPES = ("Decision", "Alternative")
_PROJECTION_OWNER_NODE_TYPE = "Entity"
_PROJECTION_OWNER_EDGE_TYPE = "belongs_to"
_SPEC_DEPENDENCY_EDGE_TYPE = "precedes"
_SPEC_DEPENDENCY_RULE_PREFIX = "precedes/spec_dependency/"
_PROJECTION_EDGE_IDENTITY_CONFLICT = (
    "Grafx projection edge identity conflicts with its before-image."
)
_PROJECTION_EDGE_RESTORE_INCOMPLETE = (
    "Grafx could not confirm a restored projection edge."
)
_PROJECTION_DEPENDENCY_CLEANUP_UNCONFIRMED = (
    "Grafx could not confirm the Spec dependency edge cleanup."
)
_PROJECTION_MEMBER_STATE_UNEXPECTED = (
    "Grafx projection member was not in the state its before-image recorded."
)
_PROJECTION_MEMBER_RESTORE_UNCONFIRMED = (
    "Grafx could not confirm the projection member restore."
)
_PROJECTION_MEMBER_VANISHED = "Grafx projection member disappeared during its removal."
_PROJECTION_MEMBER_CLEANUP_UNCONFIRMED = (
    "Grafx could not confirm the stale projection member cleanup."
)
_PROJECTION_RESTORE_FAILED_SCOPE_DISCARDED = (
    "Projection reconciliation failed and its complete before-image could not be "
    "restored; the staged scope was discarded."
)
_PROJECTION_SCOPE_DISCARD_UNPROVEN = (
    "Grafx could not prove the staged projection scope was discarded."
)
_PROJECTION_COMPENSATION_UNCONFIRMED = (
    "Grafx could not confirm the projection compensation restored the recorded state."
)
_PROJECTION_COMPENSATION_NODE_MISSING = (
    "Grafx projection member is absent, so its before-image cannot be restored."
)
_PROJECTION_RESTORE_FAILED_SCOPE_UNCONFIRMED = (
    "Projection reconciliation failed, its complete before-image could not be restored, "
    "and the staged scope could not be proven discarded"
)
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

DatabaseResolver = Callable[
    [str], "Database | tuple[Database, ScopeTerminalCallback | None]"
]
"""Resolve a board's database.

May return the ``Database`` alone, as it always has, or a ``(database, release)``
pair. The pair form is what lets a pooled resolver hand over a handle it has
ALREADY pinned: the pin has to exist before ``database.begin`` runs, because a
handle that is merely resolved can still be evicted or closed in the window
before the scope owns it.
"""
FenceRevalidator = Callable[[str, str], None]
ScopeTerminalCallback = Callable[[], None]
"""Told once, after the engine agrees the transaction is over.

The owner of the database handle needs to know when a scope is finished so it can
stop keeping the handle alive.  It must not be told while the transaction is
still active, and it must not be told twice, because on the other side of this
callback is a lease whose second release would free a handle somebody else holds.
"""
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


def _resolved_database(
    resolved: object,
) -> tuple[Database, ScopeTerminalCallback | None]:
    """Accept either a bare database or a ``(database, release)`` pair.

    Keeping both shapes is what lets a pooled resolver pin before ``begin``
    without breaking every existing caller that returns only a handle.
    """

    if isinstance(resolved, tuple):
        if len(resolved) != 2:
            raise ValueError(
                "a Grafx database resolver returns a database or (database, release)"
            )
        database, release = resolved
        if release is not None and not callable(release):
            raise ValueError("a Grafx database release must be callable")
        return database, release
    return resolved, None  # type: ignore[return-value]


def _release_quietly(release: ScopeTerminalCallback, primary: BaseException) -> None:
    """Give a pin back while an error is in flight, without replacing it."""

    try:
        release()
    except Exception as failure:  # noqa: BLE001 - attached, never substituted
        primary.add_note(f"releasing the Grafx database also failed: {failure}")


def _chain_releases(
    first: ScopeTerminalCallback | None,
    second: ScopeTerminalCallback,
) -> ScopeTerminalCallback:
    """Run both terminal callbacks, and both even if the first one raises."""

    if first is None:
        return second

    def release_both() -> None:
        try:
            first()
        except BaseException as failure:
            try:
                second()
            except Exception as also:  # noqa: BLE001 - attached, never substituted
                failure.add_note(f"the second Grafx release also failed: {also}")
            raise
        second()

    return release_both


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


def _grafx_query_parameter_value(value: Any) -> Any:
    """Translate Pulse parameter values into Grafx's public value domain.

    Core legitimately supplies timezone-aware ``datetime`` objects to generic
    Cypher operations (including cancellation decay and batched recovery
    rows).  Grafx deliberately accepts its immutable ``Timestamp`` value at
    the public boundary instead of retaining a Python ``datetime`` capability,
    so adapt that impedance mismatch recursively before entering the engine.
    """

    if isinstance(value, datetime):
        return _timestamp_from_iso(value)
    if isinstance(value, Mapping):
        return {key: _grafx_query_parameter_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_grafx_query_parameter_value(item) for item in value)
    if isinstance(value, list):
        return [_grafx_query_parameter_value(item) for item in value]
    return value


def _grafx_query_parameters(
    params: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        key: _grafx_query_parameter_value(value)
        for key, value in (params or {}).items()
    }


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


def _contains_non_finite_number(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_contains_non_finite_number(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite_number(item) for item in value)
    return False


_CATALOG_CHANGING_STATEMENT = re.compile(r"\b(?:ALTER|DROP|CALL|INSTALL|LOAD)\b")
"""Grammar that can change the catalog outright: DDL verbs, procedures and extensions."""

_NON_ROW_CREATE = re.compile(r"\b(?:CREATE|MERGE)\b(?!\s*\()")
"""A CREATE/MERGE that is not the row form ``CREATE (``: tables, spaces, indexes, sequences
and anything not yet known all look like this, and every one of them drops the snapshot."""

_LEADING_STATEMENT_TOKEN = re.compile(r"^\s*(?:EXPLAIN\s+|PROFILE\s+)?([A-Z_]+)")
_CATALOG_NEUTRAL_LEADING_TOKENS = frozenset(
    {
        "MATCH",
        "OPTIONAL",
        "CREATE",
        "MERGE",
        "UNWIND",
        "WITH",
        "RETURN",
        "DELETE",
        "SET",
        "REMOVE",
    }
)
"""Leading tokens of row-level statements; any other leading token is treated as catalog-changing."""


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
        on_terminal: ScopeTerminalCallback | None = None,
    ) -> None:
        self._board_id = board_id
        self._database = database
        self._transaction = transaction
        self._revalidate_fence = revalidate_fence
        self._node_types = node_types
        self._relationship_pairs = relationship_pairs
        self._relationship_table_resolver = relationship_table_resolver
        self._on_terminal = on_terminal
        # One public catalog snapshot per scope.  ``Database.catalog`` builds a complete,
        # linearized copy of the catalog on every access and its ``CatalogView`` answers
        # ``table()``/``space()`` by linear scan; a single Pulse operation used to pay that
        # once per resolved node type, relationship pair and vector column (94 snapshots
        # and 5.6 s of one 12-family sample on the M-PULSE-7 board).  The snapshot is
        # captured on first use, indexed by name, and dropped the moment THIS scope emits a
        # statement that can change the catalog (see ``_statement_changes_catalog``).  It is
        # never shared across scopes: a new scope always starts from the live catalog.
        self._catalog_view: Any | None = None
        self._catalog_tables: dict[str, Any] = {}
        self._catalog_spaces: dict[str, Any] = {}
        self._relationship_definitions: dict[tuple[str, str, str], tuple[str, Any]] = {}
        self._column_maps: dict[str, dict[str, Any]] = {}
        self._settled = False
        # Set when a release failed after a durable commit, so the fault is
        # findable without pretending the commit did not happen.
        self.terminal_release_error: BaseException | None = None
        self._finished = False

    def relationship_table_name(
        self,
        logical_type: str,
        from_type: str,
        to_type: str,
    ) -> str:
        """Resolve a logical Pulse endpoint pair inside this pinned Grafx scope."""

        return self._relationship_table_resolver(logical_type, from_type, to_type)

    def _settle(self) -> None:
        """Tell the owner the transaction is over -- once, and never too early.

        Gated on the ENGINE rather than on this object's own flag. A scope can
        consider itself finished while the transaction is still open (a commit
        that raised without closing it), and releasing then would hand the
        handle back while writes can still reach it.
        """

        if self._settled or self._on_terminal is None:
            return
        if self._transaction.active:
            return
        # Marked before the call, so a callback that raises is not retried into
        # a second release.
        self._settled = True
        self._on_terminal()

    def _settle_quietly(self, primary: BaseException) -> None:
        """Settle while an error is in flight, without replacing it."""

        try:
            self._settle()
        except Exception as failure:  # noqa: BLE001 - attached, never substituted
            primary.add_note(f"releasing the Grafx scope also failed: {failure}")

    def _settle_after_durable_commit(self) -> None:
        """Settle a committed scope without turning a release fault into a retry.

        The write is already durable. Raising here would present a completed
        commit as failed, and a caller acting on that would redo work the
        database has kept. The fault is recorded on the scope instead, where an
        operator can find it, and the commit stands.
        """

        try:
            self._settle()
        except Exception as failure:  # noqa: BLE001 - the commit is durable
            self.terminal_release_error = failure
            logger.warning(
                "kg.graph_transaction.release_failed board=%s phase=commit "
                "commit_durable=True error_type=%s",
                self._board_id,
                type(failure).__name__,
                extra={
                    "event": "kg.graph_transaction.release_failed",
                    "board_id": self._board_id,
                    "phase": "commit",
                    "commit_durable": True,
                    "write_may_be_applied": True,
                    "error_type": type(failure).__name__,
                },
            )

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
            return self._transaction.execute(
                statement,
                _grafx_query_parameters(params),
            )
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

    def _expand_logical_relationship_property_scan(
        self,
        statement: str,
    ) -> tuple[str, ...]:
        """Fan one frozen logical relationship scan into physical Grafx tables.

        Pulse's metrics family reads ``r.layer`` and ``r.rule_id`` through an
        untyped-endpoint logical relationship name.  Grafx stores each allowed
        endpoint pair in a distinct physical table.  Executing one branch per
        declared pair and concatenating the rows preserves the required
        multiset semantics; using Cypher ``UNION`` would incorrectly discard
        duplicate rows.  No aggregate, ordering, limit, write, or open-ended
        statement shape is widened here.
        """

        match = _LOGICAL_RELATIONSHIP_PROPERTY_SCAN.fullmatch(statement)
        if match is None:
            return (statement,)
        logical_type = match.group("logical_type")
        physical_tables = tuple(
            dict.fromkeys(
                self._relationship_table_resolver(edge_type, from_type, to_type)
                for edge_type, from_type, to_type in self._relationship_pairs
                if edge_type == logical_type
            )
        )
        if not physical_tables:
            return (statement,)
        start, end = match.span("logical_type")
        return tuple(
            f"{statement[:start]}{physical}{statement[end:]}"
            for physical in physical_tables
        )

    def _translate_typed_logical_relationships(self, statement: str) -> str:
        """Resolve endpoint-typed logical relationship names without widening Cypher.

        The two endpoint labels select exactly one table in the immutable Pulse
        layout.  Labels may be present on the relationship pattern itself or on
        earlier ``MATCH`` clauses that bind the aliases used by ``CREATE``.
        Literals and comments are blanked before scanning, so text that merely
        resembles a pattern is never rewritten.
        """

        code = strip_comments_and_literals(statement)
        labels_by_alias: dict[str, set[str]] = {}
        for node_match in _NODE_ALIAS_LABEL.finditer(code):
            labels_by_alias.setdefault(node_match.group("alias"), set()).add(
                node_match.group("label")
            )

        def resolved_label(alias: str, local: str | None) -> str | None:
            if local is not None:
                return local
            candidates = labels_by_alias.get(alias, set())
            return next(iter(candidates)) if len(candidates) == 1 else None

        replacements: list[tuple[int, int, str]] = []
        for relationship_match in _TYPED_LOGICAL_RELATIONSHIP.finditer(code):
            left_label = resolved_label(
                relationship_match.group("left_alias"),
                relationship_match.group("left_label"),
            )
            right_label = resolved_label(
                relationship_match.group("right_alias"),
                relationship_match.group("right_label"),
            )
            if left_label is None or right_label is None:
                continue
            left_arrow = relationship_match.group("left_arrow")
            right_arrow = relationship_match.group("right_arrow")
            if left_arrow == "-" and right_arrow == "->":
                from_type, to_type = left_label, right_label
            elif left_arrow == "<-" and right_arrow == "-":
                from_type, to_type = right_label, left_label
            else:
                continue
            logical_type = relationship_match.group("logical_type")
            if (logical_type, from_type, to_type) not in self._relationship_pairs:
                continue
            physical = self._relationship_table_resolver(
                logical_type,
                from_type,
                to_type,
            )
            start, end = relationship_match.span("logical_type")
            replacements.append((start, end, physical))

        translated = statement
        for start, end, physical in reversed(replacements):
            translated = f"{translated[:start]}{physical}{translated[end:]}"
        return translated

    def _catalog(self) -> Any:
        """Return this scope's catalog snapshot, captured from the public API once.

        Only ``Database.catalog`` is consulted -- the same public view the adapter always
        read -- so the AF21 reach-in ledger does not move.  The view is an immutable
        snapshot; what makes it safe to keep for the life of one scope is that the only
        way this scope can change the catalog is a statement through ``execute``, and that
        path drops the snapshot before running anything that could.
        """

        if self._catalog_view is None:
            view = self._database.catalog.catalog
            self._catalog_view = view
            self._catalog_tables = {table.name: table for table in view.tables()}
            self._catalog_spaces = {space.name: space for space in view.spaces()}
        return self._catalog_view

    def _forget_catalog(self) -> None:
        """Drop every catalog-derived memo; the next resolution re-reads the public view."""

        self._catalog_view = None
        self._catalog_tables = {}
        self._catalog_spaces = {}
        self._relationship_definitions = {}
        self._column_maps = {}

    def _catalog_table(self, name: str, *, operation: str) -> Any:
        """Look a table up in the scope's snapshot, failing exactly as the view would."""

        try:
            view = self._catalog()
            definition = self._catalog_tables.get(name)
            if definition is not None:
                return definition
            return view.table(name)
        except Exception as exc:
            mapped = map_grafx_error(exc, operation=operation)
            raise mapped from exc

    def _catalog_space(self, name: str) -> Any:
        view = self._catalog()
        space = self._catalog_spaces.get(name)
        if space is not None:
            return space
        return view.space(name)

    @staticmethod
    def _statement_changes_catalog(statement: str) -> bool:
        """Say whether a statement may change the catalog, failing towards ``True``.

        Row-level statements (``MATCH``/``SET``/``DELETE`` over nodes and relationships,
        and ``CREATE``/``MERGE`` only in their row form ``CREATE (``, wherever they occur)
        cannot change the catalog.  Everything else can and drops the snapshot: ``ALTER``,
        ``DROP``, ``CALL``, ``INSTALL``, ``LOAD``, any ``CREATE``/``MERGE`` that is not
        immediately followed by ``(`` (tables, spaces, indexes, sequences, forms not known
        yet) and any statement whose leading token this scanner does not know.  Literals
        and comments are blanked first, so DDL-looking text inside a string never counts;
        a statement the engine refuses as malformed cannot change the catalog either way.
        """

        normalized = strip_comments_and_literals(statement)
        upper = normalized.upper()
        if _CATALOG_CHANGING_STATEMENT.search(upper) or _NON_ROW_CREATE.search(upper):
            return True
        first = _LEADING_STATEMENT_TOKEN.match(upper)
        return first is None or first.group(1) not in _CATALOG_NEUTRAL_LEADING_TOKENS

    def _node_definition(self, node_type: str):
        name = _identifier("node type", node_type)
        definition = self._catalog_table(name, operation="node_schema")
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
        key = (logical, source, target)
        memo = self._relationship_definitions.get(key)
        if memo is not None:
            return memo
        physical = _identifier(
            "relationship table",
            self._relationship_table_resolver(logical, source, target),
        )
        definition = self._catalog_table(physical, operation="relationship_schema")
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
        self._relationship_definitions[key] = (physical, definition)
        return physical, definition

    def _column_map(self, definition: Any) -> dict[str, Any]:
        columns = self._column_maps.get(definition.name)
        if columns is None:
            columns = {column.name: column for column in definition.columns}
            self._column_maps[definition.name] = columns
        return columns

    def _coerce_value(self, column: Any, value: Any) -> Any:
        if value is None:
            return None
        if column.type.name == "TIMESTAMP" and not isinstance(value, Timestamp):
            if not isinstance(value, (str, datetime)):
                raise ValueError(
                    f"timestamp property {column.name!r} requires ISO text or datetime"
                )
            return _timestamp_from_iso(value)
        if column.is_vector and not isinstance(value, VectorValue):
            if not isinstance(value, (list, tuple)):
                raise ValueError(
                    f"vector property {column.name!r} requires a numeric sequence"
                )
            space = self._catalog_space(str(column.vector_space))
            components = tuple(float(item) for item in value)
            if len(components) != space.dimension:
                raise ValueError(
                    f"vector property {column.name!r} requires {space.dimension} "
                    f"components, got {len(components)}"
                )
            return VectorValue(
                values=components,
                space_ref=space.space_id,
                dtype=space.storage_dtype,
            )
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
        """Run one statement on THIS scope's transaction, fencing any write.

        Reads go through the scope rather than a fresh snapshot so a caller
        sees its own uncommitted writes, which is what makes read-your-own-
        writes hold inside a unit of work.  A statement that Core cannot prove
        read-only is treated as a write and revalidates the fence first: losing
        authority mid-transaction must stop the statement, not be discovered at
        commit.  The grammar is Core's; nothing is widened here.
        """

        from okto_pulse.community.adapters.grafx_cypher_executor import (
            pulse_value,
            statement_is_write,
            statement_kind,
        )

        self._require_active()
        if statement_is_write(statement):
            self._fence("graph_statement_precommit")
            if self._statement_changes_catalog(statement):
                # Dropped BEFORE the statement runs, so the outcome cannot matter: whether
                # the DDL lands or fails, the next resolution reads the live catalog.
                self._forget_catalog()
        try:
            prepared_params = _grafx_query_parameters(params)
            translated = self._translate_typed_logical_relationships(statement)
            statements = self._expand_logical_relationship_property_scan(translated)
            results = tuple(
                self._transaction.execute(candidate, prepared_params)
                for candidate in statements
            )
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="graph_statement")
            kind = statement_kind(statement)
            mapped.details.setdefault("phase", "execute")
            mapped.details.setdefault("statement_kind", kind)
            mapped.details.setdefault("error_code", mapped.code)
            mapped.details.setdefault("retryable", mapped.retryable)
            if mapped is exc:
                raise
            raise mapped from exc
        if len(results) == 1:
            result = results[0]
            if result is None:
                return GraphStatementResult()
            columns = tuple(str(name) for name in getattr(result, "columns", ()) or ())
            rows = tuple(
                tuple(pulse_value(cell) for cell in row)
                for row in getattr(result, "rows", ()) or ()
            )
            return GraphStatementResult(rows=rows, columns=columns)

        if any(result is None for result in results):
            raise GraphError(
                "Grafx logical relationship scan returned no query result.",
                details={
                    "backend": "okto_grafx",
                    "operation": "graph_statement",
                    "reason": "logical_relationship_scan_result_missing",
                },
            )
        branch_columns = tuple(
            tuple(str(name) for name in getattr(result, "columns", ()) or ())
            for result in results
        )
        if len(set(branch_columns)) != 1:
            raise GraphError(
                "Grafx logical relationship scan returned inconsistent columns.",
                details={
                    "backend": "okto_grafx",
                    "operation": "graph_statement",
                    "reason": "logical_relationship_scan_columns_mismatch",
                },
            )
        if not branch_columns:
            return GraphStatementResult()
        rows = tuple(
            tuple(pulse_value(cell) for cell in row)
            for result in results
            for row in getattr(result, "rows", ()) or ()
        )
        return GraphStatementResult(rows=rows, columns=branch_columns[0])

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
            definitions = self._catalog().tables()
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
        # From the SET through both confirmations an exception is an uncertain
        # staged outcome.  Poison and roll back the whole scope so a caller that
        # catches the error cannot later publish an unconfirmed replacement.
        self._fence("replace_node_payload_apply")
        try:
            result = self._query(
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
        except BaseException as primary_error:
            cleanup_error = self._abort_after_staged_failure(
                operation="replace_node_payload"
            )
            if cleanup_error is not None:
                try:
                    primary_error.add_note(
                        "Grafx rollback also failed while discarding an "
                        "unconfirmed payload replacement."
                    )
                except BaseException:  # noqa: BLE001, S110 - diagnostic only
                    pass
                raise primary_error from cleanup_error
            raise
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
            # The rollback failed, so the engine decides whether the transaction
            # is over; _settle asks it rather than assuming.
            self._settle_quietly(cleanup_error)
            return cleanup_error
        self._settle()
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

    def _projection_logical_edge_type(
        self,
        physical: str,
        from_type: str,
        to_type: str,
    ) -> str:
        """Name a physical relationship table the way the Core contract names it.

        A before-image is a promise that this exact relationship can be recreated, and the
        contract states that promise in LOGICAL names.  The provider's resolver only maps the
        other way and nothing declares it injective: two logical types may be configured onto
        one table.  Picking one of them would record a before-image that restores a different
        relationship than the one removed, so an absent or ambiguous inversion is refused
        before anything is mutated -- a pre-mutation inability to build a compensable
        before-image, which is what ``snapshot_failed`` means, not a defect in the intent.
        """

        candidates = sorted(
            {
                edge_type
                for edge_type, source, target in self._relationship_pairs
                if source == from_type
                and target == to_type
                and self._relationship_table_resolver(edge_type, source, target)
                == physical
            }
        )
        if len(candidates) != 1:
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_snapshot_failed",
                f"Grafx relationship table {physical!r} has no single logical name for "
                f"{from_type}->{to_type}; a compensable before-image cannot be built.",
            )
        return candidates[0]

    @staticmethod
    def _projection_edge_signature(edge: ProjectionEdgeBeforeImage) -> tuple[Any, ...]:
        return (
            edge.edge_type,
            edge.from_type,
            edge.to_type,
            edge.from_id,
            edge.to_id,
            tuple(sorted((str(key), repr(value)) for key, value in edge.attrs.items())),
        )

    @staticmethod
    def _projection_dependency_rule_id(
        edge: ProjectionEdgeBeforeImage,
    ) -> str | None:
        """The rule that identifies a Spec dependency edge, or None for anything else.

        One prerequisite may precede one owner under more than one rule, so for these edges
        the endpoints alone do not name a single relationship.  Every other relationship is
        identified by its endpoints, and asking for a ``rule_id`` it may not even declare
        would fail for the wrong reason.
        """

        rule_id = str(edge.attrs.get("rule_id") or "")
        if (
            edge.edge_type == _SPEC_DEPENDENCY_EDGE_TYPE
            and edge.from_type == _PROJECTION_OWNER_NODE_TYPE
            and edge.to_type == _PROJECTION_OWNER_NODE_TYPE
            and rule_id.startswith(_SPEC_DEPENDENCY_RULE_PREFIX)
        ):
            return rule_id
        return None

    def _projection_edge_properties(self, definition: Any) -> tuple[str, ...]:
        del self
        # Columns 0 and 1 of a Grafx rel table are its endpoints; the rest is the payload.
        return tuple(column.name for column in definition.columns[2:])

    def _projection_incident_edges(
        self,
        node_type: str,
        node_id: str,
    ) -> tuple[ProjectionEdgeBeforeImage, ...]:
        """Read every relationship incident to the node, completely and by logical name.

        The committed catalog is the authority here rather than the configured pairs: an
        edge that is stored but unmapped must not be able to vanish from a before-image just
        because the configuration forgot it.  That is also why the inversion above refuses an
        unmapped table instead of skipping it.
        """

        edges: list[ProjectionEdgeBeforeImage] = []
        for (
            physical,
            from_type,
            to_type,
            definition,
        ) in self._incident_relationship_definitions(node_type):
            properties = self._projection_edge_properties(definition)
            projection = ["a.id", "b.id", *(f"r.{name}" for name in properties)]
            predicate = self._incident_predicate(node_type, from_type, to_type)
            result = self._query(
                f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type}) "
                f"WHERE {predicate} RETURN {', '.join(projection)}",
                {"node_id": node_id},
                operation="snapshot_projection_incident_edges",
            )
            if not result.rows:
                # An unmapped table that holds nothing incident to this node takes nothing
                # away from the before-image, so it is not a reason to refuse.  The naming
                # requirement below applies to edges that actually exist.
                continue
            logical = self._projection_logical_edge_type(physical, from_type, to_type)
            edges.extend(
                ProjectionEdgeBeforeImage(
                    edge_type=logical,
                    from_type=from_type,
                    to_type=to_type,
                    from_id=str(row[0]),
                    to_id=str(row[1]),
                    attrs={
                        name: _normalize_value(row[index + 2])
                        for index, name in enumerate(properties)
                    },
                )
                for row in result.rows
            )
        return tuple(edges)

    def _projection_node_before_image(
        self,
        node_type: str,
        node_id: str,
    ) -> ProjectionNodeBeforeImage | None:
        snapshot = self._node_snapshot(node_type, node_id)
        if snapshot is None:
            return None
        return ProjectionNodeBeforeImage(
            node_type=node_type,
            node_id=node_id,
            source_session_id=snapshot.get("source_session_id"),
            attrs={
                name: value
                for name, value in snapshot.items()
                if name not in _IDENTITY_PROPERTIES
            },
            incident_edges=self._projection_incident_edges(node_type, node_id),
        )

    def _projection_matching_edges(
        self,
        edge: ProjectionEdgeBeforeImage,
    ) -> Counter[tuple[Any, ...]]:
        """How many of each stored relationship share this one's identity.

        A multiset, not a set: Grafx stores byte-identical parallel edges, so "this edge is
        present" and "both copies of this edge are present" are different facts and a set
        cannot tell them apart.
        """

        rule_id = self._projection_dependency_rule_id(edge)
        physical, definition = self._relationship_definition(
            edge.edge_type,
            edge.from_type,
            edge.to_type,
        )
        properties = self._projection_edge_properties(definition)
        params: dict[str, Any] = {"from_id": edge.from_id, "to_id": edge.to_id}
        predicate = "a.id = $from_id AND b.id = $to_id"
        if rule_id is not None:
            predicate += " AND r.rule_id = $rule_id"
            params["rule_id"] = rule_id
        projection = ", ".join(f"r.{name}" for name in properties) or "a.id"
        result = self._query(
            f"MATCH (a:{edge.from_type})-[r:{physical}]->(b:{edge.to_type}) "
            f"WHERE {predicate} RETURN {projection}",
            params,
            operation="projection_edge_lookup",
        )
        return Counter(
            self._projection_edge_signature(
                ProjectionEdgeBeforeImage(
                    edge_type=edge.edge_type,
                    from_type=edge.from_type,
                    to_type=edge.to_type,
                    from_id=edge.from_id,
                    to_id=edge.to_id,
                    attrs={
                        name: _normalize_value(row[index])
                        for index, name in enumerate(properties)
                    },
                )
            )
            for row in result.rows
        )

    def _projection_restore_edges(
        self,
        edges: tuple[ProjectionEdgeBeforeImage, ...],
    ) -> None:
        """Put each recorded relationship back exactly once and prove it is the one recorded.

        Compensation must be repeatable, including in a fresh scope that never applied the
        removal, so an edge already present exactly as recorded is left alone rather than
        created a second time.  An edge present with a DIFFERENT payload is not the one that
        was removed; recreating over it would silently pick a winner, so it is refused.
        """

        wanted: dict[tuple[Any, ...], Counter[tuple[Any, ...]]] = {}
        templates: dict[tuple[Any, ...], dict[tuple[Any, ...], Any]] = {}
        for edge in edges:
            # Group by what a single lookup can ask about, so multiplicity is compared once
            # per identity rather than re-derived per recorded copy.
            key = (
                edge.edge_type,
                edge.from_type,
                edge.to_type,
                edge.from_id,
                edge.to_id,
                self._projection_dependency_rule_id(edge),
            )
            signature = self._projection_edge_signature(edge)
            wanted.setdefault(key, Counter())[signature] += 1
            templates.setdefault(key, {})[signature] = edge
        for key, desired in wanted.items():
            probe = next(iter(templates[key].values()))
            present = self._projection_matching_edges(probe)
            if present - desired:
                # Present but unrecorded, or present more times than recorded: whatever is
                # there is not what was removed, and creating over it would pick a winner.
                raise GraphError(
                    _PROJECTION_EDGE_IDENTITY_CONFLICT,
                    details={
                        "backend": "okto_grafx",
                        "board_id": self._board_id,
                        "edge_type": probe.edge_type,
                        "code": "projection_edge_restore_identity_conflict",
                    },
                )
            missing = desired - present
            if not missing:
                # Already exactly as recorded: compensation must be repeatable, including in
                # a scope that never applied the removal.
                continue
            for signature, count in missing.items():
                template = templates[key][signature]
                for _copy in range(count):
                    self.create_edge(
                        template.edge_type,
                        template.from_type,
                        template.to_type,
                        template.from_id,
                        template.to_id,
                        {
                            name: value
                            for name, value in template.attrs.items()
                            if value is not None
                        },
                    )
            if self._projection_matching_edges(probe) != desired:
                raise GraphError(
                    _PROJECTION_EDGE_RESTORE_INCOMPLETE,
                    details={
                        "backend": "okto_grafx",
                        "board_id": self._board_id,
                        "edge_type": probe.edge_type,
                        "code": "projection_edge_restore_incomplete",
                    },
                )

    def _projection_delete_incident_edges(
        self,
        node_type: str,
        node_id: str,
    ) -> None:
        """Delete every relationship incident to the node as the engine holds it now.

        Reading current state rather than replaying a recorded list matters on the
        compensation path: an edge that appeared after the failure is not in the
        before-image, and a restore that claims to be exact must not leave it behind.
        """

        seen: set[tuple[str, str, str, str, str]] = set()
        for edge in self._projection_incident_edges(node_type, node_id):
            identity = (
                edge.edge_type,
                edge.from_type,
                edge.to_type,
                edge.from_id,
                edge.to_id,
            )
            # Parallel edges between one pair are deleted by the single statement that names
            # the pair, so issuing it once per stored edge would delete nothing the second
            # time and read as a silent failure.
            if identity in seen:
                continue
            seen.add(identity)
            physical, _definition = self._relationship_definition(
                edge.edge_type,
                edge.from_type,
                edge.to_type,
            )
            self._mutation(
                f"MATCH (a:{edge.from_type})-[r:{physical}]->(b:{edge.to_type}) "
                "WHERE a.id = $from_id AND b.id = $to_id DELETE r",
                {"from_id": edge.from_id, "to_id": edge.to_id},
                operation="delete_projection_incident_edge",
            )

    def _projection_apply_failure(
        self,
        receipt: ProjectionActiveSetReceipt,
        apply_error: BaseException,
        *,
        operation: str,
    ) -> NoReturn:
        """Undo one failed active-set application, or refuse to let it commit.

        Grafx stages the whole scope, so there is no native sub-transaction to roll back
        around these mutations alone: the recorded before-image is the only way to undo them
        without discarding the caller's other staged work.  When it cannot be put back, the
        scope is poisoned instead, because a half-applied active set that reaches commit is
        worse than a scope that refuses to commit at all.
        """

        if not isinstance(apply_error, Exception):
            # A process signal is discarded first and re-raised as the SAME object.  Trying
            # to restore would run more writes under an interrupt, and leaving the discard
            # to __aexit__ would let a later rollback error replace the very signal the
            # caller has to see.
            cleanup_error = self._abort_after_staged_failure(operation=operation)
            if cleanup_error is not None:
                try:
                    apply_error.add_note(_PROJECTION_SCOPE_DISCARD_UNPROVEN)
                except BaseException:  # noqa: BLE001, S110 - diagnostic only
                    pass
                raise apply_error from cleanup_error
            raise apply_error
        try:
            self.compensate_projection_active_set(receipt)
        except BaseException as restore_error:
            cleanup_error = self._abort_after_staged_failure(operation=operation)
            message = _PROJECTION_RESTORE_FAILED_SCOPE_DISCARDED
            if cleanup_error is not None:
                message = (
                    f"{_PROJECTION_RESTORE_FAILED_SCOPE_UNCONFIRMED} "
                    f"({type(cleanup_error).__name__})."
                )
            # Chained from the restore failure, not from the cleanup one: why the board could
            # not be put back is the established primary, and cleanup never displaces it.
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_apply_and_restore_failed",
                message,
                receipt=receipt,
            ) from restore_error
        raise ProjectionActiveSetReconciliationError(
            "projection_active_set_apply_failed",
            "Projection reconciliation failed and was restored.",
            receipt=receipt,
        ) from apply_error

    def _reconcile_spec_dependency_edges(
        self,
        intent: ProjectionActiveSetIntent,
    ) -> ProjectionActiveSetReceipt:
        """Replace the exact set of Spec dependency edges pointing at one root."""

        if intent.active_nodes or intent.owner_node_id is None:
            raise ProjectionActiveSetReconciliationError(
                "projection_active_set_member_invalid",
                "The Spec dependency projection owns edges and requires its root.",
            )
        desired: set[tuple[str, str, str, str, str, str]] = set()
        desired_endpoints: set[tuple[str, str, str, str, str]] = set()
        for edge in intent.active_edges:
            endpoint_identity = (
                edge.edge_type,
                edge.from_type,
                edge.to_type,
                edge.from_id,
                edge.to_id,
            )
            if (
                edge.edge_type != _SPEC_DEPENDENCY_EDGE_TYPE
                or edge.from_type != _PROJECTION_OWNER_NODE_TYPE
                or edge.to_type != _PROJECTION_OWNER_NODE_TYPE
                or edge.to_id != intent.owner_node_id
                or not edge.rule_id.startswith(_SPEC_DEPENDENCY_RULE_PREFIX)
                # Two desired edges over one pair, even under different rules, leave the
                # active set ambiguous about which of them is meant to survive.
                or endpoint_identity in desired_endpoints
            ):
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_member_invalid",
                    "A Spec dependency edge is outside the exact projection scope.",
                )
            desired.add((*endpoint_identity, edge.rule_id))
            desired_endpoints.add(endpoint_identity)

        physical, definition = self._relationship_definition(
            _SPEC_DEPENDENCY_EDGE_TYPE,
            _PROJECTION_OWNER_NODE_TYPE,
            _PROJECTION_OWNER_NODE_TYPE,
        )
        properties = self._projection_edge_properties(definition)
        owned = self._projection_owned_dependency_edges(
            intent,
            physical,
            properties,
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
        receipt = ProjectionActiveSetReceipt(intent=intent, edge_before_images=stale)
        if not stale:
            return receipt

        try:
            for edge in stale:
                self._mutation(
                    f"MATCH (a:{_PROJECTION_OWNER_NODE_TYPE})-[r:{physical}]->"
                    f"(b:{_PROJECTION_OWNER_NODE_TYPE}) "
                    "WHERE a.id = $from_id AND b.id = $to_id "
                    "AND r.rule_id = $rule_id DELETE r",
                    {
                        "from_id": edge.from_id,
                        "to_id": edge.to_id,
                        "rule_id": str(edge.attrs.get("rule_id") or ""),
                    },
                    operation="delete_projection_dependency_edge",
                )
            self._confirm_dependency_cleanup(intent, physical, properties, stale)
        # Blind on purpose: a process signal raised after a staged write must reach the
        # recovery path below.  Narrowing this to Exception is what would let an interrupt
        # walk away from a half-applied active set and let it commit.
        except BaseException as apply_error:  # noqa: BLE001 - signals must reach recovery
            self._projection_apply_failure(
                receipt,
                apply_error,
                operation="reconcile_spec_dependency_edges",
            )
        return receipt

    def _confirm_dependency_cleanup(
        self,
        intent: ProjectionActiveSetIntent,
        physical: str,
        properties: tuple[str, ...],
        stale: tuple[ProjectionEdgeBeforeImage, ...],
    ) -> None:
        """Re-read the owned set and refuse to believe a removal that did not happen."""

        remaining = {
            (identity[3], identity[5])
            for identity, _edge in self._projection_owned_dependency_edges(
                intent,
                physical,
                properties,
            )
        }
        stale_pairs = {
            (edge.from_id, str(edge.attrs.get("rule_id") or "")) for edge in stale
        }
        if remaining.intersection(stale_pairs):
            raise GraphError(
                _PROJECTION_DEPENDENCY_CLEANUP_UNCONFIRMED,
                details={
                    "backend": "okto_grafx",
                    "board_id": self._board_id,
                    "owner_node_id": intent.owner_node_id,
                    "code": "projection_stale_edge_cleanup_unconfirmed",
                },
            )

    def _projection_owned_dependency_edges(
        self,
        intent: ProjectionActiveSetIntent,
        physical: str,
        properties: tuple[str, ...],
    ) -> tuple[
        tuple[tuple[str, str, str, str, str, str], ProjectionEdgeBeforeImage], ...
    ]:
        """Every Spec dependency edge this projection owns, with its complete payload.

        Ownership is the rule prefix: a ``precedes`` edge into the same root that was written
        by something else is not this projection's to remove.
        """

        projection = ", ".join(f"r.{name}" for name in properties)
        result = self._query(
            f"MATCH (a:{_PROJECTION_OWNER_NODE_TYPE})-[r:{physical}]->"
            f"(b:{_PROJECTION_OWNER_NODE_TYPE}) "
            f"WHERE b.id = $owner_id RETURN a.id, b.id, {projection}",
            {"owner_id": intent.owner_node_id},
            operation="projection_dependency_edges",
        )
        owned: list[
            tuple[tuple[str, str, str, str, str, str], ProjectionEdgeBeforeImage]
        ] = []
        for row in result.rows:
            attrs = {
                name: _normalize_value(row[index + 2])
                for index, name in enumerate(properties)
            }
            rule_id = str(attrs.get("rule_id") or "")
            if not rule_id.startswith(_SPEC_DEPENDENCY_RULE_PREFIX):
                continue
            owned.append(
                (
                    (
                        _SPEC_DEPENDENCY_EDGE_TYPE,
                        _PROJECTION_OWNER_NODE_TYPE,
                        _PROJECTION_OWNER_NODE_TYPE,
                        str(row[0]),
                        str(row[1]),
                        rule_id,
                    ),
                    ProjectionEdgeBeforeImage(
                        edge_type=_SPEC_DEPENDENCY_EDGE_TYPE,
                        from_type=_PROJECTION_OWNER_NODE_TYPE,
                        to_type=_PROJECTION_OWNER_NODE_TYPE,
                        from_id=str(row[0]),
                        to_id=str(row[1]),
                        attrs=attrs,
                    ),
                )
            )
        return tuple(owned)

    def _projection_owned_nodes(
        self,
        intent: ProjectionActiveSetIntent,
    ) -> tuple[tuple[str, str, str, str], ...]:
        """Read the two projected node tables, then apply the exact Core parser.

        A node is this projection's either because an owner edge says so exactly -- right
        rule, right owner, owner carrying the matching source reference -- or because it
        already carries this projection's removal reason, which is how a member removed in an
        earlier round stays reachable for restore.
        """

        owned: list[tuple[str, str, str, str]] = []
        for node_type in _RELATIONAL_PROJECTION_NODE_TYPES:
            owner_physical, _definition = self._relationship_definition(
                _PROJECTION_OWNER_EDGE_TYPE,
                node_type,
                _PROJECTION_OWNER_NODE_TYPE,
            )
            owner_rows = self._query(
                f"MATCH (n:{node_type})-[r:{owner_physical}]->"
                f"(owner:{_PROJECTION_OWNER_NODE_TYPE}) "
                "RETURN n.id, owner.id, owner.source_artifact_ref, r.rule_id",
                operation="projection_owner_edges",
            ).rows
            exact_owner_node_ids = {
                str(row[0] or "")
                for row in owner_rows
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
            rows = self._query(
                f"MATCH (n:{node_type}) RETURN n.id, n.source_artifact_ref, "
                "n.created_by_agent, n.revocation_reason",
                operation="projection_candidate_nodes",
            ).rows
            for row in rows:
                node_id = str(row[0] or "")
                source_ref = str(row[1] or "")
                if not is_relational_projection_node(
                    node_type=node_type,
                    source_artifact_ref=source_ref,
                    created_by_agent=str(row[2] or ""),
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
                owned.append((node_type, node_id, source_ref, reason))
        return tuple(owned)

    def _restore_projection_member(
        self,
        before_image: ProjectionNodeBeforeImage,
    ) -> None:
        """Return one removed member to the active set without touching anything else."""

        result = self._mutation(
            f"MATCH (n:{before_image.node_type}) "
            "WHERE n.id = $node_id AND n.revocation_reason = $reason "
            "SET n.revocation_reason = $cleared RETURN n.id",
            {
                "node_id": before_image.node_id,
                "reason": SOURCE_PROJECTION_REMOVED_REASON,
                "cleared": "",
            },
            operation="restore_projection_member",
        )
        if not result.rows:
            raise GraphError(
                _PROJECTION_MEMBER_STATE_UNEXPECTED,
                details={
                    "backend": "okto_grafx",
                    "board_id": self._board_id,
                    "node_type": before_image.node_type,
                    "code": "projection_active_member_restore_unconfirmed",
                },
            )
        current = self._projection_node_before_image(
            before_image.node_type,
            before_image.node_id,
        )
        expected_edges = Counter(
            self._projection_edge_signature(edge)
            for edge in before_image.incident_edges
        )
        if (
            current is None
            or str(current.attrs.get("revocation_reason") or "") != ""
            or Counter(
                self._projection_edge_signature(edge) for edge in current.incident_edges
            )
            != expected_edges
        ):
            raise GraphError(
                _PROJECTION_MEMBER_RESTORE_UNCONFIRMED,
                details={
                    "backend": "okto_grafx",
                    "board_id": self._board_id,
                    "node_type": before_image.node_type,
                    "code": "projection_active_member_restore_unconfirmed",
                },
            )

    def _remove_projection_member(
        self,
        before_image: ProjectionNodeBeforeImage,
    ) -> None:
        """Take one member out of the active set: its edges go, its identity stays.

        The write is not guarded on the reason it was read with, because inside one staged
        scope nothing else can have changed it since; what proves the removal is the re-read
        below, which can and does fail if the statement did not take.
        """

        self._projection_delete_incident_edges(
            before_image.node_type,
            before_image.node_id,
        )
        result = self._mutation(
            f"MATCH (n:{before_image.node_type}) WHERE n.id = $node_id "
            "SET n.revocation_reason = $reason RETURN n.id",
            {
                "node_id": before_image.node_id,
                "reason": SOURCE_PROJECTION_REMOVED_REASON,
            },
            operation="remove_projection_member",
        )
        if not result.rows:
            raise GraphError(
                _PROJECTION_MEMBER_VANISHED,
                details={
                    "backend": "okto_grafx",
                    "board_id": self._board_id,
                    "node_type": before_image.node_type,
                    "code": "projection_stale_member_tombstone_unconfirmed",
                },
            )
        current = self._projection_node_before_image(
            before_image.node_type,
            before_image.node_id,
        )
        if (
            current is None
            or str(current.attrs.get("revocation_reason") or "")
            != SOURCE_PROJECTION_REMOVED_REASON
            or current.incident_edges
        ):
            raise GraphError(
                _PROJECTION_MEMBER_CLEANUP_UNCONFIRMED,
                details={
                    "backend": "okto_grafx",
                    "board_id": self._board_id,
                    "node_type": before_image.node_type,
                    "code": "projection_stale_member_cleanup_unconfirmed",
                },
            )

    def reconcile_projection_active_set(
        self,
        intent: ProjectionActiveSetIntent,
    ) -> ProjectionActiveSetReceipt:
        """Atomically replace one exact relational active set."""

        # The whole intent is validated, and every before-image captured, before the first
        # mutation: a refusal must not be able to leave half an active set staged.
        self._fence("reconcile_projection_active_set")
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
        before_images = self._projection_before_images(owned, active_refs)
        receipt = ProjectionActiveSetReceipt(
            intent=intent,
            before_images=before_images,
        )
        if not before_images:
            return receipt

        try:
            for before_image in before_images:
                source_ref = str(before_image.attrs.get("source_artifact_ref") or "")
                if source_ref in active_refs:
                    self._restore_projection_member(before_image)
                    continue
                self._remove_projection_member(before_image)
        # Blind on purpose: a process signal raised after a staged write must reach the
        # recovery path below.  Narrowing this to Exception is what would let an interrupt
        # walk away from a half-applied active set and let it commit.
        except BaseException as apply_error:  # noqa: BLE001 - signals must reach recovery
            self._projection_apply_failure(
                receipt,
                apply_error,
                operation="reconcile_projection_active_set",
            )
        return receipt

    def _projection_before_images(
        self,
        owned: tuple[tuple[str, str, str, str], ...],
        active_refs: frozenset[str],
    ) -> tuple[ProjectionNodeBeforeImage, ...]:
        """Capture a complete before-image for every member this call will actually change."""

        before_images: list[ProjectionNodeBeforeImage] = []
        for node_type, node_id, source_ref, reason in owned:
            if source_ref in active_refs:
                needs_change = reason == SOURCE_PROJECTION_REMOVED_REASON
            elif reason not in {"", SOURCE_PROJECTION_REMOVED_REASON}:
                # Deletion, cancellation and supersedence are somebody else's provenance;
                # this projection may not overwrite them with its own removal reason.
                needs_change = False
            else:
                if self._node_snapshot(node_type, node_id) is None:
                    raise ProjectionActiveSetReconciliationError(
                        "projection_active_set_snapshot_failed",
                        "A projection member disappeared while its before-image "
                        "was being captured.",
                    )
                needs_change = reason != SOURCE_PROJECTION_REMOVED_REASON or bool(
                    self._projection_incident_edges(node_type, node_id)
                )
            if not needs_change:
                continue
            snapshot = self._projection_node_before_image(node_type, node_id)
            if snapshot is None:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_snapshot_failed",
                    "A projection member disappeared while its complete "
                    "before-image was being captured.",
                )
            before_images.append(snapshot)
        return tuple(before_images)

    def _restore_projection_node(
        self,
        before_image: ProjectionNodeBeforeImage,
    ) -> None:
        """Put one recorded payload back, refusing a node that is no longer there."""

        if not self.replace_node_payload(
            before_image.node_type,
            before_image.node_id,
            dict(before_image.attrs),
            source_session_id=before_image.source_session_id,
        ):
            # False means the node is gone.  It is not a quieter kind of success: the
            # before-image cannot be restored onto a node that is not there.
            raise GraphError(
                _PROJECTION_COMPENSATION_NODE_MISSING,
                details={
                    "backend": "okto_grafx",
                    "board_id": self._board_id,
                    "node_type": before_image.node_type,
                    "code": "projection_active_set_compensation_node_missing",
                },
            )

    def _confirm_projection_restoration(
        self,
        before_image: ProjectionNodeBeforeImage,
    ) -> None:
        """Read the node back and refuse to call it restored unless it matches exactly."""

        current = self._projection_node_before_image(
            before_image.node_type,
            before_image.node_id,
        )
        recorded_edges = Counter(
            self._projection_edge_signature(edge)
            for edge in before_image.incident_edges
        )
        if (
            current is None
            or current.attrs != before_image.attrs
            # attrs excludes the identity columns on purpose, so the session that owns the
            # node is only compared if it is compared here.
            or current.source_session_id != before_image.source_session_id
            or Counter(
                self._projection_edge_signature(edge) for edge in current.incident_edges
            )
            != recorded_edges
        ):
            raise GraphError(
                _PROJECTION_COMPENSATION_UNCONFIRMED,
                details={
                    "backend": "okto_grafx",
                    "board_id": self._board_id,
                    "node_type": before_image.node_type,
                    "code": "projection_active_set_compensation_unconfirmed",
                },
            )

    def compensate_projection_active_set(
        self,
        receipt: ProjectionActiveSetReceipt,
    ) -> None:
        """Restore every projection node and relationship exactly, or discard the scope.

        Exact means the recorded multiset and nothing else, so each node's incident edges are
        cleared before the recorded ones go back: an extra edge that appeared after the
        failure would otherwise survive a restore that reports itself complete.  Once the
        first write lands, a failure here can no longer be reported and walked away from --
        a partially compensated board must not be allowed to commit.
        """

        if not receipt.before_images and not receipt.edge_before_images:
            return
        self._fence("compensate_projection_active_set")
        applied = False
        try:
            for before_image in receipt.before_images:
                # Marked before the call, not after it: a write that raises may still have
                # landed, and a flag set on the return path would never be set for exactly
                # the failure that damages the board.
                applied = True
                self._restore_projection_node(before_image)
                self._projection_delete_incident_edges(
                    before_image.node_type,
                    before_image.node_id,
                )
                self._projection_restore_edges(before_image.incident_edges)
                self._confirm_projection_restoration(before_image)
            if receipt.edge_before_images:
                applied = True
                self._projection_restore_edges(receipt.edge_before_images)
        except BaseException as primary_error:
            if applied:
                cleanup_error = self._abort_after_staged_failure(
                    operation="compensate_projection_active_set",
                )
                if cleanup_error is not None:
                    # Cleanup never displaces the reason compensation failed.
                    raise primary_error from cleanup_error
            raise

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
        *,
        preserved_projection_edges: tuple[ProjectionEdgeBeforeImage, ...] = (),
    ) -> None:
        """Delete session edges while preserving every restored before-image."""

        self._fence("delete_edges_by_session_preserving_spec_lineage")
        projection_by_pair: dict[
            tuple[str, str, str],
            list[tuple[ProjectionEdgeBeforeImage, dict[str, Any]]],
        ] = {}
        projection_by_identity: dict[
            tuple[str, str, str, str, str, str | None],
            list[ProjectionEdgeBeforeImage],
        ] = {}
        configured_pairs = set(self._relationship_pairs)
        for edge in preserved_projection_edges:
            pair = (edge.edge_type, edge.from_type, edge.to_type)
            if pair not in configured_pairs:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_cleanup_preservation_inconsistent",
                    "A restored projection edge has no configured relationship "
                    "mapping for session cleanup.",
                )
            if _contains_non_finite_number(edge.attrs):
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_cleanup_preservation_inconsistent",
                    "A restored projection edge contains a non-finite numeric "
                    "value that cannot be matched safely during session cleanup.",
                )
            _physical, definition = self._relationship_definition(
                edge.edge_type,
                edge.from_type,
                edge.to_type,
            )
            properties = self._projection_edge_properties(definition)
            if set(edge.attrs) != set(properties):
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_cleanup_preservation_inconsistent",
                    "A restored projection edge is not a complete relationship "
                    "before-image.",
                )
            write_attrs = self._coerce_properties(
                definition,
                edge.attrs,
                forbidden=frozenset({"_from", "_to"}),
            )
            normalized = ProjectionEdgeBeforeImage(
                edge_type=edge.edge_type,
                from_type=edge.from_type,
                to_type=edge.to_type,
                from_id=edge.from_id,
                to_id=edge.to_id,
                attrs={
                    name: _normalize_value(write_attrs[name]) for name in properties
                },
            )
            projection_by_pair.setdefault(
                pair,
                [],
            ).append((normalized, write_attrs))
            projection_by_identity.setdefault(
                (
                    edge.edge_type,
                    edge.from_type,
                    edge.to_type,
                    edge.from_id,
                    edge.to_id,
                    self._projection_dependency_rule_id(normalized),
                ),
                [],
            ).append(normalized)

        # Validate the complete preservation set before the first DELETE. Missing
        # snapshots are legitimate after accumulated inverse receipts; an extra
        # stored payload or multiplicity is not, because a predicate cannot choose
        # which byte-identical parallel copy to retain.
        for identity_edges in projection_by_identity.values():
            desired = Counter(
                self._projection_edge_signature(edge) for edge in identity_edges
            )
            actual = self._projection_matching_edges(identity_edges[0])
            if actual - desired:
                raise ProjectionActiveSetReconciliationError(
                    "projection_active_set_cleanup_preservation_inconsistent",
                    "The restored projection edge multiset exceeds the exact "
                    "before-images owned by the compensation receipts.",
                )

        plans: list[tuple[str, str, str, dict[str, Any], str]] = []
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
            predicates: list[str] = []
            if (
                edge_type == "belongs_to"
                and from_type == "Entity"
                and to_type == "Entity"
                and preserved_edges
            ):
                properties = tuple(column.name for column in definition.columns[2:])
                if "rule_id" not in properties:
                    raise GraphCapabilityUnavailable(
                        f"Grafx relationship table {physical!r} lacks lineage identity."
                    )
                prepared: list[tuple[SpecLineageEdgeSnapshot, dict[str, Any]]] = []
                for snapshot in preserved_edges:
                    if _contains_non_finite_number(snapshot.attrs):
                        raise SpecLineageReconciliationError(
                            "spec_lineage_edge_metadata_inconsistent",
                            "A preserved Spec-lineage edge contains a non-finite "
                            "numeric value that cannot be matched safely.",
                        )
                    if set(snapshot.attrs) != set(properties):
                        raise SpecLineageReconciliationError(
                            "spec_lineage_edge_metadata_inconsistent",
                            "A preserved Spec-lineage edge is not a complete "
                            "relationship before-image.",
                        )
                    write_attrs, normalized_attrs = (
                        self._materialize_spec_lineage_attrs(
                            definition,
                            properties,
                            snapshot.attrs,
                        )
                    )
                    normalized = SpecLineageEdgeSnapshot(
                        source_id=snapshot.source_id,
                        target_id=snapshot.target_id,
                        rule_id=snapshot.rule_id,
                        attrs=normalized_attrs,
                    )
                    if str(normalized_attrs.get("rule_id") or "") != snapshot.rule_id:
                        raise SpecLineageReconciliationError(
                            "spec_lineage_edge_metadata_inconsistent",
                            "A preserved Spec-lineage edge disagrees with its rule "
                            "identity.",
                        )
                    prepared.append((normalized, write_attrs))

                desired = Counter(
                    self._spec_lineage_edge_signature(snapshot)
                    for snapshot, _write_attrs in prepared
                )
                actual: Counter[tuple[tuple[str, str, str], ...]] = Counter()
                for source_id in dict.fromkeys(
                    snapshot.source_id for snapshot, _write_attrs in prepared
                ):
                    actual.update(
                        self._spec_lineage_edge_signature(edge)
                        for edge in self._read_spec_lineage_edges(
                            source_id,
                            physical=physical,
                            properties=properties,
                        )
                    )
                # Core accumulates preservation candidates across compensation
                # records. A later inverse may legitimately remove an
                # intermediate snapshot again, so absence is not corruption.
                # More matching copies than the receipts own is ambiguous,
                # because one predicate cannot preserve an exact multiplicity.
                if any(
                    actual[signature] > count for signature, count in desired.items()
                ):
                    raise SpecLineageReconciliationError(
                        "spec_lineage_edge_metadata_inconsistent",
                        "The restored Spec-lineage multiset exceeds the exact "
                        "multiplicity owned by the compensation receipts.",
                    )

                for index, (snapshot, write_attrs) in enumerate(prepared):
                    params[f"source_{index}"] = snapshot.source_id
                    params[f"target_{index}"] = snapshot.target_id
                    clauses = [
                        f"a.id = $source_{index}",
                        f"b.id = $target_{index}",
                    ]
                    for property_index, name in enumerate(properties):
                        if snapshot.attrs[name] is None:
                            clauses.append(f"r.{name} IS NULL")
                            continue
                        parameter = f"preserve_{index}_{property_index}"
                        params[parameter] = write_attrs[name]
                        clauses.append(f"r.{name} = ${parameter}")
                    predicates.append("(" + " AND ".join(clauses) + ")")

            for index, (edge, write_attrs) in enumerate(
                projection_by_pair.get((edge_type, from_type, to_type), ())
            ):
                params[f"projection_source_{index}"] = edge.from_id
                params[f"projection_target_{index}"] = edge.to_id
                clauses = [
                    f"a.id = $projection_source_{index}",
                    f"b.id = $projection_target_{index}",
                ]
                properties = self._projection_edge_properties(definition)
                for property_index, name in enumerate(properties):
                    if edge.attrs[name] is None:
                        clauses.append(f"r.{name} IS NULL")
                        continue
                    parameter = f"projection_{index}_{property_index}"
                    params[parameter] = write_attrs[name]
                    clauses.append(f"r.{name} = ${parameter}")
                predicates.append("(" + " AND ".join(clauses) + ")")

            preservation = (
                " AND NOT (" + " OR ".join(predicates) + ")" if predicates else ""
            )
            plans.append((physical, from_type, to_type, params, preservation))

        mutation_started = False
        try:
            for physical, from_type, to_type, params, preservation in plans:
                mutation_started = True
                self._mutation(
                    f"MATCH (a:{from_type})-[r:{physical}]->(b:{to_type}) "
                    "WHERE r.created_by_session_id = $session_id"
                    f"{preservation} DELETE r",
                    params,
                    operation="delete_edges_by_session_preserving_spec_lineage",
                )
        except BaseException as primary_error:
            if not mutation_started:
                raise
            cleanup_error = self._abort_after_staged_failure(
                operation="delete_edges_by_session_preserving_spec_lineage"
            )
            if cleanup_error is not None:
                try:
                    primary_error.add_note(
                        "Grafx rollback also failed while discarding an incomplete "
                        "session-edge cleanup."
                    )
                except BaseException:  # noqa: BLE001, S110 - diagnostic only
                    pass
                raise primary_error from cleanup_error
            raise

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
            self._settle_quietly(exc)
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
        # The commit is durable at this point. A release that fails afterwards
        # is a resource problem, not a commit problem, and must not reach the
        # caller as an ambiguous write: it would invite a retry of something
        # already applied.
        self._settle_after_durable_commit()

    async def rollback(self) -> None:
        if self._finished:
            return
        try:
            self._transaction.rollback()
        except Exception as exc:
            self._finished = not self._transaction.active
            self._settle_quietly(exc)
            mapped = map_grafx_error(exc, operation="rollback")
            if mapped is exc:
                raise
            raise mapped from exc
        self._finished = True
        self._settle()

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
        relationship_table_resolver: RelationshipTableResolver | None = None,
        on_scope_end: Callable[[str], ScopeTerminalCallback | None] | None = None,
    ) -> None:
        self._database_resolver = database_resolver
        self._revalidate_fence = revalidate_fence
        # Given a board, return the callback that scope should fire when it
        # ends -- typically releasing the lease its database came from.
        self._on_scope_end = on_scope_end
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
        if relationship_table_resolver is not None:
            self._relationship_table_resolver = relationship_table_resolver
        elif relationship_pairs is None:
            # Production composition uses the complete Pulse authority and its
            # deterministic one-table-per-pair layout.  An explicitly narrowed
            # custom/test authority keeps the historical logical table name
            # unless its caller also injects a resolver.
            self._relationship_table_resolver = resolve_relationship_table
        else:
            self._relationship_table_resolver = _default_relationship_table

    async def begin(self, board_id: str) -> _GrafxTransactionScope:
        if type(board_id) is not str or not board_id:
            raise ValueError("board_id must be non-empty text")
        self._revalidate_fence(board_id, "begin")
        try:
            resolved = self._database_resolver(board_id)
        except Exception as exc:
            mapped = map_grafx_error(exc, operation="begin")
            if mapped is exc:
                raise
            raise mapped from exc
        database, release = _resolved_database(resolved)
        transaction = None
        try:
            transaction = database.begin("write")
        except Exception as exc:
            # The pin was taken before this ran, so it is this path's job to
            # give it back -- but only if the engine left no transaction behind.
            # A transaction that somehow survived still owns the handle.
            if release is not None and (
                transaction is None or not getattr(transaction, "active", False)
            ):
                _release_quietly(release, exc)
            mapped = map_grafx_error(exc, operation="begin")
            if mapped is exc:
                raise
            raise mapped from exc
        on_terminal = release
        if self._on_scope_end is not None:
            try:
                supplied = self._on_scope_end(board_id)
            except Exception as exc:
                # The scope would otherwise start with no way to release what
                # begin already took, so this fails before the scope exists.
                try:
                    transaction.rollback()
                except Exception as cleanup:  # noqa: BLE001 - attached below
                    # The factory failure is the report; a transaction that also
                    # refuses to roll back rides along rather than vanishing.
                    exc.add_note(
                        f"rolling back the unopened Grafx scope also failed: {cleanup}"
                    )
                # The pin goes back only if the transaction is genuinely over.
                # A rollback that failed can leave it OPEN, and releasing then
                # would hand the handle to the pool while writes can still reach
                # it -- the exact use-after-close this pinning exists to stop.
                # Holding the pin leaks one entry; releasing it corrupts a
                # database, so the leak is the better failure and is reported.
                if release is not None:
                    if getattr(transaction, "active", False):
                        exc.add_note(
                            "the Grafx transaction is still active, so its database "
                            "pin was retained rather than released"
                        )
                    else:
                        _release_quietly(release, exc)
                mapped = map_grafx_error(exc, operation="begin")
                if mapped is exc:
                    raise
                raise mapped from exc
            if supplied is not None:
                on_terminal = _chain_releases(release, supplied)
        return _GrafxTransactionScope(
            board_id,
            database,
            transaction,
            self._revalidate_fence,
            node_types=self._node_types,
            relationship_pairs=self._relationship_pairs,
            relationship_table_resolver=self._relationship_table_resolver,
            on_terminal=on_terminal,
        )


__all__ = ["CommunityGrafxGraphTransaction"]
