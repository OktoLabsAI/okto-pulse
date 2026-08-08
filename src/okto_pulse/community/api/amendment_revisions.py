"""REST endpoints for Path B AmendmentHotfixRevision records (spec be089cd3 /
card 4e7e1143 / api_aaff0d99 + api_94b12eb5 + FR1).

create / list / get / associate amendment revisions for a bug. The MCP twin tools
(ir_54ceb69b) share the same orchestrator (AmendmentRevisionApiService) so REST
and MCP never diverge. FR5: there is NO skip/override/bypass path — request models
forbid extra fields and named bypass intents are rejected with a structured error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.admin_catalog import (
    AssociateAmendmentRevisionCommand,
    AssociateAmendmentRevisionUseCase,
    CreateAmendmentRevisionCommand,
    CreateAmendmentRevisionUseCase,
    GetAmendmentRevisionCommand,
    GetAmendmentRevisionUseCase,
    ListAmendmentRevisionsCommand,
    ListAmendmentRevisionsUseCase,
    TransitionAmendmentRevisionCommand,
    TransitionAmendmentRevisionUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.core.application.use_cases import PermissionDeniedError
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.amendment_revision_api import (
    AmendmentRevisionApiError,
    reject_bypass_fields,
)

router = APIRouter()


class AmendmentRevisionCreateRequest(BaseModel):
    # extra='forbid' rejects ANY unknown field (incl. bypass-equivalents) fail-closed.
    model_config = ConfigDict(extra="forbid")

    original_spec_id: str | None = None
    initial_status: str | None = None
    revision_spec_id: str | None = None
    origin_task_ids: list[str] | None = None
    affected_task_ids: list[str] | None = None
    regression_scenario_ids: list[str] | None = None
    regression_test_task_ids: list[str] | None = None
    automated_regression_refs: list[str] | None = None


class AmendmentRevisionAssociateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    regression_scenario_ids: list[str] | None = None
    regression_test_task_ids: list[str] | None = None
    automated_regression_refs: list[str] | None = None


class AmendmentRevisionLifecycleRequest(BaseModel):
    # extra='forbid' rejects ANY unknown field, including coverage_confirmation /
    # coverage_confirmed — coverage stays validator-only via confirm_amendment_coverage.
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    lineage_state: str | None = None


def _err(exc: AmendmentRevisionApiError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.to_dict())


def _invalid_request(exc: ValidationError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "error": "invalid_request",
            "code": "invalid_request",
            "message": "Request body has unsupported or invalid fields.",
            "details": exc.errors(include_url=False),
        },
    )


@router.post("/boards/{board_id}/bugs/{bug_id}/amendment-revisions")
async def create_amendment_revision(
    board_id: str,
    bug_id: str,
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    try:
        reject_bypass_fields(raw)  # FR5 structured rejection of named bypass intents
        req = AmendmentRevisionCreateRequest.model_validate(raw)
    except AmendmentRevisionApiError as exc:
        raise _err(exc)
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await CreateAmendmentRevisionUseCase().execute(
            CreateAmendmentRevisionCommand(
                board_id, bug_id, req.model_dump(exclude_none=True)
            ),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=db,
        )
        return result.data
    except AmendmentRevisionApiError as exc:
        raise _err(exc)
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)


@router.get("/boards/{board_id}/bugs/{bug_id}/amendment-revisions")
async def list_amendment_revisions(
    board_id: str,
    bug_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    try:
        result = await ListAmendmentRevisionsUseCase().execute(
            ListAmendmentRevisionsCommand(board_id, bug_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=db,
        )
        return result.data
    except AmendmentRevisionApiError as exc:
        raise _err(exc)
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)


@router.get("/boards/{board_id}/bugs/{bug_id}/amendment-revisions/{amendment_id}")
async def get_amendment_revision(
    board_id: str,
    bug_id: str,
    amendment_id: str,
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    try:
        result = await GetAmendmentRevisionUseCase().execute(
            GetAmendmentRevisionCommand(board_id, bug_id, amendment_id),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=db,
        )
        return result.data
    except AmendmentRevisionApiError as exc:
        raise _err(exc)
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)


@router.post("/boards/{board_id}/bugs/{bug_id}/amendment-revisions/{amendment_id}/associate")
async def associate_amendment_revision_artifacts(
    board_id: str,
    bug_id: str,
    amendment_id: str,
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    try:
        reject_bypass_fields(raw)
        req = AmendmentRevisionAssociateRequest.model_validate(raw)
    except AmendmentRevisionApiError as exc:
        raise _err(exc)
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await AssociateAmendmentRevisionUseCase().execute(
            AssociateAmendmentRevisionCommand(
                board_id, bug_id, amendment_id, req.model_dump(exclude_none=True)
            ),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=db,
        )
        return result.data
    except AmendmentRevisionApiError as exc:
        raise _err(exc)
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)


@router.post("/boards/{board_id}/bugs/{bug_id}/amendment-revisions/{amendment_id}/lifecycle")
async def transition_amendment_revision(
    board_id: str,
    bug_id: str,
    amendment_id: str,
    raw: dict[str, Any] = Body(default_factory=dict),
    db: PulseUnitOfWork = Depends(get_unit_of_work),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    try:
        reject_bypass_fields(raw)  # FR5: named bypass intents rejected fail-closed
        req = AmendmentRevisionLifecycleRequest.model_validate(raw)
    except AmendmentRevisionApiError as exc:
        raise _err(exc)
    except ValidationError as exc:
        raise _invalid_request(exc)
    try:
        result = await TransitionAmendmentRevisionUseCase().execute(
            TransitionAmendmentRevisionCommand(
                board_id, bug_id, amendment_id, req.model_dump(exclude_none=True)
            ),
            actor=RESTAdapterContract.actor_from_principal(
                principal, board_id=board_id
            ),
            uow=db,
        )
        return result.data
    except AmendmentRevisionApiError as exc:
        raise _err(exc)
    except PermissionDeniedError as exc:
        raise RESTAdapterContract.http_error(exc)
