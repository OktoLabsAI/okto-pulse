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

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

# The Community runtime is served by Uvicorn with its default logging config,
# which only wires the ``uvicorn.*`` logger family.  Using the module logger
# here silently dropped the INFO-level structured shutdown receipts from the
# installed runtime even though checkpoint/close itself succeeded.  Route the
# lifecycle contract through Uvicorn's error logger so operators and cutover
# automation can verify the ordered teardown from the actual process logs.
logger = logging.getLogger("uvicorn.error")

_HEALTH_PROBE_DRAIN_TIMEOUT_S = 30.0


def close_all_graphs_on_shutdown(*, runtime: Any | None = None) -> dict[str, int]:
    """Drain runtime-owned readers, then serialize the final graph close."""

    # Health endpoints deliberately return on a short budget while their
    # daemon probes keep reading the graph.  The pool is runtime-local (also
    # inside the Demo seed's isolated provider scope), so drain that exact
    # composition before acquiring the final writer lease.  Otherwise a late
    # probe can reopen Demo after checkpoint/close and race the next board's
    # event-loop bootstrap.  This preserves the event-loop fail-fast rule: the
    # lifecycle waits on its teardown worker instead of weakening contention.
    from okto_pulse.core.services.application_kg import (
        drain_kg_health_probes,
    )

    pending_probes = drain_kg_health_probes(
        timeout_s=_HEALTH_PROBE_DRAIN_TIMEOUT_S,
    )
    if pending_probes:
        logger.error(
            "kg.shutdown.health_probes_not_drained pending=%d timeout_s=%.1f",
            pending_probes,
            _HEALTH_PROBE_DRAIN_TIMEOUT_S,
            extra={
                "event": "kg.shutdown.health_probes_not_drained",
                "pending": pending_probes,
                "timeout_s": _HEALTH_PROBE_DRAIN_TIMEOUT_S,
            },
        )

    from okto_pulse.community.adapters.ladybug_writer import (
        ladybug_writer_scope,
    )

    with ladybug_writer_scope(
        scope="shutdown",
        phase="checkpoint_close",
    ):
        return _close_all_graphs_with_writer_lease(runtime=runtime)


def _close_all_graphs_with_writer_lease(
    *,
    runtime: Any | None = None,
) -> dict[str, int]:
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

    Depois dos board graphs, fecha também o handle persistente do Global
    Discovery. Esse handle não participa do cache por board e, sem este passo,
    ``discovery.lbug.wal`` sobrevive ao shutdown (normalmente só com comandos
    idempotentes de ``INSTALL/LOAD VECTOR``, mas ainda denuncia um lifecycle
    incompleto e pode preservar lock/commits no encerramento do interpretador).

    Retorna ``{"boards_closed": N, "boards_failed": M, "duration_ms": T}`` e
    emite o log estruturado ``kg.shutdown.graphs_closed`` com os mesmos campos.
    Nunca levanta: qualquer falha inesperada é logada e refletida na contagem.
    """
    started = time.perf_counter()

    # Import tardio: este módulo pode ser importado no teardown de processos
    # que nunca tocaram o KG — não pagar o custo do kg_runtime no import.
    if runtime is None:
        from okto_pulse.community.adapters import kg_runtime as runtime

    registry = None
    routed_graph = None
    try:
        from okto_pulse.core.services.application_kg import (
            get_current_provider_registry,
        )

        registry = get_current_provider_registry()
        routed_graph = getattr(
            registry,
            "_community_routed_graph_composition",
            None,
        )
    except Exception as exc:  # noqa: BLE001 - legacy/partial teardown remains safe
        logger.warning(
            "kg.shutdown.graph_registry_unavailable err=%s",
            exc,
            extra={
                "event": "kg.shutdown.graph_registry_unavailable",
                "error": str(exc),
            },
        )

    with runtime._board_db_cache_lock:
        open_dbs: list[tuple[str, Any]] = list(runtime._board_db_cache.items())

    boards_closed = 0
    boards_failed = 0
    for key, db in open_dbs:
        # A chave do cache é o path do graph.lbug (…/boards/<board_id>/graph.lbug);
        # o diretório pai identifica o board (mesma convenção do kg_runtime).
        board_id = Path(key).parent.name
        try:
            conn = runtime.kuzu.Connection(db)
            try:
                result = conn.execute("CHECKPOINT")
                close_result = getattr(result, "close", None)
                if callable(close_result):
                    close_result()
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
        runtime.close_board_db_cache(None, force_after_drain_timeout=True)
    except Exception as exc:  # noqa: BLE001 — nunca propagar para o teardown
        logger.warning(
            "kg.shutdown.graph_close_failed err=%s",
            exc,
            extra={"event": "kg.shutdown.graph_close_failed", "error": str(exc)},
        )

    # The routed Board lifecycle is the only complete close door: its Ladybug
    # half is idempotent after the checkpoint/cache close above, while its
    # Grafx half closes every Board handle in the shared pool without touching
    # Global. Account exact Board Grafx paths before/after so a partial pool
    # close remains observable rather than being reported as total success.
    if routed_graph is not None:
        board_root = routed_graph.binding_store.root / "boards"

        def pooled_board_paths() -> set[Path]:
            paths: set[Path] = set()
            for raw_path in routed_graph.grafx_pool.pooled_paths():
                path = Path(raw_path)
                try:
                    path.relative_to(board_root)
                except ValueError:
                    continue
                paths.add(path)
            return paths

        before = pooled_board_paths()
        try:
            asyncio.run(routed_graph.board.graph_lifecycle.close(None))
        except Exception as exc:  # noqa: BLE001 - Global close must still run
            after = pooled_board_paths()
            boards_closed += len(before - after)
            boards_failed += max(1, len(before & after))
            logger.warning(
                "kg.shutdown.routed_board_close_failed err=%s",
                exc,
                extra={
                    "event": "kg.shutdown.routed_board_close_failed",
                    "error": str(exc),
                    "grafx_remaining": len(before & after),
                },
            )
        else:
            boards_closed += len(before)

    # O Global Discovery usa um Database persistente próprio, fora de
    # ``_board_db_cache``. ``Database.close()`` executa o checkpoint final do
    # Ladybug e remove o WAL; apenas fechar os boards deixa esse handle vivo.
    # Resolver pelo port mantém o adapter independente da implementação
    # concreta e torna o close idempotente quando o global nunca foi aberto.
    global_close_attempted = False
    try:
        if routed_graph is not None:
            global_close_attempted = True
            routed_graph.global_graph.close_all_on_shutdown()
        elif registry is not None:
            # Legacy/non-routed registry compatibility. Productive Community
            # composition always takes the branch above.
            from okto_pulse.community.adapters.coordination import (
                community_global_discovery_writer_fence,
            )

            global_runtime = registry.require_global_discovery_runtime()
            global_close_attempted = True
            with community_global_discovery_writer_fence("shutdown_global_close"):
                global_runtime.close()
    except Exception as exc:  # noqa: BLE001 — teardown nunca propaga
        logger.warning(
            "kg.shutdown.global_graph_close_failed err=%s",
            exc,
            extra={
                "event": "kg.shutdown.global_graph_close_failed",
                "error": str(exc),
            },
        )
    else:
        if global_close_attempted:
            logger.info(
                "kg.shutdown.global_graph_closed",
                extra={"event": "kg.shutdown.global_graph_closed"},
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
