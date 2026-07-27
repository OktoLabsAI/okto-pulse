"""KGD-01 C1/TC1 — WAL salvage na fábrica única de opens (spec 26b46ef3).

Cobre os cenários do test card 9f6338ff-bdb4-4996-a3cc-3b4aa6de0a13:

* S1 (ts_9bd8ba6c) — com ``kg_wal_salvage_enabled=true`` (default), um board
  com ``graph.lbug`` íntegro e ``graph.lbug.wal`` com rabo corrompido abre com
  sucesso pela fábrica ``_open_kuzu_db``; os dados até o último COMMIT válido
  estão presentes; o log estruturado contém ``kg.wal.salvage_applied``; após
  CHECKPOINT+close limpo o reopen NÃO emite o evento de novo.
* S2 (ts_41c055c8) — com ``kg_wal_salvage_enabled=false``, o open falha
  fechado com RuntimeError contendo marcador de corrupção e NENHUM arquivo do
  board é movido/alterado (hashes sha256 idênticos antes/depois).

Fixture sintética (TR5): um SUBPROCESS cria um grafo pequeno, checkpointa,
insere commits adicionais SEM checkpoint e morre com ``os._exit`` — deixando
um ``graph.lbug.wal`` não-vazio. O processo de teste então zera os últimos
512 bytes do WAL (rabo corrompido — a assinatura INVALID_RECORD/checksum do
incidente). Nenhum grafo real de ``~/.okto-pulse`` é tocado.

Backends: parametrizado em ``default`` (resolução ``auto`` do wrapper — neste
ambiente resolve para pybind) e ``pybind`` forçado via ``LBUG_PYTHON_BACKEND``.
O backend C-API puro não é viável neste ambiente (o wheel win_amd64 não embarca
a shared lib do shim C-API — "Could not find lbug C API shared library"); a
fábrica cobre esse caso com o fallback pybind já testado em
``test_kuzu_memory_config.py``.
"""

from __future__ import annotations

import gc
import hashlib
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import ladybug
from okto_pulse.community.adapters import kg_runtime
from okto_pulse.core.infra.config import (
    CoreSettings,
    configure_settings,
    get_settings,
)

KG_LOGGER = "okto_pulse.kg.schema"

# Bytes zerados no rabo do WAL (assinatura de rasgo de cauda).
_TAIL_CORRUPTION_BYTES = 512


@pytest.fixture(autouse=True)
def _restore_core_settings():
    """Snapshot + restore do singleton CoreSettings em volta de cada teste."""
    original = get_settings()
    yield
    configure_settings(original)


def _pybind_available() -> bool:
    return kg_runtime._ladybug_pybind_available()


@pytest.fixture(params=["default", "pybind"])
def lbug_backend(request, monkeypatch):
    """Parametriza o backend do wrapper ladybug para o processo de teste."""
    backend = request.param
    if backend == "pybind":
        if not _pybind_available():
            pytest.skip(
                "backend pybind (_lbug) indisponível neste ambiente — "
                "cenário coberto apenas no backend default"
            )
        monkeypatch.setenv("LBUG_PYTHON_BACKEND", "pybind")
    else:
        monkeypatch.delenv("LBUG_PYTHON_BACKEND", raising=False)
    return backend


_FIXTURE_SCRIPT = textwrap.dedent(
    """
    import os, sys
    import ladybug

    db_path = sys.argv[1]
    db = ladybug.Database(
        db_path,
        buffer_pool_size=128 * 1024 * 1024,
        max_db_size=2 * 1024 * 1024 * 1024,
    )
    conn = ladybug.Connection(db)
    conn.execute("CREATE NODE TABLE t(id INT64, PRIMARY KEY(id))")
    conn.execute("CREATE (:t {id: 1})")
    conn.execute("CHECKPOINT")
    # Commits pos-checkpoint: ficam SOMENTE no WAL (nenhum checkpoint depois).
    for i in range(2, 203):
        conn.execute("CREATE (:t {id: %d})" % i)
    wal = db_path + ".wal"
    assert os.path.exists(wal), "WAL sidecar ausente apos commits"
    assert os.path.getsize(wal) > 4096, os.path.getsize(wal)
    # Morte dura sem teardown: WAL fica como unico portador dos commits 2..202.
    os._exit(7)
    """
)


