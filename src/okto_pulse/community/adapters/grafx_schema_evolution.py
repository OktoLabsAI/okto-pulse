"""Out-of-place logical rebuild of a Pulse 0.3.12 Grafx board into 0.5.0.

`EVOLUTION_PLAN_CODEX.md` sections 9.3.6 and 9.5 forbid raising a physical
format in place: a format change is an export/import or a rebuild into a new
generation, and the previous schema needs a logical migrator plus an n-1/n
fixture. This adapter is that migrator. It reads one fixed snapshot of a
caller-owned source and writes a separate, fresh candidate generation carrying
the M-PULSE-3C manifest. It never mutates the source, never advances
`CATALOG_FORMAT_VERSION`, and never binds or activates the candidate.

The predecessor projection is DERIVED from the published manifest descriptor
rather than transcribed, and the derivation is checked against the frozen
fingerprint at import. A manifest edit therefore fails loudly here instead of
silently comparing against a dead constant.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

import okto_grafx
from okto_grafx import Database, Timestamp, VectorValue
from okto_pulse.core.kg.interfaces.graph_errors import (
    GraphCapabilityUnavailable,
    GraphError,
    GraphLockContention,
)

from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error
from okto_pulse.community.adapters.grafx_schema_manifest import (
    EMBEDDING_DIMENSION,
    EMBEDDING_STORAGE_DTYPE,
    PULSE_GRAFX_SCHEMA_MANIFEST,
    GrafxTableManifest,
)

_OPERATION = "rebuild_grafx_schema_candidate"

SOURCE_SCHEMA_VERSION = "0.3.12"
TARGET_SCHEMA_VERSION = PULSE_GRAFX_SCHEMA_MANIFEST.schema_version
BUILD_MARKER = "building:0.3.12->0.5.0"
MEMORY_PATH = ":memory:"

SOURCE_SCHEMA_FINGERPRINT = (
    "f4f9905b1012b98df6669117c0ab8feb926f763d7d4c26caf91cc0f138354717"
)
TARGET_SCHEMA_FINGERPRINT = PULSE_GRAFX_SCHEMA_MANIFEST.logical_fingerprint

HASH_DOMAIN = b"pulse-grafx-schema-rebuild/1\x00"

MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 1024


class _StampOutcomeAmbiguous(RuntimeError):
    """Signal that the terminal commit crossed a point whose outcome needs recovery."""


# A candidate handle that did not prove a complete close still owns resources which may
# touch the database.  Releasing its path lock would advertise an authority transfer we
# have not proved.  Retaining the FileLock object is the bounded fail-closed outcome; the
# operating system releases it when this process terminates.
_RETAINED_CANDIDATE_LOCKS: list[FileLock] = []

INTRODUCED_NODE_PROPERTIES: tuple[str, ...] = (
    "investigation_receipt_id",
    "source_ref",
    "attestor_actor_id",
    "declared_revision",
    "workspace_state_id",
    "code_path",
    "symbol_qualified_name",
    "symbol_kind",
    "selector_kind",
    "selector_fingerprint",
    "resolution_state",
)

_INTRODUCED_LOGICAL_RELATIONSHIPS = frozenset({"precedes", "supports", "overlaps"})
_INTRODUCED_DERIVES_FROM_PAIR = ("Entity", "Entity")


def _divergence(
    reason: str, *, phase: str = "rebuild", **details: object
) -> GraphCapabilityUnavailable:
    """Return the one typed refusal this adapter raises, with bounded details."""
    return GraphCapabilityUnavailable(
        "The Grafx schema rebuild refused the source or candidate state.",
        details={
            "backend": "okto_grafx",
            "operation": _OPERATION,
            "phase": phase,
            "reason": reason,
            **details,
        },
    )


def _invalid_argument(field: str, value: object) -> GraphCapabilityUnavailable:
    """Refuse one argument without echoing a caller value into the details."""
    return GraphCapabilityUnavailable(
        "The Grafx schema rebuild received an invalid argument.",
        details={
            "backend": "okto_grafx",
            "operation": _OPERATION,
            "phase": "arguments",
            "reason": "invalid_rebuild_argument",
            "field": field,
            "value_type": type(value).__name__,
        },
    )


def _mapped(failure: BaseException, phase: str, **extra: object) -> BaseException:
    """Return the Core-taxonomy error one backend or filesystem failure becomes."""
    # The operation stays the frozen name so failures group; phase and a bounded reason
    # are separate keys rather than a composite string nobody can filter on.
    mapped = map_grafx_error(failure, operation=_OPERATION)
    details = getattr(mapped, "details", None)
    if isinstance(details, dict):
        details.setdefault("phase", phase)
        details.setdefault("reason", f"backend_failure_{phase}")
        for key, value in extra.items():
            details.setdefault(key, value)
    return mapped


def _backend_call(action: Callable[[], Any], phase: str, **details: object) -> Any:
    """Run one backend call and keep every escaping failure inside Core taxonomy."""
    try:
        return action()
    except BaseException as failure:
        mapped = _mapped(failure, phase, **details)
        if mapped is failure:
            raise
        raise mapped from failure


def _execute(
    handle: Any,
    text: str,
    parameters: dict[str, Any] | None = None,
    *,
    phase: str,
    table: str | None = None,
) -> Any:
    """Execute one statement with its precise, bounded boundary context."""
    details: dict[str, object] = {}
    if table is not None:
        details["table"] = table
    return _backend_call(
        lambda: handle.execute(text, parameters),
        phase,
        **details,
    )


def _bounded_identifier(value: object) -> str:
    """Return non-secret schema evidence without allowing an unbounded detail value."""
    if type(value) is not str:
        return f"<{type(value).__name__}>"
    if len(value) <= 128:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"<identifier:length={len(value)}:sha256={digest}>"


def _version_evidence(value: str) -> dict[str, object]:
    """Describe an untrusted stored version without echoing its content."""
    encoded = value.encode("utf-8")
    return {
        "observed_version_length": len(value),
        "observed_version_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _canonical_json(value: object) -> bytes:
    """Serialize one value the single way this unit hashes anything."""
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


# --- the predecessor projection, derived rather than transcribed ---------------------------------


def _introduced_relationship(table: GrafxTableManifest) -> bool:
    """Answer whether one physical relationship table arrives with 0.5.0."""
    logical = table.logical_relationship
    if logical in _INTRODUCED_LOGICAL_RELATIONSHIPS:
        return True
    return (
        logical == "derives_from"
        and (
            table.from_table,
            table.to_table,
        )
        == _INTRODUCED_DERIVES_FROM_PAIR
    )


def _predecessor_descriptor() -> dict[str, Any]:
    """Return the 0.3.12 descriptor as the spec defines it: the target minus the delta."""
    descriptor = json.loads(PULSE_GRAFX_SCHEMA_MANIFEST.logical_descriptor_json)
    descriptor["schema_version"] = SOURCE_SCHEMA_VERSION
    for node in descriptor["nodes"]:
        node["columns"] = [
            column
            for column in node["columns"]
            if column["name"] not in INTRODUCED_NODE_PROPERTIES
        ]
    kept: list[dict[str, Any]] = []
    for relationship in descriptor["relationships"]:
        if relationship["name"] in _INTRODUCED_LOGICAL_RELATIONSHIPS:
            continue
        if relationship["name"] == "derives_from":
            relationship = dict(relationship)
            relationship["endpoint_pairs"] = [
                pair
                for pair in relationship["endpoint_pairs"]
                if tuple(pair) != _INTRODUCED_DERIVES_FROM_PAIR
            ]
        kept.append(relationship)
    descriptor["relationships"] = kept
    return descriptor


_DERIVED_SOURCE_FINGERPRINT = hashlib.sha256(
    _canonical_json(_predecessor_descriptor())
).hexdigest()

if _DERIVED_SOURCE_FINGERPRINT != SOURCE_SCHEMA_FINGERPRINT:  # pragma: no cover - guard
    raise AssertionError(
        "The predecessor projection derived from the published manifest no longer "
        f"matches the frozen fingerprint: derived {_DERIVED_SOURCE_FINGERPRINT}, "
        f"frozen {SOURCE_SCHEMA_FINGERPRINT}."
    )

PREDECESSOR_NODE_TABLES: tuple[GrafxTableManifest, ...] = tuple(
    GrafxTableManifest(
        name=table.name,
        kind=table.kind,
        columns=tuple(
            column
            for column in table.columns
            if column.name not in INTRODUCED_NODE_PROPERTIES
        ),
        primary_key=table.primary_key,
    )
    for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes
)

PREDECESSOR_RELATIONSHIP_TABLES: tuple[GrafxTableManifest, ...] = tuple(
    table
    for table in PULSE_GRAFX_SCHEMA_MANIFEST.relationships
    if not _introduced_relationship(table)
)

INTRODUCED_RELATIONSHIP_TABLES: tuple[str, ...] = tuple(
    table.name
    for table in PULSE_GRAFX_SCHEMA_MANIFEST.relationships
    if _introduced_relationship(table)
)


@dataclass(frozen=True, slots=True)
class GrafxSchemaCandidateResult:
    """What one completed rebuild proved, in the frozen field order of the spec."""

    source_schema_version: str
    target_schema_version: str
    source_schema_fingerprint: str
    target_schema_fingerprint: str
    source_snapshot_lsn: int
    logical_data_fingerprint: str
    node_row_counts: tuple[tuple[str, int], ...]
    relationship_row_counts: tuple[tuple[str, int], ...]
    candidate_database_uuid: bytes
    changed: bool


@dataclass(frozen=True, slots=True)
class _BoardMeta:
    board_id: str
    schema_version: str
    bootstrapped_at: Timestamp
    embedding_model: str | None
    embedding_dimension: int | None


@dataclass(frozen=True, slots=True)
class _NodePlan:
    table: str
    columns: tuple[str, ...]
    values: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _RelationshipPlan:
    table: GrafxTableManifest
    from_key: Any
    to_key: Any
    properties: tuple[tuple[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """One fixed logical reading: its records, its counts and its digest."""

    meta: _BoardMeta
    node_plans: tuple[_NodePlan, ...]
    relationship_plans: tuple[_RelationshipPlan, ...]
    node_counts: tuple[tuple[str, int], ...]
    relationship_counts: tuple[tuple[str, int], ...]
    fingerprint: str


# --- the M3/v1 value codec ------------------------------------------------------------------------


def _finite(
    value: float, reason: str, *, phase: str = "value_codec", **details: object
) -> float:
    """Return one double after refusing the values the codec cannot represent."""
    if value != value or value in (float("inf"), float("-inf")):
        raise _divergence(reason, phase=phase, **details)
    return value


def _encode_value(value: object, *, space_name: str | None = None) -> list[Any]:
    """Encode one logical value under the frozen M3/v1 rules."""
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int64", str(value)]
    if type(value) is float:
        return ["float64", _finite(value, "non_finite_double").hex()]
    if type(value) is str:
        return ["string", value]
    if type(value) is Timestamp:
        return ["timestamp_us", str(value.micros)]
    if type(value) is VectorValue:
        return [
            "vector",
            space_name or "",
            value.dtype,
            [
                _finite(component, "non_finite_vector_component").hex()
                for component in value.values
            ],
        ]
    raise _divergence(
        "unencodable_value_type",
        phase="value_codec",
        value_type=type(value).__name__,
    )


def _meta_record(meta: _BoardMeta) -> bytes:
    """Encode the one metadata record, always normalized to the target version."""
    return _canonical_json(
        [
            "meta",
            _encode_value(meta.board_id),
            _encode_value(TARGET_SCHEMA_VERSION),
            _encode_value(meta.bootstrapped_at),
            _encode_value(meta.embedding_model),
            _encode_value(meta.embedding_dimension),
        ]
    )


def _node_record(plan: _NodePlan, space: str) -> bytes:
    """Encode one node row with every target column in manifest order."""
    return _canonical_json(
        [
            "node",
            plan.table,
            [
                [name, _encode_value(value, space_name=space)]
                for name, value in zip(plan.columns, plan.values, strict=True)
            ],
        ]
    )


def _relationship_record(plan: _RelationshipPlan) -> bytes:
    """Encode one relationship occurrence keyed by its logical triple."""
    return _canonical_json(
        [
            "rel",
            plan.table.logical_relationship,
            plan.table.from_table,
            plan.table.to_table,
            _encode_value(plan.from_key),
            _encode_value(plan.to_key),
            [[name, _encode_value(value)] for name, value in plan.properties],
        ]
    )


def _digest(records: list[bytes]) -> str:
    """Hash a length-delimited canonical record stream inside the frozen domain."""
    hasher = hashlib.sha256()
    hasher.update(HASH_DOMAIN)
    for encoded in records:
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
    return hasher.hexdigest()


def _canonical_stream(
    meta: _BoardMeta,
    node_plans: tuple[_NodePlan, ...],
    relationship_plans: tuple[_RelationshipPlan, ...],
) -> list[bytes]:
    """Order the whole stream by encoded bytes, never by the engine's raw ORDER BY.

    Node records sort by their encoded primary key inside each manifest table; relationship
    records sort lexicographically by their complete bytes inside each manifest endpoint
    entry. Equal records stay repeated, which is what preserves parallel edges.
    """
    records: list[bytes] = [_meta_record(meta)]
    by_node: dict[str, list[tuple[bytes, bytes]]] = {}
    for plan in node_plans:
        space = _space_of(plan.table)
        key = _canonical_json(_encode_value(plan.values[0]))
        by_node.setdefault(plan.table, []).append((key, _node_record(plan, space)))
    for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes:
        for _key, encoded in sorted(
            by_node.get(table.name, []), key=lambda item: item[0]
        ):
            records.append(encoded)
    by_relationship: dict[str, list[bytes]] = {}
    for plan in relationship_plans:
        by_relationship.setdefault(plan.table.name, []).append(
            _relationship_record(plan)
        )
    for table in PULSE_GRAFX_SCHEMA_MANIFEST.relationships:
        records.extend(sorted(by_relationship.get(table.name, [])))
    return records


# --- paths and the single-builder fence -----------------------------------------------------------


def _canonical_path(value: str | os.PathLike[str], field: str) -> Path:
    """Resolve one path to the absolute, symlink-free, case-normalized lock input."""
    if not isinstance(value, (str, os.PathLike)):
        raise _invalid_argument(field, value)
    try:
        return Path(os.path.normcase(os.path.realpath(os.fspath(value))))
    except BaseException as failure:
        raise _invalid_argument(field, value) from failure


def _overlaps(left: Path, right: Path) -> bool:
    """Answer whether two canonical trees are the same or contain one another."""
    if left == right:
        return True
    return left in right.parents or right in left.parents


def _lock_path(candidate: Path) -> Path:
    """Derive the one sibling lock path from the canonical candidate value."""
    try:
        return candidate.with_name(candidate.name + ".rebuild.lock")
    except BaseException as failure:
        raise _invalid_argument("candidate_path", candidate) from failure


# --- catalog shape ---------------------------------------------------------------------------------


def _require_catalog(
    database: Database,
    node_tables: tuple[GrafxTableManifest, ...],
    relationship_tables: tuple[GrafxTableManifest, ...],
    *,
    reason: str,
    phase: str,
) -> None:
    """Refuse any catalog that is not exactly the expected shape, before anything copies."""
    view = _backend_call(lambda: database.catalog.catalog, phase)
    expected: dict[str, GrafxTableManifest] = {
        table.name: table for table in node_tables
    }
    expected[PULSE_GRAFX_SCHEMA_MANIFEST.board_meta.name] = (
        PULSE_GRAFX_SCHEMA_MANIFEST.board_meta
    )
    expected.update({table.name: table for table in relationship_tables})
    observed = {table.name: table for table in _backend_call(view.tables, phase)}
    if set(observed) != set(expected):
        raise _divergence(
            reason,
            phase=phase,
            missing=[
                _bounded_identifier(name)
                for name in sorted(set(expected) - set(observed))[:8]
            ],
            unexpected=[
                _bounded_identifier(name)
                for name in sorted(set(observed) - set(expected))[:8]
            ],
        )
    for name, wanted in expected.items():
        seen = observed[name]
        if seen.kind != wanted.kind:
            raise _divergence(reason, phase=phase, table=name, detail="kind")
        if seen.primary_key != wanted.primary_key:
            raise _divergence(reason, phase=phase, table=name, detail="primary_key")
        if seen.from_table != wanted.from_table or seen.to_table != wanted.to_table:
            raise _divergence(reason, phase=phase, table=name, detail="endpoints")
        if len(seen.columns) != len(wanted.columns):
            raise _divergence(
                reason,
                phase=phase,
                table=name,
                detail="arity",
                expected=len(wanted.columns),
                observed=len(seen.columns),
            )
        for position, (column, wanted_column) in enumerate(
            zip(seen.columns, wanted.columns, strict=True)
        ):
            if (
                column.name != wanted_column.name
                or bool(column.nullable) != bool(wanted_column.nullable)
                or column.vector_space != wanted_column.vector_space
                or column.type.name != wanted_column.grafx_value_type
            ):
                raise _divergence(
                    reason,
                    phase=phase,
                    table=name,
                    detail="column",
                    position=position,
                )
    expected_spaces = {
        space.name: space for space in PULSE_GRAFX_SCHEMA_MANIFEST.spaces
    }
    observed_spaces = {space.name: space for space in _backend_call(view.spaces, phase)}
    if set(observed_spaces) != set(expected_spaces):
        raise _divergence(
            reason,
            phase=phase,
            missing_spaces=[
                _bounded_identifier(name)
                for name in sorted(set(expected_spaces) - set(observed_spaces))[:8]
            ],
            unexpected_spaces=[
                _bounded_identifier(name)
                for name in sorted(set(observed_spaces) - set(expected_spaces))[:8]
            ],
        )
    for name, wanted_space in expected_spaces.items():
        seen_space = observed_spaces[name]
        if (
            seen_space.dimension != wanted_space.dimension
            or bool(seen_space.normalized) != bool(wanted_space.normalized)
            or seen_space.storage_dtype != wanted_space.storage_dtype
            or seen_space.metric.value != wanted_space.metric
            or seen_space.state != "active"
        ):
            raise _divergence(reason, phase=phase, space=name, detail="definition")


def _space_of(node_table: str) -> str:
    """Return the manifest space name one node table's embedding belongs to."""
    for space in PULSE_GRAFX_SCHEMA_MANIFEST.spaces:
        if space.node_type == node_table:
            return space.name
    raise _divergence("unknown_node_space", phase="manifest_lookup", table=node_table)


