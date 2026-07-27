from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.params import Depends

import okto_pulse.community.api.kg_routes as kg_routes
import okto_pulse.community.adapters.kg_events as community_kg_events
import okto_pulse.core.application.kg_events_hub as kg_events_hub
from okto_pulse.community.adapters.kg_events import poll_community_kg_events
from okto_pulse.community.api.kg_routes import (
    poll_kg_events,
    require_kg_stream_board_actor,
    stream_kg_events,
)
from okto_pulse.core.ports.kg_events import KGEventsPoll, KGOutboxEvent


class _Reader:
    def __init__(self, result: KGEventsPoll) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def poll(
        self,
        *,
        board_id: str,
        after: datetime,
        after_event_id: str | None = None,
        limit: int,
    ):
        self.calls.append(
            {
                "board_id": board_id,
                "after": after,
                "after_event_id": after_event_id,
                "limit": limit,
            }
        )
        return self.result


def _dependency(function, parameter: str) -> Depends:
    value = inspect.signature(function).parameters[parameter].default
    assert isinstance(value, Depends)
    return value


def test_kg_events_poll_reuses_authenticated_board_scope() -> None:
    actor_dependency = _dependency(poll_kg_events, "_actor")
    assert actor_dependency.dependency is require_kg_stream_board_actor


@pytest.mark.asyncio
async def test_community_poll_delegates_to_registered_provider(monkeypatch) -> None:
    expected = KGEventsPoll(events=[], progress={"pending": 0, "total": 0})
    reader = _Reader(expected)
    monkeypatch.setattr(
        community_kg_events,
        "get_kg_events_reader_port",
        lambda: reader,
    )

    result = await poll_community_kg_events(
        board_id="board-1",
        after=datetime(2026, 7, 27, tzinfo=timezone.utc),
        limit=10,
    )

    assert result is expected
    assert reader.calls == [
        {
            "board_id": "board-1",
            "after": datetime(2026, 7, 27, tzinfo=timezone.utc),
            "after_event_id": None,
            "limit": 10,
        }
    ]


@pytest.mark.asyncio
async def test_kg_events_poll_returns_finite_events_progress_and_cursor(
    monkeypatch,
) -> None:
    since = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    newest = since + timedelta(seconds=2)
    reader = _Reader(
        KGEventsPoll(
            events=[
                KGOutboxEvent(
                    event_id="event-1",
                    session_id="session-1",
                    event_type="kg.session.committed",
                    created_at=since + timedelta(seconds=1),
                    payload={"board_id": "board-1"},
                ),
                KGOutboxEvent(
                    event_id="event-2",
                    session_id=None,
                    event_type="kg.board.cleared",
                    created_at=newest,
                    payload={"reason": "reset"},
                ),
            ],
            progress={"pending": 2, "claimed": 1, "done": 3, "total": 6},
        )
    )
    monkeypatch.setattr(
        kg_routes,
        "poll_community_kg_events",
        reader.poll,
    )

    response = await asyncio.wait_for(
        poll_kg_events(
            board_id="board-1",
            since=since.isoformat(),
            after_event_id="event-0",
            limit=25,
            _actor=object(),
        ),
        timeout=0.2,
    )

    assert reader.calls == [
        {
            "board_id": "board-1",
            "after": since,
            "after_event_id": "event-0",
            "limit": 25,
        }
    ]
    assert response == {
        "events": [
            {
                "event_id": "event-1",
                "session_id": "session-1",
                "event_type": "kg.session.committed",
                "created_at": (since + timedelta(seconds=1)).isoformat(),
                "payload": {"board_id": "board-1"},
            },
            {
                "event_id": "event-2",
                "session_id": None,
                "event_type": "kg.board.cleared",
                "created_at": newest.isoformat(),
                "payload": {"reason": "reset"},
            },
        ],
        "progress": {"pending": 2, "claimed": 1, "done": 3, "total": 6},
        "cursor": newest.isoformat(),
        "cursor_event_id": "event-2",
    }


