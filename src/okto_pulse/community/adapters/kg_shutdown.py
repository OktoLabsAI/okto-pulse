"""Drain application probes and close the exact composed graph participants."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")
_HEALTH_PROBE_DRAIN_TIMEOUT_S = 30.0


def close_all_graphs_on_shutdown(*, runtime: Any | None = None) -> dict[str, int]:
    """Run on the shutdown worker, after producers stop; never force live handles."""
    from okto_pulse.core.services.application_kg import (
        drain_kg_health_probes,
        get_current_provider_registry,
    )

    started = time.perf_counter()
    pending = drain_kg_health_probes(timeout_s=_HEALTH_PROBE_DRAIN_TIMEOUT_S)
    if pending:
        logger.error("kg.shutdown.health_probes_not_drained pending=%d", pending)
    summary = {"boards_closed": 0, "boards_failed": 0, "global_failed": 0}
    try:
        registry = get_current_provider_registry()
        composition = registry._community_routed_graph_composition
    except Exception:
        logger.exception("kg.shutdown.graph_registry_unavailable")
        summary["boards_failed"] = 1
    else:
        board_root = composition.binding_store.root / "boards"
        pools = (composition.grafx_pool, *composition.board.grafx_read_pools)

        def paths():
            return {
                Path(p)
                for pool in pools
                for p in pool.pooled_paths()
                if Path(p).is_relative_to(board_root)
            }

        before = paths()
        try:
            asyncio.run(composition.board.graph_lifecycle.close(None))
        except Exception:
            remaining = paths() & before
            summary["boards_closed"] = len(before - remaining)
            summary["boards_failed"] = max(1, len(remaining))
            logger.exception("kg.shutdown.routed_board_close_failed")
        else:
            summary["boards_closed"] = len(before)
        try:
            composition.global_graph.close_all_on_shutdown()
        except Exception:
            summary["global_failed"] = 1
            logger.exception("kg.shutdown.global_graph_close_failed")
    summary["duration_ms"] = int((time.perf_counter() - started) * 1000)
    logger.info(
        "kg.shutdown.graphs_closed %s",
        summary,
        extra={"event": "kg.shutdown.graphs_closed", **summary},
    )
    return summary
