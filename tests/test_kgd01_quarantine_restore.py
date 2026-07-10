"""KGD-01 C4/TC4 — Restore de quarentena: dry-run/apply com backup-swap (S7).

Cobre o cenário ts_32b1029f do spec 26b46ef3 (test card c3c0d8dc):

* Dry-run: ``plan`` retorna o plano completo (arquivos, destinos, conflitos,
  tamanhos, board_id) e NENHUM byte muda em disco (snapshot de hashes
  idêntico antes/depois); evento ``kg.quarantine.restore_dry_run`` emitido.
* Apply: os arquivos vivos do board vão para uma quarentena NOVA com
  manifest (``backup_quarantine_id``), o snapshot é restaurado, o open do
  board é validado pela fábrica ``_open_kuzu_db`` e o evento
  ``kg.quarantine.restored`` é emitido.
* Falha parcial (PermissionError NTFS simulada no meio do backup-swap): o
  manifest da operação registra o estado exato (movidos/pendentes) e a
  instrução de rollback; erro estruturado ``partial_restore`` — nunca um
  board meio-restaurado silencioso (BR4/TR6).
* CLI: ``okto-pulse kg restore <id> --json`` (argparse) em dry-run.

Fixture sintética: uma quarentena em ``tmp_path`` com ``manifest.json`` no
MESMO shape do manifest real (schema replicado por LEITURA de
``~/.okto-pulse/quarantine/q_*/manifest.json``; nada em ``~/.okto-pulse`` é
modificado) contendo um par ``graph.lbug``+``graph.lbug.wal`` pequeno gerado
via ladybug em subprocesso (WAL não-vazio, morte dura sem teardown), e um
board de destino com arquivos vivos DIFERENTES dos quarentenados.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from okto_pulse.community.adapters.quarantine_restore import (
    CommunityQuarantineRestore,
)
from okto_pulse.core.infra.config import (
    CoreSettings,
    configure_settings,
    get_settings,
)
from okto_pulse.core.kg.interfaces.quarantine_restore import (
    QuarantineRestoreError,
    QuarantineRestoreErrorCode,
)

RESTORE_LOGGER = "okto_pulse.kg.quarantine.restore"

BOARD_ID = "aaaa1111-2222-3333-4444-555566667777"
QUARANTINE_ID = "q_kgd01RestoreFixture01"

LIVE_MAIN_BYTES = b"live-main-DIFFERENT-from-snapshot"
LIVE_WAL_BYTES = b"live-wal-DIFFERENT-from-snapshot"


@pytest.fixture(autouse=True)
def _restore_core_settings():
    """Snapshot + restore do singleton CoreSettings em volta de cada teste."""
    original = get_settings()
    yield
    configure_settings(original)


# Subprocesso: cria um grafo pequeno com WAL não-vazio (commits pós-checkpoint)
# e morre com os._exit — o WAL fica como portador dos commits 2..21 e o par
# graph.lbug + graph.lbug.wal fica íntegro em disco (fixture TR5, mesmo padrão
# do test_kgd01_wal_salvage.py; nenhum grafo real de ~/.okto-pulse é tocado).
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
    for i in range(2, 22):
        conn.execute("CREATE (:t {id: %d})" % i)
    wal = db_path + ".wal"
    assert os.path.exists(wal), "WAL sidecar ausente apos commits"
    os._exit(7)
    """
)


def _make_snapshot_pair(scratch: Path) -> tuple[Path, Path]:
    scratch.mkdir(parents=True, exist_ok=True)
    db_path = scratch / "graph.lbug"
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
    assert db_path.exists() and wal_path.exists()
    return db_path, wal_path


def _manifest_payload(files: list[str]) -> dict:
    """Shape replicado do manifest.json real dos q_* (KGQuarantineService)."""
    now = datetime.now(timezone.utc)
    return {
        "quarantine_id": QUARANTINE_ID,
        "board_id": BOARD_ID,
        "graph_type": "board_graph",
        "reason": "synthetic_fixture:kgd01_s7",
        "reason_bucket": "corruption_detected",
        "correlation_ids": [],
        "affected_paths_relative": files,
        "kg_generation_id": None,
        "software_version": "0.3.0",
        "quarantined_at": now.isoformat(),
        "retention_until": (now + timedelta(days=30)).isoformat(),
        "files_moved": len(files),
    }


