"""LadybugDB per-board graph schema — 11 node tables, 13 rel tables, 9 vector indexes.

Idempotent bootstrap: `bootstrap_board_graph(board_id)` creates or opens the
per-board LadybugDB file under `kg_base_dir/boards/{board_id}/graph.lbug`,
applies DDL, creates HNSW vector indexes for searchable node types, and
records the schema version on a Board meta node.
"""

from __future__ import annotations

import gc
import logging
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import ladybug as kuzu  # type: ignore
from typing import Any

from okto_pulse.core.kg import schema_contract as _schema_contract

logger = logging.getLogger("okto_pulse.kg.schema")

GRAPH_DB_FILENAME = "graph.lbug"
CORRUPT_DB_ERROR_MARKERS = (
    "checksum verification failed",
    "corrupted wal file",
    "wal file is corrupted",
    "invalid wal record",
    "not a valid lbug database file",
    "wal_record.cpp",
    "unreachable_code",
)
CAPI_SHARED_LIB_MISSING_MARKER = "could not find lbug c api shared library"



@dataclass(frozen=True)
class BoardGraphHandle:
    """Handle returned by bootstrap_board_graph — path + schema version."""

    board_id: str
    path: Path
    schema_version: str


# Bug d0f6bab2: process-wide cache of kuzu.Database per board path.
# Kùzu locks the .kuzu directory at the OS level while ANY Database object
# exists; spawning a new one for each BoardConnection guarantees lock
# contention as soon as two coroutines/threads touch the same board.
# Multiple kuzu.Connection instances over a single Database are safe and
# the supported pattern. The cache is freed by close_board_db_cache /
# close_all_connections (the rmtree paths).
_board_db_cache: "OrderedDict[str, Any]" = OrderedDict()
_board_db_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Close guard — coordenação leitor-escritor por board (spec 3d89c192, FR-5;
# KGD-01 C6: contrato FAIL-CLOSED)
# ---------------------------------------------------------------------------
#
# `close_board_db_cache` fecha o kuzu.Database compartilhado; uma
# kuzu.Connection viva sobre ele em outra thread é use-after-close em handle
# C++ (UB: crash/corrupção silenciosa — KGD-01 KB1/H3: o "escritor stale"
# que zera páginas interiores do WAL). Este guard registra TODO leitor
# (BoardConnection e conexões cruas via `registered_raw_connection`) e faz o
# close legítimo aguardar o dreno.
#
# KGD-01 C6 — fail-closed nas duas direções (o fail-open antigo era o
# produtor provável do escritor stale):
#   * close: se o dreno não completa, o Database NÃO é fechado — o caller
#     adia (log `kg.close_guard.deferred`). Só o caminho de shutdown pode
#     forçar (`force_after_drain_timeout=True` →
#     `kg.close_guard.forced_on_shutdown`).
#   * leitor novo: se a janela closing não termina dentro do timeout, a
#     entrada FALHA com `BoardCloseInProgressError` — falhar a operação é
#     estritamente melhor que entregar um handle prestes a ser fechado.
#   * janelas closing() são mutuamente exclusivas por board (lock serial).
#   * o CHECKPOINT roda DENTRO da janela exclusiva como "dono da janela"
#     (owner_enter/owner_exit): registrado, mas fora do dreno.

_CLOSE_DRAIN_TIMEOUT_S = 5.0
_READER_ENTER_TIMEOUT_S = 10.0
# Dreno maior do shutdown (KGD-01 C6): o teardown roda APÓS a parada dos
# workers, então um leitor que não sai nesse budget é vazado por definição —
# o shutdown então força o close com registro explícito.
_SHUTDOWN_CLOSE_DRAIN_TIMEOUT_S = 15.0


class BoardCloseInProgressError(RuntimeError):
    """KGD-01 C6: entrada de leitor recusada — janela de close ativa.

    Levantada por ``_BoardCloseGuard.reader_enter`` quando a janela closing
    do board não terminou dentro de ``_READER_ENTER_TIMEOUT_S``. Callers de
    :class:`BoardConnection` devem PROPAGAR: falhar a operação é melhor que
    o fail-open antigo (entrar mesmo assim e arriscar use-after-close em
    handle C++)."""

    def __init__(self, board_id: str, timeout_s: float) -> None:
        self.board_id = board_id
        self.timeout_s = timeout_s
        super().__init__(
            f"board {board_id}: close do graph em andamento — entrada de "
            f"leitor recusada após {timeout_s:.1f}s (fail-closed, KGD-01 C6; "
            "re-tente a operação)"
        )


class _BoardCloseGuard:
    __slots__ = (
        "board_id",
        "_cond",
        "_readers",
        "_owner_readers",
        "_closing",
        "_close_serial_lock",
    )

    def __init__(self, board_id: str = "") -> None:
        self.board_id = board_id
        self._cond = threading.Condition()
        self._readers = 0
        self._owner_readers = 0
        self._closing = False
        # KGD-01 C6 (item 3): serializa janelas closing() por board — duas
        # janelas simultâneas não podem mais se sobrepor.
        self._close_serial_lock = threading.Lock()

    def reader_enter(self) -> None:
        """Registra um leitor; FAIL-CLOSED (KGD-01 C6): bloqueia (bounded)
        enquanto um close drena e levanta :class:`BoardCloseInProgressError`
        se a janela closing continuar ativa após o timeout (lookup do módulo
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

    def owner_enter(self) -> None:
        """Registro do "dono da janela" (KGD-01 C6, item 4): o CHECKPOINT
        roda DENTRO da janela exclusiva de closing — registrá-lo como reader
        normal deadlockaria (esperaria o fim da própria janela). Owners são
        registrados, mas não contam no dreno nem são barrados pela janela."""
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
        DEVE fazer fail-closed (não fechar / adiar); apenas o caminho de
        shutdown (``force_after_drain_timeout``) pode prosseguir, com o log
        estruturado ``kg.close_guard.forced_on_shutdown``.

        KGD-01 C6 (item 3): janelas são serializadas por board. Se outra
        closing() estiver ativa, esta espera até ``timeout`` pelo lock
        serial; sem sucesso, yield ``(False, readers)`` sem abrir janela
        própria (fail-closed para o caller).
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
                drained = self._cond.wait_for(lambda: self._readers == 0, timeout)
                stuck = self._readers
            try:
                yield drained, stuck
            finally:
                with self._cond:
                    self._closing = False
                    self._cond.notify_all()
        finally:
            self._close_serial_lock.release()


_board_close_guards: dict[str, _BoardCloseGuard] = {}
_board_close_guards_lock = threading.Lock()


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
def registered_raw_connection(board_id: str, *, within_close_window: bool = False):
    """Conexão ``kuzu.Connection`` crua REGISTRADA no close guard (KGD-01 C6).

    Registro universal de leitores (item 4): todo uso de conexão crua neste
    módulo (probes de bootstrap/migração, DDL, health probes, CHECKPOINT)
    passa por aqui, para que um close legítimo nunca feche o Database sob
    uma conexão crua não-registrada.

    ``within_close_window=True`` é o caminho interno do "dono da janela"
    (``_execute_checkpoint_unguarded`` roda DENTRO da janela exclusiva de
    closing): registra no slot de owner do guard, que não conta no dreno nem
    é barrado pela janela — registrá-lo como reader normal deadlockaria.

    Yields ``(db, conn)``; fecha a Connection e desregistra o leitor no exit.
    Pode propagar :class:`BoardCloseInProgressError` do ``reader_enter``.
    """
    guard = _get_close_guard(board_id)
    if within_close_window:
        guard.owner_enter()
    else:
        guard.reader_enter()
    try:
        db = _open_kuzu_db_path_cached(board_kuzu_path(board_id))
        conn = kuzu.Connection(db)
        try:
            yield db, conn
        finally:
            try:
                conn.close()
            except Exception:
                pass
    finally:
        if within_close_window:
            guard.owner_exit()
        else:
            guard.reader_exit()


# Cap LRU do cache de Databases abertos (campo 2026-06-10): cada
# kuzu.Database aloca um buffer pool de até kg_kuzu_buffer_pool_mb (512MB
# default). Sem o close-por-commit (KGDL.01), TODO board visitado ficava com
# o Database aberto para sempre — um backfill multi-board acumulou 7+ buffer
# pools e o processo morreu por exaustão de memória nativa ("No more frame
# groups can be added to the allocator" + abort silencioso). O cap limita o
# pico de memória; a eviction LRU drena leitores via close guard antes de
# fechar. Override via env KG_DB_CACHE_CAP.
_BOARD_DB_CACHE_CAP_DEFAULT = 4


def _board_db_cache_cap() -> int:
    raw = os.environ.get("KG_DB_CACHE_CAP")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _BOARD_DB_CACHE_CAP_DEFAULT


def _open_kuzu_db_path_cached(path: Path) -> Any:
    """Return a singleton kuzu.Database for ``path``, opening on miss.

    Lookup keyed by ``str(path)`` so resolved-vs-symlink callers converge.
    Open is serialized through a module lock so two concurrent misses do
    not double-create (which would itself trigger the lock contention we
    are trying to avoid). Used by every per-board callsite (BoardConnection
    + bootstrap/migration probes) to guarantee a single OS-level lock per
    board path within the process.

    LRU: acesso move a entrada para o fim; ao exceder o cap, o Database
    menos-recentemente-usado é fechado (drenando leitores via close guard).
    """
    key = str(path)
    with _board_db_cache_lock:
        cached = _board_db_cache.get(key)
        if cached is not None:
            _board_db_cache.move_to_end(key)
            return cached

    # Evict FORA do cache lock: o drain do close guard pode esperar e não
    # pode bloquear cache hits de outros boards nesse intervalo. A eviction
    # é DISCRICIONÁRIA (ao contrário do close legítimo): um board com
    # leitores ativos é pulado — exceder o cap temporariamente é melhor que
    # fechar um Database em uso (use-after-close).
    with _board_db_cache_lock:
        over = len(_board_db_cache) - _board_db_cache_cap() + 1
        candidates = list(_board_db_cache.keys())[: max(0, over) + 4] if over > 0 else []
    evicted = 0
    needed = max(0, over)
    for evict_key in candidates:
        if evicted >= needed:
            break
        if _evict_board_db(evict_key):
            evicted += 1
    if needed > 0 and evicted < needed:
        logger.debug(
            "[KG] _board_db_cache over cap (todos os candidatos LRU com "
            "leitores ativos) — pico temporário aceito"
        )

    with _board_db_cache_lock:
        cached = _board_db_cache.get(key)
        if cached is not None:
            _board_db_cache.move_to_end(key)
            return cached
        # Cache miss: call the raw factory directly to avoid recursion.
        db = _open_kuzu_db(path)
        _board_db_cache[key] = db
        logger.debug(
            "[KG] _board_db_cache.miss path=%s size=%d",
            path, len(_board_db_cache),
        )
        return db


def _evict_board_db(key: str) -> bool:
    """Close and drop one cached Database (LRU eviction path).

    A eviction é DISCRICIONÁRIA e NUNCA fecha sob leitores ativos — se o
    dreno curto não completa, a eviction é abortada e o caller tenta outra
    vítima (já era fail-closed antes do KGD-01 C6; o close legítimo
    ``close_board_db_cache`` agora segue o mesmo contrato, com exceção do
    caminho forçado de shutdown). Retorna True quando o Database foi de
    fato fechado.
    """
    guard = _get_close_guard(Path(key).parent.name)
    with guard.closing(timeout=0.5) as (drained, _stuck):
        if not drained:
            return False
        with _board_db_cache_lock:
            db = _board_db_cache.pop(key, None)
        if db is None:
            return False
        logger.info(
            "kg.db_cache.lru_evicted board=%s cache_cap=%d",
            Path(key).parent.name, _board_db_cache_cap(),
            extra={
                "event": "kg.db_cache.lru_evicted",
                "board_id": Path(key).parent.name,
            },
        )
        try:
            db.close()
        except Exception as exc:
            logger.warning(
                "kg.db_cache.close_failed key=%s err=%s", key, exc,
                extra={"event": "kg.db_cache.close_failed", "key": key},
            )
        del db
    gc.collect()  # Windows: libera handles C++ antes do próximo open
    return True


# Dreno curto do close discricionário da higiene: longo o suficiente para
# leitores rápidos (queries pontuais) saírem, curto o suficiente para não
# atrasar o worker quando um scan longo (health/orphan) segura o board.
_HYGIENE_CLOSE_DRAIN_TIMEOUT_S = 2.0

# Dreno da janela exclusiva do CHECKPOINT (6º crash): só precisa cobrir
# queries pontuais em voo — leitores longos fazem o checkpoint ser adiado
# pelo fast-path antes mesmo de abrir a janela.
_CHECKPOINT_EXCLUSIVE_DRAIN_TIMEOUT_S = 1.0


def _execute_checkpoint_unguarded(path: Path) -> None:
    """Executa CHECKPOINT com conexão crua registrada como DONO DA JANELA —
    APENAS dentro de uma janela exclusiva do close guard (zero leitores).
    KGD-01 C6 (item 4): a conexão passa por ``registered_raw_connection``
    com ``within_close_window=True`` — registrá-la como reader normal daria
    deadlock (reader_enter esperaria o fim da própria janela closing que
    nos dá a exclusividade)."""
    board_id = path.parent.name
    with registered_raw_connection(board_id, within_close_window=True) as (
        _db,
        conn,
    ):
        conn.execute("CHECKPOINT")


def _close_cached_db_unguarded(board_id: str) -> None:
    """Fecha o Database cacheado do board SEM abrir janela de guard —
    APENAS para callers que JÁ estão dentro de uma janela exclusiva.
    Falha de close propaga (BR-3: o caller converte em step failure)."""
    key = str(board_kuzu_path(board_id))
    with _board_db_cache_lock:
        db = _board_db_cache.pop(key, None)
    if db is None:
        return
    db.close()
    del db
    gc.collect()  # Windows: libera handles C++ antes do próximo open


def try_close_board_db(
    board_id: str,
    *,
    drain_timeout: float | None = None,
    fast_path: bool = True,
) -> bool:
    """Close DISCRICIONÁRIO do Database do board (higiene de buffer).

    Fechar o Database com um leitor ativo é use-after-close em handle C++ →
    abort nativo do processo inteiro (campo 2026-06-10, 4º crash: o antigo
    fail-open do close guard disparado pela higiene periódica enquanto um
    health scan lia o board → exit 5). Se o dreno não completa, NADA é
    fechado e o caller adia a higiene para o próximo commit. Desde o KGD-01
    C6 o close legítimo (``close_board_db_cache``) segue o mesmo contrato
    fail-closed (só o shutdown força, com registro explícito). Fecha as
    conexões pooled primeiro (idle no pool = reader no guard). Retorna True
    quando o Database foi liberado (ou já não estava aberto).
    """
    try:
        from okto_pulse.community.adapters.graph_connection_pool import close_board_connection
    except ImportError:
        close_board_connection = None  # type: ignore[assignment]
    if close_board_connection is not None:
        try:
            close_board_connection(board_id)
        except Exception as exc:
            logger.warning(
                "kg.hygiene.pool_close_failed board=%s err=%s", board_id, exc,
                extra={
                    "event": "kg.hygiene.pool_close_failed",
                    "board_id": board_id,
                },
            )

    key = str(board_kuzu_path(board_id))
    guard = _get_close_guard(board_id)
    # Fast-path (review dcea02d): com leitor ativo AGORA, nem abre a janela
    # de closing — abrir custaria o dreno inteiro (2s) E bloquearia leitores
    # novos durante a janela, a cada commit re-armado enquanto um scan longo
    # estiver vivo. O skip custa ~0 e a higiene re-tenta no próximo commit.
    # Callers que preferem ESPERAR o dreno (ex.: close_reopen_probe do
    # rebuild) desligam o fast-path e passam um drain_timeout generoso.
    if fast_path:
        active = guard.readers
        if active > 0:
            logger.info(
                "kg.hygiene.close_skipped_active_readers board=%s "
                "stuck_readers=%d — higiene adiada para o próximo commit",
                board_id, active,
                extra={
                    "event": "kg.hygiene.close_skipped_active_readers",
                    "board_id": board_id,
                    "stuck_readers": active,
                },
            )
            return False
    effective_timeout = (
        _HYGIENE_CLOSE_DRAIN_TIMEOUT_S if drain_timeout is None else drain_timeout
    )
    with guard.closing(timeout=effective_timeout) as (drained, stuck):
        if not drained:
            logger.warning(
                "kg.hygiene.close_skipped_active_readers board=%s "
                "stuck_readers=%d — higiene adiada para o próximo commit",
                board_id, stuck,
                extra={
                    "event": "kg.hygiene.close_skipped_active_readers",
                    "board_id": board_id,
                    "stuck_readers": stuck,
                },
            )
            return False
        with _board_db_cache_lock:
            db = _board_db_cache.pop(key, None)
        if db is None:
            return True
        # Falha REAL do close propaga (≠ skip por leitor): o caller do
        # lifecycle converte em step failure → a queue entry não é ACKada
        # (BR-3 terminal preservada). Estado pós-falha é suspeito demais
        # para fingir durabilidade.
        db.close()
        del db
    gc.collect()  # Windows: libera handles C++ antes do próximo open
    return True


def _open_kuzu_db_cached(board_id: str, path: Path) -> Any:
    """Backwards-compat shim — delegates to ``_open_kuzu_db_path_cached``."""
    return _open_kuzu_db_path_cached(path)


def close_board_db_cache(
    board_id: str | None = None,
    *,
    force_after_drain_timeout: bool = False,
) -> None:
    """Drop the cached Database(s) so the .kuzu dir can be rmtree'd or re-opened.

    ``board_id=None`` closes every cached Database (rmtree everything).
    Specific board: only that one. Idempotent — already-evicted is a no-op.

    KGD-01 C6 — FAIL-CLOSED por default: se o dreno de leitores não completa
    dentro de ``_CLOSE_DRAIN_TIMEOUT_S``, o Database daquele board NÃO é
    fechado (log estruturado ``kg.close_guard.deferred``) e o board é pulado.
    Fechar com leitor vivo era o produtor provável do "escritor stale" que
    zera páginas interiores do WAL (KB1/H3) — o caminho fail-open antigo
    (``kg.close_guard.timeout``) foi REMOVIDO do modo runtime.

    ``force_after_drain_timeout=True`` é exclusivo do caminho de shutdown
    (``kg_shutdown.close_all_graphs_on_shutdown`` roda após a parada dos
    workers, então leitores remanescentes são vazados por definição): usa o
    dreno maior ``_SHUTDOWN_CLOSE_DRAIN_TIMEOUT_S`` e, se ainda houver
    leitor preso, fecha mesmo assim com o registro explícito
    ``kg.close_guard.forced_on_shutdown``.
    """
    # Conexões pooled IDLE são readers registrados (BoardConnection vive no
    # pool do core com o reader_enter do __init__ ativo) — evict primeiro
    # para que um pool ocioso não adie o close legítimo para sempre (mesmo
    # padrão do try_close_board_db). Best-effort: pool ausente/erro não
    # bloqueia o close (o dreno fail-closed ainda protege).
    try:
        from okto_pulse.community.adapters.graph_connection_pool import (
            close_all_board_connections,
            close_board_connection,
        )
    except ImportError:
        close_all_board_connections = None  # type: ignore[assignment]
        close_board_connection = None  # type: ignore[assignment]
    try:
        if board_id is None:
            if close_all_board_connections is not None:
                close_all_board_connections()
        elif close_board_connection is not None:
            close_board_connection(board_id)
    except Exception as exc:
        logger.warning(
            "kg.close_guard.pool_close_failed board=%s err=%s", board_id, exc,
            extra={
                "event": "kg.close_guard.pool_close_failed",
                "board_id": board_id,
            },
        )

    with _board_db_cache_lock:
        if board_id is None:
            keys = list(_board_db_cache.keys())
        else:
            target = str(board_kuzu_path(board_id))
            keys = [target] if target in _board_db_cache else []

    # timeouts com lookup do módulo em runtime para que testes possam
    # encurtá-los via monkeypatch.
    drain_timeout = (
        _SHUTDOWN_CLOSE_DRAIN_TIMEOUT_S
        if force_after_drain_timeout
        else _CLOSE_DRAIN_TIMEOUT_S
    )

    closed_any = False
    for key in keys:
        # Close guard (spec 3d89c192, FR-5/BR-2): drena leitores ativos do
        # board antes de fechar o Database compartilhado. O guard é
        # adquirido FORA do _board_db_cache_lock para não bloquear opens de
        # outros boards durante o dreno. A chave do cache é o path do
        # graph.lbug (…/boards/<board_id>/graph.lbug) — o nome do diretório
        # pai identifica o board.
        guard_board_id = Path(key).parent.name
        guard = _get_close_guard(guard_board_id)
        with guard.closing(timeout=drain_timeout) as (drained, stuck):
            if not drained:
                if not force_after_drain_timeout:
                    # KGD-01 C6: fail-closed — NÃO fecha com leitores vivos;
                    # pula este board (o caller re-tenta/adia).
                    logger.warning(
                        "kg.close_guard.deferred board=%s stuck_readers=%d "
                        "timeout_s=%.1f (fail-closed: Database NÃO fechado "
                        "com leitores ativos — close adiado)",
                        guard_board_id, stuck, drain_timeout,
                        extra={
                            "event": "kg.close_guard.deferred",
                            "board_id": guard_board_id,
                            "stuck_readers": stuck,
                            "timeout_s": drain_timeout,
                        },
                    )
                    continue
                # Shutdown: workers já pararam — leitor remanescente é
                # vazado; fechar mantém o progresso do teardown, com
                # registro explícito para investigação do vazamento.
                logger.warning(
                    "kg.close_guard.forced_on_shutdown board=%s "
                    "stuck_readers=%d timeout_s=%.1f (shutdown: fechando com "
                    "leitor vazado após o dreno — investigar leitor vazado)",
                    guard_board_id, stuck, drain_timeout,
                    extra={
                        "event": "kg.close_guard.forced_on_shutdown",
                        "board_id": guard_board_id,
                        "stuck_readers": stuck,
                        "timeout_s": drain_timeout,
                    },
                )
            with _board_db_cache_lock:
                db = _board_db_cache.pop(key, None)
            if db is None:
                continue
            closed_any = True
            try:
                db.close()
            except Exception as exc:
                logger.warning(
                    "kg.db_cache.close_failed key=%s err=%s", key, exc,
                    extra={"event": "kg.db_cache.close_failed", "key": key},
                )
            del db
    if closed_any:
        gc.collect()  # Windows: ensure C++ handles release before next caller


class BoardConnection:
    """Context-managed Kùzu per-board database connection.

    Reuses a process-wide cached :class:`kuzu.Database` (Bug d0f6bab2) and
    opens a fresh :class:`kuzu.Connection` per instance — multiple
    connections over one Database is the supported pattern and avoids
    OS-level file lock contention between concurrent workers.

    Use as a context manager::

        with BoardConnection(board_id) as (db, conn):
            conn.execute("MATCH (m:BoardMeta) RETURN count(m)")

    ``close()`` releases the Connection only; the cached Database survives
    for the next caller. Use :func:`close_board_db_cache` (or the
    higher-level :func:`close_all_connections`) when the .kuzu dir itself
    must be released — e.g. before ``rmtree`` or schema migration.
    """

    def __init__(self, board_id: str) -> None:
        self._board_id = board_id
        self._closed = False
        # Defensive: self-heal missing or partial graphs before we open our
        # own handle. No-op on hot boards (cache hit in
        # ensure_board_graph_bootstrapped). Roda ANTES do reader_enter porque
        # o bootstrap pode legitimamente fechar o cache deste board
        # (migração de schema) — registrar o leitor antes causaria um
        # auto-dreno de 5s em todo cold-open com migração.
        ensure_board_graph_bootstrapped(board_id)
        # Close guard (spec 3d89c192, FR-5): registra este leitor ANTES de
        # tocar no cache de Database, para que um close legítimo em curso
        # bloqueie a entrada (bounded) e nunca entregue um handle prestes a
        # ser fechado. reader_exit acontece em close().
        # KGD-01 C6: reader_enter é FAIL-CLOSED — se a janela closing não
        # terminar dentro do timeout, levanta BoardCloseInProgressError em
        # vez de entrar; callers devem propagar (falhar a operação > risco
        # de use-after-close).
        self._close_guard = _get_close_guard(board_id)
        self._close_guard.reader_enter()
        try:
            path = board_kuzu_path(board_id)
            logger.debug("[KG] BoardConnection.__init__ board_id=%s path=%s", board_id, path)
            self.db = _open_kuzu_db_cached(board_id, path)
            logger.debug("[KG] Kùzu database (cached) for board_id=%s", board_id)
            self.conn = kuzu.Connection(self.db)  # type: ignore[attr-defined]
            # The VECTOR extension is connection-scoped in LadybugDB/Kuzu. The
            # bootstrap path loads it before creating HNSW indexes, but hot boards
            # skip bootstrap and still open fresh connections for worker commits.
            # Without this, inserts into indexed tables such as Entity can fail
            # with "Trying to insert into an index ... extension is not loaded".
            load_vector_extension(self.conn)
        except BaseException:
            self._close_guard.reader_exit()
            raise
        logger.debug("[KG] Kùzu connection created successfully for board_id=%s", board_id)

    def __enter__(self) -> tuple[Any, Any]:
        return self.db, self.conn

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __iter__(self) -> Any:
        """Yield (db, conn) so ``tuple(BoardConnection(bid))`` works."""
        yield self.db
        yield self.conn

    def close(self) -> None:
        """Close the connection only; the cached Database survives.

        Idempotent. To release the OS file lock and allow a rmtree, call
        :func:`close_board_db_cache` (or the higher-level
        :func:`close_all_connections`) instead.
        """
        if self._closed:
            return
        logger.debug("[KG] BoardConnection.close board_id=%s", self._board_id)
        self._closed = True
        # File-handle leak fix (test_kg_file_handles::test_close_releases_handles):
        # `del self.conn` confiava no refcount para destruir o handle C++ —
        # qualquer QueryResult vivo segurava a Connection (e os handles de
        # graph.lbug + WAL) indefinidamente. `Connection.close()` libera o
        # handle nativo deterministicamente, independente de referências
        # Python remanescentes.
        try:
            self.conn.close()
        except Exception as exc:
            logger.debug(
                "[KG] BoardConnection.close conn_close_failed board_id=%s err=%s",
                self._board_id, exc,
            )
        try:
            del self.conn
        except Exception:
            pass
        # Close guard (FR-5): leitor sai do registro — idempotente porque
        # `_closed` barra reentrada deste método.
        try:
            self._close_guard.reader_exit()
        except Exception:
            pass
        # NOTE: do NOT close self.db — the Database is shared via the
        # process-wide cache (Bug d0f6bab2). Releasing it here would yank
        # the lock from concurrent BoardConnections operating on the same
        # board.


def _kg_base_dir() -> Path:
    """Resolve the KG base directory (defaults to ~/.okto-pulse)."""
    from okto_pulse.core.services.application_kg import get_current_provider_registry

    raw = get_current_provider_registry().config.kg_base_dir
    return Path(os.path.expanduser(raw)).resolve()


def board_kuzu_path(board_id: str) -> Path:
    """Return the absolute path to a board's LadybugDB graph file."""
    if not board_id or "/" in board_id or ".." in board_id:
        raise ValueError(f"invalid board_id: {board_id!r}")
    return _kg_base_dir() / "boards" / board_id / GRAPH_DB_FILENAME


def _is_ladybug_corruption_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(marker in msg for marker in CORRUPT_DB_ERROR_MARKERS)


def _raise_existing_graph_open_failed(
    *,
    board_id: str,
    path: Path,
    operation: str,
    exc: BaseException,
) -> None:
    """Fail closed when an existing graph cannot be opened.

    A bootstrap/migration probe is not an operator-approved recovery action.
    Earlier code quarantined the active graph on WAL corruption and then
    created a fresh empty graph, which made a previously queryable KG appear
    to vanish. Preserve evidence in place; explicit recovery/rebuild tooling
    owns any destructive move.
    """
    logger.error(
        "kg.schema.existing_graph_open_failed_preserved "
        "board=%s operation=%s path=%s err=%s",
        board_id, operation, path, exc,
        extra={
            "event": "kg.schema.existing_graph_open_failed_preserved",
            "board_id": board_id,
            "operation": operation,
            "path": str(path),
            "error": str(exc),
        },
    )
    raise RuntimeError(
        "Existing LadybugDB graph could not be opened during "
        f"{operation}; refusing to auto-bootstrap or purge it. "
        f"board_id={board_id} path={path}. "
        "Escada de recovery automática esgotada (KGD-01: salvage de WAL → "
        "quarentena wal-only); os próximos degraus são do operador — "
        "quarantine restore ou rebuild explícito via KG Health após revisar "
        "o report de quarentena. Os arquivos atuais foram preservados "
        "(main graph.lbug intocado)."
    ) from exc


def _ladybug_open_error_context(path: Path, exc: BaseException, settings: Any) -> str:
    """Build an operator-facing error with the active Graph DB settings."""
    msg = str(exc)
    lower = msg.lower()
    settings_context = (
        "Graph DB settings in effect: "
        f"kg_kuzu_buffer_pool_mb={settings.kg_kuzu_buffer_pool_mb}MB, "
        f"kg_kuzu_max_db_size_gb={settings.kg_kuzu_max_db_size_gb}GB "
        f"(path={path})."
    )
    guidance: list[str] = []
    if "power of 2" in lower or "power-of-2" in lower:
        guidance.append(
            "Set Graph DB max database size per board to one of "
            "2, 4, 8, 16, 32 or 64 GB; Ladybug requires max_db_size to be "
            "a power of 2 in bytes."
        )
    if (
        "buffer manager" in lower
        or "buffer pool" in lower
        or "unable to allocate memory" in lower
    ):
        guidance.append(
            "Set Graph DB buffer pool per board to 512 MB and restart before "
            "retrying the consolidation."
        )
    if "could not set lock" in lower or "lock contention" in lower:
        guidance.append(
            "Another Okto Pulse process may still hold this board graph; stop "
            "the other process or wait for the lock to release."
        )
    if not guidance:
        guidance.append(
            "Check for lock contention, pending schema migration, or a corrupt "
            "Ladybug graph file."
        )
    return f"{settings_context} {' '.join(guidance)}"


def _board_quarantine_service():
    """Build the canonical KGQuarantineService for board graph purges.

    The scope_roots are the per-board storage root (parent of every
    board_kuzu_path). The quarantine base_dir lives under the same KG
    storage root so the operator's recovery tooling is one filesystem
    away from the evidence.
    """
    from okto_pulse.core.kg.quarantine import KGQuarantineService
    from okto_pulse.community.adapters.local_storage_ref import local_storage_ref

    sample_path = board_kuzu_path("__scope_probe__")
    storage_root = sample_path.parent.parent  # boards/ root
    quarantine_base = storage_root.parent  # one level up: KG storage root
    return KGQuarantineService(
        base_storage_ref_hint=local_storage_ref(quarantine_base),
        scope_storage_refs=[local_storage_ref(storage_root)],
    )


def purge_board_graph_storage(board_id: str, *, reason: str = "manual") -> list[str]:
    """Quarantine-then-clear a board's local LadybugDB graph file and sidecars.

    KG-01.4 (val_79e6f555 rework): purges of `graph.lbug` and sidecars
    MUST go through ``KGQuarantineService`` first. The service moves the
    files into a quarantine directory and writes an auditable manifest
    BEFORE the originals are gone. Direct unlink/rmtree is gone — if
    quarantine fails the whole purge is aborted with the evidence
    preserved at the original path (per FR7 / AC10 / IR ir_f175bc42).

    The returned list is the set of paths that were moved into
    quarantine, kept for backward compatibility with callers that
    counted removed entries.
    """
    from okto_pulse.core.kg.quarantine import QuarantineError

    path = board_kuzu_path(board_id)
    close_board_db_cache(board_id)
    targets: list[Path] = []
    if path.exists():
        targets.append(path)
    if path.parent.exists():
        targets.extend(sorted(path.parent.glob(path.name + ".*")))

    if not targets:
        return []

    service = _board_quarantine_service()
    try:
        from okto_pulse.community.adapters.local_storage_ref import local_storage_ref

        response = service.create(
            board_id=board_id,
            graph_type="board_graph",
            affected_storage_refs=[local_storage_ref(t) for t in targets],
            reason=reason,
            correlation_ids=[],
        )
    except QuarantineError as exc:
        logger.error(
            "kg.schema.graph_purge_blocked_quarantine_failed "
            "board=%s reason=%s code=%s err=%s",
            board_id, reason, exc.code.value, exc.reason,
            extra={
                "event": "kg.schema.graph_purge_blocked_quarantine_failed",
                "board_id": board_id,
                "reason": reason,
                "code": exc.code.value,
            },
        )
        # FR7: refuse the purge so corruption evidence survives.
        return []

    moved_count = response.files_moved
    removed_str = [str(t) for t in targets[:moved_count]]

    if path.parent.exists() and not any(path.parent.iterdir()):
        try:
            path.parent.rmdir()
        except OSError:
            pass

    if "_BOOTSTRAPPED_BOARDS" in globals():
        _BOOTSTRAPPED_BOARDS.discard(board_id)
    if "_MIGRATED_BOARDS" in globals():
        _MIGRATED_BOARDS.discard(board_id)

    logger.warning(
        "kg.schema.graph_purged board=%s reason=%s removed=%d "
        "quarantine_id=%s manifest=%s",
        board_id, reason, moved_count,
        response.quarantine_id, response.manifest_ref,
        extra={
            "event": "kg.schema.graph_purged",
            "board_id": board_id,
            "reason": reason,
            "quarantine_id": response.quarantine_id,
            "manifest_ref": response.manifest_ref,
            "files_moved": moved_count,
        },
    )
    return removed_str


def _fsync_if_file(path: Path) -> None:
    if not path.is_file():
        return
    # Windows rejects os.fsync() on a read-only descriptor with EBADF. Use
    # read/write without truncation so the durability step is real but does not
    # mutate content.
    with path.open("r+b") as fh:
        os.fsync(fh.fileno())


def _fsync_board_graph_files(board_id: str) -> None:
    path = board_kuzu_path(board_id)
    _fsync_if_file(path)
    for sibling in sorted(path.parent.glob(path.name + ".*")):
        _fsync_if_file(sibling)
    # Directory fsync is POSIX-only in practice. Keep it best-effort so
    # Windows does not fail a valid lifecycle just because directories cannot
    # be opened as file descriptors there.
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
    except Exception:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            # Windows can allow opening the directory but still reject fsync
            # on that descriptor. File fsyncs above are the required durable
            # boundary there; directory fsync remains best-effort.
            return
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _close_reopen_probe_existing_board_graph(board_id: str) -> tuple[bool, str | None]:
    """Close process handles, reopen the existing graph, and verify BoardMeta.

    This intentionally bypasses ``ensure_board_graph_bootstrapped``. A
    lifecycle probe must validate the graph that was just materialized, not
    auto-create a fresh empty graph if the expected file is missing or
    unreadable.
    """

    path = board_kuzu_path(board_id)
    if not path.exists():
        return False, f"{GRAPH_DB_FILENAME} missing at {path}"

    # Review dcea02d (F4): o close fail-open fechava o Database com leitores
    # ativos plausíveis (health scan, projeção da UI) → use-after-close
    # nativo → SIGSEGV do processo. O probe PRECISA fechar de verdade (é a
    # prova de durabilidade do rebuild), então espera um dreno generoso; se
    # um leitor não sair, o probe FALHA explicitamente — rebuild reporta a
    # causa em vez de arriscar derrubar o servidor inteiro.
    if not try_close_board_db(board_id, drain_timeout=30.0, fast_path=False):
        return False, "close_reopen_probe_blocked_by_active_readers"
    try:
        # KGD-01 C6 (item 4): probe registrado no close guard — o reopen de
        # verificação também é um leitor que um close concorrente deve drenar.
        with registered_raw_connection(board_id) as (_db, conn):
            res = conn.execute(
                "CALL SHOW_TABLES() WHERE name = 'BoardMeta' RETURN name"
            )
            try:
                has_meta_table = res.has_next()
            finally:
                res.close()
            if not has_meta_table:
                return False, "BoardMeta table missing after reopen"

            res = conn.execute(
                "MATCH (m:BoardMeta {board_id: $bid}) RETURN m.schema_version",
                {"bid": board_id},
            )
            try:
                if not res.has_next():
                    return False, "BoardMeta row missing after reopen"
                row = res.get_next()
                schema_version = row[0] if row else None
            finally:
                res.close()
            if not schema_version:
                return False, "BoardMeta schema_version empty after reopen"
            return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        # Force the next dashboard/API read to prove it can open from disk too,
        # instead of reusing a probe-created Database object.
        close_board_db_cache(board_id)
        if "_BOOTSTRAPPED_BOARDS" in globals():
            _BOOTSTRAPPED_BOARDS.discard(board_id)


def apply_ladybug_lifecycle_step(
    board_id: str,
    graph_type: str,
    step: str,
):
    """Production KG safe-write lifecycle adapter for board ``graph.lbug``.

    Earlier REST rebuild wiring used a fake ``LifecycleStepResult(ok=True)``
    for every step. That allowed rebuild reports to say COMPLETED while the
    real graph was still only readable through a live process handle. This
    adapter performs the minimum real boundary checks LadybugDB exposes:
    close handles to force WAL flush, fsync graph files, and reopen-probe the
    existing graph without auto-bootstrap.
    """

    from okto_pulse.core.kg.safe_write_lifecycle import (
        LifecycleStepResult,
        STEP_CHECKPOINT,
        STEP_CLOSE_REOPEN_PROBE,
        STEP_FLUSH,
        STEP_FSYNC,
    )

    if graph_type != "board_graph":
        return LifecycleStepResult(
            ok=False,
            detail=f"unsupported_graph_type={graph_type}",
        )

    path = board_kuzu_path(board_id)
    try:
        # Spec 3d89c192 (FR-1/FR-2/FR-3): os steps checkpoint/flush/fsync são
        # NÃO-DESTRUTIVOS — nenhum deles fecha o Database compartilhado.
        # Fechar por commit criava a corrida use-after-close com leitores
        # concorrentes (kg_service no thread pool) e custava
        # close+gc.collect+reopen por queue entry. A barreira de durabilidade
        # REAL é o WAL + fsync; o CHECKPOINT é compactação/higiene e — campo
        # 2026-06-10, 6º crash — NÃO é seguro sob leitura concorrente no
        # ladybug 0.16.1 (SIGSEGV nativo), por isso roda em janela exclusiva
        # e é adiado quando há leitor ativo. O caminho destrutivo continua
        # existindo exclusivamente em STEP_CLOSE_REOPEN_PROBE
        # (rebuild/recovery).
        if step == STEP_CHECKPOINT:
            if not path.exists():
                return LifecycleStepResult(
                    ok=False,
                    detail=f"{GRAPH_DB_FILENAME} missing at {path}",
                )
            # Higiene periódica do buffer (campo 2026-06-10, 3 crashes): o
            # Ladybug 0.16 degrada sob CHECKPOINTs sucessivos no MESMO
            # Database aberto — após algumas centenas de commits o processo
            # morre por abort nativo no meio do checkpoint ("Buffer manager
            # exception: No more frame groups..." quando o erro chega a
            # emergir). A cada K commits o step troca o CHECKPOINT pelo
            # CLOSE do board (zera o buffer pool + checkpoint implícito do
            # close — o caminho estável pré-KGDL.01), amortizando o custo do
            # close em 1/K e ficando ordens de magnitude abaixo do limiar de
            # crash observado (~250-500 checkpoints).
            if _bump_checkpoint_counter_and_check_close(board_id):
                # Close DISCRICIONÁRIO (4º crash em campo): o caminho
                # fail-open de close_all_connections fechava o Database com
                # um health scan lendo o board → use-after-close nativo →
                # abort do processo. Com leitor ativo a higiene é ADIADA
                # (contador re-armado: o próximo commit tenta de novo) e
                # este commit faz o CHECKPOINT normal para durabilidade.
                if try_close_board_db(board_id):
                    if not path.exists():
                        return LifecycleStepResult(
                            ok=False,
                            detail=f"{GRAPH_DB_FILENAME} missing at {path}",
                        )
                    return LifecycleStepResult(ok=True)
                _rearm_checkpoint_counter(board_id)
            # CHECKPOINT exige EXCLUSIVIDADE (6º crash, faulthandler:
            # CHECKPOINT em thread do worker × outbox do discovery lendo
            # embeddings no MESMO Database → SIGSEGV nativo). A premissa do
            # KGDL.01 ("CHECKPOINT seguro sob leitura concorrente ativa no
            # ladybug 0.16.1") é FALSA em campo. Com leitor ativo o
            # CHECKPOINT é ADIADO — o commit já é durável via WAL (fsync no
            # STEP_FSYNC); a compactação re-tenta nos commits seguintes e
            # sempre encontra janela (TTL pós-scan garante 300s sem scan).
            guard = _get_close_guard(board_id)
            if guard.readers > 0:
                logger.info(
                    "kg.lifecycle.checkpoint_skipped_active_readers board=%s "
                    "readers=%d — checkpoint adiado (durabilidade via WAL)",
                    board_id, guard.readers,
                    extra={
                        "event": "kg.lifecycle.checkpoint_skipped_active_readers",
                        "board_id": board_id,
                    },
                )
                return LifecycleStepResult(ok=True)
            with guard.closing(timeout=_CHECKPOINT_EXCLUSIVE_DRAIN_TIMEOUT_S) as (
                drained,
                stuck,
            ):
                if not drained:
                    logger.info(
                        "kg.lifecycle.checkpoint_skipped_active_readers "
                        "board=%s readers=%d — checkpoint adiado "
                        "(durabilidade via WAL)",
                        board_id, stuck,
                        extra={
                            "event": "kg.lifecycle.checkpoint_skipped_active_readers",
                            "board_id": board_id,
                        },
                    )
                    return LifecycleStepResult(ok=True)
                try:
                    _execute_checkpoint_unguarded(path)
                except Exception as exc:
                    # Válvula de escape: CHECKPOINT falhou (ex.: buffer
                    # exausto) → fecha o Database (libera o buffer pool
                    # inteiro + flush via close). Já estamos DENTRO da
                    # janela exclusiva (zero leitores), então o close
                    # direto é seguro. Falha REAL do close propaga →
                    # step falha → queue entry não ACKada (BR-3).
                    logger.warning(
                        "kg.lifecycle.checkpoint_statement_failed board=%s "
                        "err=%s — fallback: close do Database (flush via "
                        "close)",
                        board_id, exc,
                        extra={
                            "event": "kg.lifecycle.checkpoint_statement_failed",
                            "board_id": board_id,
                        },
                    )
                    _reset_checkpoint_counter(board_id)
                    _close_cached_db_unguarded(board_id)
                    if not path.exists():
                        return LifecycleStepResult(
                            ok=False,
                            detail=f"{GRAPH_DB_FILENAME} missing at {path}",
                        )
                    return LifecycleStepResult(ok=True)
            return LifecycleStepResult(ok=True)

        if step == STEP_FLUSH:
            # FR-2: verificação não-destrutiva. A durabilidade do WAL é
            # responsabilidade do CHECKPOINT (FR-1); aqui só confirmamos que
            # o arquivo principal continua presente.
            if not path.exists():
                return LifecycleStepResult(
                    ok=False,
                    detail=f"{GRAPH_DB_FILENAME} missing at {path}",
                )
            return LifecycleStepResult(ok=True)

        if step == STEP_FSYNC:
            # FR-3: fsync dos arquivos do grafo com o Database aberto — abrir
            # handles de leitura para fsync não exige soltar o lock do Kùzu.
            if not path.exists():
                return LifecycleStepResult(
                    ok=False,
                    detail=f"{GRAPH_DB_FILENAME} missing at {path}",
                )
            _fsync_board_graph_files(board_id)
            return LifecycleStepResult(ok=True)

        if step == STEP_CLOSE_REOPEN_PROBE:
            ok, detail = _close_reopen_probe_existing_board_graph(board_id)
            return LifecycleStepResult(ok=ok, detail=detail)

        return LifecycleStepResult(ok=False, detail=f"unknown_step={step}")
    except Exception as exc:
        return LifecycleStepResult(
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
        )


def _is_ladybug_capi_shared_lib_missing(exc: BaseException) -> bool:
    return CAPI_SHARED_LIB_MISSING_MARKER in str(exc).lower()


def _ladybug_pybind_available() -> bool:
    try:
        import ladybug._lbug  # type: ignore  # noqa: F401
    except Exception:
        return False
    return True


def _wal_sidecar_path(path: Path) -> Path:
    """WAL sidecar do main file de um grafo (graph.lbug -> graph.lbug.wal)."""
    return path.with_name(path.name + ".wal")


def _open_database_with_salvage_flag(
    kuzu_module: Any,
    path: str,
    *,
    buffer_pool_size: int,
    max_db_size: int,
    throw_on_wal_replay_failure: bool,
    backend: str | None = None,
) -> Any:
    """Open ``ladybug.Database`` passando ``throw_on_wal_replay_failure`` (KGD-01 FR1).

    Tolerante a versões do wrapper que não aceitam o kwarg (mesmo padrão
    try/except ``TypeError`` do kwarg ``backend=`` no fallback pybind): quando
    o wrapper não suporta o parâmetro e o caller pedia salvage (flag ``False``),
    loga ``kg.wal.salvage_unsupported`` e abre sem o kwarg (comportamento
    fail-closed antigo do wrapper, ``throw=True``).
    """
    kwargs: dict[str, Any] = {
        "buffer_pool_size": buffer_pool_size,
        "max_db_size": max_db_size,
    }
    if backend is not None:
        kwargs["backend"] = backend
    try:
        return kuzu_module.Database(
            path,
            throw_on_wal_replay_failure=throw_on_wal_replay_failure,
            **kwargs,
        )
    except TypeError:
        pass
    if not throw_on_wal_replay_failure:
        logger.warning(
            "kg.wal.salvage_unsupported path=%s — wrapper ladybug sem suporte a "
            "throw_on_wal_replay_failure; abrindo sem salvage",
            path,
            extra={"event": "kg.wal.salvage_unsupported", "path": str(path)},
        )
    return kuzu_module.Database(path, **kwargs)


def _open_ladybug_database_forced_pybind(
    kuzu_module: Any,
    path: str,
    *,
    buffer_pool_size: int,
    max_db_size: int,
    throw_on_wal_replay_failure: bool = True,
) -> Any:
    """Open LadybugDB through pybind when the C-API shim is unavailable.

    Some desktop launches inherit ``LBUG_PYTHON_BACKEND=capi`` or hit
    Ladybug's C-API fallback even though the bundled ``_lbug`` pybind
    extension is present. The C-API shim needs an extra shared library that
    the wheel does not install on Windows. Treat that as a backend selection
    issue, not as a graph corruption signal.

    KGD-01 FR1/TR2: o kwarg ``throw_on_wal_replay_failure`` é propagado também
    neste caminho, para que TODOS os opens da fábrica única carreguem a flag.
    """

    previous_backend = os.environ.get("LBUG_PYTHON_BACKEND")
    os.environ["LBUG_PYTHON_BACKEND"] = "pybind"
    try:
        try:
            return _open_database_with_salvage_flag(
                kuzu_module,
                path,
                buffer_pool_size=buffer_pool_size,
                max_db_size=max_db_size,
                throw_on_wal_replay_failure=throw_on_wal_replay_failure,
                backend="pybind",
            )
        except TypeError:
            return _open_database_with_salvage_flag(
                kuzu_module,
                path,
                buffer_pool_size=buffer_pool_size,
                max_db_size=max_db_size,
                throw_on_wal_replay_failure=throw_on_wal_replay_failure,
            )
    finally:
        if previous_backend is None:
            os.environ.pop("LBUG_PYTHON_BACKEND", None)
        else:
            os.environ["LBUG_PYTHON_BACKEND"] = previous_backend


def _try_open_with_wal_salvage(
    kuzu_module: Any,
    path: Path,
    *,
    buffer_pool_size: int,
    max_db_size: int,
    original_error: BaseException,
) -> Any | None:
    """Retry de open com salvage de WAL após falha por corrupção (KGD-01 FR1/D1).

    Estratégia determinística do spec KGD-01: o open primário é SEMPRE
    fail-closed (``throw_on_wal_replay_failure=True``), então uma falha de
    replay do WAL é detectada de forma inequívoca; SÓ quando o open falhou com
    marcador de corrupção E ``kg_wal_salvage_enabled=true`` este retry reabre
    com ``throw_on_wal_replay_failure=False`` — o engine (``dryReplay``)
    replica o WAL até o último COMMIT válido e trunca o sufixo corrompido.
    O caminho feliz não muda e salvage NUNCA é silencioso: sucesso emite o
    warning estruturado ``kg.wal.salvage_applied``.

    Sinal em kg_health: registrado APENAS via log estruturado. O
    ``kg_health_service`` do core computa health sob demanda a partir de
    fila/DLQ/probes — não há registry de sinais por board acessível deste
    adapter sem criar acoplamento novo core<-community; decisão documentada
    no card KGD-01-C1.

    Retorna o Database aberto, ou ``None`` quando o salvage não foi possível
    (o caller segue o caminho de erro fail-closed normal).
    """
    wal_path = _wal_sidecar_path(path)
    try:
        wal_bytes_before = wal_path.stat().st_size if wal_path.exists() else 0
    except OSError:
        wal_bytes_before = -1
    db: Any | None = None
    salvage_exc: BaseException | None = None
    try:
        db = _open_database_with_salvage_flag(
            kuzu_module,
            str(path),
            buffer_pool_size=buffer_pool_size,
            max_db_size=max_db_size,
            throw_on_wal_replay_failure=False,
        )
    except Exception as exc:
        salvage_exc = exc
        if _is_ladybug_capi_shared_lib_missing(exc) and _ladybug_pybind_available():
            try:
                db = _open_ladybug_database_forced_pybind(
                    kuzu_module,
                    str(path),
                    buffer_pool_size=buffer_pool_size,
                    max_db_size=max_db_size,
                    throw_on_wal_replay_failure=False,
                )
            except Exception as retry_exc:
                salvage_exc = retry_exc
    if db is None:
        logger.warning(
            "kg.wal.salvage_failed board=%s path=%s original_err=%s salvage_err=%s",
            path.parent.name, path, original_error, salvage_exc,
            extra={
                "event": "kg.wal.salvage_failed",
                "board_id": path.parent.name,
                "path": str(path),
            },
        )
        return None
    try:
        wal_bytes_after = wal_path.stat().st_size if wal_path.exists() else 0
    except OSError:
        wal_bytes_after = -1
    logger.warning(
        "kg.wal.salvage_applied board=%s path=%s wal_bytes_before=%s "
        "wal_bytes_after=%s original_err=%s",
        path.parent.name, path, wal_bytes_before, wal_bytes_after, original_error,
        extra={
            "event": "kg.wal.salvage_applied",
            "board_id": path.parent.name,
            "path": str(path),
            "wal_bytes_before": wal_bytes_before,
            "wal_bytes_after": wal_bytes_after,
        },
    )
    return db


def _try_open_with_wal_only_recovery(
    kuzu_module: Any,
    path: Path,
    *,
    buffer_pool_size: int,
    max_db_size: int,
    original_error: BaseException,
) -> Any | None:
    """Degrau 2 da escada de recovery (KGD-01 FR3/BR2): wal-only quarantine.

    Roda SÓ quando o open falhou com marcador de corrupção e o salvage
    (degrau 1) falhou ou está indisponível/desligado. Quarentena SOMENTE
    ``graph.lbug.wal`` + sidecars (``.shadow`` / ``.wal.checkpoint``) com
    manifest completo — o main ``graph.lbug`` NUNCA é tocado (BR2) — e
    re-tenta o open UMA vez (fail-closed, ``throw_on_wal_replay_failure=True``:
    o grafo reaberto é o estado do último checkpoint, sem replay de WAL).

    Retorna o Database aberto, ou ``None`` quando não havia nada a
    quarentenar, a quarentena falhou (partial-move vira fail-closed — o
    manifest registra o estado exato) ou o reopen ainda falhou (o caller
    segue o caminho de erro fail-closed normal, cuja mensagem aponta os
    degraus seguintes: restore/operador).
    """
    from okto_pulse.community.adapters.kg_wal_recovery import wal_only_quarantine

    board_id = path.parent.name
    try:
        result = wal_only_quarantine(
            board_id,
            f"open_failed_after_salvage: {original_error}",
            graph_path=path,
        )
    except Exception as q_exc:
        logger.warning(
            "kg.recovery.wal_only_quarantine_error board=%s path=%s err=%s",
            board_id, path, q_exc,
            extra={
                "event": "kg.recovery.wal_only_quarantine_error",
                "board_id": board_id,
                "path": str(path),
            },
        )
        return None
    if not result.ok or not result.files_moved:
        # ok=False → partial-move fail-closed (evidência no manifest);
        # files_moved vazio → nada a quarentenar, retry seria o mesmo open.
        return None

    reopen_exc: BaseException | None = None
    db: Any | None = None
    try:
        db = _open_database_with_salvage_flag(
            kuzu_module,
            str(path),
            buffer_pool_size=buffer_pool_size,
            max_db_size=max_db_size,
            throw_on_wal_replay_failure=True,
        )
    except Exception as exc:
        reopen_exc = exc
        if _is_ladybug_capi_shared_lib_missing(exc) and _ladybug_pybind_available():
            try:
                db = _open_ladybug_database_forced_pybind(
                    kuzu_module,
                    str(path),
                    buffer_pool_size=buffer_pool_size,
                    max_db_size=max_db_size,
                )
            except Exception as retry_exc:
                reopen_exc = retry_exc
    if db is None:
        logger.warning(
            "kg.recovery.wal_only_reopen_failed board=%s path=%s "
            "quarantine_id=%s original_err=%s reopen_err=%s",
            board_id, path, result.quarantine_id, original_error, reopen_exc,
            extra={
                "event": "kg.recovery.wal_only_reopen_failed",
                "board_id": board_id,
                "path": str(path),
                "quarantine_id": result.quarantine_id,
            },
        )
        return None
    logger.warning(
        "kg.recovery.wal_only_recovered board=%s path=%s quarantine_id=%s "
        "files=%s original_err=%s",
        board_id, path, result.quarantine_id,
        list(result.files_moved), original_error,
        extra={
            "event": "kg.recovery.wal_only_recovered",
            "board_id": board_id,
            "path": str(path),
            "quarantine_id": result.quarantine_id,
            "files": list(result.files_moved),
            "main_untouched": True,
        },
    )
    return db


def _open_kuzu_db(path: Path):
    """Single factory for every ``kuzu.Database()`` call in the core.

    Reads ``kg_kuzu_buffer_pool_mb`` and ``kg_kuzu_max_db_size_gb`` from
    :class:`CoreSettings` and passes them in bytes. Replaces Kùzu's own
    defaults (``buffer_pool_size=0`` → ~80% system RAM, ``max_db_size=1<<43``
    → 8 TB VA) which caused 128 GB RSS with 3 instances in field reports.

    Keeping this factory as the unique entry point lets ops re-tune memory
    from a single place and enables the runtime Settings menu (0.1.4).

    Raises a clear ``RuntimeError`` when the underlying Kùzu storage is
    corrupted due to a version incompatibility (SIGBUS / BusError) instead
    of letting the signal crash the process.

    KGD-01 FR1/BR1: esta fábrica é o ponto ÚNICO onde
    ``throw_on_wal_replay_failure`` é controlado (open primário fail-closed
    com ``True``; retry de salvage com ``False`` quando
    ``kg_wal_salvage_enabled`` estiver ativo e o open falhar com marcador de
    corrupção — ver ``_try_open_with_wal_salvage``).
    """
    import ladybug as kuzu  # type: ignore
    from okto_pulse.core.infra.config import get_settings

    logger.debug("[KG] _open_kuzu_db path=%s", path)
    s = get_settings()
    bp = s.kg_kuzu_buffer_pool_mb * 1024 * 1024
    mds = s.kg_kuzu_max_db_size_gb * 1024 * 1024 * 1024
    # KGD-01 FR1/TR3: kg_wal_salvage_enabled (default true) controla o retry
    # de salvage de WAL abaixo. getattr com default tolera um core antigo sem
    # o campo (repos versionam em lockstep, mas skew de branch acontece).
    salvage_enabled = bool(getattr(s, "kg_wal_salvage_enabled", True))
    # KGD-01 FR3/BR2: kg_wal_only_recovery_enabled (default true) controla o
    # degrau 2 (quarentena wal-only) quando o salvage falhou/indisponível.
    wal_only_enabled = bool(getattr(s, "kg_wal_only_recovery_enabled", True))
    logger.debug("[KG] _open_kuzu_db buffer_pool=%dMB max_db=%dGB", s.kg_kuzu_buffer_pool_mb, s.kg_kuzu_max_db_size_gb)

    # Bug d0f6bab2: lock contention happens because every BoardConnection
    # used to spawn a NEW kuzu.Database — but Kùzu locks the .kuzu dir at
    # the OS level for as long as ANY Database handle exists. When two
    # workers (consolidation + handler) open the same board, the second
    # blocks. Retry+gc.collect() does not help when the contention is
    # cross-thread.  Real fix: cache Database per path (singleton) so
    # multiple Connections share one Database (Kùzu supports that). The
    # caller-facing API (BoardConnection / open_board_connection) uses
    # `_open_kuzu_db_cached` which delegates here only on cache miss.
    #
    # Retry below covers the residual case where ANOTHER process holds
    # the lock (e.g. CLI run while server is up). 5× exponential backoff:
    # 0.2 / 0.4 / 0.8 / 1.6 / 3.2 = 6.2s cumulative.
    last_exc: BaseException | None = None
    salvage_attempted = False
    wal_only_attempted = False
    for attempt in range(1, 6):
        try:
            # KGD-01 FR1/TR2 (estratégia determinística de D1): o open primário
            # é SEMPRE fail-closed (throw_on_wal_replay_failure=True) para que
            # falha de replay de WAL seja detectada; o retry com salvage
            # (throw=False) acontece só no branch de corrupção abaixo, gated
            # por kg_wal_salvage_enabled — o caminho feliz não muda e salvage
            # nunca é silencioso.
            db = _open_database_with_salvage_flag(
                kuzu,
                str(path),
                buffer_pool_size=bp,
                max_db_size=mds,
                throw_on_wal_replay_failure=True,
            )
            logger.debug("[KG] kuzu.Database() created successfully for path=%s", path)
            return db
        except Exception as e:
            if _is_ladybug_capi_shared_lib_missing(e) and _ladybug_pybind_available():
                try:
                    logger.warning(
                        "kg.db_open.capi_missing_retry_pybind path=%s attempt=%d/5 err=%s",
                        path,
                        attempt,
                        e,
                        extra={
                            "event": "kg.db_open.capi_missing_retry_pybind",
                            "path": str(path),
                            "attempt": attempt,
                        },
                    )
                    db = _open_ladybug_database_forced_pybind(
                        kuzu,
                        str(path),
                        buffer_pool_size=bp,
                        max_db_size=mds,
                    )
                    logger.debug(
                        "[KG] kuzu.Database() created successfully with pybind backend for path=%s",
                        path,
                    )
                    return db
                except Exception as retry_exc:
                    e = retry_exc
            last_exc = e
            msg = str(e)
            # Auto-recovery (campo 2026-06-10): crash no MEIO de um checkpoint
            # deixa sidecars órfãos (graph.lbug.shadow vazio +
            # graph.lbug.wal.checkpoint) que fazem o replay do Ladybug abortar
            # com UNREACHABLE_CODE em wal_record.cpp — com o main file 100%
            # íntegro (confirmado em campo: 3926 nodes recuperados ao remover
            # os sidecars). Quarentena os sidecars (preserva evidência; NUNCA
            # toca o main file nem um .wal principal) e re-tenta uma vez.
            if (
                attempt < 5
                and _is_ladybug_corruption_error(e)
                and _quarantine_interrupted_checkpoint_sidecars(path)
            ):
                logger.warning(
                    "kg.db_open.interrupted_checkpoint_recovered path=%s err=%s",
                    path, e,
                    extra={
                        "event": "kg.db_open.interrupted_checkpoint_recovered",
                        "path": str(path),
                    },
                )
                continue
            # KGD-01 FR1: WAL rasgado/corrompido (cauda parcial de kill duro,
            # zeros interiores de escritor stale) com salvage habilitado —
            # reabre UMA vez com throw_on_wal_replay_failure=False para o
            # engine replicar até o último COMMIT válido e truncar o sufixo
            # corrompido. Com kg_wal_salvage_enabled=false o caminho permanece
            # fail-closed (o RuntimeError abaixo orienta o operador).
            if (
                salvage_enabled
                and not salvage_attempted
                and _is_ladybug_corruption_error(e)
            ):
                salvage_attempted = True
                salvaged = _try_open_with_wal_salvage(
                    kuzu,
                    path,
                    buffer_pool_size=bp,
                    max_db_size=mds,
                    original_error=e,
                )
                if salvaged is not None:
                    return salvaged
            # KGD-01 FR3/BR2 (degrau 2 da escada salvage → wal-only →
            # restore/operador): o salvage falhou ou está
            # desligado/indisponível e o open continua falhando com marcador
            # de corrupção — quarentena SOMENTE do graph.lbug.wal + sidecars
            # (o main graph.lbug NUNCA é tocado) e re-tenta o open UMA vez.
            # Gated por kg_wal_only_recovery_enabled (default true); com a
            # flag desligada o caminho permanece fail-closed.
            if (
                wal_only_enabled
                and not wal_only_attempted
                and _is_ladybug_corruption_error(e)
            ):
                wal_only_attempted = True
                recovered = _try_open_with_wal_only_recovery(
                    kuzu,
                    path,
                    buffer_pool_size=bp,
                    max_db_size=mds,
                    original_error=e,
                )
                if recovered is not None:
                    return recovered
            is_lock_contention = "Could not set lock" in msg or "lock contention" in msg.lower()
            if is_lock_contention and attempt < 5:
                sleep_s = 0.2 * (2 ** (attempt - 1))
                logger.warning(
                    "kg.db_open.lock_retry path=%s attempt=%d/5 sleep=%.2fs err=%s",
                    path, attempt, sleep_s, e,
                    extra={
                        "event": "kg.db_open.lock_retry",
                        "path": str(path),
                        "attempt": attempt,
                        "sleep_s": sleep_s,
                    },
                )
                gc.collect()  # Liberar handles pendentes (essencial no Windows)
                time.sleep(sleep_s)
                continue
            break

    e = last_exc  # type: ignore[assignment]
    logger.error(
        "[KG] Failed to open LadybugDB database at %s: %s: %s",
        path, type(e).__name__, e,
    )
    context = _ladybug_open_error_context(path, e, s)
    raise RuntimeError(
        f"Failed to open LadybugDB database at {path}: "
        f"{type(e).__name__}: {e}. "
        f"{context} "
        "Possible causes: "
        "(1) lock contention from concurrent writer (wait and retry); "
        "(2) schema migration needed — run "
        "`python -m okto_pulse.tools.kg_migrate_schema --board <board_id>` "
        "or call MCP tool `okto_pulse_kg_migrate_schema`; "
        "(3) corrupted db file."
    ) from e


# Contador de checkpoints por board para a higiene periódica do buffer
# (a cada K commits o STEP_CHECKPOINT vira CLOSE). Override via env
# KG_CHECKPOINT_CLOSE_INTERVAL.
_CHECKPOINT_CLOSE_INTERVAL_DEFAULT = 10
_checkpoint_counters: dict[str, int] = {}
_checkpoint_counters_lock = threading.Lock()


def _checkpoint_close_interval() -> int:
    raw = os.environ.get("KG_CHECKPOINT_CLOSE_INTERVAL")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return _CHECKPOINT_CLOSE_INTERVAL_DEFAULT


def _bump_checkpoint_counter_and_check_close(board_id: str) -> bool:
    """Incrementa o contador do board; True quando é a vez do CLOSE."""
    with _checkpoint_counters_lock:
        n = _checkpoint_counters.get(board_id, 0) + 1
        if n >= _checkpoint_close_interval():
            _checkpoint_counters[board_id] = 0
            return True
        _checkpoint_counters[board_id] = n
        return False


def _reset_checkpoint_counter(board_id: str) -> None:
    with _checkpoint_counters_lock:
        _checkpoint_counters[board_id] = 0


def _rearm_checkpoint_counter(board_id: str) -> None:
    """Deixa o contador a 1 bump do close — higiene adiada por leitor ativo
    re-tenta no commit seguinte em vez de esperar mais K commits."""
    with _checkpoint_counters_lock:
        _checkpoint_counters[board_id] = _checkpoint_close_interval() - 1


def _quarantine_interrupted_checkpoint_sidecars(path: Path) -> bool:
    """Move órfãos de checkpoint interrompido para a quarentena.

    O shadow é a área de trabalho do checkpoint EM ANDAMENTO — o main file
    só é substituído atomicamente na conclusão. Logo, quando a abertura
    falha com marcador de corrupção e existem ``<graph>.shadow`` /
    ``<graph>.wal.checkpoint`` órfãos, o main é o estado autoritativo e os
    sidecars são lixo do checkpoint que não completou (confirmado DUAS
    vezes em campo 2026-06-10: shadow vazio e shadow de 283KB — main
    íntegro com 3926/3929 nodes em ambos). Os sidecars vão para a
    quarentena (nada é destruído); o main e um eventual ``<graph>.wal``
    principal NUNCA são tocados (o .wal pode conter commits legítimos
    não-checkpointed). Concorrência: outro processo segurando o grafo
    falharia com lock contention, não com marcador de corrupção — este
    caminho só roda quando não há dono vivo.

    Retorna True quando moveu algo (o caller re-tenta a abertura).
    """
    shadow = path.parent / (path.name + ".shadow")
    wal_checkpoint = path.parent / (path.name + ".wal.checkpoint")
    movable: list[Path] = []
    if shadow.exists():
        movable.append(shadow)
    if wal_checkpoint.exists():
        movable.append(wal_checkpoint)
    if not movable:
        return False

    quarantine_dir = (
        path.parents[2]
        / "quarantine"
        / f"interrupted-checkpoint-{path.parent.name}-{_now_iso().replace(':', '').replace('.', '')}"
    )
    try:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        for f in movable:
            f.rename(quarantine_dir / f.name)
        (quarantine_dir / "manifest.txt").write_text(
            "Sidecars orfaos de checkpoint interrompido movidos automaticamente "
            f"para destravar a abertura de {path}. Main file preservado no lugar. "
            f"Arquivos: {', '.join(f.name for f in movable)}.",
            encoding="utf-8",
        )
        return True
    except OSError as exc:
        logger.warning(
            "kg.db_open.sidecar_quarantine_failed path=%s err=%s",
            path, exc,
            extra={
                "event": "kg.db_open.sidecar_quarantine_failed",
                "path": str(path),
            },
        )
        return False


def verify_kuzu_db_health(board_id: str) -> dict[str, Any]:
    """Check if the Kùzu database for a board is healthy.

    Returns a dict with ``ok`` (bool), ``node_count`` (int), and ``error``
    (str or None). Safe to call during server startup or as a health-check
    endpoint — opens a short-lived connection, counts all nodes, and closes
    cleanly.
    """
    path = board_kuzu_path(board_id)
    if not path.exists():
        return {"ok": True, "node_count": 0, "error": None,
                "note": f"{GRAPH_DB_FILENAME} does not exist yet — will be created on first access"}
    try:
        with open_board_connection(board_id) as (db, conn):
            res = conn.execute("MATCH (n) RETURN count(n) AS cnt")
            row = res.get_next()
            cnt = int(row[0])
            res.close()
        return {"ok": True, "node_count": cnt, "error": None}
    except Exception as e:
        return {"ok": False, "node_count": 0, "error": f"{type(e).__name__}: {e}"}


def _show_rel_connection_pairs(conn, rel_name: str) -> set[tuple[str, str]]:
    """Return the declared (from, to) pairs for a multi-typed REL table."""
    res = None
    try:
        res = conn.execute(f"CALL SHOW_CONNECTION('{rel_name}') RETURN *")
        pairs: set[tuple[str, str]] = set()
        while res.has_next():
            row = res.get_next()
            if len(row) >= 2:
                pairs.add((str(row[0]), str(row[1])))
        return pairs
    finally:
        if res is not None:
            try:
                res.close()
            except Exception:
                pass


def _ensure_multi_rel_pairs(
    conn,
    rel_name: str,
    pairs: tuple[tuple[str, str], ...],
) -> list[tuple[str, str]]:
    """ALTER ADD any missing endpoint pair on an existing multi-typed REL table."""
    added: list[tuple[str, str]] = []
    for from_type, to_type in pairs:
        try:
            conn.execute(f"ALTER TABLE {rel_name} ADD FROM {from_type} TO {to_type}")
            added.append((from_type, to_type))
        except Exception:
            # Pair already exists, rel table was just created with all pairs,
            # or the current Kuzu build rejected a duplicate ADD. All are safe.
            pass
    return added


class SupersedesPairsIncompleteError(RuntimeError):
    """Structured, fail-closed pair-conversion failure (spec MKG-D-S1 FR5,
    stable code ``kg_supersedes_pairs_incomplete``).

    ``_ensure_multi_rel_pairs`` deliberately swallows ALTER exceptions (the
    duplicate case is benign) — this probe is what turns a REAL conversion
    failure into an explicit error instead of an invisible no-op that only
    breaks at supersede time.
    """

    code = "kg_supersedes_pairs_incomplete"

    def __init__(self, board_hint: str, missing_pairs: list[tuple[str, str]]):
        self.missing_pairs = missing_pairs
        self.remediation = (
            "The 'supersedes' rel table could not be extended to all node-"
            "type pairs on this engine build. Run a sanctioned graph rebuild "
            "(KGD-01 flow) to re-create the rel table with the full pair set."
        )
        super().__init__(
            f"kg_supersedes_pairs_incomplete {board_hint} "
            f"missing={missing_pairs}"
        )


def _probe_supersedes_pairs(conn) -> list[tuple[str, str]]:
    """Return MISSING (from, to) pairs of the ``supersedes`` rel (spec FR5).

    Primary probe reads the catalog (SHOW_CONNECTION); fallback is a
    functional read per expected pair (a MATCH against an absent pair
    raises on this engine). Read-only either way.
    """
    expected: set[tuple[str, str]] = set()
    for rel_name, pairs in MULTI_REL_TYPES:
        if rel_name == "supersedes":
            expected = set(pairs)
            break
    if not expected:
        return []
    present: set[tuple[str, str]] = set()
    try:
        res = conn.execute("CALL SHOW_CONNECTION('supersedes') RETURN *")
        try:
            while res.has_next():
                row = res.get_next()
                labels = [value for value in row if isinstance(value, str)]
                if len(labels) >= 2:
                    present.add((labels[0], labels[1]))
        finally:
            try:
                res.close()
            except Exception:
                pass
    except Exception:
        for from_type, to_type in expected:
            try:
                res = conn.execute(
                    f"MATCH (:{from_type})-[e:supersedes]->(:{to_type}) "
                    "RETURN count(e)"
                )
                res.get_next()
                try:
                    res.close()
                except Exception:
                    pass
                present.add((from_type, to_type))
            except Exception:
                pass
    return sorted(expected - present)


def _ensure_vector_indexes(conn) -> None:
    """Create every configured vector index, tolerating already-existing ones."""
    for node_type in VECTOR_INDEX_TYPES:
        idx = vector_index_name(node_type)
        try:
            conn.execute(
                f"CALL CREATE_VECTOR_INDEX("
                f"'{node_type}', '{idx}', 'embedding', "
                f"metric := 'cosine')"
            )
        except Exception:
            pass


def _ensure_edge_metadata_columns(conn, rel_name: str) -> list[str]:
    """ALTER TABLE ADD for every v0.2.0 metadata column missing on `rel_name`.

    Returns the list of columns actually added. Idempotent: Kùzu raises on
    duplicate ADD so we catch-and-continue; no pre-check query is needed.
    """
    added: list[str] = []
    for col_name, col_type in EDGE_METADATA_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {rel_name} ADD {col_name} {col_type}")
            added.append(col_name)
        except Exception:
            # Already exists or ALTER unsupported for this version — both safe.
            pass
    return added


def _backfill_legacy_edge_metadata(conn, rel_name: str) -> int:
    """Tag pre-v0.2.0 rels that still have NULL layer as `legacy`.

    Sets layer='legacy', rule_id='legacy_pre_v2', created_by='worker_legacy'
    only where the current value is NULL so re-running the migration is safe.
    Returns the number of rels updated (best-effort; Kùzu's UPDATE count isn't
    always exposed so we return 0 on opaque drivers).
    """
    try:
        conn.execute(
            f"MATCH ()-[r:{rel_name}]->() WHERE r.layer IS NULL "
            f"SET r.layer = 'legacy', "
            f"r.rule_id = coalesce(r.rule_id, 'legacy_pre_v2'), "
            f"r.created_by = coalesce(r.created_by, 'worker_legacy')"
        )
    except Exception as exc:
        logger.warning(
            "migrate_edge_metadata.backfill_failed rel=%s err=%s",
            rel_name, exc,
            extra={"event": "migrate_edge_metadata.backfill_failed",
                   "rel_name": rel_name},
        )
        return 0
    return 0


def migrate_edge_metadata(board_id: str) -> dict[str, Any]:
    """Apply the v0.1.0 → v0.2.0 edge metadata migration to a board.

    Idempotent — safe to call on every bootstrap. Adds missing columns on every
    rel table and backfills NULL `layer` as `legacy` so consumers that filter
    by layer don't drop historical edges unexpectedly. Callable manually via
    the CLI (`okto-pulse kg backfill --migrate-schema`) or from within
    bootstrap_board_graph.

    Returns a dict summary `{rel_name: [added_columns]}` useful for audit logs.
    """
    summary: dict[str, Any] = {}
    path = board_kuzu_path(board_id)
    if not path.exists():
        return summary

    # Release pool-cached handle first. On Windows Kùzu holds an exclusive
    # file lock per-process, so a cached connection from the pool would
    # collide with the fresh one we're about to open.
    close_all_connections(board_id)

    with open_board_connection(board_id) as (_db, conn):
        rel_names = [rel_name for rel_name, _from_type, _to_type in REL_TYPES]
        rel_names.extend(rel_name for rel_name, _pairs in MULTI_REL_TYPES)
        for rel_name in rel_names:
            added = _ensure_edge_metadata_columns(conn, rel_name)
            _backfill_legacy_edge_metadata(conn, rel_name)
            summary[rel_name] = added

    logger.info(
        "migrate_edge_metadata.done board=%s summary=%s",
        board_id, summary,
        extra={"event": "migrate_edge_metadata.done", "board_id": board_id,
               "summary": summary},
    )
    return summary


def _board_meta_ddl() -> str:
    return (
        "CREATE NODE TABLE IF NOT EXISTS BoardMeta ("
        "board_id STRING PRIMARY KEY, "
        "schema_version STRING, "
        "bootstrapped_at TIMESTAMP, "
        "embedding_model STRING, "
        "embedding_dimension INT64"
        ")"
    )


#: Spec MKG-D-S1 (FR1): embedding compatibility metadata on BoardMeta.
BOARD_META_EMBEDDING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("embedding_model", "STRING"),
    ("embedding_dimension", "INT64"),
)