def _space_id(database: Database, name: str, *, phase: str) -> int:
    """Return the candidate-local numeric id of one manifest space."""
    spaces = _backend_call(lambda: database.catalog.catalog.spaces(), phase)
    for space in spaces:
        if space.name == name:
            return space.space_id
    raise _divergence("unknown_candidate_space", phase=phase, space=name)


# --- reading one fixed snapshot --------------------------------------------------------------------


def _read_board_meta(handle: Any) -> _BoardMeta:
    """Read the one BoardMeta row through the handle that owns the snapshot."""
    result = _execute(
        handle,
        "MATCH (m:BoardMeta) "
        "RETURN m.board_id, m.schema_version, m.bootstrapped_at, "
        "m.embedding_model, m.embedding_dimension",
        phase="read_board_meta",
        table="BoardMeta",
    )
    if len(result.rows) != 1:
        raise _divergence(
            "board_meta_not_singleton",
            phase="read_board_meta",
            table="BoardMeta",
            row_count=len(result.rows),
        )
    row = result.rows[0]
    if len(row) != 5:
        raise _divergence(
            "board_meta_projection_mismatch",
            phase="read_board_meta",
            table="BoardMeta",
            arity=len(row),
        )
    board_id, schema_version, bootstrapped_at, model, dimension = row
    if (
        type(board_id) is not str
        or not board_id
        or type(schema_version) is not str
        or type(bootstrapped_at) is not Timestamp
    ):
        raise _divergence(
            "board_meta_value_type_mismatch",
            phase="read_board_meta",
            table="BoardMeta",
        )
    if model is None and dimension is None:
        pass
    elif (
        type(model) is not str
        or not model
        or type(dimension) is not int
        or dimension != EMBEDDING_DIMENSION
    ):
        raise _divergence(
            "board_meta_embedding_mismatch",
            phase="read_board_meta",
            table="BoardMeta",
        )
    return _BoardMeta(
        board_id=board_id,
        schema_version=schema_version,
        bootstrapped_at=bootstrapped_at,
        embedding_model=model,
        embedding_dimension=dimension,
    )


