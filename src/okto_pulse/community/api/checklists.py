"""Human REST surfaces for the curated A3 Spec checklist."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from okto_pulse.community.api.auth_deps import get_realm_id, require_user
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core.application.use_cases.base import (
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.checklist import (
    GetChecklistBindingCommand,
    GetChecklistBindingUseCase,
    GetChecklistReceiptCommand,
    GetChecklistReceiptUseCase,
    GetChecklistSpecStateCommand,
    GetChecklistSpecStateUseCase,
    GetChecklistTemplateCommand,
    GetChecklistTemplateUseCase,
    ListChecklistExecutionsCommand,
    ListChecklistExecutionsUseCase,
    ListChecklistTemplatesUseCase,
    StartChecklistExecutionCommand,
    StartChecklistExecutionUseCase,
    SubmitChecklistExecutionCommand,
    SubmitChecklistExecutionUseCase,
    UpdateChecklistBindingCommand,
    UpdateChecklistBindingUseCase,
    ChecklistSpecState,
)
from okto_pulse.core.domain.checklist import (
    SPECIFY_CHECKLIST_TEMPLATE_V1,
    SPECIFY_CHECKLIST_TEMPLATE_VERSION,
    ChecklistBinding,
    ChecklistContractError,
    ChecklistExecution,
    ChecklistItemOutcome,
    ChecklistItemResult,
    ChecklistMode,
    ChecklistReceipt,
    ChecklistReceiptView,
    ChecklistTemplate,
)
from okto_pulse.core.inbound.ska_contract_error import (
    project_ska_contract_error,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.checklist import (
    ChecklistError,
    ChecklistErrorCategory,
)

router = APIRouter()


class ChecklistBindingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: ChecklistMode
    template_version_id: Literal["/specify/v1"]
    expected_revision: int = Field(ge=0)


class ChecklistExecutionStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str = Field(min_length=64, max_length=64)
    expected_spec_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)


class ChecklistItemResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=64)
    outcome: ChecklistItemOutcome
    anchor: str = Field(min_length=1)
    rationale: str | None = None


class ChecklistExecutionSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_execution_revision: int = Field(ge=1)
    results: list[ChecklistItemResultRequest] = Field(min_length=10, max_length=10)
    idempotency_key: str = Field(min_length=1, max_length=255)


def _checklist_error_response(exc: ChecklistError | ChecklistContractError):
    if isinstance(exc, ChecklistContractError):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": exc.code,
                "code": exc.code,
                "message": exc.message,
            },
        )
    status_by_category = {
        ChecklistErrorCategory.INVALID_ARGUMENT: status.HTTP_400_BAD_REQUEST,
        ChecklistErrorCategory.UNPROCESSABLE: status.HTTP_422_UNPROCESSABLE_CONTENT,
        ChecklistErrorCategory.NOT_FOUND: status.HTTP_404_NOT_FOUND,
        ChecklistErrorCategory.CONFLICT: status.HTTP_409_CONFLICT,
        ChecklistErrorCategory.INTERNAL: status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
    return JSONResponse(
        status_code=status_by_category[exc.category],
        content=exc.to_dict(),
    )


def _parse_checklist_pagination(
    offset: str | int,
    limit: str | int,
) -> tuple[int, int]:
    values: dict[str, int] = {}
    for field, raw in (("offset", offset), ("limit", limit)):
        try:
            values[field] = int(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": "invalid_pagination",
                    "message": f"checklist_execution_page_{field}_invalid",
                    "retryable": False,
                },
            ) from exc
    if values["offset"] < 0 or values["limit"] not in {25, 50, 100}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "invalid_pagination",
                "message": "checklist_execution_pagination_invalid",
                "retryable": False,
                "allowed_limits": [25, 50, 100],
            },
        )
    return values["offset"], values["limit"]


def _frozen_contract_error_response(
    *,
    code: str,
    http_status: int,
    retryable: bool,
    source_code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> JSONResponse:
    """Project internal A3 errors onto the frozen public API vocabulary."""

    return JSONResponse(
        status_code=http_status,
        content={
            "error": code,
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": (
                details
                if details is not None
                else {"reason_code": source_code}
            ),
        },
    )


def _project_checklist_contract_error(
    exc: ChecklistError | ChecklistContractError,
) -> tuple[str, bool, dict[str, object], str]:
    projected = project_ska_contract_error(exc, family="checklist")
    raw_code = str(getattr(exc, "code", "") or str(exc))
    code = str(projected["code"])
    message = str(exc)
    if message == raw_code and code != raw_code:
        message = code
    return (
        code,
        bool(projected["retryable"]),
        dict(projected["details"]),
        message,
    )


def _api07_error_response(
    exc: ChecklistError | ChecklistContractError,
) -> JSONResponse:
    code, retryable, details, message = _project_checklist_contract_error(
        exc
    )
    return _frozen_contract_error_response(
        code=code,
        http_status=(
            status.HTTP_409_CONFLICT
            if code in {"version_conflict", "checklist_binding_off"}
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        ),
        retryable=retryable,
        source_code=str(exc.code),
        message=message,
        details=details,
    )


def _api08_error_response(
    exc: ChecklistError | ChecklistContractError,
) -> JSONResponse:
    code, retryable, details, message = _project_checklist_contract_error(
        exc
    )
    return _frozen_contract_error_response(
        code=code,
        http_status=(
            status.HTTP_409_CONFLICT
            if code in {"version_conflict", "checklist_binding_off"}
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        ),
        retryable=retryable,
        source_code=str(exc.code),
        message=message,
        details=details,
    )


def _api09_error_response(
    exc: ChecklistError | ChecklistContractError,
) -> JSONResponse:
    code, retryable, details, message = _project_checklist_contract_error(
        exc
    )
    return _frozen_contract_error_response(
        code=code,
        http_status=(
            status.HTTP_409_CONFLICT
            if code in {"version_conflict", "checklist_binding_off"}
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        ),
        retryable=retryable,
        source_code=str(exc.code),
        message=message,
        details=details,
    )


def _forbidden_response(exc: PermissionDeniedError) -> JSONResponse:
    source_code = str(exc)
    code = (
        "human_actor_required"
        if source_code == "human_actor_required"
        else "forbidden"
    )
    return _frozen_contract_error_response(
        code=code,
        http_status=status.HTTP_403_FORBIDDEN,
        retryable=False,
        source_code=source_code,
        message=source_code,
    )


def _template_payload(template: ChecklistTemplate) -> dict[str, object]:
    return {
        "template_id": template.template_id,
        "version": template.version,
        "digest": template.digest,
        "items": [
            {
                "item_id": item.item_id,
                "title_en": item.title_en,
                "title_pt": item.title_pt,
                "description_en": item.description_en,
                "description_pt": item.description_pt,
                "allow_na": item.allow_na,
            }
            for item in template.items
        ],
    }


def _binding_payload(binding: ChecklistBinding) -> dict[str, object]:
    return {
        "id": binding.digest,
        "board_id": binding.board_id,
        "target_type": binding.target_type.value,
        "phase": binding.phase.value,
        "mode": binding.mode.value,
        "version": binding.version,
        "expected_revision": binding.revision,
        "digest": binding.digest,
        "template_version_id": binding.template_version,
    }


def _execution_payload(
    execution: ChecklistExecution,
    *,
    replayed: bool,
) -> dict[str, object]:
    return {
        "id": execution.id,
        "board_id": execution.board_id,
        "spec_id": execution.spec_id,
        "spec_version": execution.spec_version,
        "content_digest": execution.content_digest,
        "input_digest": execution.input_digest,
        "template_version_id": execution.template_version,
        "template_digest": execution.template_digest,
        "binding_version": execution.binding_version,
        "binding_id": execution.binding_digest,
        "binding_mode": execution.binding_mode.value,
        "revision": execution.revision,
        "status": execution.status.value,
        "receipt_id": execution.receipt_id,
        "created_by": execution.created_by,
        "created_at": execution.created_at.isoformat(),
        "replayed": replayed,
    }


def _receipt_payload(receipt: ChecklistReceipt) -> dict[str, object]:
    return {
        "id": receipt.id,
        "board_id": receipt.board_id,
        "spec_id": receipt.spec_id,
        "spec_version": receipt.spec_version,
        "content_digest": receipt.content_digest,
        "input_digest": receipt.input_digest,
        "template_version_id": receipt.template_version,
        "template_digest": receipt.template_digest,
        "binding_version": receipt.binding_version,
        "binding_id": receipt.binding_digest,
        "binding_mode": receipt.binding_mode.value,
        "source": receipt.source.value,
        "request_digest": receipt.request_digest,
        "head_revision": receipt.head_revision,
        "predecessor_receipt_id": receipt.predecessor_receipt_id,
        "created_by": receipt.created_by,
        "created_at": receipt.created_at.isoformat(),
        "outcome": "pass" if receipt.blocking_satisfied else "fail",
        "results": [
            {
                "item_id": item.item_id,
                "outcome": item.outcome.value,
                "anchor": item.anchor,
                "rationale": item.rationale,
            }
            for item in receipt.items
        ],
        "blocking_satisfied": receipt.blocking_satisfied,
    }


def _currentness_payload(currentness) -> dict[str, object] | None:
    if currentness is None:
        return None
    return {
        "current": currentness.current,
        "stale_reasons": [reason.value for reason in currentness.stale_reasons],
    }


def _gate_payload(gate) -> dict[str, object]:
    return {
        "mode": gate.mode.value,
        "allowed": gate.allowed,
        "reason": gate.reason,
        "currentness": _currentness_payload(gate.currentness),
    }


def _checklist_state_status(state: ChecklistSpecState) -> str:
    receipt = state.current_receipt
    if state.binding.mode is ChecklistMode.OFF:
        return "off"
    if receipt is None:
        return "not_started"
    if state.currentness is not None and not state.currentness.current:
        return "stale"
    if not receipt.blocking_satisfied:
        return "failed"
    return "current"


def _state_payload(state: ChecklistSpecState) -> dict[str, object]:
    receipt = state.current_receipt
    return {
        "status": _checklist_state_status(state),
        "subject": {
            "board_id": state.subject.board_id,
            "spec_id": state.subject.spec_id,
            "spec_version": state.subject.spec_version,
            "content_digest": state.subject.content_digest,
            "input_digest": state.subject.input_digest,
            "status": state.subject.status,
            "archived": state.subject.archived,
        },
        "binding": _binding_payload(state.binding),
        "current_receipt": (
            None if receipt is None else _receipt_payload(receipt)
        ),
        "currentness": _currentness_payload(state.currentness),
        "gate": _gate_payload(state.gate),
    }


def _receipt_view_payload(view: ChecklistReceiptView) -> dict[str, object]:
    return {
        "receipt": _receipt_payload(view.receipt),
        "is_head": view.is_head,
        "currentness": _currentness_payload(view.currentness),
        "gate": _gate_payload(view.gate),
    }


@router.get("/checklist-templates")
async def list_checklist_templates(
    _user_id: str = Depends(require_user),
):
    templates = await ListChecklistTemplatesUseCase.execute()
    return {"items": [_template_payload(item) for item in templates], "total": 1}


@router.get("/checklist-templates/{template_version:path}")
async def get_checklist_template(
    template_version: str,
    _user_id: str = Depends(require_user),
):
    # A leading slash is part of the immutable identity but cannot survive as
    # the first path segment. Accept the canonical suffix and restore it here.
    normalized = (
        template_version
        if template_version.startswith("/")
        else f"/{template_version}"
    )
    try:
        template = await GetChecklistTemplateUseCase.execute(
            GetChecklistTemplateCommand(normalized)
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return _template_payload(template)


@router.get(
    "/boards/{board_id}/checklist-bindings/{target_type}/{phase}",
)
async def get_checklist_binding(
    board_id: str,
    target_type: Literal["spec"],
    phase: Literal["spec_validation"],
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    del target_type, phase
    try:
        binding = await GetChecklistBindingUseCase().execute(
            GetChecklistBindingCommand(board_id),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    return _binding_payload(binding)


@router.put(
    "/boards/{board_id}/checklist-bindings/{target_type}/{phase}",
)
async def update_checklist_binding(
    board_id: str,
    target_type: Literal["spec"],
    phase: Literal["spec_validation"],
    data: ChecklistBindingUpdateRequest,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    del target_type, phase
    if data.template_version_id != SPECIFY_CHECKLIST_TEMPLATE_VERSION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="checklist_template_version_unsupported",
        )
    try:
        binding = await UpdateChecklistBindingUseCase().execute(
            UpdateChecklistBindingCommand(
                board_id=board_id,
                mode=data.mode,
                expected_revision=data.expected_revision,
            ),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except (ChecklistError, ChecklistContractError) as exc:
        return _api07_error_response(exc)
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionDeniedError as exc:
        return _forbidden_response(exc)
    return {
        "binding_id": binding.digest,
        "revision": binding.revision,
        "effective": _binding_payload(binding),
    }


@router.get("/boards/{board_id}/specs/{spec_id}/checklist-state")
async def get_checklist_spec_state(
    board_id: str,
    spec_id: str,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        state_view = await GetChecklistSpecStateUseCase().execute(
            GetChecklistSpecStateCommand(
                board_id=board_id,
                spec_id=spec_id,
            ),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except (ChecklistError, ChecklistContractError) as exc:
        return _checklist_error_response(exc)
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    return _state_payload(state_view)


@router.post(
    "/boards/{board_id}/specs/{spec_id}/checklist-executions",
    status_code=status.HTTP_201_CREATED,
)
async def start_checklist_execution(
    board_id: str,
    spec_id: str,
    data: ChecklistExecutionStartRequest,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        result = await StartChecklistExecutionUseCase().execute(
            StartChecklistExecutionCommand(
                board_id=board_id,
                spec_id=spec_id,
                expected_spec_version=data.expected_spec_version,
                binding_digest=data.binding_id,
                idempotency_key=data.idempotency_key,
            ),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except (ChecklistError, ChecklistContractError) as exc:
        return _api08_error_response(exc)
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionDeniedError as exc:
        return _forbidden_response(exc)
    execution = result.execution
    return {
        "execution_id": execution.id,
        "items": _template_payload(SPECIFY_CHECKLIST_TEMPLATE_V1)["items"],
        # The input digest includes both semantic content and clarification
        # identity, so it is the complete frozen subject identity.
        "subject_digest": execution.input_digest,
        "template_digest": execution.template_digest,
    }


@router.get("/boards/{board_id}/specs/{spec_id}/checklist-executions")
async def list_checklist_executions(
    board_id: str,
    spec_id: str,
    offset: str = Query(default="0"),
    limit: str = Query(default="25"),
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    resolved_offset, resolved_limit = _parse_checklist_pagination(offset, limit)
    try:
        page = await ListChecklistExecutionsUseCase().execute(
            ListChecklistExecutionsCommand(
                board_id=board_id,
                spec_id=spec_id,
                offset=resolved_offset,
                limit=resolved_limit,
            ),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except (ChecklistError, ChecklistContractError) as exc:
        return _checklist_error_response(exc)
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    return {
        "items": [_receipt_view_payload(item) for item in page.items],
        "total_filtered": page.total,
        "total_overall": page.total,
        "offset": page.offset,
        "limit": page.limit,
    }


@router.post(
    "/boards/{board_id}/specs/{spec_id}/checklist-executions/"
    "{execution_id}/submit",
)
async def submit_checklist_execution(
    board_id: str,
    spec_id: str,
    execution_id: str,
    data: ChecklistExecutionSubmitRequest,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        result = await SubmitChecklistExecutionUseCase().execute(
            SubmitChecklistExecutionCommand(
                board_id=board_id,
                spec_id=spec_id,
                execution_id=execution_id,
                expected_execution_revision=data.expected_execution_revision,
                items=tuple(
                    ChecklistItemResult(
                        item_id=item.item_id,
                        outcome=item.outcome,
                        anchor=item.anchor,
                        rationale=item.rationale,
                    )
                    for item in data.results
                ),
                idempotency_key=data.idempotency_key,
            ),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except (ChecklistError, ChecklistContractError) as exc:
        return _api09_error_response(exc)
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionDeniedError as exc:
        return _forbidden_response(exc)
    return {
        "receipt_id": result.receipt_id,
        "outcome": (
            "fail"
            if any(
                item.outcome is ChecklistItemOutcome.FAIL
                for item in data.results
            )
            else "pass"
        ),
        "head_revision": result.head_revision,
    }


@router.get("/boards/{board_id}/checklist-receipts/{receipt_id}")
async def get_checklist_receipt(
    board_id: str,
    receipt_id: str,
    user_id: str = Depends(require_user),
    realm_id: str | None = Depends(get_realm_id),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    try:
        receipt = await GetChecklistReceiptUseCase().execute(
            GetChecklistReceiptCommand(
                board_id=board_id,
                receipt_id=receipt_id,
            ),
            actor=RESTAdapterContract.actor(user_id, realm_id=realm_id),
            uow=uow,
        )
    except (ChecklistError, ChecklistContractError) as exc:
        return _checklist_error_response(exc)
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PermissionDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(exc),
        ) from exc
    return _receipt_payload(receipt)


__all__ = ["router"]