def _ensure_board_meta_embedding_columns(conn) -> list[str]:
    """ALTER TABLE ADD for the MKG-D embedding columns on legacy BoardMeta.

    Idempotent with retry on lock contention (same pattern as the node
    column ensures).
    """
    added: list[str] = []
    for col_name, col_type in BOARD_META_EMBEDDING_COLUMNS:
        if _alter_add_column_with_retry(conn, "BoardMeta", col_name, col_type) == "added":
            added.append(col_name)
    return added


def _effective_embedding_meta() -> dict:
    """Best-effort effective provider metadata (never loads a model, never
    raises). Registry unconfigured / provider absent => stub-shaped dict,
    which the pure guard classifies as INDETERMINATE (spec D2)."""
    try:
        from okto_pulse.core.kg.interfaces.embedding import (
            describe_embedding_provider,
        )
        from okto_pulse.core.kg.interfaces.registry import get_kg_registry

        provider = getattr(get_kg_registry(), "embedding_provider", None)
        return describe_embedding_provider(provider)
    except Exception:
        return {
            "model_name": None,
            "embedding_dimension": 0,
            "is_stub": True,
        }


def _read_board_meta_embedding(conn, board_id: str) -> tuple[str | None, int | None]:
    res = conn.execute(
        "MATCH (m:BoardMeta {board_id: $bid}) "
        "RETURN m.embedding_model, m.embedding_dimension LIMIT 1",
        {"bid": board_id},
    )
    try:
        if res.has_next():
            row = res.get_next()
            model = row[0] if row[0] else None
            dim = int(row[1]) if row[1] is not None else None
            return model, dim
        return None, None
    finally:
        try:
            res.close()
        except Exception:
            pass