def _read_nodes(
    handle: Any,
    batch_size: int,
    tables: tuple[GrafxTableManifest, ...],
    space_ids: dict[str, int] | None = None,
) -> tuple[tuple[_NodePlan, ...], tuple[tuple[str, int], ...]]:
    """Read every node row of the given tables as an importable plan."""
    target_columns = {
        table.name: tuple(column.name for column in table.columns)
        for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes
    }
    plans: list[_NodePlan] = []
    counts: list[tuple[str, int]] = []
    for table in tables:
        columns = tuple(column.name for column in table.columns)
        projection = ", ".join(f"n.{name}" for name in columns)
        seen = 0
        while True:
            rows = _execute(
                handle,
                f"MATCH (n:{table.name}) RETURN {projection} "
                f"ORDER BY n.{table.primary_key} SKIP $skip LIMIT $limit",
                {"skip": seen, "limit": batch_size},
                phase="read_nodes",
                table=table.name,
            ).rows
            if not rows:
                break
            for row in rows:
                if len(row) != len(columns):
                    raise _divergence(
                        "node_projection_mismatch",
                        phase="read_nodes",
                        table=table.name,
                    )
                present = dict(zip(columns, row, strict=True))
                if space_ids is not None:
                    for value in present.values():
                        if type(value) is VectorValue:
                            _require_source_vector(
                                value,
                                _space_of(table.name),
                                space_ids[table.name],
                                table.name,
                            )
                plans.append(
                    _NodePlan(
                        table=table.name,
                        columns=target_columns[table.name],
                        values=tuple(
                            present.get(name) for name in target_columns[table.name]
                        ),
                    )
                )
            seen += len(rows)
            if len(rows) < batch_size:
                break
        counts.append((table.name, seen))
    return tuple(plans), tuple(counts)


def _read_relationships(
    handle: Any, batch_size: int, tables: tuple[GrafxTableManifest, ...]
) -> tuple[tuple[_RelationshipPlan, ...], tuple[tuple[str, int], ...]]:
    """Read every relationship occurrence of the given tables, preserving multiplicity."""
    node_keys = {
        table.name: table.primary_key for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes
    }
    plans: list[_RelationshipPlan] = []
    counts: list[tuple[str, int]] = []
    for table in tables:
        properties = tuple(column.name for column in table.columns[2:])
        from_key = node_keys[table.from_table]
        to_key = node_keys[table.to_table]
        projection = ", ".join(
            [f"a.{from_key}", f"b.{to_key}", *[f"r.{name}" for name in properties]]
        )
        seen = 0
        while True:
            rows = _execute(
                handle,
                f"MATCH (a:{table.from_table})-[r:{table.name}]->"
                f"(b:{table.to_table}) RETURN {projection} "
                f"ORDER BY {projection} SKIP $skip LIMIT $limit",
                {"skip": seen, "limit": batch_size},
                phase="read_relationships",
                table=table.name,
            ).rows
            if not rows:
                break
            for row in rows:
                if len(row) != 2 + len(properties):
                    raise _divergence(
                        "relationship_projection_mismatch",
                        phase="read_relationships",
                        table=table.name,
                    )
                plans.append(
                    _RelationshipPlan(
                        table=table,
                        from_key=row[0],
                        to_key=row[1],
                        properties=tuple(zip(properties, row[2:], strict=True)),
                    )
                )
            seen += len(rows)
            if len(rows) < batch_size:
                break
        counts.append((table.name, seen))
    return tuple(plans), tuple(counts)


