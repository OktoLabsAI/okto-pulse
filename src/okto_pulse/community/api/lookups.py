"""Shared REST contract for paginated spec and ideation lookups (C9)."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import TypeAdapter, ValidationError

from okto_pulse.community.api.pagination import board_scope
from okto_pulse.core.domain.enums import IdeationStatus, SpecStatus
from okto_pulse.core.models import LookupItem, LookupResponse
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    PageRequest,
)


SPEC_STATUSES = frozenset(item.value for item in SpecStatus)
IDEATION_STATUSES = frozenset(item.value for item in IdeationStatus)

_INTEGER_QUERY = TypeAdapter(int)
_BOOLEAN_QUERY = TypeAdapter(bool)


def _error(code: str, **details: Any) -> None:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": code, **details},
    )


def _status_values(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ()
    return tuple(
        dict.fromkeys(value.strip() for value in raw.split(",") if value.strip())
    )


def _validate_common(request: Request, allowed_statuses: frozenset[str]) -> None:
    query = request.query_params
    for name in ("offset", "limit"):
        raw = query.get(name)
        if raw is None:
            continue
        try:
            value = _INTEGER_QUERY.validate_python(raw)
        except ValidationError:
            _error(f"{name}_invalid", value=raw)
        if name == "offset" and value < 0:
            _error("offset_out_of_bounds", offset=value)
        if name == "limit" and not 1 <= value <= 50:
            _error("limit_out_of_bounds", limit=value, bounds="1..50")

    statuses = _status_values(query.get("status"))
    unknown = sorted(set(statuses) - allowed_statuses)
    if unknown:
        _error("status_invalid", values=unknown)


def validate_spec_lookup_query(request: Request) -> None:
    _validate_common(request, SPEC_STATUSES)
    query = request.query_params
    for name in ("linked_to_cards", "include_archived_cards"):
        raw = query.get(name)
        if raw is None:
            continue
        try:
            _BOOLEAN_QUERY.validate_python(raw)
        except ValidationError:
            _error(f"{name}_invalid", value=raw)
    if (
        "include_archived_cards" in query
        and "linked_to_cards" not in query
    ):
        _error("include_archived_cards_requires_linked_to_cards")


def validate_ideation_lookup_query(request: Request) -> None:
    _validate_common(request, IDEATION_STATUSES)


def lookup_page_request(
    surface: str,
    board_id: str,
    *,
    statuses: str | None,
    search: str | None,
    offset: int,
    limit: int,
    linked_to_cards: bool = False,
    include_archived_cards: bool = False,
) -> PageRequest:
    """Build a lookup whose eligibility predicates precede COUNT/LIMIT."""

    filters: list[ApplicationFilter] = []
    status_values = _status_values(statuses)
    if status_values:
        filters.append(ApplicationFilter("status", "in", status_values))
    if search:
        filters.append(ApplicationFilter("title", "ilike", f"%{search}%"))
    if surface == "spec_lookup" and linked_to_cards:
        link_field = (
            "linked_to_cards"
            if include_archived_cards
            else "linked_to_active_cards"
        )
        filters.append(ApplicationFilter(link_field, "is_true", None))

    return PageRequest(
        surface=surface,
        scope=board_scope(board_id, include_archived=False),
        filters=tuple(filters),
        offset=offset,
        limit=limit,
    )


def lookup_response(page: Any) -> LookupResponse:
    return LookupResponse(
        items=[
            LookupItem(
                id=record.values["id"],
                title=record.values["title"],
                status=record.values["status"],
            )
            for record in page.items
        ],
        total=page.total_filtered,
        offset=page.offset,
        limit=page.limit,
    )


__all__ = [
    "IDEATION_STATUSES",
    "SPEC_STATUSES",
    "lookup_page_request",
    "lookup_response",
    "validate_ideation_lookup_query",
    "validate_spec_lookup_query",
]