def _enforce_embedding_guard(board_id: str) -> None:
    """Open-time embedding compatibility guard (spec MKG-D-S1 FR2/FR3).

    Runs inside ``ensure_board_graph_bootstrapped`` — the one choke point
    every opener (CLI, API, worker, MCP, search, health) passes through
    (decision D1). ``mismatch`` refuses the open fail-closed; ``stamp``
    persists the effective metadata; ``indeterminate`` logs and proceeds
    without ever dirtying a valid stamp (decision D2).
    """
    from okto_pulse.core.kg.embedding_guard import (
        VERDICT_INDETERMINATE,
        VERDICT_MISMATCH,
        VERDICT_STAMP,
        EmbeddingIncompatibleError,
        compare_embedding_compat,
    )

    effective = _effective_embedding_meta()
    try:
        with registered_raw_connection(board_id) as (_db, conn):
            persisted_model, persisted_dim = _read_board_meta_embedding(
                conn, board_id
            )
            verdict = compare_embedding_compat(
                persisted_model,
                persisted_dim,
                effective.get("model_name"),
                effective.get("embedding_dimension"),
                effective_is_stub=bool(effective.get("is_stub")),
            )
            if verdict == VERDICT_STAMP:
                conn.execute(
                    "MATCH (m:BoardMeta {board_id: $bid}) "
                    "SET m.embedding_model = $model, "
                    "m.embedding_dimension = $dim",
                    {
                        "bid": board_id,
                        "model": effective.get("model_name"),
                        "dim": int(effective.get("embedding_dimension") or 0),
                    },
                )
                logger.info(
                    "kg.embedding_guard.stamped board=%s model=%s dim=%s",
                    board_id,
                    effective.get("model_name"),
                    effective.get("embedding_dimension"),
                    extra={
                        "event": "kg.embedding_guard.stamped",
                        "board_id": board_id,
                    },
                )
    except EmbeddingIncompatibleError:
        raise
    except Exception as exc:
        # Guard reads are best-effort against transient open contention —
        # never turn a probe hiccup into an open failure (the column-based
        # migration probes stay authoritative, spec TR3).
        logger.warning(
            "kg.embedding_guard.probe_failed board=%s err=%s",
            board_id,
            exc,
        )
        return

    if verdict == VERDICT_MISMATCH:
        logger.error(
            "kg.embedding_guard.mismatch board=%s persisted=%s/%s "
            "effective=%s/%s",
            board_id,
            persisted_model,
            persisted_dim,
            effective.get("model_name"),
            effective.get("embedding_dimension"),
            extra={
                "event": "kg.embedding_guard.mismatch",
                "board_id": board_id,
                "persisted_model": persisted_model,
                "persisted_dimension": persisted_dim,
                "effective_model": effective.get("model_name"),
                "effective_dimension": effective.get("embedding_dimension"),
            },
        )
        raise EmbeddingIncompatibleError(
            board_id=board_id,
            persisted_model=persisted_model,
            persisted_dimension=persisted_dim,
            effective_model=effective.get("model_name"),
            effective_dimension=effective.get("embedding_dimension"),
        )
    if verdict == VERDICT_INDETERMINATE:
        logger.warning(
            "kg.embedding_guard.indeterminate board=%s (provider sem "
            "metadata válida/stub — guarda não aplicada)",
            board_id,
            extra={
                "event": "kg.embedding_guard.indeterminate",
                "board_id": board_id,
            },
        )


