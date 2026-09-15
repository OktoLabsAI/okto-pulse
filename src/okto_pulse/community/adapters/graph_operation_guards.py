"""Engine-neutral per-board lifetime pins and exclusive storage windows.

Ordinary readers/writers share pins concurrently. Only lifecycle/storage
replacement drains them; a timeout fails closed without touching the store.
Core write authority is revalidated by the calling Community boundary.
"""

from __future__ import annotations
import threading
from contextlib import contextmanager
from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention

_CLOSE_DRAIN_TIMEOUT_S = 5.0
_READER_ENTER_TIMEOUT_S = 10.0
_board_close_guards = {}
_board_close_guards_lock = threading.Lock()


class BoardCloseInProgressError(RuntimeError):
    """KGD-01 C6: entrada de leitor recusada � janela de close ativa.

    Levantada por ``_BoardCloseGuard.reader_enter`` quando a janela closing
    do board n�o terminou dentro de ``_READER_ENTER_TIMEOUT_S``. Callers de
    :class:`BoardConnection` devem PROPAGAR: falhar a opera��o � melhor que
    o fail-open antigo (entrar mesmo assim e arriscar use-after-close em
    handle C++)."""

    def __init__(self, board_id: str, timeout_s: float) -> None:
        self.board_id = board_id
        self.timeout_s = timeout_s
        super().__init__(
            f"board {board_id}: close do graph em andamento � entrada de "
            f"leitor recusada ap�s {timeout_s:.1f}s (fail-closed, KGD-01 C6; "
            "re-tente a opera��o)"
        )


class _BoardCloseGuard:
    __slots__ = (
        "board_id",
        "_cond",
        "_readers",
        "_owner_readers",
        "_closing",
        "_closing_owner_thread_id",
        "_close_serial_lock",
    )

    def __init__(self, board_id: str = "") -> None:
        self.board_id = board_id
        self._cond = threading.Condition()
        self._readers = 0
        self._owner_readers = 0
        self._closing = False
        self._closing_owner_thread_id: int | None = None
        # KGD-01 C6 (item 3): serializa janelas closing() por board � duas
        # janelas simult�neas n�o podem mais se sobrepor.
        self._close_serial_lock = threading.Lock()

    def reader_enter(self) -> None:
        """Registra um leitor; FAIL-CLOSED (KGD-01 C6): bloqueia (bounded)
        enquanto um close drena e levanta :class:`BoardCloseInProgressError`
        se a janela closing continuar ativa ap�s o timeout (lookup do m�dulo
        em runtime para os testes encurtarem via monkeypatch)."""
        timeout = _READER_ENTER_TIMEOUT_S
        with self._cond:
            ok = self._cond.wait_for(lambda: not self._closing, timeout)
            if not ok:
                raise BoardCloseInProgressError(self.board_id, timeout)
            self._readers += 1

    def reader_exit(self) -> None:
        with self._cond:
            self._readers = max(0, self._readers - 1)
            self._cond.notify_all()

    def pin_reader_from_closing_owner(self) -> None:
        """Atomically downgrade an exclusive owner into a retained reader.

        A fixed logical snapshot is opened only after the exclusive window has
        drained all prior graph operations.  Pinning it here, before that
        window reopens, prevents a lifecycle close from reaching the native
        database while the snapshot is consumed outside the short freeze.
        """

        with self._cond:
            if (
                not self._closing
                or self._closing_owner_thread_id != threading.get_ident()
            ):
                raise RuntimeError("board_graph_snapshot_pin_requires_window_owner")
            self._readers += 1

    def owner_enter(self) -> None:
        """Registro do "dono da janela" (KGD-01 C6, item 4): o CHECKPOINT
        roda DENTRO da janela exclusiva de closing � registr�-lo como reader
        normal deadlockaria (esperaria o fim da pr�pria janela). Owners s�o
        registrados, mas n�o contam no dreno nem s�o barrados pela janela."""
        with self._cond:
            self._owner_readers += 1

    def owner_exit(self) -> None:
        with self._cond:
            self._owner_readers = max(0, self._owner_readers - 1)
            self._cond.notify_all()

    @property
    def readers(self) -> int:
        with self._cond:
            return self._readers

    @property
    def owner_readers(self) -> int:
        with self._cond:
            return self._owner_readers

    @contextmanager
    def closing(self, timeout: float = _CLOSE_DRAIN_TIMEOUT_S):
        """Janela exclusiva de close: barra leitores novos e drena os ativos.

        Yields ``(drained, stuck_readers)``. Com ``drained=False`` o caller
        DEVE fazer fail-closed (n�o fechar / adiar); apenas o caminho de
        shutdown (``force_after_drain_timeout``) pode prosseguir, com o log
        estruturado ``kg.close_guard.forced_on_shutdown``.

        KGD-01 C6 (item 3): janelas s�o serializadas por board. Se outra
        closing() estiver ativa, esta espera at� ``timeout`` pelo lock
        serial; sem sucesso, yield ``(False, readers)`` sem abrir janela
        pr�pria (fail-closed para o caller).
        """
        acquired = self._close_serial_lock.acquire(timeout=max(0.0, timeout))
        if not acquired:
            with self._cond:
                stuck = self._readers
            yield False, stuck
            return
        try:
            with self._cond:
                self._closing = True
                self._closing_owner_thread_id = threading.get_ident()
                drained = self._cond.wait_for(lambda: self._readers == 0, timeout)
                stuck = self._readers
            try:
                yield drained, stuck
            finally:
                with self._cond:
                    self._closing = False
                    self._closing_owner_thread_id = None
                    self._cond.notify_all()
        finally:
            self._close_serial_lock.release()


