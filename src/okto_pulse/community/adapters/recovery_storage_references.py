"""Reconcile known relational file consumers against a verified recovery copy.

Read-only, internal and bounded. Paths are compared lexically to the recorded
source root, never opened in live storage or rewritten for a relocated runtime.
Unreferenced objects remain recovery data, without inferred ownership or ACLs.
"""

import hashlib
import json
from pathlib import Path
import re
import sqlite3

from okto_pulse.community.adapters.relational_recovery_snapshot import _check_time, _deadline, _encode
from okto_pulse.community.adapters.storage_recovery_snapshot import (
    StorageRecoverySnapshot, _is_control, verify_storage_recovery_snapshot,
)


_MAX_BYTES = 64 * 1024 * 1024
_CELL_BYTES = 1024 * 1024
_ARCHIVE_EVENT = "historical_archive.created"


def _schema(connection):
    required = {
        "boards": {"id"}, "cards": {"id", "board_id"},
        "attachments": {"id", "card_id", "path", "size"},
        "domain_events": {"id", "board_id", "event_type", "payload_json"},
    }
    for table, fields in required.items():
        if connection.execute("SELECT type FROM sqlite_schema WHERE name=?", (table,)).fetchall() != [("table",)]:
            raise ValueError("recovery_storage_reference_table_required: " + table)
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not fields <= {row[1] for row in columns} or [row[1] for row in columns if row[5]] != ["id"]:
            raise ValueError("recovery_storage_reference_schema_drift: " + table)
    for table, column, parent in (("cards", "board_id", "boards"),
                                  ("attachments", "card_id", "cards"),
                                  ("domain_events", "board_id", "boards")):
        foreign_keys = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        matches = [row for row in foreign_keys if row[3] == column]
        if (len(matches) != 1 or matches[0][2:5] != (parent, column, "id")
            or sum(row[0] == matches[0][0] for row in foreign_keys) != 1):
            raise ValueError("recovery_storage_reference_foreign_key_drift: " + table)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("recovery_storage_reference_duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("recovery_storage_reference_json_constant_invalid")


def _archive_payload(raw):
    if type(raw) is not str:
        raise ValueError("recovery_storage_reference_archive_payload_invalid")
    try:
        payload = json.loads(raw, object_pairs_hook=_strict_object, parse_constant=_invalid_constant)
    except (RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("recovery_storage_reference_archive_payload_invalid") from exc
    if (type(payload) is not dict
        or set(payload) != {"format", "migration_id", "storage_path", "sha256", "size", "counts"}
        or type(payload["format"]) is not str
        or payload["format"] not in {f"historical-relational-archive/v{i}" for i in (1, 2, 3)}
        or type(payload["migration_id"]) is not str or not payload["migration_id"].strip()
        or len(payload["migration_id"]) > 128
        or type(payload["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", payload["sha256"])
        or type(payload["counts"]) is not list):
        raise ValueError("recovery_storage_reference_archive_payload_invalid")
    names = set()
    for pair in payload["counts"]:
        if (type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str or not pair[0]
            or pair[0] in names or type(pair[1]) is not int or pair[1] < 0):
            raise ValueError("recovery_storage_reference_archive_counts_invalid")
        names.add(pair[0])
    return payload


def reconcile_recovery_storage_references(
    connection: sqlite3.Connection, snapshot: StorageRecoverySnapshot, *,
    max_rows: int = 100_000, max_bytes: int = _MAX_BYTES, max_seconds: float = 60,
) -> dict:
    """Certify references under the caller's pinned relational transaction.

    Capture callers hold a write reservation. Offline callers open the verified
    SQL recovery artifact read-only. The result is a compact deterministic
    certificate; raw source rows and all file bytes remain in their artifacts.
    Hash agreement is physical consistency, not historical content approval.
    """
    deadline = _deadline(max_seconds)
    if not connection.in_transaction:
        raise ValueError("recovery_storage_reference_transaction_required")
    if (type(max_rows) is not int or not 1 <= max_rows <= 100_000
        or type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_BYTES):
        raise ValueError("recovery_storage_reference_limit_invalid")
    _schema(connection)
    manifest = verify_storage_recovery_snapshot(snapshot, max_seconds=max_seconds)
    root = Path(manifest["source_root"])
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("recovery_storage_reference_source_root_invalid")
    objects = {row["path"]: row for row in manifest["files"] if not _is_control(Path(row["path"]).parts[0])}
    remaining_rows, remaining_bytes = max_rows, max_bytes

    def rows(query, columns, parameters=()):
        nonlocal remaining_rows, remaining_bytes
        # Fixed queries/identifiers only. Measure in SQL before materializing a
        # cell, including joined parent IDs and raw historical JSON.
        lengths = [f'COALESCE(length(CAST("{column}" AS BLOB)),0)' for column in columns]
        measures = ["count(*)", "COALESCE(sum(" + "+".join(lengths) + "),0)"]
        measures.extend("COALESCE(max(" + length + "),0)" for length in lengths)
        _check_time(deadline)
        limits = connection.execute("SELECT " + ",".join(measures) + " FROM (" + query + ")", parameters).fetchone()
        if limits[0] > remaining_rows or limits[1] > remaining_bytes or any(size > _CELL_BYTES for size in limits[2:]):
            raise ValueError("recovery_storage_reference_content_limit")
        remaining_rows -= limits[0]
        remaining_bytes -= limits[1]
        for row in connection.execute(query, parameters):
            _check_time(deadline)
            yield row

    def identity(value):
        if type(value) is not str or not value or len(value) > 256:
            raise ValueError("recovery_storage_reference_identity_invalid")
        return value

    boards = [identity(row[0]) for row in rows("SELECT id FROM boards ORDER BY id", ("id",))]
    if boards != manifest["board_ids"]:
        raise ValueError("recovery_storage_reference_board_population_mismatch")
    used, counts, digest = set(), {"attachment": 0, "historical_archive": 0}, hashlib.sha256()

    def reference(kind, row_id, board, path, size, *, card_id=None, expected_hash=None, payload_hash=None):
        identity(row_id)
        identity(board)
        if (type(path) is not str or not path or len(path) > 4096
            or type(size) is not int or size < 0):
            raise ValueError("recovery_storage_reference_object_invalid")
        supplied = Path(path)
        if not supplied.is_absolute() or ".." in supplied.parts:
            raise ValueError("recovery_storage_reference_absolute_path_required")
        try:
            relative = supplied.relative_to(root)
        except ValueError as exc:
            raise ValueError("recovery_storage_reference_path_outside_root") from exc
        if len(relative.parts) != 2 or relative.parts[0] != board:
            raise ValueError("recovery_storage_reference_board_path_mismatch")
        name = relative.as_posix()
        record = objects.get(name)
        if record is None:
            raise ValueError("recovery_storage_reference_object_missing")
        if record["size"] != size or (expected_hash is not None and record["sha256"] != expected_hash):
            raise ValueError("recovery_storage_reference_object_mismatch")
        digest.update(_encode({"kind": kind, "id": row_id, "board_id": board, "card_id": card_id,
            "storage_path": path, "object": name, "size": size, "sha256": record["sha256"],
            "payload_sha256": payload_hash}) + b"\n")
        used.add(name)
        counts[kind] += 1

    query = ("SELECT a.id AS id,a.card_id AS card_id,c.id AS parent_card,c.board_id AS board_id,"
        "b.id AS parent_board,a.path AS path,a.size AS size FROM attachments a "
        "LEFT JOIN cards c ON c.id=a.card_id LEFT JOIN boards b ON b.id=c.board_id ORDER BY a.id")
    for row_id, card, parent_card, board, parent_board, path, size in rows(query,
        ("id", "card_id", "parent_card", "board_id", "parent_board", "path", "size")):
        if parent_card is None or parent_board is None:
            raise ValueError("recovery_storage_reference_attachment_owner_missing")
        reference("attachment", row_id, board, path, size, card_id=identity(card))

    query = ("SELECT e.id AS id,e.board_id AS board_id,b.id AS parent_board,e.payload_json AS payload "
        "FROM domain_events e LEFT JOIN boards b ON b.id=e.board_id WHERE e.event_type=? ORDER BY e.id")
    for row_id, board, parent_board, raw in rows(query, ("id", "board_id", "parent_board", "payload"), (_ARCHIVE_EVENT,)):
        if parent_board is None:
            raise ValueError("recovery_storage_reference_archive_owner_missing")
        payload = _archive_payload(raw)
        reference("historical_archive", row_id, board, payload["storage_path"], payload["size"],
            expected_hash=payload["sha256"], payload_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest())

    unreferenced = hashlib.sha256()
    for name in sorted(objects.keys() - used):
        _check_time(deadline)
        record = objects[name]
        unreferenced.update(_encode({"path": name, "size": record["size"], "sha256": record["sha256"]}) + b"\n")
    return {"format": "relational-storage-reconciliation/v1", "references_sha256": digest.hexdigest(),
        "attachment_count": counts["attachment"], "historical_archive_count": counts["historical_archive"],
        "unreferenced_object_count": len(objects.keys() - used), "unreferenced_objects_sha256": unreferenced.hexdigest()}