@pytest.fixture
def restore_env(tmp_path):
    """Base KG sintética: quarentena com snapshot ladybug + board com vivos."""
    base = tmp_path / "kgbase"
    quarantine_dir = base / "quarantine" / QUARANTINE_ID
    quarantine_dir.mkdir(parents=True)
    board_dir = base / "boards" / BOARD_ID
    board_dir.mkdir(parents=True)

    db_path, wal_path = _make_snapshot_pair(tmp_path / "scratch")
    shutil.move(str(db_path), str(quarantine_dir / "graph.lbug"))
    shutil.move(str(wal_path), str(quarantine_dir / "graph.lbug.wal"))
    (quarantine_dir / "manifest.json").write_text(
        json.dumps(_manifest_payload(["graph.lbug", "graph.lbug.wal"]), indent=2),
        encoding="utf-8",
    )

    # Board de destino com arquivos vivos DIFERENTES dos quarentenados.
    (board_dir / "graph.lbug").write_bytes(LIVE_MAIN_BYTES)
    (board_dir / "graph.lbug.wal").write_bytes(LIVE_WAL_BYTES)

    configure_settings(
        CoreSettings(
            kg_kuzu_buffer_pool_mb=128,
            kg_kuzu_max_db_size_gb=2,
            kg_base_dir=str(base),
        )
    )
    return {
        "base": base,
        "quarantine_dir": quarantine_dir,
        "board_dir": board_dir,
        "adapter": CommunityQuarantineRestore(base_dir=base),
    }


def _disk_state(base: Path) -> dict[str, str]:
    """sha256 de todos os arquivos sob a base (snapshot de zero-mutação)."""
    state: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        if path.is_file():
            state[str(path.relative_to(base))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return state


def _events(caplog) -> list[str]:
    return [getattr(rec, "event", None) for rec in caplog.records]


# ---------------------------------------------------------------------------
# S7 — dry-run: plano completo, zero mutação
# ---------------------------------------------------------------------------


def test_plan_dry_run_full_plan_and_zero_mutation(restore_env, caplog):
    base: Path = restore_env["base"]
    adapter: CommunityQuarantineRestore = restore_env["adapter"]
    board_dir: Path = restore_env["board_dir"]
    quarantine_dir: Path = restore_env["quarantine_dir"]

    before = _disk_state(base)

    with caplog.at_level(logging.INFO, logger=RESTORE_LOGGER):
        plan = adapter.plan(QUARANTINE_ID)

    # Plano completo: board, destinos, arquivos, tamanhos, conflitos.
    assert plan.quarantine_id == QUARANTINE_ID
    assert plan.board_id == BOARD_ID
    assert Path(plan.board_dir) == board_dir
    assert plan.manifest_format == "manifest.json"
    names = [entry.name for entry in plan.files]
    assert names == ["graph.lbug", "graph.lbug.wal"]
    for entry in plan.files:
        assert Path(entry.source_path) == quarantine_dir / entry.name
        assert Path(entry.destination_path) == board_dir / entry.name
        assert entry.size_bytes == (quarantine_dir / entry.name).stat().st_size
        assert entry.size_bytes > 0
        # Os vivos existem e diferem — todo arquivo é conflito declarado.
        assert entry.conflict is True
        assert entry.live_size_bytes == (board_dir / entry.name).stat().st_size
    assert set(plan.conflicts) == {"graph.lbug", "graph.lbug.wal"}
    assert plan.total_bytes == sum(e.size_bytes for e in plan.files)

    # Auditoria TR4.
    assert "kg.quarantine.restore_dry_run" in _events(caplog)
    rec = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "kg.quarantine.restore_dry_run"
    )
    assert rec.quarantine_id == QUARANTINE_ID
    assert rec.board_id == BOARD_ID

    # ZERO mutação: nenhum byte mudou em disco.
    assert _disk_state(base) == before, "dry-run mutou o disco"


