"""Four distinct read-only SQLAlchemy terminal-debt evidence adapters."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    CanonicalDebt,
    ConsolidationDeadLetter,
    DomainEventHandlerExecution,
    DomainEventRow,
    GlobalUpdateOutbox,
)
from okto_pulse.community.adapters.terminal_debt_source import (
    sqlalchemy_source_fingerprint,
)
from okto_pulse.core.application.global_outbox_dead_letter import (
    classify_global_outbox_dead_letter,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.domain.terminal_debt import (
    TERMINAL_DEBT_MAX_FAILURE_DETAIL,
    TERMINAL_DEBT_MAX_SELECTION,
    TerminalDebtActionOwner,
    TerminalDebtCopyAction,
    TerminalDebtDomain,
    TerminalDebtIdentity,
    TerminalDebtItem,
    TerminalDebtManifest,
)
from okto_pulse.core.ports.delivery_ledger import is_governed_delivery_attempt
from okto_pulse.core.ports.global_outbox import (
    GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL,
    GLOBAL_OUTBOX_MAX_RETRIES,
)
from okto_pulse.core.ports.kg_operational import classify_kg_recovery_failure


POLICY_CONSTRAINT_PROJECTION_HANDLER = "PolicyConstraintProjectionHandler"
CANONICAL_TERMINAL_STATES = ("blocked", "deferred", "failed")
_logger = logging.getLogger(__name__)


class TerminalDebtReadError(ValueError):
    """A bounded terminal-debt inventory request is invalid."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# Compatibility name retained for the dedicated policy reader's callers.
TerminalDebtPolicyProjectionReadError = TerminalDebtReadError


def _required_scope(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 500:
        raise TerminalDebtReadError("terminal_debt_scope_invalid")
    return value.strip()


def _validate_page(*, limit: int, offset: int) -> None:
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= TERMINAL_DEBT_MAX_SELECTION
    ):
        raise TerminalDebtReadError("terminal_debt_limit_invalid")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise TerminalDebtReadError("terminal_debt_offset_invalid")


def _source_version(*values: object) -> int:
    for candidate in values:
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            if candidate >= 1:
                return candidate
        if isinstance(candidate, str) and candidate.isdecimal():
            parsed = int(candidate)
            if parsed >= 1:
                return parsed
    return 1


def _payload_source_version(payload: Mapping[str, Any]) -> int:
    return _source_version(
        payload.get("subject_version"),
        payload.get("version"),
        payload.get("artifact_version"),
    )


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _failure_detail(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:TERMINAL_DEBT_MAX_FAILURE_DETAIL]


def _attributes(**values: object) -> tuple[tuple[str, str], ...]:
    return tuple(
        (key, str(value)[:500])
        for key, value in values.items()
        if value is not None and str(value).strip()
    )


class _CommunityTerminalDebtReaderBase:
    domain: TerminalDebtDomain

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    @property
    def source_fingerprint(self) -> str:
        return sqlalchemy_source_fingerprint(self._session_factory)

    def _manifest(
        self,
        *,
        scope_id: str,
        items: tuple[TerminalDebtItem, ...],
        has_more: bool,
    ) -> TerminalDebtManifest:
        manifest = TerminalDebtManifest(
            domain=self.domain,
            scope_id=scope_id,
            source_fingerprint=self.source_fingerprint,
            items=items,
        )
        _logger.info(
            "terminal debt inventory domain=%s scope=%s count=%d truncated=%s token=%s",
            self.domain.value,
            scope_id,
            len(items),
            has_more,
            canonical_sha256(
                {
                    "domain": self.domain.value,
                    "scope_id": scope_id,
                    "manifest_digest": manifest.manifest_digest,
                }
            )[:16],
            extra={
                "event": "terminal_debt.inventory",
                "domain": self.domain.value,
                "scope_id": scope_id,
                "item_count": len(items),
                "truncated": has_more,
            },
        )
        return manifest


