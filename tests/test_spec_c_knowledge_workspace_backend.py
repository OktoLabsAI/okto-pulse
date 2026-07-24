"""Community backend evidence for the ResourceLineage.v2 Knowledge Workspace."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException

from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
    CommunitySqlAlchemyResourceGateAdapter,
)
from okto_pulse.community.api import resource_gate as resource_gate_api
from okto_pulse.community.api import traceability as traceability_api
from okto_pulse.core.application.knowledge_workspace import (
    KnowledgeWorkspaceProjectionError,
    KnowledgeWorkspaceProjector,
)
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgeAssignment,
    KnowledgeAssignmentState,
    KnowledgeOriginClass,
    KnowledgePropagationMode,
    KnowledgeRelevanceEntityType,
    KnowledgeRelevanceLink,
    KnowledgeTargetType,
)
from okto_pulse.core.domain.resource_revision import ResourceRevisionStamp
from okto_pulse.core.services.knowledge_propagation import (
    ResolvedKnowledgeAssignment,
)
from okto_pulse.core.services.resource_lineage import LineageEntityRef


def test_effective_resources_openapi_exposes_bounded_page_parameters() -> None:
    app = FastAPI()
    app.include_router(resource_gate_api.router, prefix="/api/v1")

    operation = app.openapi()["paths"][
        "/api/v1/resource-gate/{entity_type}/{entity_id}/effective-resources"
    ]["get"]
    parameters = {
        parameter["name"]: parameter for parameter in operation["parameters"]
    }

    assert {"board_id", "profile", "cursor", "limit"} <= set(parameters)
    assert parameters["profile"]["required"] is False
    assert "default" not in parameters["profile"]["schema"]
    assert parameters["cursor"]["required"] is False
    assert parameters["limit"]["required"] is False


def test_resource_lineage_dict_keeps_structured_relevance_and_lazy_detail_body() -> None:
    projection = {
        "board_id": "board-1",
        "entity_type": "spec",
        "entity_id": "spec-1",
        "resources": {
            "architecture": [],
            "mockup": [],
            "knowledge_base": [],
        },
        "resource_lineage": {
            "counts": {
                "unique_effective_count": 1,
                "raw_attachment_count": 2,
            },
            "attachments": [
                {
                    "resource_type": "knowledge_base",
                    "resource_id": "kb-physical-1",
                    "title": "Canonical KB",
                    "unique_resource_id": "knowledge_base:kb-root",
                    "attachment_kind": "inherited_reference",
                    "source_entity_type": "refinement",
                    "source_entity_id": "refinement-1",
                    "effective": True,
                    "inherited": True,
                    "revision_stamp": {
                        "root_id": "kb-root",
                        "immediate_parent_id": "kb-physical-1",
                        "source_revision": "4",
                        "source_content_sha256": "b" * 64,
                    },
                    "raw": {
                        "content": "Lazy body from the Community adapter",
                        "relevance_links": [
                            {
                                "entity_type": "functional_requirement",
                                "entity_id": "fr-1",
                            }
                        ],
                    },
                },
                {
                    "resource_type": "knowledge_base",
                    "resource_id": "kb-physical-2",
                    "title": "Canonical KB duplicate",
                    "unique_resource_id": "knowledge_base:kb-root",
                    "attachment_kind": "inherited_reference",
                    "source_entity_type": "ideation",
                    "source_entity_id": "ideation-1",
                    "effective": True,
                    "inherited": True,
                    "revision_stamp": {
                        "root_id": "kb-root",
                        "immediate_parent_id": "kb-physical-2",
                        "source_revision": "4",
                        "source_content_sha256": "b" * 64,
                    },
                    "raw": {},
                },
            ],
        },
    }

    summary = KnowledgeWorkspaceProjector.project(
        projection,
        profile="summary",
    )

    assert summary["workspace_item_count"] == 1
    assert summary["raw_attachment_count"] == 2
    assert summary["items"][0]["versioned_projection_id"] == (
        "knowledge_base:kb-root@4"
    )
    assert summary["items"][0]["relevance_links"] == [
        {
            "entity_type": "functional_requirement",
            "entity_id": "fr-1",
        }
    ]
    assert "raw" not in summary["items"][0]
    assert "_workspace_raw" not in summary["items"][0]
    assert summary["items"][0]["body_omitted_reason"] == "profile_summary"

    detail = KnowledgeWorkspaceProjector.project(
        projection,
        profile="detail",
        cursor=summary["items"][0]["detail_cursor"],
    )

    assert detail["count"] == 1
    assert detail["items"][0]["body"] == {
        "content": "Lazy body from the Community adapter"
    }


@pytest.mark.asyncio
async def test_effective_resources_route_forwards_workspace_page_and_logs_no_content(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: dict[str, object] = {}
    payload = {
        "contract_version": 2,
        "board_id": "board-1",
        "entity_type": "spec",
        "entity_id": "spec-1",
        "profile": "detail",
        "items": [
            {
                "title": "TOP-SECRET-TITLE",
                "body": "TOP-SECRET-BODY",
                "relevance_links": [
                    {
                        "entity_type": "acceptance_criterion",
                        "entity_id": "TOP-SECRET-RELEVANCE",
                    }
                ],
            }
        ],
        "count": 1,
        "next_cursor": "opaque",
        "truncated": True,
        "unique_effective_count": 4,
        "raw_attachment_count": 12,
        "workspace_item_count": 7,
        "response_bytes": 2048,
    }

    class _UseCase:
        async def execute(self, command, *, actor, uow):
            captured["command"] = command
            captured["actor"] = actor
            captured["uow"] = uow
            return SimpleNamespace(data=payload)

    monkeypatch.setattr(
        resource_gate_api,
        "GetEffectiveResourcesUseCase",
        _UseCase,
    )
    caplog.set_level(
        logging.INFO,
        logger="okto_pulse.community.knowledge_workspace",
    )

    result = await resource_gate_api.get_effective_resources(
        "spec",
        "spec-1",
        board_id="board-1",
        profile="detail",
        cursor="opaque-in",
        limit=1,
        user_id="user-1",
        realm_id=None,
        db=object(),
    )

    command = captured["command"]
    assert command.profile == "detail"
    assert command.cursor == "opaque-in"
    assert command.limit == 1
    assert result is payload

    emitted = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "okto_pulse.community.knowledge_workspace"
    )
    assert "profile=detail" in emitted
    assert "count=1" in emitted
    assert "response_bytes=2048" in emitted
    assert "TOP-SECRET-TITLE" not in emitted
    assert "TOP-SECRET-BODY" not in emitted
    assert "TOP-SECRET-RELEVANCE" not in emitted


@pytest.mark.asyncio
async def test_omitted_profile_preserves_legacy_rolling_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    legacy = {
        "board_id": "board-1",
        "entity_type": "card",
        "entity_id": "card-1",
        "resources": {"architecture": [], "mockup": [], "knowledge_base": []},
        "lineage_counts": {
            "unique_effective_count": 0,
            "raw_attachment_count": 0,
        },
    }

    class _UseCase:
        async def execute(self, command, *, actor, uow):
            captured["command"] = command
            return SimpleNamespace(data=legacy)

    monkeypatch.setattr(
        resource_gate_api,
        "GetEffectiveResourcesUseCase",
        _UseCase,
    )

    result = await resource_gate_api.get_effective_resources(
        "card",
        "card-1",
        board_id="board-1",
        profile=None,
        cursor=None,
        limit=None,
        user_id="user-1",
        realm_id=None,
        db=object(),
    )

    assert captured["command"].profile == "legacy"
    assert result["resources"]["knowledge_base"] == []
    assert "items" not in result


@pytest.mark.asyncio
async def test_workspace_projection_error_keeps_stable_http_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UseCase:
        async def execute(self, command, *, actor, uow):
            raise KnowledgeWorkspaceProjectionError(
                "knowledge_workspace_invalid_cursor",
                "The cursor is invalid.",
                details={"cursor_version": "unsupported"},
            )

    monkeypatch.setattr(
        resource_gate_api,
        "GetEffectiveResourcesUseCase",
        _UseCase,
    )

    with pytest.raises(HTTPException) as raised:
        await resource_gate_api.get_effective_resources(
            "spec",
            "spec-1",
            board_id="board-1",
            profile="summary",
            cursor="bad",
            limit=25,
            user_id="user-1",
            realm_id=None,
            db=object(),
        )

    assert raised.value.status_code == 422
    assert raised.value.detail == {
        "error": "knowledge_workspace_invalid_cursor",
        "code": "knowledge_workspace_invalid_cursor",
        "message": "The cursor is invalid.",
        "status_code": 422,
        "details": {"cursor_version": "unsupported"},
    }


@pytest.mark.asyncio
async def test_lineage_graph_exposes_selected_entity_logical_and_physical_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _LineageUseCase:
        async def execute(self, command, *, actor, uow):
            return SimpleNamespace(
                data={
                    "board_id": "board-1",
                    "nodes": [
                        {
                            "entity_type": "task",
                            "entity_id": "card-1",
                            "title": "Card",
                        }
                    ],
                }
            )

    class _WorkspaceUseCase:
        async def execute(self, command, *, actor, uow):
            captured["command"] = command
            return SimpleNamespace(
                data={
                    "unique_effective_count": 2,
                    "unique_root_version_count": 3,
                    "raw_attachment_count": 9,
                    "workspace_item_count": 3,
                }
            )

    monkeypatch.setattr(
        traceability_api,
        "GetLineageGraphUseCase",
        _LineageUseCase,
    )
    monkeypatch.setattr(
        traceability_api,
        "GetEffectiveResourcesUseCase",
        _WorkspaceUseCase,
    )

    result = await traceability_api.get_lineage_graph(
        "board-1",
        entity_type="task",
        entity_id="card-1",
        include_artifacts=False,
        user_id="user-1",
        uow=object(),
    )

    command = captured["command"]
    assert command.entity_type == "card"
    assert command.profile == "summary"
    assert command.limit == 1
    assert result["resource_counts"] == {
        "unique_effective_count": 2,
        "unique_root_version_count": 3,
        "raw_attachment_count": 9,
        "workspace_item_count": 3,
    }
    assert result["nodes"][0]["resource_counts"] == result["resource_counts"]


def test_assignment_projection_uses_only_structured_v2_relevance_links() -> None:
    link = KnowledgeRelevanceLink(
        entity_type=KnowledgeRelevanceEntityType.ACCEPTANCE_CRITERION,
        entity_id="ac-1",
    )
    assignment = KnowledgeAssignment(
        assignment_id="assignment-1",
        board_id="board-1",
        target_type=KnowledgeTargetType.CARD,
        target_id="card-1",
        source_knowledge_id="kb-1",
        revision_stamp=ResourceRevisionStamp(
            root_id="kb-root",
            immediate_parent_id="kb-1",
            source_revision="7",
            source_content_sha256="a" * 64,
        ),
        mode=KnowledgePropagationMode.REFERENCE,
        state=KnowledgeAssignmentState.ACTIVE,
        origin_class=KnowledgeOriginClass.V2,
        actor_id="user-1",
        revision=3,
        justification="Selected by the user",
        relevance_links=(link,),
    )
    resolved = ResolvedKnowledgeAssignment(
        assignment=assignment,
        state=KnowledgeAssignmentState.ACTIVE,
        effective=True,
        revision_stamp=assignment.revision_stamp,
        content_bytes=b'{"id":"kb-1","title":"AC-like words are not parsed"}',
        resolved_source_knowledge_id="kb-1",
    )
    adapter = CommunitySqlAlchemyResourceGateAdapter(db=object())
    root = LineageEntityRef(
        entity_type="card",
        entity_id="card-1",
        title="Card",
        entity=SimpleNamespace(board_id="board-1", spec_id="spec-1"),
    )
    parent = LineageEntityRef(
        entity_type="spec",
        entity_id="spec-1",
        title="Spec",
        entity=SimpleNamespace(board_id="board-1"),
    )

    projected = adapter._assignment_ref(
        root=root,
        parent=parent,
        item=resolved,
        base={"id": "kb-1"},
    )

    assert projected["relevance_links"] == [
        {
            "entity_type": "acceptance_criterion",
            "entity_id": "ac-1",
        }
    ]
    assert projected["knowledge_assignment_revision"] == 3
    assert projected["knowledge_assignment_origin_class"] == "v2"
