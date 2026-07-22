"""Board-wide refinement pagination contract (C8)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import TypeAdapter, ValidationError

from okto_pulse.community.api.pagination import board_scope
from okto_pulse.core.domain.enums import RefinementStatus
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    PageRequest,
)


REFINEMENT_STATUSES = frozenset(item.value for item in RefinementStatus)
REFINEMENT_PAGE_SIZES = frozenset({25, 50, 100})

_INTEGER_QUERY = TypeAdapter(int)
_BOOLEAN_QUERY = TypeAdapter(bool)


def _error(code: str, **details: Any) -> None:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": code, **details},
    )


def _csv_values(raw: str | None) -> tuple[str, ...] | None:
    if raw is None or raw == "":
        return None
    return tuple(
        dict.fromkeys(value.strip() for value in raw.split(",") if value.strip())
    )


def validate_board_refinement_query(request: Request) -> None:
    """Map malformed query values to the product's typed 400 envelope."""

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
        if name == "limit" and value not in REFINEMENT_PAGE_SIZES:
            _error(
                "limit_not_allowed",
                limit=value,
                allowed=sorted(REFINEMENT_PAGE_SIZES),
            )

    for name in ("derivation_pending", "include_archived"):
        raw = query.get(name)
        if raw is None:
            continue
        try:
            _BOOLEAN_QUERY.validate_python(raw)
        except ValidationError:
            _error(f"{name}_invalid", value=raw)

    status_value = query.get("status")
    if status_value is not None and status_value not in REFINEMENT_STATUSES:
        _error("status_invalid", value=status_value)


def _labels_and_search_groups(
    labels_raw: str | None,
    search: str | None,
) -> tuple[tuple[ApplicationFilter, ...], ...]:
    """Encode ``labels ANY`` AND four-field search as one DNF dimension."""

    labels = _csv_values(labels_raw)
    label_branches: tuple[tuple[ApplicationFilter, ...], ...] = ()
    if labels is not None:
        if labels:
            label_branches = tuple(
                (
                    ApplicationFilter(
                        "labels",
                        "contains",
                        json.dumps(label, ensure_ascii=False),
                    ),
                )
                for label in labels
            )
        else:
            label_branches = (
                (
                    ApplicationFilter(
                        "labels",
                        "contains",
                        '"__empty_label_filter__"',
                    ),
                ),
            )

    search_branches: tuple[tuple[ApplicationFilter, ...], ...] = ()
    if search:
        needle = f"%{search}%"
        search_branches = tuple(
            (ApplicationFilter(field, "ilike", needle),)
            for field in (
                "title",
                "description",
                "labels",
                "ideation_title",
            )
        )

    if label_branches and search_branches:
        return tuple(
            (*label_branch, *search_branch)
            for label_branch in label_branches
            for search_branch in search_branches
        )
    return label_branches or search_branches


def refinement_board_page_request(
    board_id: str,
    *,
    status_value: str | None,
    search: str | None,
    derivation_pending: bool | None,
    include_archived: bool,
    labels: str | None,
    offset: int,
    limit: int,
) -> PageRequest:
    """Build the complete board-wide predicate before counts and window."""

    filters: list[ApplicationFilter] = []
    if status_value:
        filters.append(ApplicationFilter("status", "eq", status_value))
    if derivation_pending is not None:
        filters.append(
            ApplicationFilter(
                "derivation_pending",
                "is_true" if derivation_pending else "is_false",
                None,
            )
        )

    return PageRequest(
        surface="refinement_board",
        scope=board_scope(board_id, include_archived=include_archived),
        filters=tuple(filters),
        any_groups=_labels_and_search_groups(labels, search),
        offset=offset,
        limit=limit,
    )


__all__ = [
    "REFINEMENT_PAGE_SIZES",
    "REFINEMENT_STATUSES",
    "refinement_board_page_request",
    "validate_board_refinement_query",
]
