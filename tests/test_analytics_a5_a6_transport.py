from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from okto_pulse.community.api import analytics as analytics_api
from okto_pulse.community.api.analytics_transport import (
    CanonicalBoardKgAnalyticsResponseDTO,
    DeliveryIntelligenceResponseDTO,
)
from okto_pulse.core.application.use_cases import EntityNotFoundError
from okto_pulse.core.ports.board_kg_analytics import (
    BoardKgAnalyticsContractMismatch,
    BoardKgCognitiveStatus,
    BoardKgHistoricalAsOfUnsupported,
    BoardKgMetricUnavailable,
)


SPRINT_ID = UUID("12345678-1234-5678-1234-567812345678")


def _delivery_metric(
    value: int | float | None,
    *,
    numerator: int | None,
    denominator: int | None,
    sample_size: int,
    state: str = "available",
    reason: str | None = None,
    unit: str | None = "percent",
) -> dict[str, object]:
    return {
        "state": state,
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "sample_size": sample_size,
        "reason": reason,
        "unit": unit,
    }


def _delivery_intelligence_projection() -> dict[str, object]:
    period = {
        "from": "2026-06-01T00:00:00.000000Z",
        "to": "2026-08-21T00:00:00.000000Z",
    }
    return {
        "contract_version": "1",
        "foundation_version": "1",
        "query_fingerprint": "d" * 64,
        "filters": [
            {
                "field": "sprint_id",
                "operator": "in",
                "value": [str(SPRINT_ID)],
            },
            {"field": "lane", "operator": "in", "value": ["hotfix", "normal"]},
            {
                "field": "role",
                "operator": "in",
                "value": ["implementation_agent", "validation_agent"],
            },
            {"field": "contribution_view", "operator": "eq", "value": "operator"},
        ],
        "as_of": "2026-08-21T12:00:00.000000Z",
        "board_id": "board-1",
        "result_state": "available",
        "provenance": {
            "observed_at": "2026-08-21T12:00:00.000000Z",
            "currentness": "current",
            "reason": None,
            "sources": [
                {
                    "authority": "sprint_activation_baselines",
                    "reference": "board:board-1:delivery-intelligence:v1",
                    "timestamp_field": "sprints.updated_at",
                }
            ],
        },
        "population_scope": {
            "scope_ref": "actor:user-1",
            "accessible_count": 1,
            "excluded_count": 0,
        },
        "exclusions": {
            "restricted_count": 0,
            "excluded_count": 0,
            "reasons": [],
        },
        "minimum_sample_size": 9,
        "summary": {
            "commitment_reliability": _delivery_metric(
                80.0, numerator=4, denominator=5, sample_size=1
            ),
            "throughput": {
                "state": "available",
                "total": 7,
                "normal": 5,
                "hotfix": 2,
                "sample_size": 1,
                "reason": None,
            },
            "carryover": _delivery_metric(
                None,
                numerator=None,
                denominator=None,
                sample_size=0,
                state="unavailable",
                reason="carryover_lineage_not_persisted",
                unit=None,
            ),
            "hotfix_share": _delivery_metric(
                28.6, numerator=2, denominator=7, sample_size=7
            ),
            "scope": {
                "state": "available",
                "committed_at_activation": 5,
                "completed_from_commitment": 4,
                "added_after_activation": 1,
                "removed_after_activation": 0,
                "sample_size": 1,
                "reason": None,
            },
        },
        "sprints": [
            {
                "sprint_id": str(SPRINT_ID),
                "title": "Release 24",
                "lane_type": "normal",
                "done_cards": 5,
                "commitment": {
                    "state": "available",
                    "original_member_count": 5,
                    "added_count": 1,
                    "removed_count": 0,
                },
            }
        ],
        "contributions": [
            {
                "subject_id": "user-1",
                "subject_label": "You",
                "visibility": "self",
                "role": "Implementation agent",
                "done_count": 5,
                "first_pass": _delivery_metric(
                    75.0, numerator=3, denominator=4, sample_size=4
                ),
                "validation_success": _delivery_metric(
                    100.0, numerator=2, denominator=2, sample_size=2
                ),
                "rework_introduced": 1,
                "rework_resolved": 1,
                "median_cycle_hours": _delivery_metric(
                    18.5,
                    numerator=None,
                    denominator=None,
                    sample_size=5,
                    unit="hours",
                ),
                "sample_size": 5,
                "period": period,
            }
        ],
        "next_cursor": "offset:1",
    }


