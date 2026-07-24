"""C8 board-wide refinements query parsing and Core request contract."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from okto_pulse.community.api.refinements_pagination import (
    refinement_board_page_request,
    validate_board_refinement_query,
)


def _request(items: list[tuple[str, str]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/boards/b1/refinements",
            "query_string": urlencode(items, doseq=True).encode(),
        }
    )


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([("offset", "x")], "offset_invalid"),
        ([("offset", "-1")], "offset_out_of_bounds"),
        ([("limit", "x")], "limit_invalid"),
        ([("limit", "0")], "limit_not_allowed"),
        ([("limit", "37")], "limit_not_allowed"),
        ([("include_archived", "wat")], "include_archived_invalid"),
        ([("derivation_pending", "wat")], "derivation_pending_invalid"),
        ([("status", "unknown")], "status_invalid"),
    ],
)
def test_board_refinement_query_errors_are_typed_400(
    items: list[tuple[str, str]], code: str
) -> None:
    with pytest.raises(HTTPException) as caught:
        validate_board_refinement_query(_request(items))
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == code


def test_complete_request_preserves_label_and_search_as_independent_or_dimensions() -> (
    None
):
    request = refinement_board_page_request(
        "b1",
        status_value="done",
        search="needle",
        derivation_pending=True,
        include_archived=True,
        labels="blue,green,blue",
        offset=25,
        limit=25,
    )

    assert request.surface == "refinement_board"
    assert [(item.field, item.operator, item.value) for item in request.scope] == [
        ("board_id", "eq", "b1")
    ]
    assert [(item.field, item.operator, item.value) for item in request.filters] == [
        ("status", "eq", "done"),
        ("derivation_pending", "is_true", None),
    ]

    # (blue OR green) AND (title OR description OR labels OR ideation_title)
    # is one OR-of-AND DNF dimension: 2 * 4 branches, all pre-window.
    assert len(request.any_groups) == 8
    assert all(len(branch) == 2 for branch in request.any_groups)
    assert {branch[0].field for branch in request.any_groups} == {"labels"}
    assert {branch[0].operator for branch in request.any_groups} == {"json_member"}
    assert {branch[0].value for branch in request.any_groups} == {
        "blue",
        "green",
    }
    assert {branch[1].field for branch in request.any_groups} == {
        "title",
        "description",
        "labels",
        "ideation_title",
    }
    assert {branch[1].operator for branch in request.any_groups} == {"ilike"}
    assert {branch[1].value for branch in request.any_groups} == {"%needle%"}
    assert request.offset == request.limit == 25


def test_default_scope_excludes_archived_and_has_no_discretionary_filters() -> None:
    request = refinement_board_page_request(
        "b1",
        status_value=None,
        search=None,
        derivation_pending=None,
        include_archived=False,
        labels=None,
        offset=0,
        limit=25,
    )

    assert [(item.field, item.operator, item.value) for item in request.scope] == [
        ("board_id", "eq", "b1"),
        ("archived", "is_false", None),
    ]
    assert request.filters == ()
    assert request.any_filters == ()
    assert request.any_groups == ()


def test_false_derivation_filter_is_explicit_and_search_has_all_consumer_fields() -> (
    None
):
    request = refinement_board_page_request(
        "b1",
        status_value=None,
        search="Idea TITLE",
        derivation_pending=False,
        include_archived=False,
        labels=None,
        offset=0,
        limit=50,
    )

    assert [(item.field, item.operator, item.value) for item in request.filters] == [
        ("derivation_pending", "is_false", None)
    ]
    assert len(request.any_groups) == 4
    assert {branch[0].field for branch in request.any_groups} == {
        "title",
        "description",
        "labels",
        "ideation_title",
    }
    assert {branch[0].value for branch in request.any_groups} == {"%Idea TITLE%"}


def test_label_filter_uses_exact_json_membership_not_substring_matching() -> None:
    request = refinement_board_page_request(
        "b1",
        status_value=None,
        search=None,
        derivation_pending=None,
        include_archived=False,
        labels="blue",
        offset=0,
        limit=25,
    )

    assert len(request.any_groups) == 1
    assert len(request.any_groups[0]) == 1
    predicate = request.any_groups[0][0]
    assert (predicate.field, predicate.operator, predicate.value) == (
        "labels",
        "json_member",
        "blue",
    )