@pytest.mark.asyncio
async def test_kg_events_poll_without_cursor_establishes_empty_now_baseline(
    monkeypatch,
) -> None:
    before = datetime.now(timezone.utc)
    future_event = before + timedelta(minutes=1)
    reader = _Reader(
        KGEventsPoll(
            events=[
                KGOutboxEvent(
                    event_id="event-after-baseline",
                    session_id="session-1",
                    event_type="kg.session.committed",
                    created_at=future_event,
                    payload={},
                )
            ],
            progress={"pending": 1, "total": 1},
        )
    )
    monkeypatch.setattr(
        kg_routes,
        "poll_community_kg_events",
        reader.poll,
    )

    response = await poll_kg_events(
        board_id="board-1",
        since=None,
        after_event_id=None,
        limit=500,
        _actor=object(),
    )
    after = datetime.now(timezone.utc)
    baseline = reader.calls[0]["after"]

    assert isinstance(baseline, datetime)
    assert before <= baseline <= after
    assert response == {
        "events": [],
        "progress": {"pending": 1, "total": 1},
        "cursor": baseline.isoformat(),
        "cursor_event_id": None,
    }


@pytest.mark.asyncio
async def test_kg_events_poll_rejects_invalid_cursor() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await poll_kg_events(
            board_id="board-1",
            since="not-a-timestamp",
            after_event_id=None,
            limit=500,
            _actor=object(),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "since must be ISO 8601"


@pytest.mark.asyncio
async def test_kg_events_poll_rejects_tie_breaker_without_timestamp() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await poll_kg_events(
            board_id="board-1",
            since=None,
            after_event_id="event-1",
            limit=500,
            _actor=object(),
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "after_event_id requires since"


@pytest.mark.asyncio
async def test_kg_events_stream_pages_replay_until_fixed_subscription_boundary(
    monkeypatch,
) -> None:
    created_at = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    events = [
        KGOutboxEvent(
            event_id=f"event-{index:04d}",
            session_id=f"session-{index:04d}",
            event_type="kg.session.committed",
            created_at=created_at,
            payload={},
        )
        for index in range(501)
    ]

    class _Subscription:
        cursor = created_at
        cursor_event_id = "event-0500"
        initial_progress = None
        queue: asyncio.Queue[str] = asyncio.Queue()

    class _Hub:
        def __init__(self) -> None:
            self.subscription = _Subscription()
            self.replay_calls: list[tuple[datetime, str | None, int]] = []
            self.unsubscribed = False

        def subscribe(self, _board_id: str):
            return self.subscription

        def unsubscribe(self, subscription) -> None:
            assert subscription is self.subscription
            self.unsubscribed = True

        async def replay(
            self,
            *,
            board_id: str,
            after: datetime,
            after_event_id: str | None,
            limit: int,
        ):
            assert board_id == "board-replay"
            self.replay_calls.append((after, after_event_id, limit))
            cursor = (after, after_event_id or "")
            return [
                event
                for event in events
                if (event.created_at, event.event_id) > cursor
            ][:limit]

    hub = _Hub()
    monkeypatch.setattr(kg_events_hub, "get_kg_events_hub", lambda: hub)

    response = await stream_kg_events(
        board_id="board-replay",
        since=(created_at - timedelta(seconds=1)).isoformat(),
        after_event_id=None,
        _actor=object(),
    )
    iterator = response.body_iterator
    try:
        assert await anext(iterator) == "event: hello\ndata: {}\n\n"
        received = []
        for _ in range(501):
            chunk = await asyncio.wait_for(anext(iterator), timeout=1)
            data_line = next(
                line for line in chunk.splitlines() if line.startswith("data: ")
            )
            received.append(json.loads(data_line.removeprefix("data: "))["event_id"])
    finally:
        await iterator.aclose()

    assert received == [f"event-{index:04d}" for index in range(501)]
    assert hub.replay_calls == [
        (created_at - timedelta(seconds=1), None, 500),
        (created_at, "event-0499", 500),
    ]
    assert hub.unsubscribed is True
