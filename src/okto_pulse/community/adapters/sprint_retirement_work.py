"""Internal read-only F2 inventory of durable events, executions and graph jobs.

Invoked inside the relational inventory's existing snapshot. No handler runs and
no status is rewritten. Raw audit payloads stay authoritative and untouched.
"""

from dataclasses import dataclass, replace
import json

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from okto_pulse.core.domain.sprint_retirement_events import (
    classify_historical_sprint_event,
    classify_historical_sprint_execution,
    classify_historical_sprint_queue,
)


@dataclass(frozen=True, slots=True)
class SprintRetirementWorkItem:
    table: str
    row_id: str
    board_id: str
    action: str
    reason: str
    sprint_ids: tuple[str, ...]
    parent_event_id: str | None = None
    handler_name: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class SprintWorkInventory:
    scanned_counts: tuple[tuple[str, int], ...]
    items: tuple[SprintRetirementWorkItem, ...]

    def require_classified_work(self) -> None:
        pending = tuple(item for item in self.items if item.action == "review")
        if pending:
            error = SprintRetirementWorkError(f"sprint_retirement_work_requires_review:{len(pending)}")
            error.items = pending
            raise error


class SprintRetirementWorkError(RuntimeError):
    items: tuple[SprintRetirementWorkItem, ...] = ()


_REQUIRED = {
    "domain_events": {"id", "board_id", "event_type", "payload_json"},
    "domain_event_handler_executions": {"id", "event_id", "handler_name", "status"},
    "consolidation_queue": {"id", "board_id", "artifact_type", "artifact_id", "status", "work_kind", "payload"},
}
_MAX_PAYLOAD_CHARACTERS = 131_072
_MAX_PAYLOAD_BYTES_TOTAL = 64 * 1024 * 1024


def inspect_sprint_retirement_work(
    connection: Connection, *, remaining_rows: int, sprint_boards: dict[str, str],
) -> SprintWorkInventory:
    schema = inspect(connection)
    tables = set(schema.get_table_names())
    for table, required in _REQUIRED.items():
        if table not in tables or not required <= {c["name"] for c in schema.get_columns(table)}:
            raise SprintRetirementWorkError(f"sprint_retirement_work_schema_incomplete:{table}")
    counts = {table: 0 for table in _REQUIRED}
    consumed = 0
    payload_bytes = 0
    items: list[SprintRetirementWorkItem] = []
    related_events = {}

    def consume(table):
        nonlocal consumed
        consumed += 1
        counts[table] += 1
        if consumed > remaining_rows:
            raise SprintRetirementWorkError("sprint_retirement_work_row_limit")

    def rows(query):
        # Async SQLite adapters otherwise buffer fetchall; PostgreSQL drivers can
        # do the same. Stream a bounded batch and fetch one overflow sentinel.
        statement = text(query + " LIMIT :inventory_limit").execution_options(stream_results=True, yield_per=16)
        with connection.execute(statement, {"inventory_limit": remaining_rows - consumed + 1}) as result:
            yield from result.mappings()

    def decode(raw, length, identity, *, nullable=False):
        nonlocal payload_bytes
        if length is not None and length > _MAX_PAYLOAD_CHARACTERS:
            raise SprintRetirementWorkError(f"sprint_retirement_work_payload_limit:{identity}")
        if raw is None and nullable:
            return None
        if not isinstance(raw, str):
            raise SprintRetirementWorkError(f"sprint_retirement_work_payload_invalid:{identity}")
        payload_bytes += len(raw.encode("utf-8"))
        if payload_bytes > _MAX_PAYLOAD_BYTES_TOTAL:
            raise SprintRetirementWorkError("sprint_retirement_work_payload_total_limit")
        try:
            return json.loads(raw)
        except (ValueError, RecursionError) as error:
            raise SprintRetirementWorkError(f"sprint_retirement_work_payload_invalid:{identity}") from error

    # CASE bounds each transferred payload before decoding. No LIKE/substring
    # filter can silently omit a nested reference or a new event contract.
    for row in rows(f"""
        SELECT id, board_id, event_type, length(CAST(payload_json AS TEXT)) AS payload_length,
               CASE WHEN length(CAST(payload_json AS TEXT)) <= {_MAX_PAYLOAD_CHARACTERS}
                    THEN CAST(payload_json AS TEXT) END AS payload
        FROM domain_events ORDER BY id
    """):
        consume("domain_events")
        payload = decode(row["payload"], row["payload_length"], row["id"])
        try:
            disposition = classify_historical_sprint_event(row["event_type"], payload)
        except ValueError as error:
            raise SprintRetirementWorkError(f"sprint_retirement_work_event_invalid:{row['id']}:{error}") from error
        if disposition.sprint_ids or disposition.action == "review":
            if any(identity in sprint_boards and sprint_boards[identity] != row["board_id"] for identity in disposition.sprint_ids):
                disposition = replace(disposition, action="review", reason="cross_board_sprint_reference")
            related_events[row["id"]] = (row["board_id"], row["event_type"], disposition)
            items.append(SprintRetirementWorkItem("domain_events", row["id"], row["board_id"],
                disposition.action, disposition.reason, disposition.sprint_ids))

    for row in rows("""
        SELECT execution.id, execution.event_id, execution.handler_name, execution.status,
               event.id AS parent_id
        FROM domain_event_handler_executions execution
        LEFT JOIN domain_events event ON event.id = execution.event_id ORDER BY execution.id
    """):
        consume("domain_event_handler_executions")
        if row["parent_id"] is None:
            raise SprintRetirementWorkError(f"sprint_retirement_work_orphan_execution:{row['id']}:{row['event_id']}")
        event = related_events.get(row["event_id"])
        if event is not None:
            board_id, event_type, disposition = event
            action, reason = classify_historical_sprint_execution(event_type, disposition,
                handler_name=row["handler_name"], status=row["status"])
            items.append(SprintRetirementWorkItem("domain_event_handler_executions", row["id"], board_id,
                action, reason, disposition.sprint_ids, parent_event_id=row["event_id"],
                handler_name=row["handler_name"], status=row["status"]))

    for row in rows(f"""
        SELECT id, board_id, artifact_type, artifact_id, status, work_kind,
               length(CAST(payload AS TEXT)) AS payload_length,
               CASE WHEN length(CAST(payload AS TEXT)) <= {_MAX_PAYLOAD_CHARACTERS}
                    THEN CAST(payload AS TEXT) END AS payload
        FROM consolidation_queue ORDER BY id
    """):
        consume("consolidation_queue")
        payload = decode(row["payload"], row["payload_length"], row["id"], nullable=True)
        try:
            disposition = classify_historical_sprint_queue(artifact_type=row["artifact_type"], artifact_id=row["artifact_id"],
                work_kind=row["work_kind"], status=row["status"], payload=payload)
        except ValueError as error:
            raise SprintRetirementWorkError(f"sprint_retirement_work_queue_invalid:{row['id']}") from error
        if not disposition.sprint_ids:
            continue
        if any(identity in sprint_boards and sprint_boards[identity] != row["board_id"] for identity in disposition.sprint_ids):
            disposition = replace(disposition, action="review", reason="cross_board_sprint_reference")
        items.append(SprintRetirementWorkItem("consolidation_queue", row["id"], row["board_id"],
            disposition.action, disposition.reason, disposition.sprint_ids, status=row["status"]))
    return SprintWorkInventory(tuple(sorted(counts.items())), tuple(items))
