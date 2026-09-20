"""Fenced F2C retirement after verified Card preservation, before schema cutover.

Original events and processing history remain intact. Only understood pending
work changes to superseded; no handler runs and no completion is fabricated.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import uuid

from sqlalchemy import LargeBinary, cast, func, insert, select, text

from okto_pulse.core.ports.work_retirement import (
    SUPERSEDED_WORK_STATUS, WORK_RETIRED_ORIGIN_EVENT, WORK_RETIREMENT_FORMAT, classify_historical_sprint_event,
    classify_historical_sprint_execution, classify_historical_sprint_queue,
)
from okto_pulse.community.adapters.card_validation_retirement import (
    CardValidationRetirementReceipt, _read_events as _read_card_events, _verify_events as _verify_card_events,
)
from okto_pulse.community.adapters.historical_archive_grant_installation import _install_historical_archive_grants
from okto_pulse.community.adapters.permission_retirement_checkpoint import _digest
from okto_pulse.community.adapters.sprint_retirement_archive import _cell, _encode, verify_historical_archive
from okto_pulse.community.adapters.sprint_retirement_inventory import _inspect_snapshot
from okto_pulse.community.adapters.sprint_retirement_work import inspect_sprint_retirement_work
from okto_pulse.community.adapters.sqlalchemy_models import DomainEventRow

_TABLES = ("domain_events", "domain_event_handler_executions", "consolidation_queue")
_EVENT = "migration.work_superseded"
_COMPLETE = "migration.work_retirement_completed"
_FORMAT = WORK_RETIREMENT_FORMAT
_NAMESPACE = uuid.UUID("9c273802-bb1f-5c3b-80f3-a770d168c6d6")
_MAX_BYTES = 64 * 1024 * 1024
_AUDIT = DomainEventRow.__table__


def _id(migration_id, kind, identity):
    return str(uuid.uuid5(_NAMESPACE, _encode([migration_id, kind, identity]).decode("utf-8")))


@dataclass(frozen=True, slots=True)
class WorkRetirementReceipt:
    migration_id: str
    evidence_sha256: str
    events: int
    executions: int
    queue_items: int
    origins: int

    def __post_init__(self):
        if (type(self.migration_id) is not str or not 1 <= len(self.migration_id) <= 128
                or type(self.evidence_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.evidence_sha256) is None
                or any(type(value) is not int or not 0 <= value <= 100_000
                    for value in (self.events, self.executions, self.queue_items, self.origins))):
            raise ValueError("work_retirement_receipt_invalid")


def _source_rows(documents):
    sources = {}
    for board, document in documents.items():
        for table in _TABLES:
            section = document["tables"].get(table)
            if section is None:
                continue
            names = tuple(column["name"] for column in section["columns"])
            if section["primary_key"] != ["id"] or len(set(names)) != len(names):
                raise ValueError("work_retirement_archive_schema_invalid")
            for cells in section["rows"]:
                if len(cells) != len(names) or cells[names.index("id")][0] != "text":
                    raise ValueError("work_retirement_archive_schema_invalid")
                identity = cells[names.index("id")][1]
                if (table, identity) in sources:
                    raise ValueError("work_retirement_archive_population_invalid")
                sources[table, identity] = (board, names, cells)
    return sources


def _facts(source):
    _, names, cells = source
    # Only text/null columns feed classification. Full tagged SQL values are
    # retained for exact row comparison and evidence, without normalization.
    return {name: (cell[1] if cell[0] == "text" else None) for name, cell in zip(names, cells, strict=True)}


def _plans(sources):
    plans, events = {}, {}
    for (table, identity), source in sources.items():
        if table == "domain_events":
            facts = _facts(source)
            events[identity] = (facts["event_type"], classify_historical_sprint_event(
                facts["event_type"], json.loads(facts["payload_json"])))
    for key, source in sources.items():
        table, identity = key
        facts = _facts(source)
        if table == "domain_events":
            disposition = events[identity][1]
            action, reason = disposition.action, disposition.reason
        elif table == "domain_event_handler_executions":
            event_type, disposition = events[facts["event_id"]]
            action, reason = classify_historical_sprint_execution(event_type, disposition,
                handler_name=facts["handler_name"], status=facts["status"])
        else:
            disposition = classify_historical_sprint_queue(artifact_type=facts["artifact_type"],
                artifact_id=facts["artifact_id"], work_kind=facts["work_kind"], status=facts["status"],
                payload=json.loads(facts["payload"]) if facts["payload"] is not None else None)
            action, reason = disposition.action, disposition.reason
        if action == "review" or facts.get("status") == SUPERSEDED_WORK_STATUS:
            raise ValueError("work_retirement_source_requires_review")
        plans[key] = (action, reason)
    return plans


def _evidence(migration_id, references, card_receipt, sources, plans, documents):
    by_board = {ref.board_id: ref for ref in references}
    rows, expected_after, counts = [], {}, dict.fromkeys(_TABLES, 0)
    for (table, identity), (board, names, cells) in sorted(sources.items()):
        after = [list(cell) for cell in cells]
        action, reason = plans[table, identity]
        if action == "supersede":
            counts[table] += 1
            status_index = names.index("status") if table != "domain_events" else None
            before_status = cells[status_index][1] if status_index is not None else None
            if status_index is not None:
                after[status_index] = ["text", SUPERSEDED_WORK_STATUS]
            reference = by_board[board]
            payload = {"format": _FORMAT, "migration_id": migration_id, "source_table": table, "source_id": identity,
                "archive_id": reference.event_id, "archive_sha256": reference.sha256,
                "before_status": before_status, "after_status": SUPERSEDED_WORK_STATUS,
                "before_sha256": _digest(cells), "after_sha256": _digest(after), "reason": reason}
            rows.append({"id": _id(migration_id, table, identity), "board_id": board, "event_type": _EVENT,
                "actor_type": "system", "actor_id": None, "payload_json": payload})
        expected_after[table, identity] = (board, names, after)
    origin_count = 0
    for reference in references:
        section = documents[reference.board_id]["tables"]["sprints"]
        index = next(index for index, column in enumerate(section["columns"]) if column["name"] == "id")
        for identity in sorted(cells[index][1] for cells in section["rows"]):
            origin_count += 1
            rows.append({"id": _id(migration_id, "origin", identity), "board_id": reference.board_id,
                "event_type": WORK_RETIRED_ORIGIN_EVENT, "actor_type": "system", "actor_id": None,
                "payload_json": {"format": _FORMAT, "migration_id": migration_id,
                    "origin_kind": "sprint", "origin_id": identity,
                    "archive_id": reference.event_id, "archive_sha256": reference.sha256}})
    manifests = []
    for reference in references:
        owned = [row for row in rows if row["board_id"] == reference.board_id]
        payload = {"format": _FORMAT, "migration_id": migration_id,
            "archive_id": reference.event_id, "archive_sha256": reference.sha256,
            "card_receipt_sha256": card_receipt.evidence_sha256, "count": len(owned), "rows_sha256": _digest(owned)}
        manifests.append({"id": _id(migration_id, "complete", reference.board_id), "board_id": reference.board_id,
            "event_type": _COMPLETE, "actor_type": "system", "actor_id": None, "payload_json": payload})
    receipt = WorkRetirementReceipt(migration_id, _digest(manifests), *(counts[table] for table in _TABLES), origin_count)
    if len(json.dumps(rows + manifests, allow_nan=False).encode("utf-8")) > _MAX_BYTES:
        raise ValueError("work_retirement_limit")
    return receipt, rows + manifests, expected_after


async def _stored_journal(connection, migration_id):
    where = (_AUDIT.c.event_type.in_((_EVENT, _COMPLETE, WORK_RETIRED_ORIGIN_EVENT)),
        _AUDIT.c.payload_json["migration_id"].as_string() == migration_id)
    count, size = (await connection.execute(select(func.count(), func.coalesce(func.sum(
        func.length(cast(_AUDIT.c.payload_json, LargeBinary))), 0)).where(*where))).one()
    if count > 100_000 or size > _MAX_BYTES:
        raise ValueError("work_retirement_limit")
    return [{key: value for key, value in row.items() if key != "occurred_at"}
        for row in (await connection.execute(select(_AUDIT).where(*where))).mappings()]


async def _require_rows(connection, sources):
    for (table, identity), (_, names, expected) in sources.items():
        columns = [f'"{name.replace(chr(34), chr(34) * 2)}"' for name in names]
        size = "+".join(f"coalesce(length(CAST({column} AS BLOB)),0)" for column in columns)
        length = (await connection.execute(text(f'SELECT {size} FROM "{table}" WHERE id=:id'), {"id": identity})).scalar_one_or_none()
        if length is None or length > _MAX_BYTES:
            raise ValueError("work_retirement_source_changed")
        row = (await connection.execute(text(f'SELECT {",".join(columns)} FROM "{table}" WHERE id=:id'), {"id": identity})).one()
        if [_cell(value) for value in row] != expected:
            raise ValueError("work_retirement_source_changed")


async def _require_work_population(connection, documents, sources, *, replay):
    owners = {}
    for board, document in documents.items():
        section = document["tables"]["sprints"]
        identity_index = next(index for index, column in enumerate(section["columns"]) if column["name"] == "id")
        owners.update({cells[identity_index][1]: board for cells in section["rows"]})
    work = await connection.run_sync(lambda sync: inspect_sprint_retirement_work(sync,
        remaining_rows=100_000, sprint_boards=owners))
    work.require_classified_work()
    current = {(item.table, item.row_id) for item in work.items}
    if (current - sources.keys()) or (not replay and current != sources.keys()):
        raise ValueError("work_retirement_population_changed")


async def supersede_archived_sprint_work(engine, storage, references, *,
    card_receipt: CardValidationRetirementReceipt, expected_receipt: WorkRetirementReceipt | None = None,
) -> WorkRetirementReceipt:
    """Supersede only the archived exclusive population after Card preservation."""
    if engine.dialect.name != "sqlite":
        raise ValueError("work_retirement_backend_unsupported")
    migration_id = card_receipt.migration_id
    references = tuple(sorted(references, key=lambda ref: ref.board_id))
    if (len({ref.board_id for ref in references}) != len(references)
            or any(ref.migration_id != migration_id for ref in references)
            or sum(ref.size for ref in references) > _MAX_BYTES):
        raise ValueError("work_retirement_archive_scope_invalid")
    async with engine.connect() as connection:
        try:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            documents = {}
            for reference in references:
                await _install_historical_archive_grants(connection, storage, reference, require_existing=True)
                documents[reference.board_id] = await verify_historical_archive(storage, reference)
            if await _verify_card_events(await _read_card_events(connection, migration_id), references, migration_id, storage) != card_receipt:
                raise ValueError("work_retirement_card_receipt_mismatch")
            sources = _source_rows(documents)
            plans = _plans(sources)
            receipt, expected, after = _evidence(migration_id, references, card_receipt, sources, plans, documents)
            previous = await _stored_journal(connection, migration_id)
            if expected_receipt is not None and (expected_receipt != receipt or (references and not previous)):
                raise ValueError("work_retirement_replay_mismatch")
            if previous:
                if sorted(previous, key=lambda row: row["id"]) != sorted(expected, key=lambda row: row["id"]):
                    raise ValueError("work_retirement_evidence_mismatch")
                # Surviving Card work may progress after cutover. Retired rows
                # cannot resurrect, disappear or acquire fabricated processing.
                await _require_rows(connection, {key: value for key, value in after.items() if plans[key][0] == "supersede"})
                await _require_work_population(connection, documents, sources, replay=True)
                await connection.commit()
                return receipt
            inventory = await connection.run_sync(lambda sync: _inspect_snapshot(sync, max_rows=100_000))
            inventory.require_valid_relations()
            inventory.work.require_classified_work()
            current = {(item.table, item.row_id): (item.action, item.reason) for item in inventory.work.items}
            if current != plans:
                raise ValueError("work_retirement_population_changed")
            await _require_rows(connection, sources)
            for (table, identity), (action, _) in plans.items():
                if action == "supersede" and table != "domain_events":
                    await connection.execute(text(f'UPDATE "{table}" SET status=:status WHERE id=:id'),
                        {"status": SUPERSEDED_WORK_STATUS, "id": identity})
            if expected:
                await connection.execute(insert(_AUDIT), [{**row, "occurred_at": datetime.now(timezone.utc)} for row in expected])
            await _require_rows(connection, after)
            await _require_work_population(connection, documents, sources, replay=False)
            if sorted(await _stored_journal(connection, migration_id), key=lambda row: row["id"]) != sorted(expected, key=lambda row: row["id"]):
                raise ValueError("work_retirement_evidence_mismatch")
            await connection.commit()
            return receipt
        except BaseException:
            await connection.rollback()
            raise