def _is_duplicate_column_error(exc: BaseException) -> bool:
    """Kùzu raises Binder exceptions on duplicate ADD. Recognize the benign
    idempotent case so we can distinguish it from genuine errors (lock
    contention, permission, etc.)."""
    msg = str(exc).lower()
    return (
        "already exists" in msg
        or "duplicate" in msg
        or "already has property" in msg
    )


def _is_retryable_kuzu_error(exc: BaseException) -> bool:
    """File-lock and transient IO errors on the embedded .kuzu file.

    Windows file locking is the dominant offender: when the board graph
    is open for reads (e.g. fallback search) an ALTER concurrent with the
    reader fails with ``IO exception: Could not set lock on file``.
    """
    msg = str(exc).lower()
    return (
        "could not set lock" in msg
        or "io exception" in msg
        or "timeout" in msg
    )


def _alter_add_column_with_retry(
    conn, node_type: str, col_name: str, col_type: str,
    *, max_attempts: int = 5, base_sleep: float = 0.2,
) -> str:
    """ALTER TABLE ADD with retry on lock contention.

    Returns one of: ``"added"``, ``"exists"``, ``"failed"``. ``"exists"`` is
    the idempotent path (column already present). ``"failed"`` means all
    retries exhausted on a retryable error OR a non-retryable error — either
    case logged at WARN so silent swallowing doesn't keep hiding schema
    drift like the 2026-04-19 priority_boost incident.
    """
    ddl = f"ALTER TABLE {node_type} ADD {col_name} {col_type}"
    last_err: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            conn.execute(ddl)
            return "added"
        except Exception as exc:
            last_err = exc
            if _is_duplicate_column_error(exc):
                return "exists"
            if _is_retryable_kuzu_error(exc) and attempt < max_attempts:
                sleep_s = base_sleep * (2 ** (attempt - 1))
                logger.info(
                    "kg.schema.alter_retry node=%s col=%s attempt=%d/%d sleep=%.2fs err=%s",
                    node_type, col_name, attempt, max_attempts, sleep_s, exc,
                )
                time.sleep(sleep_s)
                continue
            break
    logger.warning(
        "kg.schema.alter_failed node=%s col=%s attempts=%d err=%s",
        node_type, col_name, max_attempts, last_err,
    )
    return "failed"


