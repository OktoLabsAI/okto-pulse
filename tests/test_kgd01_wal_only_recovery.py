"""KGD-01 C3/TC3 — recovery escalonado wal-only (spec 26b46ef3, FR3/BR2/D3).

Cobre os cenários do test card 1b403fc2-23a4-41ad-ade3-47787615035e:

* S5 (ts_f7c83a18) — main íntegro + WAL com CORPO zerado (classe 05-26: bloco
  de 4096B zerado logo após o header, ~offset 100). Com o salvage (degrau 1)
  desligado para forçar deterministicamente o degrau 2: o open primário falha
  com marcador de corrupção → a quarentena wal-only executa (move SOMENTE o
  ``graph.lbug.wal``; o main NUNCA é tocado — sha256 idêntico na fronteira da
  operação de recovery) → o reopen automático abre com os dados até o último
  checkpoint → evento estruturado ``kg.recovery.wal_quarantined`` com
  ``main_untouched=True`` → manifest completo no diretório de quarentena
  (redirecionado para tmp via layout ``<kg_base>/boards/<board>/graph.lbug`` —
  a quarentena deriva de ``path.parents[2]``, nunca de ``~/.okto-pulse``).
* S6 (ts_af032a22) — nenhum caminho automático toca o main:
  (a) dinâmico — injeta corrupção e roda o fluxo de open/recovery completo em
      todas as combinações de flags + no cenário all-rungs-fail (main também
      corrompido: TODOS os degraus automáticos executam e falham); sha256 do
      ``graph.lbug`` inalterado em todos os desfechos SEM handle vivo do
      engine, e inalterado na fronteira exata da operação de recovery nos
      desfechos recuperados;
  (b) estático — varredura AST dos módulos adapters do community por call
      sites destrutivos (os.remove/os.replace/os.rename/shutil.move/
      shutil.rmtree/.unlink/.rename/.replace) cujo texto referencie o main
      ``graph.lbug`` (literal, ``GRAPH_DB_FILENAME`` ou ``board_kuzu_path(``)
      sem sufixo de sidecar, fora dos módulos sancionados (allowlist com
      justificativa). Um call site destrutivo novo sobre o main falha o teste.

NOTA DE MEDIÇÃO (empírico, ladybug 0.16.x win_amd64): o close de um Database
aberto com SUCESSO checkpointa e REESCREVE o main file — inclusive em boards
100% saudáveis sem nenhum recovery (verificado em experimento: open+close de
um board íntegro muda o sha256 do graph.lbug, de forma não-determinística).
Logo, "sha256 idêntico" só é bem-definido (i) na fronteira da operação de
recovery (antes/depois da quarentena wal-only, medido via wrapper) e (ii) em
desfechos fail-closed onde nenhum handle vivo foi devolvido. O checkpoint-on-
close do engine através de um handle legítimo é durabilidade normal, não uma
violação de BR2 — BR2 proíbe os CAMINHOS DE RECOVERY de mover/alterar o main,
e é exatamente isso que estes testes provam byte-a-byte.

Fixture sintética (mesmo padrão TR5 de tests/test_kgd01_wal_salvage.py): um
SUBPROCESS cria um grafo pequeno, checkpointa (id=1 persiste no main), insere
commits adicionais SEM checkpoint (ids 2..202 só no WAL) e morre com
``os._exit``. O corpo do WAL é então zerado logo após o header — a classe de
corrupção que o salvage NÃO recupera integralmente. Nenhum grafo real de
``~/.okto-pulse`` é tocado.
"""

from __future__ import annotations

import ast
import gc
import hashlib
import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import ladybug
from okto_pulse.community.adapters import kg_runtime
from okto_pulse.community.adapters import kg_wal_recovery
from okto_pulse.core.infra.config import (
    CoreSettings,
    configure_settings,
    get_settings,
)

# Corrupção de CORPO (classe 05-26): bloco zerado logo APÓS o header do WAL.
_BODY_CORRUPTION_OFFSET = 100
_BODY_CORRUPTION_BYTES = 4096

_ADAPTERS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "okto_pulse"
    / "community"
    / "adapters"
)


@pytest.fixture(autouse=True)
def _restore_core_settings():
    """Snapshot + restore do singleton CoreSettings em volta de cada teste."""
    original = get_settings()
    yield
    configure_settings(original)


