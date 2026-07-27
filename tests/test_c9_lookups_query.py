"""C9 lookup query parsing and Core page-request contract."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from okto_pulse.community.api.lookups import (
    lookup_page_request,
    validate_ideation_lookup_query,
    validate_spec_lookup_query,
)


def _request(items: list[tuple[str, str]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": urlencode(items, doseq=True).encode(),
        }
    )


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([("offset", "x")], "offset_invalid"),
        ([("offset", "-1")], "offset_out_of_bounds"),
        ([("limit", "x")], "limit_invalid"),
        ([("limit", "0")], "limit_out_of_bounds"),
        ([("limit", "51")], "limit_out_of_bounds"),
        ([("status", "draft,unknown")], "status_invalid"),
        ([("linked_to_cards", "wat")], "linked_to_cards_invalid"),
        (
            [("include_archived_cards", "wat"), ("linked_to_cards", "true")],
            "include_archived_cards_invalid",
        ),
        (
            [("include_archived_cards", "true")],
            "include_archived_cards_requires_linked_to_cards",
        ),
    ],
)
def test_spec_lookup_query_errors_are_typed_400(
    items: list[tuple[str, str]], code: str
) -> None:
    with pytest.raises(HTTPException) as caught:
        validate_spec_lookup_query(_request(items))
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == code


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([("offset", "x")], "offset_invalid"),
        ([("offset", "-1")], "offset_out_of_bounds"),
        ([("limit", "x")], "limit_invalid"),
        ([("limit", "0")], "limit_out_of_bounds"),
        ([("limit", "51")], "limit_out_of_bounds"),
        ([("status", "draft,unknown")], "status_invalid"),
    ],
)
def test_ideation_lookup_query_errors_are_typed_400(
    items: list[tuple[str, str]], code: str
) -> None:
    with pytest.raises(HTTPException) as caught:
        validate_ideation_lookup_query(_request(items))
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == code


def test_spec_request_applies_all_eligibility_filters_before_window() -> None:
    request = lookup_page_request(
        "spec_lookup",
        "b1",
        statuses="draft,review,draft",
        search="needle",
        offset=20,
        limit=10,
        linked_to_cards=True,
        include_archived_cards=False,
    )

    assert request.surface == "spec_lookup"
    assert [(item.field, item.operator, item.value) for item in request.scope] == [
        ("board_id", "eq", "b1"),
        ("archived", "is_false", None),
    ]
    assert [(item.field, item.operator, item.value) for item in request.filters] == [
        ("status", "in", ("draft", "review")),
        ("title", "ilike", "%needle%"),
        ("linked_to_active_cards", "is_true", None),
    ]
    assert request.offset == 20
    assert request.limit == 10


def test_spec_archived_card_toggle_changes_only_the_link_predicate() -> None:
    request = lookup_page_request(
        "spec_lookup",
        "b1",
        statuses=None,
        search=None,
        offset=0,
        limit=50,
        linked_to_cards=True,
        include_archived_cards=True,
    )

    assert [(item.field, item.operator) for item in request.filters] == [
        ("linked_to_cards", "is_true")
    ]
    assert [(item.field, item.operator) for item in request.scope] == [
        ("board_id", "eq"),
        ("archived", "is_false"),
    ]


def test_ideation_request_has_compact_filters_and_no_link_dimension() -> None:
    request = lookup_page_request(
        "ideation_lookup",
        "b1",
        statuses="review,cancelled",
        search="idea",
        offset=3,
        limit=7,
    )

    assert request.surface == "ideation_lookup"
    assert [(item.field, item.operator, item.value) for item in request.filters] == [
        ("status", "in", ("review", "cancelled")),
        ("title", "ilike", "%idea%"),
    ]
    assert request.offset == 3
    assert request.limit == 7
