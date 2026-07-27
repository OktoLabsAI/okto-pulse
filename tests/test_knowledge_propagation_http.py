import json
from types import SimpleNamespace

import pytest

from okto_pulse.community.api import knowledge_propagation as propagation_api
from okto_pulse.community.api.knowledge_propagation import (
    execute_knowledge_creation_with_one_retry,
    knowledge_propagation_error_response,
    rollback_and_record_knowledge_error,
)
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgePropagationContractError,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgePropagationServiceError,
)


def test_propagation_contract_error_maps_to_422() -> None:
    response = knowledge_propagation_error_response(
        KnowledgePropagationContractError(
            "knowledge_selection_invalid",
            "one or more Knowledge ids are not selectable",
        )
    )

    assert response.status_code == 422
    assert json.loads(response.body) == {
        "error": "knowledge_selection_invalid",
        "code": "knowledge_selection_invalid",
        "detail": "one or more Knowledge ids are not selectable",
        "details": {},
        "retryable": False,
    }


def test_propagation_revision_conflict_maps_to_409() -> None:
    response = knowledge_propagation_error_response(
        KnowledgePropagationServiceError(
            "knowledge_propagation_revision_conflict",
            "the expected revision is stale",
        )
    )

    assert response.status_code == 409


def test_propagation_missing_target_maps_to_404() -> None:
    response = knowledge_propagation_error_response(
        KnowledgePropagationServiceError(
            "knowledge_propagation_target_not_found",
            "the target does not exist",
        )
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "code",
    [
        "knowledge_propagation_port_not_configured",
        "knowledge_propagation_effective_read_failed",
        "knowledge_propagation_parent_evidence_read_failed",
        "knowledge_propagation_ledger_read_failed",
        "knowledge_propagation_scope_read_failed",
        "knowledge_read_unavailable",
    ],
)
def test_propagation_read_infrastructure_failures_map_to_503(
    code: str,
) -> None:
    response = knowledge_propagation_error_response(
        KnowledgePropagationServiceError(code, "read is unavailable")
    )

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "error": code,
        "code": code,
        "detail": "read is unavailable",
        "details": {},
        "retryable": False,
    }


@pytest.mark.parametrize(
    "code",
    [
        "knowledge_propagation_constraint_conflict",
        "knowledge_propagation_parent_changed",
        "knowledge_propagation_parent_not_eligible",
        "knowledge_propagation_preflight_stale",
        "knowledge_propagation_supersession_conflict",
        "knowledge_propagation_temporal_conflict",
    ],
)
def test_durable_concurrency_conflicts_map_to_409(code: str) -> None:
    response = knowledge_propagation_error_response(
        KnowledgePropagationServiceError(code, "concurrent durable state changed")
    )

    assert response.status_code == 409
    assert json.loads(response.body)["code"] == code


class _FakeUow:
    def __init__(self, events: list[str] | None = None) -> None:
        self.rollbacks = 0
        self.closes = 0
        self.events = events

    async def rollback(self) -> None:
        self.rollbacks += 1
        if self.events is not None:
            self.events.append("rollback")

    async def close(self) -> None:
        self.closes += 1
        if self.events is not None:
            self.events.append("close")


class _FakeUowContext:
    def __init__(self, uow: _FakeUow) -> None:
        self.uow = uow

    async def __aenter__(self) -> _FakeUow:
        return self.uow

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeUowFactory:
    def __init__(self, retry_uow: _FakeUow) -> None:
        self.retry_uow = retry_uow
        self.calls = 0

    def resolve_realm_scope(self) -> RealmScope:
        return RealmScope.local()

    def __call__(self, **_kwargs: object) -> _FakeUowContext:
        self.calls += 1
        return _FakeUowContext(self.retry_uow)


@pytest.mark.asyncio
async def test_creation_race_rolls_back_and_retries_in_fresh_uow() -> None:
    initial = _FakeUow()
    retry = _FakeUow()
    factory = _FakeUowFactory(retry)
    seen: list[_FakeUow] = []

    async def operation(uow: _FakeUow) -> str:
        seen.append(uow)
        if uow is initial:
            raise KnowledgePropagationServiceError(
                "knowledge_creation_race",
                "another writer created the deterministic target first",
            )
        return "replayed"

    result = await execute_knowledge_creation_with_one_retry(
        uow=initial,  # type: ignore[arg-type]
        uow_factory=factory,  # type: ignore[arg-type]
        actor=ActorContext("actor-1", "rest"),
        operation=operation,  # type: ignore[arg-type]
    )

    assert result == "replayed"
    assert initial.rollbacks == 1
    assert initial.closes == 1
    assert factory.calls == 1
    assert seen == [initial, retry]


@pytest.mark.asyncio
async def test_terminal_audit_runs_only_after_rollback_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    uow = _FakeUow(events)

    class _AuditSink:
        async def append_after_rollback(self, attempt: object) -> None:
            assert attempt == "attempt-1"
            events.append("audit")

    monkeypatch.setattr(
        propagation_api,
        "get_knowledge_mutation_audit_sink",
        lambda: _AuditSink(),
    )

    await rollback_and_record_knowledge_error(
        uow,  # type: ignore[arg-type]
        SimpleNamespace(ledger_attempt="attempt-1"),  # type: ignore[arg-type]
    )

    assert events == ["rollback", "close", "audit"]


@pytest.mark.asyncio
async def test_second_creation_race_is_terminal_only_after_fresh_uow_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    initial = _FakeUow(events)
    retry = _FakeUow(events)
    terminal = KnowledgePropagationServiceError(
        "knowledge_creation_race",
        "the bounded retry also lost",
    )
    terminal.ledger_attempt = "attempt-terminal"  # type: ignore[assignment]

    class _RetryContext:
        async def __aenter__(self) -> _FakeUow:
            return retry

        async def __aexit__(self, exc_type, *_args: object) -> None:
            if exc_type is not None:
                await retry.rollback()
            await retry.close()

    class _Factory:
        def resolve_realm_scope(self) -> RealmScope:
            return RealmScope.local()

        def __call__(self, **_kwargs: object) -> _RetryContext:
            return _RetryContext()

    async def operation(uow: _FakeUow) -> str:
        if uow is initial:
            raise KnowledgePropagationServiceError(
                "knowledge_creation_race",
                "the initial deterministic create lost",
            )
        raise terminal

    with pytest.raises(KnowledgePropagationServiceError) as caught:
        await execute_knowledge_creation_with_one_retry(
            uow=initial,  # type: ignore[arg-type]
            uow_factory=_Factory(),  # type: ignore[arg-type]
            actor=ActorContext("actor-1", "rest"),
            operation=operation,  # type: ignore[arg-type]
        )
    assert caught.value is terminal
    assert events == ["rollback", "close", "rollback", "close"]

    class _AuditSink:
        async def append_after_rollback(self, attempt: object) -> None:
            assert attempt == "attempt-terminal"
            events.append("audit")

    monkeypatch.setattr(
        propagation_api,
        "get_knowledge_mutation_audit_sink",
        lambda: _AuditSink(),
    )
    await rollback_and_record_knowledge_error(initial, terminal)

    assert events[-3:] == ["rollback", "close", "audit"]
    assert knowledge_propagation_error_response(terminal).status_code == 409
    assert json.loads(
        knowledge_propagation_error_response(terminal).body
    )["retryable"] is True