def _ensure_relevance_columns(conn, node_type: str) -> list[str]:
    """ALTER TABLE ADD for every v0.3.0 column missing on ``node_type``.

    Idempotent — retries on lock contention (see
    :func:`_alter_add_column_with_retry`). Returns the list of columns
    actually added this call.
    """
    added: list[str] = []
    for col_name, col_type in RELEVANCE_COLUMNS:
        if _alter_add_column_with_retry(conn, node_type, col_name, col_type) == "added":
            added.append(col_name)
    return added


def _ensure_priority_boost_columns(conn, node_type: str) -> list[str]:
    """ALTER TABLE ADD for the v0.3.1 priority_boost column on ``node_type``.

    Idempotent with retry on lock contention. Returns the list of columns
    actually added this call (typically empty on second run).
    """
    added: list[str] = []
    for col_name, col_type in PRIORITY_BOOST_COLUMNS:
        if _alter_add_column_with_retry(conn, node_type, col_name, col_type) == "added":
            added.append(col_name)
    return added


def _ensure_human_curated_columns(conn, node_type: str) -> list[str]:
    """ALTER TABLE ADD for the v0.3.2 human_curated column on ``node_type``.

    Idempotent with retry on lock contention. Default treatment for legacy
    nodes (NULL value): the UPDATE preservation path treats NULL as FALSE,
    so no backfill is required for retrocompat. Curators set TRUE
    explicitly via back-office tooling after manual edits.
    """
    added: list[str] = []
    for col_name, col_type in HUMAN_CURATED_COLUMNS:
        if _alter_add_column_with_retry(conn, node_type, col_name, col_type) == "added":
            added.append(col_name)
    return added