def _read_snapshot(
    handle: Any,
    batch_size: int,
    node_tables: tuple[GrafxTableManifest, ...],
    relationship_tables: tuple[GrafxTableManifest, ...],
    space_ids: dict[str, int] | None = None,
) -> _Snapshot:
    """Return the one fixed logical reading a handle currently exposes."""
    meta = _read_board_meta(handle)
    node_plans, node_counts = _read_nodes(handle, batch_size, node_tables, space_ids)
    relationship_plans, relationship_counts = _read_relationships(
        handle, batch_size, relationship_tables
    )
    return _Snapshot(
        meta=meta,
        node_plans=node_plans,
        relationship_plans=relationship_plans,
        node_counts=node_counts,
        relationship_counts=relationship_counts,
        fingerprint=_digest(_canonical_stream(meta, node_plans, relationship_plans)),
    )


# --- building the candidate generation --------------------------------------------------------------


_WRITE_STATISTICS: dict[str, str] = {
    "node": "rows_created",
    "relationship": "relationships_created",
}


def _commit(
    handle: Any,
    statements: tuple[tuple[str, dict[str, Any], str, str], ...],
) -> None:
    """Run one bounded write transaction, proving each statement and the commit.

    Every statement declares which counter it must move, because a CREATE that reports
    nothing and a CREATE that reported the wrong kind both look like success to a
    caller that only checks for an exception.
    """
    batch_tables = tuple(dict.fromkeys(statement[3] for statement in statements))
    boundary: dict[str, object]
    if len(batch_tables) == 1:
        boundary = {"table": batch_tables[0]}
    else:
        boundary = {
            "tables": tuple(_bounded_identifier(name) for name in batch_tables[:8])
        }
    transaction = _backend_call(
        lambda: handle.begin("write"), "candidate_batch_begin", **boundary
    )
    try:
        for text, parameters, kind, table in statements:
            result = _execute(
                transaction,
                text,
                parameters,
                phase="candidate_batch_execute",
                table=table,
            )
            if len(result.rows) != 1:
                raise _divergence(
                    "write_affected_row_mismatch",
                    phase="candidate_batch_execute",
                    table=table,
                    affected=len(result.rows),
                    kind=kind,
                )
            counter = _WRITE_STATISTICS[kind]
            observed = result.statistics.get(counter)
            if observed != 1:
                raise _divergence(
                    "write_statistic_mismatch",
                    phase="candidate_batch_execute",
                    table=table,
                    kind=kind,
                    counter=counter,
                    observed=observed,
                )
        report = _backend_call(transaction.commit, "candidate_batch_commit", **boundary)
    except BaseException as failure:
        if transaction.active:
            try:
                transaction.rollback()
            except BaseException as cleanup:  # noqa: BLE001 - reported, never raised
                failure.add_note(f"rollback also failed: {type(cleanup).__name__}")
        raise
    if not report.durable or not report.wrote:
        raise _divergence(
            "candidate_commit_not_durable",
            phase="candidate_batch_commit",
            durable=report.durable,
            wrote=report.wrote,
            **boundary,
        )


def _require_ddl(
    result: Any,
    wanted: dict[str, int],
    subject: str,
    *,
    phase: str,
    subject_kind: str,
) -> None:
    """Prove one DDL statement moved exactly the counters it is supposed to move.

    A table that was created without its index, or a vector column whose index was
    skipped, both commit cleanly and both leave a candidate that cannot serve.
    """
    statistics = result.statistics
    for counter, expected in wanted.items():
        if statistics.get(counter) != expected:
            raise _divergence(
                "ddl_statistic_mismatch",
                phase=phase,
                **{subject_kind: subject},
                counter=counter,
                expected=expected,
                observed=statistics.get(counter),
            )
    skipped = statistics.get("indexes_skipped")
    if skipped:
        raise _divergence(
            "ddl_index_skipped",
            phase=phase,
            **{subject_kind: subject},
            skipped=skipped,
        )


def _closed(handle: Database, phase: str, primary: BaseException | None = None) -> bool:
    """Close one handle and prove the close finished, without hiding a primary failure.

    A close that runs in a ``finally`` while something has already gone wrong must not
    become the exception the caller sees: the first failure is the one that explains
    what happened, so a late cleanup problem is attached as a note instead.
    """
    try:
        handle.close()
        complete = handle.close_complete
    except BaseException as failure:
        if primary is not None:
            primary.add_note(
                f"closing the {phase} handle also failed: {type(failure).__name__}"
            )
            return False
        raise _mapped(failure, f"close_{phase}") from failure
    if not complete:
        if primary is not None:
            primary.add_note(f"the {phase} handle did not report a complete close")
            return False
        raise _divergence(f"candidate_close_incomplete_{phase}", phase=f"close_{phase}")
    return True


def _initialise_candidate(candidate: Database, meta: _BoardMeta) -> None:
    """Create every space, table and the marked BoardMeta row in one transaction."""
    manifest = PULSE_GRAFX_SCHEMA_MANIFEST
    transaction = _backend_call(
        lambda: candidate.begin("write"), "candidate_initialize_begin"
    )
    try:
        for space in manifest.spaces:
            _require_ddl(
                _backend_call(
                    lambda space=space: transaction.execute(space.ddl(), {}),
                    "candidate_initialize_space",
                    space=space.name,
                ),
                {"spaces_created": 1},
                space.name,
                phase="candidate_initialize_space",
                subject_kind="space",
            )
        for table in manifest.tables:
            wanted = {
                "tables_created": 1,
                "indexes_created": 2 if table.kind == "rel" else 1,
            }
            if table.kind == "node" and any(
                column.vector_space is not None for column in table.columns
            ):
                wanted["indexes_attached"] = 1
            _require_ddl(
                _execute(
                    transaction,
                    table.ddl(),
                    {},
                    phase="candidate_initialize_table",
                    table=table.name,
                ),
                wanted,
                table.name,
                phase="candidate_initialize_table",
                subject_kind="table",
            )
        result = _execute(
            transaction,
            "CREATE (m:BoardMeta {board_id: $board_id, schema_version: $schema_version, "
            "bootstrapped_at: $bootstrapped_at, embedding_model: $embedding_model, "
            "embedding_dimension: $embedding_dimension}) RETURN m.board_id",
            {
                "board_id": meta.board_id,
                "schema_version": BUILD_MARKER,
                "bootstrapped_at": meta.bootstrapped_at,
                "embedding_model": meta.embedding_model,
                "embedding_dimension": meta.embedding_dimension,
            },
            phase="candidate_initialize_marker",
            table="BoardMeta",
        )
        if len(result.rows) != 1 or result.rows[0][0] != meta.board_id:
            raise _divergence(
                "marker_row_not_created",
                phase="candidate_initialize_marker",
                table="BoardMeta",
            )
        if result.statistics.get("rows_created") != 1:
            raise _divergence(
                "marker_rows_created_mismatch",
                phase="candidate_initialize_marker",
                table="BoardMeta",
                observed=result.statistics.get("rows_created"),
            )
        report = _backend_call(
            transaction.commit, "candidate_initialize_commit", table="BoardMeta"
        )
    except BaseException as failure:
        if transaction.active:
            try:
                transaction.rollback()
            except BaseException as cleanup:  # noqa: BLE001 - noted, never raised
                failure.add_note(f"rollback also failed: {type(cleanup).__name__}")
        raise
    if not report.durable or not report.wrote:
        raise _divergence(
            "initialisation_not_durable", phase="candidate_initialize_commit"
        )


def _require_source_vector(
    value: VectorValue, space_name: str, source_space_id: int, table: str
) -> None:
    """Prove one source vector before the fixed read carries it any further."""
    if value.space_ref != source_space_id:
        raise _divergence(
            "vector_space_ref_mismatch",
            phase="source_vector_validate",
            table=table,
            space=space_name,
            expected=source_space_id,
            observed=value.space_ref,
        )
    if value.dtype != EMBEDDING_STORAGE_DTYPE:
        raise _divergence(
            "vector_dtype_mismatch",
            phase="source_vector_validate",
            table=table,
            space=space_name,
            observed=value.dtype,
        )
    if len(value.values) != EMBEDDING_DIMENSION:
        raise _divergence(
            "vector_dimension_mismatch",
            phase="source_vector_validate",
            table=table,
            space=space_name,
            observed=len(value.values),
        )
    for component in value.values:
        _finite(
            component,
            "non_finite_vector_component",
            phase="source_vector_validate",
            table=table,
            space=space_name,
        )


