"""Query validation and Core request assembly for the paginated card list."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, Request, status
from pydantic import TypeAdapter, ValidationError

from okto_pulse.community.api.pagination import board_scope
from okto_pulse.core.domain.enums import CardPriority, CardStatus, CardType
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    PageRequest,
)


CARD_STATUSES = frozenset(item.value for item in CardStatus)
CARD_PRIORITIES = frozenset(item.value for item in CardPriority)
CARD_TYPES = frozenset(item.value for item in CardType)

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


def validate_card_list_query(request: Request) -> None:
    """Return the C7 typed 400 envelope before FastAPI can emit a 422."""

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
        if name == "limit" and value not in {25, 50, 100}:
            _error(
                "limit_not_allowed",
                limit=value,
                allowed=[25, 50, 100],
            )

    archived = query.get("include_archived")
    if archived is not None:
        try:
            _BOOLEAN_QUERY.validate_python(archived)
        except ValidationError:
            _error("include_archived_invalid", value=archived)

    status_value = query.get("status")
    if status_value is not None and status_value not in CARD_STATUSES:
        _error("status_invalid", value=status_value)

    priority = query.get("priority")
    if priority is not None and priority not in CARD_PRIORITIES:
        _error("priority_invalid", value=priority)

    if "card_types" in query:
        values = _csv_values(query.get("card_types"))
        if not values or any(value not in CARD_TYPES for value in values):
            _error("card_types_invalid", value=query.get("card_types"))


def _spec_filters(raw: str | None) -> tuple[ApplicationFilter, ...]:
    values = _csv_values(raw)
    if values is None:
        # An explicitly blank value follows the usual optional-query meaning.
        return ()
    include_unlinked = "__unlinked__" in values
    spec_ids = tuple(value for value in values if value != "__unlinked__")
    predicates: list[ApplicationFilter] = []
    if spec_ids:
        predicates.append(ApplicationFilter("spec_id", "in", spec_ids))
    if include_unlinked:
        predicates.append(ApplicationFilter("spec_id", "is_none", None))
    if not predicates:
        predicates.append(
            ApplicationFilter("spec_id", "eq", "__empty_spec_filter__")
        )
    return tuple(predicates)


def _labels_and_search_groups(
    labels_raw: str | None,
    search: str | None,
) -> tuple[tuple[ApplicationFilter, ...], ...]:
    """Build ``labels ANY`` AND ``search across three fields`` as DNF.

    ``PageRequest.any_groups`` is one OR-of-AND dimension.  Expanding the two
    independent OR dimensions into their small Cartesian product preserves
    the exact predicate: ``(label A OR label B) AND (title OR description OR
    labels search)``.  JSON-encoded label needles enforce exact membership
    rather than substring matching between labels.
    """

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
                (ApplicationFilter("labels", "contains", '"__empty_label_filter__"'),),
            )

    search_branches: tuple[tuple[ApplicationFilter, ...], ...] = ()
    if search:
        needle = f"%{search}%"
        search_branches = tuple(
            (ApplicationFilter(field, "ilike", needle),)
            for field in ("title", "description", "labels")
        )

    if label_branches and search_branches:
        return tuple(
            (*label_branch, *search_branch)
            for label_branch in label_branches
            for search_branch in search_branches
        )
    return label_branches or search_branches


def card_page_request(
    board_id: str,
    *,
    status_value: str | None,
    spec_ids: str | None,
    sprint_id: str | None,
    priority: str | None,
    card_types: str | None,
    assignee_id: str | None,
    labels: str | None,
    search: str | None,
    include_archived: bool,
    offset: int,
    limit: int,
) -> PageRequest:
    """Assemble the complete C7 predicate before both COUNTs and LIMIT."""

    filters: list[ApplicationFilter] = []
    for field, value in (
        ("status", status_value),
        ("sprint_id", sprint_id),
        ("priority", priority),
        ("assignee_id", assignee_id),
    ):
        if value:
            filters.append(ApplicationFilter(field, "eq", value))

    selected_types = _csv_values(card_types)
    if selected_types:
        filters.append(ApplicationFilter("card_type", "in", selected_types))

    return PageRequest(
        surface="card_list",
        scope=board_scope(board_id, include_archived=include_archived),
        filters=tuple(filters),
        any_filters=_spec_filters(spec_ids),
        any_groups=_labels_and_search_groups(labels, search),
        offset=offset,
        limit=limit,
    )


__all__ = [
    "CARD_PRIORITIES",
    "CARD_STATUSES",
    "CARD_TYPES",
    "card_page_request",
    "validate_card_list_query",
]
