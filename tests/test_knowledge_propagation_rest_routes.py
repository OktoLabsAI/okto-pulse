from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from okto_pulse.community.api import boards as boards_api
from okto_pulse.community.api import cards as cards_api
from okto_pulse.community.api import refinements as refinements_api
from okto_pulse.core.application.use_cases import EntityNotFoundError
from okto_pulse.core.models import CardCreate
from okto_pulse.core.models.knowledge_propagation import (
    DeriveSpecKnowledgeRequest,
    KnowledgeAssignmentReplaceRequest,
)


def _v2_envelope() -> dict[str, Any]:
    return {
        "contract_version": 2,
        "selection_state": "omitted",
        "knowledge_ids": [],
        "idempotency_key": "idem-1",
    }


class _RequestWithBody:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


def test_openapi_publishes_all_selective_propagation_rest_surfaces() -> None:
    app = FastAPI()
    app.include_router(refinements_api.router, prefix="/api/v1")
    app.include_router(boards_api.router, prefix="/api/v1/boards")
    app.include_router(cards_api.router, prefix="/api/v1/cards")

    paths = app.openapi()["paths"]
    assert "/api/v1/refinements/{refinement_id}/derive-spec" in paths
    assert "/api/v1/boards/{board_id}/cards" in paths
    assignment_path = "/api/v1/cards/{card_id}/knowledge-assignments"
    assert set(paths[assignment_path]) >= {"get", "put"}
    assert (
        "/api/v1/cards/{card_id}/knowledge-assignments/drop" in paths
    )
    assert (
        "/api/v1/cards/{card_id}/knowledge-assignments/refresh" in paths
    )


@pytest.mark.asyncio
async def test_refinement_derive_without_body_stays_on_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = object()
    seen: dict[str, Any] = {}

    async def execute(_self: Any, command: Any, **kwargs: Any) -> Any:
        seen["command"] = command
        seen["uow"] = kwargs["uow"]
        return SimpleNamespace(spec=spec, knowledge_mutation=None)

    def unexpected_factory(_request: Any) -> Any:
        raise AssertionError("v1 must not resolve the bounded-retry factory")

    monkeypatch.setattr(
        refinements_api.DeriveSpecFromRefinementUseCase,
        "execute",
        execute,
    )
    monkeypatch.setattr(
        refinements_api,
        "get_unit_of_work_factory",
        unexpected_factory,
    )
    uow = object()

    result = await refinements_api.derive_spec(
        "ref-1",
        request=object(),  # type: ignore[arg-type]
        data=None,
        user_id="user-1",
        uow=uow,  # type: ignore[arg-type]
    )

    assert result is spec
    assert seen["uow"] is uow
    assert seen["command"].knowledge_propagation is None


