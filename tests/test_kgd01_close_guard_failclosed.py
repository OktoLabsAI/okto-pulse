"""KGD-01 C6/TC6 — Close guard FAIL-CLOSED, registro universal de leitores e
serve-lock na CLI (spec 26b46ef3, board 2cd4d5ac).

Cenários do test card 97764bbf:

* S9 (ts_0393503b) — stress fail-closed: N threads fazendo open/read/write
  via BoardConnection em loop concorrente com close_board_db_cache,
  try_close_board_db e eviction LRU (KG_DB_CACHE_CAP=1); instrumentação da
  fábrica + de ``ladybug.Database.close`` prova que NENHUM ``db.close()``
  acontece com leitores registrados (>0) no modo runtime; leitor longo força
  o caminho deferido; reader_enter durante closing é fail-closed
  (``BoardCloseInProgressError`` OU espera-e-sucesso — nunca entrada
  fail-open); ao final, reopen + contagem consistente; ZERO eventos
  ``kg.close_guard.timeout`` (o código runtime nem tem mais esse caminho).

* S9b — caminho de shutdown: ``force_after_drain_timeout=True`` fecha mesmo
  com leitor vazado e loga ``kg.close_guard.forced_on_shutdown``; o default
  fail-closed adia (``kg.close_guard.deferred``). Cobre também o caller real
  (``kg_shutdown.close_all_graphs_on_shutdown``).

* S10 (ts_733b4ac4) — serve-lock na CLI: com lock de heartbeat fresco e PID
  vivo (o do próprio teste), os entrypoints ``init``, ``verify-pipeline``,
  ``kg backfill --apply`` e ``kg dedup-entities`` falham rápido (<5s, exit 2)
  com mensagem de serve-lock e NENHUM open de Database (fábrica
  instrumentada); heartbeat stale + PID morto → prossegue (takeover
  permitido); heartbeat stale + PID vivo → recusa. Inclui o guard do
  ``scripts/run_kg_tick_once.py`` (core) via subprocess.

Timeouts do guard encurtados via monkeypatch dos módulo-level do kg_runtime
(lookup em runtime) para o teste ser rápido. Nenhum grafo real de
``~/.okto-pulse`` é tocado (kg_base_dir monkeypatched para tmp).
"""

from __future__ import annotations

