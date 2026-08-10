"""REST edge for agent-attested Code Traceability.

This module only authenticates, validates transport scope and delegates to
Core use cases.  It has no repository, source acquisition, local execution or
capability-probing behavior.  Agent-owned attestation submissions require an
authenticated agent principal; human governance mutations such as target
intent, linkage, acknowledgement, waiver and revocation remain separate from
source investigation.  The Community UI consumes bounded projections and may
invoke only those governance operations.
"""

from __future__ import annotations

import base64
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
import hashlib
import hmac
import json
from typing import Mapping

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr, create_model

from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.core import get_settings
from okto_pulse.core.application.use_cases.base import (
    EntityNotFoundError,
    PermissionDeniedError,
)
from okto_pulse.core.application.use_cases.code_traceability import (
    AcknowledgeImplementationOverlapUseCase,
    ClearCodeEvidenceDispositionUseCase,
    ClearCodeTraceabilityNotApplicableUseCase,
    CreateImplementationTargetUseCase,
    GetCodeEvidenceCommand,
    GetCodeEvidenceUseCase,
    GetCodeInvestigationReceiptCommand,
    GetCodeInvestigationReceiptUseCase,
    GetCodeTraceabilityProjectionUseCase,
    GetImplementationOverlapsUseCase,
    GetImplementationTargetCommand,
    GetImplementationTargetUseCase,
    LinkCodeEvidenceToSpecUseCase,
    ListCodeEvidenceCommand,
    ListCodeEvidenceUseCase,
    ListCodeInvestigationReceiptsUseCase,
    ListImplementationTargetsUseCase,
    MarkCodeTraceabilityNotApplicableUseCase,
    ApplySpecCodeEvidenceRebaseUseCase,
    PreviewSpecCodeEvidenceRebaseUseCase,
    RevokeCodeInvestigationReceiptCommand,
    RevokeCodeInvestigationReceiptUseCase,
    RevokeCodeEvidenceUseCase,
    SetCodeEvidenceDispositionUseCase,
    StartCodeInvestigationUseCase,
    SubmitCodeEvidenceUseCase,
    SubmitCodeInvestigationReceiptUseCase,
    SubmitImplementationTargetExecutionUseCase,
    SubmitImplementationTargetResolutionUseCase,
    SupersedeCodeEvidenceUseCase,
    UnlinkCodeEvidenceFromSpecUseCase,
    UpdateImplementationTargetUseCase,
)
from okto_pulse.core.domain.code_traceability import (
    CodeEvidenceAttestationState,
    CodeInvestigationActorKindRequired,
    CodeInvestigationOutcome,
    CodeTraceabilityContextScope,
    CodeTraceabilityLifecycleStatus,
    CodeTraceabilityPage,
    CodeTraceabilityPageCursor,
    CodeTraceabilityProjectionProfile,
    CodeTraceabilitySubjectType,
    ImplementationTargetRole,
    CodeTraceabilityContractError,
)
from okto_pulse.core.models.code_traceability import (
    CodeEvidenceDispositionClearInput,
    CodeEvidenceDispositionInput,
    CodeEvidenceRevokeInput,
    CodeEvidenceSpecLinkInput,
    CodeEvidenceSpecUnlinkInput,
    CodeEvidenceSubmission,
    CodeEvidenceSupersessionSubmission,
    CodeInvestigationReceiptSubmission,
    CodeTraceabilityWaiverClearInput,
    CodeTraceabilityWaiverInput,
    ImplementationTargetCreateInput,
    ImplementationTargetExecutionSubmission,
    ImplementationTargetResolutionSubmission,
    ImplementationTargetUpdateInput,
    StartCodeInvestigationInput,
    SpecCodeEvidenceRebaseApplyInput,
    SpecCodeEvidenceRebasePreviewInput,
    TargetOverlapAcknowledgementInput,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.code_investigation import (
    CodeInvestigationPersistenceError,
    CodeInvestigationReceiptQuery,
)
from okto_pulse.core.ports.code_traceability import (
    CodeEvidenceQuery,
    CodeTraceabilityProjectionQuery,
    ImplementationTargetQuery,
    TargetOverlapQuery,
    CodeTraceabilityPersistenceError,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.code_evidence import CodeEvidenceService
from okto_pulse.core.services.code_investigation import (
    CodeInvestigationService,
    HmacCodeInvestigationChallengePolicy,
)
from okto_pulse.core.services.implementation_targets import (
    ImplementationTargetService,
)


router = APIRouter(prefix="/boards", tags=["code-traceability"])


class _ClosedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReceiptRevocationBody(_ClosedBody):
    reason_code: str = Field(min_length=1, max_length=256)
    justification: str = Field(min_length=1, max_length=20_000)


def _transport_body(
    name: str,
    source: type[BaseModel],
    *,
    server_owned: frozenset[str],
) -> type[BaseModel]:
    """Create a closed OpenAPI body without identifiers owned by the path."""

    return create_model(
        name,
        __base__=_ClosedBody,
        **{
            field_name: (field.annotation, field)
            for field_name, field in source.model_fields.items()
            if field_name not in server_owned
        },
    )


StartCodeInvestigationBody = _transport_body(
    "StartCodeInvestigationBody",
    StartCodeInvestigationInput,
    server_owned=frozenset({"board_id"}),
)
CodeInvestigationReceiptBody = _transport_body(
    "CodeInvestigationReceiptBody",
    CodeInvestigationReceiptSubmission,
    server_owned=frozenset({"board_id", "request_id"}),
)
CodeEvidenceBody = _transport_body(
    "CodeEvidenceBody",
    CodeEvidenceSubmission,
    server_owned=frozenset({"board_id"}),
)
CodeEvidenceSupersessionBody = _transport_body(
    "CodeEvidenceSupersessionBody",
    CodeEvidenceSupersessionSubmission,
    server_owned=frozenset({"board_id", "supersedes_evidence_id"}),
)
CodeEvidenceRevokeBody = _transport_body(
    "CodeEvidenceRevokeBody",
    CodeEvidenceRevokeInput,
    server_owned=frozenset({"board_id", "evidence_id"}),
)
CodeEvidenceSpecLinkBody = _transport_body(
    "CodeEvidenceSpecLinkBody",
    CodeEvidenceSpecLinkInput,
    server_owned=frozenset({"board_id", "spec_id"}),
)
CodeEvidenceDispositionBody = _transport_body(
    "CodeEvidenceDispositionBody",
    CodeEvidenceDispositionInput,
    server_owned=frozenset({"board_id", "spec_id", "evidence_id"}),
)
SpecCodeEvidenceRebasePreviewBody = _transport_body(
    "SpecCodeEvidenceRebasePreviewBody",
    SpecCodeEvidenceRebasePreviewInput,
    server_owned=frozenset({"board_id", "spec_id"}),
)
SpecCodeEvidenceRebaseApplyBody = _transport_body(
    "SpecCodeEvidenceRebaseApplyBody",
    SpecCodeEvidenceRebaseApplyInput,
    server_owned=frozenset({"board_id", "spec_id"}),
)
ImplementationTargetCreateBody = _transport_body(
    "ImplementationTargetCreateBody",
    ImplementationTargetCreateInput,
    server_owned=frozenset({"board_id", "card_id"}),
)
ImplementationTargetUpdateBody = _transport_body(
    "ImplementationTargetUpdateBody",
    ImplementationTargetUpdateInput,
    server_owned=frozenset({"board_id", "card_id", "target_id"}),
)
ImplementationTargetResolutionBody = _transport_body(
    "ImplementationTargetResolutionBody",
    ImplementationTargetResolutionSubmission,
    server_owned=frozenset({"board_id", "card_id", "target_id"}),
)
ImplementationTargetExecutionBody = _transport_body(
    "ImplementationTargetExecutionBody",
    ImplementationTargetExecutionSubmission,
    server_owned=frozenset({"board_id", "card_id", "target_id"}),
)
TargetOverlapAcknowledgementBody = _transport_body(
    "TargetOverlapAcknowledgementBody",
    TargetOverlapAcknowledgementInput,
    server_owned=frozenset({"board_id", "card_id"}),
)
CodeTraceabilityWaiverBody = _transport_body(
    "CodeTraceabilityWaiverBody",
    CodeTraceabilityWaiverInput,
    server_owned=frozenset({"board_id"}),
)


def _command(
    model: type[BaseModel],
    body: BaseModel,
    **server_owned: object,
) -> BaseModel:
    return model.model_validate(
        {
            **body.model_dump(mode="python"),
            **server_owned,
        }
    )


def _actor(principal: Principal, board_id: str):
    return RESTAdapterContract.actor_from_principal(
        principal,
        board_id=board_id,
    )


def _require_agent_submission_principal(principal: Principal) -> None:
    """Reject human/unknown REST identities before agent-owned work is read."""

    if principal.actor_kind != "agent":
        raise _http_error(CodeInvestigationActorKindRequired())


def _investigation_service() -> CodeInvestigationService:
    """Compose the stable challenge policy; missing secret fails closed in Core."""

    configured = getattr(
        get_settings(),
        "guideline_policy_cursor_signing_key",
        None,
    )
    if isinstance(configured, SecretStr):
        raw = configured.get_secret_value()
    elif isinstance(configured, str):
        raw = configured
    else:
        raw = ""
    if len(raw.encode("utf-8")) < 32:
        return CodeInvestigationService()
    derived = hashlib.sha256(
        b"okto-pulse-code-investigation-challenge-v1\x00" + raw.encode("utf-8")
    ).digest()
    return CodeInvestigationService(
        challenge_policy=HmacCodeInvestigationChallengePolicy(
            keys={"composed-v1": derived},
            active_key_id="composed-v1",
        )
    )


def _cursor_key() -> bytes:
    configured = getattr(
        get_settings(),
        "guideline_policy_cursor_signing_key",
        None,
    )
    reveal = getattr(configured, "get_secret_value", None)
    raw = reveal() if callable(reveal) else configured
    if not isinstance(raw, str) or len(raw.encode("utf-8")) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "code_traceability_cursor_unavailable",
                "message": "Code Traceability cursor signing is unavailable.",
                "details": {},
                "remediation": [],
            },
        )
    return hashlib.sha256(
        b"okto-pulse-code-traceability-cursor-v1\x00" + raw.encode("utf-8")
    ).digest()