class BoardGraphOperationPin:
    """One idempotently releasable reader pin created by a mutation owner."""

    __slots__ = ("_guard", "_released")

    def __init__(self, guard: _BoardCloseGuard) -> None:
        self._guard = guard
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> bool:
        if self._released:
            return False
        self._released = True
        self._guard.reader_exit()
        return True


def _get_close_guard(board_id: str) -> _BoardCloseGuard:
    guard = _board_close_guards.get(board_id)
    if guard is not None:
        return guard
    with _board_close_guards_lock:
        guard = _board_close_guards.get(board_id)
        if guard is None:
            guard = _BoardCloseGuard(board_id)
            _board_close_guards[board_id] = guard
        return guard


@contextmanager
def board_graph_operation_window(board_id: str):
    """Pin one board operation against lifecycle close and storage mutation.

    This is the backend-neutral reader side of :func:`board_storage_mutation_window`.
    Every routed participant enters for the complete handle lifetime. Storage
    replacement drains these pins; ordinary reads and writes remain concurrent.
    """

    if type(board_id) is not str or not board_id:
        raise ValueError("board_graph_operation_window_invalid")
    guard = _get_close_guard(board_id)
    guard.reader_enter()
    try:
        yield
    finally:
        guard.reader_exit()


def pin_board_graph_operation_from_mutation_window(
    board_id: str,
) -> BoardGraphOperationPin:
    """Retain a reader pin while the current thread owns the exclusive window.

    This low-level handoff is for a fixed snapshot opened inside
    :func:`board_storage_mutation_window`.  The caller must release the returned
    pin only after the snapshot's native connection is fully closed.
    """

    if type(board_id) is not str or not board_id:
        raise ValueError("board_graph_operation_pin_invalid")
    guard = _get_close_guard(board_id)
    guard.pin_reader_from_closing_owner()
    return BoardGraphOperationPin(guard)


@contextmanager
def board_storage_mutation_window_unguarded(
    board_id: str, *, phase: str, drain_timeout: float = 30.0
):
    """Drain lifetime pins under caller-owned writer authority; never force close."""
    if not board_id or not phase or drain_timeout <= 0:
        raise ValueError("board_storage_mutation_window_invalid")
    guard = _get_close_guard(board_id)
    with guard.closing(timeout=drain_timeout) as (drained, stuck):
        if not drained:
            raise GraphLockContention(
                "board storage mutation timed out draining graph readers",
                details={
                    "board_id": board_id,
                    "phase": phase,
                    "stuck_readers": stuck,
                    "timeout_ms": int(drain_timeout * 1000),
                    "error_code": GraphLockContention.code,
                    "retryable": GraphLockContention.retryable,
                },
            )
        yield


# The routed caller supplies logical writer authority. There is deliberately
# no process-wide native-engine writer mutex: independent stores stay parallel.
board_storage_mutation_window = board_storage_mutation_window_unguarded
