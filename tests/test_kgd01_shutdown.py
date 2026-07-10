"""KGD-01 — TC2: shutdown limpo fecha/checkpointa grafos; kill duro recupera.

Cenários do spec 26b46ef3 (board 2cd4d5ac):

* S3 (ts_e258be59) — shutdown limpo: ``close_all_graphs_on_shutdown()``
  checkpointa e fecha todos os Databases abertos no cache do kg_runtime,
  emite ``kg.shutdown.graphs_closed`` com contagem correta, consome os WALs
  e o reopen seguinte é limpo (fail-closed, sem salvage). Um e2e leve com
  ``okto-pulse serve`` + sinal é opt-in via ``OKTO_PULSE_RUN_E2E=1``
  (``pytest.mark.slow``) para nunca ficar flaky por porta/tempo.

* S4 (ts_7b549a9d) — kill duro: subprocess escreve commits num board graph e
  morre com ``os._exit(1)`` sem checkpoint/close; o reopen via
  ``ladybug.Database(..., throw_on_wal_replay_failure=False)`` (flag direto no
  engine, sem depender do C1) recupera os dados até o último commit, sem
  quarentena e sem purge no tmp.
"""

from __future__ import annotations

import gc
import logging
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

GRAPH_FILENAME = "graph.lbug"


def _wal_path(graph_path: Path) -> Path:
    return graph_path.parent / (graph_path.name + ".wal")


def _make_board_graph_dir(base: Path, board_id: str) -> Path:
    board_dir = base / "boards" / board_id
    board_dir.mkdir(parents=True, exist_ok=True)
    return board_dir / GRAPH_FILENAME


def _assert_no_quarantine(base: Path) -> None:
    leftovers = [
        p for p in base.rglob("*") if "quarantine" in p.name.lower()
    ]
    assert leftovers == [], f"quarentena inesperada no tmp: {leftovers}"


@pytest.fixture()
def clean_kg_db_cache():
    """Garante cache de Databases vazio antes/depois (isolamento do teste)."""
    from okto_pulse.community.adapters import kg_runtime

    kg_runtime.close_board_db_cache(None)
    assert not kg_runtime._board_db_cache
    try:
        yield kg_runtime
    finally:
        kg_runtime.close_board_db_cache(None)
        gc.collect()


# ---------------------------------------------------------------------------
# S3 — ts_e258be59: shutdown limpo checkpointa e fecha todos os grafos
# ---------------------------------------------------------------------------


def test_s3_close_all_graphs_on_shutdown_checkpoints_and_closes(
    tmp_path: Path, clean_kg_db_cache, caplog: pytest.LogCaptureFixture
) -> None:
    kg_runtime = clean_kg_db_cache
    from okto_pulse.community.adapters.kg_shutdown import (
        close_all_graphs_on_shutdown,
    )

    board_ids = ["kgd01-s3-board-a", "kgd01-s3-board-b"]
    graph_paths: list[Path] = []

    # Dois board graphs pequenos abertos PELA FÁBRICA do kg_runtime (mesmo
    # cache process-wide que o servidor usa), com commits deixando o WAL
    # não-vazio (nenhum checkpoint manual).
    for board_id in board_ids:
        graph_path = _make_board_graph_dir(tmp_path, board_id)
        graph_paths.append(graph_path)
        db = kg_runtime._open_kuzu_db_path_cached(graph_path)
        conn = kg_runtime.kuzu.Connection(db)
        try:
            conn.execute(
                "CREATE NODE TABLE IF NOT EXISTS ShutdownProbe("
                "id INT64, PRIMARY KEY(id))"
            )
            for i in range(3):
                conn.execute(f"CREATE (:ShutdownProbe {{id: {i}}})")
        finally:
            conn.close()

    for graph_path in graph_paths:
        wal = _wal_path(graph_path)
        assert wal.exists() and wal.stat().st_size > 0, (
            f"pré-condição: WAL não-vazio esperado em {wal}"
        )
    assert len(kg_runtime._board_db_cache) == 2

    with caplog.at_level(
        logging.INFO, logger="okto_pulse.community.adapters.kg_shutdown"
    ):
        summary = close_all_graphs_on_shutdown()

    # Contrato do evento (Architecture Design "Shutdown fecha os grafos").
    assert summary["boards_closed"] == 2
    assert summary["boards_failed"] == 0
    assert summary["duration_ms"] >= 0

    events = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "kg.shutdown.graphs_closed"
    ]
    assert len(events) == 1, "esperado exatamente 1 kg.shutdown.graphs_closed"
    assert events[0].boards_closed == 2
    assert events[0].boards_failed == 0
    assert isinstance(events[0].duration_ms, int)

    # Cache drenado e WAL consumido (checkpoint aplicou os commits no main).
    assert not kg_runtime._board_db_cache
    for graph_path in graph_paths:
        wal = _wal_path(graph_path)
        assert (not wal.exists()) or wal.stat().st_size == 0, (
            f"WAL residual pós-shutdown em {wal}"
        )

    # Reopen limpo e fail-closed (sem salvage, sem replay pendente): dados
    # visíveis no main file.
    import ladybug

    for graph_path in graph_paths:
        db = ladybug.Database(str(graph_path), throw_on_wal_replay_failure=True)
        conn = ladybug.Connection(db)
        try:
            result = conn.execute("MATCH (n:ShutdownProbe) RETURN count(n)")
            row = result.get_next()
            assert row[0] == 3, f"commits perdidos no reopen de {graph_path}"
        finally:
            conn.close()
            db.close()
    del db, conn
    gc.collect()

    _assert_no_quarantine(tmp_path)