def test_delivery_intelligence_route_publishes_closed_response_model() -> None:
    payload = _delivery_intelligence_projection()
    serialized = DeliveryIntelligenceResponseDTO.model_validate(payload).model_dump(
        mode="json", by_alias=True
    )
    route_models = {
        (route.path, next(iter(route.methods))): route.response_model
        for route in analytics_api.router.routes
        if getattr(route, "methods", None)
    }

    assert serialized == payload
    assert (
        route_models[("/boards/{board_id}/analytics/delivery-intelligence", "GET")]
        is DeliveryIntelligenceResponseDTO
    )

    payload["browser_derived_reliability"] = 100
    with pytest.raises(ValidationError):
        DeliveryIntelligenceResponseDTO.model_validate(payload)


@pytest.mark.asyncio
async def test_delivery_intelligence_rest_page_and_complete_csv_share_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _delivery_intelligence_projection()
    calls = []

    async def execute(_self, command, *, actor, uow):
        is_rest_call = not calls
        assert actor.actor_id == "user-1"
        assert actor.board_id == "board-1"
        assert uow is sentinel_uow
        assert command.window.from_inclusive == datetime(2026, 6, 1, tzinfo=UTC)
        assert command.window.to_exclusive == datetime(2026, 8, 21, tzinfo=UTC)
        assert command.as_of.tzinfo is not None
        if is_rest_call:
            assert command.cursor == "offset:7"
            assert command.limit == 17
        else:
            assert command.cursor is None
            assert command.limit == 100
        assert command.minimum_sample_size == 9
        assert [item.canonical_dict() for item in command.filters] == payload["filters"]
        calls.append(command)
        return SimpleNamespace(
            data=payload if is_rest_call else {**payload, "next_cursor": None}
        )

    monkeypatch.setattr(analytics_api.DeliveryIntelligenceUseCase, "execute", execute)
    sentinel_uow = object()
    kwargs = {
        "date_from": "2026-06-01",
        "date_to": "2026-08-20",
        "as_of": None,
        "range_value": None,
        "sprint_ids": [SPRINT_ID],
        "lanes": ["normal", "HOTFIX", "normal"],
        "roles": ["Validation_Agent", " implementation_agent "],
        "contribution_view": "OPERATOR",
        "cursor": "offset:7",
        "limit": 17,
        "minimum_sample_size": 9,
        "user_id": "user-1",
        "uow": sentinel_uow,
    }

    rest = await analytics_api.delivery_intelligence("board-1", **kwargs)
    response = await analytics_api.delivery_intelligence_export("board-1", **kwargs)
    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(
        chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
    )
    csv_rows = [
        (row["path"], row["json_value"]) for row in csv.DictReader(io.StringIO(body))
    ]

    assert rest is payload
    assert len(calls) == 2
    assert csv_rows == analytics_api._flatten_canonical_payload(
        {**payload, "next_cursor": None}
    )
    assert (
        "board-board-1-delivery-intelligence.csv"
        in response.headers["content-disposition"]
    )


