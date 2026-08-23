"""REST parity and architectural boundary tests for Code Traceability."""

from __future__ import annotations

import ast
import base64
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
import httpx
from pydantic import SecretStr
import pytest

from okto_pulse.community.api import code_traceability as api
from okto_pulse.core.domain.code_traceability import (
    CodeInvestigationSubmissionLimitExceeded,
    CodeInvestigationUnavailable,
    CodeEvidenceLegacyClassificationIdempotencyConflict,
    CodeEvidenceLegacyClassificationPayloadConflict,
    CodeEvidenceLegacyClassificationRevisionConflict,
    CodeTraceabilityPageCursor,
    CodeTraceabilityRemediation,
)
from okto_pulse.core.ports.code_traceability import (
    CodeTraceabilityRevisionConflict,
)
from okto_pulse.core.ports.authentication import Principal


def test_transport_bodies_exclude_every_path_owned_identifier() -> None:
    expectations = {
        api.StartCodeInvestigationBody: {"board_id"},
        api.CodeInvestigationReceiptBody: {"board_id", "request_id"},
        api.CodeInvestigationReceiptBodyV2: {"board_id", "request_id"},
        api.CodeEvidenceBody: {"board_id"},
        api.CodeEvidenceBodyV2: {"board_id"},
        api.CodeEvidenceSupersessionBody: {
            "board_id",
            "supersedes_evidence_id",
        },
        api.CodeEvidenceSupersessionBodyV2: {
            "board_id",
            "supersedes_evidence_id",
        },
        api.LegacyEvidenceClassificationBody: {"board_id"},
        api.CodeEvidenceRevokeBody: {"board_id", "evidence_id"},
        api.CodeEvidenceSpecLinkBody: {"board_id", "spec_id"},
        api.CodeEvidenceDispositionBody: {
            "board_id",
            "spec_id",
            "evidence_id",
        },
        api.SpecCodeEvidenceRebasePreviewBody: {"board_id", "spec_id"},
        api.SpecCodeEvidenceRebaseApplyBody: {"board_id", "spec_id"},
        api.ImplementationTargetCreateBody: {"board_id", "card_id"},
        api.ImplementationTargetUpdateBody: {
            "board_id",
            "card_id",
            "target_id",
        },
        api.ImplementationTargetResolutionBody: {
            "board_id",
            "card_id",
            "target_id",
        },
        api.ImplementationTargetExecutionBody: {
            "board_id",
            "card_id",
            "target_id",
        },
        api.TargetOverlapAcknowledgementBody: {"board_id", "card_id"},
        api.CodeTraceabilityWaiverBody: {"board_id"},
    }
    for body, forbidden in expectations.items():
        assert forbidden.isdisjoint(body.model_fields)
        assert body.model_config["extra"] == "forbid"


def test_rest_error_envelope_matches_typed_core_contract() -> None:
    error = CodeInvestigationUnavailable(
        details={"reason": "agent_reported_unavailable"},
        remediation=(
            CodeTraceabilityRemediation(
                "rerun_external_agent_check",
                "okto_pulse_start_code_investigation",
            ),
        ),
    )
    projected = api._http_error(error)
    assert projected.status_code == 409
    assert projected.detail == error.to_error_dict()

    persistence_error = CodeTraceabilityRevisionConflict(
        details={"target_id": "target-1"}
    )
    projected_persistence = api._http_error(persistence_error)
    assert projected_persistence.status_code == 409
    assert projected_persistence.detail == persistence_error.to_error_dict()

    quota_error = CodeInvestigationSubmissionLimitExceeded(
        details={"reason": "open_request_limit"}
    )
    projected_quota = api._http_error(quota_error)
    assert projected_quota.status_code == 409
    assert projected_quota.detail == quota_error.to_error_dict()


def _projection_rest_app(uow: object) -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)

    async def principal() -> Principal:
        return Principal(subject="rest-user", realm_id="local")

    async def unit_of_work() -> object:
        return uow

    app.dependency_overrides[api.require_principal] = principal
    app.dependency_overrides[api.get_unit_of_work] = unit_of_work
    return app