def _rebuilt_vector(
    value: VectorValue, space_name: str, candidate_space_id: int, table: str
) -> VectorValue:
    """Rebuild one vector against the candidate space, never carrying the source ref.

    The source vector is proved first: a ref that belongs to another space, a foreign
    dtype, a wrong dimension or a non-finite component would otherwise be laundered
    into the candidate under a correct-looking space id.
    """
    # The source ref was already proved inside the fixed read; re-proving dtype, width
    # and finiteness here is what keeps the rebind honest without touching the source
    # database again after its snapshot closed.
    if value.dtype != EMBEDDING_STORAGE_DTYPE:
        raise _divergence(
            "vector_dtype_mismatch",
            phase="candidate_vector_rebind",
            table=table,
            space=space_name,
            observed=value.dtype,
        )
    if len(value.values) != EMBEDDING_DIMENSION:
        raise _divergence(
            "vector_dimension_mismatch",
            phase="candidate_vector_rebind",
            table=table,
            space=space_name,
            observed=len(value.values),
        )
    for component in value.values:
        _finite(
            component,
            "non_finite_vector_component",
            phase="candidate_vector_rebind",
            table=table,
            space=space_name,
        )
    return VectorValue(
        values=value.values, space_ref=candidate_space_id, dtype=value.dtype
    )


def _write_nodes(
    candidate: Database,
    plans: tuple[_NodePlan, ...],
    batch_size: int,
) -> None:
    """Create every node row once, with the eleven introduced properties present and NULL."""
    candidate_ids = {
        table.name: _space_id(
            candidate,
            _space_of(table.name),
            phase="candidate_write_space_lookup",
        )
        for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes
    }
    pending: list[tuple[str, dict[str, Any], str, str]] = []
    for plan in plans:
        space_name = _space_of(plan.table)
        parameters: dict[str, Any] = {}
        for name, value in zip(plan.columns, plan.values, strict=True):
            if type(value) is VectorValue:
                value = _rebuilt_vector(
                    value,
                    space_name,
                    candidate_ids[plan.table],
                    plan.table,
                )
            parameters[name] = value
        assignments = ", ".join(f"{name}: ${name}" for name in plan.columns)
        pending.append(
            (
                f"CREATE (n:{plan.table} {{{assignments}}}) RETURN n.{plan.columns[0]}",
                parameters,
                "node",
                plan.table,
            )
        )
        if len(pending) >= batch_size:
            _commit(candidate, tuple(pending))
            pending.clear()
    if pending:
        _commit(candidate, tuple(pending))


def _write_relationships(
    candidate: Database, plans: tuple[_RelationshipPlan, ...], batch_size: int
) -> None:
    """Create exactly one relationship per exported occurrence, preserving multiplicity."""
    node_keys = {
        table.name: table.primary_key for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes
    }
    pending: list[tuple[str, dict[str, Any], str, str]] = []
    for plan in plans:
        table = plan.table
        assignments = ", ".join(f"{name}: ${name}" for name, _ in plan.properties)
        suffix = f" {{{assignments}}}" if plan.properties else ""
        parameters: dict[str, Any] = {"from_key": plan.from_key, "to_key": plan.to_key}
        parameters.update(dict(plan.properties))
        pending.append(
            (
                f"MATCH (a:{table.from_table} {{{node_keys[table.from_table]}: $from_key}}), "
                f"(b:{table.to_table} {{{node_keys[table.to_table]}: $to_key}}) "
                f"CREATE (a)-[r:{table.name}{suffix}]->(b) "
                f"RETURN a.{node_keys[table.from_table]}",
                parameters,
                "relationship",
                table.name,
            )
        )
        if len(pending) >= batch_size:
            _commit(candidate, tuple(pending))
            pending.clear()
    if pending:
        _commit(candidate, tuple(pending))


# --- certification -----------------------------------------------------------------------------------


def _expected_relationship_counts(
    source_counts: tuple[tuple[str, int], ...],
) -> tuple[tuple[str, int], ...]:
    """Return all 69 target counts in manifest order, the ten introduced ones at zero."""
    carried = dict(source_counts)
    return tuple(
        (table.name, carried.get(table.name, 0))
        for table in PULSE_GRAFX_SCHEMA_MANIFEST.relationships
    )


EXPECTED_PRIMARY_KEY_INDEXES = len(PULSE_GRAFX_SCHEMA_MANIFEST.nodes) + 1
EXPECTED_ENDPOINT_INDEXES = 2 * len(PULSE_GRAFX_SCHEMA_MANIFEST.relationships)
EXPECTED_VECTOR_INDEXES = len(PULSE_GRAFX_SCHEMA_MANIFEST.spaces)
EXPECTED_INDEX_TOTAL = (
    EXPECTED_PRIMARY_KEY_INDEXES + EXPECTED_ENDPOINT_INDEXES + EXPECTED_VECTOR_INDEXES
)


