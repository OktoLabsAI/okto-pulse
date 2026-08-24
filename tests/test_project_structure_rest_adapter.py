"""Dedicated Project structure REST contracts delegate to Core use cases."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from okto_pulse.community.api import specs


class _Dump:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "json"
        return self.payload


@pytest.mark.asyncio
async def test_spec_snapshot_get_uses_core_boundary(monkeypatch) -> None:
    expected = {
        "contract_version": "project-structure/v1",
        "state": "not_authored",
        "spec_id": "spec-rest",
        "spec_version": 3,
        "authored": False,
        "structure_revision": 0,
        "digest": None,
        "nodes": [],
    }

    class UseCase:
        async def execute(self, command, *, actor, uow):
            assert command.board_id == "board-rest"
            assert command.spec_id == "spec-rest"
            assert actor.board_id == "board-rest"
            assert uow is marker
            return SimpleNamespace(structure=_Dump(expected))

    marker = object()
    monkeypatch.setattr(specs, "GetProjectStructureUseCase", UseCase)

    assert await specs.get_project_structure(
        "board-rest", "spec-rest", user_id="user-rest", uow=marker
    ) == expected


@pytest.mark.asyncio
async def test_patch_maps_ui_batch_and_returns_receipted_nodes(monkeypatch) -> None:
    captured = {}
    node = {
        "id": "psn_root",
        "parent_id": None,
        "position": 0,
        "kind": "folder",
        "name": "src",
        "note": "Root",
        "classification": "as_is",
        "state": "existing",
        "interpretation_limit": None,
        "status": "active",
        "task_references": [],
        "test_references": [],
        "evidence_ids": [],
    }

    class UseCase:
        async def execute(self, command, *, actor, uow):
            captured["command"] = command
            return SimpleNamespace(
                structured_result=SimpleNamespace(
                    success=True,
                    replayed=False,
                    spec_version=5,
                    structure_revision=2,
                    entity_ids=["psn_root"],
                    details={"nodes": [node]},
                    error_code=None,
                    error_message=None,
                    impact_report=None,
                )
            )

    monkeypatch.setattr(specs, "MutateProjectStructureUseCase", UseCase)
    request = specs.ProjectStructureBatchMutationRequest.model_validate(
        {
            "expected_spec_version": 4,
            "expected_structure_revision": 1,
            "idempotency_key": "rest-key-1",
            "operations": [
                {
                    "operation": "create",
                    "entity_id": "psn_root",
                    "payload": {
                        key: value for key, value in node.items() if key != "id"
                    },
                }
            ],
        }
    )

    response = await specs.mutate_project_structure(
        "board-rest",
        "spec-rest",
        request,
        user_id="user-rest",
        uow=object(),
    )

    command = captured["command"]
    assert command.expected_spec_version == 4
    assert command.expected_structure_revision == 1
    assert command.idempotency_key == "rest-key-1"
    assert command.operations[0]["payload"]["id"] == "psn_root"
    assert response == {
        "replayed": False,
        "spec_version": 5,
        "structure_revision": 2,
        "affected_node_ids": ["psn_root"],
        "nodes": [node],
    }


@pytest.mark.asyncio
async def test_folder_not_empty_preserves_conflict_and_impact(monkeypatch) -> None:
    impact = {"node_id": "psn_root", "descendant_count": 2}

    class UseCase:
        async def execute(self, command, *, actor, uow):
            return SimpleNamespace(
                structured_result=SimpleNamespace(
                    success=False,
                    error_code="project_structure_folder_not_empty",
                    error_message="Folder still has descendants.",
                    details={"reason": "children_active"},
                    impact_report=impact,
                )
            )

    monkeypatch.setattr(specs, "MutateProjectStructureUseCase", UseCase)
    request = specs.ProjectStructureBatchMutationRequest.model_validate(
        {
            "expected_spec_version": 4,
            "expected_structure_revision": 1,
            "idempotency_key": "rest-key-remove",
            "operations": [
                {"operation": "remove", "node_id": "psn_root"}
            ],
        }
    )

    with pytest.raises(HTTPException) as captured:
        await specs.mutate_project_structure(
            "board-rest",
            "spec-rest",
            request,
            user_id="user-rest",
            uow=object(),
        )

    assert captured.value.status_code == 409
    assert captured.value.detail["error"] == "project_structure_folder_not_empty"
    assert captured.value.detail["impact_report"] == impact


def test_relation_status_conflict_is_a_stable_public_conflict() -> None:
    response = specs._project_structure_error(
        error_code=(
            specs.StructuredSpecEntityErrorCode.PROJECT_STRUCTURE_RELATION_STATUS_CONFLICT
        ),
        message="Links can only be edited after structural authoring is locked.",
        details={"operation": "link_task"},
    )

    assert response.status_code == 409
    assert (
        response.detail["error"]
        == "project_structure_relation_status_conflict"
    )
    assert response.detail["details"] == {"operation": "link_task"}


@pytest.mark.asyncio
async def test_card_projection_returns_core_contract(monkeypatch) -> None:
    projection = {
        "contract_version": "project-structure/v1",
        "state": "projected",
        "spec_id": "spec-rest",
        "spec_version": 5,
        "authored": True,
        "structure_revision": 2,
        "digest": "a" * 64,
        "reference_type": "task",
        "reference_id": "card-rest",
        "nodes": [],
        "affected_references": [],
    }

    class UseCase:
        async def execute(self, command, *, actor, uow):
            assert command.board_id == "board-rest"
            assert command.card_id == "card-rest"
            return SimpleNamespace(projection=_Dump(projection))

    monkeypatch.setattr(
        specs,
        "GetCardProjectStructureProjectionUseCase",
        UseCase,
    )

    assert await specs.get_card_project_structure(
        "board-rest",
        "card-rest",
        user_id="user-rest",
        uow=object(),
    ) == projection


def test_no_standalone_project_structure_export_route_exists() -> None:
    paths = {route.path for route in specs.router.routes}

    assert "/boards/{board_id}/specs/{spec_id}/project-structure" in paths
    assert "/boards/{board_id}/cards/{card_id}/project-structure" in paths
    assert all("project-structure/export" not in path for path in paths)
