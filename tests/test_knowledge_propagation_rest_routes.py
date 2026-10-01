from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI

from okto_pulse.community.api import boards as boards_api
from okto_pulse.community.api import cards as cards_api
from okto_pulse.community.api import refinements as refinements_api
from okto_pulse.community.api import ideations as ideations_api
from okto_pulse.core.application.use_cases import EntityNotFoundError
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgeAssignmentState,
    KnowledgeOriginClass,
    KnowledgePropagationMode,
    KnowledgeSelectionState,
)
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.models import CardCreate
from okto_pulse.core.models.knowledge_propagation import (
    DeriveIdeationSpecRequest,
    DeriveSpecKnowledgeRequest,
    KnowledgeAssignmentReplaceRequest,
)
from okto_pulse.core.ports.authentication import Principal


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
    app.include_router(ideations_api.router, prefix="/api/v1")
    app.include_router(boards_api.router, prefix="/api/v1/boards")
    app.include_router(cards_api.router, prefix="/api/v1/cards")

    paths = app.openapi()["paths"]
    assert "/api/v1/refinements/{refinement_id}/derive-spec" in paths
    assert "/api/v1/ideations/{ideation_id}/derive-spec" in paths
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
async def test_ideation_derive_uses_native_receipt_and_preserves_delivery_context(monkeypatch):
    mutation = object()
    factory = object()
    data = DeriveIdeationSpecRequest(delivery_context="greenfield")
    async def execute(_self, command, **kwargs):
        assert command.ideation_id == "ideation-1"
        assert command.data is data
        assert command.data.knowledge_propagation.selection_state is KnowledgeSelectionState.OMITTED
        return SimpleNamespace(knowledge_mutation=mutation)
    async def retry(**kwargs):
        assert kwargs["uow_factory"] is factory
        return await kwargs["operation"](kwargs["uow"])
    monkeypatch.setattr(ideations_api.DeriveSpecUseCase, "execute", execute)
    monkeypatch.setattr(ideations_api, "get_unit_of_work_factory", lambda request: factory)
    monkeypatch.setattr(ideations_api, "execute_knowledge_creation_with_one_retry", retry)
    monkeypatch.setattr(ideations_api, "project_derive_spec_response", lambda value: value)
    assert await ideations_api.derive_spec(
        "ideation-1", request=object(), data=data, user_id="user-1", uow=object(),
    ) is mutation
    with pytest.raises(ValueError, match="delivery_context"):
        DeriveIdeationSpecRequest()


@pytest.mark.asyncio
async def test_refinement_derive_without_body_uses_native_omitted_selection(monkeypatch):
    mutation = object()
    factory = object()
    async def execute(_self, command, **kwargs):
        assert command.knowledge_propagation.selection_state is KnowledgeSelectionState.OMITTED
        assert command.knowledge_propagation.knowledge_ids == []
        return SimpleNamespace(knowledge_mutation=mutation)
    async def retry(**kwargs):
        assert kwargs["uow_factory"] is factory
        return await kwargs["operation"](kwargs["uow"])
    monkeypatch.setattr(refinements_api.DeriveSpecFromRefinementUseCase, "execute", execute)
    monkeypatch.setattr(refinements_api, "get_unit_of_work_factory", lambda request: factory)
    monkeypatch.setattr(refinements_api, "execute_knowledge_creation_with_one_retry", retry)
    monkeypatch.setattr(refinements_api, "project_derive_spec_response", lambda value: value)
    result = await refinements_api.derive_spec(
        "ref-1", request=object(), data=None, user_id="user-1", uow=object(),
    )
    assert result is mutation


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


def test_refinement_request_rejects_removed_selector():
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        DeriveSpecKnowledgeRequest(knowledge_propagation=_v2_envelope(), kb_ids=["kb-old"])


@pytest.mark.asyncio
async def test_board_card_create_without_envelope_uses_native_omitted_selection(monkeypatch):
    mutation = object()
    factory = object()
    async def execute(_self, command, **kwargs):
        assert command.data.knowledge_propagation.selection_state is KnowledgeSelectionState.OMITTED
        assert command.data.knowledge_propagation.knowledge_ids == []
        return SimpleNamespace(knowledge_mutation=mutation)
    async def retry(**kwargs):
        assert kwargs["uow_factory"] is factory
        return await kwargs["operation"](kwargs["uow"])
    monkeypatch.setattr(boards_api.CreateCardInBoardUseCase, "execute", execute)
    monkeypatch.setattr(boards_api, "get_unit_of_work_factory", lambda request: factory)
    monkeypatch.setattr(boards_api, "execute_knowledge_creation_with_one_retry", retry)
    monkeypatch.setattr(boards_api, "project_card_create_response", lambda value: value)
    result = await boards_api.create_card(
        "board-1", request=object(), data=CardCreate(title="Native omitted"),
        principal=Principal("user-1", realm_id=LOCAL_REALM_ID, actor_kind="human"),
        uow=object(),
    )
    assert result is mutation


def test_card_create_request_rejects_explicit_null():
    with pytest.raises(ValueError, match="knowledge_propagation"):
        CardCreate(title="invalid null", knowledge_propagation=None)


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


@pytest.mark.asyncio
async def test_card_assignment_get_projects_current_v2_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment = SimpleNamespace(
        revision_stamp=SimpleNamespace(root_id="root-kb-1"),
        mode=KnowledgePropagationMode.REFERENCE,
        origin_class=KnowledgeOriginClass.V2,
    )

    class _ReadResult:
        scope_revision = 7
        selection_state = KnowledgeSelectionState.EXPLICIT_IDS
        resolved_assignments = (
            SimpleNamespace(
                assignment=assignment,
                state=KnowledgeAssignmentState.ACTIVE,
            ),
        )

        @staticmethod
        def to_dict() -> dict[str, Any]:
            return {
                "target": {
                    "board_id": "board-1",
                    "target_type": "card",
                    "target_id": "card-1",
                },
                "scope_revision": 7,
                "selection_state": KnowledgeSelectionState.EXPLICIT_IDS,
                "resolved_assignments": [],
                "effective_assignment_ids": ["assignment-1"],
                "effective_local_attachments": [],
                "history_assignments": [],
                "tombstones": [],
                "snapshots": [],
                "effective_count": 1,
            }

    async def execute(_self: Any, command: Any, **_kwargs: Any) -> Any:
        assert command.card_id == "card-1"
        return SimpleNamespace(read_result=_ReadResult())

    monkeypatch.setattr(
        cards_api.GetCardKnowledgePropagationUseCase,
        "execute",
        execute,
    )

    response = await cards_api.get_card_knowledge_assignments(
        "card-1",
        user_id="user-1",
        uow=object(),  # type: ignore[arg-type]
    )

    assert response.model_dump(mode="json") == {
        "contract_version": 2,
        "revision": 7,
        "selection_state": "explicit_ids",
        "assignments": [
            {
                "root_knowledge_id": "root-kb-1",
                "mode": "reference",
                "origin_class": "v2",
                "state": "active",
                "stale": False,
            }
        ],
    }