def _require_indexes(candidate: Database, phase: str) -> None:
    """Prove the whole index inventory, not merely that nothing was reported stale.

    A catalog and a clean digest can both agree while an index file is missing, so the
    count, the staleness and the per-space definition are each asserted.
    """
    boundary_phase = f"candidate_{phase}_indexes"
    unindexed_tables = _backend_call(lambda: candidate.unindexed_tables, boundary_phase)
    if unindexed_tables != ():
        raise _divergence(
            f"candidate_unindexed_tables_{phase}",
            phase=boundary_phase,
            tables=[
                _bounded_identifier(name)
                for name in sorted(unindexed_tables, key=str)[:8]
            ],
        )
    stale_indexes = _backend_call(lambda: candidate.stale_indexes, boundary_phase)
    if stale_indexes != ():
        raise _divergence(
            f"candidate_stale_indexes_{phase}",
            phase=boundary_phase,
            count=len(stale_indexes),
        )
    registered = _backend_call(lambda: candidate.indexes.indexes(), boundary_phase)
    if len(registered) != EXPECTED_INDEX_TOTAL:
        raise _divergence(
            f"candidate_index_count_{phase}",
            phase=boundary_phase,
            expected=EXPECTED_INDEX_TOTAL,
            observed=len(registered),
        )
    registered_names = tuple(view.name for view in registered)
    folded_registered_names = tuple(name.casefold() for name in registered_names)
    if len(set(folded_registered_names)) != len(folded_registered_names):
        raise _divergence(
            f"candidate_duplicate_index_name_{phase}", phase=boundary_phase
        )

    catalog_tables = {
        table.name: table
        for table in _backend_call(
            lambda: candidate.catalog.catalog.tables(), boundary_phase
        )
    }
    expected: dict[str, tuple[str, int, str, tuple[int, ...], str, int, str]] = {}

    def expect(
        *,
        name: str,
        table_name: str,
        positions: tuple[int, ...],
        visibility: str,
        key_derivation: str,
    ) -> None:
        table = catalog_tables[table_name]
        expected[name] = (
            f"index/{name}.idx",
            table.table_id,
            table_name,
            positions,
            visibility,
            64,
            key_derivation,
        )

    keyed_tables = (
        PULSE_GRAFX_SCHEMA_MANIFEST.board_meta,
        *PULSE_GRAFX_SCHEMA_MANIFEST.nodes,
    )
    for manifest_table in keyed_tables:
        primary_key = manifest_table.primary_key
        if primary_key is None:  # pragma: no cover - closed manifest invariant
            raise AssertionError(f"{manifest_table.name} lost its primary key")
        primary_position = next(
            position
            for position, column in enumerate(manifest_table.columns)
            if column.name == primary_key
        )
        expect(
            name=f"pk_{manifest_table.name}",
            table_name=manifest_table.name,
            positions=(primary_position,),
            visibility="exact",
            key_derivation="columns",
        )
    for manifest_table in PULSE_GRAFX_SCHEMA_MANIFEST.relationships:
        expect(
            name=f"ef_{manifest_table.name}",
            table_name=manifest_table.name,
            positions=(0,),
            visibility="exact",
            key_derivation="columns",
        )
        expect(
            name=f"et_{manifest_table.name}",
            table_name=manifest_table.name,
            positions=(1,),
            visibility="exact",
            key_derivation="columns",
        )
    for manifest_table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes:
        vector_column = next(
            (column for column in manifest_table.columns if column.vector_space),
            None,
        )
        if vector_column is None:  # pragma: no cover - closed manifest invariant
            raise AssertionError(f"{manifest_table.name} lost its vector column")
        vector_position = next(
            position
            for position, column in enumerate(manifest_table.columns)
            if column.name == vector_column.name
        )
        vector_name = f"vector_{manifest_table.name}_{vector_column.vector_space}"
        expect(
            name=vector_name,
            table_name=manifest_table.name,
            positions=(vector_position,),
            visibility="proximity",
            key_derivation="vector_digest_v1",
        )

    observed_names = set(registered_names)
    if observed_names != set(expected):
        raise _divergence(
            f"candidate_index_inventory_{phase}",
            phase=boundary_phase,
            missing=[
                _bounded_identifier(name)
                for name in sorted(set(expected) - observed_names)[:8]
            ],
            unexpected=[
                _bounded_identifier(name)
                for name in sorted(observed_names - set(expected))[:8]
            ],
        )
    for view in registered:
        definition = view.definition
        wanted = expected[view.name]
        observed = (
            view.file,
            definition.table_id,
            definition.table_name,
            definition.positions,
            definition.visibility.value,
            definition.bucket_count,
            definition.key_derivation,
        )
        if (
            observed != wanted
            or definition.name != view.name
            or definition.file != view.file
            or view.visibility.value != wanted[4]
        ):
            raise _divergence(
                f"candidate_index_definition_{phase}",
                phase=boundary_phase,
                index=_bounded_identifier(view.name),
            )
        if view.stale or view.stale_reason is not None or view.missing_targets != 0:
            raise _divergence(
                f"candidate_index_coverage_{phase}",
                phase=boundary_phase,
                index=_bounded_identifier(view.name),
                stale=view.stale,
                missing_targets=view.missing_targets,
            )

    vectors = _backend_call(lambda: candidate.vectors.indexes(), boundary_phase)
    if len(vectors) != EXPECTED_VECTOR_INDEXES:
        raise _divergence(
            f"candidate_vector_index_count_{phase}",
            phase=boundary_phase,
            expected=EXPECTED_VECTOR_INDEXES,
            observed=len(vectors),
        )
    vector_spaces = tuple(view.space_name for view in vectors)
    vector_names = tuple(view.name for view in vectors)
    if len({name.casefold() for name in vector_names}) != len(vector_names) or len(
        {name.casefold() for name in vector_spaces}
    ) != len(vector_spaces):
        raise _divergence(
            f"candidate_duplicate_vector_index_{phase}", phase=boundary_phase
        )
    wanted = {space.name: space for space in PULSE_GRAFX_SCHEMA_MANIFEST.spaces}
    if set(vector_spaces) != set(wanted):
        raise _divergence(
            f"candidate_vector_index_inventory_{phase}",
            phase=boundary_phase,
            missing=[
                _bounded_identifier(name)
                for name in sorted(set(wanted) - set(vector_spaces))[:8]
            ],
            unexpected=[
                _bounded_identifier(name)
                for name in sorted(set(vector_spaces) - set(wanted))[:8]
            ],
        )
    catalog_spaces = {
        space.name: space
        for space in _backend_call(
            lambda: candidate.catalog.catalog.spaces(), boundary_phase
        )
    }
    for view in vectors:
        space = wanted[view.space_name]
        expected_name = f"vector_{space.node_type}_{space.name}"
        expected_file = f"index/{expected_name}.idx"
        if view.stale or view.stale_reason is not None:
            raise _divergence(
                f"candidate_vector_index_stale_{phase}",
                phase=boundary_phase,
                space=view.space_name,
                stale_reason_present=view.stale_reason is not None,
            )
        if (
            view.name != expected_name
            or view.file != expected_file
            or view.space_id != catalog_spaces[space.name].space_id
            or view.space_name != space.name
            or view.dimension != space.dimension
            or view.metric_of_space.value != space.metric
            or view.storage_dtype != space.storage_dtype
        ):
            raise _divergence(
                f"candidate_vector_index_definition_{phase}",
                phase=boundary_phase,
                space=view.space_name,
            )


def _verify_clean(candidate: Database, phase: str) -> None:
    """Require a verification that actually walked something and found nothing."""
    boundary_phase = f"candidate_{phase}_verify"
    report = _backend_call(lambda: candidate.verify("all"), boundary_phase)
    clean, finding_count, pages_checked, records_checked = _backend_call(
        lambda: (
            report.clean,
            len(report.findings),
            report.pages_checked,
            report.records_checked,
        ),
        boundary_phase,
    )
    if clean is not True:
        raise _divergence(
            f"candidate_verification_not_clean_{phase}",
            phase=boundary_phase,
            findings=finding_count,
            pages=pages_checked,
            records=records_checked,
        )


def _certify(
    handle: Any,
    database: Database,
    batch_size: int,
    expected: _Snapshot,
    *,
    phase: str,
) -> tuple[tuple[str, int], ...]:
    """Prove one candidate handle is exactly the target, and return its relationship counts."""
    boundary_phase = f"candidate_{phase}_certify"
    _require_catalog(
        database,
        PULSE_GRAFX_SCHEMA_MANIFEST.nodes,
        PULSE_GRAFX_SCHEMA_MANIFEST.relationships,
        reason=f"candidate_catalog_divergent_{phase}",
        phase=f"candidate_{phase}_catalog",
    )
    candidate_ids = {
        table.name: _space_id(
            database,
            _space_of(table.name),
            phase=f"candidate_{phase}_space_lookup",
        )
        for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes
    }
    observed = _read_snapshot(
        handle,
        batch_size,
        PULSE_GRAFX_SCHEMA_MANIFEST.nodes,
        PULSE_GRAFX_SCHEMA_MANIFEST.relationships,
        candidate_ids,
    )
    wanted_relationships = _expected_relationship_counts(expected.relationship_counts)
    if observed.node_counts != expected.node_counts:
        raise _divergence(
            f"candidate_node_counts_divergent_{phase}", phase=boundary_phase
        )
    if observed.relationship_counts != wanted_relationships:
        raise _divergence(
            f"candidate_relationship_counts_divergent_{phase}",
            phase=boundary_phase,
        )
    if observed.fingerprint != expected.fingerprint:
        raise _divergence(
            f"candidate_fingerprint_divergent_{phase}",
            phase=boundary_phase,
            expected=expected.fingerprint,
            observed=observed.fingerprint,
        )
    return wanted_relationships


