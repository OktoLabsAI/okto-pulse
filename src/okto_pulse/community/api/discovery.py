"""Discovery REST router — exposes the intent catalog, saved searches
and per-user search history to the Global Discovery screen.

v1 is read-only for the intent catalog (GET). Admin CRUD on intents
(POST/PUT/DELETE) is deferred to a follow-up card tracked alongside the
parent spec. Saved-search and search-history write endpoints are also
future work — v1 only ships the GET surface needed by the redesigned
GlobalSearchView.

Spec R01A REST-FU7-S4: the five read/execute endpoints now route through the
transport-free discovery use cases + ``get_unit_of_work``; the inline SQL moved
to ``DiscoveryCatalogReader`` and the selector read policy to
``DiscoverySelectorRestAccessPolicy`` (both in
``services/discovery_catalog_reader``). Each handler is a thin adapter mapping
the use case result/errors back to HTTP, preserving the exact status codes,
detail payloads and the selector observability metric.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases import (
    CommandValidationError,
    EntityNotFoundError,
)
from okto_pulse.core.application.use_cases.discovery_crud import (
    ExecuteDiscoveryIntentCommand,
    ExecuteDiscoveryIntentUseCase,
    ListDiscoveryIntentsCommand,
    ListDiscoveryIntentsUseCase,
    ListDiscoverySavedSearchesCommand,
    ListDiscoverySavedSearchesUseCase,
    ListDiscoverySearchHistoryCommand,
    ListDiscoverySearchHistoryUseCase,
    ListDiscoverySelectorOptionsCommand,
    ListDiscoverySelectorOptionsUseCase,
)
from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
from okto_pulse.community.api.auth_deps import require_user
from okto_pulse.core.models.schemas import (
    DiscoveryIntentResponse,
    DiscoverySavedSearchResponse,
    DiscoverySearchHistoryResponse,
)
from okto_pulse.core.repositories import PulseUnitOfWork
from okto_pulse.core.services.discovery_executor import (
    DiscoverySelectorExecutionError,
)
from okto_pulse.core.services.discovery_selector_catalog import (
    DiscoverySelectorAccessDenied,
    DiscoverySelectorInvalidRequest,
    DiscoverySelectorSpecNotFound,
    DiscoverySelectorUnsafeProjection,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _selector_error(
    status_code: int,
    error: str,
) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"error": error})


def _selector_metric(
    *,
    board_id: str,
    selector_kind: str,
    child_type: str | None,
    status_filter: str | None,
    cache_status: str,
    outcome: str,
    reason: str | None,
    duration_ms: int,
) -> None:
    logger.info(
        "discovery.selector_options outcome=%s reason=%s",
        outcome,
        reason or "none",
        extra={
            "event": "discovery.selector_options",
            "board_id": board_id,
            "selector_kind": selector_kind,
            "child_type": child_type or "none",
            "status": status_filter or "none",
            "cache_status": cache_status,
            "outcome": outcome,
            "reason": reason or "none",
            "duration_ms": duration_ms,
            "metric_name": "discovery_selector_options_latency_ms",
            "value": duration_ms,
        },
    )
    if outcome == "forbidden":
        logger.info(
            "discovery.selector_access_denied reason=%s",
            reason or "none",
            extra={
                "event": "discovery.selector_access_denied",
                "metric_name": "discovery_selector_access_denied_total",
                "value": 1,
                "board_id": board_id,
                "selector_kind": selector_kind,
                "child_type": child_type or "none",
                "outcome": outcome,
                "reason": reason or "none",
            },
        )
    if outcome == "unsafe_projection":
        logger.info(
            "discovery.selector_unsafe_projection reason=%s",
            reason or "none",
            extra={
                "event": "discovery.selector_unsafe_projection",
                "metric_name": "discovery_selector_unsafe_projection_total",
                "value": 1,
                "board_id": board_id,
                "selector_kind": selector_kind,
                "child_type": child_type or "none",
                "outcome": outcome,
                "reason": reason or "none",
            },
        )


@router.get(
    "/discovery/intents",
    response_model=list[DiscoveryIntentResponse],
)
async def list_discovery_intents(
    _user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return the active catalog of user-facing Discovery intents."""
    result = await ListDiscoveryIntentsUseCase().execute(
        ListDiscoveryIntentsCommand(),
        actor=RESTAdapterContract.actor(_user_id),
        uow=uow,
    )
    return result.intents


