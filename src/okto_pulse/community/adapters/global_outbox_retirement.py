"""Private offline retirement of proved, source-exclusive global outbox work.

The retained plan contains raw operational evidence; it is not product history
and must be sealed with the original backup by the offline coordinator. This
helper does not authorize runtime admission or provide a cross-store commit.
"""

from dataclasses import dataclass
import hashlib
import json

from sqlalchemy import inspect, text

from okto_pulse.core.ports.global_outbox import GLOBAL_OUTBOX_RETIRED_SENTINEL
from okto_pulse.core.ports.global_retirement_graph import GlobalGraphRetirementPlan
from okto_pulse.core.ports.outbox_retirement import classify_outbox_retirement
from okto_pulse.core.ports.retirement_graph import graph_retirement_fingerprint
from .grafx_global_retirement import _require_retired_boards
from .logical_transfer_factories import make_grafx_logical_source
from .sprint_retirement_archive import _cell, _encode
from .sprint_retirement_embedded import _constant, _object
from .sqlalchemy_models import ConsolidationAudit, GlobalUpdateOutbox, KuzuNodeRef

_MODELS = (ConsolidationAudit, GlobalUpdateOutbox, KuzuNodeRef)
_MAX_ROWS = 100_000
_MAX_BYTES = 64 * 1024 * 1024
_MAX_ROW_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class OutboxRetirementPlan:
    graph_plan: GlobalGraphRetirementPlan
    archived_origins: tuple[tuple[str, tuple[str, ...]], ...]
    original: bytes
    selected_ids: tuple[str, ...]
    after_sha256: str


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _snapshot(connection):
    if connection.dialect.name != "sqlite":
        raise ValueError("outbox_retirement_sqlite_required")
    schema = inspect(connection)
    document, count, size = {}, 0, 0
    for model in _MODELS:
        table = model.__table__
        names = tuple(sorted(column.name for column in table.columns))
        if (not schema.has_table(table.name)
                or set(names) != {column["name"] for column in schema.get_columns(table.name)}
                or tuple(schema.get_pk_constraint(table.name)["constrained_columns"])
                != tuple(column.name for column in table.primary_key)):
            raise ValueError("outbox_retirement_schema_mismatch")
        # Names come from closed Community mappings; values remain parameters.
        length = "+".join(f'coalesce(length(CAST("{name}" AS BLOB)),0)' for name in names)
        projection = ",".join(f'CASE WHEN ({length}) <= {_MAX_ROW_BYTES} THEN "{name}" END AS "{name}"' for name in names)
        query = text(f'SELECT {projection}, {length} AS _row_bytes FROM "{table.name}" '
            f'ORDER BY {next(iter(table.primary_key)).name} LIMIT :cap'
        ).execution_options(stream_results=True, yield_per=16)
        rows = []
        with connection.execute(query, {"cap": _MAX_ROWS - count + 1}) as result:
            for row in result.mappings():
                count += 1
                if count > _MAX_ROWS or row["_row_bytes"] > _MAX_ROW_BYTES:
                    raise ValueError("outbox_retirement_snapshot_limit")
                cells = [_cell(row[name]) for name in names]
                size += len(_encode(cells))
                if size > _MAX_BYTES:
                    raise ValueError("outbox_retirement_snapshot_limit")
                rows.append(cells)
        document[table.name] = {"columns": names, "rows": rows}
    raw = _encode(document)
    if len(raw) > _MAX_BYTES:
        raise ValueError("outbox_retirement_snapshot_limit")
    return raw


def _rows(section):
    for cells in section["rows"]:
        yield {key: (int(value) if kind == "integer" else value if kind in {"text", "null"} else None)
            for key, (kind, value) in zip(section["columns"], cells, strict=True)}


