"""REST confidentiality for Code Traceability projected into generic KG APIs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from okto_pulse.community.api import kg_routes
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.permissions import PermissionSet


BOARD_ID = "board-ct-rest"
LEGACY_ID = "legacy-node"
CT_ID = "ct-node"


def _actor(*, ct_read: bool) -> ActorContext:
    return ActorContext(
        "rest-ct-reader",
        "rest",
        board_id=BOARD_ID,
        permissions=PermissionSet(
            {
                "board": {"read": True},
                "kg": {"power": {"cypher": True}},
                "code_traceability": {
                    "investigation": {"read": ct_read},
                    "evidence": {"read": ct_read},
                    "target": {"read": ct_read},
                    "overlap": {"read": ct_read},
                },
            }
        ),
    )


def _row(node_id: str, *, kind_of: str | None = None) -> dict[str, Any]:
    return {
        "id": node_id,
        "node_type": "Entity",
        "title": node_id,
        "content": f"content-{node_id}",
        "created_at": "2026-08-09T12:00:00+00:00",
        "kind_of": kind_of,
    }


class _ProjectionService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    @staticmethod
    def _include(kwargs: dict[str, Any]) -> bool:
        return kwargs.get("include_code_traceability", True)

    def get_all_nodes(self, *_args, **kwargs):
        include = self._include(kwargs)
        self.calls.append(("nodes", include))
        rows = [_row(LEGACY_ID)]
        if include:
            rows.insert(0, _row(CT_ID, kind_of="code_evidence"))
        return rows

    def count_all_nodes(self, *_args, **kwargs):
        include = self._include(kwargs)
        self.calls.append(("count", include))
        return 2 if include else 1

    def get_node_detail(self, _board_id: str, node_id: str, **kwargs):
        include = self._include(kwargs)
        self.calls.append(("detail", include))
        if node_id == CT_ID and not include:
            return None
        return _row(
            node_id,
            kind_of="code_evidence" if node_id == CT_ID else None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("ct_read", "expected_ids"), ((False, [LEGACY_ID]), (True, [CT_ID, LEGACY_ID])))
async def test_nodes_total_and_detail_follow_all_of_ct_permission(
    ct_read: bool,
    expected_ids: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _ProjectionService()
    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: service)

    page = await kg_routes.list_nodes(
        BOARD_ID,
        type="",
        min_confidence=0.5,
        min_relevance=0.0,
        limit=50,
        cursor="",
        graph_layer="canonical",
        actor=_actor(ct_read=ct_read),
        uow=SimpleNamespace(),
    )
    detail = await kg_routes.get_node_detail(
        BOARD_ID,
        CT_ID,
        actor=_actor(ct_read=ct_read),
        uow=SimpleNamespace(),
    )

    assert [node["id"] for node in page["nodes"]] == expected_ids
    assert page["total_hint"] == len(expected_ids)
    if ct_read:
        assert detail["id"] == CT_ID
    else:
        assert detail.status_code == 404
    expected_include = ct_read
    assert service.calls == [
        ("nodes", expected_include),
        ("count", expected_include),
        ("detail", expected_include),
    ]


@pytest.mark.asyncio
async def test_graph_filters_ct_nodes_and_edge_endpoints_for_explicit_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _ProjectionService()
    observed: dict[str, bool] = {}

    def _edges(_board_id: str, _node_ids: set[str], **kwargs):
        include = kwargs.get("include_code_traceability", True)
        observed["edges"] = include
        edges = [{"source": LEGACY_ID, "target": CT_ID}] if include else []
        return edges, {"edge_read_status": "ok"}

    monkeypatch.setattr(kg_routes, "get_kg_service", lambda: service)
    monkeypatch.setattr(kg_routes, "_fetch_edges_for_nodes", _edges)

    payload = await kg_routes.get_subgraph(
        BOARD_ID,
        center="",
        depth=2,
        limit=100,
        cursor="",
        min_relevance=0.0,
        type="",
        graph_layer="canonical",
        actor=_actor(ct_read=False),
        uow=SimpleNamespace(),
    )

    assert [node["id"] for node in payload["nodes"]] == [LEGACY_ID]
    assert payload["edges"] == []
    assert observed == {"edges": False}


@pytest.mark.asyncio
async def test_cypher_guard_denies_without_complete_ct_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        kg_routes,
        "execute_cypher_read_only",
        lambda *_args, **_kwargs: calls.append("query") or {"rows": [["legacy"]]},
    )

    with pytest.raises(HTTPException) as exc_info:
        await kg_routes.cypher_query(
            BOARD_ID,
            cypher="MATCH (n) RETURN n",
            actor=_actor(ct_read=False),
            uow=SimpleNamespace(),
        )
    assert exc_info.value.status_code == 403
    assert calls == []


@pytest.mark.asyncio
async def test_cypher_complete_ct_grant_bypasses_materialization_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        kg_routes,
        "execute_cypher_read_only",
        lambda *_args, **_kwargs: {"rows": [[CT_ID]]},
    )

    payload = await kg_routes.cypher_query(
        BOARD_ID,
        cypher="MATCH (n) RETURN n",
        actor=_actor(ct_read=True),
        uow=SimpleNamespace(),
    )

    assert payload == {"rows": [[CT_ID]]}