# Mesmo script do fixture TR5 de test_kgd01_wal_salvage.py: id=1 checkpointado
# (persiste no main); ids 2..202 só no WAL (nenhum checkpoint depois).
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


def _make_board_with_zeroed_wal_body(tmp_path: Path) -> Path:
    """Board sintético em layout <kg_base>/boards/<board_id>/graph.lbug com
    WAL de corpo zerado (4096B logo após o header).

    O layout de 3 níveis importa: a quarentena wal-only deriva de
    ``path.parents[2]/quarantine`` — com o kg_base dentro de ``tmp_path`` a
    quarentena REAL (~/.okto-pulse) nunca é tocada.
    """
    board_dir = tmp_path / "kgbase" / "boards" / "board-kgd01-c3"
    board_dir.mkdir(parents=True)
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
    assert wal_size > _BODY_CORRUPTION_OFFSET + _BODY_CORRUPTION_BYTES, (
        f"WAL pequeno demais para corromper o corpo: {wal_size}B"
    )

    # Corpo zerado logo após o header (~offset 100): o primeiro record do
    # replay já é inválido — a classe 05-26 que o salvage não recupera
    # integralmente (dryReplay para antes de qualquer commit válido).
    with open(wal_path, "r+b") as f:
        f.seek(_BODY_CORRUPTION_OFFSET)
        f.write(b"\x00" * _BODY_CORRUPTION_BYTES)
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


def _configure(salvage: bool, wal_only: bool) -> None:
    configure_settings(
        CoreSettings(
            kg_kuzu_buffer_pool_mb=128,
            kg_kuzu_max_db_size_gb=2,
            kg_wal_salvage_enabled=salvage,
            kg_wal_only_recovery_enabled=wal_only,
        )
    )


@pytest.fixture
def quarantine_boundary_hashes(monkeypatch):
    """Instrumenta ``wal_only_quarantine`` para medir o sha256 do main na
    fronteira EXATA da operação de recovery (antes/depois da quarentena),
    sem o checkpoint-on-close legítimo do engine dentro da medição.

    O import em ``kg_runtime._try_open_with_wal_only_recovery`` é lazy
    (função), então o monkeypatch no módulo ``kg_wal_recovery`` é efetivo.
    """
    captured: dict[str, tuple[str, str]] = {}
    real = kg_wal_recovery.wal_only_quarantine

    def wrapper(board_id, reason, *, graph_path=None):
        assert graph_path is not None, "fluxo de open deve passar o path"
        before = _sha256(graph_path)
        result = real(board_id, reason, graph_path=graph_path)
        captured[board_id] = (before, _sha256(graph_path))
        return result

    monkeypatch.setattr(kg_wal_recovery, "wal_only_quarantine", wrapper)
    return captured


# ---------------------------------------------------------------------------
# S5 (ts_f7c83a18) — wal-only preserva o main e reabre até o último checkpoint
# ---------------------------------------------------------------------------


