"""SK-B/B13 board-scoped policy-governance REST contract."""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.api.guidelines import router as legacy_guidelines_router
from okto_pulse.community.api.policy_governance import (
    CorePolicyGovernanceFacade,
    GuidelineExportV2Request,
    PolicyErrorEnvelope,
    _project_core_result,
    get_policy_governance_facade,
    router,
)
from okto_pulse.community.auth import LocalAuthProvider
from okto_pulse.community.config import CommunitySettings
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import (
    CommandValidationError,
    ConflictError,
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.policy_governance import (
    GuidelineRevisionUnderBump,
)
from okto_pulse.core.domain.guideline_compliance import PolicyProjection
from okto_pulse.core.domain.guideline_compliance import (
    GuidelineRevisionListItem,
    GuidelineRevisionProjectionPage,
)
from okto_pulse.core.domain.guideline_policy import GuidelineRevisionPageCursor
from okto_pulse.core.domain.guideline_lifecycle import GuidelineVersionBump
from okto_pulse.core.domain.realm import LOCAL_REALM_ID
from okto_pulse.core.inbound.guideline_policy_cursor import (
    GuidelinePolicyCursorConfigurationError,
    policy_cursor_codec_from_settings,
)
from okto_pulse.core.infra.config import (
    CoreSettings,
    configure_settings,
    get_settings,
    reset_settings_for_tests,
)
from okto_pulse.core.infra.auth import (
    configure_auth,
    get_auth_provider,
    reset_auth_for_tests,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.authentication import (
    AuthorizationDenied,
    MissingCredential,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyAdapterMissing,
    GuidelinePolicyCasConflict,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyIdempotencyConflict,
    GuidelineRevisionListQuery,
)
from okto_pulse.core.services.main import GuidelineService


class _Facade:
    def __init__(
        self,
        error: Exception | None = None,
        *,
        results: dict[str, object] | None = None,
    ) -> None:
        self.error = error
        self.results = results or {}
        self.calls: list[tuple[str, dict, object, object]] = []

    async def execute(self, operation, values, *, actor, uow):
        self.calls.append((operation, values, actor, uow))
        if self.error is not None:
            raise self.error
        if operation in self.results:
            return self.results[operation]
        if operation.startswith("list_") and operation != "list_waiver_events":
            return {"items": [], "limit": values["limit"], "has_more": False, "next_cursor": None}
        if operation == "create_revision":
            return {
                "status": "applied",
                "revision": None,
                "head": None,
                "minimum_bump": None,
                "rejection_code": None,
            }
        if operation == "get_revision":
            return {
                "guideline": {},
                "revision": {},
                "head": {},
                "retirement": None,
            }
        if operation == "retire_guideline":
            return {"retirement": {}}
        if operation in {"preview_impact", "get_impact", "get_compliance_receipt", "get_current_compliance"}:
            return {"receipt": {}}
        if operation == "adopt_revision":
            return {"binding": {}, "receipt": {}}
        if operation == "evaluate_compliance":
            return {"evaluation": {}}
        if operation == "get_waiver":
            return {"waiver": {}}
        if operation == "list_waiver_events":
            return {"events": []}
        return {"waiver": {}, "event": {}}


def _client(
    facade: _Facade | None,
) -> tuple[TestClient, object]:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    uow = SimpleNamespace(marker="uow", dependency_entries=0)

    async def override_uow():
        uow.dependency_entries += 1
        yield uow

    principal = Principal(
        subject="owner-b13",
        realm_id=LOCAL_REALM_ID,
        claims={
            "permissions": {
                "guidelines": {
                    "revisions": {"read": True, "create": True, "retire": True},
                    "rules": {"author_blocking": True},
                    "impact": {"preview": True},
                    "adoption": {"manage": True},
                    "compliance": {"read": True, "evaluate": True},
                    "waiver": {
                        "read": True,
                        "request": True,
                        "review": True,
                        "revoke": True,
                        "revalidate": True,
                    },
                }
            }
        },
    )
    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[get_unit_of_work] = override_uow
    if facade is not None:
        app.dependency_overrides[get_policy_governance_facade] = lambda: facade
    return TestClient(app, raise_server_exceptions=False), uow


def _minimal_v2_envelope() -> dict:
    created_at = "2026-07-29T18:00:00Z"
    return {
        "contract_version": "guideline-export/v2",
        "schema_version": "2",
        "kind": "guidelines",
        "exported_at": created_at,
        "source_board_id": "board-b13",
        "content_digest": "a" * 64,
        "guidelines": [
            {
                "identity": {
                    "guideline_id": "guideline-b13",
                    "owner_id": "owner-b13",
                    "scope": "inline",
                    "board_id": "board-b13",
                    "context_scope": "all",
                    "created_at": created_at,
                },
                "revisions": [
                    {
                        "revision_id": "revision-b13",
                        "guideline_id": "guideline-b13",
                        "revision_number": 1,
                        "semantic_version": "1.0.0",
                        "title": "B13",
                        "content": "Closed export.",
                        "content_digest": "b" * 64,
                        "rules": [],
                        "created_by": "owner-b13",
                        "created_at": created_at,
                        "parent_revision_id": None,
                        "tags": [],
                        "published_head_revision": 1,
                        "published_head_updated_at": created_at,
                        "legacy_version": None,
                        "legacy_version_unresolvable": False,
                        "legacy_tags": None,
                    }
                ],
                "head": {
                    "guideline_id": "guideline-b13",
                    "revision_id": "revision-b13",
                    "revision_number": 1,
                    "semantic_version": "1.0.0",
                    "head_revision": 1,
                    "updated_at": created_at,
                },
                "retirement": None,
                "bindings": [],
                "history_status": "complete",
                "migration_notes": [],
            }
        ],
    }


def _blocking_v2_envelope() -> dict:
    from okto_pulse.core.domain.guideline_import_export import (
        GuidelineExportAggregate,
        GuidelineExportRevision,
        build_guideline_export_v2,
        guideline_export_payload,
    )
    from okto_pulse.core.domain.guideline_lifecycle import (
        guideline_revision_content_digest_v1,
    )
    from okto_pulse.core.domain.guideline_policy import (
        Guideline,
        GuidelineEnforcement,
        GuidelineHead,
        GuidelinePredicate,
        GuidelineRevision,
        GuidelineRule,
        GuidelineScope,
        PolicyEntityType,
    )

    now = datetime(2026, 7, 29, 18, tzinfo=timezone.utc)
    rule = GuidelineRule(
        rule_id="rule-b13",
        code="require_title",
        title="Require title",
        description="A blocking import authorization fixture.",
        target_entity_types=(PolicyEntityType.SPEC,),
        predicates=(GuidelinePredicate("field_present", (("field", "title"),)),),
        enforcement=GuidelineEnforcement.BLOCKING,
        waivable=True,
    )
    revision = GuidelineRevision(
        revision_id="revision-b13",
        guideline_id="guideline-b13",
        revision_number=1,
        semantic_version="1.0.0",
        title="Blocking B13",
        content="One blocking policy.",
        content_digest=guideline_revision_content_digest_v1(
            title="Blocking B13",
            content="One blocking policy.",
            rules=(rule,),
            tags=(),
        ),
        rules=(rule,),
        created_by="owner-b13",
        created_at=now,
    )
    aggregate = GuidelineExportAggregate(
        identity=Guideline(
            guideline_id="guideline-b13",
            owner_id="owner-b13",
            scope=GuidelineScope.INLINE,
            board_id="board-b13",
            created_at=now,
        ),
        revisions=(GuidelineExportRevision(revision),),
        head=GuidelineHead(
            guideline_id="guideline-b13",
            revision_id="revision-b13",
            revision_number=1,
            semantic_version="1.0.0",
            head_revision=1,
            updated_at=now,
        ),
    )
    return guideline_export_payload(
        build_guideline_export_v2(
            (aggregate,),
            exported_at=now,
            source_board_id="board-b13",
        )
    )


def test_literal_governance_routes_are_registered_before_legacy_param_routes() -> None:
    paths = [route.path for route in router.routes]
    board_export = "/boards/{board_id}/guidelines/export"
    board_import = "/boards/{board_id}/guidelines/import"
    assert paths[:2] == [board_export, board_import]
    assert (
        paths.index("/boards/{board_id}/policy-compliance/receipts/current")
        < paths.index(
            "/boards/{board_id}/policy-compliance/receipts/{receipt_id}"
        )
    )
    assert (
        paths.index("/boards/{board_id}/policy-waivers/{waiver_id}/events")
        < paths.index("/boards/{board_id}/policy-waivers/{waiver_id}")
    )
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "okto_pulse"
        / "community"
        / "api"
        / "router.py"
    ).read_text(encoding="utf-8")
    assert source.index("include_router(policy_governance_router") < source.index(
        "include_router(guidelines_router"
    )


def test_revision_projection_and_closed_body_plumb_to_one_facade() -> None:
    facade = _Facade()
    client, uow = _client(facade)

    response = client.get(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
        params={"projection": "detail", "limit": 17},
    )
    assert response.status_code == 200
    operation, values, actor, captured_uow = facade.calls[-1]
    assert operation == "list_revisions"
    assert values == {
        "board_id": "board-b13",
        "guideline_id": "guideline-b13",
        "limit": 17,
        "cursor": None,
        "projection": "detail",
    }
    assert actor.board_id == "board-b13"
    assert captured_uow is uow

    create = client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
        json={
            "idempotency_key": "revision:b13:2",
            "patch": {"title": "Second revision"},
        },
    )
    assert create.status_code == 201
    assert facade.calls[-1][0] == "create_revision"
    assert facade.calls[-1][1]["patch"] == {
        "title": "Second revision",
        "content": None,
        "tags": None,
        "rules": None,
    }

    unknown = client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
        json={
            "idempotency_key": "revision:b13:3",
            "patch": {"title": "Third revision"},
            "unexpected": True,
        },
    )
    assert unknown.status_code == 400
    assert unknown.json()["detail"]["code"] == "validation_failed"
    assert facade.calls[-1][1]["idempotency_key"] == "revision:b13:2"


