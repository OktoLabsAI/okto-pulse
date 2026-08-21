from __future__ import annotations

import csv
import io
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from okto_pulse.community.api import analytics as analytics_api
from okto_pulse.community.api.analytics_transport import (
    CanonicalBoardKgAnalyticsResponseDTO,
    CanonicalDeliveryForecastResponseDTO,
)
from okto_pulse.core.application.use_cases import EntityNotFoundError
from okto_pulse.core.ports.delivery_forecast import (
    ForecastDependencyContractMismatch,
    ForecastInputUnavailable,
    HistoricalAnalyticsAsOfUnsupported,
)
from okto_pulse.core.ports.board_kg_analytics import (
    BoardKgAnalyticsContractMismatch,
    BoardKgCognitiveStatus,
    BoardKgHistoricalAsOfUnsupported,
    BoardKgMetricUnavailable,
)


SPRINT_ID = UUID("12345678-1234-5678-1234-567812345678")


def _nonready_forecast() -> dict[str, object]:
    return {
        "contract_version": "1",
        "dependency_versions": {
            "analytics_foundation": "1",
            "delivery_phase_1": "1",
        },
        "query_fingerprint": "a" * 64,
        "filters": [
            {
                "field": "sprint_id",
                "operator": "in",
                "value": [str(SPRINT_ID)],
            }
        ],
        "as_of": "2026-08-21T11:00:00.000000Z",
        "board_id": "board-1",
        "result_state": "partial",
        "provenance": {
            "observed_at": "2026-08-21T10:59:00.000000Z",
            "currentness": "current",
            "reason": None,
            "sources": [
                {
                    "authority": "delivery_commitment",
                    "reference": "board:board-1:sprints",
                    "timestamp_field": "completed_at",
                }
            ],
        },
        "readiness": {
            "ready": False,
            "state": "insufficient_history",
            "reason": "minimum_comparable_observations_not_met",
            "remediation": "Complete more comparable sprints.",
            "actual_observations": 5,
            "required_observations": 8,
            "rule_version": "forecast-readiness-v1",
        },
        "backtest": {
            "state": "empty",
            "error": None,
            "calibration": None,
            "method_version": "empirical-quantile-v1",
            "sample_size": 0,
            "evaluation_window": None,
            "reason": "no_comparable_backtest_outcomes",
        },
        "population_scope": {
            "scope_ref": "actor:user-1",
            "accessible_count": 5,
            "excluded_count": 0,
        },
        "exclusions": {
            "restricted_count": 0,
            "excluded_count": 0,
            "reasons": [],
        },
    }


def _ready_forecast() -> dict[str, object]:
    payload = _nonready_forecast()
    payload["result_state"] = "available"
    payload["readiness"] = {
        "ready": True,
        "state": "ready",
        "reason": None,
        "remediation": None,
        "actual_observations": 8,
        "required_observations": 8,
        "rule_version": "forecast-readiness-v1",
    }
    payload["forecast"] = {
        "point": 12.0,
        "lower_bound": 8.0,
        "upper_bound": 16.0,
        "confidence_level": 0.8,
        "horizon": "next_sprint",
        "assumptions": ["Authorized completed observations are comparable."],
        "sample_size": 8,
        "source_period": {
            "from": "2026-06-01T00:00:00.000000Z",
            "to": "2026-08-01T00:00:00.000000Z",
        },
        "method_version": "empirical-quantile-v1",
    }
    payload["backtest"] = {
        "state": "available",
        "error": 1.25,
        "calibration": 0.75,
        "method_version": "empirical-quantile-v1",
        "sample_size": 4,
        "evaluation_window": {
            "from": "2026-06-01T00:00:00.000000Z",
            "to": "2026-08-01T00:00:00.000000Z",
        },
        "reason": None,
    }
    return payload


@pytest.mark.parametrize("payload_factory", (_nonready_forecast, _ready_forecast))
def test_delivery_forecast_dto_preserves_the_complete_core_projection(
    payload_factory,
) -> None:
    payload = payload_factory()

    serialized = CanonicalDeliveryForecastResponseDTO.model_validate(
        payload
    ).model_dump(mode="json", by_alias=True)

    assert serialized == payload


def test_nonready_forecast_contract_forbids_estimate_and_unknown_fields() -> None:
    with_estimate = _nonready_forecast()
    with_estimate["forecast"] = None
    with_global_signal = _nonready_forecast()
    with_global_signal["global_discovery_count"] = 12

    with pytest.raises(ValidationError):
        CanonicalDeliveryForecastResponseDTO.model_validate(with_estimate)
    with pytest.raises(ValidationError):
        CanonicalDeliveryForecastResponseDTO.model_validate(with_global_signal)


def test_delivery_forecast_route_publishes_closed_response_model() -> None:
    route_models = {
        (route.path, next(iter(route.methods))): route.response_model
        for route in analytics_api.router.routes
        if getattr(route, "methods", None)
    }

    assert route_models[
        ("/boards/{board_id}/analytics/delivery-forecast", "GET")
    ] is CanonicalDeliveryForecastResponseDTO