@pytest.mark.asyncio
async def test_refinement_derive_rejects_explicit_json_null_before_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_execute(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("explicit null must not enter v1 or create a target")

    monkeypatch.setattr(
        refinements_api.DeriveSpecFromRefinementUseCase,
        "execute",
        unexpected_execute,
    )

    response = await refinements_api.derive_spec(
        "ref-1",
        request=_RequestWithBody(b" \n null \t"),  # type: ignore[arg-type]
        data=None,
        user_id="user-1",
        uow=object(),  # type: ignore[arg-type]
    )

    assert response.status_code == 422
    assert b'"code":"knowledge_propagation_envelope_required"' in response.body


@pytest.mark.asyncio
async def test_refinement_derive_v2_uses_bounded_retry_and_receipt_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation = object()
    projected = {"spec_id": "spec-v2"}
    factory = object()
    seen: dict[str, Any] = {}

    async def execute(_self: Any, command: Any, **kwargs: Any) -> Any:
        seen["command"] = command
        return SimpleNamespace(spec=None, knowledge_mutation=mutation)

    async def retry(**kwargs: Any) -> Any:
        seen["factory"] = kwargs["uow_factory"]
        return await kwargs["operation"](kwargs["uow"])

    monkeypatch.setattr(
        refinements_api.DeriveSpecFromRefinementUseCase,
        "execute",
        execute,
    )
    monkeypatch.setattr(
        refinements_api,
        "execute_knowledge_creation_with_one_retry",
        retry,
    )
    monkeypatch.setattr(
        refinements_api,
        "get_unit_of_work_factory",
        lambda _request: factory,
    )
    monkeypatch.setattr(
        refinements_api,
        "project_derive_spec_response",
        lambda value: projected if value is mutation else None,
    )
    data = DeriveSpecKnowledgeRequest(
        knowledge_propagation=_v2_envelope(),
    )

    result = await refinements_api.derive_spec(
        "ref-1",
        request=object(),  # type: ignore[arg-type]
        data=data,
        user_id="user-1",
        uow=object(),  # type: ignore[arg-type]
    )

    assert result is projected
    assert seen["factory"] is factory
    assert seen["command"].knowledge_propagation is data.knowledge_propagation


@pytest.mark.asyncio
async def test_refinement_derive_rejects_legacy_and_v2_before_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_execute(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a conflicting request must not create a target")

    monkeypatch.setattr(
        refinements_api.DeriveSpecFromRefinementUseCase,
        "execute",
        unexpected_execute,
    )
    data = DeriveSpecKnowledgeRequest(
        knowledge_propagation=_v2_envelope(),
        kb_ids=["kb-legacy"],
    )

    response = await refinements_api.derive_spec(
        "ref-1",
        request=object(),  # type: ignore[arg-type]
        data=data,
        user_id="user-1",
        uow=object(),  # type: ignore[arg-type]
    )

    assert response.status_code == 422
    assert b'"code":"conflicting_propagation_parameters"' in response.body


@pytest.mark.asyncio
async def test_board_card_create_without_envelope_stays_on_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = object()

    async def execute(_self: Any, command: Any, **_kwargs: Any) -> Any:
        assert command.data.knowledge_propagation is None
        return SimpleNamespace(card=card, knowledge_mutation=None)

    monkeypatch.setattr(
        boards_api.CreateCardInBoardUseCase,
        "execute",
        execute,
    )
    monkeypatch.setattr(
        boards_api,
        "get_unit_of_work_factory",
        lambda _request: (_ for _ in ()).throw(
            AssertionError("v1 must not resolve the bounded-retry factory")
        ),
    )

    result = await boards_api.create_card(
        "board-1",
        request=object(),  # type: ignore[arg-type]
        data=CardCreate(title="legacy"),
        user_id="user-1",
        realm_id=None,
        uow=object(),  # type: ignore[arg-type]
    )

    assert result is card


@pytest.mark.asyncio
async def test_board_card_create_rejects_explicit_null_field_before_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_execute(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("explicit null must not enter v1 or create a target")

    monkeypatch.setattr(
        boards_api.CreateCardInBoardUseCase,
        "execute",
        unexpected_execute,
    )
    data = CardCreate(title="invalid null", knowledge_propagation=None)
    assert "knowledge_propagation" in data.model_fields_set

    response = await boards_api.create_card(
        "board-1",
        request=object(),  # type: ignore[arg-type]
        data=data,
        user_id="user-1",
        realm_id=None,
        uow=object(),  # type: ignore[arg-type]
    )

    assert response.status_code == 422
    assert b'"code":"knowledge_propagation_envelope_required"' in response.body


@pytest.mark.asyncio
async def test_card_assignment_put_dispatches_and_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation = object()
    projected = {"operation_id": "op-1"}

    async def execute(_self: Any, command: Any, **_kwargs: Any) -> Any:
        assert command.card_id == "card-1"
        return mutation

    monkeypatch.setattr(
        cards_api.ReplaceCardKnowledgeAssignmentsUseCase,
        "execute",
        execute,
    )
    monkeypatch.setattr(
        cards_api,
        "project_knowledge_mutation_response",
        lambda value: projected if value is mutation else None,
    )
    request = KnowledgeAssignmentReplaceRequest(
        contract_version=2,
        mode="reference",
        knowledge_ids=["kb-1"],
        justification="Required by AC-1",
        idempotency_key="idem-put",
        expected_revision=0,
    )

    result = await cards_api.replace_card_knowledge_assignments(
        "card-1",
        request,
        user_id="user-1",
        uow=object(),  # type: ignore[arg-type]
    )

    assert result is projected


@pytest.mark.asyncio
async def test_card_assignment_get_has_stable_not_found_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(_self: Any, _command: Any, **_kwargs: Any) -> Any:
        raise EntityNotFoundError("card", "missing")

    monkeypatch.setattr(
        cards_api.GetCardKnowledgePropagationUseCase,
        "execute",
        execute,
    )

    response = await cards_api.get_card_knowledge_assignments(
        "missing",
        user_id="user-1",
        uow=object(),  # type: ignore[arg-type]
    )

    assert response.status_code == 404
    assert b'"code":"card_not_found"' in response.body