def _rescan_and_stamp(
    candidate: Database, batch_size: int, expected: _Snapshot
) -> None:
    """Rescan the whole candidate and move the marker, in one write transaction."""
    transaction = _backend_call(
        lambda: candidate.begin("write"), "candidate_terminal_begin"
    )
    try:
        _certify(transaction, candidate, batch_size, expected, phase="terminal")
        result = _execute(
            transaction,
            "MATCH (m:BoardMeta {board_id: $board_id, schema_version: $marker}) "
            "SET m.schema_version = $target RETURN m.board_id",
            {
                "board_id": expected.meta.board_id,
                "marker": BUILD_MARKER,
                "target": TARGET_SCHEMA_VERSION,
            },
            phase="candidate_terminal_cas",
            table="BoardMeta",
        )
        if len(result.rows) != 1 or result.rows[0][0] != expected.meta.board_id:
            raise _divergence(
                "stamp_predicate_unmatched",
                phase="candidate_terminal_cas",
                table="BoardMeta",
                affected=len(result.rows),
            )
        if result.statistics.get("rows_updated") != 1 or (
            result.statistics.get("properties_set") != 1
        ):
            raise _divergence(
                "stamp_statistic_mismatch",
                phase="candidate_terminal_cas",
                table="BoardMeta",
                rows_updated=result.statistics.get("rows_updated"),
                properties_set=result.statistics.get("properties_set"),
            )
    except BaseException as failure:
        if transaction.active:
            try:
                transaction.rollback()
            except BaseException as cleanup:  # noqa: BLE001 - noted, never raised
                failure.add_note(f"rollback also failed: {type(cleanup).__name__}")
        raise

    try:
        report = transaction.commit()
    except BaseException as failure:
        mapped = _mapped(failure, "candidate_terminal_commit", table="BoardMeta")
        if transaction.active:
            try:
                transaction.rollback()
            except BaseException as cleanup:  # noqa: BLE001 - noted, never raised
                mapped.add_note(f"rollback also failed: {type(cleanup).__name__}")
            if mapped is failure:
                raise
            raise mapped from failure
        # An inactive transaction has crossed a terminal lifecycle boundary.  Grafx
        # exposes its report when the durable barrier won, but absence or a hostile
        # report is not evidence that it did not win.  Only same-invocation recovery
        # and the complete cold proof may decide this state.
        raise _StampOutcomeAmbiguous("terminal stamp outcome is ambiguous") from mapped
    try:
        durable = report.durable
        wrote = report.wrote
    except BaseException as failure:
        mapped = _mapped(failure, "candidate_terminal_commit_report", table="BoardMeta")
        raise _StampOutcomeAmbiguous("terminal stamp report is ambiguous") from mapped
    if type(durable) is not bool or type(wrote) is not bool or not durable or not wrote:
        invalid = _divergence(
            "stamp_commit_report_invalid",
            phase="candidate_terminal_commit_report",
            table="BoardMeta",
            durable_type=type(durable).__name__,
            wrote_type=type(wrote).__name__,
            durable_is_true=durable is True,
            wrote_is_true=wrote is True,
        )
        raise _StampOutcomeAmbiguous("terminal stamp report is ambiguous") from invalid


# --- the public surface --------------------------------------------------------------------------------


def _open(
    path: Path, identity: Any, *, read_only: bool, phase: str = "candidate_open"
) -> Database:
    """Open one candidate handle with the source's physical identity contract."""
    try:
        return okto_grafx.connect(
            path,
            page_size=identity.page_size,
            partitions_per_table=identity.partitions_per_table,
            read_only=read_only,
        )
    except BaseException as failure:
        mapped = _mapped(failure, phase)
        if mapped is failure:
            raise
        raise mapped from failure


