"""Shared operation pins; exclusive generation/lifecycle drain, not a DB lock."""

from contextlib import contextmanager
from threading import Condition, RLock, get_ident, local
from time import monotonic

from okto_pulse.core.kg.interfaces.graph_errors import GraphUnavailable


class GlobalOperationGate:
    def __init__(self, *, timeout=10.0):
        self._condition = Condition(RLock())
        self._writer_owner = None
        self._writer_depth = 0
        self._pins = {}
        self._owner = None
        self._depth = 0
        self._waiting = 0
        self._timeout = timeout
        self._contexts = local()

    def _wait(self, predicate):
        deadline = monotonic() + self._timeout
        while not predicate():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise GraphUnavailable(
                    "Global graph operation drain timed out.",
                    details={"reason": "global_operation_drain_timeout"},
                )
            self._condition.wait(remaining)

    @contextmanager
    def operation(self, *, write=False):
        # Only writes keep the existing local writer ordering; readers do not.
        ident = get_ident()
        with self._condition:
            self._wait(
                lambda: (
                    (not write or self._writer_owner in (None, ident))
                    and (
                        self._owner == ident
                        or (
                            self._owner is None
                            and (not self._waiting or ident in self._pins)
                        )
                    )
                )
            )
            self._pins[ident] = self._pins.get(ident, 0) + 1
            if write:
                self._writer_owner = ident
                self._writer_depth += 1
        try:
            yield
        finally:
            with self._condition:
                self._pins[ident] -= 1
                if not self._pins[ident]:
                    del self._pins[ident]
                if write:
                    self._writer_depth -= 1
                    if not self._writer_depth:
                        self._writer_owner = None
                self._condition.notify_all()

    @contextmanager
    def exclusive(self):
        ident = get_ident()
        with self._condition:
            self._waiting += 1
            try:
                self._wait(
                    lambda: (
                        self._owner == ident
                        or (self._owner is None and not (self._pins.keys() - {ident}))
                    )
                )
                self._owner = ident
                self._depth += 1
            finally:
                self._waiting -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._depth -= 1
                if not self._depth:
                    self._owner = None
                self._condition.notify_all()

    def __enter__(self):
        context = self.exclusive()
        result = context.__enter__()
        if not hasattr(self._contexts, "stack"):
            self._contexts.stack = []
        self._contexts.stack.append(context)
        return result

    def __exit__(self, *exc):
        return self._contexts.stack.pop().__exit__(*exc)