def _ensure_last_recomputed_at_columns(conn, node_type: str) -> list[str]:
    """ALTER TABLE ADD for the v0.3.3 last_recomputed_at column on ``node_type``.

    Idempotent with retry on lock contention. Legacy rows get NULL — the
    daily decay tick treats NULL as "never recomputed" and prioritises those
    nodes first when sizing its workload. No backfill required.
    """
    added: list[str] = []
    for col_name, col_type in LAST_RECOMPUTED_COLUMNS:
        if _alter_add_column_with_retry(conn, node_type, col_name, col_type) == "added":
            added.append(col_name)
    return added


def _ensure_kg_layer_columns(conn, node_type: str) -> list[str]:
    """ALTER TABLE ADD for v0.3.6 graph_layer/maturity_status columns."""

    added: list[str] = []
    for col_name, col_type in KG_LAYER_COLUMNS:
        if _alter_add_column_with_retry(conn, node_type, col_name, col_type) == "added":
            added.append(col_name)
    return added


def _ensure_provenance_columns(conn, node_type: str) -> list[str]:
    """ALTER TABLE ADD for the v0.3.9 provenance columns (spec MKG-B-S1 FR1)."""
    added: list[str] = []
    for col_name, col_type in PROVENANCE_COLUMNS:
        if _alter_add_column_with_retry(conn, node_type, col_name, col_type) == "added":
            added.append(col_name)
    return added


def _ensure_attestation_columns(conn, node_type: str) -> list[str]:
    """ALTER TABLE ADD for the v0.3.9 attestation columns (spec MKG-B-S1 FR2).

    NEW names only — LEGACY_NODE_COLUMNS ('corroboration_count') stays
    untouched: its PRESENCE flags a board as v0.2.0 and triggers the
    destructive recreate path.
    """
    added: list[str] = []
    for col_name, col_type in ATTESTATION_COLUMNS:
        if _alter_add_column_with_retry(conn, node_type, col_name, col_type) == "added":
            added.append(col_name)
    return added


def _ensure_subtype_columns(conn, node_type: str) -> list[str]:
    """ALTER TABLE ADD for the v0.3.10 declarative-subtype column (spec
    MKG-E-S1 FR1). Idempotent with retry; legacy rows read NULL (no
    subtype declared) — fully transparent to existing consumers."""
    added: list[str] = []
    for col_name, col_type in SUBTYPE_COLUMNS:
        if _alter_add_column_with_retry(conn, node_type, col_name, col_type) == "added":
            added.append(col_name)
    return added


def _ensure_generation_columns(conn, node_type: str) -> list[str]:
    """ALTER TABLE ADD for the v0.3.8 generation column (spec MKG-A-S1 FR3).

    Idempotent with retry on lock contention. Legacy rows get NULL — the
    supersede path treats NULL as generation 0 (``_node_generation`` in
    core primitives), so no backfill is required for retrocompat.
    """
    added: list[str] = []
    for col_name, col_type in GENERATION_COLUMNS:
        if _alter_add_column_with_retry(conn, node_type, col_name, col_type) == "added":
            added.append(col_name)
    return added


def _backfill_kg_layer_defaults(conn, node_type: str) -> None:
    """Mark legacy rows as canonical so existing boards keep querying.

    The rebuild manifest and CanonicalDebt surfaces now own precise maturity
    classification for future rebuilds. Existing graph rows predate that
    metadata, so defaulting them to canonical preserves compatibility while
    letting operators rebuild to materialize stricter partitioning later.
    """

    try:
        conn.execute(
            f"MATCH (n:{node_type}) "
            f"WHERE n.graph_layer IS NULL "
            f"SET n.graph_layer = 'canonical'"
        )
    except Exception as exc:
        logger.warning(
            "migrate_kg_layer.backfill_failed node=%s col=graph_layer err=%s",
            node_type, exc,
        )
    try:
        conn.execute(
            f"MATCH (n:{node_type}) "
            f"WHERE n.maturity_status IS NULL "
            f"SET n.maturity_status = 'canonical_eligible'"
        )
    except Exception as exc:
        logger.warning(
            "migrate_kg_layer.backfill_failed node=%s col=maturity_status err=%s",
            node_type, exc,
        )


def _backfill_relevance_defaults(conn, node_type: str) -> None:
    """Populate the v0.3.0 columns for rows that existed before the migration.

    Sets relevance_score=0.5 and query_hits=0 only where the value is NULL,
    keeping the migration re-runnable. last_queried_at stays NULL — it will be
    populated organically by the R2 hit-counter path.
    """
    try:
        conn.execute(
            f"MATCH (n:{node_type}) "
            f"WHERE n.relevance_score IS NULL "
            f"SET n.relevance_score = 0.5"
        )
    except Exception as exc:
        logger.warning(
            "migrate_relevance.backfill_failed node=%s col=relevance_score err=%s",
            node_type, exc,
        )
    try:
        conn.execute(
            f"MATCH (n:{node_type}) "
            f"WHERE n.query_hits IS NULL "
            f"SET n.query_hits = 0"
        )
    except Exception as exc:
        logger.warning(
            "migrate_relevance.backfill_failed node=%s col=query_hits err=%s",
            node_type, exc,
        )


def _node_has_legacy_columns(conn, node_type: str) -> bool:
    """Returns True iff ``node_type`` still has validation_status /
    corroboration_count columns from v0.2.0. Uses the table info catalog.

    A best-effort probe — any error is treated as "no legacy columns" so we
    don't try to re-drop on a fresh v0.3.0 board.
    """
    res = None
    try:
        res = conn.execute(f"CALL TABLE_INFO('{node_type}') RETURN *")
        cols: set[str] = set()
        while res.has_next():
            row = res.get_next()
            # TABLE_INFO returns columns including "name" somewhere in the row;
            # normalise by iterating.
            for item in row:
                if isinstance(item, str):
                    cols.add(item)
        return any(c in cols for c in LEGACY_NODE_COLUMNS)
    except Exception:
        return False
    finally:
        if res is not None:
            try:
                res.close()
            except Exception:
                pass


def _node_has_relevance_columns(conn, node_type: str) -> bool:
    """Returns True iff ``node_type`` already has the v0.3.0 columns."""
    res = None
    try:
        res = conn.execute(f"CALL TABLE_INFO('{node_type}') RETURN *")
        cols: set[str] = set()
        while res.has_next():
            row = res.get_next()
            for item in row:
                if isinstance(item, str):
                    cols.add(item)
        return all(c in cols for c in (name for name, _ in RELEVANCE_COLUMNS))
    except Exception:
        return False
    finally:
        if res is not None:
            try:
                res.close()
            except Exception:
                pass


def _migrate_node_table_v030(conn, node_type: str) -> int:
    """Drop + recreate a node table with the v0.3.0 schema, preserving rows.

    Kùzu v0.6 has no ALTER TABLE DROP COLUMN, so when validation_status /
    corroboration_count must go we have to:

      1. dump every row via ``MATCH (n:Type) RETURN n.*``
      2. ``DROP NODE TABLE Type``
      3. ``CREATE NODE TABLE Type (...)`` with the new schema
      4. re-insert the dumped rows, mapping legacy cols onto the new defaults

    Returns the number of rows migrated (best-effort — 0 when the driver
    doesn't expose a count). The caller is expected to recreate any vector
    index on the table afterwards (``CREATE_VECTOR_INDEX`` is idempotent and
    lives in ``bootstrap_board_graph``).
    """
    dumped: list[dict[str, Any]] = []
    res = None
    try:
        res = conn.execute(
            f"MATCH (n:{node_type}) RETURN n.id AS id, n.title AS title, "
            f"n.content AS content, n.context AS context, "
            f"n.justification AS justification, "
            f"n.source_artifact_ref AS source_artifact_ref, "
            f"n.source_session_id AS source_session_id, "
            f"n.created_at AS created_at, n.created_by_agent AS created_by_agent, "
            f"n.source_confidence AS source_confidence, "
            f"n.superseded_by AS superseded_by, "
            f"n.superseded_at AS superseded_at, "
            f"n.revocation_reason AS revocation_reason, "
            f"n.embedding AS embedding"
        )
        while res.has_next():
            row = res.get_next()
            # Row is positional — map to column names in the SELECT order.
            dumped.append({
                "id": row[0],
                "title": row[1],
                "content": row[2],
                "context": row[3],
                "justification": row[4],
                "source_artifact_ref": row[5],
                "source_session_id": row[6],
                "created_at": row[7],
                "created_by_agent": row[8],
                "source_confidence": row[9],
                "superseded_by": row[10],
                "superseded_at": row[11],
                "revocation_reason": row[12],
                "embedding": row[13],
            })
    except Exception as exc:
        logger.warning(
            "migrate_v030.dump_failed node=%s err=%s — skipping table",
            node_type, exc,
        )
        return 0
    finally:
        if res is not None:
            try:
                res.close()
            except Exception:
                pass

    try:
        conn.execute(f"DROP TABLE {node_type}")
    except Exception as exc:
        logger.warning(
            "migrate_v030.drop_failed node=%s err=%s — table may be in use",
            node_type, exc,
        )
        return 0

    try:
        conn.execute(_build_node_ddl(node_type))
    except Exception as exc:
        logger.error(
            "migrate_v030.create_failed node=%s err=%s — data loss risk",
            node_type, exc,
        )
        raise

    restored = 0
    for row in dumped:
        try:
            conn.execute(
                f"CREATE (n:{node_type} {{"
                f"id: $id, title: $title, content: $content, context: $context, "
                f"justification: $justification, "
                f"source_artifact_ref: $source_artifact_ref, "
                f"graph_layer: 'canonical', maturity_status: 'canonical_eligible', "
                f"source_session_id: $source_session_id, "
                f"created_at: $created_at, created_by_agent: $created_by_agent, "
                f"source_confidence: $source_confidence, "
                f"relevance_score: 0.5, query_hits: 0, last_queried_at: NULL, "
                f"priority_boost: 0.0, "
                f"superseded_by: $superseded_by, superseded_at: $superseded_at, "
                f"revocation_reason: $revocation_reason, embedding: $embedding"
                f"}})",
                row,
            )
            restored += 1
        except Exception as exc:
            logger.warning(
                "migrate_v030.restore_failed node=%s id=%s err=%s",
                node_type, row.get("id"), exc,
            )

    logger.info(
        "migrate_v030.table_done node=%s dumped=%d restored=%d",
        node_type, len(dumped), restored,
        extra={"event": "migrate_v030.table_done", "node_type": node_type,
               "dumped": len(dumped), "restored": restored},
    )
    return restored


def migrate_board_to_v030(board_id: str) -> dict[str, Any]:
    """Apply the v0.2.0 → v0.3.0 migration to a board.

    Idempotent, non-destructive. For every node table:

      * ``ALTER TABLE ADD`` the three v0.3.0 columns when missing.
      * Backfill ``relevance_score = 0.5`` / ``query_hits = 0`` where NULL.

    Kùzu v0.6 does not allow ``DROP NODE TABLE`` while rel tables or
    vector indexes reference it, so we leave the legacy
    ``validation_status`` / ``corroboration_count`` columns in place as
    orphans. The Python code no longer reads them — they are harmless
    dead data until a future hard reset.

    Vector indexes remain intact; the migration never drops them.

    Returns a summary ``{node_type: {"strategy": "alter", "added": [...]}}``
    for audit logs.
    """
    summary: dict[str, Any] = {}
    path = board_kuzu_path(board_id)
    if not path.exists():
        return summary

    close_all_connections(board_id)

    # Use a raw kuzu.Connection here — open_board_connection() would
    # re-enter _board_needs_v030_migration and recurse infinitely, and
    # the migration must run BEFORE the BoardConnection bootstrap path
    # ever owns the handle. KGD-01 C6 (item 4): a conexão crua passa pelo
    # registro do close guard (registered_raw_connection). O __enter__
    # manual preserva a semântica antiga: falha do OPEN vira
    # _raise_existing_graph_open_failed (fail-closed com evidência
    # preservada), sem capturar falhas do corpo da migração.
    raw_ctx = registered_raw_connection(board_id)
    try:
        db, conn = raw_ctx.__enter__()
    except Exception as exc:
        _raise_existing_graph_open_failed(
            board_id=board_id,
            path=path,
            operation="schema_migration_open",
            exc=exc,
        )
    try:
        for node_type in NODE_TYPES:
            added = _ensure_relevance_columns(conn, node_type)
            added.extend(_ensure_kg_layer_columns(conn, node_type))
            _backfill_relevance_defaults(conn, node_type)
            _backfill_kg_layer_defaults(conn, node_type)
            had_legacy = _node_has_legacy_columns(conn, node_type)
            summary[node_type] = {
                "strategy": "alter",
                "added": added,
                "legacy_columns_left": had_legacy,
            }

        try:
            conn.execute(
                "MATCH (m:BoardMeta {board_id: $bid}) "
                "SET m.schema_version = $v",
                {"bid": board_id, "v": SCHEMA_VERSION},
            )
            # Spec MKG-D-S1 (FR1): the migrate path also stamps the
            # effective embedding metadata (it used to update only
            # schema_version — risk R4: fields written on one path would
            # evaporate on the other).
            _ensure_board_meta_embedding_columns(conn)
            _meta = _effective_embedding_meta()
            if not _meta.get("is_stub") and _meta.get("model_name") and int(
                _meta.get("embedding_dimension") or 0
            ) > 0:
                conn.execute(
                    "MATCH (m:BoardMeta {board_id: $bid}) "
                    "SET m.embedding_model = $model, "
                    "m.embedding_dimension = $dim",
                    {
                        "bid": board_id,
                        "model": _meta.get("model_name"),
                        "dim": int(_meta.get("embedding_dimension") or 0),
                    },
                )
        except Exception as exc:
            logger.warning(
                "migrate_v030.meta_update_failed board=%s err=%s",
                board_id, exc,
            )
    finally:
        # Fecha a Connection e desregistra o leitor no guard. Bug d0f6bab2:
        # o db é process-cached (_board_db_cache); NÃO fechar aqui — o cache
        # é dropado explicitamente via close_board_db_cache().
        raw_ctx.__exit__(None, None, None)
        gc.collect()

    _MIGRATED_BOARDS.add(board_id)

    logger.info(
        "migrate_v030.done board=%s summary=%s",
        board_id, summary,
        extra={"event": "migrate_v030.done", "board_id": board_id,
               "summary": summary},
    )
    return summary


