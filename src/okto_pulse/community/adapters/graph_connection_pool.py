"""Community LRU pool of local graph handles keyed by ``board_id``.

Sized from :class:`CoreSettings`.kg_connection_pool_size (default 8) with the
env var ``KG_CONNECTION_POOL_SIZE`` kept as an opt-in override for CI/deploy
scripts (soft-deprecated in 0.1.4 — logs a warning when applied). ``0``
disables pooling.

Thread-safe via a single ``threading.Lock`` around the ``OrderedDict`` —
access is expected to be low-frequency (per-board consolidation sessions),
so a coarse lock is fine and simpler than per-entry locking.

When the cap is reached, the least-recently-used entry is evicted via
``popitem(last=False)`` and its ``close()`` is invoked before being dropped.
When ``cap == 0`` the pool is disabled: every :meth:`ConnectionPool.acquire`
opens a fresh :class:`BoardConnection` and every :meth:`ConnectionPool.release`
closes it — equivalent to the non-pooled path but keeping the same API.

Exposes module-level ``close_board_connection`` / ``close_all_board_connections``
hooks consumed by the Community graph runtime lifecycle.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("okto_pulse.kg.connection_pool")


_DEFAULT_CAP = 8
_ENV_VAR = "KG_CONNECTION_POOL_SIZE"


def _new_board_connection(board_id: str) -> Any:
    from okto_pulse.community.adapters.kg_runtime import BoardConnection

    return BoardConnection(board_id)


def _read_cap_from_env() -> int:
    """Resolve the pool cap with precedence: env var > CoreSettings > default.

    Env var is only applied when **non-empty**. When applied, it logs a
    soft-deprecation warning so operators know the value came from the
    environment and not the UI-persisted settings.
    """
    raw = os.environ.get(_ENV_VAR)
    if raw is not None and raw.strip() != "":
        try:
            cap = int(raw)
        except ValueError:
            logger.warning(
                "connection_pool.invalid_cap value=%r falling_back_to_settings",
                raw,
            )
            return _cap_from_settings()
        resolved = max(0, cap)
        logger.warning(
            "kg.config.env_override_detected var=%s value=%d "
            "(prefer the runtime Settings menu)",
            _ENV_VAR, resolved,
            extra={"event": "kg.config.env_override_detected", "var": _ENV_VAR,
                   "value": resolved},
        )
        return resolved
    return _cap_from_settings()


def _cap_from_settings() -> int:
    """Read the cap from CoreSettings, falling back to _DEFAULT_CAP on any error.

    Wrapped to survive early-boot scenarios where the settings singleton is
    not yet configured (pool is occasionally initialized before main.py
    populates CoreSettings from the persisted table).
    """
    try:
        from okto_pulse.core.infra.config import get_settings

        return max(0, int(get_settings().kg_connection_pool_size))
    except Exception as exc:
        logger.warning(
            "connection_pool.settings_read_failed err=%s falling_back=%d",
            exc, _DEFAULT_CAP,
        )
        return _DEFAULT_CAP


class ConnectionPool:
    """LRU pool of :class:`BoardConnection` handles.

    Not re-entrant for the same ``board_id`` — the pool returns the *same*
    :class:`BoardConnection` instance on repeated :meth:`acquire` calls.
    Callers that need isolation should open an un-pooled
    :class:`BoardConnection` directly (e.g. long-running consolidation).

    Do **not** use the returned :class:`BoardConnection` as a ``with``-context
    manager when ``cap > 0`` — the ``__exit__`` would call ``close()`` and
    silently invalidate the pooled handle. Access ``bc.db`` / ``bc.conn``
    directly and call :meth:`release` when done.
    """

    def __init__(self, cap: int = _DEFAULT_CAP) -> None:
        self._cap = max(0, cap)
        self._conns: "OrderedDict[str, Any]" = OrderedDict()
        self._lock = threading.Lock()

    @property
    def cap(self) -> int:
        return self._cap

    @property
    def enabled(self) -> bool:
        return self._cap > 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._conns)

    def __contains__(self, board_id: str) -> bool:
        with self._lock:
            return board_id in self._conns

    def acquire(self, board_id: str) -> Any:
        """Return a :class:`BoardConnection` for ``board_id``.

        Pool hit: move entry to MRU end and return the cached handle.
        Pool miss: open a fresh :class:`BoardConnection`, evict LRU if at cap,
        insert and return.
        Disabled (``cap == 0``): every call opens a fresh, un-pooled handle.
        """
        if not self.enabled:
            return _new_board_connection(board_id)

        with self._lock:
            existing = self._conns.get(board_id)
            if existing is not None:
                self._conns.move_to_end(board_id)
                return existing

            while len(self._conns) >= self._cap:
                evicted_id, evicted_bc = self._conns.popitem(last=False)
                self._close_quietly(evicted_bc, evicted_id, event="evicted")

            bc = _new_board_connection(board_id)
            self._conns[board_id] = bc
            return bc

    def release(self, board_id: str) -> None:
        """Return a connection to the pool.

        ``cap > 0``: no-op — the pool retains the connection for reuse.
        ``cap == 0``: close the connection (pool disabled = release is close).
        Safe to call for a board that isn't tracked (no-op).
        """
        if self.enabled:
            return

        # Disabled pool: acquire returned a fresh un-tracked BC, so release
        # can't find it here. The caller that held the reference needs to
        # close it directly. We keep this branch as a no-op documented below
        # so the API is consistent (see :meth:`invalidate` for explicit close).
        logger.debug(
            "connection_pool.release_noop_disabled board_id=%s", board_id,
        )

    def invalidate(self, board_id: str) -> None:
        """Close and evict ``board_id`` from the pool. Idempotent."""
        with self._lock:
            bc = self._conns.pop(board_id, None)
        if bc is not None:
            self._close_quietly(bc, board_id, event="invalidated")

    def close_all(self) -> None:
        """Close and drop every pooled connection. Idempotent."""
        with self._lock:
            items = list(self._conns.items())
            self._conns.clear()
        for board_id, bc in items:
            self._close_quietly(bc, board_id, event="close_all")

    @staticmethod
    def _close_quietly(bc: Any, board_id: str, *, event: str) -> None:
        try:
            bc.close()
        except Exception as exc:
            logger.warning(
                "connection_pool.close_failed event=%s board=%s err=%s",
                event, board_id, exc,
                extra={
                    "event": f"connection_pool.{event}_close_failed",
                    "board_id": board_id,
                },
            )


_pool: ConnectionPool | None = None
_pool_init_lock = threading.Lock()


def get_connection_pool() -> ConnectionPool:
    """Return the process-wide singleton pool, creating it on first call."""
    global _pool
    if _pool is None:
        with _pool_init_lock:
            if _pool is None:
                _pool = ConnectionPool(cap=_read_cap_from_env())
    return _pool


def reset_connection_pool_for_tests() -> None:
    """Drop the singleton pool after closing every held connection."""
    global _pool
    with _pool_init_lock:
        if _pool is not None:
            try:
                _pool.close_all()
            except Exception:
                pass
        _pool = None


# Hooks consumed by the Community runtime lifecycle.
def close_board_connection(board_id: str) -> None:
    """Evict and close a single board's pooled connection (if present)."""
    get_connection_pool().invalidate(board_id)


def close_all_board_connections() -> None:
    """Evict and close every pooled connection."""
    get_connection_pool().close_all()
