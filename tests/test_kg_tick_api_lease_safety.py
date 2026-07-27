from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import okto_pulse.community.api.kg_tick as kg_tick_api
from okto_pulse.community.adapters.coordination import CommunityLocalLeaseProvider
from okto_pulse.core.application.kg_tick import KGTickAdmissionDeferred


class _FakeDatabase:
    def __init__(self, dispatch_error: BaseException | None = None) -> None:
        self.dispatch_error = dispatch_error
        self.committed = False
        self.rolled_back = False
        self.dispatch_kwargs: dict[str, object] | None = None
        self.services = SimpleNamespace(
            kg=SimpleNamespace(dispatch_manual_tick=self.dispatch_manual_tick)
        )

    async def dispatch_manual_tick(self, **kwargs) -> list[str]:
        self.dispatch_kwargs = dict(kwargs)
        if self.dispatch_error is not None:
            raise self.dispatch_error
        tick_id = str(kwargs["tick_id"])
        return [tick_id]

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _ReleaseFailingLeaseProvider:
    def __init__(self) -> None:
        self.release_calls = 0

    async def try_acquire(self, *_args, **_kwargs):
        return SimpleNamespace(owner_token="lease-token")

    async def release(self, _lease) -> None:
        self.release_calls += 1
        raise RuntimeError("coordination release unavailable")


async def _allow_access(*_args, **_kwargs):
    return SimpleNamespace()


async def _assert_lease_reacquirable(
    provider: CommunityLocalLeaseProvider,
) -> None:
    lease = await provider.try_acquire("kg_daily_tick", ttl_seconds=30)
    assert lease is not None
    await provider.release(lease)


@pytest.mark.asyncio
async def test_health_refusal_releases_tick_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CommunityLocalLeaseProvider()

    async def refuse(*_args, **_kwargs):
        return {
            "error": "graph_recovery_needed",
            "graph_state": "recovery_needed",
        }

    monkeypatch.setattr(kg_tick_api, "get_lease_provider", lambda: provider)
    monkeypatch.setattr(kg_tick_api, "_require_tick_access", _allow_access)
    monkeypatch.setattr(kg_tick_api, "_refuse_tick_if_degraded", refuse)
    monkeypatch.setattr(
        kg_tick_api,
        "scheduler_control_from_request",
        lambda _request: None,
    )

    with pytest.raises(HTTPException) as raised:
        await kg_tick_api.run_tick_now(
            kg_tick_api.TickRunNowRequest(board_id="board-degraded"),
            object(),
            principal=SimpleNamespace(subject="operator"),
            db=_FakeDatabase(),
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["error"] == "graph_recovery_needed"
    await _assert_lease_reacquirable(provider)


@pytest.mark.asyncio
async def test_health_probe_exception_releases_tick_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CommunityLocalLeaseProvider()

    async def fail_probe(*_args, **_kwargs):
        raise RuntimeError("health probe unavailable")

    monkeypatch.setattr(kg_tick_api, "get_lease_provider", lambda: provider)
    monkeypatch.setattr(kg_tick_api, "_require_tick_access", _allow_access)
    monkeypatch.setattr(kg_tick_api, "_refuse_tick_if_degraded", fail_probe)
    monkeypatch.setattr(
        kg_tick_api,
        "scheduler_control_from_request",
        lambda _request: None,
    )

    with pytest.raises(RuntimeError, match="health probe unavailable"):
        await kg_tick_api.run_tick_now(
            kg_tick_api.TickRunNowRequest(board_id="board-probe-error"),
            object(),
            principal=SimpleNamespace(subject="operator"),
            db=_FakeDatabase(),
        )
    await _assert_lease_reacquirable(provider)


@pytest.mark.asyncio
async def test_recovery_defer_is_retryable_and_releases_tick_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CommunityLocalLeaseProvider()

    async def healthy(*_args, **_kwargs):
        return None

    database = _FakeDatabase(
        KGTickAdmissionDeferred(reason_code="global_recovery_active")
    )
    monkeypatch.setattr(kg_tick_api, "get_lease_provider", lambda: provider)
    monkeypatch.setattr(kg_tick_api, "_require_tick_access", _allow_access)
    monkeypatch.setattr(kg_tick_api, "_refuse_tick_if_degraded", healthy)
    monkeypatch.setattr(
        kg_tick_api,
        "scheduler_control_from_request",
        lambda _request: None,
    )

    with pytest.raises(HTTPException) as raised:
        await kg_tick_api.run_tick_now(
            kg_tick_api.TickRunNowRequest(board_id="board-recovery-active"),
            object(),
            principal=SimpleNamespace(subject="operator"),
            db=database,
        )
    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "error": "kg_tick_deferred_for_global_recovery",
        "reason": "global_recovery_active",
        "retryable": True,
        "message": (
            "KG tick deferred while Global Discovery recovery owns the "
            "mutation fence"
        ),
    }
    assert database.rolled_back is True
    await _assert_lease_reacquirable(provider)


@pytest.mark.asyncio
async def test_release_failure_after_commit_preserves_durable_accepted_response(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _ReleaseFailingLeaseProvider()
    database = _FakeDatabase()

    async def healthy(*_args, **_kwargs):
        return None

    monkeypatch.setattr(kg_tick_api, "get_lease_provider", lambda: provider)
    monkeypatch.setattr(kg_tick_api, "_require_tick_access", _allow_access)
    monkeypatch.setattr(kg_tick_api, "_refuse_tick_if_degraded", healthy)
    monkeypatch.setattr(
        kg_tick_api,
        "scheduler_control_from_request",
        lambda _request: None,
    )
    caplog.set_level("ERROR", logger="okto_pulse.api.kg_tick")

    response = await kg_tick_api.run_tick_now(
        kg_tick_api.TickRunNowRequest(
            board_id="board-release-failure",
            force_full_rebuild=True,
        ),
        object(),
        principal=SimpleNamespace(subject="operator"),
        db=database,
    )

    assert response.status == "running"
    assert response.tick_ids == [response.tick_id]
    assert response.correlation_id == response.tick_id
    assert database.committed is True
    assert database.rolled_back is False
    assert database.dispatch_kwargs == {
        "tick_id": response.tick_id,
        "board_id": "board-release-failure",
        "force_full_rebuild": True,
        "scheduled_at": response.scheduled_at,
    }
    assert provider.release_calls == 1
    assert "schedule_committed=True" in caplog.text
