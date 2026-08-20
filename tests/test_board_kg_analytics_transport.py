from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from okto_pulse.community.api import analytics as analytics_api


CANONICAL = {
    "contract_version": "1",
    "query_fingerprint": "a" * 64,
    "result_state": "unavailable",
    "health": {
        "state": "healthy",
        "classification_reason": "metric_unavailable",
        "reason_codes": [],
        "components": [],
    },
    "debt_domains": {
        "active_queue_count": None,
        "technical_dlq_count": None,
        "canonical_debt_count": None,
    },
}


def test_board_kg_command_uses_canonical_half_open_window() -> None:
    command = analytics_api._board_kg_analytics_command(
        "board-1",
        date_from="2026-08-19",
        date_to="2026-08-20",
        as_of="2026-08-20T12:00:00Z",
    )

    assert command.as_of == datetime(2026, 8, 20, 12, tzinfo=UTC)
    assert command.window.from_inclusive == datetime(2026, 8, 19, tzinfo=UTC)
    assert command.window.to_exclusive == datetime(2026, 8, 21, tzinfo=UTC)


def test_canonical_csv_flattening_preserves_null_and_empty_values() -> None:
    rows = dict(analytics_api._flatten_canonical_payload(CANONICAL))

    assert rows["$.result_state"] == '"unavailable"'
    assert rows["$.health.reason_codes"] == "[]"
    assert rows["$.debt_domains.active_queue_count"] == "null"


@pytest.mark.asyncio
async def test_rest_and_complete_csv_share_the_exact_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(_self, command, *, actor, uow):
        assert command.board_id == "board-1"
        assert actor.actor_id == "user-1"
        assert uow is sentinel_uow
        return SimpleNamespace(data=CANONICAL)

    monkeypatch.setattr(analytics_api.BoardKgAnalyticsUseCase, "execute", execute)
    sentinel_uow = object()

    rest = await analytics_api.board_kg_analytics(
        "board-1",
        date_from="2026-08-19",
        date_to="2026-08-20",
        as_of="2026-08-20T12:00:00Z",
        user_id="user-1",
        uow=sentinel_uow,
    )
    response = await analytics_api.board_kg_analytics_export(
        "board-1",
        date_from="2026-08-19",
        date_to="2026-08-20",
        as_of="2026-08-20T12:00:00Z",
        user_id="user-1",
        uow=sentinel_uow,
    )
    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(
        chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
    )
    csv_rows = list(csv.DictReader(io.StringIO(body)))
    reconstructed = {row["path"]: json.loads(row["json_value"]) for row in csv_rows}

    assert rest is CANONICAL
    assert reconstructed["$.query_fingerprint"] == CANONICAL["query_fingerprint"]
    assert reconstructed["$.result_state"] == CANONICAL["result_state"]
    assert reconstructed["$.health.state"] == CANONICAL["health"]["state"]
    assert reconstructed["$.debt_domains.active_queue_count"] is None


@pytest.mark.asyncio
async def test_canonical_coverage_rest_and_csv_share_factual_skip_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coverage = {
        "query_fingerprint": "b" * 64,
        "totals": {
            "state": "available",
            "applicable": 2,
            "covered": 0,
            "uncovered": 2,
            "skipped": 2,
            "value": 0.0,
        },
        "coverage": [],
    }

    async def execute(_self, command, *, actor, uow):
        assert command.board_id == "board-1"
        return SimpleNamespace(data=coverage)

    monkeypatch.setattr(
        analytics_api.CoverageTraceabilityAnalyticsUseCase,
        "execute",
        execute,
    )
    uow = object()
    rest = await analytics_api.canonical_board_coverage(
        "board-1",
        date_from="2026-08-19",
        date_to="2026-08-20",
        as_of="2026-08-20T12:00:00Z",
        user_id="user-1",
        uow=uow,
    )
    response = await analytics_api.canonical_board_coverage_export(
        "board-1",
        date_from="2026-08-19",
        date_to="2026-08-20",
        as_of="2026-08-20T12:00:00Z",
        user_id="user-1",
        uow=uow,
    )
    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(
        chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
    )
    rows = {
        row["path"]: json.loads(row["json_value"])
        for row in csv.DictReader(io.StringIO(body))
    }

    assert rest["totals"]["skipped"] == 2
    assert rest["totals"]["covered"] == 0
    assert rows["$.totals.skipped"] == 2
    assert rows["$.totals.covered"] == 0
