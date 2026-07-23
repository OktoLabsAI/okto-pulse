"""Restart-safe grandfathering of physical legacy Knowledge Base attachments.

Candidate discovery is read-only and closes before mutation begins.  Every
spec/card target is then classified through the Core service in its own
transaction, so a later failure preserves earlier canonical ledgers and a
subsequent run resumes by comparing the latest durable grandfather details.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import Any

from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_database import (
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
    CommunitySqlAlchemyKnowledgePropagationStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    KnowledgeMutationLedgerRecord,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.core.domain.knowledge_selection import KnowledgeTargetType
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeMutationKind,
    KnowledgeMutationOutcome,
    KnowledgePropagationPortError,
    KnowledgeScopeLookup,
    KnowledgeTargetKey,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgeGrandfatherCommand,
    KnowledgePropagationService,
    KnowledgePropagationServiceError,
)


KNOWLEDGE_PROPAGATION_BACKFILL_ACTOR_ID = "system:knowledge-propagation-v2-backfill"
_IDEMPOTENCY_PREFIX = "kb-grandfather-v2:"


@dataclass(frozen=True, slots=True, order=True)
class KnowledgePropagationBackfillTarget:
    """Stable candidate identity used by the restart scan."""

    board_id: str
    target_type: KnowledgeTargetType
    target_id: str

    def to_key(self) -> KnowledgeTargetKey:
        return KnowledgeTargetKey(
            board_id=self.board_id,
            target_type=self.target_type,
            target_id=self.target_id,
        )


@dataclass(frozen=True, slots=True)
class KnowledgePropagationBackfillResult:
    """Bounded summary; every scanned target has a terminal local outcome."""

    scanned_targets: int
    applied_targets: int
    already_current_targets: int
    active_v2_targets: int
    empty_targets: int
    vanished_targets: int

    def __post_init__(self) -> None:
        values = (
            self.scanned_targets,
            self.applied_targets,
            self.already_current_targets,
            self.active_v2_targets,
            self.empty_targets,
            self.vanished_targets,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("knowledge_propagation_backfill_result_invalid")
        terminal = sum(values[1:])
        if terminal != self.scanned_targets:
            raise ValueError("knowledge_propagation_backfill_result_incoherent")


class KnowledgePropagationBackfillError(RuntimeError):
    """Locate a target whose independent transaction could not converge."""

    def __init__(
        self,
        target: KnowledgePropagationBackfillTarget,
        cause: BaseException,
    ) -> None:
        self.target = target
        self.cause = cause
        super().__init__(
            "knowledge_propagation_backfill_target_failed:"
            f"{target.board_id}:{target.target_type.value}:{target.target_id}"
        )


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _idempotency_key(
    target: KnowledgeTargetKey,
    desired_details: Mapping[str, object],
) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "target": target.to_dict(),
                "details": dict(desired_details),
                "actor_id": KNOWLEDGE_PROPAGATION_BACKFILL_ACTOR_ID,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"{_IDEMPOTENCY_PREFIX}{digest}"


async def _discover_targets(
    session_factory: Callable[[], Any],
) -> tuple[KnowledgePropagationBackfillTarget, ...]:
    """Close the discovery snapshot before opening any target transaction."""

    async with session_factory() as session:
        spec_rows = (
            await session.execute(
                select(Spec.board_id, Spec.id)
                .join(
                    SpecKnowledgeBase,
                    SpecKnowledgeBase.spec_id == Spec.id,
                )
                .distinct()
            )
        ).all()
        card_rows = (
            await session.execute(
                select(Card.board_id, Card.id).where(Card.knowledge_bases.is_not(None))
            )
        ).all()
    targets = {
        KnowledgePropagationBackfillTarget(
            board_id=str(board_id),
            target_type=KnowledgeTargetType.SPEC,
            target_id=str(target_id),
        )
        for board_id, target_id in spec_rows
    }
    targets.update(
        KnowledgePropagationBackfillTarget(
            board_id=str(board_id),
            target_type=KnowledgeTargetType.CARD,
            target_id=str(target_id),
        )
        for board_id, target_id in card_rows
    )
    return tuple(
        sorted(
            targets,
            key=lambda item: (
                item.board_id,
                item.target_type.value,
                item.target_id,
            ),
        )
    )


async def _latest_grandfather_details(
    context: Any,
    target: KnowledgeTargetKey,
) -> Mapping[str, object] | None:
    rows = (
        (
            await context.execute(
                select(
                    KnowledgeMutationLedgerRecord.revision,
                    KnowledgeMutationLedgerRecord.operation_id,
                    KnowledgeMutationLedgerRecord.details,
                )
                .where(
                    KnowledgeMutationLedgerRecord.board_id == target.board_id,
                    KnowledgeMutationLedgerRecord.target_type
                    == target.target_type.value,
                    KnowledgeMutationLedgerRecord.target_id == target.target_id,
                    KnowledgeMutationLedgerRecord.operation_kind
                    == KnowledgeMutationKind.GRANDFATHER.value,
                    KnowledgeMutationLedgerRecord.outcome
                    == KnowledgeMutationOutcome.GRANDFATHERED.value,
                )
                .order_by(
                    KnowledgeMutationLedgerRecord.revision.desc(),
                    KnowledgeMutationLedgerRecord.operation_id.desc(),
                )
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return None
    highest_revision = int(rows[0]["revision"])
    contenders = [row for row in rows if int(row["revision"]) == highest_revision]
    selected = contenders[0]["details"]
    if not isinstance(selected, Mapping):
        raise ValueError("knowledge_propagation_grandfather_details_invalid")
    canonical = _canonical_json(selected)
    for contender in contenders[1:]:
        details = contender["details"]
        if not isinstance(details, Mapping) or _canonical_json(details) != canonical:
            raise ValueError("knowledge_propagation_grandfather_revision_ambiguous")
    return selected


async def backfill_knowledge_propagation_v2(
    *,
    session_factory: Callable[[], Any] | None = None,
    store: CommunitySqlAlchemyKnowledgePropagationStore | None = None,
    service: KnowledgePropagationService | None = None,
) -> KnowledgePropagationBackfillResult:
    """Grandfather every legacy-bearing target, committing one target at a time."""

    factory = session_factory or get_session_factory()
    propagation_store = store or CommunitySqlAlchemyKnowledgePropagationStore(factory)
    propagation_service = service or KnowledgePropagationService(port=propagation_store)
    candidates = await _discover_targets(factory)
    applied = 0
    already_current = 0
    active_v2 = 0
    empty = 0
    vanished = 0

    for candidate in candidates:
        target = candidate.to_key()
        try:
            async with factory() as session:
                try:
                    scope = await propagation_store.load_scope(
                        session,
                        request=KnowledgeScopeLookup(target=target),
                    )
                    if scope.v2_active:
                        active_v2 += 1
                        await session.rollback()
                        continue
                    inventory = await propagation_store.load_grandfather_inventory(
                        session,
                        target,
                    )
                    if not inventory:
                        empty += 1
                        await session.rollback()
                        continue
                    command = KnowledgeGrandfatherCommand(
                        target=target,
                        attachments=inventory,
                        actor_id=KNOWLEDGE_PROPAGATION_BACKFILL_ACTOR_ID,
                        expected_revision=scope.scope_revision,
                        idempotency_key="pending",
                    )
                    desired_details: dict[str, object] = {
                        "contract_version": 2,
                        "legacy_content_preserved": True,
                        "grandfathered_attachments": [
                            item.to_dict() for item in command.attachments
                        ],
                    }
                    command = KnowledgeGrandfatherCommand(
                        target=target,
                        attachments=command.attachments,
                        actor_id=command.actor_id,
                        expected_revision=command.expected_revision,
                        idempotency_key=_idempotency_key(
                            target,
                            desired_details,
                        ),
                    )
                    latest = await _latest_grandfather_details(
                        session,
                        target,
                    )
                    if latest is not None and _canonical_json(latest) == (
                        _canonical_json(desired_details)
                    ):
                        already_current += 1
                        await session.rollback()
                        continue

                    await propagation_service.grandfather(session, command)
                    await session.commit()
                    applied += 1
                except Exception:
                    await session.rollback()
                    raise
        except KnowledgePropagationServiceError as exc:
            if exc.code == "knowledge_propagation_target_not_found":
                vanished += 1
                continue
            if exc.ledger_attempt is not None:
                try:
                    await propagation_store.append_after_rollback(exc.ledger_attempt)
                except Exception as audit_exc:
                    raise KnowledgePropagationBackfillError(
                        candidate,
                        audit_exc,
                    ) from audit_exc
            raise KnowledgePropagationBackfillError(candidate, exc) from exc
        except KnowledgePropagationPortError as exc:
            if exc.code == "knowledge_propagation_target_not_found":
                vanished += 1
                continue
            raise KnowledgePropagationBackfillError(candidate, exc) from exc
        except Exception as exc:
            raise KnowledgePropagationBackfillError(candidate, exc) from exc

    return KnowledgePropagationBackfillResult(
        scanned_targets=len(candidates),
        applied_targets=applied,
        already_current_targets=already_current,
        active_v2_targets=active_v2,
        empty_targets=empty,
        vanished_targets=vanished,
    )


__all__ = [
    "KNOWLEDGE_PROPAGATION_BACKFILL_ACTOR_ID",
    "KnowledgePropagationBackfillError",
    "KnowledgePropagationBackfillResult",
    "KnowledgePropagationBackfillTarget",
    "backfill_knowledge_propagation_v2",
]