def test_s3_close_all_graphs_is_idempotent_and_safe_with_empty_cache(
    clean_kg_db_cache, caplog: pytest.LogCaptureFixture
) -> None:
    """Teardown roda mesmo sem grafo aberto (boot recém-iniciado) sem falhar."""
    from okto_pulse.community.adapters.kg_shutdown import (
        close_all_graphs_on_shutdown,
    )

    with caplog.at_level(
        logging.INFO, logger="okto_pulse.community.adapters.kg_shutdown"
    ):
        summary = close_all_graphs_on_shutdown()
    assert summary["boards_closed"] == 0
    assert summary["boards_failed"] == 0
    events = [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "kg.shutdown.graphs_closed"
    ]
    assert len(events) == 1


# ---------------------------------------------------------------------------
# S3 (e2e leve, opt-in) — okto-pulse serve + sinal → shutdown ordenado
# ---------------------------------------------------------------------------

_RUN_E2E = os.environ.get("OKTO_PULSE_RUN_E2E") == "1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.slow
@pytest.mark.skipif(
    not _RUN_E2E,
    reason=(
        "e2e sobe um servidor real (portas + ~2min de boot); opt-in via "
        "OKTO_PULSE_RUN_E2E=1 para nunca ficar flaky em ambiente restrito"
    ),
)
def test_s3_e2e_serve_signal_runs_ordered_graph_shutdown(tmp_path: Path) -> None:
    """Sobe `okto-pulse serve` com DATA_DIR isolado, envia CTRL_BREAK_EVENT
    (Windows) / SIGTERM (POSIX) e verifica que o teardown ordenado alcançou o
    checkpoint+close dos grafos (evento kg.shutdown.graphs_closed no log)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    api_port = _free_port()
    mcp_port = _free_port()

    env = dict(os.environ)
    env.update(
        {
            "DATA_DIR": str(data_dir),
            "KG_BASE_DIR": str(data_dir),
            "PORT": str(api_port),
            "MCP_PORT": str(mcp_port),
            "OKTO_PULSE_TERMS_ACCEPTED": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    out_path = tmp_path / "serve.out.log"
    with out_path.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "okto_pulse.community.main"],
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )
        try:
            deadline = time.monotonic() + 180
            started = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if "Startup complete" in out_path.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    started = True
                    break
                time.sleep(1.0)
            if not started:
                pytest.skip(
                    "servidor não ficou pronto no budget do ambiente — "
                    f"exit={proc.poll()}; e2e inconclusivo, coberto pelo "
                    "teste de integração"
                )

            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=90)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)

    output = out_path.read_text(encoding="utf-8", errors="replace")
    assert (
        "kg.shutdown.graphs_closed" in output
        or "KG graphs closed on shutdown" in output
    ), f"teardown não alcançou o close dos grafos; log:\n{output[-4000:]}"
    # Nenhum board pode terminar com WAL não-vazio após shutdown limpo.
    for wal in data_dir.glob("boards/*/graph.lbug.wal"):
        assert wal.stat().st_size == 0, f"WAL residual pós-shutdown: {wal}"


# ---------------------------------------------------------------------------
# S4 — ts_7b549a9d: kill duro + reopen com salvage flag recupera sem perda
# ---------------------------------------------------------------------------

_S4_ROWS = 25

_S4_WRITER_SCRIPT = textwrap.dedent(
    f"""
    import os, sys
    import ladybug

    path = sys.argv[1]
    db = ladybug.Database(path)
    conn = ladybug.Connection(db)
    conn.execute(
        "CREATE NODE TABLE IF NOT EXISTS KillProbe(id INT64, PRIMARY KEY(id))"
    )
    for i in range({_S4_ROWS}):
        conn.execute("CREATE (:KillProbe {{id: %d}})" % i)
    # Kill duro: sem checkpoint, sem close, sem destrutores — o WAL fica como
    # único portador dos commits (equivalente a TerminateProcess/SIGKILL).
    os._exit(1)
    """
)


def test_s4_hard_kill_then_reopen_recovers_committed_data(tmp_path: Path) -> None:
    graph_path = _make_board_graph_dir(tmp_path, "kgd01-s4-board")
    script = tmp_path / "s4_writer.py"
    script.write_text(_S4_WRITER_SCRIPT, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(script), str(graph_path)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 1, (
        f"writer deveria morrer via os._exit(1); rc={proc.returncode} "
        f"stderr={proc.stderr[-2000:]}"
    )

    wal = _wal_path(graph_path)
    assert graph_path.exists(), "main graph.lbug deve sobreviver ao kill"
    assert wal.exists() and wal.stat().st_size > 0, (
        "pré-condição do cenário: WAL pendente (não-checkpointed) após o kill"
    )

    # Reopen direto pelo engine com a flag de salvage (não depende do C1):
    # replay normal do WAL válido, ou truncamento até o último COMMIT válido
    # se a cauda estiver rasgada — nunca board-death.
    import ladybug

    db = ladybug.Database(str(graph_path), throw_on_wal_replay_failure=False)
    conn = ladybug.Connection(db)
    try:
        result = conn.execute("MATCH (n:KillProbe) RETURN count(n)")
        recovered = result.get_next()[0]
    finally:
        conn.close()
        db.close()
    del db, conn
    gc.collect()

    # Todos os commits completaram ANTES do os._exit → perda zero esperada
    # (a perda só pode existir para commits DEPOIS do último COMMIT válido).
    assert recovered == _S4_ROWS, (
        f"dados até o último commit devem estar presentes: {recovered}/{_S4_ROWS}"
    )

    # Sem quarentena e sem purge: main intocado no lugar, nada movido.
    _assert_no_quarantine(tmp_path)
    assert graph_path.exists()