@pytest.mark.asyncio
async def test_delivery_forecast_rest_and_csv_share_authorized_nonready_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _nonready_forecast()
    calls = []

    async def execute(_self, command, *, actor, uow):
        assert actor.actor_id == "user-1"
        assert actor.board_id == "board-1"
        assert uow is sentinel_uow
        assert command.window.from_inclusive == datetime(2026, 6, 1, tzinfo=UTC)
        assert command.window.to_exclusive == datetime(2026, 8, 21, tzinfo=UTC)
        assert command.as_of.tzinfo is not None
        assert command.historical_as_of is None
        assert command.horizon == "next_sprint"
        assert command.confidence_level == 0.8
        assert command.method_version == "empirical-quantile-v1"
        assert command.filters[0].canonical_dict() == {
            "field": "sprint_id",
            "operator": "in",
            "value": [str(SPRINT_ID)],
        }
        calls.append(command)
        return SimpleNamespace(data=payload)

    monkeypatch.setattr(analytics_api.DeliveryForecastUseCase, "execute", execute)
    sentinel_uow = object()
    kwargs = {
        "date_from": "2026-06-01",
        "date_to": "2026-08-20",
        "range_value": None,
        "sprint_ids": [SPRINT_ID],
        "horizon": "next_sprint",
        "confidence_level": 0.8,
        "method_version": "empirical-quantile-v1",
        "historical_as_of": None,
        "user_id": "user-1",
        "uow": sentinel_uow,
    }

    rest = await analytics_api.delivery_forecast("board-1", **kwargs)
    response = await analytics_api.delivery_forecast_export("board-1", **kwargs)
    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(
        chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
    )
    rows = {
        row["path"]: json.loads(row["json_value"])
        for row in csv.DictReader(io.StringIO(body))
    }

    assert rest is payload
    assert len(calls) == 2
    assert "$.forecast" not in rows
    assert rows["$.readiness.state"] == "insufficient_history"
    assert rows["$.readiness.actual_observations"] == 5
    assert rows["$.backtest.error"] is None
    assert (
        "board-board-1-delivery-forecast.csv"
        in response.headers["content-disposition"]
    )


@pytest.mark.asyncio
async def test_ambiguous_range_is_rejected_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(*_args, **_kwargs):
        raise AssertionError("projection must not execute")

    monkeypatch.setattr(analytics_api.DeliveryForecastUseCase, "execute", execute)

    with pytest.raises(HTTPException) as caught:
        await analytics_api._delivery_forecast_payload(
            "board-1",
            date_from=None,
            date_to=None,
            range_value="last_90_days",
            historical_as_of=None,
            sprint_ids=(),
            horizon="next_sprint",
            confidence_level=0.8,
            method_version="empirical-quantile-v1",
            user_id="user-1",
            uow=object(),
        )

    assert caught.value.status_code == 400
    assert caught.value.detail["code"] == "analytics_query_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    (
        (
            ForecastDependencyContractMismatch(),
            409,
            "forecast_dependency_contract_mismatch",
        ),
        (ForecastInputUnavailable(), 503, "forecast_input_unavailable"),
        (
            HistoricalAnalyticsAsOfUnsupported(),
            409,
            "analytics_historical_as_of_unsupported",
        ),
    ),
)
async def test_delivery_forecast_typed_errors_keep_core_http_contract(
    monkeypatch: pytest.MonkeyPatch,
    error,
    expected_status: int,
    expected_code: str,
) -> None:
    async def execute(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(analytics_api.DeliveryForecastUseCase, "execute", execute)

    with pytest.raises(HTTPException) as caught:
        await analytics_api._delivery_forecast_payload(
            "board-1",
            date_from=None,
            date_to=None,
            range_value=None,
            historical_as_of=None,
            sprint_ids=(),
            horizon="next_sprint",
            confidence_level=0.8,
            method_version="empirical-quantile-v1",
            user_id="user-1",
            uow=object(),
        )

    assert caught.value.status_code == expected_status
    assert caught.value.detail["error"] == expected_code
    assert caught.value.detail["status_code"] == expected_status


@pytest.mark.asyncio
async def test_delivery_forecast_missing_board_is_non_enumerable_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(*_args, **_kwargs):
        raise EntityNotFoundError("board", "board-1")

    monkeypatch.setattr(analytics_api.DeliveryForecastUseCase, "execute", execute)

    with pytest.raises(HTTPException) as caught:
        await analytics_api._delivery_forecast_payload(
            "board-1",
            date_from=None,
            date_to=None,
            range_value=None,
            historical_as_of=None,
            sprint_ids=(),
            horizon="next_sprint",
            confidence_level=0.8,
            method_version="empirical-quantile-v1",
            user_id="user-1",
            uow=object(),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {
        "code": "board_not_found",
        "message": "Board not found",
    }


def test_delivery_forecast_payload_fixture_is_not_mutated_by_ready_variant() -> None:
    nonready = _nonready_forecast()
    original = deepcopy(nonready)
    _ready_forecast()
    assert nonready == original


def test_kg_effectiveness_routes_publish_the_closed_v2_response_model() -> None:
    route_models = {
        (route.path, next(iter(route.methods))): route.response_model
        for route in analytics_api.router.routes
        if getattr(route, "methods", None)
    }

    assert route_models[
        ("/boards/{board_id}/analytics/kg-effectiveness", "GET")
    ] is CanonicalBoardKgAnalyticsResponseDTO
    assert route_models[
        ("/boards/{board_id}/analytics/kg", "GET")
    ] is CanonicalBoardKgAnalyticsResponseDTO


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
        assert command.cursor == "offset:25"
        assert command.limit == 25
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
    assert "board-board-1-kg-analytics.csv" in response.headers[
        "content-disposition"
    ]


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
            as_of="2026-08-20" if isinstance(error, BoardKgHistoricalAsOfUnsupported) else None,
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