# ---------------------------------------------------------------------------
# S7 — apply: backup-swap + restauração + open validado + auditoria
# ---------------------------------------------------------------------------


def test_apply_backup_swap_restores_and_validates_open(restore_env, caplog):
    base: Path = restore_env["base"]
    adapter: CommunityQuarantineRestore = restore_env["adapter"]
    board_dir: Path = restore_env["board_dir"]
    quarantine_dir: Path = restore_env["quarantine_dir"]

    snapshot_hashes_before = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in quarantine_dir.iterdir()
        if p.is_file()
    }

    with caplog.at_level(logging.INFO, logger=RESTORE_LOGGER):
        report = adapter.apply(QUARANTINE_ID)

    assert report.applied is True
    assert report.quarantine_id == QUARANTINE_ID
    assert report.board_id == BOARD_ID
    assert report.errors == ()

    # backup_quarantine_id criado, com manifest e os vivos preservados.
    assert report.backup_quarantine_id and report.backup_quarantine_id.startswith("q_")
    backup_dir = base / "quarantine" / report.backup_quarantine_id
    assert backup_dir.is_dir()
    backup_manifest = json.loads(
        (backup_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert backup_manifest["quarantine_id"] == report.backup_quarantine_id
    assert backup_manifest["board_id"] == BOARD_ID
    assert backup_manifest["reason"] == f"restore_backup_swap:{QUARANTINE_ID}"
    assert sorted(backup_manifest["affected_paths_relative"]) == [
        "graph.lbug", "graph.lbug.wal",
    ]
    assert backup_manifest["files_moved"] == 2
    assert (backup_dir / "graph.lbug").read_bytes() == LIVE_MAIN_BYTES
    assert (backup_dir / "graph.lbug.wal").read_bytes() == LIVE_WAL_BYTES

    # Arquivos restaurados no board (vivos antigos substituídos).
    assert sorted(report.restored_files) == ["graph.lbug", "graph.lbug.wal"]
    assert (board_dir / "graph.lbug").is_file()
    assert (board_dir / "graph.lbug").read_bytes() != LIVE_MAIN_BYTES

    # Open validado pela fábrica única (_open_kuzu_db) — replay do WAL ok.
    assert report.open_validated is True

    # A quarentena de origem permanece INTACTA (restore copia, não move).
    snapshot_hashes_after = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in quarantine_dir.iterdir()
        if p.is_file()
    }
    assert snapshot_hashes_after == snapshot_hashes_before

    # Auditoria TR4 no caplog.
    assert "kg.quarantine.restored" in _events(caplog)
    rec = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "kg.quarantine.restored"
    )
    assert rec.quarantine_id == QUARANTINE_ID
    assert rec.board_id == BOARD_ID
    assert rec.backup_quarantine_id == report.backup_quarantine_id
    assert rec.open_validated is True

    # Manifest da operação registra o fluxo completo até phase=done.
    op = json.loads(
        (backup_dir / "restore_operation.json").read_text(encoding="utf-8")
    )
    assert op["phase"] == "done"
    assert op["open_validated"] is True
    assert sorted(op["moved_to_backup"]) == ["graph.lbug", "graph.lbug.wal"]
    assert sorted(op["copied_from_snapshot"]) == ["graph.lbug", "graph.lbug.wal"]
    assert op["pending_backup"] == [] and op["pending_copy"] == []


# ---------------------------------------------------------------------------
# S7 — falha parcial (PermissionError NTFS): estado exato + partial_restore
# ---------------------------------------------------------------------------