@pytest.mark.asyncio
async def test_projection_route_maps_detail_gate_contract_error_to_409(
    monkeypatch,
) -> None:
    calls: list[object] = []

    class ProjectionUseCaseSpy:
        async def execute(self, command, **_kwargs):
            calls.append(command)
            raise AssertionError("invalid query must not reach the use case")

    monkeypatch.setattr(
        api,
        "GetCodeTraceabilityProjectionUseCase",
        ProjectionUseCaseSpy,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_projection_rest_app(object())),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/boards/board-1/code-traceability-projection",
            params={
                "subject_type": "spec",
                "subject_id": "spec-1",
                "subject_version": 7,
                "profile": "detail",
                "context_scope": "gate",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "code_traceability_gate_profile_full_required"
    )
    assert calls == []


@pytest.mark.asyncio
async def test_projection_route_accepts_full_gate_and_executes_use_case(
    monkeypatch,
) -> None:
    calls: list[tuple[object, object, object]] = []
    uow = object()

    class ProjectionUseCaseSpy:
        async def execute(self, command, *, actor, uow):
            calls.append((command, actor, uow))
            return {
                "profile": command.profile,
                "context_scope": command.context_scope,
            }

    monkeypatch.setattr(
        api,
        "GetCodeTraceabilityProjectionUseCase",
        ProjectionUseCaseSpy,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_projection_rest_app(uow)),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/boards/board-1/code-traceability-projection",
            params={
                "subject_type": "spec",
                "subject_id": "spec-1",
                "subject_version": 7,
                "profile": "full",
                "context_scope": "gate",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"profile": "full", "context_scope": "gate"}
    assert len(calls) == 1
    command, actor, received_uow = calls[0]
    assert command.profile.value == "full"
    assert command.context_scope.value == "gate"
    assert actor.board_id == "board-1"
    assert received_uow is uow


@pytest.mark.asyncio
async def test_projection_route_preserves_classification_input_payload(
    monkeypatch,
) -> None:
    class Projection:
        def as_dict(self):
            return {
                "subject_type": "refinement",
                "source_context_classification_inputs": [
                    {
                        "evidence_id": "legacy-1",
                        "expected_evidence_payload_sha256": "a" * 64,
                        "expected_classification_revision": 0,
                        "baseline_provenance": {
                            "presence": "preexisting_worktree",
                            "workspace_state_id": "workspace-dirty",
                            "provenance_note": None,
                            "provenance_note_required": True,
                        },
                    }
                ],
            }

    class ProjectionUseCaseSpy:
        async def execute(self, _command, **_kwargs):
            return Projection()

    monkeypatch.setattr(
        api,
        "GetCodeTraceabilityProjectionUseCase",
        ProjectionUseCaseSpy,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_projection_rest_app(object())),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/boards/board-1/code-traceability-projection",
            params={
                "subject_type": "refinement",
                "subject_id": "refinement-1",
                "subject_version": 3,
                "profile": "detail",
            },
        )

    assert response.status_code == 200
    assert response.json()["source_context_classification_inputs"] == (
        Projection().as_dict()["source_context_classification_inputs"]
    )


def test_cursor_is_signed_and_bound_to_board_and_filters(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(
            guideline_policy_cursor_signing_key=SecretStr("k" * 64)
        ),
    )
    cursor = CodeTraceabilityPageCursor(
        created_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        item_id="evidence-1",
    )
    binding = api._cursor_binding(
        "code_evidence",
        "board-1",
        parent_type="spec",
    )
    encoded = api._encode_cursor(cursor, binding=binding)
    assert encoded is not None
    assert api._decode_cursor(encoded, binding=binding) == cursor

    replacement = "A" if encoded[-1] != "A" else "B"
    with pytest.raises(HTTPException) as tampered:
        api._decode_cursor(encoded[:-1] + replacement, binding=binding)
    assert tampered.value.status_code == 400

    with pytest.raises(HTTPException) as cross_scope:
        api._decode_cursor(
            encoded,
            binding=api._cursor_binding(
                "code_evidence",
                "board-2",
                parent_type="spec",
            ),
        )
    assert cross_scope.value.status_code == 400


