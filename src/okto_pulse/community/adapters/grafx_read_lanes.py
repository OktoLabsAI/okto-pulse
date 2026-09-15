"""Operation-scoped scheduling hints, never graph or route authority."""
from contextlib import contextmanager
from threading import Lock
from collections.abc import Iterator


class GrafxReadLanes:
    """Prefer a less occupied existing participant; do not add handles or waits.

    Counts cover admitted adapter operations, including open and result shaping.
    They are hints: unscoped legacy callers and native lock holders still exist.
    Grafx's own participant/snapshot/lease protocol remains the only I/O authority.
    Board state exists only while at least one scoped operation is active.
    """

    def __init__(self, lanes: int):
        if type(lanes) is not int or lanes < 1:
            raise ValueError("lanes must be a positive integer")
        self._lanes = lanes
        self._next = 0
        self._active: dict[str, list[int]] = {}
        self._lock = Lock()

    @contextmanager
    def reserve(self, board_id: str) -> Iterator[int]:
        with self._lock:
            counts = self._active.setdefault(board_id, [0] * self._lanes)
            lane = min(range(self._lanes), key=lambda i: (counts[i], (i-self._next) % self._lanes))
            counts[lane] += 1
            self._next = (lane + 1) % self._lanes
        try:
            yield lane
        finally:
            with self._lock:
                counts[lane] -= 1
                if not any(counts):
                    del self._active[board_id]