def test_apply_partial_failure_records_exact_state(restore_env, monkeypatch):
    base: Path = restore_env["base"]
    adapter: CommunityQuarantineRestore = restore_env["adapter"]
    board_dir: Path = restore_env["board_dir"]

    real_move = shutil.move

    def _failing_move(src, dst, *args, **kwargs):
        # PermissionError no SEGUNDO arquivo do backup-swap (graph.lbug.wal),
        # simulando o handle NTFS preso do modo de falha real do Windows.
        if str(src).endswith("graph.lbug.wal"):
            raise PermissionError(13, "held by another process", str(src))
        return real_move(src, dst, *args, **kwargs)

    monkeypatch.setattr(
        "okto_pulse.community.adapters.quarantine_restore.shutil.move",
        _failing_move,
    )

    with pytest.raises(QuarantineRestoreError) as exc_info:
        adapter.apply(QUARANTINE_ID)

    err = exc_info.value
    assert err.code is QuarantineRestoreErrorCode.PARTIAL_RESTORE
    payload = err.to_payload()
    assert payload["error"] == "partial_restore"

    details = payload["details"]
    # Estado exato: graph.lbug movido, graph.lbug.wal pendente, nada copiado.
    assert details["moved_to_backup"] == ["graph.lbug"]
    assert details["pending_backup"] == ["graph.lbug.wal"]
    assert details["copied_from_snapshot"] == []
    assert sorted(details["pending_copy"]) == ["graph.lbug", "graph.lbug.wal"]
    assert details["rollback_instruction"]

    # Manifest da operação em disco espelha o estado exato + rollback.
    op_path = Path(details["operation_manifest"])
    assert op_path.is_file()
    op = json.loads(op_path.read_text(encoding="utf-8"))
    assert op["moved_to_backup"] == ["graph.lbug"]
    assert op["pending_backup"] == ["graph.lbug.wal"]
    assert op["copied_from_snapshot"] == []
    assert op["error"]["type"] == "PermissionError"
    assert op["error"]["step"] == "backup_swap:graph.lbug.wal"
    assert op["rollback_instruction"]

    # Nada silencioso: o disco bate com o manifest — o vivo graph.lbug foi
    # movido para a quarentena de backup; graph.lbug.wal continua no board.
    backup_dir = base / "quarantine" / details["backup_quarantine_id"]
    assert (backup_dir / "graph.lbug").read_bytes() == LIVE_MAIN_BYTES
    assert not (board_dir / "graph.lbug").exists()
    assert (board_dir / "graph.lbug.wal").read_bytes() == LIVE_WAL_BYTES


# ---------------------------------------------------------------------------
# S7 — erro estruturado quarantine_not_found
# ---------------------------------------------------------------------------


def test_plan_unknown_quarantine_raises_not_found(restore_env):
    adapter: CommunityQuarantineRestore = restore_env["adapter"]
    with pytest.raises(QuarantineRestoreError) as exc_info:
        adapter.plan("q_doesNotExist123")
    assert exc_info.value.code is QuarantineRestoreErrorCode.QUARANTINE_NOT_FOUND
    assert exc_info.value.to_payload()["error"] == "quarantine_not_found"


# ---------------------------------------------------------------------------
# S7 — CLI (argparse): okto-pulse kg restore <id> --json em dry-run
# ---------------------------------------------------------------------------


def test_cli_restore_dry_run_json(restore_env, monkeypatch, capsys):
    base: Path = restore_env["base"]
    before = _disk_state(base)

    from okto_pulse.community import cli as community_cli

    monkeypatch.setattr(
        sys, "argv", ["okto-pulse", "kg", "restore", QUARANTINE_ID, "--json"]
    )
    with pytest.raises(SystemExit) as exc_info:
        community_cli.main()
    assert exc_info.value.code == 0

    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload["applied"] is False
    assert payload["quarantine_id"] == QUARANTINE_ID
    assert payload["board_id"] == BOARD_ID
    plan_names = sorted(entry["name"] for entry in payload["plan"])
    assert plan_names == ["graph.lbug", "graph.lbug.wal"]
    assert sorted(payload["conflicts"]) == ["graph.lbug", "graph.lbug.wal"]
    assert all(entry["size_bytes"] > 0 for entry in payload["plan"])

    # CLI dry-run também é zero-mutação.
    assert _disk_state(base) == before, "CLI dry-run mutou o disco"
