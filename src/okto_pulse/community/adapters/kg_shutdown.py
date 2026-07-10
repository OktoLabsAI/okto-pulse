"""KGD-01 (FR2/BR3) — checkpoint + close dos grafos LadybugDB no shutdown.

O ``combined_lifespan`` do community SUBSTITUI o ``_default_lifespan`` do core
e historicamente NÃO fechava os grafos no teardown — todo shutdown deixava o
``graph.lbug.wal`` como único portador dos commits recentes (~29 boards com
WAL residual). Este adapter fecha a lacuna: itera os ``ladybug.Database``
abertos no cache process-wide do ``kg_runtime``, executa um CHECKPOINT
best-effort por board (trunca o WAL, aplicando os commits no main) e então
fecha todos via :func:`kg_runtime.close_board_db_cache`.

Contrato de observabilidade (Architecture Design "Shutdown fecha os grafos"):
emite ``kg.shutdown.graphs_closed`` com ``boards_closed``/``boards_failed``/
``duration_ms``.

NÃO importa nada do engine diretamente — todo acesso a ladybug passa pelo
``kg_runtime`` (fábrica única / cache / close guard), preservando o boundary
hexagonal do board.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def close_all_graphs_on_shutdown() -> dict[str, int]:
    """Checkpoint (best-effort) e close de TODOS os board graphs abertos.

    Deve rodar no teardown do ``combined_lifespan`` DEPOIS que os workers de
    escrita (Consolidation/Outbox/tick) pararam e ANTES do close do SQLite.
    Síncrona e bloqueante (o engine é síncrono) — o caller decide se roda em
    ``asyncio.to_thread`` protegido contra cancelamento.

    Sequência por board (snapshot do cache sob o lock):
      1. ``CHECKPOINT`` via conexão crua sobre o Database cacheado — aplica o
         WAL no ``graph.lbug`` main e trunca o sidecar. Falha (ex.: leitor com
         transação ativa) é contada em ``boards_failed`` e logada; o close
         ainda acontece.
      2. ``close_board_db_cache(None)`` fecha todos os Databases do cache com
         o dreno do close guard (o comportamento de timeout é o do kg_runtime).

    Retorna ``{"boards_closed": N, "boards_failed": M, "duration_ms": T}`` e
    emite o log estruturado ``kg.shutdown.graphs_closed`` com os mesmos campos.
    Nunca levanta: qualquer falha inesperada é logada e refletida na contagem.
    """
    started = time.perf_counter()

    # Import tardio: este módulo pode ser importado no teardown de processos
    # que nunca tocaram o KG — não pagar o custo do kg_runtime no import.
    from okto_pulse.community.adapters import kg_runtime

    with kg_runtime._board_db_cache_lock:
        open_dbs: list[tuple[str, Any]] = list(kg_runtime._board_db_cache.items())

    boards_closed = 0
    boards_failed = 0
    for key, db in open_dbs:
        # A chave do cache é o path do graph.lbug (…/boards/<board_id>/graph.lbug);
        # o diretório pai identifica o board (mesma convenção do kg_runtime).
        board_id = Path(key).parent.name
        try:
            conn = kg_runtime.kuzu.Connection(db)
            try:
                conn.execute("CHECKPOINT")
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001 — close da conexão é best-effort
                    pass
        except Exception as exc:  # noqa: BLE001 — checkpoint é best-effort por board
            boards_failed += 1
            logger.warning(
                "kg.shutdown.graph_checkpoint_failed board=%s err=%s",
                board_id,
                exc,
                extra={
                    "event": "kg.shutdown.graph_checkpoint_failed",
                    "board_id": board_id,
                    "error": str(exc),
                },
            )
        else:
            boards_closed += 1

    try:
        # KGD-01 C6: o shutdown é o ÚNICO caller autorizado a forçar o close
        # após o timeout do dreno — o teardown roda APÓS a parada dos workers,
        # então um leitor remanescente é vazado por definição. O default
        # fail-closed de close_board_db_cache adiaria o close e deixaria WAL
        # residual; o force usa dreno maior e loga
        # kg.close_guard.forced_on_shutdown quando fecha com leitor preso.
        kg_runtime.close_board_db_cache(None, force_after_drain_timeout=True)
    except Exception as exc:  # noqa: BLE001 — nunca propagar para o teardown
        logger.warning(
            "kg.shutdown.graph_close_failed err=%s",
            exc,
            extra={"event": "kg.shutdown.graph_close_failed", "error": str(exc)},
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    summary = {
        "boards_closed": boards_closed,
        "boards_failed": boards_failed,
        "duration_ms": duration_ms,
    }
    logger.info(
        "kg.shutdown.graphs_closed boards_closed=%d boards_failed=%d duration_ms=%d",
        boards_closed,
        boards_failed,
        duration_ms,
        extra={"event": "kg.shutdown.graphs_closed", **summary},
    )
    return summary