def _make_torn_wal_board(tmp_path: Path) -> Path:
    """Cria um board sintético com WAL não-vazio e corrompe o rabo do WAL."""
    board_dir = tmp_path / "board-kgd01"
    board_dir.mkdir()
    db_path = board_dir / "graph.lbug"

    proc = subprocess.run(
        [sys.executable, "-c", _FIXTURE_SCRIPT, str(db_path)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 7, (
        f"fixture subprocess falhou (rc={proc.returncode}):\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )

    wal_path = Path(str(db_path) + ".wal")
    assert db_path.exists(), "main graph.lbug ausente"
    assert wal_path.exists(), "graph.lbug.wal ausente"
    wal_size = wal_path.stat().st_size
    assert wal_size > 4096, f"WAL pequeno demais para a fixture: {wal_size}B"

    # Corrompe o rabo: run zerado nos últimos 512 bytes (record type 0x00 /
    # checksum inválido no replay — mesma classe do incidente 2cd4d5ac).
    with open(wal_path, "r+b") as f:
        f.seek(wal_size - _TAIL_CORRUPTION_BYTES)
        f.write(b"\x00" * _TAIL_CORRUPTION_BYTES)
    assert wal_path.stat().st_size == wal_size, "corrupção não pode mudar o tamanho"
    return db_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count_rows(conn, cypher: str) -> int:
    res = conn.execute(cypher)
    row = res.get_next()
    return int(row[0])


def _events(caplog) -> list[str]:
    return [getattr(rec, "event", None) for rec in caplog.records]


# ---------------------------------------------------------------------------
# S1 (ts_9bd8ba6c) — salvage abre WAL rasgado e emite kg.wal.salvage_applied
# ---------------------------------------------------------------------------


def test_salvage_opens_torn_wal_and_emits_event(tmp_path, caplog, lbug_backend):
    configure_settings(
        CoreSettings(
            kg_kuzu_buffer_pool_mb=128,
            kg_kuzu_max_db_size_gb=2,
            kg_wal_salvage_enabled=True,  # default explícito para determinismo
        )
    )
    db_path = _make_torn_wal_board(tmp_path)
    wal_path = Path(str(db_path) + ".wal")
    wal_bytes_corrupted = wal_path.stat().st_size

    with caplog.at_level(logging.INFO, logger=KG_LOGGER):
        db = kg_runtime._open_kuzu_db(db_path)
    assert db is not None

    try:
        conn = ladybug.Connection(db)
        # Linha pré-checkpoint (persistida no main) SEMPRE presente.
        assert _count_rows(conn, "MATCH (n:t) WHERE n.id = 1 RETURN count(n)") == 1
        # Primeiro commit pós-checkpoint: gravado no início do WAL, muito antes
        # do rabo zerado — sobrevive ao salvage (replay até o último COMMIT
        # válido antes da região corrompida).
        assert _count_rows(conn, "MATCH (n:t) WHERE n.id = 2 RETURN count(n)") == 1
        total = _count_rows(conn, "MATCH (n:t) RETURN count(n)")
        assert total >= 2, f"salvage deveria preservar >=2 linhas, veio {total}"

        events = _events(caplog)
        assert "kg.wal.salvage_applied" in events, (
            f"evento kg.wal.salvage_applied ausente; eventos={events}"
        )
        rec = next(
            r for r in caplog.records
            if getattr(r, "event", None) == "kg.wal.salvage_applied"
        )
        assert rec.levelno == logging.WARNING
        assert rec.board_id == db_path.parent.name
        assert rec.path == str(db_path)
        assert rec.wal_bytes_before == wal_bytes_corrupted
        assert isinstance(rec.wal_bytes_after, int)

        # CHECKPOINT + close limpo consomem o WAL.
        conn.execute("CHECKPOINT")
        conn.close()
    finally:
        db.close()
    gc.collect()  # Windows: libera handles C++ antes do reopen

    assert (not wal_path.exists()) or wal_path.stat().st_size < wal_bytes_corrupted, (
        "após CHECKPOINT+close o WAL deveria estar truncado/consumido"
    )

    # Reopen limpo: nenhum novo evento de salvage.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=KG_LOGGER):
        db2 = kg_runtime._open_kuzu_db(db_path)
    try:
        assert "kg.wal.salvage_applied" not in _events(caplog), (
            "reopen após shutdown limpo não pode precisar de salvage"
        )
        conn2 = ladybug.Connection(db2)
        assert _count_rows(conn2, "MATCH (n:t) WHERE n.id = 1 RETURN count(n)") == 1
        assert _count_rows(conn2, "MATCH (n:t) WHERE n.id = 2 RETURN count(n)") == 1
        conn2.close()
    finally:
        db2.close()
    gc.collect()


# ---------------------------------------------------------------------------
# S2 (ts_41c055c8) — salvage desligado: open falha fechado, zero mutação
# ---------------------------------------------------------------------------


def test_salvage_disabled_fails_closed_no_mutation(tmp_path, caplog, lbug_backend):
    configure_settings(
        CoreSettings(
            kg_kuzu_buffer_pool_mb=128,
            kg_kuzu_max_db_size_gb=2,
            kg_wal_salvage_enabled=False,
            # KGD-01 C3: o degrau 2 (wal-only recovery, default true) também
            # precisa estar desligado — S2 valida o caminho 100% fail-closed
            # com TODA a escada de recovery automática desativada (zero
            # mutação de arquivos). O degrau 2 com a flag ligada é coberto
            # por tests/test_kgd01_wal_only_recovery.py (S5/S6).
            kg_wal_only_recovery_enabled=False,
        )
    )
    db_path = _make_torn_wal_board(tmp_path)
    wal_path = Path(str(db_path) + ".wal")
    board_dir = db_path.parent

    main_hash_before = _sha256(db_path)
    wal_hash_before = _sha256(wal_path)
    listing_before = sorted(p.name for p in board_dir.iterdir())

    with caplog.at_level(logging.INFO, logger=KG_LOGGER):
        with pytest.raises(RuntimeError) as exc_info:
            kg_runtime._open_kuzu_db(db_path)
    gc.collect()

    msg = str(exc_info.value).lower()
    assert any(marker in msg for marker in kg_runtime.CORRUPT_DB_ERROR_MARKERS), (
        f"RuntimeError sem marcador de corrupção: {msg[:400]}"
    )

    # Fail-closed: nada movido, nada mutado.
    assert db_path.exists() and wal_path.exists()
    assert _sha256(db_path) == main_hash_before, "graph.lbug foi mutado"
    assert _sha256(wal_path) == wal_hash_before, "graph.lbug.wal foi mutado"
    listing_after = sorted(p.name for p in board_dir.iterdir())
    assert listing_after == listing_before, (
        f"arquivos do board mudaram: {listing_before} -> {listing_after}"
    )

    # Nenhum evento de salvage com a flag desligada.
    events = _events(caplog)
    assert "kg.wal.salvage_applied" not in events
    assert "kg.wal.salvage_failed" not in events
