"""Permission-aware Markdown and standalone HTML entity reports."""

from __future__ import annotations

import os
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from okto_pulse.community.adapters.sqlalchemy_entity_export import (
    CommunityEntityExportLimitError,
    CommunityEntityExportRequestError,
)
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work_factory
from okto_pulse.community.api.download_headers import (
    attachment_content_disposition,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.services.entity_export_renderer import (
    ENTITY_EXPORT_HTML_CSP,
    render_entity_export_html,
    render_entity_export_markdown,
)
from okto_pulse.core.application.use_cases.base import EntityNotFoundError
from okto_pulse.core.application.use_cases.entity_export import (
    GetEntityExportBundleCommand,
    GetEntityExportBundleUseCase,
)
from okto_pulse.core.domain.entity_export import (
    EntityExportHistoryScope,
    EntityExportType,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.repositories.interfaces.unit_of_work import (
    ConsistentReadContractError,
    UnitOfWorkFactory,
)


router = APIRouter()

_SUPPORTED_TYPES = frozenset(
    {
        EntityExportType.STORY,
        EntityExportType.IDEATION,
        EntityExportType.REFINEMENT,
        EntityExportType.SPEC,
        EntityExportType.SPRINT,
        EntityExportType.CARD,
    }
)
_MAX_SECTIONS = 128
_DEFAULT_MAX_OUTPUT_BYTES = 25 * 1024 * 1024


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value >= 1 else default


_MAX_OUTPUT_BYTES = _positive_env(
    "OKTO_PULSE_EXPORT_MAX_OUTPUT_BYTES",
    _DEFAULT_MAX_OUTPUT_BYTES,
)


class EntityExportPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["current", "complete"] = "complete"
    sections: list[str] | None = Field(default=None, max_length=_MAX_SECTIONS)


class EntityExportDownloadRequest(EntityExportPreflightRequest):
    format: Literal["markdown", "html"]
    expected_snapshot_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


def _entity_type(value: str) -> EntityExportType:
    try:
        parsed = EntityExportType(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export entity not found",
        ) from exc
    if parsed not in _SUPPORTED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export entity not found",
        )
    return parsed


def _slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return normalized[:96] or "entity"


def _preflight_payload(bundle) -> dict[str, object]:
    raw = bundle.to_dict()
    identity = {
        "entity_type": raw["subject"]["entity_type"],
        "entity_id": raw["subject"]["entity_id"],
        "title": raw["subject"]["title"],
        "status": raw["subject"]["status"],
        "version": raw["subject"].get("version"),
        "edition": raw["subject"].get("edition"),
    }
    sections = [
        {
            **entry,
            "state": entry["status"],
        }
        for entry in raw["manifest"]["entries"]
    ]
    return {
        "schema_version": raw["contract_version"],
        "formats": ["markdown", "html"],
        "scope": raw["history_scope"],
        "identity": identity,
        "entity": identity,
        "snapshot_fingerprint": raw["snapshot_fingerprint"],
        "sections": sections,
        "manifest": sections,
        "complete_for_actor": raw["complete_for_actor"],
        "source_complete": raw["source_complete"],
        "limits": {
            "max_sections": _MAX_SECTIONS,
            "max_output_bytes": _MAX_OUTPUT_BYTES,
        },
    }


async def _materialize_bundle(
    *,
    board_id: str,
    entity_type: EntityExportType,
    entity_id: str,
    scope: str,
    sections: list[str] | None,
    principal: Principal,
    factory: UnitOfWorkFactory,
):
    actor = RESTAdapterContract.actor_from_principal(principal, board_id=board_id)
    realm_scope = factory.resolve_realm_scope()
    try:
        async with factory(realm_scope=realm_scope, actor=actor) as uow:
            result = await GetEntityExportBundleUseCase().execute(
                GetEntityExportBundleCommand(
                    board_id=board_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    history_scope=EntityExportHistoryScope(scope),
                    requested_sections=tuple(sections or ()),
                ),
                actor=actor,
                uow=uow,
            )
            # The result owns only immutable/detached Core DTOs.  Leaving this
            # context releases the consistent read before serialization/render.
            return result.bundle
    except EntityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export entity not found",
        ) from exc
    except CommunityEntityExportRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "entity_export_request_invalid", "message": str(exc)},
        ) from exc
    except CommunityEntityExportLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "entity_export_limit_exceeded",
                "message": "The requested report exceeds the explicit export budget.",
                "details": {
                    "section_key": exc.section_key,
                    "limit": exc.limit,
                },
            },
        ) from exc
    except ConsistentReadContractError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "entity_export_consistent_read_unavailable",
                "message": "A consistent report snapshot could not be established.",
                "details": {"reason": exc.reason},
            },
        ) from exc


