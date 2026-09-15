"""Native history and projection observations, translated at the Community boundary."""

from collections.abc import Mapping
from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
from functools import wraps
import math
from time import monotonic

from okto_grafx import CommitId, TemporalLimits, Timestamp
from okto_grafx.errors import GrafxQueryDeadlineExceeded, GrafxQueryBudgetExceeded
from okto_grafx.projections import (
    project_graph,
    ProjectionLimits,
    GraphProjection,
    ProjectionNode,
    ProjectionEdge,
)
from okto_pulse.core.kg import cypher_templates as tpl
from okto_pulse.core.kg.schema_contract import NODE_TYPES
from okto_pulse.core.domain.code_traceability_kg import CODE_TRACEABILITY_KG_SUBTYPES
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_relationship_layout import (
    PULSE_RELATIONSHIP_LAYOUT,
)

_VISIBLE_PAYLOAD_BYTES = 16 * 1024 * 1024


def mapped(operation):
    @wraps(operation)
    def invoke(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Exception as exc:
            error = map_grafx_error(exc, operation=operation.__name__)
            if error is exc:
                raise
            raise error from exc

    return invoke


def scope(node_types, relationship_types):
    if (
        type(node_types) is not tuple
        or not node_types
        or len(node_types) > len(NODE_TYPES)
        or len(set(node_types)) != len(node_types)
        or any(x not in NODE_TYPES for x in node_types)
    ):
        raise ValueError("select_distinct_supported_node_types")
    entries = PULSE_RELATIONSHIP_LAYOUT.entries
    allowed = {entry.logical_type for entry in entries}
    if (
        type(relationship_types) is not tuple
        or len(relationship_types) > len(allowed)
        or len(set(relationship_types)) != len(relationship_types)
        or any(x not in allowed for x in relationship_types)
    ):
        raise ValueError("select_distinct_supported_relationship_types")
    selected = tuple(
        entry
        for entry in entries
        if entry.logical_type in relationship_types
        and entry.from_type in node_types
        and entry.to_type in node_types
    )
    if set(relationship_types) - {entry.logical_type for entry in selected}:
        raise ValueError("relationship_has_no_pair_in_selected_node_scope")
    tables = tuple(("node", x) for x in node_types) + tuple(
        ("rel", x.physical_table) for x in selected
    )
    return tables, selected


def bounds(value, maximum, name):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"invalid_{name}")
    return value


def audit_reason(reason):
    if type(reason) is not str or not reason.strip() or len(reason) > 1024:
        raise ValueError("a_bounded_audit_reason_is_required")


def json_value(value):
    if isinstance(value, Timestamp):
        return (
            datetime(1970, 1, 1, tzinfo=timezone.utc)
            + timedelta(microseconds=value.micros)
        ).isoformat()
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, Mapping):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    raise ValueError("unsupported_observation_value")


