"""Non-live Grafx constructor budget projection; never claims an RSS bound."""

from __future__ import annotations

from typing import Any
from okto_pulse.core.kg.interfaces.graph_runtime_store import GraphRuntimeBudgetSnapshot


def build_native_runtime_budget_snapshot(
    settings: Any | None = None,
) -> GraphRuntimeBudgetSnapshot:
    if settings is None:
        from okto_pulse.core import get_settings

        settings = get_settings()
    budget = int(settings.kg_grafx_buffer_pool_mb)
    readers = int(settings.kg_grafx_read_participants)
    return GraphRuntimeBudgetSnapshot(
        source="runtime_capability",
        status="available",
        requested={
            "board_buffer_pool_mb": budget,
            "global_buffer_pool_mb": budget,
            "read_participants": readers,
        },
        normalized={},
        effective={
            "board_buffer_pool_mb": budget,
            "global_buffer_pool_mb": budget,
            "read_participants": readers,
        },
        sources={},
        process_envelope={
            "buffer_pool_per_board_mb": budget * (1 + readers),
            "global_buffer_pool_mb": budget,
            "total_process_bound_available": False,
        },
    )