def _cursor_binding(kind: str, board_id: str, **filters: object) -> str:
    return json.dumps(
        {
            "kind": kind,
            "board_id": board_id,
            "filters": _native(filters),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _encode_cursor(
    cursor: CodeTraceabilityPageCursor | None,
    *,
    binding: str,
) -> str | None:
    if cursor is None:
        return None
    payload = json.dumps(
        {
            "version": 1,
            "binding_sha256": hashlib.sha256(binding.encode("utf-8")).hexdigest(),
            "created_at": cursor.created_at.isoformat(),
            "item_id": cursor.item_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(_cursor_key(), payload, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload + b"." + signature).decode("ascii").rstrip("=")
    )


def _decode_cursor(
    value: str | None,
    *,
    binding: str,
) -> CodeTraceabilityPageCursor | None:
    if value is None:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload_bytes, signature = decoded.rsplit(b".", 1)
        expected_signature = hmac.new(
            _cursor_key(),
            payload_bytes,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise ValueError
        payload = json.loads(payload_bytes.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "binding_sha256",
            "created_at",
            "item_id",
        }:
            raise ValueError
        if payload["version"] != 1 or not hmac.compare_digest(
            str(payload["binding_sha256"]),
            hashlib.sha256(binding.encode("utf-8")).hexdigest(),
        ):
            raise ValueError
        return CodeTraceabilityPageCursor(
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            item_id=str(payload["item_id"]),
        )
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "code_traceability_cursor_invalid"},
        ) from exc


def _native(value: object, *, cursor_binding: str | None = None) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, SecretStr):
        return "**********"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _native(item, cursor_binding=cursor_binding)
            for key, item in value.items()
        }
    if isinstance(value, tuple | list | set | frozenset):
        return [_native(item, cursor_binding=cursor_binding) for item in value]
    if isinstance(value, CodeTraceabilityPage):
        if cursor_binding is None:
            raise TypeError("code_traceability_rest_cursor_binding_required")
        return {
            "items": [
                _native(item, cursor_binding=cursor_binding) for item in value.items
            ],
            "limit": value.limit,
            "next_cursor": _encode_cursor(
                value.next_cursor,
                binding=cursor_binding,
            ),
            "has_more": value.next_cursor is not None,
        }
    if hasattr(value, "as_dict"):
        return _native(value.as_dict(), cursor_binding=cursor_binding)
    if hasattr(value, "model_dump"):
        return _native(
            value.model_dump(mode="json", exclude_none=True),
            cursor_binding=cursor_binding,
        )
    if is_dataclass(value):
        return {
            field.name: _native(
                getattr(value, field.name),
                cursor_binding=cursor_binding,
            )
            for field in fields(value)
            if getattr(value, field.name) is not None
        }
    raise TypeError(
        f"code_traceability_rest_projection_unsupported:{type(value).__name__}"
    )