def test_governance_request_validation_is_structured_and_never_calls_facade() -> None:
    facade = _Facade()
    client, _ = _client(facade)

    invalid_enum = client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/impact-previews",
        json={
            "proposed_priority": 1,
            "proposed_default_enforcement": "sometimes",
            "idempotency_key": "impact:b13",
        },
    )
    invalid_date = client.post(
        "/api/v1/boards/board-b13/policy-waivers",
        json={
            "finding_id": "finding-b13",
            "justification": "Temporary.",
            "expires_at": "not-a-date",
            "idempotency_key": "waiver:b13",
        },
    )

    assert invalid_enum.status_code == 400
    assert invalid_date.status_code == 400
    assert invalid_enum.json()["detail"]["code"] == "validation_failed"
    assert invalid_date.json()["detail"]["code"] == "validation_failed"
    assert facade.calls == []


def test_import_v2_schema_is_recursively_closed_before_core_or_uow() -> None:
    payload = _minimal_v2_envelope()
    payload["guidelines"][0]["identity"]["unexpected"] = "must fail"
    facade = _Facade()
    client, _ = _client(facade)

    response = client.post(
        "/api/v1/boards/board-b13/guidelines/import",
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "validation_failed"
    with pytest.raises(ValueError):
        GuidelineExportV2Request.model_validate(payload)
    assert facade.calls == []


def test_export_preserves_required_nullable_fields_for_import_roundtrip() -> None:
    payload = _minimal_v2_envelope()
    payload["source_board_id"] = None
    aggregate = payload["guidelines"][0]
    aggregate["identity"]["scope"] = "global"
    aggregate["identity"]["board_id"] = None
    aggregate["retirement"] = None
    revision = aggregate["revisions"][0]
    revision["parent_revision_id"] = None
    revision["legacy_version"] = None
    revision["legacy_tags"] = None

    export_route = next(
        route
        for route in router.routes
        if route.path == "/boards/{board_id}/guidelines/export"
    )
    assert export_route.response_model_exclude_none is False
    projected = GuidelineExportV2Request.model_validate(payload).model_dump(
        mode="json",
        exclude_none=export_route.response_model_exclude_none,
    )

    assert projected["source_board_id"] is None
    assert projected["guidelines"][0]["identity"]["board_id"] is None
    assert projected["guidelines"][0]["retirement"] is None
    assert projected["guidelines"][0]["revisions"][0][
        "parent_revision_id"
    ] is None
    GuidelineExportV2Request.model_validate(projected)


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    (
        (CommandValidationError("invalid"), 400, "validation_failed"),
        (PermissionDeniedError("denied"), 403, "permission_denied"),
        (EntityNotFoundError("guideline", "secret-id"), 404, "not_found"),
        (ConflictError("guideline", "secret-id"), 409, "conflict"),
        (
            GuidelinePolicyAdapterMissing("missing"),
            503,
            "service_unavailable",
        ),
    ),
)
def test_governance_errors_use_bounded_shared_projection(
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    facade = _Facade(error)
    client, _ = _client(facade)

    response = client.get(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions"
    )

    assert response.status_code == expected_status
    detail = response.json()["detail"]
    assert detail["code"] == expected_code
    assert detail["http_status"] == expected_status
    assert "secret-id" not in str(detail)
    assert set(detail) == {
        "outcome",
        "error",
        "code",
        "error_code",
        "message",
        "category",
        "status_category",
        "http_status",
        "retryable",
        "next_action",
        "details",
    }


@pytest.mark.parametrize(
    ("auth_error", "expected_status", "expected_code"),
    (
        (MissingCredential(), 401, "authentication_required"),
        (AuthorizationDenied(), 403, "permission_denied"),
    ),
)
def test_real_auth_dependency_failures_use_closed_policy_envelope(
    auth_error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    class RejectingAuth:
        async def authenticate(self, credential):
            del credential
            raise auth_error

    try:
        previous = get_auth_provider()
    except RuntimeError:
        previous = None
    configure_auth(RejectingAuth())
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions"
            )
    finally:
        if previous is None:
            reset_auth_for_tests()
        else:
            configure_auth(previous)

    envelope = PolicyErrorEnvelope.model_validate(response.json())
    assert response.status_code == expected_status
    assert envelope.detail.code == expected_code
    assert envelope.detail.error == "permission_denied"
    assert envelope.detail.category == "permission_denied"
    assert envelope.detail.http_status == expected_status
    if expected_status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


def test_real_uow_dependency_failure_is_bounded_and_canonical() -> None:
    try:
        previous = get_auth_provider()
    except RuntimeError:
        previous = None
    configure_auth(LocalAuthProvider())
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions"
            )
    finally:
        if previous is None:
            reset_auth_for_tests()
        else:
            configure_auth(previous)

    envelope = PolicyErrorEnvelope.model_validate(response.json())
    assert response.status_code == 503
    assert envelope.detail.error == "service_unavailable"
    assert envelope.detail.code == "persistence_provider_not_configured"
    assert "UnitOfWorkFactory" not in json.dumps(response.json())


