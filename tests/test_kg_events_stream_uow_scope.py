from __future__ import annotations

import inspect

from fastapi.params import Depends

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.kg_routes import (
    require_kg_stream_board_actor,
    stream_kg_events,
)


def _dependency(function, parameter: str) -> Depends:
    value = inspect.signature(function).parameters[parameter].default
    assert isinstance(value, Depends)
    return value


def test_kg_events_stream_closes_authorization_uow_before_streaming() -> None:
    actor_dependency = _dependency(stream_kg_events, "_actor")
    assert actor_dependency.dependency is require_kg_stream_board_actor

    uow_dependency = _dependency(require_kg_stream_board_actor, "uow")
    assert uow_dependency.dependency is get_unit_of_work
    assert uow_dependency.scope == "function"