# R-P2-05: pure schema metadata is owned by schema_contract. Keep this module as
# a compatibility surface while the Ladybug runtime is moved to Community.
SCHEMA_VERSION = _schema_contract.SCHEMA_VERSION
EDGE_LAYERS = _schema_contract.EDGE_LAYERS
EDGE_METADATA_COLUMNS = _schema_contract.EDGE_METADATA_COLUMNS
NODE_TYPES = _schema_contract.NODE_TYPES
VECTOR_INDEX_TYPES = _schema_contract.VECTOR_INDEX_TYPES
REL_TYPES = _schema_contract.REL_TYPES
MULTI_REL_TYPES = _schema_contract.MULTI_REL_TYPES
STABLE_NODE_PROPERTIES = _schema_contract.STABLE_NODE_PROPERTIES
RELEVANCE_COLUMNS = _schema_contract.RELEVANCE_COLUMNS
PRIORITY_BOOST_COLUMNS = _schema_contract.PRIORITY_BOOST_COLUMNS
HUMAN_CURATED_COLUMNS = _schema_contract.HUMAN_CURATED_COLUMNS
LAST_RECOMPUTED_COLUMNS = _schema_contract.LAST_RECOMPUTED_COLUMNS
KG_LAYER_COLUMNS = _schema_contract.KG_LAYER_COLUMNS
GENERATION_COLUMNS = _schema_contract.GENERATION_COLUMNS
PROVENANCE_COLUMNS = _schema_contract.PROVENANCE_COLUMNS
ATTESTATION_COLUMNS = _schema_contract.ATTESTATION_COLUMNS
SUBTYPE_COLUMNS = _schema_contract.SUBTYPE_COLUMNS
LEGACY_NODE_COLUMNS = _schema_contract.LEGACY_NODE_COLUMNS
stable_rel_type_entries = _schema_contract.stable_rel_type_entries
relationship_endpoint_pairs = _schema_contract.relationship_endpoint_pairs
resolve_relationship_endpoint_pair = _schema_contract.resolve_relationship_endpoint_pair
from okto_pulse.community.adapters.graph_ddl import (
    COMMON_NODE_ATTRIBUTES as _COMMON_NODE_ATTRS,
    build_multi_rel_ddl as _build_multi_rel_ddl,
    build_node_ddl as _build_node_ddl,
    build_rel_ddl as _build_rel_ddl,
)
vector_index_name = _schema_contract.vector_index_name


def load_vector_extension(conn) -> None:
    """Ensure the Kùzu VECTOR extension is loaded on the given connection.

    INSTALL is idempotent (persists in the DB file); LOAD must be called on
    every fresh connection but is also a no-op when already loaded.
    """
    try:
        conn.execute("INSTALL VECTOR")
    except Exception:
        pass  # already installed or bundled
    try:
        conn.execute("LOAD VECTOR")
    except Exception:
        pass  # already loaded or bundled


# Cache of boards already migrated this process — avoids re-running the
# write-heavy ALTER+UPDATE DDL on every connection open (which competes with
# concurrent commits for Kùzu's per-database lock and silently rolled back the
# real edge writes during historical drains).
_MIGRATED_BOARDS: set[str] = set()


def _board_needs_migration(board_id: str) -> bool:
    """Return True iff the board lacks any rel table or endpoint pair.

    The original probe only compared table names. That missed additive
    changes inside a multi-typed REL table, such as adding ``Entity -> Bug`` to
    ``belongs_to`` for Architecture Design nodes attached to Bug cards.
    """
    if board_id in _MIGRATED_BOARDS:
        return False
    try:
        # KGD-01 C6 (item 4): probe registrado no close guard.
        with registered_raw_connection(board_id) as (_db, conn):
            res = None
            try:
                res = conn.execute(
                    "CALL show_tables() WHERE type='REL' RETURN name"
                )
                existing = set()
                while res.has_next():
                    existing.add(res.get_next()[0])
                res.close()
                res = None
                expected = (
                    {r[0] for r in REL_TYPES} | {m[0] for m in MULTI_REL_TYPES}
                )
                if not expected.issubset(existing):
                    return True
                for rel_name, pairs in MULTI_REL_TYPES:
                    existing_pairs = _show_rel_connection_pairs(conn, rel_name)
                    if not set(pairs).issubset(existing_pairs):
                        return True
            finally:
                if res is not None:
                    try:
                        res.close()
                    except Exception:
                        pass
        return False
    except Exception:
        # Probe failed — assume migration is needed; the apply itself is
        # idempotent so a false positive only costs one extra DDL pass.
        return True


def _probe_first_node_type_columns(board_id: str) -> set[str]:
    """TABLE_INFO no primeiro node type — retorna as colunas existentes.

    TABLE_INFO on the first node type is representative: as migrações
    adicionam colunas a todos os node types num único loop, então se uma
    falta, todas faltam (boards legados foram bootstrapped em um passe).

    KGD-01 C6 (item 4): a conexão crua do probe é registrada no close guard
    via ``registered_raw_connection``. Exceções (open/query) propagam — os
    probes chamadores mantêm seus try/except (BR6).
    """
    with registered_raw_connection(board_id) as (_db, conn):
        res = None
        try:
            probe_node = NODE_TYPES[0]
            res = conn.execute(f"CALL TABLE_INFO('{probe_node}') RETURN *")
            existing_cols: set[str] = set()
            while res.has_next():
                row = res.get_next()
                # TABLE_INFO row: [index, name, type, default, pk]
                existing_cols.add(str(row[1]))
            return existing_cols
        finally:
            if res is not None:
                try:
                    res.close()
                except Exception:
                    pass


def _board_needs_priority_boost_migration(board_id: str) -> bool:
    """Returns True iff the board is missing the v0.3.1 ``priority_boost``
    column on any node type.

    Fixes the 2026-04-19 incident where boards bootstrapped before the
    ``7c032ee`` commit had rel tables + v0.3.0 columns (so the other two
    probes short-circuited) but lacked ``priority_boost``, silently
    breaking every ``commit_consolidation`` with a Binder exception.

    Column-based probe over the first node type — authoritative regardless
    of BoardMeta.schema_version. Returns False on probe failure so a stuck
    probe never loops the migration.
    """
    try:
        existing_cols = _probe_first_node_type_columns(board_id)
        expected = {c for c, _ in PRIORITY_BOOST_COLUMNS}
        return not expected.issubset(existing_cols)
    except Exception:
        return False


def _board_needs_v030_migration(board_id: str) -> bool:
    """Returns True iff the board is missing the v0.3.0 node columns.

    The probe is column-based — it does NOT trust BoardMeta.schema_version
    alone because an earlier destructive-migration attempt may have bumped
    the recorded version without actually adding the ALTER columns (if a
    DROP TABLE failed against a referenced rel/index). Inspecting
    ``TABLE_INFO`` on the first node type is the authoritative answer.

    Returns False on any probe error so a broken probe never loops a
    destructive re-migration.
    """
    if board_id in _MIGRATED_BOARDS:
        return False
    try:
        path = board_kuzu_path(board_id)
        if not path.exists():
            return False
        # KGD-01 C6 (item 4): probe registrado no close guard.
        with registered_raw_connection(board_id) as (_db, conn):
            for node_type in NODE_TYPES:
                if not _node_has_relevance_columns(conn, node_type):
                    return True
                break  # one probe is enough — all node types share _COMMON_NODE_ATTRS
            return False
    except Exception:
        return False


def _board_needs_human_curated_migration(board_id: str) -> bool:
    """Returns True iff the board is missing the v0.3.2 ``human_curated``
    column on any node type.

    Spec 818748f2 — FR2. Same column-based pattern as
    `_board_needs_priority_boost_migration` (L918). The migration adds
    ``human_curated`` to every node type in a single loop, so probing the
    first node is representative — if one is missing, all are missing.

    Returns False on probe failure (BR6: silent in failure to NOT loop
    a stuck migration).
    """
    try:
        existing_cols = _probe_first_node_type_columns(board_id)
        expected = {c for c, _ in HUMAN_CURATED_COLUMNS}
        return not expected.issubset(existing_cols)
    except Exception:
        return False


def _board_needs_last_recomputed_migration(board_id: str) -> bool:
    """Returns True iff the board is missing the v0.3.3
    ``last_recomputed_at`` column on any node type.

    Spec 818748f2 — FR2. Same column-based pattern as the priority_boost
    and human_curated probes. Returns False on probe failure (BR6).
    """
    try:
        existing_cols = _probe_first_node_type_columns(board_id)
        expected = {c for c, _ in LAST_RECOMPUTED_COLUMNS}
        return not expected.issubset(existing_cols)
    except Exception:
        return False


def _board_needs_post_v030_migration(board_id: str) -> bool:
    """Compose probe — True iff any v0.3.1+ column is missing on the board.

    Spec 818748f2 — FR3. Aggregates the three column probes (priority_boost,
    human_curated, last_recomputed_at) via short-circuit OR. Cache hit via
    `_MIGRATED_BOARDS` makes this a no-op after the first migration succeeds
    (BR1: idempotent re-runs cost only the cache lookup).

    Probes ordered chronologically (v0.3.1 → v0.3.2 → v0.3.3). Boards that
    are most behind short-circuit at the earliest probe.
    """
    if board_id in _MIGRATED_BOARDS:
        return False
    return (
        _board_needs_priority_boost_migration(board_id)
        or _board_needs_human_curated_migration(board_id)
        or _board_needs_last_recomputed_migration(board_id)
    )


def _migrate_board_schema(board_id: str) -> bool:
    """One-shot schema apply for a pre-existing board. Wraps the DDL pass
    in its own short-lived connection so the caller's connection lifecycle
    isn't tangled with the migration's, then caches the board as migrated only
    after the apply succeeds."""
    try:
        # KGD-01 C6 (item 4): DDL de migração registrado no close guard.
        with registered_raw_connection(board_id) as (_db, conn):
            apply_schema_to_connection(conn)
        _MIGRATED_BOARDS.add(board_id)
        return True
    except Exception as exc:
        _MIGRATED_BOARDS.discard(board_id)
        logger.warning(
            "board_migrate.apply_failed board=%s err=%s",
            board_id, exc,
        )
        return False


def migrate_schema_for_board(board_id: str) -> dict[str, Any]:
    """Force-apply schema migrations for a single board (idempotent).

    Spec 818748f2 (FR5). Public surface for the CLI/MCP/REST tripleta —
    re-runs ALTER TABLE ADD for every v0.3.x column on every node type and
    returns a structured summary so callers can display columns added per
    node type.

    Differs from `_migrate_board_schema`:
    - Discards `_MIGRATED_BOARDS` cache for this board so the migration
      re-runs even if a previous attempt cached the board (BR1: idempotent
      means re-runnable, not skip-after-first-success).
    - Captures columns_added per node type via the existing return values
      from `_ensure_*_columns` (which already track ALTER ADD success).
    - Surfaces errors as a list (non-fatal) instead of swallowing.
    - Returns timing for observability.

    Args:
        board_id: Board ID to migrate.

    Returns:
        ``{"board_id": str, "migrated": bool, "columns_added":
        {node_type: [col_name]}, "errors": [str], "duration_ms": int}``
    """
    start = time.time()
    columns_added: dict[str, list[str]] = {}
    errors: list[str] = []
    migrated = False

    # BR1: idempotent re-run requires invalidating the cache.
    _MIGRATED_BOARDS.discard(board_id)

    try:
        path = board_kuzu_path(board_id)
        if not path.exists():
            errors.append(
                f"board_not_found: {GRAPH_DB_FILENAME} missing at {path}"
            )
            return {
                "board_id": board_id,
                "migrated": False,
                "columns_added": columns_added,
                "errors": errors,
                "duration_ms": int((time.time() - start) * 1000),
            }

        # KGD-01 C6 (item 4): DDL registrado no close guard (__enter__ manual
        # para não re-indentar o corpo; o __exit__ no finally fecha a
        # Connection e desregistra o leitor).
        raw_ctx = registered_raw_connection(board_id)
        _db, conn = raw_ctx.__enter__()
        try:
            load_vector_extension(conn)
            conn.execute(_board_meta_ddl())
            _ensure_board_meta_embedding_columns(conn)
            for node_type in NODE_TYPES:
                added_for_type: list[str] = []
                try:
                    conn.execute(_build_node_ddl(node_type))
                    added_for_type.extend(
                        _ensure_priority_boost_columns(conn, node_type)
                    )
                    added_for_type.extend(
                        _ensure_human_curated_columns(conn, node_type)
                    )
                    added_for_type.extend(
                        _ensure_last_recomputed_at_columns(conn, node_type)
                    )
                    added_for_type.extend(
                        _ensure_kg_layer_columns(conn, node_type)
                    )
                    added_for_type.extend(
                        _ensure_generation_columns(conn, node_type)
                    )
                    added_for_type.extend(
                        _ensure_provenance_columns(conn, node_type)
                    )
                    added_for_type.extend(
                        _ensure_attestation_columns(conn, node_type)
                    )
                    added_for_type.extend(
                        _ensure_subtype_columns(conn, node_type)
                    )
                    _backfill_kg_layer_defaults(conn, node_type)
                except Exception as nt_exc:
                    errors.append(
                        f"node_type_failed: {node_type}: {nt_exc}"
                    )
                if added_for_type:
                    columns_added[node_type] = added_for_type
            for rel_name, from_type, to_type in REL_TYPES:
                try:
                    conn.execute(_build_rel_ddl(rel_name, from_type, to_type))
                    _ensure_edge_metadata_columns(conn, rel_name)
                    _backfill_legacy_edge_metadata(conn, rel_name)
                except Exception as rel_exc:
                    errors.append(
                        f"rel_failed: {rel_name}: {rel_exc}"
                    )
            for rel_name, pairs in MULTI_REL_TYPES:
                try:
                    conn.execute(_build_multi_rel_ddl(rel_name, pairs))
                    _ensure_multi_rel_pairs(conn, rel_name, pairs)
                    _ensure_edge_metadata_columns(conn, rel_name)
                    _backfill_legacy_edge_metadata(conn, rel_name)
                except Exception as mrel_exc:
                    errors.append(
                        f"multi_rel_failed: {rel_name}: {mrel_exc}"
                    )
            try:
                _ensure_vector_indexes(conn)
            except Exception as vector_exc:
                errors.append(f"vector_indexes_failed: {vector_exc}")
        finally:
            # Bug d0f6bab2: db is process-cached; NÃO fechar o Database aqui.
            raw_ctx.__exit__(None, None, None)
        # BR3: only cache as migrated if migration actually completed.
        # We treat "no errors" as success even if columns_added is empty
        # (idempotent no-op on already-migrated boards).
        if not errors:
            _MIGRATED_BOARDS.add(board_id)
            migrated = True
        else:
            # Partial migration — some node/rel types may have applied
            # but at least one failed. Don't cache so the next open retries.
            migrated = False
    except Exception as exc:
        errors.append(f"migration_failed: {exc}")
        migrated = False
        logger.warning(
            "kg.migrate_schema.failed board=%s err=%s",
            board_id, exc,
            extra={
                "event": "kg.migrate_schema.failed",
                "board_id": board_id,
                "error": str(exc),
            },
        )

    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        "kg.migrate_schema.done board=%s migrated=%s columns_added=%s "
        "errors=%d duration_ms=%d",
        board_id, migrated, columns_added, len(errors), duration_ms,
        extra={
            "event": "kg.migrate_schema.done",
            "board_id": board_id,
            "migrated": migrated,
            "columns_added_count": sum(len(v) for v in columns_added.values()),
            "errors_count": len(errors),
            "duration_ms": duration_ms,
        },
    )
    return {
        "board_id": board_id,
        "migrated": migrated,
        "columns_added": columns_added,
        "errors": errors,
        "duration_ms": duration_ms,
    }


