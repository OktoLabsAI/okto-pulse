from __future__ import annotations

from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from okto_pulse.community.api.columns_pagination import (
    column_page_request,
    parse_columns_parameters,
)


def _request(items: list[tuple[str, str]] = []) -> Request:  # noqa: B006
    query = urlencode(items, doseq=True).encode()
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": query})


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([('offset', '1')], "params_require_per_column_limit"),
        ([('per_column_limit', 'x')], "per_column_limit_invalid"),
        ([('per_column_limit', '0')], "per_column_limit_out_of_bounds"),
        ([('per_column_limit', '101')], "per_column_limit_out_of_bounds"),
        ([('per_column_limit', '25'), ('offset', '1')], "offset_requires_column"),
        ([('per_column_limit', '25'), ('column', 'done'), ('offset', '-1')], "offset_invalid"),
        ([('per_column_limit', '25'), ('column', 'wat')], "unknown_column"),
        ([('per_column_limit', '25'), ('card_types', 'done')], "card_types_malformed"),
        ([('per_column_limit', '25'), ('card_types', 'done:nope')], "card_types_invalid"),
        (
            [('per_column_limit', '25'), ('include_archived', 'yes')],
            "include_archived_invalid",
        ),
    ],
)
def test_columns_query_typed_400(items: list[tuple[str, str]], code: str) -> None:
    with pytest.raises(HTTPException) as caught:
        parse_columns_parameters(_request(items))
    assert caught.value.status_code == 400
    assert caught.value.detail["error"] == code


def test_include_archived_alone_stays_on_literal_legacy_branch() -> None:
    assert parse_columns_parameters(_request([("include_archived", "true")])) is None


def test_repeated_card_types_are_status_scoped_and_last_value_wins() -> None:
    parsed = parse_columns_parameters(
        _request(
            [
                ("per_column_limit", "25"),
                ("card_types", "done:normal"),
                ("card_types", "in_progress:test,bug"),
                ("card_types", "done:test"),
                ("spec_ids", "s1,__unlinked__,s2"),
                ("search", "needle"),
            ]
        )
    )
    assert parsed is not None
    assert parsed.card_types_by_status == {
        "done": ("test",),
        "in_progress": ("test", "bug"),
    }
    assert parsed.spec_ids == ("s1", "s2")
    assert parsed.include_unlinked is True

    request = column_page_request("b", "done", parsed)
    assert request.offset == 0
    assert request.limit == 25
    assert request.any_filters[-1].operator == "is_none"
    assert request.any_groups[0][0].operator == "ilike"
    assert any(item.field == "card_type" for item in request.filters)


def test_column_without_offset_defaults_to_zero() -> None:
    parsed = parse_columns_parameters(
        _request([("per_column_limit", "9"), ("column", "validation")])
    )
    assert parsed is not None
    assert parsed.offset == 0
