"""Cooperative, aggregate budgets for non-mutating metadata observation."""

from collections.abc import Callable
from pathlib import Path
import os


class FilesystemObservationLimit(OSError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class FilesystemObservationBudget:
    """Bound enumeration and document reads without inventing absence.

    The callback shares the enclosing observation's deadline. These bounds do
    not preempt a blocked OS call or provide an atomic filesystem snapshot.
    One instance belongs to one observation; privacy/recovery use no budget.
    """

    def __init__(self, remaining_seconds: Callable[[], float | None]) -> None:
        self._remaining_seconds = remaining_seconds
        self._entries = 0
        self._bytes = 0

    def check(self) -> None:
        remaining = self._remaining_seconds()
        if remaining is None or remaining <= 0:
            raise FilesystemObservationLimit("observation_timeout")

    def children(self, path: Path) -> tuple[Path, ...]:
        self.check()
        result = []
        # Path.iterdir eagerly collects names on Python 3.13.
        with os.scandir(path) as entries:
            for entry in entries:
                self.check()
                self._entries += 1
                if self._entries > 2000:
                    raise FilesystemObservationLimit("observation_entry_limit")
                result.append(Path(entry.path))
        self.check()
        return tuple(result)

    def read_text(self, path: Path, *, max_file_bytes: int) -> str:
        self.check()
        available = min(max_file_bytes, 4 * 1024 * 1024 - self._bytes)
        with path.open("rb") as stream:
            payload = stream.read(available + 1)
        self.check()
        if len(payload) > available:
            raise FilesystemObservationLimit("observation_byte_limit")
        self._bytes += len(payload)
        return payload.decode("utf-8")