def apply_schema_to_connection(conn) -> None:
    """Run all DDL against an already-open Kùzu connection.

    Every statement uses ``IF NOT EXISTS`` (or the equivalent try/except for
    ALTER ADD), so this is safe to invoke on every BoardConnection open. It's
    the migration path for boards bootstrapped under an older schema — the
    deterministic worker rolling out new rel tables (e.g. ``belongs_to``)
    relies on this re-running so existing boards don't need a destructive
    reset to pick up additions.
    """
    load_vector_extension(conn)
    conn.execute(_board_meta_ddl())
    # Spec MKG-D-S1 (FR1): legacy BoardMeta tables gain the embedding
    # columns via idempotent ALTER ADD (CREATE IF NOT EXISTS is a no-op on
    # pre-existing tables).
    _ensure_board_meta_embedding_columns(conn)
    for node_type in NODE_TYPES:
        conn.execute(_build_node_ddl(node_type))
        # v0.3.1: ensure priority_boost column exists on legacy boards. The
        # CREATE TABLE IF NOT EXISTS above is a no-op on pre-existing tables
        # so we still need to run the ALTER ADD path to backfill the column.
        _ensure_priority_boost_columns(conn, node_type)
        # v0.3.2 (spec 4007e4a3): human_curated marks human-edited nodes that
        # the agent UPDATE path must skip without explicit override.
        _ensure_human_curated_columns(conn, node_type)
        # v0.3.3 (spec 28583299 — Ideação #4): last_recomputed_at is the
        # ISO timestamp of the last relevance_score persist. Read by the
        # daily decay tick and kg_health for observability.
        _ensure_last_recomputed_at_columns(conn, node_type)
        # v0.3.6: graph partition metadata for canonical-only query surfaces.
        _ensure_kg_layer_columns(conn, node_type)
        # v0.3.8 (spec MKG-A-S1): supersedence generation for deterministic
        # node identity. NULL on legacy rows reads as 0 in core primitives.
        _ensure_generation_columns(conn, node_type)
        # v0.3.9 (spec MKG-B-S1): atomic provenance + graded attestation.
        # NULL on legacy rows: provenance stays NULL; attestation_count
        # reads as 1 in the scoring boost.
        _ensure_provenance_columns(conn, node_type)
        _ensure_attestation_columns(conn, node_type)
        # v0.3.10 (spec MKG-E-S1): declarative subtyping column.
        _ensure_subtype_columns(conn, node_type)
        _backfill_kg_layer_defaults(conn, node_type)
    for rel_name, from_type, to_type in REL_TYPES:
        conn.execute(_build_rel_ddl(rel_name, from_type, to_type))
        # v0.1.0 → v0.2.0 backfill: ALTER ADD the metadata cols on legacy
        # tables and tag any pre-existing rows so queries filtering by
        # layer stay correct.
        _ensure_edge_metadata_columns(conn, rel_name)
        _backfill_legacy_edge_metadata(conn, rel_name)

    # Multi-pair rel types (hierarchy backbone — `belongs_to`).
    supersedes_added: list[tuple[str, str]] = []
    for rel_name, pairs in MULTI_REL_TYPES:
        conn.execute(_build_multi_rel_ddl(rel_name, pairs))
        added_pairs = _ensure_multi_rel_pairs(conn, rel_name, pairs)
        if rel_name == "supersedes" and added_pairs:
            supersedes_added = added_pairs
        _ensure_edge_metadata_columns(conn, rel_name)
        _backfill_legacy_edge_metadata(conn, rel_name)
    # Spec MKG-D-S1 (FR5/AC9): fail-closed probe — a failed ALTER on the
    # supersedes conversion must never be an invisible no-op.
    missing_pairs = _probe_supersedes_pairs(conn)
    if missing_pairs:
        logger.error(
            "kg.supersedes.pairs_incomplete missing=%s",
            missing_pairs,
            extra={
                "event": "kg.supersedes.pairs_incomplete",
                "missing_pairs": [list(p) for p in missing_pairs],
            },
        )
        raise SupersedesPairsIncompleteError("apply_schema", missing_pairs)
    if supersedes_added:
        logger.info(
            "kg.supersedes.pairs_ensured added=%d",
            len(supersedes_added),
            extra={
                "event": "kg.supersedes.pairs_ensured",
                "added_pairs": [list(p) for p in supersedes_added],
            },
        )
    _ensure_vector_indexes(conn)


def bootstrap_board_graph(board_id: str) -> BoardGraphHandle:
    """Create or open a per-board Kùzu graph with the full MVP schema.

    Idempotent: re-invoking returns the same handle without re-creating tables.
    """
    try:
        import ladybug as kuzu  # type: ignore
    except ImportError as exc:  # pragma: no cover — deps required for runtime
        raise RuntimeError(
            "kuzu is required for the knowledge graph layer — "
            "install with `pip install kuzu`"
        ) from exc

    path = board_kuzu_path(board_id)
    # Sinalização forense (2026-06-10): criar um grafo NOVO para um board é
    # normal no primeiro uso, mas quando o arquivo anterior foi removido por
    # fora (deleção manual durante troubleshooting, por exemplo) este
    # bootstrap recriava um grafo VAZIO silenciosamente — o health então via
    # "empty_after_materialized_history" e o board entrava em
    # recovery_needed sem nenhuma pista no log de QUANDO o conteúdo sumiu.
    # O warning abaixo é a pista.
    if not path.exists():
        logger.warning(
            "kg.bootstrap.fresh_graph_created board=%s path=%s "
            "(se um grafo anterior existia e foi removido manualmente, "
            "re-materialize via historical consolidation/rebuild)",
            board_id, path,
            extra={
                "event": "kg.bootstrap.fresh_graph_created",
                "board_id": board_id,
            },
        )
    path.parent.mkdir(parents=True, exist_ok=True)

    # KGD-01 C6 (item 4): DDL de bootstrap registrado no close guard
    # (__enter__ manual para não re-indentar o corpo).
    raw_ctx = registered_raw_connection(board_id)
    _db, conn = raw_ctx.__enter__()
    try:
        apply_schema_to_connection(conn)

        # Vector indexes: one HNSW index per searchable node type. Kùzu 0.11
        # CREATE_VECTOR_INDEX takes (table, idx_name, col_name) positional +
        # named metric. We declare `cosine` explicitly so the `1 - distance`
        # conversion in search.py stays correct even if Kùzu's default metric
        # changes across versions.
        _ensure_vector_indexes(conn)

        # Record schema version on the BoardMeta singleton. Use DELETE+CREATE
        # so a re-bootstrap updates the version if the schema has evolved.
        # Spec MKG-D-S1 (FR1): preserve/stamp the embedding metadata across
        # the DELETE+CREATE (risk R4 — fields must survive re-bootstrap).
        prior_model, prior_dim = _read_board_meta_embedding(conn, board_id)
        _meta = _effective_embedding_meta()
        if not _meta.get("is_stub") and _meta.get("model_name") and int(
            _meta.get("embedding_dimension") or 0
        ) > 0 and not prior_model:
            prior_model = _meta.get("model_name")
            prior_dim = int(_meta.get("embedding_dimension") or 0)
        conn.execute(
            "MATCH (m:BoardMeta {board_id: $bid}) DELETE m",
            {"bid": board_id},
        )
        conn.execute(
            "CREATE (m:BoardMeta {board_id: $bid, schema_version: $v, "
            "bootstrapped_at: timestamp($ts), "
            "embedding_model: $emodel, embedding_dimension: $edim})",
            {
                "bid": board_id,
                "v": SCHEMA_VERSION,
                "ts": _now_iso(),
                "emodel": prior_model,
                "edim": prior_dim,
            },
        )
    finally:
        # Bug d0f6bab2: db is now process-cached (_board_db_cache); do NOT
        # close it here or concurrent BoardConnections lose the lock.
        # Cache is dropped explicitly via close_board_db_cache(). O __exit__
        # fecha só a Connection e desregistra o leitor no guard.
        raw_ctx.__exit__(None, None, None)

    return BoardGraphHandle(board_id=board_id, path=path, schema_version=SCHEMA_VERSION)


# Process-local cache of boards whose Kùzu graph has been bootstrapped in
# this process. Parallel to _MIGRATED_BOARDS but for the cold-path
# bootstrap step, not the ALTER-based migrations. Populated by
# ensure_board_graph_bootstrapped() which is called from BoardConnection.
_BOOTSTRAPPED_BOARDS: set[str] = set()

# Per-board lock so concurrent openers serialize the bootstrap. Kùzu's own
# file lock would catch cross-process races, but within one process asyncio
# tasks could both see the empty cache and both try to bootstrap.
_BOOTSTRAP_LOCKS: dict[str, threading.Lock] = {}
_BOOTSTRAP_LOCKS_GUARD = threading.Lock()


def _get_bootstrap_lock(board_id: str) -> threading.Lock:
    with _BOOTSTRAP_LOCKS_GUARD:
        lock = _BOOTSTRAP_LOCKS.get(board_id)
        if lock is None:
            lock = threading.Lock()
            _BOOTSTRAP_LOCKS[board_id] = lock
        return lock


def _graph_needs_bootstrap(board_id: str) -> bool:
    """Cheap probe: does the board's .kuzu path exist AND contain the
    BoardMeta node table?

    Returns True when the graph is missing entirely OR when it exists but
    lacks the BoardMeta table (signaling a partial bootstrap). False when
    the graph is present AND BoardMeta exists.
    """
    if board_id in _BOOTSTRAPPED_BOARDS:
        return False
    path = board_kuzu_path(board_id)
    if not path.exists():
        return True
    try:
        # KGD-01 C6 (item 4): probe de bootstrap registrado no close guard.
        with registered_raw_connection(board_id) as (_db, conn):
            res = conn.execute(
                "CALL SHOW_TABLES() WHERE name = 'BoardMeta' RETURN name"
            )
            has_meta = res.has_next()
            res.close()
        if has_meta:
            return False
        return True
    except Exception as exc:
        _raise_existing_graph_open_failed(
            board_id=board_id,
            path=path,
            operation="bootstrap_probe",
            exc=exc,
        )


def ensure_board_graph_bootstrapped(board_id: str) -> None:
    """Idempotent, thread-safe guarantee that the board's Kùzu graph exists
    with the current schema. Safe to call from any entry point — CLI, API,
    worker, MCP tool, search, health check.

    Called automatically by BoardConnection.__init__ so direct callers of
    open_board_connection and all primitives that open connections get
    the guarantee for free.

    Spec 818748f2 — FR1: when BoardMeta exists but post-v0.3.0 columns are
    missing (legacy boards bootstrapped pre-v0.3.2), `_migrate_board_schema`
    is dispatched in the same lock window so the next consolidation does not
    hit a binder exception. Cache add to `_BOOTSTRAPPED_BOARDS` happens AFTER
    the migration completes (BR3 — never cache a broken state).
    """
    if board_id in _BOOTSTRAPPED_BOARDS:
        return
    lock = _get_bootstrap_lock(board_id)
    with lock:
        if board_id in _BOOTSTRAPPED_BOARDS:
            return
        bootstrapped = False
        if _graph_needs_bootstrap(board_id):
            logger.info(
                "kg.schema.autobootstrap board=%s path=%s",
                board_id, board_kuzu_path(board_id),
                extra={"event": "kg.schema.autobootstrap", "board_id": board_id},
            )
            bootstrap_board_graph(board_id)
            _MIGRATED_BOARDS.add(board_id)
            bootstrapped = True
        elif (
            _board_needs_migration(board_id)
            or _board_needs_post_v030_migration(board_id)
        ):
            logger.info(
                "kg.schema.auto_migrate_post_v030 board=%s path=%s",
                board_id, board_kuzu_path(board_id),
                extra={
                    "event": "kg.schema.auto_migrate_post_v030",
                    "board_id": board_id,
                },
            )
            bootstrapped = _migrate_board_schema(board_id)
        else:
            _MIGRATED_BOARDS.add(board_id)
            bootstrapped = True
        # Spec MKG-D-S1 (FR2, decision D1): open-time embedding guard at the
        # single choke point. A mismatch raises BEFORE caching, so a blocked
        # board is re-checked (and can open again once the provider is
        # restored) instead of being cached as bootstrapped.
        _enforce_embedding_guard(board_id)
        if bootstrapped:
            _BOOTSTRAPPED_BOARDS.add(board_id)


def reset_bootstrap_cache_for_tests() -> None:
    """Test helper — clear the process-local bootstrap cache so the next
    open triggers a fresh bootstrap probe. Call from pytest fixtures that
    delete board directories mid-test."""
    _BOOTSTRAPPED_BOARDS.clear()
    with _BOOTSTRAP_LOCKS_GUARD:
        _BOOTSTRAP_LOCKS.clear()


def open_board_connection(board_id: str) -> BoardConnection:
    """Open a fresh Kùzu connection for a board as a :class:`BoardConnection`.

    Returns a :class:`BoardConnection` — use as a context manager
    (``with open_board_connection(bid) as (db, conn):``) to guarantee
    ``close()`` runs even under exceptions. The return value is also
    iterable, so legacy ``db, conn = open_board_connection(bid)`` sites
    continue to work during the retrofit.
    """
    return BoardConnection(board_id)


def open_board_connection_raw(board_id: str):
    """Deprecated: use ``with open_board_connection(bid) as (db, conn):``.

    Returns ``(db, conn)`` as a plain tuple — no context manager wrapping.
    The caller is responsible for ``del conn, db`` (and ideally a follow-up
    ``gc.collect()`` on Windows) to release the Kùzu file lock.

    Exists so legacy call sites can be migrated incrementally across
    several PRs without forcing a single-commit flag-day rewrite.
    """
    import warnings

    warnings.warn(
        "open_board_connection_raw is deprecated; use "
        "`with open_board_connection(board_id) as (db, conn):` instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return tuple(BoardConnection(board_id))


def close_all_connections(board_id: str | None = None) -> None:
    """Release Kùzu connections so the underlying ``.kuzu`` dirs can be rmtree'd.

    ``board_id=None``: close the global discovery singleton *and* every
    per-board connection the pool is holding.

    ``board_id=<id>``: close only that board's pooled connection. The global
    singleton is left alone because it points at a different ``.kuzu`` dir.

    Idempotent and best-effort: missing pool (card 1.2 not yet landed) or an
    already-closed global are both no-ops. The primary consumer is the
    right-to-erasure path, which needs every handle released before the
    rmtree runs on Windows.
    """
    try:
        from okto_pulse.community.adapters.graph_connection_pool import (  # type: ignore
            close_board_connection,
            close_all_board_connections,
        )
    except ImportError:
        close_board_connection = None  # type: ignore[assignment]
        close_all_board_connections = None  # type: ignore[assignment]

    if board_id is not None:
        if close_board_connection is not None:
            try:
                close_board_connection(board_id)
            except Exception as exc:
                logger.warning(
                    "close_all.board_failed board=%s err=%s", board_id, exc,
                    extra={
                        "event": "close_all.board_failed",
                        "board_id": board_id,
                    },
                )
        # Bug d0f6bab2: drop the cached Database for this board so a
        # follow-up rmtree (or migration) can grab the OS lock.
        close_board_db_cache(board_id=board_id)
        return

    if close_all_board_connections is not None:
        try:
            close_all_board_connections()
        except Exception as exc:
            logger.warning(
                "close_all.pool_failed err=%s", exc,
                extra={"event": "close_all.pool_failed"},
            )

    # Bug d0f6bab2: also drop the per-board Database cache so the OS file
    # lock is released. Without this, even after legacy pool eviction the
    # BoardConnection cache keeps the .kuzu dir locked.
    close_board_db_cache(board_id=None)

    # global is released only when closing everything — per-board callers
    # (e.g. single-board DELETE) must not nuke the shared discovery handle.
    from okto_pulse.core.services.application_kg import get_current_provider_registry

    get_current_provider_registry().require_global_discovery_runtime().close()


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
