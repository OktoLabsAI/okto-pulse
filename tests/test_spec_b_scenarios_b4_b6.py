"""Native executable evidence for Spec B test cards B4 and B5.

Each test intentionally keeps the Pulse scenario id in its stable name so the
test card can point at one replayable integration oracle.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, text

from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    KnowledgeAssignmentRecord,
    KnowledgeMutationAttemptRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
    Spec,
)
from okto_pulse.core.application.use_cases.mcp_spec_crud import (
    McpDeriveSpecCommand,
)
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgePropagationMode,
    KnowledgeSelection,
    KnowledgeTargetType,
)
from okto_pulse.core.models.knowledge_propagation import (
    DeriveSpecKnowledgeRequest,
)
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeParentKey,
    KnowledgeParentType,
    KnowledgeScopeLookup,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgeCreationPreflightCommand,
    KnowledgeMutationCommand,
    KnowledgePropagationService,
    KnowledgePropagationServiceError,
)

from test_knowledge_propagation_adapter import (
    propagation_store,  # noqa: F401
)
from test_knowledge_propagation_parent_adapter import (
    ACTOR_ID as PARENT_ACTOR_ID,
    BOARD_ID as PARENT_BOARD_ID,
    _parent_spec,
    _target as parent_target,
    propagation_runtime,  # noqa: F401
)


async def _count(session, model, column) -> int:
    return int((await session.scalar(select(func.count(column)))) or 0)


@pytest.mark.asyncio
async def test_ts_27a706e3_invalid_or_conflicting_selection_is_atomic(
    propagation_runtime,  # noqa: F811
) -> None:
    """B4: both creation kinds reject the full set before any target/write."""

    store, sessions = propagation_runtime
    service = KnowledgePropagationService(port=store)
    cases = (
        (
            KnowledgeParentKey(
                board_id=PARENT_BOARD_ID,
                parent_type=KnowledgeParentType.REFINEMENT,
                parent_id="refinement-parent-imp4",
            ),
            KnowledgeTargetType.SPEC,
            ("kb-refinement-local", "kb-foreign"),
            "derive-spec-b4",
            Spec,
        ),
        (
            _parent_spec(),
            KnowledgeTargetType.CARD,
            ("root-stable", "kb-foreign"),
            "create-card-b4",
            Card,
        ),
    )
    future_targets: list[tuple[type[Spec] | type[Card], str]] = []

    for parent, target_type, knowledge_ids, key, model in cases:
        command = KnowledgeCreationPreflightCommand(
            parent=parent,
            target_type=target_type,
            selection=KnowledgeSelection.explicit_ids(
                knowledge_ids,
                mode=KnowledgePropagationMode.REFERENCE,
            ),
            actor_id=PARENT_ACTOR_ID,
            idempotency_key=key,
            justification="the complete selection must validate atomically",
            semantic_creation_hash="a" * 64,
        )
        future_targets.append((model, command.target.target_id))
        async with sessions() as session:
            with pytest.raises(KnowledgePropagationServiceError) as caught:
                await service.preflight_creation(session, command)
            await session.rollback()

        assert caught.value.code == "knowledge_selection_invalid"
        assert caught.value.details == {
            "requested": sorted(knowledge_ids),
            "matched": [knowledge_ids[0]],
            "missing": ["kb-foreign"],
            "invalid": [],
            "ambiguous": [],
        }

    # The single transport contract rejects obsolete parameters at admission.
    with pytest.raises(ValidationError, match="extra_forbidden"):
        DeriveSpecKnowledgeRequest.model_validate({"kb_ids": ["kb-refinement-local"]})
    with pytest.raises(TypeError, match="kb_ids"):
        McpDeriveSpecCommand("refinement", "refinement-parent-imp4", kb_ids=[])

    async with sessions() as session:
        for model, target_id in future_targets:
            assert await session.get(model, target_id) is None
        assert (
            await _count(
                session,
                KnowledgePropagationScopeRecord,
                KnowledgePropagationScopeRecord.id,
            )
            == 0
        )
        assert (
            await _count(
                session,
                KnowledgeAssignmentRecord,
                KnowledgeAssignmentRecord.assignment_id,
            )
            == 0
        )
        assert (
            await _count(
                session,
                KnowledgeSnapshotRecord,
                KnowledgeSnapshotRecord.snapshot_id,
            )
            == 0
        )
        assert (
            await _count(
                session,
                KnowledgeTombstoneRecord,
                KnowledgeTombstoneRecord.tombstone_id,
            )
            == 0
        )
        assert (
            await _count(
                session,
                KnowledgeMutationLedgerRecord,
                KnowledgeMutationLedgerRecord.operation_id,
            )
            == 0
        )
        assert (
            await _count(
                session,
                KnowledgeMutationAttemptRecord,
                KnowledgeMutationAttemptRecord.attempt_id,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_ts_e4be6345_idempotency_revision_and_append_only_ledger(
    propagation_runtime,  # noqa: F811
) -> None:
    """B5: replay/CAS and the physical ledger are immutable and recoverable."""

    store, sessions = propagation_runtime
    service = KnowledgePropagationService(port=store)
    target = parent_target()
    command = KnowledgeMutationCommand(
        target=target,
        selection=KnowledgeSelection.explicit_ids(
            ("root-stable",),
            mode=KnowledgePropagationMode.REFERENCE,
        ),
        actor_id=PARENT_ACTOR_ID,
        expected_revision=0,
        idempotency_key="b5-stable-key",
        justification="selected once and replayed exactly",
        parent=_parent_spec(),
    )

    async with sessions() as session:
        original = await service.mutate(session, command)
        await session.commit()
    async with sessions() as session:
        replay = await service.mutate(session, command)
        await session.commit()
    assert replay.replayed is True
    assert replay.operation_id == original.operation_id
    assert replay.revision == original.revision == 1

    divergent = replace(
        command,
        justification="same key but a different semantic payload",
    )
    async with sessions() as session:
        with pytest.raises(KnowledgePropagationServiceError) as conflict:
            await service.mutate(session, divergent)
        await session.rollback()
    assert conflict.value.code == "knowledge_propagation_idempotency_conflict"
    assert conflict.value.ledger_attempt is not None
    await store.append_after_rollback(conflict.value.ledger_attempt)

    stale = replace(
        command,
        idempotency_key="b5-stale-revision",
    )
    async with sessions() as session:
        with pytest.raises(KnowledgePropagationServiceError) as revision:
            await service.mutate(session, stale)
        await session.rollback()
    assert revision.value.code == "knowledge_propagation_revision_conflict"
    assert revision.value.ledger_attempt is not None
    await store.append_after_rollback(revision.value.ledger_attempt)

    async with sessions() as session:
        scope = await store.load_scope(
            session,
            KnowledgeScopeLookup(target=target),
        )
        assert scope.scope_revision == 1
        assert (
            len([item for item in scope.assignments if item.temporal.is_current]) == 1
        )
        assert (
            await _count(
                session,
                KnowledgeMutationLedgerRecord,
                KnowledgeMutationLedgerRecord.operation_id,
            )
            == 1
        )
        # One exact replay plus the two rejected post-rollback attempts.
        assert (
            await _count(
                session,
                KnowledgeMutationAttemptRecord,
                KnowledgeMutationAttemptRecord.attempt_id,
            )
            == 3
        )

    # Attack the records produced by the native mutation/replay above.
    for table in ("knowledge_mutation_ledger", "knowledge_mutation_attempts"):
        for statement in (
            f"UPDATE {table} SET actor_id='rewritten'",
            f"DELETE FROM {table}",
        ):
            async with sessions() as session:
                with pytest.raises(Exception, match="ledger_immutable"):
                    await session.execute(text(statement))
                await session.rollback()
    async with sessions() as session:
        assert (
            await _count(
                session,
                KnowledgeMutationLedgerRecord,
                KnowledgeMutationLedgerRecord.operation_id,
            )
            == 1
        )
        assert (
            await _count(
                session,
                KnowledgeMutationAttemptRecord,
                KnowledgeMutationAttemptRecord.attempt_id,
            )
            == 3
        )