def test_s5_wal_only_quarantine_unit_never_touches_main(tmp_path, caplog):
    """Unidade do degrau 2: a operação de quarentena em si é byte-neutra no
    main (sha256 estritamente idêntico — nenhum engine no meio)."""
    db_path = _make_board_with_zeroed_wal_body(tmp_path)
    kg_base = db_path.parents[2]
    wal_path = Path(str(db_path) + ".wal")
    board_id = db_path.parent.name

    main_hash_before = _sha256(db_path)
    wal_hash_before = _sha256(wal_path)

    caplog.set_level(logging.INFO)
    result = kg_wal_recovery.wal_only_quarantine(
        board_id, "tc3-unit", graph_path=db_path
    )

    assert result.ok
    assert result.files_moved == ("graph.lbug.wal",)
    assert result.quarantine_id

    # BR2 estrito: main byte-idêntico e no lugar; WAL preservado byte-idêntico
    # na quarentena (evidência), main NUNCA aparece lá.
    assert db_path.exists()
    assert _sha256(db_path) == main_hash_before, "graph.lbug foi mutado (BR2)"
    assert not wal_path.exists(), "graph.lbug.wal deveria ter sido movido"
    quarantine_dir = kg_base / "quarantine" / result.quarantine_id
    assert quarantine_dir.is_dir()
    moved_wal = quarantine_dir / "graph.lbug.wal"
    assert moved_wal.exists()
    assert _sha256(moved_wal) == wal_hash_before, "evidência do WAL alterada"
    assert not (quarantine_dir / "graph.lbug").exists(), (
        "main graph.lbug apareceu na quarentena (BR2)"
    )

    # Manifest completo.
    manifest = json.loads(
        (quarantine_dir / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["kind"] == "kg_wal_only_quarantine"
    assert manifest["board_id"] == board_id
    assert manifest["quarantine_id"] == result.quarantine_id
    assert manifest["main_untouched"] is True
    assert manifest["files"] == ["graph.lbug.wal"]
    assert manifest["error"] is None
    assert manifest["reason"] == "tc3-unit"
    assert manifest["main_file"] == "graph.lbug"
    planned_names = {p["name"] for p in manifest["planned_files"]}
    assert planned_names == {"graph.lbug.wal"}

    # Evento estruturado.
    rec = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "kg.recovery.wal_quarantined"
    )
    assert rec.levelno == logging.WARNING
    assert rec.board_id == board_id
    assert rec.main_untouched is True
    assert rec.files == ["graph.lbug.wal"]


def test_s5_wal_only_recovery_preserves_main(
    tmp_path, caplog, quarantine_boundary_hashes
):
    """Integração do degrau 2 no fluxo de open: open falha → quarentena
    wal-only executa (main byte-idêntico na fronteira da operação) → reopen
    automático OK com os dados até o último checkpoint → evento + manifest."""
    # Salvage (degrau 1) desligado para forçar deterministicamente o degrau 2
    # (a rota "corrupção que até o salvage-open falha" depende do engine;
    # a flag é o gate determinístico documentado no card TC3).
    _configure(salvage=False, wal_only=True)
    db_path = _make_board_with_zeroed_wal_body(tmp_path)
    kg_base = db_path.parents[2]
    wal_path = Path(str(db_path) + ".wal")
    board_id = db_path.parent.name

    main_hash_before = _sha256(db_path)
    wal_hash_before = _sha256(wal_path)

    caplog.set_level(logging.INFO)
    db = kg_runtime._open_kuzu_db(db_path)
    assert db is not None, "degrau 2 deveria reabrir o board"

    try:
        conn = ladybug.Connection(db)
        # Dados até o último checkpoint: id=1 persiste no main.
        assert _count_rows(conn, "MATCH (n:t) WHERE n.id = 1 RETURN count(n)") == 1
        # Commits que viviam SÓ no WAL corrompido foram para a quarentena
        # (não replicados) — perda esperada e auditada do degrau 2.
        assert _count_rows(conn, "MATCH (n:t) WHERE n.id = 2 RETURN count(n)") == 0
        assert _count_rows(conn, "MATCH (n:t) RETURN count(n)") == 1
        conn.close()
    finally:
        db.close()
    gc.collect()  # Windows: libera handles C++ antes das asserções de arquivo

    # BR2 na fronteira da operação de recovery: a quarentena wal-only rodou
    # com o main byte-idêntico antes E depois (ver NOTA DE MEDIÇÃO no topo:
    # o checkpoint-on-close do engine pós-recovery é durabilidade normal e
    # fica fora da medição byte-a-byte por design).
    assert board_id in quarantine_boundary_hashes, (
        "a quarentena wal-only não executou no fluxo de open"
    )
    boundary_before, boundary_after = quarantine_boundary_hashes[board_id]
    assert boundary_before == main_hash_before
    assert boundary_after == main_hash_before, "graph.lbug mutado pelo degrau 2 (BR2)"
    assert db_path.exists(), "graph.lbug sumiu do lugar original"

    # Evento estruturado do degrau 2.
    events = _events(caplog)
    assert "kg.recovery.wal_quarantined" in events, (
        f"evento kg.recovery.wal_quarantined ausente; eventos={events}"
    )
    rec = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "kg.recovery.wal_quarantined"
    )
    assert rec.levelno == logging.WARNING
    assert rec.board_id == board_id
    assert rec.main_untouched is True
    assert "graph.lbug.wal" in rec.files
    assert "graph.lbug" not in rec.files, "main listado como movido (BR2)"
    quarantine_id = rec.quarantine_id
    assert quarantine_id

    assert "kg.recovery.wal_only_recovered" in events
    # Salvage desligado: nenhum evento do degrau 1.
    assert "kg.wal.salvage_applied" not in events
    assert "kg.wal.salvage_failed" not in events

    # Quarentena: WAL movido byte-idêntico (evidência) + manifest completo;
    # o main NÃO está lá.
    quarantine_dir = kg_base / "quarantine" / quarantine_id
    assert quarantine_dir.is_dir(), f"quarentena ausente em {quarantine_dir}"
    moved_wal = quarantine_dir / "graph.lbug.wal"
    assert moved_wal.exists(), "graph.lbug.wal não chegou à quarentena"
    assert _sha256(moved_wal) == wal_hash_before, (
        "o WAL quarentenado deveria ser byte-idêntico ao original (evidência)"
    )
    assert not (quarantine_dir / "graph.lbug").exists(), (
        "main graph.lbug apareceu na quarentena (BR2)"
    )
    manifest_path = quarantine_dir / "manifest.json"
    assert manifest_path.exists(), "manifest.json ausente na quarentena"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["board_id"] == board_id
    assert manifest["quarantine_id"] == quarantine_id
    assert manifest["main_untouched"] is True
    assert manifest["files"] == ["graph.lbug.wal"]
    assert manifest["error"] is None
    assert manifest["reason"]

    # Reopen limpo pós-recovery: dados do checkpoint presentes, sem novos
    # eventos de recovery/salvage.
    caplog.clear()
    db2 = kg_runtime._open_kuzu_db(db_path)
    try:
        conn2 = ladybug.Connection(db2)
        assert _count_rows(conn2, "MATCH (n:t) WHERE n.id = 1 RETURN count(n)") == 1
        conn2.close()
    finally:
        db2.close()
    gc.collect()
    assert "kg.recovery.wal_quarantined" not in _events(caplog), (
        "reopen pós-recovery não pode precisar de nova quarentena"
    )