@pytest.mark.asyncio
async def test_delivery_intelligence_csv_drains_every_cursor_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _delivery_intelligence_projection()
    first["sprints"] = [{"sprint_id": "sprint-1", "title": "Sprint 1"}]
    first["next_cursor"] = "offset:100"
    second = {
        **first,
        "sprints": [{"sprint_id": "sprint-2", "title": "Sprint 2"}],
        "next_cursor": None,
    }
    commands = []

    async def execute(_self, command, *, actor, uow):
        commands.append(command)
        return SimpleNamespace(data=first if command.cursor is None else second)

    monkeypatch.setattr(analytics_api.DeliveryIntelligenceUseCase, "execute", execute)
    response = await analytics_api.delivery_intelligence_export(
        "board-1",
        date_from="2026-06-01",
        date_to="2026-08-20",
        as_of=None,
        range_value=None,
        sprint_ids=None,
        lanes=None,
        roles=None,
        contribution_view="self_and_aggregates",
        cursor="offset:999",
        limit=1,
        minimum_sample_size=5,
        user_id="user-1",
        uow=object(),
    )
    chunks = [chunk async for chunk in response.body_iterator]
    rows = {
        row["path"]: json.loads(row["json_value"])
        for row in csv.DictReader(
            io.StringIO(
                "".join(
                    chunk.decode() if isinstance(chunk, bytes) else chunk
                    for chunk in chunks
                )
            )
        )
    }

    assert [(command.cursor, command.limit) for command in commands] == [
        (None, 100),
        ("offset:100", 100),
    ]
    assert commands[0].as_of == commands[1].as_of
    assert rows["$.sprints[0].sprint_id"] == "sprint-1"
    assert rows["$.sprints[1].sprint_id"] == "sprint-2"
    assert rows["$.next_cursor"] is None


