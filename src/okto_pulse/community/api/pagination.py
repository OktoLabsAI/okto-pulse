"""REST pagination helpers for the list routes (card C5, spec 8b33f9a8).

Bridges the HTTP surface to the Core surface executor: validates the
REST-only page-size menu ``{25, 50, 100}`` (FR0 — the Core accepts 1..200;
the menu is UX policy), assembles the mandatory scope (identity anchor +
archived policy), wraps the request in the PRODUCTIVE ``statement_budget``
cap (TR1 — list routes declare 6) and executes through the catalog contract
``uow.services.entity_pages``. Legacy calls without ``offset``/``limit``
never enter this module (DR9: byte-identical legacy shapes).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import TypeAdapter, ValidationError

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    statement_budget,
)
from okto_pulse.community.sql_like import literal_contains_pattern
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    PAGE_OFFSET_MAX,
    PageRequest,
    PageResult,
)

#: REST/UX page-size menu (FR0): the Core window accepts 1..200; the routes
#: restrict to the product's paginator options.
REST_PAGE_SIZES: frozenset[int] = frozenset({25, 50, 100})

#: TR1 statement cap for a single list-route request (exact totals + page +
#: projection headroom; the adapter may combine the filtered total/page scan).
LIST_ROUTE_STATEMENT_CAP = 6

_INTEGER_QUERY = TypeAdapter(int)
_BOOLEAN_QUERY = TypeAdapter(bool)


def _raw_pagination_requested(request: Request) -> bool:
    """Return whether the opt-in window is present in the raw query."""
    return "offset" in request.query_params or "limit" in request.query_params


def validate_pagination_query(request: Request) -> None:
    """Map malformed pagination primitives to the specified typed 400.

    FastAPI normally reports malformed ``int``/``bool`` query values as 422.
    TR7 deliberately assigns these transport errors to 400, so this dependency
    inspects the raw query before route-parameter validation.  The accepted
    boolean spellings match FastAPI/Pydantic's compatibility vocabulary.
    Bounds and the REST page-size menu remain the responsibility of
    :func:`resolve_window` after typed parsing.
    """
    if not _raw_pagination_requested(request):
        return
    for name in ("offset", "limit"):
        raw = request.query_params.get(name)
        if raw is None:
            continue
        try:
            _INTEGER_QUERY.validate_python(raw)
        except ValidationError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": f"{name}_invalid", "value": raw},
            )
    for name in ("include_archived",):
        raw = request.query_params.get(name)
        if raw is None:
            continue
        try:
            _BOOLEAN_QUERY.validate_python(raw)
        except ValidationError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": f"{name}_invalid", "value": raw},
            )


def validate_story_pagination_query(request: Request) -> None:
    """Apply the common guard plus the Story-only boolean filters.

    ``linked`` and ``converted`` belong only to the Story surface.  Keeping
    them out of the shared dependency preserves DR9 on the other legacy
    routes, where unknown query parameters continue to be ignored.
    """
    validate_pagination_query(request)
    if not _raw_pagination_requested(request):
        return
    for name in ("linked", "converted"):
        raw = request.query_params.get(name)
        if raw is None:
            continue
        try:
            _BOOLEAN_QUERY.validate_python(raw)
        except ValidationError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": f"{name}_invalid", "value": raw},
            )


def validate_ideation_pagination_query(request: Request) -> None:
    """Apply the common guard plus the Ideation-only boolean filter.

    Keeping ``derivation_pending`` scoped to this route means unrelated list
    surfaces continue to ignore that unknown parameter, while paginated
    Ideation requests receive the typed-400 contract used by the other page
    filters.
    """
    validate_pagination_query(request)
    if not _raw_pagination_requested(request):
        return
    raw = request.query_params.get("derivation_pending")
    if raw is None:
        return
    try:
        _BOOLEAN_QUERY.validate_python(raw)
    except ValidationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "derivation_pending_invalid", "value": raw},
        )


def pagination_requested(offset: int | None, limit: int | None) -> bool:
    """The envelope is OPT-IN: any of offset/limit present activates it."""
    return offset is not None or limit is not None


def resolve_window(offset: int | None, limit: int | None) -> tuple[int, int]:
    """Validate the REST window (typed 400s) and fill the defaults."""
    resolved_offset = 0 if offset is None else offset
    resolved_limit = 25 if limit is None else limit
    if resolved_offset < 0 or resolved_offset > PAGE_OFFSET_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "offset_out_of_bounds", "offset": resolved_offset},
        )
    if resolved_limit not in REST_PAGE_SIZES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "limit_not_allowed",
                "limit": resolved_limit,
                "allowed": sorted(REST_PAGE_SIZES),
            },
        )
    return resolved_offset, resolved_limit


def anchor_scope(
    anchor_field: str, anchor_id: str, *, include_archived: bool
) -> tuple[ApplicationFilter, ...]:
    """Mandatory base scope: identity anchor + archived policy.

    ``include_archived=False`` keeps the base scope active-only, so BOTH
    totals move together when the toggle flips (ts_8d76d28e).
    """
    scope: tuple[ApplicationFilter, ...] = (
        ApplicationFilter(anchor_field, "eq", anchor_id),
    )
    if not include_archived:
        scope = (*scope, ApplicationFilter("archived", "is_false", None))
    return scope


def board_scope(
    board_id: str, *, include_archived: bool
) -> tuple[ApplicationFilter, ...]:
    """Board-anchored base scope (see :func:`anchor_scope`)."""
    return anchor_scope("board_id", board_id, include_archived=include_archived)


def search_groups(
    search: str | None, fields: tuple[str, ...]
) -> tuple[tuple[ApplicationFilter, ...], ...]:
    """Server-side search: OR of per-field ilike predicates (any_groups)."""
    if not search:
        return ()
    needle = literal_contains_pattern(search)
    return tuple((ApplicationFilter(field, "ilike", needle),) for field in fields)


def record_fields(record: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """Scalar projection helper: pick the row-derivable fields of a record."""
    return {field: record.values.get(field) for field in fields}


def project_page(page: PageResult, item_factory: Any) -> Any:
    """Wrap a :class:`PageResult` into the REST envelope via a projector."""
    from okto_pulse.core.models.schemas import PageEnvelope

    return PageEnvelope(
        items=[item_factory(record) for record in page.items],
        total_filtered=page.total_filtered,
        total_overall=page.total_overall,
        offset=page.offset,
        limit=page.limit,
    )


async def run_paginated_list(
    uow: Any,
    request: PageRequest | Callable[[Any], PageRequest],
    *,
    preflight: Callable[[], Awaitable[Any]] | None = None,
) -> PageResult:
    """Execute one paginated surface list under the productive TR1 cap.

    The budget session is the UoW's shared relational context; authorization
    preflight, parent-derived request construction, both counts, the page and
    projection reads all share the declared cap. Over-budget statements abort
    fail-closed.
    """
    session = uow.services.cards.db
    try:
        async with statement_budget(session, LIST_ROUTE_STATEMENT_CAP):
            preflight_result: Any = None
            if preflight is not None:
                preflight_result = await preflight()
            resolved_request = (
                request(preflight_result) if callable(request) else request
            )
            return await uow.services.entity_pages.list(resolved_request)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "page_request_invalid", "message": str(exc)},
        ) from exc