# ---------------------------------------------------------------------------
# S6a (ts_af032a22) — dinâmico: nenhum caminho automático toca o main
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "salvage,wal_only,expect",
    [
        # Escada toda desligada → fail-closed puro (nada movido, nada mutado;
        # nenhum handle vivo → sha256 estrito no fluxo INTEIRO).
        (False, False, "fail_closed"),
        # Só o degrau 2 → recupera via quarentena wal-only.
        (False, True, "opened"),
        # Escada completa: salvage tenta primeiro; qualquer que seja o degrau
        # que resolva, o desfecho não pode ser fail-closed nesta classe.
        (True, True, "any"),
    ],
    ids=["all-disabled", "wal-only-only", "full-ladder"],
)
def test_s6_dynamic_no_automatic_path_touches_main(
    tmp_path, caplog, quarantine_boundary_hashes, salvage, wal_only, expect
):
    _configure(salvage=salvage, wal_only=wal_only)
    db_path = _make_board_with_zeroed_wal_body(tmp_path)
    kg_base = db_path.parents[2]
    wal_path = Path(str(db_path) + ".wal")
    board_id = db_path.parent.name
    main_hash_before = _sha256(db_path)
    wal_hash_before = _sha256(wal_path)

    caplog.set_level(logging.INFO)
    db = None
    raised: BaseException | None = None
    try:
        db = kg_runtime._open_kuzu_db(db_path)
    except RuntimeError as exc:
        raised = exc
    finally:
        if db is not None:
            db.close()
    gc.collect()

    # Invariantes S6/BR2 comuns a TODOS os desfechos: o main continua no
    # lugar original e NUNCA aparece em uma quarentena.
    assert db_path.exists(), "graph.lbug sumiu do lugar original"
    quarantine_root = kg_base / "quarantine"
    if quarantine_root.exists():
        for qdir in quarantine_root.iterdir():
            assert not (qdir / "graph.lbug").exists(), (
                f"main graph.lbug apareceu na quarentena {qdir.name} (BR2)"
            )
    # Se a quarentena wal-only executou, o main era byte-idêntico na
    # fronteira da operação (medição sem o handle vivo do engine).
    if board_id in quarantine_boundary_hashes:
        boundary_before, boundary_after = quarantine_boundary_hashes[board_id]
        assert boundary_before == boundary_after == main_hash_before, (
            "graph.lbug mutado pela operação de recovery (BR2)"
        )

    if expect == "fail_closed":
        assert raised is not None, "escada desligada deveria falhar fechado"
        # Nenhum handle vivo existiu: sha256 estrito do fluxo inteiro.
        assert _sha256(db_path) == main_hash_before, "graph.lbug mutado (BR2)"
        assert wal_path.exists() and _sha256(wal_path) == wal_hash_before, (
            "com a escada desligada nada pode ser movido/mutado"
        )
        assert "kg.recovery.wal_quarantined" not in _events(caplog)
    elif expect == "opened":
        assert raised is None, f"degrau 2 deveria ter reaberto: {raised}"
        assert board_id in quarantine_boundary_hashes, (
            "o desfecho 'opened' exige a quarentena wal-only no caminho"
        )
        assert "kg.recovery.wal_quarantined" in _events(caplog)
    else:  # "any" — o degrau vencedor depende do engine; fail-closed proibido.
        if raised is not None:
            pytest.fail(
                "escada completa não deveria terminar fail-closed nesta "
                f"classe de corrupção: {raised}"
            )


