"""Community wal-only recovery adapter (spec KGD-01 FR3/BR2/D3, card f7af4678).

Degrau 2 da escada de recovery (salvage -> wal-only -> restore/operador):
quando o open de um board graph falha com marcador de corrupção e o salvage
nativo de WAL (degrau 1, KGD-01 FR1) falhou ou está indisponível/desligado,
este adapter move SOMENTE o ``graph.lbug.wal`` e os sidecars de checkpoint
(``.shadow`` / ``.wal.checkpoint``) para uma quarentena nova com manifest
completo, preservando a evidência para restore posterior via o port
``QuarantineRestore`` (KGD-01 FR4).

BR2 (invariante duro): NENHUM caminho deste módulo move, renomeia ou deleta o
main ``graph.lbug``. O pior desfecho do degrau 2 é perder os commits
pós-checkpoint (que ficam preservados na quarentena), nunca o grafo inteiro.

Windows-first (mesmo contrato do ``CommunityQuarantineRestore``):
``gc.collect()`` antes dos moves libera handles C++ pendentes; PermissionError
tem retry curto com backoff; um partial-move NUNCA é silencioso — o manifest
registra exatamente o que foi movido e a falha vira ``ok=False`` no resultado
(o caller segue fail-closed).
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from okto_pulse.core.kg.interfaces.graph_recovery import WalRecoveryReport

logger = logging.getLogger("okto_pulse.kg.recovery")

MANIFEST_JSON = "manifest.json"

# Retry curto de PermissionError (TR6 — handles NTFS): suficiente para um
# handle transitório (AV scan, indexer) soltar; curto o bastante para um lock
# real falhar rápido em testes e para o operador.
_MOVE_ATTEMPTS = 3
_MOVE_BACKOFF_BASE_SECONDS = 0.05


@dataclass(frozen=True)
class WalOnlyQuarantineResult:
    """Resultado da quarentena wal-only (intermediário; ver port p/ report)."""

    board_id: str
    ok: bool
    quarantine_id: str | None = None
    files_moved: tuple[str, ...] = ()
    reason: str | None = None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _move_with_retry(src: Path, dst: Path) -> None:
    """shutil.move com retry curto em PermissionError (Windows-first)."""
    last_exc: BaseException | None = None
    for attempt in range(1, _MOVE_ATTEMPTS + 1):
        try:
            shutil.move(str(src), str(dst))
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt < _MOVE_ATTEMPTS:
                gc.collect()  # Windows: solta handles C++ pendentes
                time.sleep(_MOVE_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    assert last_exc is not None
    raise last_exc


def _wal_only_candidates(graph_path: Path) -> list[Path]:
    """Sidecars elegíveis para a quarentena wal-only — NUNCA o main.

    O ``.wal`` principal é o alvo do degrau 2 (o replay dele é o que aborta o
    open); ``.shadow`` e ``.wal.checkpoint`` são lixo de checkpoint
    interrompido e vão junto quando ainda existirem (normalmente o degrau de
    auto-recovery de sidecars já os quarentenou antes).
    """
    return [
        graph_path.with_name(graph_path.name + ".wal"),
        graph_path.with_name(graph_path.name + ".shadow"),
        graph_path.with_name(graph_path.name + ".wal.checkpoint"),
    ]


def wal_only_quarantine(
    board_id: str,
    reason: str,
    *,
    graph_path: Path | None = None,
) -> WalOnlyQuarantineResult:
    """Move SOMENTE graph.lbug.wal + sidecars para uma quarentena nova.

    Segue o padrão de diretório/manifest de
    ``kg_runtime._quarantine_interrupted_checkpoint_sidecars``: a quarentena
    vive em ``<kg_base>/quarantine/<quarantine_id>/`` (dois níveis acima do
    board dir) e carrega um ``manifest.json`` completo — board_id, motivo,
    arquivos movidos com tamanho/sha256, e ``main_untouched: true``.

    BR2: o main ``graph.lbug`` NUNCA entra na lista de candidatos; um guard
    estrutural aborta qualquer tentativa de mover um arquivo com o nome do
    main. Partial-move nunca é silencioso: em falha no meio dos moves o
    manifest é escrito mesmo assim registrando o estado exato e o resultado
    volta ``ok=False``.

    ``graph_path`` permite ao caller (fábrica de open / testes) passar o path
    já resolvido; quando omitido, resolve via ``board_kuzu_path`` (exige o
    provider registry configurado).
    """
    if graph_path is None:
        from okto_pulse.community.adapters.kg_runtime import board_kuzu_path

        graph_path = board_kuzu_path(board_id)

    candidates = [c for c in _wal_only_candidates(graph_path) if c.exists()]
    if not candidates:
        return WalOnlyQuarantineResult(
            board_id=board_id,
            ok=True,
            files_moved=(),
            reason="no_wal_or_sidecar_files_present",
        )

    timestamp = _now_utc().strftime("%Y%m%dT%H%M%S%f")
    quarantine_id = f"wal-only-{board_id}-{timestamp}"
    quarantine_dir = graph_path.parents[2] / "quarantine" / quarantine_id

    planned = [
        {
            "name": c.name,
            "size": c.stat().st_size if c.exists() else None,
            "sha256": _sha256_file(c),
        }
        for c in candidates
    ]

    moved: list[str] = []
    failure: str | None = None
    gc.collect()  # Windows: libera handles C++ antes do primeiro move
    try:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        for candidate in candidates:
            # BR2 — guard estrutural: NUNCA mover o main file. Os candidatos
            # são construídos com sufixo, mas o guard mantém o invariante
            # auditável mesmo se a lista mudar no futuro.
            if candidate.name == graph_path.name:
                raise RuntimeError(
                    "BR2 violation blocked: wal_only_quarantine tentou mover "
                    f"o main file {candidate}"
                )
            _move_with_retry(candidate, quarantine_dir / candidate.name)
            moved.append(candidate.name)
    except (OSError, RuntimeError) as exc:
        failure = f"{type(exc).__name__}: {exc}"

    manifest = {
        "kind": "kg_wal_only_quarantine",
        "quarantine_id": quarantine_id,
        "board_id": board_id,
        "reason": reason,
        "created_at": _now_utc().isoformat(),
        "graph_path": str(graph_path),
        "planned_files": planned,
        "files": moved,
        "main_untouched": True,
        "main_file": graph_path.name,
        "error": failure,
    }
    try:
        (quarantine_dir / MANIFEST_JSON).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as manifest_exc:
        failure = failure or f"manifest_write_failed: {manifest_exc}"

    if failure is not None:
        # Partial-move NUNCA silencioso: o manifest (quando gravável) já
        # registra o que moveu; o log estruturado carrega o mesmo estado.
        logger.error(
            "kg.recovery.wal_quarantine_failed board=%s quarantine_id=%s "
            "moved=%s err=%s",
            board_id,
            quarantine_id,
            moved,
            failure,
            extra={
                "event": "kg.recovery.wal_quarantine_failed",
                "board_id": board_id,
                "quarantine_id": quarantine_id,
                "files": list(moved),
                "main_untouched": True,
            },
        )
        return WalOnlyQuarantineResult(
            board_id=board_id,
            ok=False,
            quarantine_id=quarantine_id,
            files_moved=tuple(moved),
            reason=failure,
        )

    logger.warning(
        "kg.recovery.wal_quarantined board=%s quarantine_id=%s files=%s "
        "main_untouched=True reason=%s",
        board_id,
        quarantine_id,
        moved,
        reason,
        extra={
            "event": "kg.recovery.wal_quarantined",
            "board_id": board_id,
            "quarantine_id": quarantine_id,
            "files": list(moved),
            "main_untouched": True,
        },
    )
    return WalOnlyQuarantineResult(
        board_id=board_id,
        ok=True,
        quarantine_id=quarantine_id,
        files_moved=tuple(moved),
        reason=reason,
    )


class CommunityGraphRecovery:
    """Adapter do port ``GraphRecovery`` (KGD-01 FR3): wal-only + open probe.

    Delegação fina: fecha o cache de Database do board (libera o lock do OS
    no Windows), quarentena wal-only via :func:`wal_only_quarantine` e prova
    o reopen pela fábrica única de opens do adapter. O main ``graph.lbug``
    nunca é tocado (BR2) — ``main_untouched`` é sempre True no report.
    """

    async def recover_wal_only(self, board_id: str) -> WalRecoveryReport:
        return await self._recover_wal_only(board_id, close_window_owned=False)

    async def recover_wal_only_unguarded(self, board_id: str) -> WalRecoveryReport:
        """Recover while the routed facade owns the exclusive close window."""

        return await self._recover_wal_only(board_id, close_window_owned=True)

    async def _recover_wal_only(
        self,
        board_id: str,
        *,
        close_window_owned: bool,
    ) -> WalRecoveryReport:
        # Lazy import: mesmo padrão de CommunityKuzuGraphLifecycle — importar
        # este módulo nunca eager-carrega Ladybug.
        from okto_pulse.community.adapters import kg_runtime

        path = kg_runtime.board_kuzu_path(board_id)
        if not path.exists():
            return WalRecoveryReport(
                board_id=board_id,
                status="skipped",
                main_untouched=True,
                reason=f"{path.name} missing at {path}",
            )

        # Solta o Database cacheado (e o file lock) antes de mover sidecars.
        if close_window_owned:
            kg_runtime._close_cached_db_unguarded(board_id)
        else:
            kg_runtime.close_board_db_cache(board_id)

        result = wal_only_quarantine(
            board_id, "graph_recovery.recover_wal_only", graph_path=path
        )
        if not result.ok:
            return WalRecoveryReport(
                board_id=board_id,
                status="failed",
                quarantine_id=result.quarantine_id,
                files_moved=result.files_moved,
                main_untouched=True,
                reason=result.reason,
            )
        if not result.files_moved:
            return WalRecoveryReport(
                board_id=board_id,
                status="skipped",
                main_untouched=True,
                reason=result.reason,
            )

        # Open probe pela fábrica única; fecha depois para o próximo leitor
        # provar o open a partir do disco (mesmo contrato do
        # close_reopen_probe do rebuild).
        try:
            kg_runtime._open_kuzu_db_path_cached(path)
        except Exception as exc:
            return WalRecoveryReport(
                board_id=board_id,
                status="failed",
                quarantine_id=result.quarantine_id,
                files_moved=result.files_moved,
                main_untouched=True,
                reason=f"reopen_failed: {type(exc).__name__}: {exc}",
            )
        finally:
            if close_window_owned:
                kg_runtime._close_cached_db_unguarded(board_id)
            else:
                kg_runtime.close_board_db_cache(board_id)

        return WalRecoveryReport(
            board_id=board_id,
            status="recovered",
            quarantine_id=result.quarantine_id,
            files_moved=result.files_moved,
            main_untouched=True,
            reason=None,
        )


__all__ = [
    "CommunityGraphRecovery",
    "WalOnlyQuarantineResult",
    "wal_only_quarantine",
]
