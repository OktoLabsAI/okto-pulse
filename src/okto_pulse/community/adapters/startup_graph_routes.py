"""Community startup adoption for pre-routing Board graph storage.

Existing Pulse installations can have physical Board databases created before
immutable backend bindings existed.  Core's schema sweep intentionally skips a
Board whose runtime reports no graph, so Community must adopt those existing
databases before delegating to that sweep.  This helper never creates a graph;
the routed composition owns the metadata-only adoption decision.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any


async def adopt_existing_board_routes_before_schema_sweep(
    *,
    uow_factory: Any | None = None,
    logger: logging.Logger,
    routed_graph: Any | None = None,
) -> dict[str, int]:
    """Adopt only already-present unbound Board graphs, soft-failing per Board."""

    counts = {"inspected": 0, "ready": 0, "absent": 0, "failed": 0}
    try:
        if routed_graph is None:
            from okto_pulse.community.adapters.composition import (
                require_community_routed_graph_composition,
            )

            routed_graph = require_community_routed_graph_composition()

        from okto_pulse.core.runtime_registry import resolve_unit_of_work_factory

        factory = uow_factory or resolve_unit_of_work_factory()
        realm_scope = factory.resolve_realm_scope()
        async with factory(realm_scope=realm_scope) as uow:
            board_ids = tuple(await uow.services.list_board_ids())
    except Exception as failure:  # noqa: BLE001 - startup remains board-local
        counts["failed"] += 1
        logger.warning(
            "kg.graph_route.pre_sweep_unavailable err=%s",
            failure,
            extra={
                "event": "kg.graph_route.pre_sweep_unavailable",
                "error_type": type(failure).__name__,
            },
        )
        return counts

    for raw_board_id in board_ids:
        board_id = raw_board_id
        counts["inspected"] += 1
        try:
            if type(board_id) is not str or not board_id:
                raise ValueError("startup graph route board_id is invalid")
            snapshot = await asyncio.to_thread(
                routed_graph.adopt_existing_board_route,
                board_id,
            )
        except Exception as failure:  # noqa: BLE001 - soft-fail per Board
            counts["failed"] += 1
            logger.warning(
                "kg.graph_route.pre_sweep_adoption_failed board_id=%s err=%s",
                board_id,
                failure,
                extra={
                    "event": "kg.graph_route.pre_sweep_adoption_failed",
                    "board_id": board_id,
                    "error_type": type(failure).__name__,
                },
            )
            continue
        if snapshot is None:
            counts["absent"] += 1
            continue
        counts["ready"] += 1
        logger.info(
            "kg.graph_route.pre_sweep_ready board_id=%s backend=%s generation=%s",
            board_id,
            snapshot.backend,
            snapshot.generation,
            extra={
                "event": "kg.graph_route.pre_sweep_ready",
                "board_id": board_id,
                "backend": snapshot.backend,
                "generation": snapshot.generation,
            },
        )
    return counts


__all__ = ["adopt_existing_board_routes_before_schema_sweep"]