class CommunityGrafxHistory:
    def __init__(
        self, database_resolver, revalidate_fence, *, read_database_scope=None
    ):
        self._resolve, self._fence = database_resolver, revalidate_fence
        self._read = read_database_scope or (
            lambda board: nullcontext(database_resolver(board))
        )

    @mapped
    def activate(self, board_id, node_types, relationship_types, *, reason):
        tables, _ = scope(node_types, relationship_types)
        audit_reason(reason)
        db = self._resolve(board_id)
        # Missing schema is knowable before a one-way phase is applied.
        for kind, table in tables:
            db.catalog.catalog.table(table, kind=kind)
        # Each native phase is independently durable and idempotent. Do not claim
        # this is a single cross-capability activation transaction.
        for name, operation in (
            ("identity", db.ensure_identity_indexes),
            ("commits", db.enable_commit_history),
            ("system_time", lambda: db.enable_system_history(tables)),
        ):
            self._fence(board_id, f"history_activate_{name}")
            operation()
            self._fence(board_id, f"history_activate_{name}_complete")
        return {
            "enabled": True,
            "node_types": list(node_types),
            "relationship_types": list(relationship_types),
            "reason": reason,
            "prior_history_available": False,
            "one_way": True,
        }

    @mapped
    def commits(self, board_id, *, after=None, limit=100):
        bounds(limit, 1000, "limit")
        cursor = None if after is None else CommitId.parse(after)
        with self._read(board_id) as db, db.begin("read") as reader:
            page = reader.commit_history(after=cursor, limit=limit)
            return {
                "entries": [
                    {
                        "commit": entry.identity.to_token(),
                        "kind": entry.kind.name.lower(),
                        "observed_at": json_value(entry.timing.observed_at),
                        "ordered_at": json_value(entry.timing.ordered_at),
                        "clock_adjusted": entry.timing.clock_adjusted,
                        "metadata": None
                        if entry.metadata is None
                        else {
                            **{
                                key: getattr(entry.metadata, key)
                                for key in (
                                    "actor",
                                    "origin",
                                    "reason",
                                    "correlation_id",
                                )
                            },
                            "attributes": json_value(entry.metadata.attributes),
                        },
                    }
                    for entry in page.entries
                ],
                "snapshot": CommitId(page.database_uuid, page.read_sequence).to_token(),
                "tracked_after": CommitId(
                    page.database_uuid, page.activation_sequence
                ).to_token(),
                "has_more": page.has_more,
                "next_after": page.entries[-1].identity.to_token()
                if page.has_more and page.entries
                else None,
            }

    @staticmethod
    def _limits(max_rows, max_bytes):
        return TemporalLimits(
            max_rows=bounds(max_rows, 10000, "max_rows"),
            max_bytes=bounds(max_bytes, 64 * 1024 * 1024, "max_bytes"),
            max_events=100000,
        )

    @staticmethod
    def _picture(picture, entries):
        schemas = {schema.table_id: schema for schema in picture.schemas}
        logical = {entry.physical_table: entry for entry in entries}
        nodes, edges, identities = [], [], {}
        for row in picture.rows:
            schema = schemas[row.table_id]
            values = dict(
                zip((column.name for column in schema.columns), row.values, strict=True)
            )
            # Embeddings are not useful audit display payloads; explicitly omitted.
            props = {
                key: json_value(value)
                for key, value in values.items()
                if key not in ("embedding", "_from", "_to")
            }
            item = {
                "type": schema.name,
                "lineage": f"{row.table_id}:{row.record_id}",
                "properties": props,
                "since": row.system_from.to_token(),
            }
            if schema.kind == "node":
                item["id"] = values["id"]
                identities[(schema.name, row.record_id)] = {
                    "type": schema.name,
                    "id": values["id"],
                }
                nodes.append(item)
            else:
                entry = logical[schema.name]
                item["type"] = entry.logical_type
                edges.append((item, entry, values["_from"], values["_to"]))
        return {
            "at": picture.as_of.to_token(),
            "nodes": nodes,
            "edges": [
                dict(
                    item,
                    source=identities[(entry.from_type, source)],
                    target=identities[(entry.to_type, target)],
                )
                for item, entry, source, target in edges
            ],
            "omitted_properties": ["embedding"],
            "complete": True,
        }

    @mapped
    def as_of(
        self,
        board_id,
        at,
        node_types,
        relationship_types,
        *,
        max_rows=1000,
        max_bytes=16777216,
    ):
        tables, entries = scope(node_types, relationship_types)
        token, limits = CommitId.parse(at), self._limits(max_rows, max_bytes)
        with self._read(board_id) as db, db.begin("read") as reader:
            return self._picture(
                reader.system_as_of(token, tables=tables, limits=limits), entries
            )

    @mapped
    def diff(
        self,
        board_id,
        before,
        after,
        node_types,
        relationship_types,
        *,
        max_rows=1000,
        max_bytes=16777216,
    ):
        tables, entries = scope(node_types, relationship_types)
        start, end = CommitId.parse(before), CommitId.parse(after)
        limits = self._limits(max_rows, max_bytes)
        logical = {entry.physical_table: entry.logical_type for entry in entries}
        with self._read(board_id) as db, db.begin("read") as reader:
            result = reader.system_diff(
                start, end, tables=tables, limits=limits, max_changes=max_rows
            )
            # Resolve historical business identities/endpoints, not current rows.
            # These two additional bounded pictures share the same reader.
            pictures = [
                self._picture(
                    reader.system_as_of(token, tables=tables, limits=limits), entries
                )
                for token in (start, end)
            ]
            identities = [
                {row["lineage"]: row for row in picture["nodes"] + picture["edges"]}
                for picture in pictures
            ]
            return {
                "before": before,
                "after": after,
                "complete": True,
                "omitted_properties": ["embedding"],
                "schema_changes": [
                    {
                        "type": logical.get(change.table, change.table),
                        "before": None
                        if change.before is None
                        else [c.name for c in change.before.columns],
                        "after": None
                        if change.after is None
                        else [c.name for c in change.after.columns],
                    }
                    for change in result.schemas
                ],
                "changes": [
                    {
                        "type": logical.get(change.table, change.table),
                        "kind": change.table_kind,
                        "lineage": f"{(change.after or change.before).table_id}:{change.record_id}",
                        "before_entity": identities[0].get(
                            f"{(change.after or change.before).table_id}:{change.record_id}"
                        ),
                        "after_entity": identities[1].get(
                            f"{(change.after or change.before).table_id}:{change.record_id}"
                        ),
                        "operation": change.operation,
                        "properties": [
                            {
                                "name": prop.name,
                                "before_present": prop.before_present,
                                "after_present": prop.after_present,
                                "before": json_value(prop.before),
                                "after": json_value(prop.after),
                            }
                            for prop in change.properties
                            if prop.name not in ("embedding", "_from", "_to")
                        ],
                        "labels_added": list(change.labels_added),
                        "labels_removed": list(change.labels_removed),
                    }
                    for change in result.rows
                ],
            }

    @mapped
    def prune(
        self,
        board_id,
        before,
        node_types,
        relationship_types,
        *,
        reason,
        max_bytes=16777216,
    ):
        tables, _ = scope(node_types, relationship_types)
        token = CommitId.parse(before)
        bounds(max_bytes, 64 * 1024 * 1024, "max_bytes")
        audit_reason(reason)
        self._fence(board_id, "history_prune")
        result = self._resolve(board_id).prune_system_history(
            token, tables=tables, max_bytes=max_bytes
        )
        self._fence(board_id, "history_prune_complete")
        return {
            "retained_from": before,
            "commit": None if result.commit is None else result.commit.to_token(),
            "redacted_versions": result.redacted_versions,
            "redacted_bytes": result.redacted_bytes,
            "physical_bytes_reclaimed": result.physical_bytes_reclaimed,
            "reason": reason,
        }


