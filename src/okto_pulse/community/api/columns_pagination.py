"""Typed query parsing and projection helpers for paginated Kanban columns.

The legacy branch is selected only when ``per_column_limit`` is absent and no
new columns parameter is present.  Everything else is parsed from the raw
query string so malformed scalars receive the C6 400 envelope instead of a
FastAPI-generated 422.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from fastapi import HTTPException, Request, status

from okto_pulse.core.domain.enums import CardStatus, CardType
from okto_pulse.core.ports.application_persistence import (
    ApplicationFilter,
    GroupCountRequest,
    PAGE_OFFSET_MAX,
    PageRequest,
)


KANBAN_STATUSES: tuple[str, ...] = tuple(item.value for item in CardStatus)
KANBAN_CARD_TYPES: frozenset[str] = frozenset(item.value for item in CardType)
_NEW_QUERY_KEYS: tuple[str, ...] = (
    "spec_ids",
    "card_types",
    "search",
    "assignee_id",
    "column",
    "offset",
)

CARD_SUMMARY_FIELDS: tuple[str, ...] = (
    "id",
    "board_id",
    "spec_id",
    "title",
    "description",
    "status",
    "priority",
    "position",
    "assignee_id",
    "created_by",
    "created_at",
    "updated_at",
    "due_date",
    "labels",
    "test_scenario_ids",
    "conclusions",
    "card_type",
    "origin_task_id",
    "severity",
    "linked_test_task_ids",
    "archived",
    "open_qa_count",
)


def _error(code: str, **details: Any) -> None:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": code, **details},
    )


def parse_include_archived(request: Request) -> bool:
    """Parse the existing toggle strictly for both legacy and opt-in calls."""

    raw = request.query_params.get("include_archived", "false")
    if raw not in {"true", "false"}:
        _error("include_archived_invalid")
    return raw == "true"


@dataclass(frozen=True, slots=True)
class ColumnsParameters:
    per_column_limit: int
    column: str | None
    offset: int
    spec_ids: tuple[str, ...] | None
    include_unlinked: bool
    search: str | None
    assignee_id: str | None
    include_archived: bool
    card_types_by_status: dict[str, tuple[str, ...]]


def parse_columns_parameters(request: Request) -> ColumnsParameters | None:
    """Return ``None`` for the literal legacy branch, else typed C6 params."""

    query = request.query_params
    limit_raw = query.get("per_column_limit")
    if limit_raw is None:
        present = [name for name in _NEW_QUERY_KEYS if name in query]
        if present:
            _error("params_require_per_column_limit", params=present)
        return None

    include_archived = parse_include_archived(request)
    if not limit_raw.isdigit():
        _error("per_column_limit_invalid")
    per_column_limit = int(limit_raw)
    if not 1 <= per_column_limit <= 100:
        _error("per_column_limit_out_of_bounds", bounds="1..100")

    column = query.get("column")
    offset_raw = query.get("offset")
    if offset_raw is not None and column is None:
        _error("offset_requires_column")
    if offset_raw is not None and not offset_raw.isdigit():
        _error("offset_invalid")
    offset = int(offset_raw) if offset_raw is not None else 0
    if offset > PAGE_OFFSET_MAX:
        # A huge all-digit offset passes isdigit() but would overflow SQLite's
        # signed 64-bit OFFSET binding — reject with this route's existing typed
        # offset error instead of letting it reach the database.
        _error("offset_invalid")
    if column is not None and column not in KANBAN_STATUSES:
        _error("unknown_column", column=column)

    spec_ids_raw = query.get("spec_ids")
    spec_ids: tuple[str, ...] | None = None
    include_unlinked = False
    if spec_ids_raw:
        values = tuple(value for value in spec_ids_raw.split(",") if value)
        include_unlinked = "__unlinked__" in values
        spec_ids = tuple(value for value in values if value != "__unlinked__")

    type_map: dict[str, tuple[str, ...]] = {}
    for raw in query.getlist("card_types"):
        if ":" not in raw:
            _error("card_types_malformed", value=raw)
        card_status, raw_types = raw.split(":", 1)
        card_types = tuple(value for value in raw_types.split(",") if value)
        if (
            card_status not in KANBAN_STATUSES
            or not card_types
            or any(value not in KANBAN_CARD_TYPES for value in card_types)
        ):
            _error("card_types_invalid", value=raw)
        # Repeated status entries deliberately use last-write-wins semantics.
        type_map[card_status] = card_types

    return ColumnsParameters(
        per_column_limit=per_column_limit,
        column=column,
        offset=offset,
        spec_ids=spec_ids,
        include_unlinked=include_unlinked,
        search=query.get("search"),
        assignee_id=query.get("assignee_id"),
        include_archived=include_archived,
        card_types_by_status=type_map,
    )


def _spec_disjunction(
    parameters: ColumnsParameters,
) -> tuple[ApplicationFilter, ...]:
    if parameters.spec_ids is None:
        return ()
    predicates: list[ApplicationFilter] = []
    if parameters.spec_ids:
        predicates.append(ApplicationFilter("spec_id", "in", parameters.spec_ids))
    if parameters.include_unlinked:
        predicates.append(ApplicationFilter("spec_id", "is_none", None))
    # A syntactically non-empty CSV that contains only separators intentionally
    # matches no rows, mirroring SQL's false empty-set clause.
    if not predicates:
        predicates.append(ApplicationFilter("spec_id", "eq", "__empty_spec_filter__"))
    return tuple(predicates)


def _search_disjunction(
    parameters: ColumnsParameters,
) -> tuple[tuple[ApplicationFilter, ...], ...]:
    if not parameters.search:
        return ()
    needle = f"%{parameters.search}%"
    return tuple(
        (ApplicationFilter(field, "ilike", needle),)
        for field in ("title", "description", "labels")
    )


def column_page_request(
    board_id: str,
    card_status: str,
    parameters: ColumnsParameters,
    *,
    offset: int = 0,
    include_type_filter: bool = True,
) -> PageRequest:
    """Build one catalog-validated three-statement Kanban page request."""

    scope: list[ApplicationFilter] = [
        ApplicationFilter("board_id", "eq", board_id),
        ApplicationFilter("status", "eq", card_status),
    ]
    if not parameters.include_archived:
        scope.append(ApplicationFilter("archived", "is_false", None))

    filters: list[ApplicationFilter] = []
    if parameters.assignee_id:
        filters.append(ApplicationFilter("assignee_id", "eq", parameters.assignee_id))
    if include_type_filter:
        allowed_types = parameters.card_types_by_status.get(card_status)
        if allowed_types:
            filters.append(ApplicationFilter("card_type", "in", allowed_types))

    return PageRequest(
        surface="kanban_column",
        scope=tuple(scope),
        filters=tuple(filters),
        any_filters=_spec_disjunction(parameters),
        any_groups=_search_disjunction(parameters),
        offset=offset,
        limit=parameters.per_column_limit,
    )


def _aggregate_scope(
    board_id: str,
    parameters: ColumnsParameters,
    *,
    card_status: str | None = None,
) -> tuple[ApplicationFilter, ...]:
    scope: list[ApplicationFilter] = [ApplicationFilter("board_id", "eq", board_id)]
    if card_status is not None:
        scope.append(ApplicationFilter("status", "eq", card_status))
    if not parameters.include_archived:
        scope.append(ApplicationFilter("archived", "is_false", None))
    return tuple(scope)


def _aggregate_filters(
    parameters: ColumnsParameters,
) -> tuple[ApplicationFilter, ...]:
    if not parameters.assignee_id:
        return ()
    return (ApplicationFilter("assignee_id", "eq", parameters.assignee_id),)


def _aggregate_dimensions(
    parameters: ColumnsParameters,
    *,
    include_type_map: bool,
) -> tuple[tuple[tuple[ApplicationFilter, ...], ...], ...]:
    dimensions: list[tuple[tuple[ApplicationFilter, ...], ...]] = []
    spec_predicates = _spec_disjunction(parameters)
    if spec_predicates:
        dimensions.append(tuple((predicate,) for predicate in spec_predicates))
    search_predicates = _search_disjunction(parameters)
    if search_predicates:
        dimensions.append(search_predicates)
    if include_type_map and parameters.card_types_by_status:
        status_branches: list[tuple[ApplicationFilter, ...]] = []
        for card_status in KANBAN_STATUSES:
            branch = [ApplicationFilter("status", "eq", card_status)]
            allowed = parameters.card_types_by_status.get(card_status)
            if allowed:
                branch.append(ApplicationFilter("card_type", "in", allowed))
            status_branches.append(tuple(branch))
        dimensions.append(tuple(status_branches))
    return tuple(dimensions)


def column_type_facet_request(
    board_id: str,
    card_status: str,
    parameters: ColumnsParameters,
) -> GroupCountRequest:
    """One self-excluding card-type aggregate for column continuation."""

    return GroupCountRequest(
        surface="kanban_facets",
        scope=_aggregate_scope(board_id, parameters, card_status=card_status),
        group_by=("card_type",),
        filters=_aggregate_filters(parameters),
        disjunctions=_aggregate_dimensions(parameters, include_type_map=False),
    )


def batch_type_facet_request(
    board_id: str,
    parameters: ColumnsParameters,
) -> GroupCountRequest:
    """One grouped self-excluding card-type aggregate for all columns."""

    return GroupCountRequest(
        surface="kanban_facets",
        scope=_aggregate_scope(board_id, parameters),
        group_by=("status", "card_type"),
        filters=_aggregate_filters(parameters),
        disjunctions=_aggregate_dimensions(parameters, include_type_map=False),
    )


def assignee_facet_request(
    board_id: str,
    parameters: ColumnsParameters,
) -> GroupCountRequest:
    """Board-wide assignee aggregate with the status-aware type map applied."""

    return GroupCountRequest(
        surface="kanban_facets",
        scope=_aggregate_scope(board_id, parameters),
        group_by=("assignee_id",),
        filters=_aggregate_filters(parameters),
        disjunctions=_aggregate_dimensions(parameters, include_type_map=True),
    )


def _wire_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return value


def card_summary(record: Any) -> dict[str, Any]:
    """Project the exact 22-field legacy card wire from a scalar record."""

    values = record.values
    result = {field: _wire_value(values.get(field)) for field in CARD_SUMMARY_FIELDS}
    result["priority"] = result["priority"] or "none"
    result["card_type"] = result["card_type"] or "normal"
    result["labels"] = result["labels"] or []
    result["archived"] = bool(result["archived"])
    result["open_qa_count"] = int(result["open_qa_count"] or 0)
    return result


def page_meta(
    page: Any, card_type_facet: dict[str, int] | None = None
) -> dict[str, Any]:
    return {
        "total_filtered": page.total_filtered,
        "total_overall": page.total_overall,
        "has_more": page.offset + len(page.items) < page.total_filtered,
        "facets": {"card_type": card_type_facet or {}},
    }


__all__ = [
    "CARD_SUMMARY_FIELDS",
    "KANBAN_STATUSES",
    "ColumnsParameters",
    "assignee_facet_request",
    "batch_type_facet_request",
    "card_summary",
    "column_page_request",
    "column_type_facet_request",
    "page_meta",
    "parse_columns_parameters",
    "parse_include_archived",
]
