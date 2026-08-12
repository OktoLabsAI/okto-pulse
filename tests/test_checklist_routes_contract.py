"""Frozen A3 checklist REST surface contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from okto_pulse.community.api import checklists, default_board_config, specs
from okto_pulse.core.application.use_cases.checklist import (
    StartChecklistExecutionUseCase,
    SubmitChecklistExecutionUseCase,
    UpdateChecklistBindingUseCase,
)
from okto_pulse.core.application.use_cases.submit_spec_validation import (
    SubmitSpecValidationUseCase,
)
from okto_pulse.core.domain.checklist import (
    SPECIFY_CHECKLIST_ITEM_IDS,
    SPECIFY_CHECKLIST_TEMPLATE_V1,
    ChecklistBinding,
    ChecklistCommitResult,
    ChecklistExecution,
    ChecklistExecutionStartResult,
    ChecklistReceipt,
    ChecklistReceiptSource,
    ChecklistMode,
)
from okto_pulse.core.services.gate_contracts import GateContractError
from okto_pulse.core.services.checklist import ChecklistConflictError
from okto_pulse.core.domain.spec_validation import (
    RequirementLintRequired,
    SpecValidationEditionConflict,
    SpecValidationGateNotReady,
    SpecValidationVersionConflict,
)


@pytest.mark.asyncio
async def test_api07_consecutive_updates_project_real_binding_revision(
    monkeypatch,
) -> None:
    observed_revisions: list[int] = []
    results = iter(
        (
            ChecklistBinding(
                board_id="board-1",
                mode=ChecklistMode.ADVISORY,
                version=1,
                revision=1,
            ),
            ChecklistBinding(
                board_id="board-1",
                mode=ChecklistMode.BLOCKING,
                version=2,
                revision=2,
            ),
        )
    )

    async def fake_execute(self, command, *, actor, uow):
        del self, actor, uow
        observed_revisions.append(command.expected_revision)
        return next(results)

    monkeypatch.setattr(UpdateChecklistBindingUseCase, "execute", fake_execute)

    first = await checklists.update_checklist_binding(
        board_id="board-1",
        target_type="spec",
        phase="spec_validation",
        data=checklists.ChecklistBindingUpdateRequest(
            mode="advisory",
            template_version_id="/specify/v1",
            expected_revision=0,
        ),
        user_id="human-1",
        realm_id=None,
        uow=object(),
    )
    assert set(first) == {"binding_id", "revision", "effective"}
    assert first["revision"] == 1
    assert first["effective"]["expected_revision"] == 1

    second = await checklists.update_checklist_binding(
        board_id="board-1",
        target_type="spec",
        phase="spec_validation",
        data=checklists.ChecklistBindingUpdateRequest(
            mode="blocking",
            template_version_id="/specify/v1",
            expected_revision=first["effective"]["expected_revision"],
        ),
        user_id="human-1",
        realm_id=None,
        uow=object(),
    )
    assert second["revision"] == 2
    assert second["effective"]["expected_revision"] == 2
    assert observed_revisions == [0, 1]


@pytest.mark.asyncio
async def test_api08_success_is_exact_and_subject_digest_includes_inputs(
    monkeypatch,
) -> None:
    execution = ChecklistExecution(
        id="execution-1",
        board_id="board-1",
        spec_id="spec-1",
        spec_version=4,
        content_digest="c" * 64,
        input_digest="d" * 64,
        template_version="/specify/v1",
        template_digest=SPECIFY_CHECKLIST_TEMPLATE_V1.digest,
        binding_version=2,
        binding_digest="b" * 64,
        binding_mode=ChecklistMode.BLOCKING,
        request_digest="e" * 64,
        idempotency_key="start-1",
        created_by="human-1",
        created_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        spec_edition=3,
    )

    async def fake_execute(self, command, *, actor, uow):
        del self, command, actor, uow
        return ChecklistExecutionStartResult(execution=execution)

    monkeypatch.setattr(StartChecklistExecutionUseCase, "execute", fake_execute)

    response = await checklists.start_checklist_execution(
        board_id="board-1",
        spec_id="spec-1",
        data=checklists.ChecklistExecutionStartRequest(
            spec_edition=3,
            expected_spec_version=4,
            binding_version=2,
        ),
        user_id="human-1",
        realm_id=None,
        uow=object(),
    )
    assert response == {
        "execution_id": "execution-1",
        "spec_edition": 3,
        "status": "started",
    }


@pytest.mark.asyncio
async def test_api09_success_is_exact_and_any_failed_item_fails_outcome(
    monkeypatch,
) -> None:
    async def fake_execute(self, command, *, actor, uow):
        del self, command, actor, uow
        return ChecklistCommitResult(
            board_id="board-1",
            spec_id="spec-1",
            spec_version=4,
            receipt_id="receipt-1",
            request_digest="f" * 64,
            head_revision=3,
            spec_edition=3,
        )

    monkeypatch.setattr(SubmitChecklistExecutionUseCase, "execute", fake_execute)
    results = [
        checklists.ChecklistItemResultRequest(
            item_id=item_id,
            outcome="fail" if index == 0 else "pass",
            anchor=f"spec://spec-1/{item_id}",
            rationale="Observed mismatch" if index == 0 else None,
        )
        for index, item_id in enumerate(SPECIFY_CHECKLIST_ITEM_IDS)
    ]
    response = await checklists.submit_checklist_execution(
        board_id="board-1",
        spec_id="spec-1",
        execution_id="execution-1",
        data=checklists.ChecklistExecutionSubmitRequest(
            spec_edition=3,
            expected_spec_version=4,
            execution_id="execution-1",
            item_results=results,
        ),
        user_id="human-1",
        realm_id=None,
        uow=object(),
    )
    assert response == {
        "result_id": "receipt-1",
        "spec_edition": 3,
        "status": "failed",
    }


def test_checklist_write_requests_are_closed() -> None:
    with pytest.raises(ValidationError):
        checklists.ChecklistBindingUpdateRequest.model_validate(
            {
                "mode": "advisory",
                "template_version_id": "/specify/v1",
                "expected_revision": 0,
                "agent_override": True,
            }
        )


def test_spec_validation_acknowledgement_is_exact_and_audit_free() -> None:
    assert set(specs.SpecValidationAcceptedResponse.model_fields) == {
        "validation_id",
        "validation_edition",
        "is_current",
    }
    route = next(
        route
        for route in specs.router.routes
        if getattr(route, "path", None) == "/specs/{spec_id}/validation"
    )
    assert route.response_model is specs.SpecValidationAcceptedResponse


@pytest.mark.parametrize(
    "projector",
    (checklists._api07_error_response, checklists._api08_error_response),
)
@pytest.mark.parametrize(
    "code",
    ("checklist_spec_edition_conflict", "checklist_binding_conflict"),
)
def test_checklist_edition_and_binding_conflicts_are_http_409(
    projector,
    code: str,
) -> None:
    response = projector(ChecklistConflictError(code))

    assert response.status_code == 409


@pytest.mark.parametrize(
    "code",
    (
        "checklist_spec_edition_conflict",
        "checklist_spec_status_conflict",
        "checklist_execution_conflict",
        "checklist_binding_conflict",
    ),
)
def test_checklist_submit_conflicts_are_http_409(code: str) -> None:
    response = checklists._api09_error_response(ChecklistConflictError(code))

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_checklist_submit_path_body_execution_mismatch_is_typed_409() -> None:
    response = await checklists.submit_checklist_execution(
        board_id="board-1",
        spec_id="spec-1",
        execution_id="execution-path",
        data=checklists.ChecklistExecutionSubmitRequest(
            spec_edition=3,
            expected_spec_version=4,
            execution_id="execution-body",
            item_results=[
                checklists.ChecklistItemResultRequest(
                    item_id=item_id,
                    outcome="pass",
                    anchor=f"spec://spec-1/{item_id}",
                )
                for item_id in SPECIFY_CHECKLIST_ITEM_IDS
            ],
        ),
        user_id="human-1",
        realm_id=None,
        uow=object(),
    )

    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["code"] == "checklist_execution_conflict"
    with pytest.raises(ValidationError):
        checklists.ChecklistExecutionSubmitRequest.model_validate(
            {
                "spec_edition": 3,
                "expected_spec_version": 4,
                "execution_id": "execution-1",
                "item_results": [
                    {
                        "item_id": item_id,
                        "outcome": "pass",
                        "anchor": f"spec://spec-1/{item_id}",
                        "unsupported": True,
                    }
                    for item_id in SPECIFY_CHECKLIST_ITEM_IDS
                ],
            }
        )


def test_default_board_checklist_mode_request_is_closed_to_canonical_values() -> None:
    accepted = default_board_config.DefaultBoardConfigVersionCreateRequest.model_validate(
        {"spec_checklist_mode": "blocking"}
    )
    assert accepted.spec_checklist_mode == "blocking"

    with pytest.raises(ValidationError):
        default_board_config.DefaultBoardConfigVersionCreateRequest.model_validate(
            {"spec_checklist_mode": "unsupported"}
        )


def test_checklist_state_distinguishes_not_started_from_failed() -> None:
    binding = SimpleNamespace(mode=ChecklistMode.BLOCKING)
    assert (
        checklists._checklist_state_status(
            SimpleNamespace(
                binding=binding,
                current_receipt=None,
                currentness=None,
            )
        )
        == "not_started"
    )
    assert (
        checklists._checklist_state_status(
            SimpleNamespace(
                binding=binding,
                current_receipt=SimpleNamespace(blocking_satisfied=False),
                currentness=SimpleNamespace(current=True),
            )
        )
        == "failed"
    )


def test_legacy_unverified_receipt_never_projects_a_vacuous_pass() -> None:
    receipt = ChecklistReceipt(
        id="receipt-legacy",
        board_id="board-1",
        spec_id="spec-1",
        spec_version=4,
        content_digest="c" * 64,
        input_digest="d" * 64,
        template_version="/specify/v1",
        template_digest=SPECIFY_CHECKLIST_TEMPLATE_V1.digest,
        binding_version=1,
        binding_digest="b" * 64,
        binding_mode=ChecklistMode.BLOCKING,
        items=(),
        source=ChecklistReceiptSource.LEGACY_UNVERIFIED,
        request_digest="f" * 64,
        created_by="legacy-import",
        created_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
        head_revision=1,
        manual_checklist_ref="legacy://manual-checklist",
    )

    assert checklists._receipt_payload(receipt)["outcome"] == "fail"


@pytest.mark.parametrize(
    "query",
    (
        "offset=abc&limit=25",
        "offset=-1&limit=25",
        "offset=0&limit=26",
        "offset=0&limit=foo",
    ),
)
def test_checklist_history_invalid_pagination_is_typed_400(query: str) -> None:
    app = FastAPI()
    app.include_router(checklists.router, prefix="/api/v1")
    app.dependency_overrides[checklists.require_user] = lambda: "human-1"
    app.dependency_overrides[checklists.get_realm_id] = lambda: None
    app.dependency_overrides[checklists.get_unit_of_work] = lambda: object()

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/boards/board-1/specs/spec-1/checklist-executions?{query}"
        )

    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "invalid_pagination"
    assert response.json()["detail"]["retryable"] is False


@pytest.mark.asyncio
async def test_checklist_history_uses_canonical_page_totals(monkeypatch) -> None:
    async def fake_execute(self, command, *, actor, uow):
        del self, actor, uow
        assert command.offset == 0
        assert command.limit == 25
        return SimpleNamespace(items=(), total=12, offset=0, limit=25)

    monkeypatch.setattr(
        checklists.ListChecklistExecutionsUseCase,
        "execute",
        fake_execute,
    )

    response = await checklists.list_checklist_executions(
        board_id="board-1",
        spec_id="spec-1",
        offset="0",
        limit="25",
        user_id="human-1",
        realm_id=None,
        uow=object(),
    )

    assert response == {
        "items": [],
        "total_filtered": 12,
        "total_overall": 12,
        "offset": 0,
        "limit": 25,
        "has_more": True,
    }


@pytest.mark.asyncio
async def test_submit_spec_validation_maps_gate_contract_like_move_spec(
    monkeypatch,
) -> None:
    async def blocked(self, command, *, actor, uow):
        del self, command, actor, uow
        raise GateContractError(
            code="spec_checklist_gate_required",
            message="Current checklist receipt is required.",
            gate_type="spec_checklist",
            entity_type="spec",
            entity_id="spec-1",
        )

    monkeypatch.setattr(SubmitSpecValidationUseCase, "execute", blocked)
    with pytest.raises(HTTPException) as exc_info:
        await specs.submit_spec_validation(
            spec_id="spec-1",
            data=specs.SpecValidationSubmit(
                expected_validation_edition=1,
                expected_spec_version=1,
                expected_head_revision=0,
                score=100,
                summary="Validation result is ready for submission.",
            ),
            user_id="human-1",
            uow=object(),
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "spec_checklist_gate_required"


@pytest.mark.asyncio
async def test_submit_spec_validation_rest_preserves_legacy_shape_without_null_formal_fields(
    monkeypatch,
) -> None:
    observed: dict[str, object] = {}

    async def capture(self, command, *, actor, uow):
        del self, actor, uow
        observed.update(command.data)
        command.validate()
        return SimpleNamespace(
            payload={
                "validation_id": "validation-1",
                "validation_edition": 1,
                "is_current": True,
            }
        )

    monkeypatch.setattr(SubmitSpecValidationUseCase, "execute", capture)
    response = await specs.submit_spec_validation(
        spec_id="spec-1",
        data=specs.SpecValidationSubmit(
            expected_validation_edition=1,
            expected_spec_version=3,
            expected_head_revision=0,
            completeness=95,
            completeness_justification="Complete enough for validation.",
            assertiveness=94,
            assertiveness_justification="Assertive enough for validation.",
            ambiguity=4,
            ambiguity_justification="Ambiguity is sufficiently low.",
            general_justification=(
                "The legacy dimensions support a human approval decision."
            ),
            recommendation="approve",
        ),
        user_id="human-1",
        uow=object(),
    )

    assert response["validation_id"] == "validation-1"
    assert "score" not in observed
    assert "summary" not in observed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "code"),
    (
        (SpecValidationEditionConflict, "spec_validation_edition_conflict"),
        (SpecValidationVersionConflict, "spec_validation_version_conflict"),
        (SpecValidationGateNotReady, "spec_validation_gate_not_ready"),
        (RequirementLintRequired, "requirement_lint_required"),
    ),
)
async def test_submit_spec_validation_preserves_typed_conflict_codes(
    monkeypatch,
    error_type,
    code: str,
) -> None:
    async def blocked(self, command, *, actor, uow):
        del self, command, actor, uow
        raise error_type()

    monkeypatch.setattr(SubmitSpecValidationUseCase, "execute", blocked)
    with pytest.raises(HTTPException) as exc_info:
        await specs.submit_spec_validation(
            spec_id="spec-1",
            data=specs.SpecValidationSubmit(
                expected_validation_edition=2,
                expected_spec_version=4,
                expected_head_revision=0,
                score=80,
                summary="Formal validation assessment is complete.",
            ),
            user_id="human-1",
            uow=object(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error_code"] == code
    assert exc_info.value.detail["code"] == code