def test_cursor_decode_uses_fixed_signature_boundary(monkeypatch) -> None:
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(
            guideline_policy_cursor_signing_key=SecretStr("k" * 64)
        ),
    )
    binding = api._cursor_binding("code_evidence", "board-1")
    for index in range(1_000):
        cursor = CodeTraceabilityPageCursor(
            created_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
            item_id=f"evidence-{index}",
        )
        encoded = api._encode_cursor(cursor, binding=binding)
        assert encoded is not None
        decoded = base64.urlsafe_b64decode(
            (encoded + "=" * (-len(encoded) % 4)).encode("ascii")
        )
        if b"." in decoded[-32:]:
            assert api._decode_cursor(encoded, binding=binding) == cursor
            break
    else:
        pytest.fail("fixture did not produce a signature containing the delimiter byte")


def test_routes_are_board_scoped_and_overlap_ack_is_card_scoped() -> None:
    paths = {route.path for route in api.router.routes}
    assert paths
    assert all(path.startswith("/boards/{board_id}/") for path in paths)
    assert (
        "/boards/{board_id}/cards/{card_id}/implementation-overlaps/acknowledgements"
        in paths
    )
    assert "/boards/{board_id}/code-evidence/{evidence_id}/revoke" in paths
    assert "/boards/{board_id}/code-evidence/legacy-classifications" in paths
    assert "/boards/{board_id}/specs/{spec_id}/code-evidence/rebase/preview" in paths
    assert "/boards/{board_id}/specs/{spec_id}/code-evidence/rebase" in paths
    assert "/boards/{board_id}/implementation-overlap-acknowledgements" not in paths


def _classification_payload() -> dict[str, object]:
    return {
        "items": [
            {
                "evidence_id": "evidence-1",
                "expected_evidence_payload_sha256": "a" * 64,
                "expected_classification_revision": 0,
                "source_role": "current_implementation",
                "relevance_summary": "Existing behavior relevant to the scope.",
                "scope_relation": "Directly constrains the requested behavior.",
                "source_origin": "src/example.py",
                "baseline_provenance": {
                    "presence": "committed_snapshot",
                    "workspace_state_id": "workspace-1",
                },
            }
        ],
        "justification": "Human review of ambiguous legacy Evidence.",
        "idempotency_key": "classification-1",
    }


def _classification_rest_app(
    *,
    actor_kind: str,
    uow: object,
) -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)

    async def principal() -> Principal:
        return Principal(
            subject="reviewer-1",
            realm_id="local",
            actor_kind=actor_kind,
        )

    async def unit_of_work() -> object:
        return uow

    app.dependency_overrides[api.require_principal] = principal
    app.dependency_overrides[api.get_unit_of_work] = unit_of_work
    return app


@pytest.mark.asyncio
async def test_legacy_classification_accepts_agent_principal(
    monkeypatch,
) -> None:
    calls: list[object] = []

    class ClassificationUseCaseSpy:
        async def execute(self, command, *, actor, uow):
            calls.append((command, actor, uow))
            return {"batch_id": "batch-agent", "board_id": command.board_id}

    monkeypatch.setattr(
        api,
        "ClassifyLegacyCodeEvidenceUseCase",
        ClassificationUseCaseSpy,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_classification_rest_app(actor_kind="agent", uow=object())
        ),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/boards/board-1/code-evidence/legacy-classifications",
            json=_classification_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {"batch_id": "batch-agent", "board_id": "board-1"}
    assert len(calls) == 1
    command, actor, received_uow = calls[0]
    assert command.board_id == "board-1"
    assert actor.actor_kind == "agent"
    assert received_uow is not None


