from __future__ import annotations

from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from okto_pulse.community.api.cards_pagination import (
    card_page_request,
    validate_card_list_query,
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
        ([('offset', 'x')], "offset_invalid"),
        ([('offset', '-1')], "offset_out_of_bounds"),
        ([('limit', 'x')], "limit_invalid"),
        ([('limit', '37')], "limit_not_allowed"),
        ([('include_archived', 'wat')], "include_archived_invalid"),
        ([('status', 'unknown')], "status_invalid"),
        ([('priority', 'urgent')], "priority_invalid"),
        ([('card_types', '')], "card_types_invalid"),
        ([('card_types', 'normal,unknown')], "card_types_invalid"),
    ],
)
def test_card_list_query_errors_are_typed_400(
    items: list[tuple[str, str]], code: str
) -> None:
    with pytest.raises(HTTPException) as caught:
        validate_card_list_query(_request(items))
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == code


def test_complete_filter_request_preserves_three_independent_or_dimensions() -> None:
    request = card_page_request(
        "b1",
        status_value="in_progress",
        spec_ids="s1,s2,__unlinked__",
        sprint_id="sp1",
        priority="high",
        card_types="normal,test",
        assignee_id="alice",
        labels="blue,green",
        search="needle",
        include_archived=True,
        offset=25,
        limit=25,
    )

    assert request.surface == "card_list"
    assert request.scope[0].field == "board_id"
    assert all(item.field != "archived" for item in request.scope)
    assert {(item.field, item.operator) for item in request.filters} == {
        ("status", "eq"),
        ("sprint_id", "eq"),
        ("priority", "eq"),
        ("assignee_id", "eq"),
        ("card_type", "in"),
    }
    assert [(item.field, item.operator) for item in request.any_filters] == [
        ("spec_id", "in"),
        ("spec_id", "is_none"),
    ]

    # (blue OR green) AND (title OR description OR labels search) is expanded
    # into six OR branches, each containing one predicate from each dimension.
    assert len(request.any_groups) == 6
    assert all(len(branch) == 2 for branch in request.any_groups)
    assert {branch[0].value for branch in request.any_groups} == {
        '"blue"',
        '"green"',
    }
    assert {branch[1].field for branch in request.any_groups} == {
        "title",
        "description",
        "labels",
    }
    assert request.offset == request.limit == 25


def test_default_scope_excludes_archived_cards() -> None:
    request = card_page_request(
        "b1",
        status_value=None,
        spec_ids=None,
        sprint_id=None,
        priority=None,
        card_types=None,
        assignee_id=None,
        labels=None,
        search=None,
        include_archived=False,
        offset=0,
        limit=25,
    )
    assert [(item.field, item.operator) for item in request.scope] == [
        ("board_id", "eq"),
        ("archived", "is_false"),
    ]
    assert request.any_filters == ()
    assert request.any_groups == ()
