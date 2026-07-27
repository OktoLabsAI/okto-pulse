"""Executable evidence for Spec B test cards B4 through B6.

Each test intentionally keeps the Pulse scenario id in its stable name so the
test card can point at one replayable integration oracle.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

import okto_pulse.community.adapters.relational_schema_steps as schema_steps
from okto_pulse.community.adapters.sqlalchemy_effective_resource import (
    CommunitySqlAlchemyEffectiveResourcePersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card,
    KnowledgeAssignmentRecord,
    KnowledgeMutationAttemptRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    KnowledgeSnapshotRecord,
    KnowledgeTombstoneRecord,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import (
    CommunitySqlAlchemyResourceGateAdapter,
)
from okto_pulse.community.api import refinements as refinements_api
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.mcp_spec_crud import (
    McpDeriveSpecCommand,
    McpDeriveSpecUseCase,
)
from okto_pulse.core.domain.knowledge_selection import (
    KnowledgeOriginClass,
    KnowledgePropagationMode,
    KnowledgeSelection,
    KnowledgeSelectionState,
    KnowledgeTargetType,
)
from okto_pulse.core.models.knowledge_propagation import (
    DeriveSpecKnowledgeRequest,
    KnowledgePropagationEnvelopeV2,
)
from okto_pulse.core.ports.knowledge_propagation import (
    get_knowledge_propagation_port,
    KnowledgeParentKey,
    KnowledgeParentType,
    KnowledgeScopeLookup,
    register_knowledge_propagation_port,
    reset_knowledge_propagation_port_for_tests,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgeCreationPreflightCommand,
    KnowledgeGrandfatherCommand,
    KnowledgeGrandfatherEvidence,
    KnowledgeMutationCommand,
    KnowledgePropagationService,
    KnowledgePropagationServiceError,
    KnowledgeRefreshByKnowledgeIdsCommand,
)
from okto_pulse.core.services.resource_lineage import (
    ResolvedResourceLineageService,
)

from test_knowledge_propagation_adapter import (
    ACTOR_ID as V2_ACTOR_ID,
    BOARD_ID as V2_BOARD_ID,
    NOW as V2_NOW,
    SPEC_ID as V2_SPEC_ID,
    _target as v2_target,
    propagation_store,  # noqa: F401
)
from test_knowledge_propagation_parent_adapter import (
    ACTOR_ID as PARENT_ACTOR_ID,
    BOARD_ID as PARENT_BOARD_ID,
    _parent_spec,
    _target as parent_target,
    propagation_runtime,  # noqa: F401
)
from test_knowledge_propagation_schema_migration import (
    OWNED_TABLE_NAMES,
    _legacy_engine,
    _table_names,
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
            ("kb-local", "kb-foreign"),
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

    # REST and MCP reject the legacy+v2 conflict before either can open/create
    # the deterministic target.
    conflict = DeriveSpecKnowledgeRequest(
        kb_ids=["kb-refinement-local"],
        knowledge_propagation=KnowledgePropagationEnvelopeV2(
            selection_state="omitted",
            idempotency_key="rest-conflict-b4",
        ),
    )
    response = await refinements_api.derive_spec(
        "refinement-parent-imp4",
        request=object(),  # type: ignore[arg-type]
        data=conflict,
        user_id=PARENT_ACTOR_ID,
        uow=object(),  # type: ignore[arg-type]
    )
    assert response.status_code == 422
    assert json.loads(response.body)["code"] == "conflicting_propagation_parameters"

    with pytest.raises(KnowledgePropagationServiceError) as mcp_conflict:
        await McpDeriveSpecUseCase().execute(
            McpDeriveSpecCommand(
                "refinement",
                "refinement-parent-imp4",
                kb_ids=[],
                knowledge_propagation=KnowledgePropagationEnvelopeV2(
                    selection_state="omitted",
                    idempotency_key="mcp-conflict-b4",
                ),
            ),
            actor=ActorContext(
                PARENT_ACTOR_ID,
                "mcp",
                board_id=PARENT_BOARD_ID,
            ),
            uow=SimpleNamespace(),  # type: ignore[arg-type]
        )
    assert mcp_conflict.value.code == "conflicting_propagation_parameters"

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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B5: replay/CAS and the physical ledger are immutable and recoverable."""

    store, sessions = propagation_runtime
    service = KnowledgePropagationService(port=store)
    target = parent_target()
    command = KnowledgeMutationCommand(
        target=target,
        selection=KnowledgeSelection.explicit_ids(
            ("kb-local",),
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

    # Every migration checkpoint is one transaction: no partially-created
    # schema survives, and the retry converges to the same no-op state.
    for stage in ("scope", "assignment", "snapshot", "ledger"):
        engine = await _legacy_engine(tmp_path / f"b5-{stage}.sqlite3", monkeypatch)

        def inject(current: str, *, expected: str = stage) -> None:
            if current == expected:
                raise RuntimeError(f"injected after {current}")

        monkeypatch.setattr(
            schema_steps,
            "_knowledge_propagation_migration_checkpoint",
            inject,
        )
        with pytest.raises(RuntimeError, match=f"injected after {stage}"):
            await schema_steps._migrate_knowledge_propagation_v2_schema()
        assert not (OWNED_TABLE_NAMES & await _table_names(engine))
        monkeypatch.setattr(
            schema_steps,
            "_knowledge_propagation_migration_checkpoint",
            lambda _stage: None,
        )
        assert await schema_steps._migrate_knowledge_propagation_v2_schema() is None
        assert (
            await schema_steps._migrate_knowledge_propagation_v2_schema() == "skipped"
        )
        await engine.dispose()

    audit_engine = await _legacy_engine(
        tmp_path / "b5-append-only.sqlite3",
        monkeypatch,
    )
    monkeypatch.setattr(
        schema_steps,
        "_knowledge_propagation_migration_checkpoint",
        lambda _stage: None,
    )
    await schema_steps._migrate_knowledge_propagation_v2_schema()
    async with audit_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO knowledge_propagation_scopes "
                "(id, board_id, target_type, target_id, scope_revision, "
                "v2_active, selection_state, v2_activated_at) VALUES "
                "('scope-b5', 'board-1', 'card', 'card-1', 1, 1, "
                "'explicit_ids', '2026-07-23 12:00:00')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_ledger "
                "(operation_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "previous_revision, revision, outcome, details, applied_at, "
                "recorded_at) VALUES "
                "('operation-b5', 'scope-b5', 'board-1', 'card', 'card-1', "
                "'key-b5', :digest, 'replace', 'actor-b5', 0, 1, 'applied', "
                "'{}', '2026-07-23 12:00:00', '2026-07-23 12:00:00')"
            ),
            {"digest": "b" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO knowledge_mutation_attempts "
                "(attempt_id, scope_id, board_id, target_type, target_id, "
                "idempotency_key, request_hash, operation_kind, actor_id, "
                "outcome, recorded_at, original_operation_id, details) "
                "VALUES ('attempt-b5', 'scope-b5', 'board-1', 'card', "
                "'card-1', 'key-b5', :digest, 'replace', 'actor-b5', "
                "'replayed', '2026-07-23 12:01:00', 'operation-b5', '{}')"
            ),
            {"digest": "b" * 64},
        )

    forbidden_writes = (
        "UPDATE knowledge_mutation_ledger SET actor_id='other' "
        "WHERE operation_id='operation-b5'",
        "DELETE FROM knowledge_mutation_ledger WHERE operation_id='operation-b5'",
        "UPDATE knowledge_mutation_attempts SET actor_id='other' "
        "WHERE attempt_id='attempt-b5'",
        "DELETE FROM knowledge_mutation_attempts WHERE attempt_id='attempt-b5'",
    )
    for statement in forbidden_writes:
        async with audit_engine.connect() as connection:
            transaction = await connection.begin()
            with pytest.raises(Exception, match="ledger_immutable"):
                await connection.execute(text(statement))
            await transaction.rollback()

    async with audit_engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM knowledge_mutation_ledger "
                    "WHERE operation_id='operation-b5'"
                )
            )
            == 1
        )
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM knowledge_mutation_attempts "
                    "WHERE attempt_id='attempt-b5'"
                )
            )
            == 1
        )
    await audit_engine.dispose()