import gc
import json
import logging
import os
import subprocess
import sys
import threading
import time
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ladybug
from okto_pulse.community import serve_lock
from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters.graph_memory_pressure import (
    GraphMemoryPressure,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphLockContention
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core.infra.config import configure_settings, get_settings
from repo_layout import resolve_core_repo

KG_LOGGER = "okto_pulse.kg.schema"


@pytest.fixture(autouse=True)
def _restore_core_settings():
    """Snapshot + restore do singleton CoreSettings em volta de cada teste."""
    original = get_settings()
    yield
    configure_settings(original)


@pytest.fixture()
def kg_env(tmp_path: Path, monkeypatch):
    """KG base isolado em tmp + caches limpos antes/depois."""
    base = tmp_path / "kgbase"
    base.mkdir()
    configure_settings(
        CommunitySettings(
            data_dir=str(tmp_path / "pulse"),
            kg_kuzu_buffer_pool_mb=128,
            kg_kuzu_max_db_size_gb=2,
            kg_base_dir=str(base),
        )
    )
    monkeypatch.setattr(kg_runtime, "_kg_base_dir", lambda: base)
    kg_runtime.close_board_db_cache(None, force_after_drain_timeout=True)
    try:
        yield base
    finally:
        # Higiene: zera qualquer leitor preso deixado por um teste falho para
        # o force-close do teardown não esperar o dreno inteiro.
        for guard in list(kg_runtime._board_close_guards.values()):
            with guard._cond:
                guard._readers = 0
                guard._owner_readers = 0
                guard._cond.notify_all()
        kg_runtime.close_board_db_cache(None, force_after_drain_timeout=True)
        kg_runtime.reset_bootstrap_cache_for_tests()
        gc.collect()


@pytest.fixture()
def close_instrumentation(monkeypatch):
    """Instrumenta a fábrica + ``ladybug.Database.close`` (TC6/S9).

    Registra board por ``id(db)`` no open e, a cada ``close()``, captura o
    número de leitores registrados no guard daquele board. Retorna a lista
    de violações ``(board_id, readers)`` — deve ser VAZIA no modo runtime.
    """
    violations: list[tuple[str, int]] = []
    db_boards: dict[int, str] = {}
    real_open = kg_runtime._open_kuzu_db

    def tracking_open(path):
        db = real_open(path)
        db_boards[id(db)] = Path(str(path)).parent.name
        return db

    monkeypatch.setattr(kg_runtime, "_open_kuzu_db", tracking_open)

    real_close = ladybug.Database.close

    def guarded_close(self, *args, **kwargs):
        board = db_boards.get(id(self))
        if board is not None:
            readers = kg_runtime._get_close_guard(board).readers
            if readers > 0:
                violations.append((board, readers))
        return real_close(self, *args, **kwargs)

    monkeypatch.setattr(ladybug.Database, "close", guarded_close)
    return violations


def _seed_board(board_id: str, *, rows: int = 0) -> Path:
    """Grafo mínimo criado pela fábrica única do kg_runtime (conexão crua
    registrada — dogfooding do item 4) + caches de bootstrap pré-populados
    para o BoardConnection pular o DDL completo (fora do escopo do stress)."""
    path = kg_runtime.board_kuzu_path(board_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with kg_runtime.registered_raw_connection(board_id) as (_db, conn):
        conn.execute(
            "CREATE NODE TABLE IF NOT EXISTS StressProbe(id INT64, PRIMARY KEY(id))"
        )
        for i in range(rows):
            conn.execute(f"CREATE (:StressProbe {{id: {i}}})")
    kg_runtime._BOOTSTRAPPED_BOARDS.add(board_id)
    kg_runtime._MIGRATED_BOARDS.add(board_id)
    return path


def _events(caplog) -> list[str]:
    return [getattr(rec, "event", None) for rec in caplog.records]


def _count_probe_rows(board_id: str) -> int:
    with kg_runtime.registered_raw_connection(board_id) as (_db, conn):
        res = conn.execute("MATCH (n:StressProbe) RETURN count(n)")
        try:
            return int(res.get_next()[0])
        finally:
            res.close()


def test_board_storage_mutation_window_drains_or_fails_closed(kg_env):
    from okto_pulse.community.adapters.ladybug_writer import (
        writer_lease_is_active,
    )

    board_id = "kgd01-governed-storage-window"
    _seed_board(board_id)
    active_reader = kg_runtime.BoardConnection(board_id)
    entered = False
    try:
        with pytest.raises(GraphLockContention):
            with kg_runtime.board_storage_mutation_window(
                board_id,
                phase="test_restore",
                drain_timeout=0.05,
            ):
                entered = True
    finally:
        active_reader.close()

    assert entered is False

    with kg_runtime.board_storage_mutation_window(
        board_id,
        phase="test_restore",
        drain_timeout=1.0,
    ):
        assert writer_lease_is_active() is True
        assert kg_runtime._get_close_guard(board_id).readers == 0


def test_board_graph_operation_window_pins_and_releases_on_failure(kg_env):
    board_id = "kgd01-routed-reader-window"
    guard = kg_runtime._get_close_guard(board_id)

    with pytest.raises(RuntimeError, match="operation_failed"):
        with kg_runtime.board_graph_operation_window(board_id):
            assert guard.readers == 1
            raise RuntimeError("operation_failed")

    assert guard.readers == 0


def test_board_graph_operation_window_blocks_storage_mutation(kg_env):
    board_id = "kgd01-routed-reader-drain"
    entered = False

    with kg_runtime.board_graph_operation_window(board_id):
        with pytest.raises(GraphLockContention) as exc_info:
            with kg_runtime.board_storage_mutation_window(
                board_id,
                phase="test_grafx_restore",
                drain_timeout=0.02,
            ):
                entered = True

    assert entered is False
    assert exc_info.value.details["stuck_readers"] == 1
    assert kg_runtime._get_close_guard(board_id).readers == 0


@pytest.mark.parametrize("board_id", ["", None, 7])
def test_board_graph_operation_window_rejects_invalid_scope(kg_env, board_id):
    with pytest.raises(ValueError, match="board_graph_operation_window_invalid"):
        with kg_runtime.board_graph_operation_window(board_id):
            raise AssertionError("invalid board must never enter")


# ---------------------------------------------------------------------------
# S9 — ts_0393503b: stress concorrente, zero close com leitor registrado
# ---------------------------------------------------------------------------

_S9_BOARD = "kgd01-s9-stress"
_S9_EVICT_BOARD = "kgd01-s9-evict"
_S9_TARGET_ITERATIONS = 1200
_S9_MIN_ITERATIONS = 1000
_S9_WORKERS = 8
_S9_DEADLINE_S = 120.0


def test_s9_stress_close_guard_is_fail_closed(
    kg_env, close_instrumentation, monkeypatch, caplog
):
    monkeypatch.setenv("KG_DB_CACHE_CAP", "1")
    monkeypatch.setattr(kg_runtime, "_CLOSE_DRAIN_TIMEOUT_S", 0.5)
    monkeypatch.setattr(kg_runtime, "_READER_ENTER_TIMEOUT_S", 1.0)
    monkeypatch.setattr(kg_runtime, "_HYGIENE_CLOSE_DRAIN_TIMEOUT_S", 0.3)

    _seed_board(_S9_BOARD)
    _seed_board(_S9_EVICT_BOARD)

    caplog.set_level(logging.INFO, logger=KG_LOGGER)

    stop = threading.Event()
    state_lock = threading.Lock()
    iterations = 0
    write_ids: list[int] = []
    next_write_id = iter(range(10_000_000))
    open_refusals = 0
    op_errors: list[str] = []
    unexpected_open_errors: list[str] = []

    def is_resident_admission_pressure(exc: GraphMemoryPressure) -> bool:
        return exc.details.get("admission_reason_code") == "resident_databases_pinned"

    def bump_iterations() -> int:
        nonlocal iterations
        with state_lock:
            iterations += 1
            return iterations

    def worker(tid: int) -> None:
        nonlocal open_refusals
        i = 0
        while not stop.is_set():
            i += 1
            try:
                bc = kg_runtime.BoardConnection(_S9_BOARD)
            except kg_runtime.BoardCloseInProgressError:
                # Fail-closed permitido: janela closing ativa além do timeout.
                with state_lock:
                    open_refusals += 1
                bump_iterations()
                continue
            except GraphMemoryPressure as exc:
                if is_resident_admission_pressure(exc):
                    with state_lock:
                        open_refusals += 1
                    bump_iterations()
                    continue
                with state_lock:
                    unexpected_open_errors.append(repr(exc))
                bump_iterations()
                continue
            except Exception as exc:  # noqa: BLE001 — falha de open é bug
                with state_lock:
                    unexpected_open_errors.append(repr(exc))
                bump_iterations()
                continue
            try:
                if i % 5 == 0:
                    with state_lock:
                        wid = next(next_write_id)
                    bc.conn.execute(f"CREATE (:StressProbe {{id: {wid}}})")
                    with state_lock:
                        write_ids.append(wid)
                else:
                    res = bc.conn.execute("MATCH (n:StressProbe) RETURN count(n)")
                    res.get_next()
                    res.close()
            except Exception as exc:  # noqa: BLE001 — conflito de escrita etc.
                with state_lock:
                    op_errors.append(repr(exc))
            finally:
                bc.close()
            bump_iterations()

    def legit_closer() -> None:
        while not stop.is_set():
            kg_runtime.close_board_db_cache(_S9_BOARD)
            time.sleep(0.05)

    def hygiene_closer() -> None:
        while not stop.is_set():
            kg_runtime.try_close_board_db(_S9_BOARD)
            time.sleep(0.07)

    def evictor() -> None:
        # KG_DB_CACHE_CAP=1: abrir o segundo board força tentativa de
        # eviction LRU do board de stress (discricionária — deve pular
        # quando há leitores).
        while not stop.is_set():
            try:
                bc = kg_runtime.BoardConnection(_S9_EVICT_BOARD)
                bc.close()
            except kg_runtime.BoardCloseInProgressError:
                pass
            except GraphMemoryPressure as exc:
                if not is_resident_admission_pressure(exc):
                    with state_lock:
                        unexpected_open_errors.append(repr(exc))
            time.sleep(0.1)

    def long_reader() -> None:
        # Leitor longo: segura o board por MUITO mais que o drain timeout
        # (0.5s) — todo close legítimo concorrente TEM de ser adiado.
        try:
            bc = kg_runtime.BoardConnection(_S9_BOARD)
        except kg_runtime.BoardCloseInProgressError:
            return
        except GraphMemoryPressure as exc:
            if is_resident_admission_pressure(exc):
                return
            with state_lock:
                unexpected_open_errors.append(repr(exc))
            return
        try:
            time.sleep(1.5)
            res = bc.conn.execute("MATCH (n:StressProbe) RETURN count(n)")
            res.get_next()
            res.close()
        finally:
            bc.close()

    def runtime_thread(target, *args) -> threading.Thread:
        context = copy_context()
        return threading.Thread(
            target=context.run,
            args=(target, *args),
            daemon=True,
        )

    threads = [runtime_thread(worker, t) for t in range(_S9_WORKERS)]
    threads.append(runtime_thread(legit_closer))
    threads.append(runtime_thread(hygiene_closer))
    threads.append(runtime_thread(evictor))
    threads.append(runtime_thread(long_reader))
    for t in threads:
        t.start()

    deadline = time.monotonic() + _S9_DEADLINE_S
    while time.monotonic() < deadline:
        with state_lock:
            done = iterations >= _S9_TARGET_ITERATIONS
        if done:
            break
        time.sleep(0.1)
    stop.set()
    for t in threads:
        t.join(timeout=30.0)
    assert all(not t.is_alive() for t in threads), "thread presa no stress"

    assert iterations >= _S9_MIN_ITERATIONS, (
        f"apenas {iterations} iterações agregadas no budget "
        f"(mínimo {_S9_MIN_ITERATIONS})"
    )
    # Contrato central do C6: NENHUM db.close() com leitores registrados.
    assert close_instrumentation == [], (
        f"db.close() com leitores ativos (fail-open!): {close_instrumentation}"
    )
    assert unexpected_open_errors == [], (
        "opens falharam fora dos modos fail-closed esperados: "
        f"{unexpected_open_errors[:5]}"
    )
    events = _events(caplog)
    assert "kg.close_guard.timeout" not in events, (
        "evento fail-open kg.close_guard.timeout emitido no modo runtime"
    )
    assert not any("fail-open" in rec.getMessage() for rec in caplog.records), (
        "log de fail-open emitido no modo runtime"
    )
    # O caminho fail-closed foi de fato exercitado (leitor longo × closers).
    assert (
        "kg.close_guard.deferred" in events
        or "kg.hygiene.close_skipped_active_readers" in events
    ), f"nenhum close adiado registrado; eventos={set(events)}"

    # Reopen + contagem consistente: tudo que reportou sucesso de escrita
    # está presente após close total + reopen do zero.
    kg_runtime.close_board_db_cache(_S9_BOARD)
    key = str(kg_runtime.board_kuzu_path(_S9_BOARD))
    assert key not in kg_runtime._board_db_cache, (
        "close final deveria ter drenado (todos os leitores saíram)"
    )
    assert _count_probe_rows(_S9_BOARD) == len(write_ids), (
        f"contagem inconsistente pós-reopen: esperado {len(write_ids)}"
    )
    # Diagnóstico (não-fatal): conflitos de escrita concorrente são aceitos,
    # mas nunca podem ser a maioria das operações.
    assert len(op_errors) < iterations / 2, (
        f"erros demais nas operações: {len(op_errors)}/{iterations} "
        f"(amostra: {op_errors[:3]})"
    )


def test_s9_reader_enter_during_closing_is_fail_closed(kg_env, monkeypatch):
    """reader_enter durante closing: BoardCloseInProgressError OU espera e
    sucesso — nunca entrada fail-open com a janela ativa."""
    monkeypatch.setattr(kg_runtime, "_READER_ENTER_TIMEOUT_S", 0.2)
    board = "kgd01-s9-enter"
    _seed_board(board)
    guard = kg_runtime._get_close_guard(board)

    entered = threading.Event()
    release = threading.Event()

    def hold_window() -> None:
        with guard.closing(timeout=0.05):
            entered.set()
            release.wait(5.0)

    holder = threading.Thread(target=hold_window, daemon=True)
    holder.start()
    assert entered.wait(2.0), "janela closing não abriu"

    # Janela ativa além do timeout do leitor → exceção estruturada, com
    # board_id, em vez da entrada fail-open antiga.
    with pytest.raises(kg_runtime.BoardCloseInProgressError) as exc_info:
        kg_runtime.BoardConnection(board)
    assert exc_info.value.board_id == board
    assert guard.readers == 0, "leitor não pode ficar registrado após recusa"

    release.set()
    holder.join(timeout=5.0)

    # Janela encerrada → entrada volta a funcionar (espera e sucesso).
    bc = kg_runtime.BoardConnection(board)
    try:
        assert guard.readers == 1
    finally:
        bc.close()
    assert guard.readers == 0


def test_s9_closing_windows_are_mutually_exclusive(kg_env):
    """Item 3 do C6: duas janelas closing() do mesmo board não se sobrepõem."""
    guard = kg_runtime._get_close_guard("kgd01-s9-serial")

    in_first = threading.Event()
    release = threading.Event()

    def first_window() -> None:
        with guard.closing(timeout=0.5):
            in_first.set()
            release.wait(5.0)

    holder = threading.Thread(target=first_window, daemon=True)
    holder.start()
    assert in_first.wait(2.0)

    # Segunda janela com a primeira ativa: NÃO sobrepõe — reporta drained
    # False (fail-closed para o caller) sem abrir janela própria.
    started = time.monotonic()
    with guard.closing(timeout=0.2) as (drained, _stuck):
        assert drained is False
    assert time.monotonic() - started < 2.0

    release.set()
    holder.join(timeout=5.0)

    # Primeira liberada → janela abre e drena normalmente.
    with guard.closing(timeout=0.5) as (drained, stuck):
        assert drained is True
        assert stuck == 0


def test_s9_checkpoint_owner_path_does_not_deadlock(kg_env):
    """Item 4 do C6: o CHECKPOINT (dono da janela) roda DENTRO da janela
    exclusiva sem deadlock e sem contar como leitor no dreno."""
    board = "kgd01-s9-owner"
    path = _seed_board(board, rows=2)
    guard = kg_runtime._get_close_guard(board)

    with guard.closing(timeout=1.0) as (drained, _stuck):
        assert drained is True
        started = time.monotonic()
        kg_runtime._execute_checkpoint_unguarded(path)
        assert time.monotonic() - started < 5.0, "owner path deadlockou?"
        assert guard.readers == 0, "owner não pode contar como reader"
    assert guard.owner_readers == 0


# ---------------------------------------------------------------------------
# S9b — shutdown: force_after_drain_timeout fecha com leitor vazado + log
# ---------------------------------------------------------------------------


def test_s9b_shutdown_force_closes_leaked_reader_with_explicit_log(
    kg_env, close_instrumentation, monkeypatch, caplog
):
    monkeypatch.setattr(kg_runtime, "_CLOSE_DRAIN_TIMEOUT_S", 0.3)
    monkeypatch.setattr(kg_runtime, "_SHUTDOWN_CLOSE_DRAIN_TIMEOUT_S", 0.3)
    board = "kgd01-s9b-shutdown"
    path = _seed_board(board, rows=3)
    key = str(path)

    # Popula o cache e simula um leitor VAZADO (registro no guard sem
    # conexão viva — seguro nativamente; o guard é o objeto sob teste).
    kg_runtime._open_kuzu_db_path_cached(path)
    guard = kg_runtime._get_close_guard(board)
    guard.reader_enter()
    try:
        caplog.set_level(logging.INFO, logger=KG_LOGGER)

        # Default (runtime): FAIL-CLOSED — adia, não fecha, loga deferred.
        kg_runtime.close_board_db_cache(board)
        assert key in kg_runtime._board_db_cache, (
            "fail-closed: Database não pode ser fechado com leitor vivo"
        )
        events = _events(caplog)
        assert "kg.close_guard.deferred" in events
        assert "kg.close_guard.forced_on_shutdown" not in events
        deferred = next(
            r
            for r in caplog.records
            if getattr(r, "event", None) == "kg.close_guard.deferred"
        )
        assert deferred.board_id == board
        assert deferred.stuck_readers >= 1
        assert deferred.timeout_s == pytest.approx(0.3)

        # Shutdown: força após o dreno, com registro explícito.
        caplog.clear()
        kg_runtime.close_board_db_cache(board, force_after_drain_timeout=True)
        assert key not in kg_runtime._board_db_cache, (
            "shutdown force deveria ter fechado mesmo com leitor vazado"
        )
        events = _events(caplog)
        assert "kg.close_guard.forced_on_shutdown" in events
        forced = next(
            r
            for r in caplog.records
            if getattr(r, "event", None) == "kg.close_guard.forced_on_shutdown"
        )
        assert forced.board_id == board
        assert forced.stuck_readers >= 1
        # A instrumentação DEVE ter visto o close com leitor registrado — é
        # exatamente o caso forçado (e só ele).
        assert close_instrumentation == [(board, 1)]
    finally:
        guard.reader_exit()

    # Reopen limpo: dados intactos.
    assert _count_probe_rows(board) == 3


def test_s9b_close_all_graphs_on_shutdown_uses_force_path(kg_env, monkeypatch, caplog):
    """O caller real de C2 (kg_shutdown) atravessa o force path do C6."""
    from okto_pulse.community.adapters.kg_shutdown import (
        close_all_graphs_on_shutdown,
    )

    monkeypatch.setattr(kg_runtime, "_SHUTDOWN_CLOSE_DRAIN_TIMEOUT_S", 0.3)
    board = "kgd01-s9b-teardown"
    path = _seed_board(board, rows=2)
    key = str(path)

    kg_runtime._open_kuzu_db_path_cached(path)
    guard = kg_runtime._get_close_guard(board)
    guard.reader_enter()  # leitor vazado no teardown
    try:
        caplog.set_level(logging.INFO, logger=KG_LOGGER)
        summary = close_all_graphs_on_shutdown(runtime=kg_runtime)
        assert key not in kg_runtime._board_db_cache, (
            "shutdown não fechou o grafo com leitor vazado"
        )
        assert summary["boards_closed"] >= 1
        assert "kg.close_guard.forced_on_shutdown" in _events(caplog)
    finally:
        guard.reader_exit()
    assert _count_probe_rows(board) == 2


# ---------------------------------------------------------------------------
# S10 — ts_733b4ac4: serve-lock na CLI (fail-fast <5s, zero open de Database)
# ---------------------------------------------------------------------------


def _write_serve_lock(data_dir: Path, *, pid: int, age_seconds: float) -> Path:
    lock_path = data_dir / serve_lock.LOCK_FILENAME
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": pid,
                "data_dir": str(data_dir),
                "created_at": stamp.isoformat(),
                "heartbeat_at": stamp.isoformat(),
                "heartbeat_interval_seconds": serve_lock.HEARTBEAT_INTERVAL_SECONDS,
                "heartbeat_ttl_seconds": serve_lock.HEARTBEAT_TTL_SECONDS,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return lock_path


@pytest.fixture()
def dead_pid() -> int:
    """PID comprovadamente morto: subprocess que já saiu. O handle do Popen
    fica aberto durante o teste, então o Windows não recicla o número."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=60)
    yield proc.pid


@pytest.mark.parametrize(
    "argv",
    [
        ["init"],
        ["verify-pipeline", "board-s10"],
        ["kg", "backfill", "board-s10", "--apply"],
        ["kg", "dedup-entities", "board-s10"],
    ],
    ids=["init", "verify-pipeline", "kg-backfill-apply", "kg-dedup-entities"],
)
def test_s10_cli_entrypoints_fail_fast_with_live_serve_lock(
    tmp_path: Path, monkeypatch, capsys, argv
):
    from okto_pulse.community import cli

    data_dir = tmp_path / "pulse-data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("OKTO_PULSE_NO_BANNER", "1")
    # Heartbeat fresco + PID do PRÓPRIO processo de teste (vivo).
    _write_serve_lock(data_dir, pid=os.getpid(), age_seconds=1.0)

    opened: list[str] = []

    def forbidden_open(path):
        opened.append(str(path))
        raise AssertionError(f"Database open sob serve-lock ativo: {path}")

    monkeypatch.setattr(kg_runtime, "_open_kuzu_db", forbidden_open)
    monkeypatch.setattr(sys, "argv", ["okto-pulse", *argv])

    started = time.monotonic()
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    elapsed = time.monotonic() - started

    assert exc_info.value.code == 2, f"exit code {exc_info.value.code} != 2"
    assert elapsed < 5.0, f"fail-fast estourou o budget: {elapsed:.1f}s"
    err = capsys.readouterr().err
    assert "serve-lock" in err, f"mensagem sem serve-lock: {err[:400]}"
    assert "already using this data directory" in err
    assert opened == [], "NENHUM open de Database é permitido sob serve-lock"


def test_s10_stale_heartbeat_dead_pid_allows_cli(
    tmp_path: Path, monkeypatch, dead_pid: int
):
    """Heartbeat stale + PID morto → o guard da CLI permite prosseguir
    (takeover permitido); a checagem direta não levanta."""
    data_dir = tmp_path / "pulse-data"
    _write_serve_lock(
        data_dir,
        pid=dead_pid,
        age_seconds=serve_lock.HEARTBEAT_TTL_SECONDS + 300,
    )
    # Não levanta:
    serve_lock.assert_no_live_server(data_dir, operation="test")


def test_s10_stale_heartbeat_live_pid_refuses_cli(tmp_path: Path):
    """Heartbeat stale + PID vivo (o nosso) → recusa (KGD-01 FR6)."""
    data_dir = tmp_path / "pulse-data"
    _write_serve_lock(
        data_dir,
        pid=os.getpid(),
        age_seconds=serve_lock.HEARTBEAT_TTL_SECONDS + 300,
    )
    with pytest.raises(serve_lock.ServeAlreadyRunningError) as exc_info:
        serve_lock.assert_no_live_server(data_dir, operation="test")
    assert "serve-lock" in str(exc_info.value)


def test_s10_fresh_heartbeat_dead_pid_still_refuses_cli(tmp_path: Path, dead_pid: int):
    """Heartbeat fresco → recusa mesmo com PID morto (servidor pode ter
    morrido há segundos, com WAL/handles em estado transitório)."""
    data_dir = tmp_path / "pulse-data"
    _write_serve_lock(data_dir, pid=dead_pid, age_seconds=1.0)
    with pytest.raises(serve_lock.ServeAlreadyRunningError):
        serve_lock.assert_no_live_server(data_dir, operation="test")


def test_s10_no_lock_file_allows_cli(tmp_path: Path):
    serve_lock.assert_no_live_server(tmp_path / "empty", operation="test")


def test_s10_serve_lock_takeover_requires_dead_pid(tmp_path: Path, dead_pid: int):
    """acquire(): heartbeat stale + PID morto (real) → takeover; heartbeat
    stale + PID vivo (real, o nosso) → recusa. Sem monkeypatch do check de
    PID — exercita OpenProcess/GetExitCodeProcess de verdade."""
    data_dir = tmp_path / "pulse-data"

    # PID morto → takeover permitido.
    _write_serve_lock(
        data_dir,
        pid=dead_pid,
        age_seconds=serve_lock.HEARTBEAT_TTL_SECONDS + 300,
    )
    lock = serve_lock.ServeInstanceLock(data_dir).acquire()
    try:
        payload = json.loads(
            (data_dir / serve_lock.LOCK_FILENAME).read_text(encoding="utf-8")
        )
        assert payload["pid"] == os.getpid()
    finally:
        lock.release()

    # PID vivo → recusa mesmo com heartbeat stale (KGD-01 FR6).
    _write_serve_lock(
        data_dir,
        pid=os.getpid(),
        age_seconds=serve_lock.HEARTBEAT_TTL_SECONDS + 300,
    )
    with pytest.raises(serve_lock.ServeAlreadyRunningError):
        serve_lock.ServeInstanceLock(data_dir).acquire()


_CORE_TICK_SCRIPT = (
    resolve_core_repo(Path(__file__).resolve().parents[1])
    / "scripts"
    / "run_kg_tick_once.py"
)


@pytest.mark.skipif(
    not _CORE_TICK_SCRIPT.exists(),
    reason="checkout do core ausente neste ambiente",
)
def test_s10_run_kg_tick_once_refuses_live_serve_lock(tmp_path: Path):
    """scripts/run_kg_tick_once.py (core) tem o mesmo check de serve-lock."""
    data_dir = tmp_path / "pulse-data"
    (data_dir / "data").mkdir(parents=True)
    (data_dir / "data" / "pulse.db").write_bytes(b"")  # passa o check de DB
    _write_serve_lock(data_dir, pid=os.getpid(), age_seconds=1.0)

    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(repo_root / "src"),
            str(_CORE_TICK_SCRIPT.parents[1] / "src"),
            env.get("PYTHONPATH", ""),
        ]
    )
    proc = subprocess.run(
        [sys.executable, str(_CORE_TICK_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 2, (
        f"rc={proc.returncode}; stdout={proc.stdout[-500:]} "
        f"stderr={proc.stderr[-1000:]}"
    )
    assert "serve-lock" in proc.stderr