def _http_error(exc: Exception) -> HTTPException:
    """Project the same typed envelope exposed by the Core MCP adapter."""

    if isinstance(exc, CodeInvestigationActorKindRequired):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=exc.to_error_dict(),
        )
    if isinstance(exc, CodeTraceabilityContractError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.to_error_dict(),
        )
    if isinstance(
        exc,
        CodeInvestigationPersistenceError | CodeTraceabilityPersistenceError,
    ):
        projected = exc.to_error_dict()
        code = str(projected.get("code") or "")
        conflict = any(
            marker in code
            for marker in (
                "conflict",
                "immutable",
                "revision",
                "idempotency",
            )
        )
        return HTTPException(
            status_code=(
                status.HTTP_409_CONFLICT
                if conflict
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            detail=projected,
        )
    if isinstance(exc, PermissionDeniedError):
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "forbidden",
                "message": "Code Traceability permission denied.",
                "details": {},
                "remediation": [],
            },
        )
    if isinstance(exc, EntityNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "not_found",
                "message": str(exc),
                "details": {},
                "remediation": [],
            },
        )
    return RESTAdapterContract.http_error(exc)


async def _execute(
    use_case: object,
    command: object,
    *,
    board_id: str,
    principal: Principal,
    uow: PulseUnitOfWork,
    cursor_binding: str | None = None,
) -> object:
    try:
        result = await getattr(use_case, "execute")(
            command,
            actor=_actor(principal, board_id),
            uow=uow,
        )
    except (
        CodeTraceabilityContractError,
        CodeInvestigationPersistenceError,
        CodeTraceabilityPersistenceError,
        EntityNotFoundError,
        PermissionDeniedError,
        ValueError,
    ) as exc:
        raise _http_error(exc) from exc
    return _native(result, cursor_binding=cursor_binding)