@pytest.mark.asyncio
async def test_legacy_classification_delegates_closed_board_scoped_batch(
    monkeypatch,
) -> None:
    calls: list[tuple[object, object, object]] = []
    uow = object()

    class ClassificationUseCaseSpy:
        async def execute(self, command, *, actor, uow):
            calls.append((command, actor, uow))
            return {
                "batch_id": "batch-1",
                "board_id": command.board_id,
                "replayed": False,
            }

    monkeypatch.setattr(
        api,
        "ClassifyLegacyCodeEvidenceUseCase",
        ClassificationUseCaseSpy,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_classification_rest_app(actor_kind="human", uow=uow)
        ),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/boards/board-1/code-evidence/legacy-classifications",
            json=_classification_payload(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "batch_id": "batch-1",
        "board_id": "board-1",
        "replayed": False,
    }
    assert len(calls) == 1
    command, actor, received_uow = calls[0]
    assert command.board_id == "board-1"
    assert command.items[0].evidence_id == "evidence-1"
    assert actor.actor_kind == "human"
    assert received_uow is uow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    (
        (
            CodeEvidenceLegacyClassificationPayloadConflict,
            "code_evidence_legacy_classification_payload_conflict",
        ),
        (
            CodeEvidenceLegacyClassificationRevisionConflict,
            "code_evidence_legacy_classification_revision_conflict",
        ),
        (
            CodeEvidenceLegacyClassificationIdempotencyConflict,
            "code_evidence_legacy_classification_idempotency_conflict",
        ),
    ),
)
async def test_legacy_classification_conflicts_are_distinct_typed_409s(
    monkeypatch,
    error_type,
    expected_code: str,
) -> None:
    calls: list[object] = []
    uow = SimpleNamespace(commit_count=0, events=[], heads={})

    class ClassificationUseCaseSpy:
        async def execute(self, command, **_kwargs):
            calls.append(command)
            raise error_type(details={"evidence_id": "evidence-1"})

    monkeypatch.setattr(
        api,
        "ClassifyLegacyCodeEvidenceUseCase",
        ClassificationUseCaseSpy,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_classification_rest_app(actor_kind="human", uow=uow)
        ),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/boards/board-1/code-evidence/legacy-classifications",
            json=_classification_payload(),
        )

    assert response.status_code == 409
    assert (
        response.json()["detail"]
        == error_type(details={"evidence_id": "evidence-1"}).to_error_dict()
    )
    assert response.json()["detail"]["code"] == expected_code
    assert len(calls) == 1
    assert uow.commit_count == 0
    assert uow.events == []
    assert uow.heads == {}


@pytest.mark.asyncio
async def test_legacy_classification_rejects_invalid_context_as_422() -> None:
    payload = _classification_payload()
    payload["items"][0]["source_role"] = "uncategorized_legacy"  # type: ignore[index]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_classification_rest_app(actor_kind="human", uow=object())
        ),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/boards/board-1/code-evidence/legacy-classifications",
            json=payload,
        )

    assert response.status_code == 422


def _contextual_evidence_payload() -> dict[str, object]:
    return {
        "contract_version": 2,
        "investigation_receipt_id": "receipt-1",
        "parent_type": "spec",
        "parent_id": "spec-1",
        "evidence_type": "structure",
        "claim": "The existing boundary constrains this delivery.",
        "selector": {
            "kind": "file",
            "relative_path": "src/module.py",
        },
        "declared_source_content_sha256": "b" * 64,
        "idempotency_key": "contextual-evidence-1",
        "source_role": "existing_constraint",
        "relevance_summary": "This boundary is directly relevant.",
        "scope_relation": "It constrains the target adapter.",
        "source_origin": "Committed source baseline.",
        "baseline_provenance": {
            "presence": "committed_snapshot",
            "workspace_state_id": "workspace-1",
        },
    }