@pytest.mark.asyncio
async def test_delivery_intelligence_rejects_historical_as_of_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(*_args, **_kwargs):
        raise AssertionError("current-state projection must not execute")

    monkeypatch.setattr(analytics_api.DeliveryIntelligenceUseCase, "execute", execute)

    with pytest.raises(HTTPException) as caught:
        await analytics_api._delivery_intelligence_payload(
            "board-1",
            date_from=None,
            date_to=None,
            as_of="2026-01-01T00:00:00Z",
            range_value=None,
            sprint_ids=(),
            lanes=(),
            roles=(),
            contribution_view="self_and_aggregates",
            cursor=None,
            limit=50,
            minimum_sample_size=5,
            user_id="user-1",
            uow=object(),
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == {
        "code": "analytics_query_invalid",
        "message": "delivery_intelligence_historical_as_of_unsupported",
    }


@pytest.mark.asyncio
async def test_delivery_intelligence_export_rejects_historical_as_of() -> None:
    with pytest.raises(HTTPException) as caught:
        await analytics_api.delivery_intelligence_export(
            "board-1",
            date_from=None,
            date_to=None,
            as_of="2026-01-01T00:00:00Z",
            range_value=None,
            sprint_ids=None,
            lanes=None,
            roles=None,
            contribution_view="self_and_aggregates",
            cursor=None,
            limit=100,
            minimum_sample_size=5,
            user_id="user-1",
            uow=object(),
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == {
        "code": "analytics_query_invalid",
        "message": "delivery_intelligence_historical_as_of_unsupported",
    }


@pytest.mark.asyncio
async def test_delivery_intelligence_missing_board_is_non_enumerable_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(*_args, **_kwargs):
        raise EntityNotFoundError("board", "board-1")

    monkeypatch.setattr(analytics_api.DeliveryIntelligenceUseCase, "execute", execute)

    with pytest.raises(HTTPException) as caught:
        await analytics_api._delivery_intelligence_payload(
            "board-1",
            date_from=None,
            date_to=None,
            as_of=None,
            range_value=None,
            sprint_ids=(),
            lanes=(),
            roles=(),
            contribution_view="self_and_aggregates",
            cursor=None,
            limit=50,
            minimum_sample_size=5,
            user_id="user-1",
            uow=object(),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {
        "code": "board_not_found",
        "message": "Board not found",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lanes", "contribution_view", "limit", "expected_message"),
    (
        (("expedite",), "self", 50, "delivery_intelligence_lane_invalid"),
        (
            (),
            "team",
            50,
            "delivery_intelligence_contribution_view_invalid",
        ),
        ((), "self", 0, "delivery_intelligence_limit_invalid"),
    ),
)
async def test_delivery_intelligence_validation_errors_fail_before_projection(
    monkeypatch: pytest.MonkeyPatch,
    lanes: tuple[str, ...],
    contribution_view: str,
    limit: int,
    expected_message: str,
) -> None:
    async def execute(*_args, **_kwargs):
        raise AssertionError("projection must not execute")

    monkeypatch.setattr(analytics_api.DeliveryIntelligenceUseCase, "execute", execute)

    with pytest.raises(HTTPException) as caught:
        await analytics_api._delivery_intelligence_payload(
            "board-1",
            date_from=None,
            date_to=None,
            as_of=None,
            range_value=None,
            sprint_ids=(),
            lanes=lanes,
            roles=(),
            contribution_view=contribution_view,
            cursor=None,
            limit=limit,
            minimum_sample_size=5,
            user_id="user-1",
            uow=object(),
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == {
        "code": "analytics_query_invalid",
        "message": expected_message,
    }


def test_kg_effectiveness_routes_publish_the_closed_v2_response_model() -> None:
    route_models = {
        (route.path, next(iter(route.methods))): route.response_model
        for route in analytics_api.router.routes
        if getattr(route, "methods", None)
    }

    assert (
        route_models[("/boards/{board_id}/analytics/kg-effectiveness", "GET")]
        is CanonicalBoardKgAnalyticsResponseDTO
    )
    assert (
        route_models[("/boards/{board_id}/analytics/kg", "GET")]
        is CanonicalBoardKgAnalyticsResponseDTO
    )


@pytest.mark.asyncio
async def test_kg_effectiveness_rest_and_csv_share_one_authorized_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "contract_version": "2",
        "result_state": "partial",
        "diagnostics": [{"domain": "cognitive_backlog"}],
    }
    commands = []

    async def execute(_self, command, *, actor, uow):
        assert actor.actor_id == "user-1"
        assert actor.board_id == "board-1"
        assert uow is sentinel_uow
        assert command.window.from_inclusive == datetime(2026, 6, 1, tzinfo=UTC)
        assert command.window.to_exclusive == datetime(2026, 8, 21, tzinfo=UTC)
        assert command.as_of.tzinfo is not None
        assert command.historical_as_of is None
        assert command.cognitive_status == ("failed", "pending")
        assert command.artifact_types == ("card", "spec")
        if not commands:
            assert command.cursor == "offset:25"
            assert command.limit == 25
        else:
            assert command.cursor is None
            assert command.limit == 500
        commands.append(command)
        return SimpleNamespace(data=payload)

    monkeypatch.setattr(analytics_api.BoardKgAnalyticsUseCase, "execute", execute)
    sentinel_uow = object()
    kwargs = {
        "date_from": "2026-06-01",
        "date_to": "2026-08-20",
        "as_of": None,
        "cognitive_status": (
            BoardKgCognitiveStatus.FAILED,
            BoardKgCognitiveStatus.PENDING,
        ),
        "artifact_types": ("card", "spec"),
        "cursor": "offset:25",
        "limit": 25,
        "user_id": "user-1",
        "uow": sentinel_uow,
    }

    rest = await analytics_api._board_kg_analytics_payload("board-1", **kwargs)
    response = await analytics_api.board_kg_analytics_export(
        "board-1",
        date_from=kwargs["date_from"],
        date_to=kwargs["date_to"],
        as_of=None,
        cognitive_status=list(kwargs["cognitive_status"]),
        artifact_types=list(kwargs["artifact_types"]),
        cursor=kwargs["cursor"],
        limit=kwargs["limit"],
        user_id="user-1",
        uow=sentinel_uow,
    )
    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(
        chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
    )
    rows = {
        row["path"]: json.loads(row["json_value"])
        for row in csv.DictReader(io.StringIO(body))
    }

    assert rest is payload
    assert len(commands) == 2
    assert rows["$.contract_version"] == "2"
    assert rows["$.diagnostics[0].domain"] == "cognitive_backlog"
    assert "board-board-1-kg-analytics.csv" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_kg_effectiveness_csv_drains_every_cursor_page_without_fake_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = []

    async def execute(_self, command, *, actor, uow):
        commands.append(command)
        return SimpleNamespace(
            data={
                "contract_version": "2",
                "board_id": "board-1",
                "filters": [],
                "diagnostics": [
                    {"domain": "page-1" if command.cursor is None else "page-2"}
                ],
                "next_cursor": "opaque-next" if command.cursor is None else None,
            }
        )

    monkeypatch.setattr(analytics_api.BoardKgAnalyticsUseCase, "execute", execute)
    response = await analytics_api.board_kg_analytics_export(
        "board-1",
        date_from="2026-06-01",
        date_to="2026-08-20",
        as_of=None,
        cognitive_status=None,
        artifact_types=None,
        cursor="ignored",
        limit=1,
        user_id="user-1",
        uow=object(),
    )
    chunks = [chunk async for chunk in response.body_iterator]
    rows = {
        row["path"]: json.loads(row["json_value"])
        for row in csv.DictReader(
            io.StringIO(
                "".join(
                    chunk.decode() if isinstance(chunk, bytes) else chunk
                    for chunk in chunks
                )
            )
        )
    }

    assert [(command.cursor, command.limit) for command in commands] == [
        (None, 500),
        ("opaque-next", 500),
    ]
    assert commands[0].as_of == commands[1].as_of
    assert all(command.historical_as_of is None for command in commands)
    assert rows["$.complete"] is True
    assert rows["$.page_count"] == 2
    assert rows["$.pages[0].diagnostics[0].domain"] == "page-1"
    assert rows["$.pages[1].diagnostics[0].domain"] == "page-2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    (
        (
            BoardKgAnalyticsContractMismatch(),
            409,
            "kg_analytics_contract_mismatch",
        ),
        (BoardKgMetricUnavailable(), 503, "board_kg_metric_unavailable"),
        (
            BoardKgHistoricalAsOfUnsupported(),
            409,
            "analytics_historical_as_of_unsupported",
        ),
    ),
)
async def test_kg_effectiveness_typed_errors_keep_core_http_contract(
    monkeypatch: pytest.MonkeyPatch,
    error,
    expected_status: int,
    expected_code: str,
) -> None:
    async def execute(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(analytics_api.BoardKgAnalyticsUseCase, "execute", execute)

    with pytest.raises(HTTPException) as caught:
        await analytics_api._board_kg_analytics_payload(
            "board-1",
            date_from=None,
            date_to=None,
            as_of="2026-08-20"
            if isinstance(error, BoardKgHistoricalAsOfUnsupported)
            else None,
            user_id="user-1",
            uow=object(),
        )

    assert caught.value.status_code == expected_status
    assert caught.value.detail["error"] == expected_code
    assert caught.value.detail["status_code"] == expected_status


@pytest.mark.asyncio
async def test_kg_effectiveness_missing_board_is_non_enumerable_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(*_args, **_kwargs):
        raise EntityNotFoundError("board", "board-1")

    monkeypatch.setattr(analytics_api.BoardKgAnalyticsUseCase, "execute", execute)

    with pytest.raises(HTTPException) as caught:
        await analytics_api._board_kg_analytics_payload(
            "board-1",
            date_from=None,
            date_to=None,
            as_of=None,
            user_id="user-1",
            uow=object(),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {
        "code": "board_not_found",
        "message": "Board not found",
    }
