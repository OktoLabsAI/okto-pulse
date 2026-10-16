"""Bound the existing SQLite source reader without a second source classifier."""

import sqlite3
import time

from okto_pulse.core.application.rebuild_ports import SourceObservationBudget, SourceReadFailure


class BoundedSourceConnection:
    def __init__(self, connection: sqlite3.Connection, budget: SourceObservationBudget, *, deadline_at=None):
        self.connection = connection
        self.budget = budget
        self.deadline = time.monotonic() + budget.timeout_seconds if deadline_at is None else deadline_at
        self.rows = 0
        self.bytes = 0
        # SQLite rejects oversized values/encoded rows before returning them to
        # Python. The cursor also enforces one cumulative budget across queries,
        # including source catalog and cross-source fingerprint reads.
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, budget.max_bytes)
        connection.set_progress_handler(lambda: int(time.monotonic() >= self.deadline), 1000)

    def check(self):
        if time.monotonic() >= self.deadline:
            raise SourceReadFailure("source observation deadline exceeded", cause_type="observation_timeout")

    def execute(self, statement, parameters=()):
        self.check()
        return _BoundedSourceCursor(self, self.connection.execute(statement, parameters))

    def observe(self, row):
        self.check()
        if row is None:
            return row
        self.rows += 1
        self.bytes += sum(
            len(value.encode("utf-8")) if isinstance(value, str)
            else len(value) if isinstance(value, bytes) else 8
            for value in row
        )
        if self.rows > self.budget.max_rows or self.bytes > self.budget.max_bytes:
            raise SourceReadFailure("source observation volume exceeded", cause_type="observation_volume_exceeded")
        return row


class _BoundedSourceCursor:
    def __init__(self, owner, cursor):
        self.owner = owner
        self.cursor = cursor

    def fetchone(self):
        return self.owner.observe(self.cursor.fetchone())

    def fetchall(self):
        return list(self)

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row