@pytest.mark.asyncio
async def test_ts_2db9d6c3_grandfathering_to_explicit_empty_is_conservative(
    propagation_store,  # noqa: F811
) -> None:
    """B6: grandfather evidence survives the first explicit-empty write."""

    store, sessions = propagation_store
    physical_payloads = {
        "kb-legacy-all-b6": "plain legacy bytes",
        "kb-selected-b6": "durably selected legacy bytes",
        "kb-unresolved-b6": "divergent cyclic legacy bytes",
    }
    async with sessions() as session:
        session.add_all(
            [
                SpecKnowledgeBase(
                    id="kb-legacy-all-b6",
                    spec_id=V2_SPEC_ID,
                    title="Legacy all",
                    content=physical_payloads["kb-legacy-all-b6"],
                    created_by=V2_ACTOR_ID,
                    created_at=V2_NOW - timedelta(seconds=3),
                ),
                SpecKnowledgeBase(
                    id="kb-selected-b6",
                    spec_id=V2_SPEC_ID,
                    title="Selected legacy",
                    content=physical_payloads["kb-selected-b6"],
                    created_by=V2_ACTOR_ID,
                    created_at=V2_NOW - timedelta(seconds=2),
                ),
                SpecKnowledgeBase(
                    id="kb-unresolved-b6",
                    spec_id=V2_SPEC_ID,
                    title="Unresolved legacy",
                    content=physical_payloads["kb-unresolved-b6"],
                    source_kb_id="kb-unresolved-b6",
                    immediate_parent_kb_id="kb-unresolved-b6",
                    content_hash="a" * 64,
                    created_by=V2_ACTOR_ID,
                    created_at=V2_NOW - timedelta(seconds=1),
                ),
            ]
        )
        await session.commit()

    async with sessions() as session:
        inventory = await store.load_grandfather_inventory(
            session,
            v2_target(),
        )
    by_id = {item.source_knowledge_id: item for item in inventory}
    assert by_id["kb-unresolved-b6"].evidence.origin_cycle is True
    assert by_id["kb-unresolved-b6"].evidence.content_divergent is True
    classified = tuple(
        replace(
            item,
            evidence=KnowledgeGrandfatherEvidence(
                durable_selection_evidence=True,
            ),
        )
        if item.source_knowledge_id == "kb-selected-b6"
        else item
        for item in inventory
    )

    service = KnowledgePropagationService(port=store, now=lambda: V2_NOW)
    async with sessions() as session:
        await service.grandfather(
            session,
            KnowledgeGrandfatherCommand(
                target=v2_target(),
                attachments=classified,
                actor_id="system:migration",
                expected_revision=0,
                idempotency_key="b6-grandfather",
            ),
        )
        await session.commit()
    async with sessions() as session:
        before = await service.read(session, v2_target())
    assert before.v2_active is False
    assert {item.origin_class for item in before.effective_legacy_attachments} == {
        KnowledgeOriginClass.LEGACY_ALL,
        KnowledgeOriginClass.SELECTED_LEGACY,
    }
    assert {item.origin_class for item in before.history_legacy_attachments} == {
        KnowledgeOriginClass.LEGACY_ALL,
        KnowledgeOriginClass.SELECTED_LEGACY,
        KnowledgeOriginClass.LEGACY_UNRESOLVED,
    }

    async with sessions() as session:
        await service.mutate(
            session,
            KnowledgeMutationCommand(
                target=v2_target(),
                selection=KnowledgeSelection.explicit_empty(),
                actor_id=V2_ACTOR_ID,
                expected_revision=1,
                idempotency_key="b6-first-v2-explicit-empty",
                justification="no inherited Knowledge is relevant",
            ),
        )
        await session.commit()

    try:
        previous_port = get_knowledge_propagation_port()
    except RuntimeError:
        previous_port = None
    register_knowledge_propagation_port(store)
    try:
        async with sessions() as session:
            after = await service.read(session, v2_target())
            scope = await store.load_scope(
                session,
                KnowledgeScopeLookup(target=v2_target()),
            )
            rows = (
                (
                    await session.execute(
                        select(SpecKnowledgeBase)
                        .where(SpecKnowledgeBase.spec_id == V2_SPEC_ID)
                        .order_by(SpecKnowledgeBase.id)
                    )
                )
                .scalars()
                .all()
            )
            lineage = await ResolvedResourceLineageService(
                CommunitySqlAlchemyResourceGateAdapter(session)
            ).resolve(
                V2_BOARD_ID,
                "spec",
                V2_SPEC_ID,
                include_coverage=False,
            )
            effective_for_propagation = await CommunitySqlAlchemyEffectiveResourcePersistence().load_knowledge_bases(
                session,
                source_entity_type="spec",
                source_entity_id=V2_SPEC_ID,
            )
    finally:
        reset_knowledge_propagation_port_for_tests()
        if previous_port is not None:
            register_knowledge_propagation_port(previous_port)

    assert after.v2_active is True
    assert after.selection_state is KnowledgeSelectionState.EXPLICIT_EMPTY
    assert after.scope_revision == 2
    assert after.effective_count == 0
    assert after.effective_legacy_attachments == ()
    assert {item.origin_class for item in after.history_legacy_attachments} == {
        KnowledgeOriginClass.LEGACY_ALL,
        KnowledgeOriginClass.SELECTED_LEGACY,
        KnowledgeOriginClass.LEGACY_UNRESOLVED,
    }
    assert scope.v2_active is True
    assert scope.selection_state is KnowledgeSelectionState.EXPLICIT_EMPTY
    assert len(rows) == 3
    assert {row.id: row.content for row in rows} == physical_payloads

    knowledge_lineage = [
        item for item in lineage.attachments if item.resource_type == "knowledge_base"
    ]
    assert len(knowledge_lineage) == 3
    assert all(item.effective is False for item in knowledge_lineage)
    assert {item.origin_class for item in knowledge_lineage} == {
        "legacy_all",
        "selected_legacy",
        "legacy_unresolved",
    }
    assert effective_for_propagation == []

    async with sessions() as session:
        with pytest.raises(KnowledgePropagationServiceError) as refresh:
            await service.refresh_by_knowledge_ids(
                session,
                KnowledgeRefreshByKnowledgeIdsCommand(
                    target=v2_target(),
                    knowledge_ids=("kb-legacy-all-b6",),
                    actor_id=V2_ACTOR_ID,
                    expected_revision=2,
                    idempotency_key="b6-refresh-suppressed-history",
                ),
            )
        await session.rollback()
    assert refresh.value.code == "knowledge_assignment_not_refreshable"
