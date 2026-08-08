"""REST/MCP parity for KG power and semantic-query permission gates."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from okto_pulse.community.api import kg_routes
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.permissions import PermissionSet
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.services import application_kg


BOARD_ID = "board-kg-rest-policy"


def _permission_set(values: dict[str, bool]) -> PermissionSet:
    document: dict[str, Any] = {}
    for path, value in values.items():
        cursor = document
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return PermissionSet(document)


def _actor(permissions: Any, *, board_id: str | None = BOARD_ID) -> ActorContext:
    return ActorContext(
        "rest-operator",
        "rest",
        board_id=board_id,
        realm_id=LOCAL_REALM_ID,
        permissions=permissions,
    )


_DENIAL_CASES = (
    (
        "kg.query.related_context",
        lambda actor, uow: kg_routes.get_subgraph(
            BOARD_ID,
            center="decision:1",
            depth=2,
            limit=100,
            cursor="",
            min_relevance=0.0,
            type="",
            graph_layer="canonical",
            actor=actor,
            uow=uow,
        ),
    ),
    (
        "kg.query.global",
        lambda actor, uow: kg_routes.global_search(
            q="policy",
            limit=20,
            min_similarity=0.3,
            graph_layer="canonical",
            actor=actor,
            uow=uow,
        ),
    ),
    (
        "kg.query.similar_decisions",
        lambda actor, uow: kg_routes.find_similar(
            BOARD_ID,
            topic="policy",
            actor=actor,
            uow=uow,
        ),
    ),
    (
        "kg.query.supersedence_chain",
        lambda actor, uow: kg_routes.get_supersedence(
            BOARD_ID,
            "decision-1",
            actor=actor,
            uow=uow,
        ),
    ),
    (
        "kg.query.contradictions",
        lambda actor, uow: kg_routes.find_contradictions(
            BOARD_ID,
            actor=actor,
            uow=uow,
        ),
    ),
    (
        "kg.power.cypher",
        lambda actor, uow: kg_routes.cypher_query(
            BOARD_ID,
            cypher="MATCH (n) RETURN n",
            actor=actor,
            uow=uow,
        ),
    ),
    (
        "kg.power.schema_info",
        lambda actor, uow: kg_routes.schema_info(
            board_id="",
            actor=actor,
            uow=uow,
        ),
    ),
)


_BOARD_SCOPED_CASES = (
    _DENIAL_CASES[0],
    _DENIAL_CASES[2],
    _DENIAL_CASES[3],
    _DENIAL_CASES[4],
    _DENIAL_CASES[5],
    (
        "kg.power.schema_info",
        lambda actor, uow: kg_routes.schema_info(
            board_id=BOARD_ID,
            actor=actor,
            uow=uow,
        ),
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "invoke"),
    _DENIAL_CASES,
    ids=[case[0] for case in _DENIAL_CASES],
)
async def test_explicit_exact_denial_stops_before_kg_service(
    operation: str,
    invoke: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("KG service must not run before exact authorization")

    monkeypatch.setattr(kg_routes, "get_kg_service", _unexpected)
    monkeypatch.setattr(kg_routes, "execute_cypher_read_only", _unexpected)
    monkeypatch.setattr(kg_routes, "get_schema_info", _unexpected)
    monkeypatch.setattr(application_kg, "query_global", _unexpected)
    permission_values = {operation: False}
    if operation not in {"kg.query.global", "kg.power.schema_info"}:
        permission_values["board.read"] = True
    actor = _actor(
        _permission_set(permission_values),
        board_id=None
        if operation in {"kg.query.global", "kg.power.schema_info"}
        else BOARD_ID,
    )

    with pytest.raises(HTTPException) as exc_info:
        await invoke(actor, SimpleNamespace())

    assert exc_info.value.status_code == 403
    assert operation in str(exc_info.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "invoke"),
    _BOARD_SCOPED_CASES,
    ids=[case[0] for case in _BOARD_SCOPED_CASES],
)
async def test_board_read_denial_stops_exact_kg_operation_before_service(
    operation: str,
    invoke: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("KG service must not run without board.read")

    monkeypatch.setattr(kg_routes, "get_kg_service", _unexpected)
    monkeypatch.setattr(kg_routes, "execute_cypher_read_only", _unexpected)
    monkeypatch.setattr(kg_routes, "get_schema_info", _unexpected)
    actor = _actor(
        _permission_set(
            {
                "board.read": False,
                operation: True,
            }
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await invoke(actor, SimpleNamespace())

    assert exc_info.value.status_code == 403
    assert "board.read" in str(exc_info.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "invoke"),
    _DENIAL_CASES,
    ids=[case[0] for case in _DENIAL_CASES],
)
async def test_legacy_board_read_remains_compatible(
    operation: str,
    invoke: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = SimpleNamespace(
        get_related_context=lambda *_args, **_kwargs: [],
        find_similar_decisions=lambda *_args, **_kwargs: [],
        get_supersedence_chain=lambda *_args, **_kwargs: {"chain": []},
        find_contradictions=lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: service)
    monkeypatch.setattr(
        kg_routes,
        "execute_cypher_read_only",
        lambda *_args, **_kwargs: {"rows": []},
    )
    monkeypatch.setattr(
        kg_routes,
        "get_schema_info",
        lambda *_args, **_kwargs: {"schema_version": "test"},
    )
    monkeypatch.setattr(application_kg, "query_global", lambda *_args, **_kwargs: [])

    uow = SimpleNamespace(
        services=SimpleNamespace(
            boards=SimpleNamespace(
                list_boards=AsyncMock(return_value=([], 0)),
            ),
        ),
    )
    actor_board_id = (
        None
        if operation in {"kg.query.global", "kg.power.schema_info"}
        else BOARD_ID
    )

    result = await invoke(
        _actor(["board:read"], board_id=actor_board_id),
        uow,
    )

    assert result is not None


@pytest.mark.asyncio
async def test_internal_schema_requires_admin_read_before_introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def _unexpected(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(kg_routes, "get_schema_info", _unexpected)
    actor = _actor(
        _permission_set(
            {
                "kg.power.schema_info": True,
                "kg.admin.settings_read": False,
            }
        ),
        board_id=None,
    )

    with pytest.raises(HTTPException) as exc_info:
        await kg_routes.schema_info(
            board_id="",
            include_internal=True,
            actor=actor,
            uow=SimpleNamespace(),
        )

    assert exc_info.value.status_code == 403
    assert "kg.admin.settings_read" in str(exc_info.value.detail)
    assert not called
