"""Bound the REST Health route-metadata phase without opening graph storage."""
from collections.abc import Callable
import time
from typing import TypeVar
import uuid

from okto_pulse.core.ports.materialization_health import run_bounded_health_probe

T = TypeVar("T")
_ROUTE_OBSERVATION_SECONDS = 0.35


async def observe_graph_route_metadata(
    board_id: str, *, render: Callable[[object, str], T], unavailable: T,
) -> T:
    """Render authenticated metadata inside the edition's observation scope.

    The fixed Health worker pool isolates blocked filesystem calls from the
    event loop. Cooperative limits bound Global metadata and no result arriving
    after the phase deadline is accepted. No new executor or recovery path.
    """
    deadline = time.monotonic() + _ROUTE_OBSERVATION_SECONDS
    observation_id = uuid.uuid4().hex

    def read():
        from okto_pulse.community.adapters.composition import (
            require_community_routed_graph_composition,
        )

        bundle = require_community_routed_graph_composition()
        capability = bundle.board.graph_health_observation
        remaining = deadline - time.monotonic()
        if capability is None or remaining <= 0:
            return observation_id, unavailable
        with capability.scope(board_id, timeout_seconds=remaining):
            value = render(bundle, board_id)
        return observation_id, value if time.monotonic() < deadline else unavailable

    result = await run_bounded_health_probe(
        name="community_graph_route_metadata", board_id=board_id, generation_id=None,
        build=read, fallback=(observation_id, unavailable), deadline_at=deadline, ttl_s=0,
    )
    identity, value = result.value
    # A previous/coalesced request cannot lend us stale binding metadata.
    return value if identity == observation_id else unavailable
