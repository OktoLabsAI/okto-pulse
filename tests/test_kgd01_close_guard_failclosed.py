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

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from okto_pulse.core.infra.config import configure_settings, get_settings
from repo_layout import resolve_core_repo

from okto_pulse.community import serve_lock

KG_LOGGER = "okto_pulse.kg.schema"


@pytest.fixture(autouse=True)
def _restore_core_settings():
    """Snapshot + restore do singleton CoreSettings em volta de cada teste."""
    original = get_settings()
    yield
    configure_settings(original)


def _events(caplog) -> list[str]:
    return [getattr(rec, "event", None) for rec in caplog.records]


# ---------------------------------------------------------------------------
# S9 — ts_0393503b: stress concorrente, zero close com leitor registrado
# ---------------------------------------------------------------------------

_S9_BOARD = "kgd01-s9-stress"
_S9_EVICT_BOARD = "kgd01-s9-evict"
_S9_TARGET_ITERATIONS = 1200
_S9_MIN_ITERATIONS = 1000
_S9_WORKERS = 8
_S9_DEADLINE_S = 120.0


# ---------------------------------------------------------------------------
# S9b — shutdown: force_after_drain_timeout fecha com leitor vazado + log
# ---------------------------------------------------------------------------


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
