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


_FORMAT = "historical-relational-archive/v1"
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


def _capture(connection: Connection, *, migration_id: str, max_rows: int, max_bytes: int):
    inventory = _inspect_snapshot(connection, max_rows=max_rows)
    inventory.require_valid_relations()
    inventory.historical_references.require_resolved_scopes()
    # Unknown pending effects do not prevent preserving their history. They do
    # prevent cutover through require_classified_work(), which is a separate gate.
    schema = inspect(connection)
    boards = dict(connection.execute(text("SELECT id, board_id FROM sprints ORDER BY id")).all())
    documents = {board: {"format": _FORMAT, "board_id": board, "migration_id": migration_id,
        "origin_kind": "sprint", "tables": {}, "card_links": []} for board in sorted(set(boards.values()))}
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
    result = []
    total = 0
    for board, document in documents.items():
        encoded = _encode(document)
        total += len(encoded)
        if total > max_bytes:
            raise ValueError("historical_archive_capture_limit")
        counts = tuple((name, len(document["tables"][name]["rows"])) for name in _TABLES) + (("card_links", len(document["card_links"])),)
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
    if (document.get("format") != _FORMAT or document.get("board_id") != reference.board_id
            or document.get("migration_id") != reference.migration_id):
        raise ValueError("historical_archive_scope_mismatch")
    counts = tuple((name, len(document["tables"][name]["rows"])) for name in _TABLES) + (("card_links", len(document["card_links"])),)
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