@router.post("/{board_id}/code-investigations", status_code=status.HTTP_201_CREATED)
async def start_code_investigation(
    board_id: str,
    body: StartCodeInvestigationBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    _require_agent_submission_principal(principal)
    return await _execute(
        StartCodeInvestigationUseCase(_investigation_service()),
        _command(StartCodeInvestigationInput, body, board_id=board_id),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post(
    "/{board_id}/code-investigations/{request_id}/receipts",
    status_code=status.HTTP_201_CREATED,
)
async def submit_code_investigation_receipt(
    board_id: str,
    request_id: str,
    body: CodeInvestigationReceiptBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    _require_agent_submission_principal(principal)
    return await _execute(
        SubmitCodeInvestigationReceiptUseCase(_investigation_service()),
        _command(
            CodeInvestigationReceiptSubmission,
            body,
            board_id=board_id,
            request_id=request_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.get("/{board_id}/code-investigation-receipts")
async def list_code_investigation_receipts(
    board_id: str,
    subject_type: CodeTraceabilitySubjectType | None = None,
    subject_id: str | None = None,
    source_ref: str | None = None,
    outcome: CodeInvestigationOutcome | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    binding = _cursor_binding(
        "code_investigation_receipts",
        board_id,
        subject_type=subject_type,
        subject_id=subject_id,
        source_ref=source_ref,
        outcome=outcome,
    )
    return await _execute(
        ListCodeInvestigationReceiptsUseCase(),
        CodeInvestigationReceiptQuery(
            board_id=board_id,
            subject_type=subject_type,
            subject_id=subject_id,
            source_ref=source_ref,
            outcome=outcome,
            cursor=_decode_cursor(cursor, binding=binding),
            limit=limit,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
        cursor_binding=binding,
    )


@router.get("/{board_id}/code-investigation-receipts/{receipt_id}")
async def get_code_investigation_receipt(
    board_id: str,
    receipt_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        GetCodeInvestigationReceiptUseCase(_investigation_service()),
        GetCodeInvestigationReceiptCommand(board_id, receipt_id),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/code-investigation-receipts/{receipt_id}/revoke")
async def revoke_code_investigation_receipt(
    board_id: str,
    receipt_id: str,
    body: ReceiptRevocationBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        RevokeCodeInvestigationReceiptUseCase(_investigation_service()),
        RevokeCodeInvestigationReceiptCommand(
            board_id,
            receipt_id,
            body.reason_code,
            body.justification,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/code-evidence", status_code=status.HTTP_201_CREATED)
async def submit_code_evidence(
    board_id: str,
    body: CodeEvidenceBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    _require_agent_submission_principal(principal)
    return await _execute(
        SubmitCodeEvidenceUseCase(
            _investigation_service(),
            CodeEvidenceService(),
        ),
        _command(CodeEvidenceSubmission, body, board_id=board_id),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.get("/{board_id}/code-evidence")
async def list_code_evidence(
    board_id: str,
    parent_type: CodeTraceabilitySubjectType | None = None,
    parent_id: str | None = None,
    lifecycle_status: CodeTraceabilityLifecycleStatus | None = Query(
        None,
        alias="status",
    ),
    attestation_state: CodeEvidenceAttestationState | None = None,
    profile: CodeTraceabilityProjectionProfile = (
        CodeTraceabilityProjectionProfile.SUMMARY
    ),
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    binding = _cursor_binding(
        "code_evidence",
        board_id,
        parent_type=parent_type,
        parent_id=parent_id,
        lifecycle_status=lifecycle_status,
        attestation_state=attestation_state,
        profile=profile,
    )
    return await _execute(
        ListCodeEvidenceUseCase(),
        ListCodeEvidenceCommand(
            CodeEvidenceQuery(
                board_id=board_id,
                parent_type=parent_type,
                parent_id=parent_id,
                lifecycle_status=lifecycle_status,
                attestation_state=attestation_state,
                cursor=_decode_cursor(cursor, binding=binding),
                limit=limit,
            ),
            profile,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
        cursor_binding=binding,
    )


@router.get("/{board_id}/code-evidence/{evidence_id}")
async def get_code_evidence(
    board_id: str,
    evidence_id: str,
    profile: CodeTraceabilityProjectionProfile = (
        CodeTraceabilityProjectionProfile.DETAIL
    ),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        GetCodeEvidenceUseCase(),
        GetCodeEvidenceCommand(board_id, evidence_id, profile),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/code-evidence/{evidence_id}/supersede")
async def supersede_code_evidence(
    board_id: str,
    evidence_id: str,
    body: CodeEvidenceSupersessionBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    _require_agent_submission_principal(principal)
    return await _execute(
        SupersedeCodeEvidenceUseCase(
            _investigation_service(),
            CodeEvidenceService(),
        ),
        _command(
            CodeEvidenceSupersessionSubmission,
            body,
            board_id=board_id,
            supersedes_evidence_id=evidence_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/code-evidence/{evidence_id}/revoke")
async def revoke_code_evidence(
    board_id: str,
    evidence_id: str,
    body: CodeEvidenceRevokeBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        RevokeCodeEvidenceUseCase(),
        _command(
            CodeEvidenceRevokeInput,
            body,
            board_id=board_id,
            evidence_id=evidence_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/specs/{spec_id}/code-evidence-links")
async def link_code_evidence(
    board_id: str,
    spec_id: str,
    body: CodeEvidenceSpecLinkBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        LinkCodeEvidenceToSpecUseCase(CodeEvidenceService()),
        _command(
            CodeEvidenceSpecLinkInput,
            body,
            board_id=board_id,
            spec_id=spec_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.delete("/{board_id}/specs/{spec_id}/code-evidence-links/{link_id}")
async def unlink_code_evidence(
    board_id: str,
    spec_id: str,
    link_id: str,
    expected_spec_version: int = Query(..., ge=1),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        UnlinkCodeEvidenceFromSpecUseCase(CodeEvidenceService()),
        CodeEvidenceSpecUnlinkInput(
            board_id=board_id,
            spec_id=spec_id,
            link_id=link_id,
            expected_spec_version=expected_spec_version,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.put("/{board_id}/specs/{spec_id}/code-evidence/{evidence_id}/disposition")
async def set_code_evidence_disposition(
    board_id: str,
    spec_id: str,
    evidence_id: str,
    body: CodeEvidenceDispositionBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        SetCodeEvidenceDispositionUseCase(CodeEvidenceService()),
        _command(
            CodeEvidenceDispositionInput,
            body,
            board_id=board_id,
            spec_id=spec_id,
            evidence_id=evidence_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.delete("/{board_id}/specs/{spec_id}/code-evidence/{evidence_id}/disposition")
async def clear_code_evidence_disposition(
    board_id: str,
    spec_id: str,
    evidence_id: str,
    expected_spec_version: int = Query(..., ge=1),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        ClearCodeEvidenceDispositionUseCase(CodeEvidenceService()),
        CodeEvidenceDispositionClearInput(
            board_id=board_id,
            spec_id=spec_id,
            evidence_id=evidence_id,
            expected_spec_version=expected_spec_version,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/specs/{spec_id}/code-evidence/rebase/preview")
async def preview_spec_code_evidence_rebase(
    board_id: str,
    spec_id: str,
    body: SpecCodeEvidenceRebasePreviewBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        PreviewSpecCodeEvidenceRebaseUseCase(),
        _command(
            SpecCodeEvidenceRebasePreviewInput,
            body,
            board_id=board_id,
            spec_id=spec_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/specs/{spec_id}/code-evidence/rebase")
async def apply_spec_code_evidence_rebase(
    board_id: str,
    spec_id: str,
    body: SpecCodeEvidenceRebaseApplyBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        ApplySpecCodeEvidenceRebaseUseCase(),
        _command(
            SpecCodeEvidenceRebaseApplyInput,
            body,
            board_id=board_id,
            spec_id=spec_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/cards/{card_id}/implementation-targets")
async def create_implementation_target(
    board_id: str,
    card_id: str,
    body: ImplementationTargetCreateBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        CreateImplementationTargetUseCase(
            _investigation_service(),
            ImplementationTargetService(),
        ),
        _command(
            ImplementationTargetCreateInput,
            body,
            board_id=board_id,
            card_id=card_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.get("/{board_id}/implementation-targets")
async def list_implementation_targets(
    board_id: str,
    card_id: str | None = None,
    source_ref: str | None = None,
    lifecycle_status: CodeTraceabilityLifecycleStatus | None = None,
    role: ImplementationTargetRole | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    binding = _cursor_binding(
        "implementation_targets",
        board_id,
        card_id=card_id,
        source_ref=source_ref,
        lifecycle_status=lifecycle_status,
        role=role,
    )
    return await _execute(
        ListImplementationTargetsUseCase(),
        ImplementationTargetQuery(
            board_id=board_id,
            card_id=card_id,
            source_ref=source_ref,
            lifecycle_status=lifecycle_status,
            role=role,
            cursor=_decode_cursor(cursor, binding=binding),
            limit=limit,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
        cursor_binding=binding,
    )


@router.get("/{board_id}/implementation-targets/{target_id}")
async def get_implementation_target(
    board_id: str,
    target_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        GetImplementationTargetUseCase(),
        GetImplementationTargetCommand(board_id, target_id),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.patch("/{board_id}/cards/{card_id}/implementation-targets/{target_id}")
async def update_implementation_target(
    board_id: str,
    card_id: str,
    target_id: str,
    body: ImplementationTargetUpdateBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        UpdateImplementationTargetUseCase(ImplementationTargetService()),
        _command(
            ImplementationTargetUpdateInput,
            body,
            board_id=board_id,
            card_id=card_id,
            target_id=target_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post(
    "/{board_id}/cards/{card_id}/implementation-targets/{target_id}/resolution-receipts"
)
async def submit_implementation_target_resolution(
    board_id: str,
    card_id: str,
    target_id: str,
    body: ImplementationTargetResolutionBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    _require_agent_submission_principal(principal)
    return await _execute(
        SubmitImplementationTargetResolutionUseCase(
            _investigation_service(),
            ImplementationTargetService(),
        ),
        _command(
            ImplementationTargetResolutionSubmission,
            body,
            board_id=board_id,
            card_id=card_id,
            target_id=target_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post(
    "/{board_id}/cards/{card_id}/implementation-targets/{target_id}/execution-receipts"
)
async def submit_implementation_target_execution(
    board_id: str,
    card_id: str,
    target_id: str,
    body: ImplementationTargetExecutionBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    _require_agent_submission_principal(principal)
    return await _execute(
        SubmitImplementationTargetExecutionUseCase(
            _investigation_service(),
            ImplementationTargetService(),
        ),
        _command(
            ImplementationTargetExecutionSubmission,
            body,
            board_id=board_id,
            card_id=card_id,
            target_id=target_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.get("/{board_id}/cards/{card_id}/implementation-overlaps")
async def get_implementation_overlaps(
    board_id: str,
    card_id: str,
    include_informational: bool = True,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        GetImplementationOverlapsUseCase(),
        TargetOverlapQuery(
            board_id=board_id,
            card_id=card_id,
            include_informational=include_informational,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/cards/{card_id}/implementation-overlaps/acknowledgements")
async def acknowledge_implementation_overlap(
    board_id: str,
    card_id: str,
    body: TargetOverlapAcknowledgementBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        AcknowledgeImplementationOverlapUseCase(),
        _command(
            TargetOverlapAcknowledgementInput,
            body,
            board_id=board_id,
            card_id=card_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.post("/{board_id}/code-traceability-waivers")
async def mark_code_traceability_not_applicable(
    board_id: str,
    body: CodeTraceabilityWaiverBody,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        MarkCodeTraceabilityNotApplicableUseCase(),
        _command(CodeTraceabilityWaiverInput, body, board_id=board_id),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.delete("/{board_id}/code-traceability-waivers/{waiver_id}")
async def clear_code_traceability_not_applicable(
    board_id: str,
    waiver_id: str,
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        ClearCodeTraceabilityNotApplicableUseCase(),
        CodeTraceabilityWaiverClearInput(
            board_id=board_id,
            waiver_id=waiver_id,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


@router.get("/{board_id}/code-traceability-projection")
async def get_code_traceability_projection(
    board_id: str,
    subject_type: CodeTraceabilitySubjectType,
    subject_id: str,
    subject_version: int = Query(..., ge=1),
    profile: CodeTraceabilityProjectionProfile = (
        CodeTraceabilityProjectionProfile.SUMMARY
    ),
    context_scope: CodeTraceabilityContextScope = (
        CodeTraceabilityContextScope.DEFAULT
    ),
    principal: Principal = Depends(require_principal),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> object:
    return await _execute(
        GetCodeTraceabilityProjectionUseCase(),
        CodeTraceabilityProjectionQuery(
            board_id=board_id,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_version=subject_version,
            profile=profile,
            context_scope=context_scope,
        ),
        board_id=board_id,
        principal=principal,
        uow=uow,
    )


__all__ = ["router"]
