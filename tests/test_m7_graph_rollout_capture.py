"""Prepared-first capture at the routed Board transaction boundary."""

from __future__ import annotations

from typing import Any, Self

import pytest

from okto_pulse.community.adapters.graph_rollout_capture import (
    MUTATION_ENVELOPE_FORMAT,
    CapturedGraphTransactionScope,
    invoke_captured_auto_commit,
    mutation_envelope,
    statement_mutation_envelope,
)


class _Recorder:
    def __init__(self, events: list[tuple[Any, ...]], *, active: bool = True) -> None:
        self.events = events
        self.active = active
        self.next_token = 0
        self.fail_terminal = False

    def prepare_mutation(self, **values: Any) -> object | None:
        self.events.append(("prepare", values))
        if not self.active:
            return None
        self.next_token += 1
        return f"token-{self.next_token}"

    def mark_source_committed(self, token: object) -> None:
        self.events.append(("committed", token))
        if self.fail_terminal:
            raise OSError("injected terminal capture failure")

    def mark_source_abandoned(self, token: object) -> None:
        self.events.append(("abandoned", token))

    def mark_source_ambiguous(self, token: object, *, error_type: str) -> None:
        self.events.append(("ambiguous", token, error_type))


class _Delegate:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events
        self.fail_operation = False

    def create_node(self, *args: Any, **kwargs: Any) -> str:
        self.events.append(("source_create_node", args, kwargs))
        if self.fail_operation:
            raise RuntimeError("injected source failure")
        return "created"

    def replace_with_source_deleted_tombstone(self, *args: Any, **kwargs: Any) -> str:
        self.events.append(("source_tombstone", args, kwargs))
        return "tombstoned"

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> str:
        self.events.append(("source_execute", statement, params))
        if self.fail_operation:
            raise RuntimeError("injected source failure")
        return "executed"

    async def commit(self) -> None:
        self.events.append(("source_commit",))

    async def rollback(self) -> None:
        self.events.append(("source_rollback",))

    async def __aenter__(self) -> Self:
        self.events.append(("source_enter",))
        return self


def _scope(
    backend: str,
    events: list[tuple[Any, ...]],
    *,
    recorder: _Recorder | None = None,
) -> tuple[CapturedGraphTransactionScope, _Delegate, _Recorder]:
    active_recorder = recorder or _Recorder(events)
    delegate = _Delegate(events)
    return (
        CapturedGraphTransactionScope(
            delegate,
            recorder=active_recorder,
            board_id="board-1",
            backend=backend,
            binding_sha256="a" * 64,
            transaction_id="tx-fixed",
        ),
        delegate,
        active_recorder,
    )


def test_envelope_hashes_user_values_without_persisting_them() -> None:
    secret = "customer@example.test"
    envelope = mutation_envelope(
        "create_node",
        ("Entity", "node-1", {"title": secret}),
        {"source_session_id": "session-secret"},
    )
    statement = statement_mutation_envelope(
        "CREATE (n:Entity {id: $id, title: $title})",
        {"id": "node-1", "title": secret},
    )

    assert envelope["format"] == MUTATION_ENVELOPE_FORMAT
    assert envelope["family"] == "create_node"
    assert len(str(envelope["arguments_sha256"])) == 64
    assert secret not in repr(envelope)
    assert secret not in repr(statement)
    assert statement["parameter_names"] == ["id", "title"]
    assert len(str(statement["statement_sha256"])) == 64


def test_ladybug_capture_is_prepared_first_and_terminal_after_auto_apply() -> None:
    events: list[tuple[Any, ...]] = []
    scope, _delegate, _recorder = _scope("ladybug", events)

    assert scope.create_node("Entity", "node-1", {"title": "one"}) == "created"

    assert [event[0] for event in events] == [
        "prepare",
        "source_create_node",
        "committed",
    ]
    prepared = events[0][1]
    assert prepared["board_id"] == "board-1"
    assert prepared["backend"] == "ladybug"
    assert prepared["binding_sha256"] == "a" * 64
    assert prepared["transaction_id"] == "tx-fixed"
    assert prepared["family"] == "create_node"


def test_auto_commit_capture_is_prepared_first_and_privacy_bounded() -> None:
    events: list[tuple[Any, ...]] = []
    recorder = _Recorder(events)

    result = invoke_captured_auto_commit(
        lambda: events.append(("source_apply",)) or 7,
        recorder=recorder,
        board_id="board-1",
        backend="grafx",
        binding_sha256="a" * 64,
        family="create_node",
        args=("Entity", "node-1", {"secret": "customer@example.test"}),
        kwargs={},
    )

    assert result == 7
    assert [event[0] for event in events] == [
        "prepare",
        "source_apply",
        "committed",
    ]
    assert "customer@example.test" not in repr(events[0][1]["payload"])


