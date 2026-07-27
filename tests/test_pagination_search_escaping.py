"""Literal-search regression coverage for every paginated REST constructor."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.requests import Request

from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import Base
from okto_pulse.community.api.cards_pagination import card_page_request
from okto_pulse.community.api.columns_pagination import (
    column_page_request,
    parse_columns_parameters,
)
from okto_pulse.community.api.lookups import lookup_page_request
from okto_pulse.community.api.pagination import search_groups
from okto_pulse.community.api.refinements_pagination import (
    refinement_board_page_request,
)
from okto_pulse.community.sql_like import literal_contains_pattern
from okto_pulse.core.application.use_cases.entity_pagination import EntityPageService
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    get_application_persistence_port,
    register_application_persistence_port,
    reset_application_persistence_port_for_tests,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("_", "%\\_%"),
        ("%", "%\\%%"),
        ("\\", "%\\\\%"),
        ("MiX_%\\", "%MiX\\_\\%\\\\%"),
    ],
)
def test_literal_contains_pattern_escapes_like_metacharacters(
    value: str,
    expected: str,
) -> None:
    assert literal_contains_pattern(value) == expected


def _request(items: list[tuple[str, str]]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": urlencode(items, doseq=True).encode(),
        }
    )


def _group_patterns(groups: tuple[tuple[object, ...], ...]) -> set[object]:
    return {getattr(predicate, "value") for group in groups for predicate in group}


def test_all_paginated_rest_constructors_share_the_literal_pattern() -> None:
    search = "literal_%\\tail"
    expected = "%literal\\_\\%\\\\tail%"

    assert _group_patterns(search_groups(search, ("title", "description"))) == {
        expected
    }

    cards = card_page_request(
        "b1",
        status_value=None,
        spec_ids=None,
        sprint_id=None,
        priority=None,
        card_types=None,
        assignee_id=None,
        labels=None,
        search=search,
        include_archived=False,
        offset=0,
        limit=25,
    )
    assert _group_patterns(cards.any_groups) == {expected}

    parameters = parse_columns_parameters(
        _request([("per_column_limit", "25"), ("search", search)])
    )
    assert parameters is not None
    columns = column_page_request("b1", "not_started", parameters)
    assert _group_patterns(columns.any_groups) == {expected}

    refinements = refinement_board_page_request(
        "b1",
        status_value=None,
        search=search,
        derivation_pending=None,
        include_archived=False,
        labels=None,
        offset=0,
        limit=25,
    )
    assert _group_patterns(refinements.any_groups) == {expected}

    lookup = lookup_page_request(
        "spec_lookup",
        "b1",
        statuses=None,
        search=search,
        offset=0,
        limit=25,
    )
    assert {
        predicate.value for predicate in lookup.filters if predicate.operator == "ilike"
    } == {expected}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("search", "expected_id"),
    [
        ("case_", "literal-underscore"),
        ("100% ready", "literal-percent"),
        ("folder\\leaf", "literal-escape"),
    ],
)
async def test_literal_search_preserves_case_insensitivity_and_total_filtered(
    tmp_path: Path,
    search: str,
    expected_id: str,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'search.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "INSERT INTO boards (id, name, owner_id, realm_id) "
                "VALUES ('b1', 'Board', 'owner', 'local')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO cards "
                "(id, board_id, title, status, position, archived, created_by, "
                "card_type) VALUES "
                "('literal-underscore', 'b1', 'Release CASE_tag', 'not_started', "
                "1, 0, 'owner', 'normal'), "
                "('decoy-underscore', 'b1', 'release caseXtag', 'not_started', "
                "2, 0, 'owner', 'normal'), "
                "('literal-percent', 'b1', 'Release 100% READY', 'not_started', "
                "3, 0, 'owner', 'normal'), "
                "('decoy-percent', 'b1', 'release 100X ready', 'not_started', "
                "4, 0, 'owner', 'normal'), "
                "('literal-escape', 'b1', 'Folder\\LEAF', 'not_started', "
                "5, 0, 'owner', 'normal'), "
                "('decoy-escape', 'b1', 'folderXleaf', 'not_started', "
                "6, 0, 'owner', 'normal')"
            )
        )

    adapter = CommunitySqlAlchemyApplicationPersistence()
    try:
        previous = get_application_persistence_port()
    except Exception:  # noqa: BLE001 - an unset isolated port is valid
        previous = None
    register_application_persistence_port(adapter)
    try:
        async with AsyncSession(engine) as session:
            session.info["realm_scope"] = RealmScope.local()
            page = await EntityPageService(session).list(
                card_page_request(
                    "b1",
                    status_value=None,
                    spec_ids=None,
                    sprint_id=None,
                    priority=None,
                    card_types=None,
                    assignee_id=None,
                    labels=None,
                    search=search,
                    include_archived=False,
                    offset=0,
                    limit=25,
                )
            )

        assert page.total_filtered == 1
        assert page.total_overall == 6
        assert [record.id for record in page.items] == [expected_id]
    finally:
        if previous is None:
            reset_application_persistence_port_for_tests()
        else:
            register_application_persistence_port(previous)
        await engine.dispose()
