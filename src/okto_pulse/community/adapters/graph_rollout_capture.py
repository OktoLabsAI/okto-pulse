"""Prepared-first mutation capture for an active Board rollout.

The capture contains no second graph writer.  It records a privacy-bounded
logical operation envelope before the routed provider is invoked; a shadow
worker later reconciles a fixed source snapshot into a fresh candidate.  An
ambiguous prepared record is therefore safe: the source snapshot, rather than
an attempted operation replay, decides what actually exists.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, Self, TypeVar

from okto_pulse.community.adapters.cypher_statement_policy import (
    statement_is_write,
)

logger = logging.getLogger(__name__)

MUTATION_ENVELOPE_FORMAT = "okto-pulse-board-rollout-mutation/1"
_ResultT = TypeVar("_ResultT")


class BoardRolloutMutationRecorder(Protocol):
    """Durable store seam consumed by the routed transaction wrapper."""

    def prepare_mutation(
        self,
        *,
        board_id: str,
        binding_sha256: str,
        backend: str,
        transaction_id: str,
        family: str,
        payload: Mapping[str, object],
    ) -> object | None:
        """Persist before source mutation, or return ``None`` when no rollout exists."""

    def mark_source_committed(self, token: object) -> None: ...

    def mark_source_abandoned(self, token: object) -> None: ...

    def mark_source_ambiguous(self, token: object, *, error_type: str) -> None: ...


def _canonical_value(value: Any) -> Any:
    """Return a deterministic JSON value used only to hash mutation arguments."""

    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        # ``allow_nan=False`` below deliberately refuses non-portable numbers.
        return value
    if isinstance(value, Enum):
        return {
            "$enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _canonical_value(value.value),
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "$dataclass": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": _canonical_value(dataclasses.asdict(value)),
        }
    if isinstance(value, (datetime, date)):
        return {
            "$temporal": f"{type(value).__module__}.{type(value).__qualname__}",
            "iso": value.isoformat(),
        }
    if isinstance(value, Path):
        return {"$path": str(value)}
    if isinstance(value, bytes):
        return {
            "$bytes_sha256": hashlib.sha256(value).hexdigest(),
            "length": len(value),
        }
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or key in normalized:
                raise TypeError("rollout mutation mapping keys must be unique strings")
            normalized[key] = _canonical_value(item)
        return normalized
    if isinstance(value, (tuple, list)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonical_value(item) for item in value]
        return {
            "$set": sorted(
                items,
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        }
    # Pulse values such as UUID/Timestamp have a stable text form but are not
    # duplicated in the rollout store.  Their type and text participate only
    # in the digest below.
    return {
        "$typed_text": f"{type(value).__module__}.{type(value).__qualname__}",
        "text": str(value),
    }


def _argument_digest(args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {
            "args": _canonical_value(tuple(args)),
            "kwargs": _canonical_value(dict(kwargs)),
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mutation_envelope(
    family: str,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
) -> dict[str, object]:
    """Describe one logical operation without persisting its user payload."""

    if type(family) is not str or not family:
        raise ValueError("rollout mutation family must be non-empty text")
    if any(type(key) is not str or not key for key in kwargs):
        raise TypeError("rollout mutation keyword names must be non-empty strings")
    return {
        "format": MUTATION_ENVELOPE_FORMAT,
        "family": family,
        "arguments_sha256": _argument_digest(args, kwargs),
        "positional_count": len(args),
        "keyword_names": sorted(kwargs),
    }


def statement_mutation_envelope(
    statement: str,
    params: Mapping[str, Any] | None,
) -> dict[str, object]:
    """Describe a mutating generic statement without retaining Cypher or values."""

    if type(statement) is not str or not statement.strip():
        raise ValueError("rollout mutation statement must be non-empty text")
    parameters = dict(params or {})
    envelope = mutation_envelope("execute", (statement,), parameters)
    envelope.update(
        {
            "statement_sha256": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
            "parameter_names": sorted(parameters),
        }
    )
    return envelope


def invoke_captured_auto_commit(
    operation: Callable[[], _ResultT],
    *,
    recorder: BoardRolloutMutationRecorder,
    board_id: str,
    backend: str,
    binding_sha256: str,
    family: str,
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
) -> _ResultT:
    """Capture one synchronous source mutation without making it retryable.

    Both routed ``SemanticGraphStore`` implementations commit within the
    delegated call.  Consequently a terminal journal failure after ``operation``
    returns cannot be raised to the caller: doing so could cause a duplicate
    retry.  The durable prepared record remains available for snapshot
    reconciliation and the failure is emitted as structured evidence.
    """

    if backend not in {"ladybug", "grafx"}:
        raise ValueError("captured auto-commit backend is invalid")
    token = recorder.prepare_mutation(
        board_id=board_id,
        binding_sha256=binding_sha256,
        backend=backend,
        transaction_id=secrets.token_hex(16),
        family=family,
        payload=mutation_envelope(family, args, kwargs),
    )
    try:
        result = operation()
    except BaseException as failure:
        if token is not None:
            try:
                recorder.mark_source_ambiguous(
                    token,
                    error_type=type(failure).__name__,
                )
            except BaseException as capture_failure:  # noqa: BLE001
                failure.add_note(
                    "marking the rollout mutation ambiguous also failed: "
                    f"{type(capture_failure).__name__}: {capture_failure}"
                )
        raise
    if token is not None:
        try:
            recorder.mark_source_committed(token)
        except BaseException as failure:  # noqa: BLE001 - source auto-committed
            logger.error(
                "kg.graph_rollout.capture_terminal_failed "
                "board=%s backend=%s phase=source_committed error_type=%s",
                board_id,
                backend,
                type(failure).__name__,
                extra={
                    "event": "kg.graph_rollout.capture_terminal_failed",
                    "board_id": board_id,
                    "backend": backend,
                    "phase": "source_committed",
                    "error_type": type(failure).__name__,
                    "source_may_be_applied": True,
                },
            )
    return result


_MUTATING_SCOPE_METHODS = frozenset(
    {
        "create_node",
        "update_node",
        "replace_node_payload",
        "replace_with_source_deleted_tombstone",
        "restore_node_properties",
        "mark_superseded",
        "create_edge",
        "reconcile_spec_lineage_parent",
        "compensate_spec_lineage_parent",
        "clear_spec_lineage_parent",
        "reconcile_projection_active_set",
        "compensate_projection_active_set",
        "delete_edges_by_session",
        "delete_edges_by_session_preserving_spec_lineage",
        "delete_nodes_by_session",
        "increment_attestation",
    }
)


class CapturedGraphTransactionScope:
    """Proxy one routed scope and retain prepared records through termination."""

    __slots__ = (
        "_backend",
        "_binding_sha256",
        "_board_id",
        "_delegate",
        "_pending",
        "_recorder",
        "_terminal",
        "_transaction_id",
        "terminal_capture_errors",
    )

    def __init__(
        self,
        delegate: Any,
        *,
        recorder: BoardRolloutMutationRecorder,
        board_id: str,
        backend: str,
        binding_sha256: str,
        transaction_id: str | None = None,
    ) -> None:
        if backend not in {"ladybug", "grafx"}:
            raise ValueError("captured transaction backend is invalid")
        self._delegate = delegate
        self._recorder = recorder
        self._board_id = board_id
        self._backend = backend
        self._binding_sha256 = binding_sha256
        self._transaction_id = transaction_id or secrets.token_hex(16)
        self._pending: list[object] = []
        self._terminal = False
        # Capture publication after an auto-commit or durable Grafx commit may
        # fail.  Such a failure is evidence, not permission to make the caller
        # retry an already-applied graph mutation.
        self.terminal_capture_errors: list[BaseException] = []

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._delegate, name)
        if name not in _MUTATING_SCOPE_METHODS or not callable(attribute):
            return attribute

        def captured(*args: Any, **kwargs: Any) -> Any:
            return self._invoke(name, attribute, args, kwargs)

        return captured

    def _prepare(self, family: str, payload: Mapping[str, object]) -> object | None:
        return self._recorder.prepare_mutation(
            board_id=self._board_id,
            binding_sha256=self._binding_sha256,
            backend=self._backend,
            transaction_id=self._transaction_id,
            family=family,
            payload=payload,
        )

    def _capture_failure(self, token: object | None, failure: BaseException) -> None:
        if token is None:
            return
        try:
            self._recorder.mark_source_ambiguous(
                token,
                error_type=type(failure).__name__,
            )
        except BaseException as capture_failure:  # noqa: BLE001
            failure.add_note(
                "marking the rollout mutation ambiguous also failed: "
                f"{type(capture_failure).__name__}: {capture_failure}"
            )

    def _record_after_ladybug_apply(self, token: object | None) -> None:
        if token is None:
            return
        try:
            self._recorder.mark_source_committed(token)
        except BaseException as failure:  # noqa: BLE001 - source auto-committed
            self.terminal_capture_errors.append(failure)
            logger.error(
                "kg.graph_rollout.capture_terminal_failed "
                "board=%s backend=ladybug phase=source_committed error_type=%s",
                self._board_id,
                type(failure).__name__,
                extra={
                    "event": "kg.graph_rollout.capture_terminal_failed",
                    "board_id": self._board_id,
                    "backend": "ladybug",
                    "phase": "source_committed",
                    "error_type": type(failure).__name__,
                    "source_may_be_applied": True,
                },
            )

    def _invoke(
        self,
        family: str,
        operation: Callable[..., Any],
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
        *,
        payload: Mapping[str, object] | None = None,
    ) -> Any:
        if self._terminal:
            # Preserve the delegate's own typed terminal error and do not create
            # an outbox record for an operation that cannot start.
            return operation(*args, **dict(kwargs))
        token = self._prepare(
            family,
            payload if payload is not None else mutation_envelope(family, args, kwargs),
        )
        if token is not None and self._backend == "grafx":
            # Append before the call: apply-then-raise remains covered and a
            # later rollback/commit can terminalize the prepared record.
            self._pending.append(token)
        try:
            result = operation(*args, **dict(kwargs))
        except BaseException as failure:
            self._capture_failure(token, failure)
            raise
        if self._backend == "ladybug":
            self._record_after_ladybug_apply(token)
        return result

    def execute(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not statement_is_write(statement):
            return self._delegate.execute(statement, params)
        return self._invoke(
            "execute",
            self._delegate.execute,
            (statement, params),
            {},
            payload=statement_mutation_envelope(statement, params),
        )

    def _terminalize_pending(self, method_name: str) -> None:
        method = getattr(self._recorder, method_name)
        pending, self._pending = self._pending, []
        for token in pending:
            try:
                method(token)
            except BaseException as failure:  # noqa: BLE001 - engine is terminal
                self.terminal_capture_errors.append(failure)
                logger.error(
                    "kg.graph_rollout.capture_terminal_failed "
                    "board=%s backend=%s phase=%s error_type=%s",
                    self._board_id,
                    self._backend,
                    method_name,
                    type(failure).__name__,
                    extra={
                        "event": "kg.graph_rollout.capture_terminal_failed",
                        "board_id": self._board_id,
                        "backend": self._backend,
                        "phase": method_name,
                        "error_type": type(failure).__name__,
                    },
                )

    async def commit(self) -> None:
        if self._terminal:
            return
        try:
            await self._delegate.commit()
        except BaseException as failure:
            for token in self._pending:
                self._capture_failure(token, failure)
            raise
        self._terminal = True
        if self._backend == "grafx":
            self._terminalize_pending("mark_source_committed")

    async def rollback(self) -> None:
        if self._terminal:
            return
        try:
            await self._delegate.rollback()
        except BaseException as failure:
            for token in self._pending:
                self._capture_failure(token, failure)
            raise
        self._terminal = True
        if self._backend == "grafx":
            self._terminalize_pending("mark_source_abandoned")

    async def __aenter__(self) -> Self:
        await self._delegate.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if exc and exc[0] is not None:
            await self.rollback()
        else:
            await self.commit()


__all__ = [
    "MUTATION_ENVELOPE_FORMAT",
    "BoardRolloutMutationRecorder",
    "CapturedGraphTransactionScope",
    "invoke_captured_auto_commit",
    "mutation_envelope",
    "statement_mutation_envelope",
]