class CommunityGrafxAnalytics:
    def __init__(self, read_database_scope):
        self._read = read_database_scope

    @mapped
    def analyze(
        self,
        board_id,
        *,
        node_types,
        relationship_types,
        algorithm,
        graph_layer="canonical",
        include_code_traceability=False,
        source_type=None,
        source_id=None,
        direction="out",
        max_depth=10,
        max_nodes=1000,
        max_edges=10000,
        timeout_seconds=10.0,
    ):
        _, entries = scope(node_types, relationship_types)
        bounds(max_nodes, 10000, "max_nodes")
        bounds(max_edges, 100000, "max_edges")
        if type(max_depth) is not int or not 0 <= max_depth <= 100:
            raise ValueError("invalid_max_depth")
        if (
            algorithm not in ("components", "cycles", "dependency_impact")
            or graph_layer not in ("all", "canonical", "working")
            or direction not in ("in", "out", "both")
            or type(include_code_traceability) is not bool
        ):
            raise ValueError("invalid_analytics_options")
        if (
            type(timeout_seconds) not in (float, int)
            or not math.isfinite(timeout_seconds)
            or not 0.001 <= timeout_seconds <= 30
        ):
            raise ValueError("invalid_timeout_seconds")
        if algorithm == "dependency_impact" and (
            source_type not in node_types or type(source_id) is not str or not source_id
        ):
            raise ValueError("dependency_impact_requires_a_qualified_source")
        deadline = monotonic() + timeout_seconds

        def remaining():
            value = deadline - monotonic()
            if value <= 0:
                raise GrafxQueryDeadlineExceeded("Graph analytics deadline exceeded.")
            return value

        limits = ProjectionLimits(
            max_nodes=max_nodes,
            max_edges=max_edges,
            max_memory_bytes=64 * 1024 * 1024,
            max_work=2000000,
        )
        with self._read(board_id) as db, db.begin("read") as reader:
            captured = project_graph(
                db,
                reader,
                node_tables=node_types,
                relationship_tables=tuple(entry.physical_table for entry in entries),
                limits=limits,
                timeout_seconds=remaining(),
            )
            visible, scanned = {}, 0
            retained_bytes = 0
            for table in node_types:
                cursor = None
                while True:
                    page = reader.scan_rows_v1(
                        table,
                        kind="node",
                        columns=(
                            "id",
                            "title",
                            "graph_layer",
                            "kind_of",
                            "revocation_reason",
                            "superseded_by",
                        ),
                        limit=256,
                        cursor=cursor,
                        max_batch_bytes=4 * 1024 * 1024,
                        timeout_seconds=remaining(),
                    )
                    for row in page.rows:
                        scanned += 1
                        if scanned > max_nodes:
                            raise GrafxQueryBudgetExceeded(
                                "Analytics node bound exceeded."
                            )
                        key, title, layer, subtype, revocation, superseded = row.values
                        if (
                            superseded
                            or revocation in tpl.ACTIVE_READ_TOMBSTONE_REASONS
                        ):
                            continue
                        if graph_layer != "all" and layer != graph_layer:
                            continue
                        if (
                            not include_code_traceability
                            and subtype in CODE_TRACEABILITY_KG_SUBTYPES
                        ):
                            continue
                        retained_bytes += 256 + sum(
                            4 * len(value) if isinstance(value, str) else 32
                            for value in (table, key, title)
                        )
                        if retained_bytes > _VISIBLE_PAYLOAD_BYTES:
                            raise GrafxQueryBudgetExceeded(
                                "Analytics visible payload bound exceeded."
                            )
                        visible[ProjectionNode(table, row.record_id)] = {
                            "type": table,
                            "id": key,
                            "title": title,
                        }
                    cursor = page.next_cursor
                    if cursor is None:
                        break
            # Build an induced picture: hidden nodes cannot remain as bridges.
            visible_edges, edge_rows = set(), 0
            for entry in entries:
                cursor = None
                definition = db.catalog.catalog.table(entry.physical_table, kind="rel")
                layered = any(
                    column.name == "graph_layer" for column in definition.columns
                )
                while True:
                    page = reader.scan_rows_v1(
                        entry.physical_table,
                        kind="rel",
                        columns=("graph_layer",) if layered else ("_from",),
                        limit=256,
                        cursor=cursor,
                        max_batch_bytes=4 * 1024 * 1024,
                        timeout_seconds=remaining(),
                    )
                    for row in page.rows:
                        edge_rows += 1
                        if edge_rows > max_edges:
                            raise GrafxQueryBudgetExceeded(
                                "Analytics edge bound exceeded."
                            )
                        if (
                            not layered
                            or graph_layer == "all"
                            or row.values[0] == graph_layer
                        ):
                            visible_edges.add((entry.physical_table, row.record_id))
                    cursor = page.next_cursor
                    if cursor is None:
                        break
            indices = {
                old: new
                for new, old in enumerate(
                    i for i, node in enumerate(captured.nodes) if node in visible
                )
            }
            nodes = tuple(captured.nodes[i] for i in indices)
            edges = tuple(
                ProjectionEdge(
                    edge.table,
                    edge.record_id,
                    indices[edge.source],
                    indices[edge.target],
                )
                for edge in captured.edges
                if edge.source in indices
                and edge.target in indices
                and (edge.table, edge.record_id) in visible_edges
            )
            graph = GraphProjection(
                captured.database_uuid,
                captured.snapshot_lsn,
                nodes,
                edges,
                limits,
                captured.logical_bytes,
            )
            remaining()
            result = {
                "algorithm": algorithm,
                "complete": True,
                "scope": {
                    "node_types": list(node_types),
                    "relationship_types": list(relationship_types),
                    "graph_layer": graph_layer,
                },
                "nodes": len(nodes),
                "edges": len(edges),
                "snapshot": str(captured.snapshot_lsn),
            }
            if algorithm == "components":
                labels = graph.weakly_connected_components()
                groups = {}
                for node, label in zip(nodes, labels, strict=True):
                    groups.setdefault(label, []).append(visible[node])
                result["components"] = list(groups.values())
            elif algorithm == "cycles":
                order = graph.topological_order()
                labels = graph.strongly_connected_components()
                groups = {}
                for node, label in zip(nodes, labels, strict=True):
                    groups.setdefault(label, []).append(node)
                loops = {
                    nodes[edge.source] for edge in edges if edge.source == edge.target
                }
                result.update(
                    acyclic=order.acyclic,
                    order=[visible[node] for node in order.order],
                    blocked=[visible[node] for node in order.blocked],
                    cyclic_components=[
                        [visible[node] for node in group]
                        for group in groups.values()
                        if len(group) > 1 or group[0] in loops
                    ],
                )
            else:
                source = next(
                    (
                        node
                        for node in nodes
                        if visible[node]["type"] == source_type
                        and visible[node]["id"] == source_id
                    ),
                    None,
                )
                if source is None:
                    raise ValueError("source_not_visible_in_analytics_scope")
                result["reachable"] = [
                    visible[node]
                    for node in graph.reachable(
                        source,
                        direction=direction,
                        max_depth=max_depth,
                        max_results=max_nodes,
                    )
                ]
            remaining()
            return result
