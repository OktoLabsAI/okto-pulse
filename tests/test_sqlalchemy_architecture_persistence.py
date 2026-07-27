"""Focused transaction cleanup coverage for Architecture persistence."""

from __future__ import annotations

import pytest

from okto_pulse.community.adapters.sqlalchemy_architecture_persistence import (
    CommunitySqlAlchemyArchitecturePersistence,
)
from okto_pulse.core.ports.architecture_persistence import ArchitectureRecord


class _RollbackContext:
    def __init__(self) -> None:
        self.rollback_calls = 0

    async def rollback(self) -> None:
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_rollback_clears_tracked_records() -> None:
    adapter = CommunitySqlAlchemyArchitecturePersistence()
    context = _RollbackContext()
    adapter._tracked[context] = [
        ArchitectureRecord(
            entity="architecture_design",
            values={"id": "design-1"},
        )
    ]

    await adapter.rollback(context)

    assert context.rollback_calls == 1
    assert context not in adapter._tracked