@router.post(
    "/boards/{board_id}/entity-exports/{entity_type}/{entity_id}/preflight"
)
async def preflight_entity_export(
    board_id: str,
    entity_type: str,
    entity_id: str,
    body: EntityExportPreflightRequest,
    principal: Principal = Depends(require_principal),
    factory: UnitOfWorkFactory = Depends(get_unit_of_work_factory),
) -> dict[str, object]:
    bundle = await _materialize_bundle(
        board_id=board_id,
        entity_type=_entity_type(entity_type),
        entity_id=entity_id,
        scope=body.scope,
        sections=body.sections,
        principal=principal,
        factory=factory,
    )
    return _preflight_payload(bundle)


@router.post(
    "/boards/{board_id}/entity-exports/{entity_type}/{entity_id}/download"
)
async def download_entity_export(
    board_id: str,
    entity_type: str,
    entity_id: str,
    body: EntityExportDownloadRequest,
    principal: Principal = Depends(require_principal),
    factory: UnitOfWorkFactory = Depends(get_unit_of_work_factory),
) -> Response:
    parsed_type = _entity_type(entity_type)
    bundle = await _materialize_bundle(
        board_id=board_id,
        entity_type=parsed_type,
        entity_id=entity_id,
        scope=body.scope,
        sections=body.sections,
        principal=principal,
        factory=factory,
    )
    if bundle.snapshot_fingerprint != body.expected_snapshot_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "entity_export_snapshot_changed",
                "message": "The entity changed after export preflight. Refresh and retry.",
                "details": {
                    "current_snapshot_fingerprint": bundle.snapshot_fingerprint,
                },
            },
        )
    detached = bundle.to_dict()
    extension = "html" if body.format == "html" else "md"
    filename = (
        f"{parsed_type.value}_{_slug(bundle.subject.title)}_"
        f"{body.scope}.{extension}"
    )
    common_headers = {
        "Content-Disposition": attachment_content_disposition(
            filename,
            fallback_filename=f"{parsed_type.value}_report.{extension}",
        ),
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Export-Snapshot-Fingerprint": bundle.snapshot_fingerprint,
        # The optimistic snapshot fence is stable for identical report facts;
        # ``bundle_digest`` intentionally includes observation timestamps and
        # would therefore produce a different validator on every request.
        "ETag": f'"{bundle.snapshot_fingerprint}"',
    }
    if body.format == "html":
        content = render_entity_export_html(detached).encode("utf-8")
        if len(content) > _MAX_OUTPUT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={
                    "code": "entity_export_output_limit_exceeded",
                    "message": "The rendered report exceeds the explicit output budget.",
                    "details": {"limit": _MAX_OUTPUT_BYTES},
                },
            )
        common_headers["Content-Security-Policy"] = ENTITY_EXPORT_HTML_CSP
        return Response(
            content=content,
            media_type="text/html; charset=utf-8",
            headers=common_headers,
        )
    content = render_entity_export_markdown(detached).encode("utf-8")
    if len(content) > _MAX_OUTPUT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "entity_export_output_limit_exceeded",
                "message": "The rendered report exceeds the explicit output budget.",
                "details": {"limit": _MAX_OUTPUT_BYTES},
            },
        )
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers=common_headers,
    )


__all__ = ["router"]