@pytest.mark.parametrize(
    ("path", "use_case_name", "payload", "expected_command"),
    (
        pytest.param(
            "/boards/board-1/code-investigations/request-1/receipts",
            "SubmitCodeInvestigationReceiptUseCase",
            {
                "contract_version": 2,
                "challenge_token": "challenge",
                "outcome": "evidence_applicable",
                "capabilities": [],
                "tooling": {
                    "tool_id": "external-agent",
                    "tool_version": "2",
                    "method_id": "source-preflight/v2",
                },
                "observed_at": "2026-08-22T00:00:00Z",
                "idempotency_key": "contextual-receipt-1",
            },
            "CodeInvestigationReceiptSubmissionV2",
            id="contextual-investigation-receipt",
        ),
        pytest.param(
            "/boards/board-1/code-evidence",
            "SubmitCodeEvidenceUseCase",
            _contextual_evidence_payload(),
            "CodeEvidenceSubmissionV2",
            id="contextual-evidence",
        ),
        pytest.param(
            "/boards/board-1/code-evidence/evidence-1/supersede",
            "SupersedeCodeEvidenceUseCase",
            {
                **_contextual_evidence_payload(),
                "idempotency_key": "contextual-supersession-1",
                "supersession_reason": "The baseline meaning was refined.",
            },
            "CodeEvidenceSupersessionSubmissionV2",
            id="contextual-evidence-supersession",
        ),
    ),
)
@pytest.mark.asyncio
async def test_rest_selects_explicit_v2_command_without_adapter_semantics(
    monkeypatch,
    path: str,
    use_case_name: str,
    payload: dict[str, object],
    expected_command: str,
) -> None:
    calls: list[object] = []

    class UseCaseSpy:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def execute(self, command, **_kwargs):
            calls.append(command)
            return {"command_type": type(command).__name__}

    monkeypatch.setattr(api, use_case_name, UseCaseSpy)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_classification_rest_app(actor_kind="agent", uow=object())
        ),
        base_url="http://test",
    ) as client:
        response = await client.post(path, json=payload)

    assert response.status_code in {200, 201}, response.text
    assert response.json()["command_type"] == expected_command
    assert len(calls) == 1
    assert type(calls[0]).__name__ == expected_command
    assert getattr(calls[0], "contract_version") == 2


def test_rest_v2_openapi_contracts_are_closed_and_version_visible() -> None:
    app = FastAPI()
    app.include_router(api.router)
    schemas = app.openapi()["components"]["schemas"]
    for name in (
        "CodeInvestigationReceiptBodyV2",
        "CodeEvidenceBodyV2",
        "CodeEvidenceSupersessionBodyV2",
    ):
        assert schemas[name]["additionalProperties"] is False
        assert "contract_version" in schemas[name]["properties"]
        assert "contract_version" in schemas[name]["required"]


def test_code_traceability_community_modules_have_no_source_acquisition() -> None:
    root = Path(__file__).resolve().parents[1]
    relative_paths = (
        "src/okto_pulse/community/adapters/sqlalchemy_code_traceability.py",
        "src/okto_pulse/community/adapters/sqlalchemy_code_traceability_event_effects.py",
        "src/okto_pulse/community/adapters/relational_application.py",
        "src/okto_pulse/community/api/code_traceability.py",
        "src/okto_pulse/community/commands/code_traceability_diagnostics.py",
    )
    forbidden_import_roots = {
        "dulwich",
        "git",
        "gitpython",
        "httpx",
        "pathlib",
        "pygit2",
        "subprocess",
    }
    forbidden_calls = {
        "clone",
        "glob",
        "open",
        "popen",
        "read_text",
        "read_bytes",
        "rglob",
        "system",
        "walk",
    }
    forbidden_identifiers = {
        "ContentIngestionResolver",
        "GitRepository",
        "RepositoryReader",
        "SourceProvider",
        "WorkspaceReader",
    }

    for relative_path in relative_paths:
        source = (root / relative_path).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".", 1)[0].lower() not in forbidden_import_roots
                    for alias in node.names
                ), relative_path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert (
                    node.module.split(".", 1)[0].lower() not in forbidden_import_roots
                ), relative_path
            elif isinstance(node, ast.Call):
                called = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                )
                assert called.lower() not in forbidden_calls, (
                    relative_path,
                    called,
                )
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden_identifiers, (
                    relative_path,
                    node.id,
                )

    cli_path = root / "src/okto_pulse/community/cli.py"
    cli_tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename=str(cli_path))
    traceability_functions = [
        node
        for node in cli_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {
            "_emit_code_traceability_diagnostics",
            "cmd_code_traceability",
        }
    ]
    assert {node.name for node in traceability_functions} == {
        "_emit_code_traceability_diagnostics",
        "cmd_code_traceability",
    }
    for function in traceability_functions:
        for node in ast.walk(function):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".", 1)[0].lower() not in forbidden_import_roots
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert (
                    node.module.split(".", 1)[0].lower() not in forbidden_import_roots
                )
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden_identifiers