def _derive(graph_plan, archived_origins, original):
    if not isinstance(graph_plan, GlobalGraphRetirementPlan):
        raise ValueError("outbox_retirement_graph_plan_invalid")
    boards = tuple(item.board_plan.board_id for item in graph_plan.sources)
    if (type(archived_origins) is not tuple
            or tuple(owner for owner, _ in archived_origins) != boards
            or sum(len(ids) for _, ids in archived_origins) > _MAX_ROWS
            or any(type(ids) is not tuple or any(type(key) is not str or not 1 <= len(key) <= 128 for key in ids)
                or ids != tuple(sorted(set(ids))) for _, ids in archived_origins)
            or type(original) is not bytes or len(original) > _MAX_BYTES):
        raise ValueError("outbox_retirement_inputs_invalid")
    document = json.loads(original, object_pairs_hook=_object, parse_constant=_constant)
    audits = {row["session_id"]: row for row in _rows(document["consolidation_audit"])}
    refs = {}
    for row in _rows(document["kuzu_node_refs"]):
        refs.setdefault(row["session_id"], []).append(row)
    plans = {item.board_plan.board_id: item.board_plan for item in graph_plan.sources}
    origins = {owner: frozenset(ids) for owner, ids in archived_origins}
    selected = []
    section = document["global_update_outbox"]
    retry_index = section["columns"].index("retry_count")
    for cells, event in zip(section["rows"], _rows(section), strict=True):
        if event["board_id"] not in plans:
            continue
        event["payload"] = json.loads(event["payload"], object_pairs_hook=_object, parse_constant=_constant)
        disposition = classify_outbox_retirement(event=event, audit=audits.get(event["session_id"]),
            references=tuple(refs.get(event["session_id"], ())), board_plan=plans[event["board_id"]],
            archived_origin_ids=origins[event["board_id"]])
        if disposition.action == "review":
            raise ValueError(f"outbox_retirement_requires_review:{event['id']}:{disposition.reason}")
        if disposition.action == "supersede":
            selected.append(event["id"])
            cells[retry_index] = ["integer", str(GLOBAL_OUTBOX_RETIRED_SENTINEL)]
    return OutboxRetirementPlan(graph_plan, archived_origins, original, tuple(sorted(selected)), _sha(_encode(document)))


async def prepare_outbox_retirement(engine, *, graph_plan, archived_origins):
    # Validate scope before constructing SQL and retain the original, never
    # recapture after a partially completed migration.
    if not isinstance(graph_plan, GlobalGraphRetirementPlan):
        raise ValueError("outbox_retirement_graph_plan_invalid")
    async with engine.connect() as connection:
        await connection.execute(text("BEGIN"))
        try:
            original = await connection.run_sync(_snapshot)
            return _derive(graph_plan, archived_origins, original)
        finally:
            await connection.rollback()


def _graphs_retired(plan, database, board_databases):
    _require_retired_boards(plan.graph_plan, board_databases)
    snapshot = make_grafx_logical_source(database, scope="global_discovery").open_snapshot()
    try:
        if graph_retirement_fingerprint(snapshot, scope="global_discovery") != plan.graph_plan.after_sha256:
            raise ValueError("outbox_retirement_global_not_retired")
    finally:
        snapshot.close()


async def apply_outbox_retirement(engine, plan, *, global_database, board_databases):
    if not isinstance(plan, OutboxRetirementPlan) or _derive(plan.graph_plan, plan.archived_origins, plan.original) != plan:
        raise ValueError("outbox_retirement_plan_mismatch")
    _graphs_retired(plan, global_database, board_databases)
    async with engine.connect() as connection:
        await connection.execute(text("BEGIN IMMEDIATE"))
        try:
            current = await connection.run_sync(_snapshot)
            if _sha(current) == plan.after_sha256:
                return plan
            if current != plan.original:
                raise ValueError("outbox_retirement_before_mismatch")
            for identity in plan.selected_ids:
                await connection.execute(text("UPDATE global_update_outbox SET retry_count=:retired WHERE id=:identity"),
                    {"retired": GLOBAL_OUTBOX_RETIRED_SENTINEL, "identity": identity})
            after = await connection.run_sync(_snapshot)
            if _sha(after) != plan.after_sha256:
                raise ValueError("outbox_retirement_after_mismatch")
            _graphs_retired(plan, global_database, board_databases)
            await connection.commit()
            return plan
        finally:
            await connection.rollback()
