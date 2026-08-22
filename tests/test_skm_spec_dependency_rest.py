"""SK-M REST request, projection and lifecycle-conflict contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from okto_pulse.community.api import allowed_transitions, boards, specs
from okto_pulse.community.api.auth_deps import get_realm_id, require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.domain.enums import SpecStatus
from okto_pulse.core.application.use_cases.allowed_transitions import (
    AllowedTransition,
    AllowedTransitionsReadModel,
)
from okto_pulse.core.application.use_cases.base import (
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.domain.spec_dependency import (
    SPEC_DEPENDENCY_CURSOR_MAX_LENGTH,
    SPEC_DEPENDENCY_REMOVAL_REASON_MAX_LENGTH,
    SpecDependencyBlocker,
    SpecDependencyCapabilities,
    SpecDependencyDirection,
    SpecDependencyListItem,
    SpecDependencyMutationReceipt,
    SpecDependencyOperationError,
    SpecDependencyPage,
    SpecDependencyReadiness,
    SpecDependencyRecord,
    SpecDependencySpecSnapshot,
)
from okto_pulse.core.ports.authentication import Principal


def _snapshot(
    spec_id: str,
    *,
    status: SpecStatus = SpecStatus.REVIEW,
    version: int = 4,
    archived: bool = False,
) -> SpecDependencySpecSnapshot:
    return SpecDependencySpecSnapshot(
        id=spec_id,
        board_id="board-1",
        title=f"Spec {spec_id}",
        status=status,
        edition=2,
        version=version,
        archived=archived,
    )


def _record(*, removed: bool = False) -> SpecDependencyRecord:
    now = datetime.now(timezone.utc)
    return SpecDependencyRecord(
        id="dependency-1",
        board_id="board-1",
        source_spec_id="source",
        target_spec_id="target",
        created_at=now,
        created_by="user-1",
        created_by_type="user",
        created_by_name="User",
        source_version_on_create=4,
        source_status_on_create=SpecStatus.REVIEW,
        target_status_on_create=SpecStatus.DONE,
        target_version_on_create=8,
        resolved_on_create=True,
        add_idempotency_key="add-key",
        removed_at=now if removed else None,
        removed_by="user-1" if removed else None,
        removed_by_type="user" if removed else None,
        removed_by_name="User" if removed else None,
        removal_reason="No longer required" if removed else None,
        source_version_on_remove=5 if removed else None,
        remove_idempotency_key="remove-key" if removed else None,
    )


def _app(uow_dependency) -> FastAPI:
    app = FastAPI()
    app.include_router(specs.router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "user-1"
    app.dependency_overrides[get_unit_of_work] = uow_dependency
    return app


def test_dependency_capability_schema_accepts_permission_denied_reason() -> None:
    capability = specs.SpecDependencyCapabilitiesResponse.model_validate(
        {
            "can_remove": False,
            "remove_reason_code": "permission_denied",
            "can_navigate": True,
        }
    )

    assert capability.can_remove is False
    assert capability.remove_reason_code == "permission_denied"


def test_malformed_dependency_write_is_rejected_before_uow_resolution() -> None:
    calls = 0

    def uow():
        nonlocal calls
        calls += 1
        return object()

    with TestClient(_app(uow), raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/boards/board-1/specs/source/dependencies",
            json={
                "prerequisite_spec_id": "target",
                "expected_spec_version": 3,
                "expected_spec_edition": 2,
                "idempotency_key": "key",
                "unexpected": True,
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_spec_dependency_request"
    assert calls == 0


@pytest.mark.parametrize(
    ("method", "path", "body"),
    (
        (
            "POST",
            "/api/v1/boards/board-1/specs/source/dependencies",
            {
                "prerequisite_spec_id": "target",
                "expected_spec_version": 3,
                "idempotency_key": "add-key",
            },
        ),
        (
            "DELETE",
            "/api/v1/boards/board-1/specs/source/dependencies/dependency-1",
            {
                "reason": "No longer required",
                "expected_spec_version": 3,
                "idempotency_key": "remove-key",
            },
        ),
    ),
)
def test_dependency_writes_require_expected_spec_edition_before_uow_resolution(
    method: str,
    path: str,
    body: dict[str, object],
) -> None:
    calls = 0

    def uow():
        nonlocal calls
        calls += 1
        return object()

    with TestClient(_app(uow), raise_server_exceptions=False) as client:
        response = client.request(method, path, json=body)

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_spec_dependency_request"
    assert calls == 0


@pytest.mark.parametrize(
    "query",
    (
        "direction=sideways",
        "limit=0",
        "limit=101",
        "limit=not-an-integer",
        "active_state=pending",
        "satisfaction=pending",
        "retrospective=maybe",
        "related_status=pending",
        "lineage=unknown",
        "unexpected=true",
    ),
)
def test_dependency_list_query_validation_uses_closed_400_before_uow(
    query: str,
) -> None:
    calls = 0

    def uow():
        nonlocal calls
        calls += 1
        return object()

    with TestClient(_app(uow), raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/boards/board-1/specs/source/dependencies?" + query
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "invalid_spec_dependency_request",
            "message": "Spec dependency request is invalid.",
            "retryable": False,
        }
    }
    assert calls == 0


def test_allowed_transitions_rest_preserves_structured_dependency_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_model = AllowedTransitionsReadModel(
        board_id="board-1",
        entity_type="spec",
        entity_id="source",
        current_status="validated",
        allowed_transitions=[
            AllowedTransition(
                to_status="in_progress",
                label="Start implementation",
                gate="blocked",
                blocked_reason="spec_dependencies_incomplete: complete prerequisites",
                blocked_facts={
                    "spec_id": "source",
                    "blocking_count": 1,
                    "blocking_dependencies": [
                        {
                            "dependency_id": "dependency-1",
                            "target_spec_id": "target",
                            "target_title": "Archived Done",
                            "target_status": "done",
                            "target_archived": True,
                        }
                    ],
                },
            )
        ],
    )

    async def execute(_self, *_args, **_kwargs):
        return SimpleNamespace(read_model=read_model)

    monkeypatch.setattr(
        allowed_transitions.ListAllowedTransitionsUseCase,
        "execute",
        execute,
    )
    app = FastAPI()
    app.include_router(allowed_transitions.router, prefix="/api/v1")
    app.dependency_overrides[require_user] = lambda: "user-1"
    app.dependency_overrides[get_realm_id] = lambda: "local"
    app.dependency_overrides[get_unit_of_work] = lambda: object()

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/boards/board-1/allowed-transitions",
            params={"entity_type": "spec", "entity_id": "source"},
        )

    assert response.status_code == 200
    blocked = response.json()["allowed_transitions"][0]["blocked_facts"]
    assert blocked["blocking_count"] == 1
    assert blocked["blocking_dependencies"][0]["target_archived"] is True


def test_add_remove_and_list_project_closed_dependency_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added = _record()
    removed = _record(removed=True)
    add_receipt = SpecDependencyMutationReceipt(
        operation="add",
        dependency=added,
        source_spec=_snapshot("source", version=4),
        request_digest="a" * 64,
        satisfied=True,
    )
    remove_receipt = SpecDependencyMutationReceipt(
        operation="remove",
        dependency=removed,
        source_spec=_snapshot("source", version=5),
        request_digest="b" * 64,
        satisfied=False,
    )
    blocker = SpecDependencyBlocker(
        dependency_id="dependency-1",
        source_spec_id="source",
        target_spec_id="target",
        target_title="Spec target",
        target_status=SpecStatus.DONE,
        target_edition=2,
        target_version=8,
        target_archived=True,
    )
    page = SpecDependencyPage(
        items=(
            SpecDependencyListItem(
                dependency=added,
                direction=SpecDependencyDirection.OUTGOING,
                related_spec=_snapshot(
                    "target", status=SpecStatus.DONE, version=8, archived=True
                ),
                satisfied=False,
                retrospective=False,
                same_ideation=False,
                capabilities=SpecDependencyCapabilities(
                    can_remove=False,
                    can_navigate=False,
                    removal_blocked_reason="source_archived",
                ),
            ),
        ),
        total=1,
        next_cursor="next-page",
        readiness=SpecDependencyReadiness(
            spec_id="source",
            board_id="board-1",
            current_edition=2,
            last_started_edition=None,
            active_dependency_count=1,
            blocking_count=1,
            archived_blocking_count=1,
            unfinished_blocking_count=0,
            blockers_truncated=False,
            blockers=(blocker,),
        ),
    )

    mutation_commands: list[object] = []

    async def add_execute(_self, command, *_args, **_kwargs):
        mutation_commands.append(command)
        return SimpleNamespace(receipt=add_receipt)

    async def remove_execute(_self, command, *_args, **_kwargs):
        mutation_commands.append(command)
        return SimpleNamespace(receipt=remove_receipt)

    async def list_execute(_self, *_args, **_kwargs):
        return SimpleNamespace(page=page)

    async def unexpected_get_spec(*_args, **_kwargs):
        raise AssertionError("dependency writes must not pre-read through GetSpec")

    monkeypatch.setattr(specs.GetSpecUseCase, "execute", unexpected_get_spec)
    monkeypatch.setattr(specs.AddSpecDependencyUseCase, "execute", add_execute)
    monkeypatch.setattr(specs.RemoveSpecDependencyUseCase, "execute", remove_execute)
    monkeypatch.setattr(specs.ListSpecDependenciesUseCase, "execute", list_execute)

    with TestClient(_app(lambda: object())) as client:
        add_response = client.post(
            "/api/v1/boards/board-1/specs/source/dependencies",
            json={
                "prerequisite_spec_id": "target",
                "expected_spec_version": 3,
                "expected_spec_edition": 2,
                "idempotency_key": "add-key",
            },
        )
        remove_response = client.request(
            "DELETE",
            "/api/v1/boards/board-1/specs/source/dependencies/dependency-1",
            json={
                "reason": "No longer required",
                "expected_spec_version": 4,
                "expected_spec_edition": 2,
                "idempotency_key": "remove-key",
            },
        )
        list_response = client.get(
            "/api/v1/boards/board-1/specs/source/dependencies",
            params={
                "direction": "depends_on",
                "satisfaction": "unmet",
                "lineage": "cross_ideation",
            },
        )

    assert add_response.status_code == 201
    add_payload = add_response.json()
    assert add_payload["dependency"]["source_status_on_create"] == "review"
    assert add_payload["spec_version"] == 4
    assert add_payload["replayed"] is False

    assert remove_response.status_code == 200
    remove_payload = remove_response.json()
    assert remove_payload["dependency"]["removed_at_spec_version"] == 5
    assert remove_payload["dependency"]["removal_reason"] == "No longer required"
    # The target was Done when the edge was created, but not when removed.
    assert remove_payload["dependency"]["target_status_on_create"] == "done"
    assert remove_payload["dependency"]["satisfied"] is False
    assert [command.board_id for command in mutation_commands] == [
        "board-1",
        "board-1",
    ]

    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["items"][0]["satisfied"] is False
    assert list_payload["items"][0]["related_spec"]["archived"] is True
    assert list_payload["items"][0]["lineage"] == "cross_ideation"
    assert list_payload["items"][0]["capabilities"] == {
        "can_remove": False,
        "remove_reason_code": "source_archived",
        "can_navigate": False,
    }
    assert list_payload["readiness"]["can_start"] is False
    assert list_payload["readiness"]["archived_blocking_count"] == 1
    assert list_payload["readiness"]["unfinished_blocking_count"] == 0
    assert list_payload["readiness"]["blockers_truncated"] is False
    assert list_payload["readiness"]["blockers"][0]["target_archived"] is True
    assert list_payload["next_cursor"] == "next-page"


def test_dependency_writes_preserve_client_edition_fence_after_draft_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = SimpleNamespace(
        board_id="board-1",
        status=SpecStatus.VALIDATED,
        version=4,
        edition=2,
    )
    current = SimpleNamespace(
        board_id="board-1",
        status=SpecStatus.DRAFT,
        version=previous.version,
        edition=previous.edition + 1,
    )
    commands: list[object] = []

    async def reject_stale_edition(_self, command, *_args, **_kwargs):
        commands.append(command)
        if command.expected_spec_edition != previous.edition:
            raise AssertionError("REST replaced the client's stale edition fence")
        raise SpecDependencyOperationError(
            "spec_dependency_state_conflict",
            "Spec lifecycle edition changed after the dependency form was loaded.",
            remediation="refresh_spec",
            facts={
                "spec_id": "source",
                "expected_spec_edition": command.expected_spec_edition,
                "current_spec_edition": current.edition,
            },
        )

    monkeypatch.setattr(
        specs.AddSpecDependencyUseCase,
        "execute",
        reject_stale_edition,
    )
    monkeypatch.setattr(
        specs.RemoveSpecDependencyUseCase,
        "execute",
        reject_stale_edition,
    )

    with TestClient(_app(lambda: object()), raise_server_exceptions=False) as client:
        add_response = client.post(
            "/api/v1/boards/board-1/specs/source/dependencies",
            json={
                "prerequisite_spec_id": "target",
                "expected_spec_version": previous.version,
                "expected_spec_edition": previous.edition,
                "idempotency_key": "stale-add",
            },
        )
        remove_response = client.request(
            "DELETE",
            "/api/v1/boards/board-1/specs/source/dependencies/dependency-1",
            json={
                "reason": "No longer required",
                "expected_spec_version": previous.version,
                "expected_spec_edition": previous.edition,
                "idempotency_key": "stale-remove",
            },
        )

    assert current.version == previous.version
    assert current.edition != previous.edition
    assert [command.expected_spec_version for command in commands] == [4, 4]
    assert [command.expected_spec_edition for command in commands] == [2, 2]
    for response in (add_response, remove_response):
        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "spec_dependency_state_conflict",
            "message": (
                "The Spec lifecycle edition changed while processing the dependency."
            ),
            "retryable": False,
            "remediation": "refresh_spec",
            "facts": {
                "spec_id": "source",
                "expected_spec_edition": 2,
                "current_spec_edition": 3,
            },
        }


def test_dependency_routes_publish_closed_openapi_response_contracts() -> None:
    document = _app(lambda: object()).openapi()
    path = "/api/v1/boards/{board_id}/specs/{spec_id}/dependencies"
    item_path = f"{path}/{{dependency_id}}"

    expected_mutation_errors = {
        "400": "SpecDependencyBadRequestResponse",
        "403": "SpecDependencyForbiddenResponse",
        "404": "SpecDependencyNotFoundResponse",
        "409": "SpecDependencyConflictResponse",
    }
    for operation in (
        document["paths"][path]["post"],
        document["paths"][item_path]["delete"],
    ):
        assert "422" not in operation["responses"]
        for status_code, schema_name in expected_mutation_errors.items():
            assert operation["responses"][status_code]["content"][
                "application/json"
            ]["schema"] == {"$ref": f"#/components/schemas/{schema_name}"}

    assert document["paths"][path]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/SpecDependencyMutationResponse"}
    assert document["paths"][item_path]["delete"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/SpecDependencyMutationResponse"}
    assert document["paths"][path]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/SpecDependencyPageResponse"}
    assert document["paths"][path]["get"]["responses"]["400"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/SpecDependencyInvalidRequestResponse"}
    assert "422" not in document["paths"][path]["get"]["responses"]
    assert document["paths"][path]["get"]["responses"]["4XX"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/SpecDependencyListErrorResponse"}

    schemas = document["components"]["schemas"]
    assert "expected_spec_edition" in schemas["SpecDependencyAddRequest"]["required"]
    assert "expected_spec_edition" in schemas["SpecDependencyRemoveRequest"]["required"]
    assert schemas["SpecDependencyRemoveRequest"]["properties"]["reason"][
        "maxLength"
    ] == SPEC_DEPENDENCY_REMOVAL_REASON_MAX_LENGTH
    cursor_schema = next(
        parameter["schema"]
        for parameter in document["paths"][path]["get"]["parameters"]
        if parameter["name"] == "cursor" and parameter["in"] == "query"
    )
    cursor_string_schema = next(
        branch for branch in cursor_schema["anyOf"] if branch.get("type") == "string"
    )
    assert cursor_string_schema["maxLength"] == SPEC_DEPENDENCY_CURSOR_MAX_LENGTH
    for name in (
        "SpecDependencyBadRequestDetail",
        "SpecDependencyBadRequestResponse",
        "SpecDependencyForbiddenDetail",
        "SpecDependencyForbiddenResponse",
        "SpecDependencyNotFoundDetail",
        "SpecDependencyNotFoundResponse",
        "SpecDependencyConflictDetail",
        "SpecDependencyConflictResponse",
        "SpecDependencyRecordResponse",
        "SpecDependencyMutationResponse",
        "SpecDependencyRelatedSpecResponse",
        "SpecDependencyCapabilitiesResponse",
        "SpecDependencyListItemResponse",
        "SpecDependencyListBlockerResponse",
        "SpecDependencyListReadinessResponse",
        "SpecDependencyPageResponse",
        "SpecDependencyInvalidRequestDetail",
        "SpecDependencyInvalidRequestResponse",
        "SpecDependencyListErrorDetail",
        "SpecDependencyListErrorResponse",
    ):
        assert schemas[name]["additionalProperties"] is False


@pytest.mark.parametrize(
    ("method", "path", "body"),
    (
        (
            "POST",
            "/api/v1/boards/board-1/specs/source/dependencies",
            {
                "prerequisite_spec_id": "target",
                "expected_spec_version": 3,
                "expected_spec_edition": 2,
                "idempotency_key": "add-key",
            },
        ),
        (
            "DELETE",
            "/api/v1/boards/board-1/specs/source/dependencies/dependency-1",
            {
                "reason": "No longer required",
                "expected_spec_version": 3,
                "expected_spec_edition": 2,
                "idempotency_key": "remove-key",
            },
        ),
        (
            "GET",
            "/api/v1/boards/board-1/specs/source/dependencies",
            None,
        ),
    ),
)
def test_source_not_found_is_distinct_on_every_dependency_route(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    async def source_missing(_self, *_args, **_kwargs):
        raise EntityNotFoundError("spec", "source")

    monkeypatch.setattr(specs.AddSpecDependencyUseCase, "execute", source_missing)
    monkeypatch.setattr(specs.RemoveSpecDependencyUseCase, "execute", source_missing)
    monkeypatch.setattr(
        specs.ListSpecDependenciesUseCase,
        "execute",
        source_missing,
    )
    with TestClient(_app(lambda: object())) as client:
        response = client.request(method, path, json=body)

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "spec_not_found",
        "message": "Spec was not found in the requested board.",
        "retryable": False,
    }


def test_add_target_unavailable_is_non_disclosing_and_not_source_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def target_hidden(_self, *_args, **_kwargs):
        raise EntityNotFoundError("spec", "target")

    monkeypatch.setattr(specs.AddSpecDependencyUseCase, "execute", target_hidden)
    with TestClient(_app(lambda: object())) as client:
        response = client.post(
            "/api/v1/boards/board-1/specs/source/dependencies",
            json={
                "prerequisite_spec_id": "target",
                "expected_spec_version": 3,
                "expected_spec_edition": 2,
                "idempotency_key": "add-key",
            },
        )

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "dependency_target_unavailable",
        "message": "Dependency target is unavailable.",
        "retryable": False,
    }


def test_dependency_list_permission_denial_uses_fixed_non_disclosing_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "required_permission=secret.internal.permission"

    async def denied(_self, *_args, **_kwargs):
        raise PermissionDeniedError(secret)

    monkeypatch.setattr(specs.ListSpecDependenciesUseCase, "execute", denied)
    with TestClient(_app(lambda: object()), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/boards/board-1/specs/source/dependencies")

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "permission_denied",
        "message": "Permission denied for this Spec dependency operation.",
        "retryable": False,
    }
    assert secret not in response.text


@pytest.mark.asyncio
async def test_delete_and_archive_preserve_typed_dependency_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = SpecDependencyOperationError(
        "spec_dependency_state_conflict",
        "Another active Spec depends on this target.",
        remediation="remove_incoming_dependencies",
        facts={"incoming_count": 1},
    )

    async def fail_delete(_self, *_args, **_kwargs):
        raise error

    async def fail_archive(_self, *_args, **_kwargs):
        raise error

    monkeypatch.setattr(specs.DeleteSpecUseCase, "execute", fail_delete)
    monkeypatch.setattr(boards.ArchiveTreeUseCase, "execute", fail_archive)

    with pytest.raises(HTTPException) as delete_error:
        await specs.delete_spec("source", user_id="user-1", uow=object())
    assert delete_error.value.status_code == 409
    assert delete_error.value.detail["code"] == "spec_dependency_state_conflict"
    assert delete_error.value.detail["retryable"] is False

    principal = Principal(subject="user-1", realm_id="local", actor_kind="human")
    with pytest.raises(HTTPException) as archive_error:
        await boards.archive_tree(
            "board-1",
            "spec",
            "target",
            principal=principal,
            uow=object(),
        )
    assert archive_error.value.status_code == 409
    assert archive_error.value.detail["facts"] == {"incoming_count": 1}


@pytest.mark.parametrize(
    ("code", "expected_status", "expected_message"),
    (
        (
            "invalid_spec_dependency_request",
            400,
            "Spec dependency request is invalid.",
        ),
        (
            "invalid_cursor",
            400,
            "The Spec dependency cursor is invalid for this query.",
        ),
        (
            "dependency_target_unavailable",
            404,
            "Dependency target is unavailable.",
        ),
        (
            "spec_dependency_not_found",
            404,
            "Active Spec dependency was not found.",
        ),
        (
            "spec_dependency_version_conflict",
            409,
            "Spec changed after the dependency request was prepared.",
        ),
        (
            "spec_dependency_cycle",
            409,
            "This dependency would create a cycle between Specs.",
        ),
        (
            "cross_board_dependency_forbidden",
            409,
            "Operational Spec dependencies cannot cross board boundaries.",
        ),
        (
            "spec_dependency_state_conflict",
            409,
            "Spec dependency operation conflicts with the current state.",
        ),
    ),
)
def test_closed_error_vocabulary_maps_without_sql_details(
    code: str,
    expected_status: int,
    expected_message: str,
) -> None:
    projected = specs._spec_dependency_error(
        SpecDependencyOperationError(code, "Safe domain message")
    )
    assert projected.status_code == expected_status
    assert projected.detail == {
        "code": code,
        "message": expected_message,
        "retryable": code == "spec_dependency_version_conflict",
    }


def test_unknown_dependency_error_code_fails_closed_without_leaking_details() -> None:
    secret = "postgresql://admin:secret@example.invalid/pulse"
    projected = specs._spec_dependency_error(
        SpecDependencyOperationError(
            "future_unreviewed_dependency_error",
            secret,
            remediation=secret,
            facts={"sql": secret},
        )
    )

    assert projected.status_code == 500
    assert projected.detail == {
        "code": "spec_dependency_internal_error",
        "message": "Spec dependency operation could not be completed.",
        "retryable": False,
    }
    assert secret not in str(projected.detail)