def test_canonical_endpoint_http_exception_is_not_double_wrapped() -> None:
    from okto_pulse.core.inbound.guideline_policy_error import (
        project_guideline_policy_error,
    )

    detail = project_guideline_policy_error(
        ConflictError("guideline", "private-guideline-id")
    )
    client, _ = _client(
        _Facade(
            HTTPException(
                status_code=409,
                detail=detail,
                headers={"X-Policy-Error": "canonical"},
            )
        )
    )

    response = client.get(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions"
    )

    assert response.status_code == 409
    assert response.json() == {"detail": detail}
    assert response.headers["x-policy-error"] == "canonical"


def test_stale_adoption_is_http_409_with_closed_currentness_reasons() -> None:
    client, _ = _client(
        _Facade(
            GuidelinePolicyCasConflict(
                "guideline_impact_stale",
                details=(
                    (
                        "stale_reasons",
                        "guideline_head_changed,waiver_snapshot_changed",
                    ),
                ),
            )
        )
    )

    response = client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/adoptions",
        json={
            "impact_receipt_id": "impact-b13",
            "impact_digest": "a" * 64,
            "idempotency_key": "adoption:b13",
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "conflict"
    assert detail["details"] == {
        "reason_code": "guideline_impact_stale",
        "stale_reasons": (
            "guideline_head_changed,waiver_snapshot_changed"
        ),
    }


def test_untrusted_dependency_error_code_cannot_escape_or_break_projection() -> None:
    client, _ = _client(
        _Facade(
            HTTPException(
                status_code=503,
                detail={"code": ["not", "hashable"], "message": "private"},
            )
        )
    )

    response = client.get(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions"
    )

    envelope = PolicyErrorEnvelope.model_validate(response.json())
    assert response.status_code == 503
    assert envelope.detail.code == "service_unavailable"
    assert "private" not in json.dumps(response.json())


def test_list_surfaces_close_projection_limit_and_cursor_plumbing() -> None:
    facade = _Facade()
    client, _ = _client(facade)
    evaluated_at = "2026-07-29T18:00:00Z"

    assert (
        client.get(
            "/api/v1/boards/board-b13/policy-compliance/receipts",
            params={"projection": "full"},
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/v1/boards/board-b13/policy-compliance/findings",
            params={"limit": 201},
        ).status_code
        == 400
    )
    response = client.get(
        "/api/v1/boards/board-b13/policy-waivers",
        params={
            "evaluated_at": evaluated_at,
            "projection": "detail",
            "cursor": "opaque.cursor",
            "status": "approved",
        },
    )
    assert response.status_code == 200
    operation, values, _, _ = facade.calls[-1]
    assert operation == "list_waivers"
    assert values["cursor"] == "opaque.cursor"
    assert values["projection"] == "detail"
    assert values["status"] == "approved"
    assert values["evaluated_at"] == datetime(
        2026, 7, 29, 18, tzinfo=timezone.utc
    )


def test_waiver_action_routes_are_distinct_and_closed() -> None:
    facade = _Facade(EntityNotFoundError("policy_waiver", "waiver-b13"))
    client, _ = _client(facade)
    base = "/api/v1/boards/board-b13/policy-waivers/waiver-b13"
    common = {
        "reason": "Independently reviewed.",
        "evidence_refs": ["kb:b13-review"],
        "expected_waiver_revision": 1,
        "idempotency_key": "waiver:b13:action",
    }

    assert client.post(f"{base}/review", json={**common, "decision": "approve"}).status_code == 404
    assert facade.calls[-1][0] == "review_waiver"
    assert client.post(f"{base}/revoke", json=common).status_code == 404
    assert facade.calls[-1][0] == "revoke_waiver"
    assert (
        client.post(
            f"{base}/revalidate",
            json={**common, "new_expires_at": "2026-08-29T18:00:00Z"},
        ).status_code
        == 404
    )
    assert facade.calls[-1][0] == "revalidate_waiver"
    assert client.get(f"{base}/events").status_code == 404
    assert facade.calls[-1][0] == "list_waiver_events"
    assert client.get(base).status_code == 404
    assert facade.calls[-1][0] == "get_waiver"


def test_missing_cursor_secret_fails_closed_as_503() -> None:
    try:
        previous = get_settings()
    except RuntimeError:
        previous = None
    configure_settings(CoreSettings(guideline_policy_cursor_signing_key=None))
    client, _ = _client(None)
    try:
        response = client.get(
            "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions"
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "service_unavailable"
        assert response.json()["detail"]["details"]["reason_code"] == (
            "guideline_policy_cursor_unavailable"
        )
    finally:
        if previous is None:
            reset_settings_for_tests()
        else:
            configure_settings(previous)


@pytest.mark.asyncio
async def test_non_paginated_facade_does_not_require_cursor_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_pulse.community.api.policy_governance as module

    class Command:
        def __init__(self, **values):
            self.values = values

    class UseCase:
        async def execute(self, command, *, actor, uow):
            return {"values": command.values, "actor": actor, "uow": uow}

    real_import = module.import_module

    def import_for_test(name: str):
        if name == "okto_pulse.core.application.use_cases.policy_governance":
            return SimpleNamespace(
                GetGuidelineRevisionCommand=Command,
                GetGuidelineRevisionUseCase=UseCase,
            )
        return real_import(name)

    monkeypatch.setattr(module, "import_module", import_for_test)
    try:
        previous = get_settings()
    except RuntimeError:
        previous = None
    configure_settings(CoreSettings(guideline_policy_cursor_signing_key=None))
    try:
        result = await CorePolicyGovernanceFacade().execute(
            "get_revision",
            {
                "board_id": "board-b13",
                "guideline_id": "guideline-b13",
                "revision_id": "revision-b13",
            },
            actor=object(),
            uow=object(),  # type: ignore[arg-type]
        )
        assert result["values"]["revision_id"] == "revision-b13"
    finally:
        if previous is None:
            reset_settings_for_tests()
        else:
            configure_settings(previous)


def test_community_env_secret_is_stable_and_interoperable_across_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "stable-b13-policy-cursor-secret-0001"
    monkeypatch.setenv("GUIDELINE_POLICY_CURSOR_SIGNING_KEY", secret)
    first_settings = CommunitySettings(_env_file=None)
    second_settings = CommunitySettings(_env_file=None)
    first = policy_cursor_codec_from_settings(first_settings)
    second = policy_cursor_codec_from_settings(second_settings)
    cursor = GuidelineRevisionPageCursor(
        revision_number=8,
        item_id="revision-b13-8",
        filter_digest="a" * 64,
        projection_digest="b" * 64,
    )

    token = first.encode(cursor)

    assert second.decode(token, expected_kind="revision") == cursor
    assert token == second.encode(cursor)
    with pytest.raises(GuidelinePolicyCursorConfigurationError):
        policy_cursor_codec_from_settings(
            CoreSettings(guideline_policy_cursor_signing_key=None)
        )


def test_revision_projection_is_slim_or_detailed_without_none_leaks() -> None:
    settings = CoreSettings(
        guideline_policy_cursor_signing_key="projection-b13-secret-key-00000001"
    )
    codec = policy_cursor_codec_from_settings(settings)
    common = {
        "revision_id": "revision-b13",
        "guideline_id": "guideline-b13",
        "revision_number": 2,
        "semantic_version": "1.1.0",
        "title": "B13 projection",
        "created_by": "owner-b13",
        "created_at": datetime(2026, 7, 29, 18, tzinfo=timezone.utc),
        "parent_revision_id": "revision-b13-1",
    }
    summary_item = GuidelineRevisionListItem(
        projection=PolicyProjection.SUMMARY,
        **common,
    )
    detail_item = GuidelineRevisionListItem(
        projection=PolicyProjection.DETAIL,
        content="Detailed content.",
        content_digest="c" * 64,
        tags=("policy",),
        rules=(),
        **common,
    )

    summary = _project_core_result(
        SimpleNamespace(
            page=GuidelineRevisionProjectionPage(
                items=(summary_item,),
                limit=50,
                next_cursor=None,
                has_more=False,
            )
        ),
        codec=codec,
    )
    detail = _project_core_result(
        SimpleNamespace(
            page=GuidelineRevisionProjectionPage(
                items=(detail_item,),
                limit=50,
                next_cursor=None,
                has_more=False,
            )
        ),
        codec=codec,
    )

    assert not {"content", "content_digest", "tags", "rules"}.intersection(
        summary["items"][0]
    )
    assert detail["items"][0]["content"] == "Detailed content."
    assert detail["items"][0]["tags"] == ["policy"]
    assert detail["items"][0]["rules"] == []


def test_revision_cursor_is_bound_to_projection_profile() -> None:
    secret = "cross-profile-b13-secret-key-0000001"
    settings = CoreSettings(guideline_policy_cursor_signing_key=secret)
    codec = policy_cursor_codec_from_settings(settings)
    detail_query = GuidelineRevisionListQuery(
        guideline_id="guideline-b13",
        projection=PolicyProjection.DETAIL,
    )
    token = codec.encode(
        GuidelineRevisionPageCursor(
            revision_number=2,
            item_id="revision-b13-2",
            filter_digest=detail_query.filter_digest,
            projection_digest=detail_query.projection_digest,
        )
    )
    try:
        previous = get_settings()
    except RuntimeError:
        previous = None
    configure_settings(settings)
    client, _ = _client(None)
    try:
        response = client.get(
            "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
            params={"cursor": token, "projection": "summary"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_cursor"
    finally:
        if previous is None:
            reset_settings_for_tests()
        else:
            configure_settings(previous)


def test_empty_cursor_is_rejected_instead_of_restarting_pagination() -> None:
    settings = CoreSettings(
        guideline_policy_cursor_signing_key="empty-cursor-b13-secret-key-000001"
    )
    try:
        previous = get_settings()
    except RuntimeError:
        previous = None
    configure_settings(settings)
    client, _ = _client(None)
    try:
        response = client.get(
            "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
            params={"cursor": ""},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_cursor"
    finally:
        client.close()
        if previous is None:
            reset_settings_for_tests()
        else:
            configure_settings(previous)


@pytest.mark.asyncio
async def test_local_auth_materializes_canonical_full_control_permissions() -> None:
    principal = await LocalAuthProvider().authenticate(None)
    actor = RESTAdapterContract.actor_from_principal(
        principal,
        board_id="board-b13",
    )
    permissions = actor.permissions
    assert isinstance(permissions, dict)
    from okto_pulse.core.application.use_cases.policy_governance import (
        POLICY_GOVERNANCE_CAPABILITIES,
    )
    from okto_pulse.core.domain.permissions import PermissionSet

    effective = PermissionSet(permissions)
    assert all(
        effective.check(capability) is None
        for capability in POLICY_GOVERNANCE_CAPABILITIES
    )


def test_every_governance_route_declares_a_closed_response_model() -> None:
    assert router.routes
    assert all(route.response_model is not None for route in router.routes)


def test_legacy_patch_and_delete_delegate_immutable_append_and_retirement() -> None:
    update_source = inspect.getsource(GuidelineService.update_guideline)
    delete_source = inspect.getsource(GuidelineService.delete_guideline)

    assert "GuidelineRevisionPatch" in update_source
    assert "append_revision_cas" in update_source
    assert "GuidelineRetirementCommand" in delete_source
    assert "retire_guideline_cas" in delete_source
    assert "session.delete" not in delete_source


def test_projection_enum_remains_exactly_summary_and_detail() -> None:
    assert {item.value for item in PolicyProjection} == {"summary", "detail"}


def test_import_rejects_unsupported_nested_enum_before_application_work() -> None:
    payload = _minimal_v2_envelope()
    payload["guidelines"][0]["identity"]["scope"] = "workspace"
    client, _ = _client(_Facade())

    response = client.post(
        "/api/v1/boards/board-b13/guidelines/import",
        json=payload,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "validation_failed"


@pytest.mark.parametrize("evidence", [None, []])
def test_waiver_mutations_require_non_empty_evidence_before_facade(
    evidence: list[str] | None,
) -> None:
    facade = _Facade()
    client, _ = _client(facade)
    body = {
        "finding_id": "finding-b13",
        "justification": "Temporary exception.",
        "expires_at": "2026-08-29T18:00:00Z",
        "idempotency_key": "waiver:b13",
    }
    if evidence is not None:
        body["evidence_refs"] = evidence

    response = client.post(
        "/api/v1/boards/board-b13/policy-waivers",
        json=body,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "validation_failed"
    assert facade.calls == []


def test_revision_storage_boundaries_reject_boundary_plus_one_before_facade() -> None:
    facade = _Facade()
    client, _ = _client(facade)
    route = (
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions"
    )
    valid = {
        "next_revision_id": "r" * 36,
        "idempotency_key": "i" * 255,
        "declared_semantic_version": "1." + ("0" * 62),
        "patch": {"title": "Boundary"},
    }
    accepted = client.post(route, json=valid)
    assert accepted.status_code == 201
    assert len(facade.calls) == 1

    for field, value in (
        ("next_revision_id", "r" * 37),
        ("idempotency_key", "i" * 256),
        ("declared_semantic_version", "1." + ("0" * 63)),
    ):
        response = client.post(route, json={**valid, field: value})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "validation_failed"
    assert len(facade.calls) == 1


def test_rule_and_priority_storage_boundaries_are_closed_at_rest() -> None:
    facade = _Facade()
    client, _ = _client(facade)
    revision_route = (
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions"
    )
    rule = {
        "rule_id": "r" * 64,
        "code": "require_title",
        "title": "Require title",
        "description": "A durable executable rule.",
        "target_entity_types": ["spec"],
        "predicates": [
            {
                "predicate_code": "field_present",
                "parameters": {"field": "title"},
            }
        ],
        "enforcement": "advisory",
        "operator": "all",
        "waivable": False,
    }
    accepted_rule = client.post(
        revision_route,
        json={
            "idempotency_key": "rule:boundary",
            "patch": {"rules": [rule]},
        },
    )
    rejected_rule = client.post(
        revision_route,
        json={
            "idempotency_key": "rule:boundary:invalid",
            "patch": {"rules": [{**rule, "rule_id": "r" * 65}]},
        },
    )
    assert accepted_rule.status_code == 201
    assert rejected_rule.status_code == 400
    assert len(facade.calls) == 1

    impact_facade = _Facade(EntityNotFoundError("guideline", "guideline-b13"))
    impact_client, _ = _client(impact_facade)
    impact_route = (
        "/api/v1/boards/board-b13/guidelines/guideline-b13/impact-previews"
    )
    common = {
        "proposed_default_enforcement": "advisory",
        "idempotency_key": "impact:boundary",
    }
    accepted_priority = impact_client.post(
        impact_route,
        json={**common, "proposed_priority": 2_147_483_647},
    )
    rejected_priority = impact_client.post(
        impact_route,
        json={**common, "proposed_priority": 2_147_483_648},
    )
    assert accepted_priority.status_code == 404
    assert rejected_priority.status_code == 400
    assert len(impact_facade.calls) == 1


def test_path_and_import_ids_reject_storage_boundary_plus_one() -> None:
    facade = _Facade()
    client, uow = _client(facade)
    accepted_path = client.get(
        f"/api/v1/boards/{'b' * 36}/guidelines/{'g' * 36}/revisions"
    )
    rejected_path = client.get(
        f"/api/v1/boards/{'b' * 37}/guidelines/{'g' * 36}/revisions"
    )
    assert accepted_path.status_code == 200
    assert rejected_path.status_code == 400
    assert len(facade.calls) == 1
    assert uow.dependency_entries == 1

    payload = _minimal_v2_envelope()
    payload["guidelines"][0]["identity"]["guideline_id"] = "g" * 37
    imported = client.post(
        "/api/v1/boards/board-b13/guidelines/import",
        json=payload,
    )
    assert imported.status_code == 400
    assert imported.json()["detail"]["code"] == "validation_failed"
    assert uow.dependency_entries == 1


@pytest.mark.parametrize(
    "board_id_location",
    ("source", "identity"),
)
def test_import_board_ids_enforce_community_physical_boundary(
    board_id_location: str,
) -> None:
    accepted = _minimal_v2_envelope()
    accepted["source_board_id"] = "b" * 36
    accepted["guidelines"][0]["identity"]["board_id"] = "b" * 36
    GuidelineExportV2Request.model_validate(accepted)

    rejected = _minimal_v2_envelope()
    if board_id_location == "source":
        rejected["source_board_id"] = "b" * 37
    else:
        rejected["guidelines"][0]["identity"]["board_id"] = "b" * 37
    client, uow = _client(_Facade())

    response = client.post(
        "/api/v1/boards/board-b13/guidelines/import",
        json=rejected,
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "validation_failed"
    assert uow.dependency_entries == 0


def test_import_accepts_36_character_board_ids_through_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.application.use_cases import (
        guideline_import_export as import_use_cases,
    )
    from okto_pulse.core.domain.guideline_import_export import (
        GuidelineImportResult,
        GuidelineImportTransactionStatus,
    )

    captured: dict[str, object] = {}

    class ImportUseCase:
        async def execute(self, command, *, actor, uow):
            captured.update(command=command, actor=actor, uow=uow)
            return SimpleNamespace(
                result=GuidelineImportResult(
                    transaction_status=GuidelineImportTransactionStatus.PLANNED,
                    created_count=0,
                    skip_identical_count=0,
                    conflict_count=0,
                    overwritten_row_count=0,
                    dry_run=False,
                )
            )

    monkeypatch.setattr(
        import_use_cases,
        "ImportGuidelinePolicyUseCase",
        ImportUseCase,
    )
    payload = _minimal_v2_envelope()
    payload["source_board_id"] = "s" * 36
    payload["guidelines"][0]["identity"]["board_id"] = "i" * 36
    client, uow = _client(_Facade())

    response = client.post(
        f"/api/v1/boards/{'t' * 36}/guidelines/import",
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["transaction_status"] == "planned"
    assert captured["command"].target_board_id == "t" * 36
    assert uow.dependency_entries == 1


@pytest.mark.asyncio
async def test_local_auth_claims_are_fresh_for_every_authentication() -> None:
    provider = LocalAuthProvider()
    first = await provider.authenticate(None)
    first_permissions = first.claims["permissions"]
    first_permissions["guidelines"]["revisions"]["read"] = False

    second = await provider.authenticate(None)

    assert second.claims["permissions"]["guidelines"]["revisions"]["read"] is True


def test_real_local_auth_reaches_board_authorization_instead_of_403() -> None:
    class MissingBoards:
        calls = 0

        async def get(self, board_id: str):
            self.calls += 1
            assert board_id == "board-b13"
            return None

    boards = MissingBoards()
    uow = SimpleNamespace(boards=boards)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_uow():
        yield uow

    app.dependency_overrides[get_unit_of_work] = override_uow
    try:
        previous = get_auth_provider()
    except RuntimeError:
        previous = None
    configure_auth(LocalAuthProvider())
    try:
        response = TestClient(app, raise_server_exceptions=False).get(
            "/api/v1/boards/board-b13/guidelines/guideline-b13/"
            "revisions/revision-b13"
        )
    finally:
        if previous is not None:
            configure_auth(previous)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
    assert boards.calls == 1


def test_revision_http_statuses_preserve_noop_and_replay_semantics() -> None:
    payload = {
        "idempotency_key": "revision:b13:2",
        "patch": {"title": "Second revision"},
    }
    applied_result = {
        "status": "applied",
        "revision": None,
        "head": None,
        "minimum_bump": None,
        "rejection_code": None,
    }
    applied_client, _ = _client(
        _Facade(results={"create_revision": applied_result})
    )
    first = applied_client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
        json=payload,
    )
    replay = applied_client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
        json=payload,
    )
    noop_client, _ = _client(
        _Facade(
            results={
                "create_revision": {
                    **applied_result,
                    "status": "noop",
                }
            }
        )
    )
    noop = noop_client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
        json={**payload, "idempotency_key": "revision:b13:noop"},
    )

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert noop.status_code == 200
    assert noop.json()["status"] == "noop"


def test_revision_minimum_bump_uses_the_normative_string_wire_enum() -> None:
    client, _ = _client(
        _Facade(
            results={
                "create_revision": {
                    "status": "noop",
                    "revision": None,
                    "head": None,
                    "minimum_bump": GuidelineVersionBump.MINOR,
                    "rejection_code": None,
                }
            }
        )
    )

    response = client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
        json={
            "idempotency_key": "revision:b13:minimum-bump",
            "patch": {"title": "No-op"},
        },
    )

    assert response.status_code == 200
    assert response.json()["minimum_bump"] == "minor"


def test_under_bump_error_is_closed_and_preserves_remediation() -> None:
    client, _ = _client(
        _Facade(
            GuidelineRevisionUnderBump(
                minimum_bump=GuidelineVersionBump.MAJOR,
                minimum_semantic_version="2.0.0",
                declared_semantic_version="1.1.0",
            )
        )
    )

    response = client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
        json={
            "idempotency_key": "revision:b13:under-bump",
            "patch": {"title": "Breaking"},
            "declared_semantic_version": "1.1.0",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "outcome": "error",
        "error": "under_bump",
        "code": "under_bump",
        "error_code": "under_bump",
        "message": "The declared semantic version is below the required minimum.",
        "category": "invalid_argument",
        "status_category": "invalid_argument",
        "http_status": 400,
        "retryable": False,
        "next_action": "increase_semantic_version",
        "details": {
            "minimum_bump": "major",
            "minimum_semantic_version": "2.0.0",
            "declared_semantic_version": "1.1.0",
        },
    }


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    (
        (
            CommandValidationError("guideline_semver_below_minimum"),
            400,
            "validation_failed",
        ),
        (
            GuidelinePolicyIdempotencyConflict(
                "guideline_revision_idempotency_payload_mismatch"
            ),
            409,
            "conflict",
        ),
    ),
)
def test_revision_under_bump_and_idempotency_conflict_are_structured(
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    client, _ = _client(_Facade(error))
    response = client.post(
        "/api/v1/boards/board-b13/guidelines/guideline-b13/revisions",
        json={
            "idempotency_key": "revision:b13:2",
            "patch": {"title": "Second revision"},
        },
    )
    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == code


def test_import_and_export_require_capabilities_before_uow_access() -> None:
    class PoisonUow:
        def __getattribute__(self, name: str):
            raise AssertionError(f"uow must remain untouched: {name}")

    principal = Principal(
        subject="limited-b13",
        realm_id=LOCAL_REALM_ID,
        claims={"permissions": {}},
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def override_uow():
        yield PoisonUow()

    app.dependency_overrides[require_principal] = lambda: principal
    app.dependency_overrides[get_unit_of_work] = override_uow
    client = TestClient(app, raise_server_exceptions=False)

    exported = client.get("/api/v1/boards/board-b13/guidelines/export")
    imported = client.post(
        "/api/v1/boards/board-b13/guidelines/import",
        json=_minimal_v2_envelope(),
    )

    assert exported.status_code == imported.status_code == 403
    assert exported.json()["detail"]["code"] == "permission_denied"
    assert imported.json()["detail"]["code"] == "permission_denied"

    create_only = Principal(
        subject="limited-b13",
        realm_id=LOCAL_REALM_ID,
        claims={
            "permissions": {
                "guidelines": {"revisions": {"create": True}},
                "spec": {"entity": {"edit_fields": True}},
            }
        },
    )
    blocking_app = FastAPI()
    blocking_app.include_router(router, prefix="/api/v1")
    blocking_app.dependency_overrides[require_principal] = lambda: create_only
    blocking_app.dependency_overrides[get_unit_of_work] = override_uow
    blocking = TestClient(
        blocking_app,
        raise_server_exceptions=False,
    ).post(
        "/api/v1/boards/board-b13/guidelines/import",
        json=_blocking_v2_envelope(),
    )
    assert blocking.status_code == 403
    assert blocking.json()["detail"]["code"] == "permission_denied"


def test_every_governance_success_schema_is_recursively_closed_and_exact() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    document = app.openapi()
    schemas = document["components"]["schemas"]

    def visit(node: object, visited: set[str]) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item, visited)
            return
        if not isinstance(node, dict):
            return
        reference = node.get("$ref")
        if isinstance(reference, str):
            name = reference.rsplit("/", 1)[-1]
            if name in visited:
                return
            visited.add(name)
            visit(schemas[name], visited)
            return
        if node.get("type") == "object":
            additional = node.get("additionalProperties")
            assert additional is False or isinstance(additional, dict)
        for value in node.values():
            visit(value, visited)

    checked_operations = 0
    for path_item in document["paths"].values():
        for method in ("get", "post", "patch", "delete"):
            operation = path_item.get(method)
            if operation is None:
                continue
            responses = operation["responses"]
            assert "422" not in responses
            assert {"400", "401", "403", "404", "409", "503"}.issubset(
                responses
            )
            for status_code in (
                "400",
                "401",
                "403",
                "404",
                "409",
                "503",
                "4XX",
            ):
                visit(
                    responses[status_code]["content"]["application/json"]["schema"],
                    set(),
                )
            success_schemas = [
                media["content"]["application/json"]["schema"]
                for status_code, media in responses.items()
                if status_code.startswith("2")
                and "application/json" in media.get("content", {})
            ]
            assert success_schemas
            for response_schema in success_schemas:
                visit(response_schema, set())
            checked_operations += 1
    assert checked_operations == len(router.routes)

    route_schema = document["paths"][
        "/api/v1/boards/{board_id}/guidelines/{guideline_id}/revisions"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    visited: set[str] = set()
    visit(route_schema, visited)

    assert "ClosedGuidelineRevisionListItem" in visited
    assert "ClosedPolicyWaiverListItem" not in visited
    assert "ClosedGuidelineImpactItem" not in visited
    assert "ClosedPolicyComplianceReceiptListItem" not in visited
    assert "PolicyWaiverPageResponse" not in json.dumps(route_schema)
    create_responses = document["paths"][
        "/api/v1/boards/{board_id}/guidelines/{guideline_id}/revisions"
    ]["post"]["responses"]
    assert {"200", "201"}.issubset(create_responses)
    assert (
        create_responses["200"]["content"]["application/json"]["schema"]
        == create_responses["201"]["content"]["application/json"]["schema"]
    )


def test_legacy_patch_delete_gate_then_delegate_append_retire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.core.application.use_cases.guidelines_crud import (
        DeleteGuidelineUseCase,
        UpdateGuidelineUseCase,
    )

    calls: list[str] = []
    now = datetime(2026, 7, 29, 18, tzinfo=timezone.utc)

    async def fake_update(self, command, *, actor, uow):
        del self, command, actor, uow
        calls.append("update")
        return SimpleNamespace(
            guideline={
                "id": "guideline-b13",
                "title": "Updated",
                "content": "Immutable append.",
                "tags": [],
                "scope": "global",
                "board_id": None,
                "owner_id": "owner-b13",
                "created_at": now,
                "updated_at": now,
            }
        )

    async def fake_delete(self, command, *, actor, uow):
        del self, command, actor, uow
        calls.append("delete")

    monkeypatch.setattr(UpdateGuidelineUseCase, "execute", fake_update)
    monkeypatch.setattr(DeleteGuidelineUseCase, "execute", fake_delete)

    def legacy_client(permissions: dict) -> TestClient:
        app = FastAPI()
        app.include_router(legacy_guidelines_router, prefix="/api/v1")
        principal = Principal(
            subject="owner-b13",
            realm_id=LOCAL_REALM_ID,
            claims={"permissions": permissions},
        )

        async def override_uow():
            yield object()

        app.dependency_overrides[require_principal] = lambda: principal
        app.dependency_overrides[get_unit_of_work] = override_uow
        return TestClient(app, raise_server_exceptions=False)

    denied = legacy_client({})
    denied_patch = denied.patch(
        "/api/v1/guidelines/guideline-b13",
        json={"title": "Denied"},
    )
    denied_delete = denied.delete("/api/v1/guidelines/guideline-b13")
    assert denied_patch.status_code == denied_delete.status_code == 403
    assert denied_patch.json()["detail"]["code"] == "permission_denied"
    assert denied_delete.json()["detail"]["code"] == "permission_denied"
    assert calls == []

    full_control = legacy_client(
        {
            "guidelines": {
                "delete": True,
                "revisions": {
                    "create": True,
                    "retire": True,
                }
            },
            "spec": {"entity": {"edit_fields": True}},
        }
    )
    updated = full_control.patch(
        "/api/v1/guidelines/guideline-b13",
        json={"title": "Updated"},
    )
    deleted = full_control.delete("/api/v1/guidelines/guideline-b13")

    assert updated.status_code == 200
    assert updated.json()["title"] == "Updated"
    assert deleted.status_code == 204
    assert calls == ["update", "delete"]
