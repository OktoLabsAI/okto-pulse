"""Normalize legacy Card Started rows after SQLite admission and before ORM readers.

Each board commits its row changes, audit trail and projection intent together.
The domain-event dispatcher owns all later KG work; this module never opens a
graph connection or awaits a projection while holding SQLite's writer lock.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from okto_pulse.core.events.types import (
    CARD_STATUS_MIGRATION_ID,
    CardStatusNormalized,
)


logger = logging.getLogger("okto_pulse.community.legacy_card_status_normalization")


async def normalize_legacy_card_status(engine: object) -> int:
    """Return the number of current Started rows normalized on this startup."""

    normalized = 0
    async with engine.connect() as connection:
        # A second process may start while the first is migrating a large
        # board. Wait for its startup transaction, then re-read rows under our
        # own writer lock; never duplicate audit or projection intents.
        await connection.exec_driver_sql("PRAGMA busy_timeout = 120000")
        boards = (
            await connection.exec_driver_sql(
                "SELECT DISTINCT board_id FROM cards ORDER BY board_id"
            )
        ).scalars().all()
        await connection.rollback()

        for board_id in boards:
            started_at = time.perf_counter()
            try:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
                rows = (
                    await connection.exec_driver_sql(
                        "SELECT id, spec_id, position, policy_version, archived, "
                        "pre_archive_status FROM cards "
                        "WHERE board_id = ? AND status = 'started' "
                        "ORDER BY COALESCE(position, 9223372036854775807), id",
                        (board_id,),
                    )
                ).all()
                base = (
                    await connection.exec_driver_sql(
                        "SELECT COALESCE(MAX(position), -1) FROM cards "
                        "WHERE board_id = ? AND status = 'in_progress'",
                        (board_id,),
                    )
                ).scalar_one()
                for offset, row in enumerate(rows, start=1):
                    card_id, spec_id, old_position, old_version, archived, _ = row
                    new_position = base + offset
                    result = await connection.exec_driver_sql(
                        "UPDATE cards SET status = 'in_progress', position = ?, "
                        "pre_archive_status = CASE WHEN pre_archive_status = 'started' "
                        "THEN 'in_progress' ELSE pre_archive_status END, "
                        "policy_version = policy_version + 1 "
                        "WHERE id = ? AND board_id = ? AND status = 'started'",
                        (new_position, card_id, board_id),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("card_status_normalization_concurrent_change")
                    event = CardStatusNormalized(
                        event_id=str(uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"okto-pulse:{CARD_STATUS_MIGRATION_ID}:{board_id}:{card_id}",
                        )),
                        board_id=board_id,
                        actor_type="system",
                        card_id=card_id,
                        spec_id=spec_id,
                        old_position=old_position,
                        new_position=new_position,
                        old_policy_version=old_version,
                        new_policy_version=old_version + 1,
                        archived=bool(archived),
                    )
                    details = {
                        "migration_id": CARD_STATUS_MIGRATION_ID,
                        "normalization_version": 1,
                        "from_status": "started",
                        "to_status": "in_progress",
                        "old_position": old_position,
                        "new_position": new_position,
                        "old_policy_version": old_version,
                        "new_policy_version": old_version + 1,
                        "event_id": event.event_id,
                        "archived": bool(archived),
                    }
                    await connection.exec_driver_sql(
                        "INSERT INTO activity_logs "
                        "(id, board_id, card_id, action, actor_type, actor_id, "
                        "actor_name, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()), board_id, card_id,
                            "card_status_normalized", "system", "pulse-startup",
                            "Pulse startup", json.dumps(details),
                        ),
                    )
                    await connection.exec_driver_sql(
                        "INSERT INTO domain_events "
                        "(id, event_type, board_id, actor_id, actor_type, "
                        "payload_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            event.event_id, event.event_type, board_id, None,
                            "system", json.dumps(event.payload_for_storage()),
                            event.occurred_at.isoformat(sep=" "),
                        ),
                    )
                    await connection.exec_driver_sql(
                        "INSERT INTO domain_event_handler_executions "
                        "(id, event_id, handler_name, status, attempts) "
                        "VALUES (?, ?, 'ConsolidationEnqueuer', 'pending', 0)",
                        (str(uuid.uuid4()), event.event_id),
                    )

                # The archived restore snapshot must never reintroduce Started.
                # These rows do not change their current status or KG projection.
                restore_rows = (
                    await connection.exec_driver_sql(
                        "SELECT id, status, policy_version FROM cards "
                        "WHERE board_id = ? AND pre_archive_status = 'started' "
                        "ORDER BY id",
                        (board_id,),
                    )
                ).all()
                for card_id, current_status, old_version in restore_rows:
                    result = await connection.exec_driver_sql(
                        "UPDATE cards SET pre_archive_status = 'in_progress', "
                        "policy_version = policy_version + 1 "
                        "WHERE id = ? AND board_id = ? "
                        "AND pre_archive_status = 'started'",
                        (card_id, board_id),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("card_restore_status_normalization_concurrent_change")
                    await connection.exec_driver_sql(
                        "INSERT INTO activity_logs "
                        "(id, board_id, card_id, action, actor_type, actor_id, "
                        "actor_name, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(uuid.uuid4()), board_id, card_id,
                            "card_restore_status_normalized", "system", "pulse-startup",
                            "Pulse startup", json.dumps({
                                "migration_id": CARD_STATUS_MIGRATION_ID,
                                "normalization_version": 1,
                                "current_status": current_status,
                                "pre_archive_status_before": "started",
                                "pre_archive_status_after": "in_progress",
                                "old_policy_version": old_version,
                                "new_policy_version": old_version + 1,
                            }),
                        ),
                    )
                await connection.commit()
                normalized += len(rows)
                logger.info(
                    "card.status_normalization board=%s migration_id=%s "
                    "outcome=%s cards=%d restore_snapshots=%d duration_ms=%d",
                    board_id,
                    CARD_STATUS_MIGRATION_ID,
                    "committed" if rows or restore_rows else "noop",
                    len(rows),
                    len(restore_rows),
                    max(0, int((time.perf_counter() - started_at) * 1000)),
                )
            except BaseException:
                await connection.rollback()
                logger.exception(
                    "card.status_normalization board=%s migration_id=%s "
                    "outcome=rolled_back cards=0 duration_ms=%d",
                    board_id,
                    CARD_STATUS_MIGRATION_ID,
                    max(0, int((time.perf_counter() - started_at) * 1000)),
                )
                raise
    return normalized


__all__ = ["normalize_legacy_card_status"]
