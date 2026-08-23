from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from okto_pulse.community.api import analytics as analytics_api
from okto_pulse.community.adapters.sqlalchemy_analytics_evidence import (
    _encode_board_kg_cursor,
)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "extra"),
    (
        (analytics_api._board_kg_analytics_payload, {}),
        (analytics_api._canonical_coverage_payload, {}),
        (analytics_api._flow_health_payload, {}),
        (analytics_api._readiness_payload, {"kind": "spec"}),
    ),
)
@pytest.mark.parametrize(
    ("date_from", "date_to"),
    (("not-a-date", None), ("2026-08-20", "2026-08-19")),
)
async def test_canonical_payloads_map_invalid_temporal_input_to_400(
    payload,
    extra: dict[str, str],
    date_from: str,
    date_to: str | None,
) -> None:
    with pytest.raises(analytics_api.HTTPException) as caught:
        await payload(
            "board-1",
            date_from=date_from,
            date_to=date_to,
            as_of=None,
            user_id="user-1",
            uow=object(),
            **extra,
        )

    assert caught.value.status_code == 400
    assert caught.value.detail["code"] == "analytics_query_invalid"


@pytest.mark.asyncio
async def test_implicit_kg_window_is_stable_across_cursor_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = []

    async def execute(_self, command, *, actor, uow):
        del actor, uow
        commands.append(command)
        return SimpleNamespace(
            data={
                **CANONICAL,
                "next_cursor": (
                    _encode_board_kg_cursor(
                        snapshot_id="b" * 64,
                        observed_at=command.as_of,
                        offset=1,
                    )
                    if command.cursor is None
                    else None
                ),
            }
        )

    monkeypatch.setattr(analytics_api.BoardKgAnalyticsUseCase, "execute", execute)
    first = await analytics_api.board_kg_analytics(
        "board-1",
        user_id="user-1",
        uow=object(),
    )
    await analytics_api.board_kg_analytics(
        "board-1",
        cursor=first["next_cursor"],
        user_id="user-1",
        uow=object(),
    )

    assert len(commands) == 2
    assert commands[1].as_of == commands[0].as_of
    assert commands[1].window == commands[0].window


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


@pytest.mark.asyncio
async def test_flow_health_rest_and_csv_share_governed_episode_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flow = {
        "query_fingerprint": "c" * 64,
        "as_of": "2026-08-20T12:00:00.000000Z",
        "summary": {"healthy": 1, "stale": 1},
        "items": [
            {
                "subject": {"type": "card", "id": "card-1"},
                "current_episode": {
                    "state": "in_progress",
                    "entry_event_id": "event-2",
                    "age_seconds": 288000,
                },
            }
        ],
    }

    async def execute(_self, command, *, actor, uow):
        assert command.board_id == "board-1"
        return SimpleNamespace(data=flow)

    monkeypatch.setattr(analytics_api.FlowHealthAnalyticsUseCase, "execute", execute)
    uow = object()
    rest = await analytics_api.canonical_flow_health(
        "board-1",
        date_from="2026-08-19",
        date_to="2026-08-20",
        as_of="2026-08-20T12:00:00Z",
        user_id="user-1",
        uow=uow,
    )
    response = await analytics_api.canonical_flow_health_export(
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

    assert rest is flow
    assert rows["$.items[0].current_episode.entry_event_id"] == "event-2"
    assert rows["$.items[0].current_episode.age_seconds"] == 288000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "use_case", "rest_handler", "export_handler", "fact_path", "fact_value"),
    (
        (
            "spec",
            analytics_api.SpecReadinessAnalyticsUseCase,
            analytics_api.canonical_spec_readiness,
            analytics_api.canonical_spec_readiness_export,
            "$.specs[0].validation.measures.confidence",
            83,
        ),
        (
            "policy-resource",
            analytics_api.PolicyResourceReadinessAnalyticsUseCase,
            analytics_api.canonical_policy_resource_readiness,
            analytics_api.canonical_policy_resource_readiness_export,
            "$.specs[0].resources.covered_only_by_cancelled_task",
            1,
        ),
    ),
)
async def test_readiness_rest_and_csv_share_exact_canonical_facts(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    use_case: type,
    rest_handler,
    export_handler,
    fact_path: str,
    fact_value: object,
) -> None:
    readiness = {
        "contract_version": "1",
        "query_fingerprint": "d" * 64,
        "as_of": "2026-08-20T12:00:00.000000Z",
        "specs": [
            {
                "spec_id": "spec-1",
                "edition": 3,
                "validation": {
                    "state": "current",
                    "measures": {"confidence": 83},
                    "lifecycle_ready": True,
                },
                "lifecycle": {"spec_pending_validation": False},
                "policy": {
                    "totals": {
                        "native_pass": 1,
                        "blocking_pending": 0,
                        "blocking_failed": 0,
                    }
                },
                "resources": {
                    "l1": [{"resource_type": "architecture", "state": "provided"}],
                    "l2": [],
                    "covered_only_by_cancelled_task": 1,
                },
            }
        ],
    }

    async def execute(_self, command, *, actor, uow):
        assert command.board_id == "board-1"
        assert actor.actor_id == "user-1"
        return SimpleNamespace(data=readiness)

    monkeypatch.setattr(use_case, "execute", execute)
    kwargs = {
        "date_from": "2026-08-19",
        "date_to": "2026-08-20",
        "as_of": "2026-08-20T12:00:00Z",
        "user_id": "user-1",
        "uow": object(),
    }
    rest = await rest_handler("board-1", **kwargs)
    response = await export_handler("board-1", **kwargs)
    chunks = [chunk async for chunk in response.body_iterator]
    body = "".join(
        chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
    )
    rows = {
        row["path"]: json.loads(row["json_value"])
        for row in csv.DictReader(io.StringIO(body))
    }

    assert rest is readiness
    assert rows[fact_path] == fact_value
    assert (
        f"board-board-1-{kind}-readiness.csv" in response.headers["content-disposition"]
    )