def test_s6_dynamic_all_rungs_fail_leaves_main_untouched(tmp_path, caplog):
    """O caso mais forte do S6a: main TAMBÉM corrompido (header zerado) —
    TODOS os degraus automáticos executam e falham (salvage → wal-only →
    reopen), o open termina fail-closed e o main é byte-idêntico do início ao
    fim (nenhum handle vivo jamais existiu)."""
    _configure(salvage=True, wal_only=True)
    db_path = _make_board_with_zeroed_wal_body(tmp_path)
    kg_base = db_path.parents[2]
    wal_path = Path(str(db_path) + ".wal")

    # Header do main zerado: "not a valid lbug database file" — nenhum degrau
    # consegue reabrir, mas todos EXECUTAM.
    with open(db_path, "r+b") as f:
        f.seek(0)
        f.write(b"\x00" * 64)

    main_hash_before = _sha256(db_path)
    wal_hash_before = _sha256(wal_path)

    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError):
        kg_runtime._open_kuzu_db(db_path)
    gc.collect()

    # Escada completa executou...
    events = _events(caplog)
    assert "kg.wal.salvage_failed" in events, "degrau 1 deveria ter rodado"
    assert "kg.recovery.wal_quarantined" in events, "degrau 2 deveria ter rodado"
    assert "kg.recovery.wal_only_reopen_failed" in events

    # ...e o main é byte-idêntico (sha256 estrito no fluxo inteiro).
    assert db_path.exists()
    assert _sha256(db_path) == main_hash_before, "graph.lbug mutado (BR2)"

    # O WAL foi preservado byte-idêntico na quarentena (evidência); o main
    # nunca aparece lá.
    rec = next(
        r for r in caplog.records
        if getattr(r, "event", None) == "kg.recovery.wal_quarantined"
    )
    quarantine_dir = kg_base / "quarantine" / rec.quarantine_id
    moved_wal = quarantine_dir / "graph.lbug.wal"
    assert moved_wal.exists()
    assert _sha256(moved_wal) == wal_hash_before
    assert not (quarantine_dir / "graph.lbug").exists(), (
        "main graph.lbug apareceu na quarentena (BR2)"
    )
    assert (quarantine_dir / "manifest.json").exists()


# ---------------------------------------------------------------------------
# S6b (ts_af032a22) — estático: regressão de call sites destrutivos no main
# ---------------------------------------------------------------------------

# Módulos adapters SANCIONADOS a conter operações destrutivas sobre o main
# graph.lbug (identificados por leitura do código; qualquer módulo novo que
# apareça aqui exige revisão explícita do porquê):
_SANCTIONED_MAIN_DESTRUCTIVE_MODULES: dict[str, str] = {
    # Purge explícito de rebuild/erasure: purge_board_graph_storage move o
    # main via KGQuarantineService (quarantine-then-clear auditado, FR7 do
    # KG-01.4) apenas sob comando explícito de operador/rebuild; o módulo
    # também referencia board_kuzu_path/GRAPH_DB_FILENAME em quarentenas
    # SOMENTE de sidecars (interrupted-checkpoint / wal-only degrau 2).
    "kg_runtime.py": "purge explícito de rebuild via KGQuarantineService",
    # Restore de quarentena (KGD-01 FR4): o backup-swap do apply move o main
    # vivo para uma quarentena de backup e copia o snapshot de volta — ação
    # explícita de operador, nunca disparada por open automático.
    "quarantine_restore.py": "restore backup-swap operador-driven (FR4)",
}