def test_auto_commit_source_failure_is_left_ambiguous() -> None:
    events: list[tuple[Any, ...]] = []
    recorder = _Recorder(events)

    def fail() -> None:
        events.append(("source_apply",))
        raise RuntimeError("source may have applied")

    with pytest.raises(RuntimeError, match="source may have applied"):
        invoke_captured_auto_commit(
            fail,
            recorder=recorder,
            board_id="board-1",
            backend="ladybug",
            binding_sha256="a" * 64,
            family="update_node",
            args=("Entity", "node-1", {}),
            kwargs={},
        )

    assert [event[0] for event in events] == [
        "prepare",
        "source_apply",
        "ambiguous",
    ]


def test_auto_commit_terminal_failure_never_makes_source_retryable() -> None:
    events: list[tuple[Any, ...]] = []
    recorder = _Recorder(events)
    recorder.fail_terminal = True

    result = invoke_captured_auto_commit(
        lambda: events.append(("source_apply",)) or "applied",
        recorder=recorder,
        board_id="board-1",
        backend="ladybug",
        binding_sha256="a" * 64,
        family="delete_nodes_by_session",
        args=("session-1",),
        kwargs={},
    )

    assert result == "applied"
    assert [event[0] for event in events] == [
        "prepare",
        "source_apply",
        "committed",
    ]


def test_optional_productive_tombstone_mutation_is_captured() -> None:
    events: list[tuple[Any, ...]] = []
    scope, _delegate, _recorder = _scope("ladybug", events)

    assert (
        scope.replace_with_source_deleted_tombstone(
            "Entity",
            "node-1",
            {"source_deleted": True},
        )
        == "tombstoned"
    )

    assert [event[0] for event in events] == [
        "prepare",
        "source_tombstone",
        "committed",
    ]
    assert events[0][1]["family"] == "replace_with_source_deleted_tombstone"


def test_apply_then_raise_remains_ambiguous_and_preserves_primary_failure() -> None:
    events: list[tuple[Any, ...]] = []
    scope, delegate, _recorder = _scope("ladybug", events)
    delegate.fail_operation = True

    with pytest.raises(RuntimeError, match="injected source failure"):
        scope.create_node("Entity", "node-1", {"title": "one"})

    assert [event[0] for event in events] == [
        "prepare",
        "source_create_node",
        "ambiguous",
    ]
    assert events[-1] == ("ambiguous", "token-1", "RuntimeError")


@pytest.mark.asyncio
async def test_grafx_records_are_committed_only_after_engine_commit() -> None:
    events: list[tuple[Any, ...]] = []
    scope, _delegate, _recorder = _scope("grafx", events)

    assert scope.create_node("Entity", "node-1", {"title": "one"}) == "created"
    assert [event[0] for event in events] == ["prepare", "source_create_node"]

    await scope.commit()

    assert [event[0] for event in events] == [
        "prepare",
        "source_create_node",
        "source_commit",
        "committed",
    ]
    await scope.commit()
    assert [event[0] for event in events].count("source_commit") == 1


@pytest.mark.asyncio
async def test_grafx_rollback_abandons_every_prepared_operation() -> None:
    events: list[tuple[Any, ...]] = []
    scope, _delegate, _recorder = _scope("grafx", events)
    scope.create_node("Entity", "node-1", {"title": "one"})
    scope.create_node("Entity", "node-2", {"title": "two"})

    await scope.rollback()

    assert events[-3:] == [
        ("source_rollback",),
        ("abandoned", "token-1"),
        ("abandoned", "token-2"),
    ]


@pytest.mark.asyncio
async def test_terminal_capture_failure_does_not_turn_durable_commit_retryable() -> (
    None
):
    events: list[tuple[Any, ...]] = []
    recorder = _Recorder(events)
    recorder.fail_terminal = True
    scope, _delegate, _recorder = _scope("grafx", events, recorder=recorder)
    scope.create_node("Entity", "node-1", {"title": "one"})

    await scope.commit()

    assert len(scope.terminal_capture_errors) == 1
    assert isinstance(scope.terminal_capture_errors[0], OSError)
    assert ("source_commit",) in events


def test_read_only_execute_does_not_create_a_false_mutation_record() -> None:
    events: list[tuple[Any, ...]] = []
    scope, _delegate, _recorder = _scope("grafx", events)

    assert scope.execute("MATCH (n:Entity) RETURN n.id") == "executed"
    assert [event[0] for event in events] == ["source_execute"]


@pytest.mark.asyncio
async def test_mutating_execute_is_captured_and_context_exit_commits() -> None:
    events: list[tuple[Any, ...]] = []
    scope, _delegate, _recorder = _scope("grafx", events)

    async with scope as entered:
        assert entered is scope
        assert (
            scope.execute(
                "CREATE (n:Entity {id: $id})",
                {"id": "node-1"},
            )
            == "executed"
        )

    assert [event[0] for event in events] == [
        "source_enter",
        "prepare",
        "source_execute",
        "source_commit",
        "committed",
    ]


def test_inactive_rollout_adds_no_terminal_work_but_keeps_same_source_call() -> None:
    events: list[tuple[Any, ...]] = []
    recorder = _Recorder(events, active=False)
    scope, _delegate, _recorder = _scope("ladybug", events, recorder=recorder)

    assert scope.create_node("Entity", "node-1", {"title": "one"}) == "created"
    assert [event[0] for event in events] == ["prepare", "source_create_node"]
