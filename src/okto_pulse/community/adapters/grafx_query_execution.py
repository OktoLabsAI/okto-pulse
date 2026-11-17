"""One Board query budget shared by Community's synchronous Grafx adapters."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import time

from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout


class CommunityGraphQueryExecution:
    def __init__(self):
        self._active: ContextVar[tuple[str, float, bool] | None] = ContextVar(
            'grafx_foreground_query_deadline', default=None)

    @contextmanager
    def scope(self, board_id: str, *, timeout_ms: int,
              deadline: float | None = None, existing_only: bool = False):
        if type(board_id) is not str or not board_id.strip():
            raise ValueError('query_board_required')
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 30000:
            raise ValueError('query_timeout_ms_requires_1_to_30000')
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise ValueError('query_deadline_requires_finite_monotonic_time')
        if type(existing_only) is not bool:
            raise ValueError('query_existing_only_requires_boolean')
        parent = self._active.get()
        local_deadline = time.monotonic() + timeout_ms / 1000
        deadline = min(local_deadline, deadline) if deadline is not None else local_deadline
        if parent is not None:
            self.remaining(board_id)
            deadline = min(deadline, parent[1])
            existing_only = existing_only or parent[2]
        token = self._active.set((board_id, deadline, existing_only))
        try:
            self.remaining(board_id)
            yield
            # CPU shaping or a provider fallback may have consumed the budget.
            self.remaining(board_id)
        finally:
            self._active.reset(token)

    def remaining(self, board_id: str) -> float | None:
        active = self._active.get()
        if active is None:
            return None
        if board_id != active[0]:
            raise ValueError('query_board_scope_mismatch')
        remaining = active[1] - time.monotonic()
        if remaining <= 0:
            raise GraphQueryTimeout('Board query execution deadline exceeded.')
        return remaining

    def deadline(self, board_id: str) -> float | None:
        self.remaining(board_id)
        active = self._active.get()
        return None if active is None else active[1]

    def existing_only(self, board_id: str) -> bool:
        self.remaining(board_id)
        active = self._active.get()
        return active is not None and active[2]


class GrafxDeadlineReader:
    """Narrow execute-only view; lifetime stays owned by the native transaction."""
    def __init__(self, reader, board_id, remaining):
        self._reader = reader
        self._board_id = board_id
        self._remaining = remaining

    def check(self):
        return self._remaining(self._board_id) if self._remaining is not None else None

    def execute(self, query, params=None):
        left = self.check()
        result = (self._reader.execute(query, params) if left is None else
                  self._reader.execute(query, params, timeout_seconds=left))
        self.check()
        return result
