"""Community-facing KG health probe over the public unit-of-work services."""

from __future__ import annotations

from okto_pulse.core.ports.scheduler import SchedulerControl
from okto_pulse.core.repositories import PulseUnitOfWork


async def get_kg_health(
    board_id: str,
    uow: PulseUnitOfWork,
    *,
    scheduler_control: SchedulerControl | None = None,
) -> dict[str, object]:
    return await uow.services.kg.health(
        board_id,
        scheduler_control=scheduler_control,
    )


__all__ = ["get_kg_health"]