@router.get("/discovery/boards/{board_id}/selector-options")
async def list_discovery_selector_options(
    board_id: str,
    selector_kind: str = Query("spec"),
    spec_id: str | None = Query(None),
    child_type: str | None = Query(None),
    status_filter: str | None = Query("active", alias="status"),
    q: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_superseded: bool = Query(False),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict[str, Any]:
    """Return metadata-only Discovery selector options for a board.

    The API performs an explicit board read check before catalog projection so
    403 responses do not reveal whether requested specs or children exist.
    """

    started = time.perf_counter()
    cache_status = "bypass"
    outcome = "success"
    reason: str | None = None
    try:
        result = await ListDiscoverySelectorOptionsUseCase().execute(
            ListDiscoverySelectorOptionsCommand(
                board_id,
                selector_kind=selector_kind,
                spec_id=spec_id,
                child_type=child_type,
                status_filter=status_filter,
                q=q,
                limit=limit,
                offset=offset,
                include_superseded=include_superseded,
            ),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
        cache_status = result.cache_status
        return result.payload
    except DiscoverySelectorInvalidRequest as exc:
        outcome = "invalid_request"
        reason = exc.code
        raise _selector_error(status.HTTP_400_BAD_REQUEST, exc.code) from exc
    except DiscoverySelectorAccessDenied as exc:
        outcome = "forbidden"
        reason = "selector_access_denied"
        raise _selector_error(status.HTTP_403_FORBIDDEN, "selector_access_denied") from exc
    except DiscoverySelectorSpecNotFound as exc:
        outcome = "not_found"
        reason = "selector_spec_not_found"
        raise _selector_error(status.HTTP_404_NOT_FOUND, "selector_spec_not_found") from exc
    except DiscoverySelectorUnsafeProjection as exc:
        outcome = "unsafe_projection"
        reason = "selector_projection_failed"
        raise _selector_error(status.HTTP_500_INTERNAL_SERVER_ERROR, "selector_projection_failed") from exc
    finally:
        _selector_metric(
            board_id=board_id,
            selector_kind=selector_kind,
            child_type=child_type,
            status_filter=status_filter,
            cache_status=cache_status,
            outcome=outcome,
            reason=reason,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


@router.get(
    "/discovery/boards/{board_id}/saved-searches",
    response_model=list[DiscoverySavedSearchResponse],
)
async def list_saved_searches(
    board_id: str,
    _user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return the saved searches for a board (shared with all members)."""
    result = await ListDiscoverySavedSearchesUseCase().execute(
        ListDiscoverySavedSearchesCommand(board_id),
        actor=RESTAdapterContract.actor(_user_id, board_id=board_id),
        uow=uow,
    )
    return result.items


@router.get(
    "/discovery/boards/{board_id}/search-history",
    response_model=list[DiscoverySearchHistoryResponse],
)
async def list_search_history(
    board_id: str,
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
):
    """Return the current user's last 50 search entries on this board."""
    result = await ListDiscoverySearchHistoryUseCase().execute(
        ListDiscoverySearchHistoryCommand(board_id),
        actor=RESTAdapterContract.actor(user_id, board_id=board_id),
        uow=uow,
    )
    return result.items


@router.post("/discovery/intents/{intent_id}/execute")
async def execute_discovery_intent(
    intent_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    user_id: str = Depends(require_user),
    uow: PulseUnitOfWork = Depends(get_unit_of_work),
) -> dict[str, Any]:
    """Execute the real tool bound to an intent and return a normalized payload.

    Ideação a4f526df — this is what closes the "semantic fallback masking
    real tool" gap observed in the v1 catalog. Body shape::

        {"board_id": "<uuid>", "params": {"topic": "...", ...}}

    Missing required params listed by `intent.params_schema` yield 400 with
    the specific field name. Unknown tool_binding yields 400 too so drift is
    caught at runtime (a CI parity test is tracked in a separate ideation).
    """
    board_id = (payload or {}).get("board_id")
    params = (payload or {}).get("params") or {}
    try:
        result = await ExecuteDiscoveryIntentUseCase().execute(
            ExecuteDiscoveryIntentCommand(intent_id, board_id, params),
            actor=RESTAdapterContract.actor(user_id, board_id=board_id),
            uow=uow,
        )
    except CommandValidationError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except EntityNotFoundError as e:
        raise HTTPException(status_code=404, detail="Intent not found") from e
    except DiscoverySelectorExecutionError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": e.code},
        ) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result.payload
