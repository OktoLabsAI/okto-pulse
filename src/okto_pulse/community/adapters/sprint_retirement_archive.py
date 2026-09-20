"""Internal F2 archive capture using existing Board storage and audit records.

No product reader or permission grant is installed here. The raw archive includes
sections with different historical read authorities; a future public reader must
authorize those sections before disclosing them. This preserves owned relational
history, not a complete environment backup or permission to perform cutover.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
import math
import uuid

from sqlalchemy import inspect, insert, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from okto_pulse.core import StorageProvider
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow
from okto_pulse.community.adapters.sprint_retirement_inventory import _inspect_snapshot


_LEGACY_FORMAT = "historical-relational-archive/v1"
_RELATED_FORMAT = "historical-relational-archive/v2"
_FORMAT = "historical-relational-archive/v3"
_EVENT = "historical_archive.created"
_NAMESPACE = uuid.UUID("9ad371f4-024e-5cbe-a550-b112926cf66a")
_TABLES = ("sprints", "sprint_history", "sprint_qa_items", "sprint_activation_baselines")
_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class HistoricalArchiveReference:
    event_id: str
    board_id: str
    migration_id: str
    storage_path: str
    sha256: str
    size: int
    counts: tuple[tuple[str, int], ...]


def _encode(value) -> bytes:
    # Recovery evidence must preserve Unicode, whitespace and stored JSON text;
    # domain canonicalizers intentionally normalize some of those values.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _cell(value):
    if value is None:
        return ["null", None]
    if type(value) is str:
        return ["text", value]
    if type(value) is int:
        return ["integer", str(value)]
    if type(value) is float and math.isfinite(value):
        return ["real", value.hex()]
    if type(value) is bytes:
        return ["blob", base64.b64encode(value).decode("ascii")]
    raise ValueError("historical_archive_sql_value_unsupported")


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _counts(document):
    tables = _TABLES if document["format"] == _LEGACY_FORMAT else sorted(document["tables"])
    result = tuple((name, len(document["tables"][name]["rows"])) for name in tables)
    result += (("card_links", len(document["card_links"])),)
    if document["format"] != _LEGACY_FORMAT:
        result += (("reference_roles", len(document["reference_roles"])),)
    return result


def _related_plan(inventory):
    """Deduplicate physical rows while retaining every observed reference role."""
    plan = {}

    def add(table, key, board, role):
        identity = (table, key)
        if identity not in plan:
            plan[identity] = (board, [])
        if not board or plan[identity][0] != board:
            raise ValueError("historical_archive_reference_owner_conflict")
        plan[identity][1].append(role)

    for reference in inventory.historical_references.references:
        add(reference.table, reference.key, reference.owner_board_id, {
            "role": reference.role, "origin_id": reference.sprint_id,
            "reference_board_id": reference.reference_board_id, "scope_state": reference.scope_state,
        })
    for item in inventory.work.items:
        add(item.table, (("id", item.row_id),), item.board_id, {
            "role": "durable_work", "origin_ids": list(item.sprint_ids),
            "disposition": item.action, "reason": item.reason,
        })
    for reference in inventory.embedded_references.references:
        add(reference.table, reference.key, reference.owner_board_id, {
            "role": "embedded_source", "column": reference.column, "path": list(reference.path),
            "origin_id": reference.sprint_id, "reference_board_id": reference.reference_board_id,
            "scope_state": reference.scope_state, "form": reference.form,
        })
    return plan


def _append_related(connection, schema, plan, documents, consume, remaining_bytes):
    by_table = {}
    for (table, key), value in plan.items():
        by_table.setdefault(table, {})[key] = value
    for table, wanted in sorted(by_table.items()):
        columns = schema.get_columns(table)
        names = [column["name"] for column in columns]
        primary_key = tuple(schema.get_pk_constraint(table)["constrained_columns"])
        if not primary_key or any(tuple(name for name, _ in key) != primary_key for key in wanted):
            raise ValueError("historical_archive_reference_key_mismatch")
        descriptor = {"columns": [{"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]} for c in columns],
            "primary_key": list(primary_key)}
        ordered = sorted(wanted, key=lambda key: _encode([[_cell(v) for _, v in key]]))
        # Parameterized batches cover composite keys too, without N+1 reads or
        # type-aware ORM decoding that could normalize the stored JSON evidence.
        for start in range(0, len(ordered), 16):
            batch = ordered[start:start + 16]
            predicates, parameters = [], {}
            for index, key in enumerate(batch):
                predicates.append("(" + " AND ".join(f"{_quoted(name)}=:k{index}_{part}" for part, (name, _) in enumerate(key)) + ")")
                parameters.update({f"k{index}_{part}": value for part, (_, value) in enumerate(key)})
            predicate = " OR ".join(predicates)
            size = "+".join(f"coalesce(length(CAST({_quoted(name)} AS BLOB)),0)" for name in names)
            if connection.execute(text(f"SELECT 1 FROM {_quoted(table)} WHERE ({predicate}) AND ({size}) > :byte_limit LIMIT 1"),
                    {**parameters, "byte_limit": remaining_bytes()}).first() is not None:
                raise ValueError("historical_archive_capture_limit")
            query = f"SELECT * FROM {_quoted(table)} WHERE {predicate} ORDER BY " + ",".join(map(_quoted, primary_key))
            observed = set()
            with connection.execute(text(query).execution_options(stream_results=True, yield_per=16), parameters) as rows:
                for row in rows.mappings():
                    key = tuple((name, row[name]) for name in primary_key)
                    board, roles = wanted[key]
                    if "board_id" in row and row["board_id"] != board:
                        raise ValueError("historical_archive_reference_owner_conflict")
                    cells = [_cell(row[name]) for name in names]
                    evidence = {"table": table, "key": [[name, _cell(value)] for name, value in key], "roles": roles}
                    consume([cells, evidence])
                    document = documents[board]
                    section = document["tables"].setdefault(table, {**descriptor, "rows": []})
                    # Owned rows were already copied above; embedded references
                    # add provenance roles, not duplicate historical source rows.
                    if table not in _TABLES:
                        section["rows"].append(cells)
                    document["reference_roles"].append(evidence)
                    observed.add(key)
            if observed != set(batch):
                raise ValueError("historical_archive_reference_row_missing")


def _capture(connection: Connection, *, migration_id: str, max_rows: int, max_bytes: int):
    inventory = _inspect_snapshot(connection, max_rows=max_rows)
    inventory.require_valid_relations()
    inventory.historical_references.require_resolved_scopes()
    inventory.embedded_references.require_resolved_scopes()
    # Unknown pending effects do not prevent preserving their history. They do
    # prevent cutover through require_classified_work(), which is a separate gate.
    schema = inspect(connection)
    boards = dict(connection.execute(text("SELECT id, board_id FROM sprints ORDER BY id")).all())
    plan = _related_plan(inventory)
    owner_boards = set(boards.values()) | {owner for owner, _ in plan.values()}
    existing_boards = set()
    ordered_boards = sorted(owner_boards)
    for start in range(0, len(ordered_boards), 16):
        parameters = {f"b{index}": board for index, board in enumerate(ordered_boards[start:start + 16])}
        placeholders = ",".join(f":{name}" for name in parameters)
        existing_boards.update(connection.execute(text(f"SELECT id FROM boards WHERE id IN ({placeholders})"), parameters).scalars())
    if not owner_boards <= existing_boards:
        raise ValueError("historical_archive_reference_owner_missing")
    documents = {board: {"format": _FORMAT, "board_id": board, "migration_id": migration_id,
        "origin_kind": "sprint", "tables": {}, "card_links": [], "reference_roles": []} for board in sorted(owner_boards)}
    consumed = 0
    encoded_size = 0

    def consume(value):
        nonlocal consumed, encoded_size
        consumed += 1
        encoded_size += len(_encode(value))
        if consumed > max_rows or encoded_size > max_bytes:
            raise ValueError("historical_archive_capture_limit")

    for table in _TABLES:
        columns = schema.get_columns(table)
        names = [column["name"] for column in columns]
        primary_key = schema.get_pk_constraint(table)["constrained_columns"]
        if not primary_key:
            raise ValueError(f"historical_archive_primary_key_missing:{table}")
        descriptor = {"columns": [{"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"]} for c in columns],
            "primary_key": primary_key}
        for document in documents.values():
            document["tables"][table] = {**descriptor, "rows": []}
        # Reject oversized raw rows in SQLite before transferring a potentially
        # huge text/blob into Python. Encoded/tag/base64 overhead is checked too.
        size_expression = "+".join(f"coalesce(length(CAST({_quoted(name)} AS BLOB)),0)" for name in names)
        if connection.execute(text(f"SELECT 1 FROM {_quoted(table)} WHERE ({size_expression}) > :limit LIMIT 1"),
                {"limit": max_bytes - encoded_size}).first() is not None:
            raise ValueError("historical_archive_capture_limit")
        query = f'SELECT * FROM {_quoted(table)} ORDER BY ' + ",".join(map(_quoted, primary_key)) + " LIMIT :limit"
        with connection.execute(text(query).execution_options(stream_results=True, yield_per=16), {"limit": max_rows - consumed + 1}) as rows:
            for row in rows.mappings():
                board = row["board_id"] if table == "sprints" else boards[row["sprint_id"]]
                cells = [_cell(row[name]) for name in names]
                consume(cells)
                documents[board]["tables"][table]["rows"].append(cells)
    with connection.execute(text("SELECT id,board_id,spec_id,sprint_id FROM cards WHERE sprint_id IS NOT NULL ORDER BY id LIMIT :limit")
            .execution_options(stream_results=True, yield_per=16), {"limit": max_rows - consumed + 1}) as rows:
        for row in rows.mappings():
            link = dict(row)
            consume(link)
            documents[row["board_id"]]["card_links"].append(link)
    _append_related(connection, schema, plan, documents, consume, lambda: max_bytes - encoded_size)
    result = []
    total = 0
    for board, document in documents.items():
        encoded = _encode(document)
        total += len(encoded)
        if total > max_bytes:
            raise ValueError("historical_archive_capture_limit")
        counts = _counts(document)
        identity = str(uuid.uuid5(_NAMESPACE, _encode([migration_id, board]).decode("utf-8")))
        result.append((identity, board, encoded, counts))
    return result


async def verify_historical_archive(storage: StorageProvider, reference: HistoricalArchiveReference) -> dict:
    """Privileged migration verification only; not an ACL-aware product read."""
    if not 0 < reference.size <= _MAX_BYTES:
        raise ValueError("historical_archive_size_invalid")
    metadata = await storage.stat(reference.storage_path)
    if metadata.size != reference.size:
        raise ValueError("historical_archive_size_mismatch")
    content = bytearray()
    async for chunk in storage.open_stream(reference.storage_path):
        content.extend(chunk)
        if len(content) > reference.size:
            raise ValueError("historical_archive_size_mismatch")
    if len(content) != reference.size or hashlib.sha256(content).hexdigest() != reference.sha256:
        raise ValueError("historical_archive_hash_mismatch")
    document = json.loads(content)
    if (document.get("format") not in {_LEGACY_FORMAT, _RELATED_FORMAT, _FORMAT} or document.get("board_id") != reference.board_id
            or document.get("migration_id") != reference.migration_id):
        raise ValueError("historical_archive_scope_mismatch")
    counts = _counts(document)
    if counts != reference.counts:
        raise ValueError("historical_archive_counts_mismatch")
    return document


async def capture_sprint_retirement_archive(
    engine: AsyncEngine, storage: StorageProvider, *, migration_id: str,
    max_rows: int = 100_000, max_bytes: int = _MAX_BYTES,
) -> tuple[HistoricalArchiveReference, ...]:
    """Capture all Boards atomically under a SQLite write reservation.

    This internal operation does not detach Cards or remove live tables. A later
    cutover must compare/reconcile the archive under its own fence. A replay of
    the same migration ID is accepted only for identical current source content.
    Durable blobs without committed audit references are never product-visible;
    uncertain commit outcomes retain them for operator reconciliation.
    """
    if not isinstance(migration_id, str) or not migration_id.strip() or len(migration_id) > 128:
        raise ValueError("historical_archive_migration_id_invalid")
    if type(max_rows) is not int or max_rows < 1 or type(max_bytes) is not int or not 0 < max_bytes <= _MAX_BYTES:
        raise ValueError("historical_archive_limit_invalid")
    if engine.dialect.name != "sqlite":
        raise ValueError("historical_archive_backend_unsupported")
    references = []
    created_paths = []
    commit_started = False
    async with engine.connect() as connection:
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            captures = await connection.run_sync(lambda sync: _capture(sync, migration_id=migration_id, max_rows=max_rows, max_bytes=max_bytes))
            existing = (await connection.execute(select(DomainEventRow.id).where(
                DomainEventRow.event_type == _EVENT,
                DomainEventRow.payload_json["migration_id"].as_string() == migration_id,
            ).limit(max_rows + 1))).scalars().all()
            if existing and set(existing) != {item[0] for item in captures}:
                raise ValueError("historical_archive_replay_population_mismatch")
            for identity, board, content, counts in captures:
                digest = hashlib.sha256(content).hexdigest()
                row = (await connection.execute(select(DomainEventRow.__table__).where(DomainEventRow.id == identity))).mappings().one_or_none()
                if row is not None:
                    payload = row["payload_json"]
                    if (row["event_type"] != _EVENT or row["board_id"] != board or not isinstance(payload, dict)
                            or set(payload) != {"format", "migration_id", "storage_path", "sha256", "size", "counts"}
                            or payload["format"] != _FORMAT or payload["migration_id"] != migration_id
                            or payload["sha256"] != digest or payload["size"] != len(content)
                            or payload["counts"] != [list(pair) for pair in counts]):
                        raise ValueError("historical_archive_replay_mismatch")
                    path = payload["storage_path"]
                else:
                    path = await storage.save(board, f"historical-archive-{identity}.json", content)
                    created_paths.append(path)
                reference = HistoricalArchiveReference(identity, board, migration_id, path, digest, len(content), counts)
                await verify_historical_archive(storage, reference)
                if row is None:
                    await connection.execute(insert(DomainEventRow.__table__).values(id=identity, board_id=board,
                        event_type=_EVENT, actor_type="system", actor_id=None, occurred_at=datetime.now(timezone.utc),
                        payload_json={"format": _FORMAT, "migration_id": migration_id, "storage_path": path,
                            "sha256": digest, "size": len(content), "counts": [list(pair) for pair in counts]}))
                references.append(reference)
            commit_started = True
            await connection.commit()
        except BaseException:
            await connection.rollback()
            if not commit_started:
                for path in created_paths:
                    # Cleanup errors do not mask the migration failure. Without
                    # a committed audit reference, these blobs are unreachable.
                    try:
                        await storage.delete(path)
                    except Exception:
                        pass
            raise
    return tuple(references)