_DESTRUCTIVE_ATTRS = {"remove", "replace", "rename", "unlink", "move", "rmtree"}
_MAIN_MARKERS = ("graph.lbug", "GRAPH_DB_FILENAME", "board_kuzu_path(")
_SIDECAR_SUFFIXED = (
    "graph.lbug.wal",
    "graph.lbug.shadow",
    "graph.lbug.checkpoint",
    "graph.lbug.wal.checkpoint",
)


def _destructive_main_call_sites(source: str, filename: str) -> list[str]:
    """Retorna call sites destrutivos cujo texto referencia o MAIN graph.lbug.

    Heurística do TC3: o texto integral da chamada (receiver + argumentos)
    precisa conter um marcador do main ('graph.lbug' literal, a constante
    GRAPH_DB_FILENAME ou um board_kuzu_path(...)) que NÃO esteja imediatamente
    sufixado como sidecar (.wal/.shadow/.checkpoint). Chamadas destrutivas
    sobre variáveis opacas não são flagadas (cobertas pelo teste dinâmico);
    o alvo aqui é o call site NOVO e textual sobre o main.
    """
    flagged: list[str] = []
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in _DESTRUCTIVE_ATTRS:
            continue
        text = ast.unparse(node)
        # Remove as ocorrências sufixadas de sidecar; o que sobrar de
        # marcador de main é uma referência destrutiva ao main.
        stripped = text
        for suffixed in _SIDECAR_SUFFIXED:
            stripped = stripped.replace(suffixed, "")
        # Concatenações do tipo path.name + ".wal" apontam para sidecar.
        for concat in ('+ ".wal"', "+ '.wal'", '+ ".shadow"', "+ '.shadow'",
                      '+ ".wal.checkpoint"', "+ '.wal.checkpoint'"):
            if concat in stripped.replace("  ", " "):
                stripped = ""
                break
        if stripped and any(marker in stripped for marker in _MAIN_MARKERS):
            flagged.append(f"{filename}:{node.lineno}: {text}")
    return flagged


def test_s6_static_scanner_detects_synthetic_regression():
    """Auto-teste do scanner: um call site destrutivo sintético sobre o main
    PRECISA ser flagado — garante que o gate não é inócuo."""
    bad_snippets = [
        "board_kuzu_path(board_id).unlink()",
        "os.remove(str(board_kuzu_path(bid)))",
        "shutil.move(str(path / 'graph.lbug'), dst)",
        "os.replace(path.parent / GRAPH_DB_FILENAME, target)",
    ]
    for snippet in bad_snippets:
        assert _destructive_main_call_sites(snippet, "<synthetic>"), (
            f"scanner não flagou o call site sintético: {snippet}"
        )
    ok_snippets = [
        "wal.rename(quarantine_dir / wal.name)",
        "shutil.move(str(path) + '.wal', dst)",
        "os.remove(str(board_kuzu_path(bid)) + '.wal')",
    ]
    for snippet in ok_snippets:
        assert not _destructive_main_call_sites(snippet, "<synthetic>"), (
            f"scanner flagou falso positivo de sidecar: {snippet}"
        )


def test_s6_static_no_new_destructive_main_call_sites():
    py_files = sorted(_ADAPTERS_DIR.glob("*.py"))
    assert len(py_files) > 10, (
        f"varredura suspeita: só {len(py_files)} módulos em {_ADAPTERS_DIR}"
    )

    violations: list[str] = []
    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8", errors="ignore")
        flagged = _destructive_main_call_sites(source, py_file.name)
        if flagged and py_file.name not in _SANCTIONED_MAIN_DESTRUCTIVE_MODULES:
            violations.extend(flagged)

    assert not violations, (
        "Call site destrutivo NOVO sobre o main graph.lbug em módulo "
        "não-sancionado (BR2: nenhum caminho automático move/deleta o main). "
        "Se a operação é legítima (purge/restore explícito de operador), "
        "adicione o módulo a _SANCTIONED_MAIN_DESTRUCTIVE_MODULES com "
        f"justificativa. Violações:\n" + "\n".join(violations)
    )