class CommunitySqlAlchemyConsolidationTerminalDebtReader(
    _CommunityTerminalDebtReaderBase
):
    domain = TerminalDebtDomain.CONSOLIDATION_DLQ

    async def list_consolidation_terminal_debt(
        self,
        *,
        scope_id: str,
        limit: int = TERMINAL_DEBT_MAX_SELECTION,
        offset: int = 0,
    ) -> TerminalDebtManifest:
        board_id = _required_scope(scope_id)
        _validate_page(limit=limit, offset=offset)
        statement = (
            select(ConsolidationDeadLetter)
            .where(ConsolidationDeadLetter.board_id == board_id)
            .order_by(
                ConsolidationDeadLetter.dead_lettered_at.asc(),
                ConsolidationDeadLetter.id.asc(),
            )
            .offset(offset)
            .limit(limit + 1)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
        consumed = rows[:limit]
        return self._manifest(
            scope_id=board_id,
            items=tuple(self._to_item(row) for row in consumed),
            has_more=len(rows) > limit,
        )

    @staticmethod
    def _to_item(row: ConsolidationDeadLetter) -> TerminalDebtItem:
        errors = list(row.errors) if isinstance(row.errors, list) else []
        last = errors[-1] if errors and isinstance(errors[-1], Mapping) else {}
        error_type = str(last.get("error_type") or "UnknownError")
        message = str(last.get("message") or "")
        classified = classify_kg_recovery_failure(error_type, message)
        replay_safe = (
            last.get("replay_safe")
            if isinstance(last.get("replay_safe"), bool)
            else classified.replay_safe
        )
        recovery_class = str(last.get("recovery_class") or classified.recovery_class)[
            :100
        ]
        reason_code = str(last.get("reason_code") or classified.reason_code)
        dead_lettered_at = _utc_iso(row.dead_lettered_at)
        return TerminalDebtItem(
            identity=TerminalDebtIdentity(
                domain=TerminalDebtDomain.CONSOLIDATION_DLQ,
                value=str(row.id),
            ),
            recovery_class=recovery_class,
            replay_safe=replay_safe,
            action_owner=(
                TerminalDebtActionOwner.AUTOMATION
                if replay_safe
                else TerminalDebtActionOwner.HUMAN
            ),
            source_version=_source_version(row.attempts),
            content_hash=canonical_sha256(
                {
                    "id": row.id,
                    "board_id": row.board_id,
                    "artifact_type": row.artifact_type,
                    "artifact_id": row.artifact_id,
                    "original_queue_id": row.original_queue_id,
                    "attempts": int(row.attempts or 0),
                    "errors": errors,
                    "dead_lettered_at": dead_lettered_at,
                }
            ),
            copy_action=(
                TerminalDebtCopyAction.REQUEUE_CONSOLIDATION_COPY
                if replay_safe
                else None
            ),
            failure_detail=_failure_detail(message or error_type),
            attributes=_attributes(
                artifact_id=row.artifact_id,
                artifact_type=row.artifact_type,
                attempts=int(row.attempts or 0),
                dead_lettered_at=dead_lettered_at,
                original_queue_id=row.original_queue_id,
                reason_code=reason_code,
            ),
        )


class CommunitySqlAlchemyGlobalOutboxTerminalDebtReader(
    _CommunityTerminalDebtReaderBase
):
    domain = TerminalDebtDomain.GLOBAL_OUTBOX_DEAD_LETTER

    async def list_global_outbox_terminal_debt(
        self,
        *,
        scope_id: str,
        limit: int = TERMINAL_DEBT_MAX_SELECTION,
        offset: int = 0,
    ) -> TerminalDebtManifest:
        board_id = _required_scope(scope_id)
        _validate_page(limit=limit, offset=offset)
        statement = (
            select(GlobalUpdateOutbox)
            .where(
                GlobalUpdateOutbox.board_id == board_id,
                GlobalUpdateOutbox.processed_at.is_(None),
                or_(
                    GlobalUpdateOutbox.retry_count
                    == GLOBAL_OUTBOX_DEAD_LETTER_SENTINEL,
                    GlobalUpdateOutbox.retry_count >= GLOBAL_OUTBOX_MAX_RETRIES,
                ),
            )
            .order_by(
                GlobalUpdateOutbox.created_at.asc(),
                GlobalUpdateOutbox.id.asc(),
            )
            .offset(offset)
            .limit(limit + 1)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
        consumed = rows[:limit]
        return self._manifest(
            scope_id=board_id,
            items=tuple(self._to_item(row) for row in consumed),
            has_more=len(rows) > limit,
        )

    @staticmethod
    def _to_item(row: GlobalUpdateOutbox) -> TerminalDebtItem:
        payload = dict(row.payload) if isinstance(row.payload, Mapping) else {}
        governed = is_governed_delivery_attempt(
            event_id=str(row.event_id),
            payload=payload,
        )
        created_at = _utc_iso(row.created_at)
        return TerminalDebtItem(
            identity=TerminalDebtIdentity(
                domain=TerminalDebtDomain.GLOBAL_OUTBOX_DEAD_LETTER,
                value=str(row.id),
            ),
            recovery_class=classify_global_outbox_dead_letter(row.last_error),
            replay_safe=not governed,
            action_owner=(
                TerminalDebtActionOwner.TICK
                if governed
                else TerminalDebtActionOwner.AUTOMATION
            ),
            source_version=_source_version(abs(int(row.retry_count or 0))),
            content_hash=canonical_sha256(
                {
                    "id": row.id,
                    "event_id": row.event_id,
                    "board_id": row.board_id,
                    "session_id": row.session_id,
                    "event_type": row.event_type,
                    "payload": payload,
                    "created_at": created_at,
                    "processed_at": (
                        _utc_iso(row.processed_at) if row.processed_at else None
                    ),
                    "retry_count": int(row.retry_count or 0),
                    "last_error": row.last_error,
                }
            ),
            copy_action=(
                None
                if governed
                else TerminalDebtCopyAction.REPROCESS_GLOBAL_OUTBOX_COPY
            ),
            failure_detail=_failure_detail(row.last_error),
            attributes=_attributes(
                created_at=created_at,
                event_id=row.event_id,
                event_type=row.event_type,
                governed=str(governed).lower(),
                retry_count=int(row.retry_count or 0),
            ),
        )


class CommunitySqlAlchemyCanonicalDebtTerminalReader(_CommunityTerminalDebtReaderBase):
    domain = TerminalDebtDomain.CANONICAL_DEBT

    async def list_canonical_terminal_debt(
        self,
        *,
        scope_id: str,
        limit: int = TERMINAL_DEBT_MAX_SELECTION,
        offset: int = 0,
    ) -> TerminalDebtManifest:
        board_id = _required_scope(scope_id)
        _validate_page(limit=limit, offset=offset)
        statement = (
            select(CanonicalDebt)
            .where(
                CanonicalDebt.board_id == board_id,
                CanonicalDebt.canonical_state.in_(CANONICAL_TERMINAL_STATES),
            )
            .order_by(CanonicalDebt.updated_at.asc(), CanonicalDebt.id.asc())
            .offset(offset)
            .limit(limit + 1)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
        consumed = rows[:limit]
        return self._manifest(
            scope_id=board_id,
            items=tuple(self._to_item(row) for row in consumed),
            has_more=len(rows) > limit,
        )

    @staticmethod
    def _to_item(row: CanonicalDebt) -> TerminalDebtItem:
        updated_at = _utc_iso(row.updated_at)
        return TerminalDebtItem(
            identity=TerminalDebtIdentity(
                domain=TerminalDebtDomain.CANONICAL_DEBT,
                value=str(row.id),
            ),
            recovery_class=f"canonical_{row.canonical_state}",
            replay_safe=False,
            action_owner=TerminalDebtActionOwner.HUMAN,
            source_version=_source_version(
                row.source_version,
                int(row.retry_count or 0) + 1,
            ),
            content_hash=canonical_sha256(
                {
                    "id": row.id,
                    "board_id": row.board_id,
                    "artifact_type": row.artifact_type,
                    "artifact_id": row.artifact_id,
                    "source_ref": row.source_ref,
                    "source_version": row.source_version,
                    "source_content_hash": row.content_hash,
                    "target_status": row.target_status,
                    "canonical_state": row.canonical_state,
                    "graph_layer": row.graph_layer,
                    "maturity_status": row.maturity_status,
                    "failure_reason": row.failure_reason,
                    "last_error": row.last_error,
                    "retry_count": int(row.retry_count or 0),
                    "updated_at": updated_at,
                }
            ),
            failure_detail=_failure_detail(row.last_error or row.failure_reason),
            attributes=_attributes(
                artifact_id=row.artifact_id,
                artifact_type=row.artifact_type,
                canonical_state=row.canonical_state,
                retry_count=int(row.retry_count or 0),
                source_ref=row.source_ref,
                target_status=row.target_status,
                updated_at=updated_at,
            ),
        )


class CommunitySqlAlchemyPolicyProjectionTerminalDebtReader(
    _CommunityTerminalDebtReaderBase
):
    domain = TerminalDebtDomain.POLICY_CONSTRAINT_PROJECTION_DLQ

    async def list_policy_projection_terminal_debt(
        self,
        *,
        scope_id: str,
        limit: int = TERMINAL_DEBT_MAX_SELECTION,
        offset: int = 0,
    ) -> TerminalDebtManifest:
        board_id = _required_scope(scope_id)
        _validate_page(limit=limit, offset=offset)
        statement = (
            select(DomainEventHandlerExecution, DomainEventRow)
            .join(
                DomainEventRow,
                DomainEventRow.id == DomainEventHandlerExecution.event_id,
            )
            .where(
                DomainEventRow.board_id == board_id,
                DomainEventHandlerExecution.handler_name
                == POLICY_CONSTRAINT_PROJECTION_HANDLER,
                DomainEventHandlerExecution.status == "dlq",
            )
            .order_by(
                DomainEventRow.occurred_at.asc(),
                DomainEventRow.id.asc(),
                DomainEventHandlerExecution.id.asc(),
            )
            .offset(offset)
            .limit(limit + 1)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        consumed = rows[:limit]
        return self._manifest(
            scope_id=board_id,
            items=tuple(
                self._to_item(execution=execution, event=event)
                for execution, event in consumed
            ),
            has_more=len(rows) > limit,
        )

    @staticmethod
    def _to_item(
        *,
        execution: DomainEventHandlerExecution,
        event: DomainEventRow,
    ) -> TerminalDebtItem:
        payload = (
            dict(event.payload_json) if isinstance(event.payload_json, Mapping) else {}
        )
        occurred_at = _utc_iso(event.occurred_at)
        content_hash = canonical_sha256(
            {
                "event_id": event.id,
                "event_type": event.event_type,
                "board_id": event.board_id,
                "actor_id": event.actor_id,
                "actor_type": event.actor_type,
                "payload": payload,
                "occurred_at": occurred_at,
            }
        )
        return TerminalDebtItem(
            identity=TerminalDebtIdentity(
                domain=TerminalDebtDomain.POLICY_CONSTRAINT_PROJECTION_DLQ,
                value=str(execution.id),
            ),
            recovery_class="terminal_policy_projection_delivery",
            replay_safe=False,
            action_owner=TerminalDebtActionOwner.HUMAN,
            source_version=_payload_source_version(payload),
            content_hash=content_hash,
            failure_detail=_failure_detail(execution.last_error),
            attributes=_attributes(
                attempts=int(execution.attempts or 0),
                event_id=event.id,
                event_type=event.event_type,
                handler_name=execution.handler_name,
                occurred_at=occurred_at,
                status=execution.status,
            ),
        )


@dataclass(frozen=True, slots=True)
class CommunityTerminalDebtReaders:
    """Composition bundle; each field retains its own domain-specific port."""

    consolidation: CommunitySqlAlchemyConsolidationTerminalDebtReader
    global_outbox: CommunitySqlAlchemyGlobalOutboxTerminalDebtReader
    canonical_debt: CommunitySqlAlchemyCanonicalDebtTerminalReader
    policy_projection: CommunitySqlAlchemyPolicyProjectionTerminalDebtReader


def build_community_terminal_debt_readers(
    session_factory: Callable[[], Any],
) -> CommunityTerminalDebtReaders:
    return CommunityTerminalDebtReaders(
        consolidation=CommunitySqlAlchemyConsolidationTerminalDebtReader(
            session_factory
        ),
        global_outbox=CommunitySqlAlchemyGlobalOutboxTerminalDebtReader(
            session_factory
        ),
        canonical_debt=CommunitySqlAlchemyCanonicalDebtTerminalReader(session_factory),
        policy_projection=CommunitySqlAlchemyPolicyProjectionTerminalDebtReader(
            session_factory
        ),
    )


__all__ = [
    "CANONICAL_TERMINAL_STATES",
    "CommunitySqlAlchemyCanonicalDebtTerminalReader",
    "CommunitySqlAlchemyConsolidationTerminalDebtReader",
    "CommunitySqlAlchemyGlobalOutboxTerminalDebtReader",
    "CommunitySqlAlchemyPolicyProjectionTerminalDebtReader",
    "CommunityTerminalDebtReaders",
    "POLICY_CONSTRAINT_PROJECTION_HANDLER",
    "TerminalDebtPolicyProjectionReadError",
    "TerminalDebtReadError",
    "build_community_terminal_debt_readers",
]
