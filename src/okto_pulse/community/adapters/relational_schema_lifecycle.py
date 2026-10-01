"""Community-owned creation and admission of the current relational format."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .current_relational_schema import current_schema_contract, initialize_current_schema


class CommunityRelationalSchemaLifecycleOrchestrator:
    """Initialize only the current format under the runtime's lifecycle lock."""

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], Any],
        seed: Callable[[], Awaitable[None]],
    ) -> None:
        self._runtime_provider = runtime_provider
        self._seed = seed

    async def initialize_schema(self) -> None:
        from .sqlalchemy_database import _serialized_schema_lifecycle

        runtime = self._runtime_provider()
        async with _serialized_schema_lifecycle(runtime):
            await initialize_current_schema(runtime.engine, current_schema_contract())
            await self._seed()


def make_community_relational_schema_lifecycle_orchestrator(
    *, target: str = "community-sqlite",
) -> CommunityRelationalSchemaLifecycleOrchestrator:
    if target != "community-sqlite":
        raise ValueError("community_database_requires_sqlite")
    from .current_data_seeds import seed_current_catalogs
    from .sqlalchemy_database import resolve_community_database_runtime

    return CommunityRelationalSchemaLifecycleOrchestrator(
        runtime_provider=resolve_community_database_runtime,
        seed=seed_current_catalogs,
    )


def register_community_relational_schema_lifecycle(
    *, target: str = "community-sqlite",
) -> CommunityRelationalSchemaLifecycleOrchestrator:
    from okto_pulse.core import register_relational_schema_lifecycle_orchestrator

    orchestrator = make_community_relational_schema_lifecycle_orchestrator(target=target)
    register_relational_schema_lifecycle_orchestrator(orchestrator)
    return orchestrator


__all__ = [
    "CommunityRelationalSchemaLifecycleOrchestrator",
    "make_community_relational_schema_lifecycle_orchestrator",
    "register_community_relational_schema_lifecycle",
]
