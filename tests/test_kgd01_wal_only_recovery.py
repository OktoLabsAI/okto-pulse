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
from pathlib import Path

import pytest

from okto_pulse.core.infra.config import (
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














# ---------------------------------------------------------------------------
# S5 (ts_f7c83a18) — wal-only preserva o main e reabre até o último checkpoint
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# S6a (ts_af032a22) — dinâmico: nenhum caminho automático toca o main
# ---------------------------------------------------------------------------


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
        for concat in (
            '+ ".wal"',
            "+ '.wal'",
            '+ ".shadow"',
            "+ '.shadow'",
            '+ ".wal.checkpoint"',
            "+ '.wal.checkpoint'",
        ):
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
        "justificativa. Violações:\n" + "\n".join(violations)
    )
