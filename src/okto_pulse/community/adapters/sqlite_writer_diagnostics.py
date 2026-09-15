"""Opt-in, parameter-free SQLite writer ownership diagnostics."""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from pathlib import Path

from sqlalchemy import event

logger = logging.getLogger(__name__)


def install_sqlite_writer_diagnostics(engine) -> None:
    """Track successful DML until transaction end; never log SQL or values.

    This is diagnostic evidence of a write transaction, not a global lock
    oracle: other processes/engines and writes hidden inside CTEs are not tracked.
    """
    if engine.url.get_backend_name() != "sqlite":
        return
    active: dict[int, tuple[float, str, str, str]] = {}
    sync = engine.sync_engine

    @event.listens_for(sync, "after_cursor_execute")
    def after_write(conn, cursor, statement, parameters, context, many):
        tokens = statement.lstrip().split(None, 1)
        operation = tokens[0].upper() if tokens else ""
        if operation not in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            return
        key = id(conn)
        if key in active:
            return
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        task_name = task.get_name() if task else "no-async-task"
        frames = traceback.extract_stack(limit=65)
        sites = ">".join(
            f"{Path(f.filename).name}:{f.lineno}:{f.name}"
            for f in frames
            if "okto_pulse" in f.filename and Path(f.filename).name != Path(__file__).name
        )[-1500:]
        active[key] = (time.monotonic(), task_name, operation, sites)
        logger.warning("db.sqlite.writer_started connection=%s task=%s operation=%s site=%s",
                    key, task_name, operation, sites)

    def end(conn):
        item = active.pop(id(conn), None)
        if item is not None:
            started, task, operation, _site = item
            logger.warning("db.sqlite.writer_end_requested connection=%s task=%s operation=%s age_s=%.3f",
                        id(conn), task, operation, time.monotonic() - started)

    event.listen(sync, "commit", end)
    event.listen(sync, "rollback", end)

    @event.listens_for(sync, "handle_error")
    def on_error(context):
        error = context.original_exception
        code = getattr(error, "sqlite_errorcode", None)
        if code is None or code & 0xFF not in {5, 6}:
            return
        now = time.monotonic()
        owners = [(key, round(now - started, 3), task, op, site)
                  for key, (started, task, op, site) in active.items()]
        logger.warning("db.sqlite.lock_failure code=%s name=%s tracked_writers=%s",
                       code, getattr(error, "sqlite_errorname", "unknown"), owners)