def rebuild_grafx_schema_candidate(
    source: Database,
    candidate_path: str | os.PathLike[str],
    *,
    batch_size: int = 256,
) -> GrafxSchemaCandidateResult:
    """Rebuild one 0.3.12 source into a fresh, unbound 0.5.0 candidate generation.

    The source is never mutated and the candidate is never bound. The returned result
    exists only after the candidate has been stamped, closed, reopened read-only and
    proved again from cold.
    """
    if type(source) is not Database:
        raise _invalid_argument("source", source)
    if type(batch_size) is not int or not (
        MIN_BATCH_SIZE <= batch_size <= MAX_BATCH_SIZE
    ):
        raise _invalid_argument("batch_size", batch_size)
    try:
        candidate_text = os.fspath(candidate_path)
    except BaseException as failure:
        raise _invalid_argument("candidate_path", candidate_path) from failure
    if candidate_text == MEMORY_PATH:
        raise _invalid_argument("candidate_path", candidate_path)

    candidate_root = _canonical_path(candidate_path, "candidate_path")
    source_path = _backend_call(lambda: source.path, "source_preflight")
    if source_path != MEMORY_PATH:
        # Database.path preserves the caller's text and the public storage view exposes
        # no root, so a relative source cannot be re-resolved once the cwd has moved:
        # resolving it here could name a different tree and silently pass the overlap gate.
        if not os.path.isabs(source_path):
            raise _divergence(
                "relative_source_path_not_recoverable", phase="path_validation"
            )
        source_root = _canonical_path(source_path, "source")
        if _overlaps(source_root, candidate_root):
            raise _divergence("candidate_overlaps_source", phase="path_validation")
        if candidate_root.exists() and source_root.exists():
            try:
                if os.path.samefile(candidate_root, source_root):
                    raise _divergence("candidate_is_source", phase="path_validation")
            except OSError as failure:
                # Unable to decide identity is not permission to proceed.
                raise _divergence(
                    "candidate_identity_undecidable", phase="path_validation"
                ) from failure

    lock_file = _lock_path(candidate_root)
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as failure:
        raise _mapped(failure, "lock_prepare") from failure
    try:
        lock = FileLock(str(lock_file), timeout=0)
        lock.acquire()
    except FileLockTimeout as failure:
        raise GraphLockContention(
            "Another builder holds this Grafx candidate path.",
            details={
                "backend": "okto_grafx",
                "operation": _OPERATION,
                "phase": "lock_acquire",
                "reason": "candidate_locked",
            },
        ) from failure
    except BaseException as failure:
        raise _mapped(failure, "lock_acquire") from failure

    retain_candidate_lock = False

    def close_owned(
        handle: Database,
        phase: str,
        failure: BaseException | None = None,
    ) -> bool:
        """Close an owned handle and quarantine the lock if completion is unproved."""
        nonlocal retain_candidate_lock
        try:
            complete = _closed(handle, phase, failure)
        except BaseException:
            retain_candidate_lock = True
            raise
        if not complete:
            retain_candidate_lock = True
        return complete

    primary: BaseException | None = None
    try:
        try:
            # The catalog is proved BEFORE a snapshot exists, then again immediately after
            # the snapshot opens and once more after the last page, so drift cannot enter
            # between the decision to read and the reading itself.
            _require_catalog(
                source,
                PREDECESSOR_NODE_TABLES,
                PREDECESSOR_RELATIONSHIP_TABLES,
                reason="source_catalog_not_predecessor",
                phase="source_preflight_catalog",
            )
            source_space_ids = {
                table.name: _space_id(
                    source,
                    _space_of(table.name),
                    phase="source_preflight_space_lookup",
                )
                for table in PULSE_GRAFX_SCHEMA_MANIFEST.nodes
            }
            transaction = _backend_call(
                lambda: source.begin("read"), "source_snapshot_begin"
            )
            source_failure: BaseException | None = None
            try:
                _require_catalog(
                    source,
                    PREDECESSOR_NODE_TABLES,
                    PREDECESSOR_RELATIONSHIP_TABLES,
                    reason="source_catalog_drifted_at_snapshot",
                    phase="source_snapshot_catalog",
                )
                snapshot = _read_snapshot(
                    transaction,
                    batch_size,
                    PREDECESSOR_NODE_TABLES,
                    PREDECESSOR_RELATIONSHIP_TABLES,
                    source_space_ids,
                )
                if snapshot.meta.schema_version != SOURCE_SCHEMA_VERSION:
                    raise _divergence(
                        "source_version_unsupported",
                        phase="source_snapshot_validate",
                        **_version_evidence(snapshot.meta.schema_version),
                    )
                snapshot_lsn = int(transaction.snapshot.read_lsn)
                _require_catalog(
                    source,
                    PREDECESSOR_NODE_TABLES,
                    PREDECESSOR_RELATIONSHIP_TABLES,
                    reason="source_catalog_drifted",
                    phase="source_snapshot_catalog",
                )
            except BaseException as failure:
                source_failure = failure
                raise
            finally:
                if transaction.active:
                    try:
                        transaction.rollback()
                    except BaseException as cleanup:
                        if source_failure is not None:
                            source_failure.add_note(
                                "closing the source snapshot also failed: "
                                f"{type(cleanup).__name__}"
                            )
                        else:
                            raise _mapped(cleanup, "source_snapshot_close") from cleanup

            identity = _backend_call(lambda: source.identity, "source_identity")
            probed_uuid: bytes | None = None
            try:
                existing = candidate_root.exists() and any(candidate_root.iterdir())
            except OSError as failure:
                raise _mapped(failure, "candidate_scan") from failure
            if existing:
                noop: GrafxSchemaCandidateResult | None = None
                probe = _open(
                    candidate_root,
                    identity,
                    read_only=True,
                    phase="candidate_probe_open",
                )
                try:
                    if probe.identity.database_uuid == identity.database_uuid:
                        raise _divergence(
                            "candidate_shares_source_identity",
                            phase="candidate_probe_identity",
                        )
                    if probe.catalog.catalog.is_empty():
                        existing = False
                        probed_uuid = probe.identity.database_uuid
                    else:
                        existing_meta = _read_board_meta(probe)
                        if existing_meta.schema_version == BUILD_MARKER:
                            raise _divergence(
                                "candidate_abandoned_marker",
                                phase="candidate_probe_metadata",
                            )
                        if existing_meta.schema_version != TARGET_SCHEMA_VERSION:
                            raise _divergence(
                                "candidate_version_unknown",
                                phase="candidate_probe_metadata",
                                **_version_evidence(existing_meta.schema_version),
                            )
                        relationship_counts = _certify(
                            probe, probe, batch_size, snapshot, phase="noop"
                        )
                        _require_indexes(probe, "noop")
                        _verify_clean(probe, "noop")
                        noop = GrafxSchemaCandidateResult(
                            source_schema_version=SOURCE_SCHEMA_VERSION,
                            target_schema_version=TARGET_SCHEMA_VERSION,
                            source_schema_fingerprint=SOURCE_SCHEMA_FINGERPRINT,
                            target_schema_fingerprint=TARGET_SCHEMA_FINGERPRINT,
                            source_snapshot_lsn=snapshot_lsn,
                            logical_data_fingerprint=snapshot.fingerprint,
                            node_row_counts=snapshot.node_counts,
                            relationship_row_counts=relationship_counts,
                            candidate_database_uuid=probe.identity.database_uuid,
                            changed=False,
                        )
                except BaseException as failure:
                    close_owned(probe, "probe", failure)
                    raise
                close_owned(probe, "probe")
                if noop is not None:
                    return noop

            ambiguous: BaseException | None = None
            candidate = _open(
                candidate_root,
                identity,
                read_only=False,
                phase="candidate_build_open",
            )
            try:
                if candidate.identity.database_uuid == identity.database_uuid:
                    raise _divergence(
                        "candidate_shares_source_identity",
                        phase="candidate_build_identity",
                    )
                if probed_uuid is not None and (
                    candidate.identity.database_uuid != probed_uuid
                ):
                    raise _divergence(
                        "candidate_identity_changed_since_probe",
                        phase="candidate_build_identity",
                    )
                if not candidate.catalog.catalog.is_empty():
                    raise _divergence(
                        "candidate_not_empty", phase="candidate_build_preflight"
                    )
                # Capture identity after proving inequality/emptiness and before the first
                # possible candidate write.  This is the authority which permits only this
                # invocation to reconcile an ambiguous terminal commit writably.
                candidate_uuid = candidate.identity.database_uuid
                _initialise_candidate(candidate, snapshot.meta)
                _write_nodes(candidate, snapshot.node_plans, batch_size)
                _write_relationships(candidate, snapshot.relationship_plans, batch_size)
                _certify(candidate, candidate, batch_size, snapshot, phase="hot")
                _require_indexes(candidate, "hot")
                _backend_call(candidate.checkpoint, "candidate_hot_checkpoint")
                _verify_clean(candidate, "hot")
                # A stamp whose commit outcome is ambiguous is not decided here: the cold
                # proof below is the only authority, after a writable recovery/checkpoint.
                try:
                    _rescan_and_stamp(candidate, batch_size, snapshot)
                except _StampOutcomeAmbiguous as failure:
                    ambiguous = failure
                else:
                    try:
                        _backend_call(
                            candidate.checkpoint,
                            "candidate_terminal_checkpoint",
                        )
                    except BaseException as failure:
                        # The stamp is already durable.  Even a typed checkpoint failure is
                        # therefore an outcome ambiguity, not a pre-barrier refusal.
                        ambiguous = failure
            except BaseException as failure:
                close_owned(candidate, "candidate", failure)
                raise
            else:
                if not close_owned(candidate, "candidate", ambiguous):
                    if ambiguous is None:  # pragma: no cover - no-primary close raises
                        raise AssertionError(
                            "an incomplete candidate close lost its cause"
                        )
                    raise ambiguous

            if ambiguous is not None:
                recovered: Database | None = None
                try:
                    # A writable open runs Grafx recovery before exposing the catalog.  It
                    # is sanctioned only here, under the same held lock and UUID receipt.
                    recovered = _open(
                        candidate_root,
                        identity,
                        read_only=False,
                        phase="candidate_ambiguous_recovery_open",
                    )
                    if recovered.identity.database_uuid != candidate_uuid:
                        raise _divergence(
                            "recovery_identity_divergent",
                            phase="candidate_ambiguous_recovery_identity",
                        )
                    _backend_call(
                        recovered.checkpoint,
                        "candidate_ambiguous_recovery_checkpoint",
                    )
                except BaseException as recovery_failure:
                    if recovered is not None:
                        close_owned(recovered, "recovery", recovery_failure)
                    raise recovery_failure from ambiguous
                else:
                    close_owned(recovered, "recovery")

            try:
                cold = _open(
                    candidate_root,
                    identity,
                    read_only=True,
                    phase="candidate_cold_open",
                )
            except BaseException as failure:
                if ambiguous is not None:
                    raise failure from ambiguous
                raise
            try:
                cold_meta = _read_board_meta(cold)
                if cold_meta.schema_version == BUILD_MARKER:
                    abandoned = _divergence(
                        "cold_generation_abandoned",
                        phase="candidate_cold_metadata",
                    )
                    if ambiguous is not None:
                        raise abandoned from ambiguous
                    raise abandoned
                if cold_meta.schema_version != TARGET_SCHEMA_VERSION:
                    divergent = _divergence(
                        "cold_version_divergent",
                        phase="candidate_cold_metadata",
                        **_version_evidence(cold_meta.schema_version),
                    )
                    if ambiguous is not None:
                        raise divergent from ambiguous
                    raise divergent
                relationship_counts = _certify(
                    cold, cold, batch_size, snapshot, phase="cold"
                )
                _require_indexes(cold, "cold")
                _verify_clean(cold, "cold")
                if cold.identity.database_uuid != candidate_uuid:
                    raise _divergence(
                        "cold_identity_divergent",
                        phase="candidate_cold_identity",
                    )
            except BaseException as failure:
                close_owned(cold, "cold", failure)
                raise
            else:
                close_owned(cold, "cold")

            return GrafxSchemaCandidateResult(
                source_schema_version=SOURCE_SCHEMA_VERSION,
                target_schema_version=TARGET_SCHEMA_VERSION,
                source_schema_fingerprint=SOURCE_SCHEMA_FINGERPRINT,
                target_schema_fingerprint=TARGET_SCHEMA_FINGERPRINT,
                source_snapshot_lsn=snapshot_lsn,
                logical_data_fingerprint=snapshot.fingerprint,
                node_row_counts=snapshot.node_counts,
                relationship_row_counts=relationship_counts,
                candidate_database_uuid=candidate_uuid,
                changed=True,
            )
        except GraphError:
            # Already in the Core taxonomy: re-raising keeps the precise reason.
            raise
        except BaseException as failure:
            raise _mapped(failure, "rebuild") from failure
    except BaseException as failure:
        primary = failure
        raise
    finally:
        # `finally` is the only clause that also runs when the body RETURNS, and both
        # success paths return from inside the try. An `else` here would silently skip
        # the release on exactly the paths that hold the lock longest.
        if retain_candidate_lock:
            _RETAINED_CANDIDATE_LOCKS.append(lock)
            if primary is not None:
                primary.add_note(
                    "the candidate path lock was retained because handle close "
                    "completion was not proved"
                )
        else:
            try:
                lock.release()
            except BaseException as cleanup:  # pragma: no cover - kernel lock release
                if primary is not None:
                    primary.add_note(
                        f"releasing the candidate lock also failed: "
                        f"{type(cleanup).__name__}"
                    )
                else:
                    raise _mapped(cleanup, "lock_release") from cleanup


__all__ = [
    "GrafxSchemaCandidateResult",
    "rebuild_grafx_schema_candidate",
]
