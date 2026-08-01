"""SK-B/B08 relational impact evidence, unlink lineage, and erasure."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest
import sqlalchemy
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_schema_steps import (
    GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX,
    _guideline_binding_fence_digest_v2,
    _guideline_binding_fence_payload_v2,
    _migrate_guideline_impact_substrate,
    _migrate_guideline_impact_v1_schema,
    _postgresql_owned_table_contract,
    guideline_impact_immutability_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
    _binding_row,
    _guideline_adoption_digest,
    _guideline_retirement_impact_id,
    _guideline_unlink_digest,
    _retirement_row,
    _semantic_binding_row,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.semantic_guideline_kg_events import (
    SEMANTIC_GUIDELINE_PROJECTION_HANDLER,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Base,
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
    GuidelineBoardBindingRow,
    GuidelineImpactAdoptionRow,
    GuidelineImpactItemRow,
    GuidelineImpactReceiptRow,
    GuidelineImpactUnlinkRow,
    GuidelineRetirementImpactRow,
)
from okto_pulse.core.domain.guideline_impact import (
    GuidelineAdoptionMutation,
    GuidelineImpactPreviewCommand,
    GuidelineRetirementImpactMutation,
    GuidelineUnlinkMutation,
    impact_fence_from_receipt,
    plan_guideline_adoption,
    plan_guideline_impact_preview,
    plan_guideline_retirement_impact,
    plan_guideline_unlink,
)
from okto_pulse.core.domain.guideline_lifecycle import (
    guideline_request_digest_v1,
)
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    Guideline,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelineLifecycleStatus,
    GuidelineRetirement,
    GuidelineRevision,
    GuidelineScope,
    guideline_binding_snapshot_digest,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.events.types import (
    SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelineImpactPreviewReplay,
    GuidelinePolicyIdempotencyConflict,
)


NOW = datetime(2026, 7, 29, 18, 0, tzinfo=timezone.utc)
BOARD_ID = "board-b08-impact"
GUIDELINE_ID = "guideline-b08-impact"


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    assert await _migrate_guideline_impact_substrate() == "skipped"
    assert await _migrate_guideline_impact_v1_schema() is None


async def _replace_impact_trigger_manifest(
    manifest: dict[str, tuple[str, str]],
) -> None:
    async with get_engine().begin() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND name LIKE :prefix"
                    ),
                    {"prefix": (f"{GUIDELINE_IMPACT_IMMUTABILITY_TRIGGER_PREFIX}%")},
                )
            )
            .scalars()
            .all()
        )
        for trigger_name in rows:
            await connection.exec_driver_sql(f'DROP TRIGGER "{trigger_name}"')
        for _, trigger_sql in manifest.values():
            await connection.execute(text(trigger_sql))


def _authority() -> tuple[Guideline, GuidelineRevision, GuidelineHead]:
    title = "B08 exact impact"
    content = "Explicit adoption and immutable unlink lineage."
    revision = GuidelineRevision(
        revision_id="revision-b08-impact-1",
        guideline_id=GUIDELINE_ID,
        revision_number=1,
        semantic_version="1.0.0",
        title=title,
        content=content,
        metrics=(),
        created_by="author-b08",
        created_at=NOW,
        parent_revision_id=None,
    )
    return (
        Guideline(
            guideline_id=GUIDELINE_ID,
            owner_id="author-b08",
            scope=GuidelineScope.GLOBAL,
            created_at=NOW,
        ),
        revision,
        GuidelineHead(
            guideline_id=GUIDELINE_ID,
            revision_id=revision.revision_id,
            revision_number=1,
            semantic_version=revision.semantic_version,
            head_revision=1,
            updated_at=NOW,
        ),
    )


@pytest.mark.parametrize(
    "omitted_field",
    (
        "minimum_confidence",
        "metric_threshold_overrides",
        "configuration_digest",
    ),
)
def test_b08_relational_binding_fence_digest_covers_v2_configuration(
    omitted_field: str,
) -> None:
    _, revision, _ = _authority()
    binding = BoardGuidelineBinding(
        binding_id="binding-b08-fence",
        board_id=BOARD_ID,
        guideline_id=GUIDELINE_ID,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.revision_digest,
        priority=9,
        binding_revision=3,
        adopted_by="agent-b08",
        adopted_at=NOW,
        enforcement=GuidelineEnforcement.BLOCKING,
        minimum_confidence=83,
        metric_threshold_overrides={"policy.b08.impact_list": 75},
    )
    arguments = {
        "board_id": binding.board_id,
        "guideline_id": binding.guideline_id,
        "binding_id": binding.binding_id,
        "binding_revision": binding.binding_revision,
        "revision_id": binding.revision_id,
        "semantic_version": binding.semantic_version,
        "revision_digest": binding.revision_digest,
        "priority": binding.priority,
        "enforcement": binding.enforcement.value,
        "minimum_confidence": binding.minimum_confidence,
        "metric_threshold_overrides": binding.metric_threshold_overrides,
        "configuration_digest": binding.configuration_digest,
        "state": binding.state.value,
        "source_kind": binding.source_kind.value,
    }
    expected = guideline_binding_snapshot_digest(
        binding,
        board_id=BOARD_ID,
        guideline_id=GUIDELINE_ID,
    )

    assert _guideline_binding_fence_digest_v2(**arguments) == expected
    incomplete = _guideline_binding_fence_payload_v2(**arguments)
    incomplete.pop(omitted_field)
    assert canonical_sha256(incomplete) != expected


async def _count(session, model) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(model))).scalar_one()
    )


async def _seed_active_binding() -> tuple[
    GuidelineRevision,
    BoardGuidelineBinding,
    GuidelineAdoptionMutation,
]:
    guideline, revision, head = _authority()
    async with get_session_factory()() as session:
        session.add(
            Board(
                id=BOARD_ID,
                name="B08 impact",
                owner_id="author-b08",
            )
        )
        await CommunitySqlAlchemyGuidelinePolicy(session).create_guideline(
            guideline=guideline,
            initial_revision=revision,
            initial_head=head,
            idempotency_key="create:b08",
            request_digest="1" * 64,
        )
        await session.commit()

    preview = plan_guideline_impact_preview(
        GuidelineImpactPreviewCommand(
            impact_receipt_id="impact-b08-1",
            board_id=BOARD_ID,
            guideline_id=GUIDELINE_ID,
            head=head,
            to_revision=revision,
            current_binding=None,
            from_revision=None,
            active_bindings=(),
            active_revisions=(),
            subjects=(),
            waivers=(),
            proposed_priority=5,
            proposed_enforcement=GuidelineEnforcement.ADVISORY,
            proposed_minimum_confidence=0,
            proposed_metric_threshold_overrides={},
            requested_by="agent-b08",
            created_at=NOW + timedelta(minutes=1),
            idempotency_key="preview:b08",
        )
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        stored = await adapter.save_impact_preview(plan=preview)
        replay = await adapter.save_impact_preview(plan=preview)
        assert replay == stored == preview.receipt
        await session.commit()

    adoption = plan_guideline_adoption(
        receipt=preview.receipt,
        current_snapshot=impact_fence_from_receipt(preview.receipt),
        current_binding=None,
        retirement=None,
        actor_id="agent-b08",
        actor_type="agent",
        occurred_at=NOW + timedelta(minutes=2),
        event_id="event-adopt-b08",
        idempotency_key="adopt:b08",
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        binding, stored = await adapter.adopt_revision_cas(mutation=adoption)
        replay_binding, replay_receipt = await adapter.adopt_revision_cas(
            mutation=adoption
        )
        assert (replay_binding, replay_receipt) == (binding, stored)
        await session.commit()
    return revision, binding, adoption


@pytest.mark.asyncio
async def test_b13_explicit_historical_impact_target_preserves_request_digest(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b13-explicit-impact-target.sqlite3")
    guideline, revision_1, head_1 = _authority()
    title = "B08 newer head"
    content = "The explicit preview still targets revision one."
    revision_2 = GuidelineRevision(
        revision_id="revision-b08-impact-2",
        guideline_id=GUIDELINE_ID,
        revision_number=2,
        semantic_version="1.1.0",
        title=title,
        content=content,
        metrics=(),
        created_by="author-b08",
        created_at=NOW + timedelta(seconds=1),
        parent_revision_id=revision_1.revision_id,
    )
    head_2 = GuidelineHead(
        guideline_id=GUIDELINE_ID,
        revision_id=revision_2.revision_id,
        revision_number=2,
        semantic_version=revision_2.semantic_version,
        head_revision=2,
        updated_at=NOW + timedelta(seconds=2),
    )
    async with get_session_factory()() as session:
        session.add(
            Board(
                id=BOARD_ID,
                name="B13 explicit target",
                owner_id="author-b08",
            )
        )
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=guideline,
            initial_revision=revision_1,
            initial_head=head_1,
            idempotency_key="create:b13:explicit-target",
            request_digest="1" * 64,
        )
        await adapter.append_revision_cas(
            revision=revision_2,
            next_head=head_2,
            expected_head_revision=1,
            idempotency_key="append:b13:explicit-target",
            request_digest="2" * 64,
        )
        await session.commit()

    preview = plan_guideline_impact_preview(
        GuidelineImpactPreviewCommand(
            impact_receipt_id="impact-b13-explicit-target",
            board_id=BOARD_ID,
            guideline_id=GUIDELINE_ID,
            head=head_2,
            to_revision=revision_1,
            current_binding=None,
            from_revision=None,
            active_bindings=(),
            active_revisions=(),
            subjects=(),
            waivers=(),
            proposed_priority=1,
            proposed_enforcement=GuidelineEnforcement.ADVISORY,
            proposed_minimum_confidence=0,
            proposed_metric_threshold_overrides={},
            requested_by="agent-b13",
            created_at=NOW + timedelta(minutes=1),
            idempotency_key="preview:b13:explicit-target",
            requested_to_revision_id=revision_1.revision_id,
        )
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert await adapter.save_impact_preview(plan=preview) == preview.receipt
        await session.commit()

    async with get_session_factory()() as session:
        replay = await CommunitySqlAlchemyGuidelinePolicy(
            session
        ).get_impact_receipt_by_idempotency(
            board_id=BOARD_ID,
            idempotency_key=preview.idempotency_key,
        )
        assert replay == GuidelineImpactPreviewReplay(
            receipt=preview.receipt,
            request_digest=preview.request_digest,
        )


async def _plan_followup_adoption(
    *,
    revision: GuidelineRevision,
    binding: BoardGuidelineBinding,
) -> GuidelineAdoptionMutation:
    head = GuidelineHead(
        guideline_id=GUIDELINE_ID,
        revision_id=revision.revision_id,
        revision_number=revision.revision_number,
        semantic_version=revision.semantic_version,
        head_revision=revision.revision_number,
        updated_at=NOW,
    )
    preview = plan_guideline_impact_preview(
        GuidelineImpactPreviewCommand(
            impact_receipt_id="impact-b08-tamper-2",
            board_id=BOARD_ID,
            guideline_id=GUIDELINE_ID,
            head=head,
            to_revision=revision,
            current_binding=binding,
            from_revision=revision,
            active_bindings=(binding,),
            active_revisions=(revision,),
            subjects=(),
            waivers=(),
            proposed_priority=binding.priority + 1,
            proposed_enforcement=binding.enforcement,
            proposed_minimum_confidence=binding.minimum_confidence,
            proposed_metric_threshold_overrides=(
                binding.metric_threshold_overrides
            ),
            requested_by="agent-b08",
            created_at=NOW + timedelta(minutes=6),
            idempotency_key="preview:tamper:b08",
        )
    )
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(session).save_impact_preview(
            plan=preview
        )
        await session.commit()
    return plan_guideline_adoption(
        receipt=preview.receipt,
        current_snapshot=impact_fence_from_receipt(preview.receipt),
        current_binding=binding,
        retirement=None,
        actor_id="agent-b08",
        actor_type="agent",
        occurred_at=NOW + timedelta(minutes=7),
        event_id="event-adopt-tamper-b08",
        idempotency_key="adopt:tamper:b08",
    )


def _adoption_evidence_rows(
    *,
    mutation: GuidelineAdoptionMutation,
    payload: dict[str, object],
    request_digest: str | None = None,
    adoption_digest: str | None = None,
) -> tuple[tuple[object, ...], GuidelineImpactAdoptionRow]:
    resolved_request_digest = request_digest or mutation.request_digest
    adoption_id = str(
        uuid.uuid5(
            uuid.UUID("e8c3085f-0354-5f1e-b1d8-e40ebf87479d"),
            mutation.event.event_id,
        )
    )
    parents = (
        _binding_row(
            mutation.binding,
            idempotency_key=mutation.idempotency_key,
            request_digest=resolved_request_digest,
            impact_receipt_id=mutation.receipt.impact_receipt_id,
            impact_adoption_id=adoption_id,
        ),
        _semantic_binding_row(mutation.binding),
        DomainEventRow(
            id=mutation.event.event_id,
            event_type=mutation.event.event_type,
            board_id=mutation.event.board_id,
            actor_id=mutation.event.actor_id,
            actor_type=mutation.event.actor_type,
            payload_json=payload,
            occurred_at=mutation.event.occurred_at,
        ),
        ActivityLog(
            id=mutation.activity_id,
            board_id=mutation.event.board_id,
            card_id=None,
            action=mutation.activity_action,
            actor_type=mutation.event.actor_type,
            actor_id=mutation.event.actor_id,
            actor_name=mutation.event.actor_id,
            details=payload,
            created_at=mutation.event.occurred_at,
        ),
    )
    ledger = GuidelineImpactAdoptionRow(
        adoption_id=adoption_id,
        board_id=mutation.receipt.board_id,
        guideline_id=mutation.receipt.guideline_id,
        impact_receipt_id=mutation.receipt.impact_receipt_id,
        binding_id=mutation.binding.binding_id,
        binding_revision=mutation.binding.binding_revision,
        expected_binding_revision=(mutation.receipt.expected_binding_revision),
        impact_digest=mutation.receipt.impact_digest,
        binding_digest=mutation.receipt.binding_digest,
        adopted_by=mutation.event.actor_id,
        adopted_at=mutation.event.occurred_at,
        event_id=mutation.event.event_id,
        activity_id=mutation.activity_id,
        idempotency_key=mutation.idempotency_key,
        request_digest=resolved_request_digest,
        adoption_digest=(
            adoption_digest
            or _guideline_adoption_digest(
                adoption_id=adoption_id,
                receipt=mutation.receipt,
                binding=mutation.binding,
                event_id=mutation.event.event_id,
                activity_id=mutation.activity_id,
                actor_id=mutation.event.actor_id,
                adopted_at=mutation.event.occurred_at,
            )
        ),
    )
    return parents, ledger


def _unlink_evidence_rows(
    *,
    mutation: GuidelineUnlinkMutation,
    payload: dict[str, object],
    request_digest: str | None = None,
    unlink_digest: str | None = None,
) -> tuple[tuple[object, ...], GuidelineImpactUnlinkRow]:
    resolved_request_digest = request_digest or mutation.request_digest
    unlink_id = str(
        uuid.uuid5(
            uuid.UUID("4be83e35-ec6c-5d6d-ac7e-b05a8e3545bf"),
            mutation.event.event_id,
        )
    )
    parents = (
        _binding_row(
            mutation.binding,
            idempotency_key=mutation.idempotency_key,
            request_digest=resolved_request_digest,
            impact_unlink_id=unlink_id,
        ),
        _semantic_binding_row(mutation.binding),
        DomainEventRow(
            id=mutation.event.event_id,
            event_type=mutation.event.event_type,
            board_id=mutation.event.board_id,
            actor_id=mutation.event.actor_id,
            actor_type=mutation.event.actor_type,
            payload_json=payload,
            occurred_at=mutation.event.occurred_at,
        ),
        ActivityLog(
            id=mutation.activity_id,
            board_id=mutation.event.board_id,
            card_id=None,
            action=mutation.activity_action,
            actor_type=mutation.event.actor_type,
            actor_id=mutation.event.actor_id,
            actor_name=mutation.event.actor_id,
            details=payload,
            created_at=mutation.event.occurred_at,
        ),
    )
    event = mutation.event
    ledger = GuidelineImpactUnlinkRow(
        unlink_id=unlink_id,
        board_id=event.board_id,
        guideline_id=event.guideline_id,
        binding_id=mutation.binding.binding_id,
        binding_revision=mutation.binding.binding_revision,
        previous_binding_revision=(mutation.previous_binding.binding_revision),
        binding_digest_before=event.binding_digest_before,
        binding_head_digest_before=event.binding_head_digest_before,
        binding_head_digest_after=event.binding_head_digest_after,
        policy_set_digest_before=event.policy_set_digest_before,
        policy_set_digest_after=event.policy_set_digest_after,
        removed_metric_ids=list(event.removed_metric_ids),
        unlinked_by=event.actor_id,
        actor_type=event.actor_type,
        unlinked_at=event.occurred_at,
        event_id=event.event_id,
        activity_id=mutation.activity_id,
        idempotency_key=mutation.idempotency_key,
        request_digest=resolved_request_digest,
        unlink_digest=(
            unlink_digest
            or _guideline_unlink_digest(
                unlink_id=unlink_id,
                mutation=mutation,
            )
        ),
    )
    return parents, ledger


def _retirement_request_digest(
    retirement: GuidelineRetirement,
) -> str:
    return guideline_request_digest_v1(
        operation="retire",
        scope_id=retirement.guideline_id,
        payload={
            "guideline_id": retirement.guideline_id,
            "expected_head_revision": retirement.retired_head_revision,
            "retired_revision_id": retirement.retired_revision_id,
            "retired_revision_number": retirement.retired_revision_number,
            "retired_semantic_version": retirement.retired_semantic_version,
            "retired_revision_digest": retirement.retired_revision_digest,
            "status": retirement.status.value,
            "reason": retirement.reason,
            "superseded_by_guideline_id": (retirement.superseded_by_guideline_id),
            "actor_id": retirement.retired_by,
        },
    )


def _retirement_mutation(
    *,
    revision: GuidelineRevision,
    binding: BoardGuidelineBinding,
    request_digest: str | None = None,
) -> GuidelineRetirementImpactMutation:
    retirement = GuidelineRetirement(
        retirement_id="retirement-tamper-b08",
        guideline_id=GUIDELINE_ID,
        status=GuidelineLifecycleStatus.RETIRED,
        retired_revision_id=revision.revision_id,
        retired_revision_number=revision.revision_number,
        retired_semantic_version=revision.semantic_version,
        retired_revision_digest=revision.revision_digest,
        retired_head_revision=revision.revision_number,
        reason="Retirement evidence test.",
        retired_by="agent-b08",
        retired_at=NOW + timedelta(minutes=9),
    )
    return plan_guideline_retirement_impact(
        retirement=retirement,
        current_binding=binding,
        current_revision=revision,
        active_bindings=(binding,),
        active_revisions=(revision,),
        actor_type="agent",
        request_digest=(request_digest or _retirement_request_digest(retirement)),
    )


def _retirement_evidence_rows(
    *,
    mutation: GuidelineRetirementImpactMutation,
    payload: dict[str, object],
    terminal_request_digest: str | None = None,
    impact_request_digest: str | None = None,
    impact_digest: str | None = None,
) -> tuple[tuple[object, ...], GuidelineRetirementImpactRow]:
    event = mutation.event
    terminal_digest = terminal_request_digest or event.request_digest
    ledger_request_digest = impact_request_digest or event.request_digest
    parents = (
        _retirement_row(
            mutation.retirement,
            idempotency_key="retire:tamper:b08",
            request_digest=terminal_digest,
        ),
        DomainEventRow(
            id=event.event_id,
            event_type=event.event_type,
            board_id=event.board_id,
            actor_id=event.actor_id,
            actor_type=event.actor_type,
            payload_json=payload,
            occurred_at=event.occurred_at,
        ),
        ActivityLog(
            id=mutation.activity_id,
            board_id=event.board_id,
            card_id=None,
            action=mutation.activity_action,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            actor_name=event.actor_id,
            details=payload,
            created_at=event.occurred_at,
        ),
    )
    ledger = GuidelineRetirementImpactRow(
        impact_id=_guideline_retirement_impact_id(event.event_id),
        retirement_id=mutation.retirement.retirement_id,
        board_id=event.board_id,
        guideline_id=event.guideline_id,
        retirement_status=event.retirement_status,
        superseded_by_guideline_id=event.superseded_by_guideline_id,
        binding_id=event.binding_id,
        binding_revision=event.binding_revision,
        revision_id=event.revision_id,
        revision_number=event.revision_number,
        semantic_version=event.semantic_version,
        revision_digest=event.revision_digest,
        binding_digest_before=event.binding_digest_before,
        binding_head_digest_before=event.binding_head_digest_before,
        binding_head_digest_after=event.binding_head_digest_after,
        policy_set_digest_before=event.policy_set_digest_before,
        policy_set_digest_after=event.policy_set_digest_after,
        removed_metric_ids=list(event.removed_metric_ids),
        retired_by=event.actor_id,
        actor_type=event.actor_type,
        retired_at=event.occurred_at,
        event_id=event.event_id,
        activity_id=mutation.activity_id,
        request_digest=ledger_request_digest,
        impact_digest=impact_digest or mutation.impact_digest,
    )
    return parents, ledger


@pytest.mark.asyncio
async def test_b08_preview_adopt_unlink_replay_tamper_and_erasure(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-impact.sqlite3")
    revision, binding, adoption = await _seed_active_binding()
    assert await _migrate_guideline_impact_v1_schema() == "skipped"

    # A syntactically valid ledger row still fails unless its binding, event,
    # and Activity form the exact reciprocal unlink operation.
    async with get_session_factory()() as session:
        session.add(
            GuidelineImpactUnlinkRow(
                unlink_id="invalid-unlink-b08",
                board_id=BOARD_ID,
                guideline_id=GUIDELINE_ID,
                binding_id=binding.binding_id,
                binding_revision=2,
                previous_binding_revision=1,
                binding_digest_before="2" * 64,
                binding_head_digest_before="3" * 64,
                binding_head_digest_after="4" * 64,
                policy_set_digest_before="5" * 64,
                policy_set_digest_after="6" * 64,
                removed_metric_ids=[],
                unlinked_by="agent-b08",
                actor_type="agent",
                unlinked_at=NOW + timedelta(minutes=3),
                event_id="event-adopt-b08",
                activity_id=adoption.activity_id,
                idempotency_key="invalid-unlink:b08",
                request_digest="7" * 64,
                unlink_digest="8" * 64,
            )
        )
        with pytest.raises(
            IntegrityError,
            match="guideline_impact_unlink_evidence_invalid",
        ):
            await session.flush()
        await session.rollback()

    unlink = plan_guideline_unlink(
        current_binding=binding,
        current_revision=revision,
        active_bindings=(binding,),
        active_revisions=(revision,),
        retirement=None,
        actor_id="agent-b08",
        actor_type="agent",
        occurred_at=NOW + timedelta(minutes=3),
        event_id="event-unlink-b08",
        idempotency_key="unlink:b08",
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        unlinked = await adapter.unlink_binding_cas(mutation=unlink)
        replay = await adapter.unlink_binding_cas(mutation=unlink)
        assert replay == unlinked == unlink.binding
        await session.commit()
    assert await _migrate_guideline_impact_v1_schema() == "skipped"

    for model, values, error in (
        (
            DomainEventRow,
            {"payload_json": {"operation": "tampered"}},
            "guideline_impact_audit_evidence_immutable",
        ),
        (
            ActivityLog,
            {"details": {"operation": "tampered"}},
            "guideline_impact_audit_evidence_immutable",
        ),
        (
            GuidelineImpactUnlinkRow,
            {"removed_metric_ids": ["tampered"]},
            "guideline_impact_evidence_immutable",
        ),
    ):
        async with get_session_factory()() as session:
            identity = (
                "event-unlink-b08"
                if model is DomainEventRow
                else unlink.activity_id
                if model is ActivityLog
                else "invalid"
            )
            predicate = (
                model.id == identity
                if model in {DomainEventRow, ActivityLog}
                else model.unlink_id.is_not(None)
            )
            with pytest.raises(IntegrityError, match=error):
                await session.execute(update(model).where(predicate).values(**values))
            await session.rollback()

    async with get_session_factory()() as session:
        executions = tuple(
            (
                await session.execute(
                    select(DomainEventHandlerExecution)
                    .order_by(DomainEventHandlerExecution.event_id)
                )
            ).scalars()
        )
        events = {
            row.id: row
            for row in (
                await session.execute(select(DomainEventRow))
            ).scalars()
        }
        direct_causations = {"event-adopt-b08", "event-unlink-b08"}
        assert direct_causations.issubset(
            {row.event_id for row in executions}
        )
        assert all(
            row.handler_name == SEMANTIC_GUIDELINE_PROJECTION_HANDLER
            and row.status == "pending"
            and row.attempts == 0
            and row.event_id in events
            for row in executions
        )
        semantic_events = tuple(
            events[row.event_id]
            for row in executions
            if events[row.event_id].event_type
            == SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE
        )
        assert semantic_events
        assert {
            event.payload_json["causation_id"]
            for event in semantic_events
        } == direct_causations
        assert all(
            event.payload_json["entity_kind"]
            in {"revision", "metric_definition", "binding_configuration"}
            and event.payload_json["operation"] in {"upsert", "terminate"}
            for event in semantic_events
        )
        for model in (
            GuidelineImpactReceiptRow,
            GuidelineImpactItemRow,
            GuidelineImpactAdoptionRow,
            GuidelineImpactUnlinkRow,
            GuidelineBoardBindingRow,
        ):
            assert await _count(session, model) >= 1
        await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
            session,
            board_id=BOARD_ID,
        )
        for model in (
            GuidelineImpactReceiptRow,
            GuidelineImpactItemRow,
            GuidelineImpactAdoptionRow,
            GuidelineImpactUnlinkRow,
            GuidelineBoardBindingRow,
        ):
            assert await _count(session, model) == 0
        await session.commit()


@pytest.mark.asyncio
async def test_b08_adoption_guard_rejects_payload_tamper_at_insert(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-adoption-tamper.sqlite3")
    revision, binding, _ = await _seed_active_binding()
    mutation = await _plan_followup_adoption(
        revision=revision,
        binding=binding,
    )
    cases = (
        ("schema", {"event_schema_version": "guideline-impact/rogue"}),
        ("event_id", {"event_id": "event-payload-rogue"}),
        ("previous", {"previous_binding_revision": None}),
        ("from", {"from_revision_id": None}),
        ("to", {"to_revision_digest": "b" * 64}),
        ("binding_digest", {"binding_digest_before": "c" * 64}),
        ("head_digest", {"binding_head_digest_after": "d" * 64}),
        ("policy_alias", {"policy_set_digest": "e" * 64}),
        ("metrics", {"added_metric_ids": ["rogue-metric"]}),
        ("actor", {"actor_type": "user"}),
        (
            "time",
            {
                "occurred_at": (
                    mutation.event.occurred_at + timedelta(days=1)
                ).isoformat()
            },
        ),
        ("extra_key", {"unexpected": "must-fail-closed"}),
    )
    for _case_name, overrides in cases:
        payload = mutation.event.payload()
        payload.update(overrides)
        parents, ledger = _adoption_evidence_rows(
            mutation=mutation,
            payload=payload,
        )
        async with get_session_factory()() as session:
            session.add_all(parents)
            await session.flush()
            session.add(ledger)
            with pytest.raises(
                IntegrityError,
                match="guideline_impact_adoption_evidence_invalid",
            ):
                await session.flush()
            await session.rollback()


@pytest.mark.asyncio
async def test_b08_row_audit_recomputes_adoption_digests(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-adoption-digest.sqlite3")
    revision, binding, _ = await _seed_active_binding()
    mutation = await _plan_followup_adoption(
        revision=revision,
        binding=binding,
    )
    await _replace_impact_trigger_manifest(
        guideline_impact_immutability_trigger_manifest(
            verify_full_adoption_evidence=False,
        )
    )
    rogue_digest = "f" * 64
    parents, ledger = _adoption_evidence_rows(
        mutation=mutation,
        payload=mutation.event.payload(),
        request_digest=rogue_digest,
        adoption_digest=rogue_digest,
    )
    async with get_session_factory()() as session:
        session.add_all(parents)
        await session.flush()
        session.add(ledger)
        await session.commit()

    with pytest.raises(
        RuntimeError,
        match="guideline impact adoption digest audit failed: 1",
    ):
        await _migrate_guideline_impact_v1_schema()


@pytest.mark.asyncio
async def test_b08_unlink_guard_rejects_payload_tamper_at_insert(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-unlink-tamper.sqlite3")
    revision, binding, _ = await _seed_active_binding()
    mutation = plan_guideline_unlink(
        current_binding=binding,
        current_revision=revision,
        active_bindings=(binding,),
        active_revisions=(revision,),
        retirement=None,
        actor_id="agent-b08",
        actor_type="agent",
        occurred_at=NOW + timedelta(minutes=8),
        event_id="event-unlink-tamper-b08",
        idempotency_key="unlink:tamper:b08",
    )
    cases = (
        ("schema", {"event_schema_version": "guideline-impact/rogue"}),
        ("event_id", {"event_id": "event-payload-rogue"}),
        (
            "time",
            {
                "occurred_at": (
                    mutation.event.occurred_at + timedelta(days=1)
                ).isoformat()
            },
        ),
        ("extra_key", {"unexpected": "must-fail-closed"}),
    )
    for _case_name, overrides in cases:
        payload = mutation.event.payload()
        payload.update(overrides)
        parents, ledger = _unlink_evidence_rows(
            mutation=mutation,
            payload=payload,
        )
        async with get_session_factory()() as session:
            session.add_all(parents)
            await session.flush()
            session.add(ledger)
            with pytest.raises(
                IntegrityError,
                match="guideline_impact_unlink_evidence_invalid",
            ):
                await session.flush()
            await session.rollback()


@pytest.mark.asyncio
async def test_b08_row_audit_recomputes_unlink_digests(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-unlink-digest.sqlite3")
    revision, binding, _ = await _seed_active_binding()
    mutation = plan_guideline_unlink(
        current_binding=binding,
        current_revision=revision,
        active_bindings=(binding,),
        active_revisions=(revision,),
        retirement=None,
        actor_id="agent-b08",
        actor_type="agent",
        occurred_at=NOW + timedelta(minutes=8),
        event_id="event-unlink-digest-b08",
        idempotency_key="unlink:digest:b08",
    )
    await _replace_impact_trigger_manifest(
        guideline_impact_immutability_trigger_manifest(
            verify_full_unlink_evidence=False,
        )
    )
    rogue_digest = "f" * 64
    parents, ledger = _unlink_evidence_rows(
        mutation=mutation,
        payload=mutation.event.payload(),
        request_digest=rogue_digest,
        unlink_digest=rogue_digest,
    )
    async with get_session_factory()() as session:
        session.add_all(parents)
        await session.flush()
        session.add(ledger)
        await session.commit()

    with pytest.raises(
        RuntimeError,
        match="guideline impact unlink digest audit failed: 1",
    ):
        await _migrate_guideline_impact_v1_schema()


@pytest.mark.asyncio
async def test_b08_retirement_guard_rejects_payload_tamper_at_insert(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-retirement-tamper.sqlite3")
    revision, binding, _ = await _seed_active_binding()
    mutation = _retirement_mutation(
        revision=revision,
        binding=binding,
    )
    cases = (
        (
            "extra_key",
            {"unexpected": "must-fail-closed"},
            None,
            None,
        ),
        ("activity_actor", {}, "rogue-actor", None),
        ("terminal_request", {}, None, "f" * 64),
    )
    for (
        _case_name,
        overrides,
        activity_actor_name,
        terminal_request_digest,
    ) in cases:
        payload = mutation.event.payload()
        payload.update(overrides)
        parents, ledger = _retirement_evidence_rows(
            mutation=mutation,
            payload=payload,
            terminal_request_digest=terminal_request_digest,
        )
        if activity_actor_name is not None:
            parents[2].actor_name = activity_actor_name
        async with get_session_factory()() as session:
            session.add_all(parents)
            await session.flush()
            session.add(ledger)
            with pytest.raises(
                IntegrityError,
                match="guideline_retirement_impact_evidence_invalid",
            ):
                await session.flush()
            await session.rollback()


@pytest.mark.asyncio
async def test_b08_row_audit_recomputes_retirement_digests(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-retirement-digest.sqlite3")
    revision, binding, _ = await _seed_active_binding()
    rogue_digest = "f" * 64
    mutation = _retirement_mutation(
        revision=revision,
        binding=binding,
        request_digest=rogue_digest,
    )
    await _replace_impact_trigger_manifest(
        guideline_impact_immutability_trigger_manifest(
            verify_full_retirement_evidence=False,
        )
    )
    parents, ledger = _retirement_evidence_rows(
        mutation=mutation,
        payload=mutation.event.payload(),
        terminal_request_digest=rogue_digest,
        impact_request_digest=rogue_digest,
        impact_digest="e" * 64,
    )
    async with get_session_factory()() as session:
        session.add_all(parents)
        await session.flush()
        session.add(ledger)
        await session.commit()

    with pytest.raises(
        RuntimeError,
        match="guideline impact retirement digest audit failed: 1",
    ):
        await _migrate_guideline_impact_v1_schema()


@pytest.mark.asyncio
async def test_b08_adapter_retirement_replay_tamper_and_erasure(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-retirement.sqlite3")
    pinned_revision, binding, _ = await _seed_active_binding()
    head_title = "B08 terminal head"
    head_content = "A newer head does not rewrite a board's historical pin."
    retired_head_revision = GuidelineRevision(
        revision_id="revision-b08-impact-2",
        guideline_id=GUIDELINE_ID,
        revision_number=2,
        semantic_version="1.1.0",
        title=head_title,
        content=head_content,
        metrics=(),
        created_by="author-b08",
        created_at=NOW + timedelta(minutes=3),
        parent_revision_id=pinned_revision.revision_id,
    )
    retired_head = GuidelineHead(
        guideline_id=GUIDELINE_ID,
        revision_id=retired_head_revision.revision_id,
        revision_number=2,
        semantic_version=retired_head_revision.semantic_version,
        head_revision=2,
        updated_at=NOW + timedelta(minutes=3, seconds=1),
    )
    async with get_session_factory()() as session:
        await CommunitySqlAlchemyGuidelinePolicy(session).append_revision_cas(
            revision=retired_head_revision,
            next_head=retired_head,
            expected_head_revision=1,
            idempotency_key="revise-before-retirement:b08",
            request_digest="8" * 64,
        )
        await session.commit()

    retired_at = NOW + timedelta(minutes=4)
    retirement = GuidelineRetirement(
        retirement_id="retirement-b08",
        guideline_id=GUIDELINE_ID,
        status=GuidelineLifecycleStatus.RETIRED,
        retired_revision_id=retired_head_revision.revision_id,
        retired_revision_number=retired_head_revision.revision_number,
        retired_semantic_version=retired_head_revision.semantic_version,
        retired_revision_digest=retired_head_revision.revision_digest,
        retired_head_revision=2,
        reason="No longer applicable.",
        retired_by="agent-b08",
        retired_at=retired_at,
    )
    request_digest = _retirement_request_digest(retirement)
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        stored = await adapter.retire_guideline_cas(
            retirement=retirement,
            expected_head_revision=2,
            idempotency_key="retire:b08",
            request_digest=request_digest,
            actor_type="agent",
        )
        replay = await adapter.retire_guideline_cas(
            retirement=retirement,
            expected_head_revision=2,
            idempotency_key="retire:b08",
            request_digest=request_digest,
            actor_type="agent",
        )
        assert replay == stored == retirement
        with pytest.raises(
            GuidelinePolicyIdempotencyConflict,
            match="guideline_retirement_idempotency_digest_mismatch",
        ):
            await adapter.retire_guideline_cas(
                retirement=retirement,
                expected_head_revision=2,
                idempotency_key="retire:b08",
                request_digest="a" * 64,
                actor_type="agent",
            )
        with pytest.raises(
            GuidelinePolicyIdempotencyConflict,
            match="guideline_retirement_idempotency_payload_mismatch",
        ):
            await adapter.retire_guideline_cas(
                retirement=replace(
                    retirement,
                    reason="Divergent idempotency-key reuse.",
                ),
                expected_head_revision=2,
                idempotency_key="retire:b08",
                request_digest=request_digest,
                actor_type="agent",
            )
        await session.commit()
    assert await _migrate_guideline_impact_v1_schema() == "skipped"

    async with get_session_factory()() as session:
        impact = (
            await session.execute(select(GuidelineRetirementImpactRow))
        ).scalar_one()
        event = await session.get(DomainEventRow, impact.event_id)
        activity = await session.get(ActivityLog, impact.activity_id)
        assert impact.retirement_id == retirement.retirement_id
        assert impact.binding_id == binding.binding_id
        assert impact.binding_revision == binding.binding_revision
        assert impact.revision_id == pinned_revision.revision_id
        assert impact.revision_id != retirement.retired_revision_id
        assert impact.retired_by == retirement.retired_by
        assert impact.actor_type == "agent"
        assert event is not None
        assert (
            event.event_type
            == "board.semantic_guideline_retirement_changed.v2"
        )
        assert event.payload_json["operation"] == "retire"
        assert event.payload_json["retirement_id"] == retirement.retirement_id
        assert activity is not None
        assert activity.action == "guideline_retired"
        assert activity.details == event.payload_json
        event_id = event.id
        activity_id = activity.id
        impact_id = impact.impact_id

    unlink = plan_guideline_unlink(
        current_binding=binding,
        current_revision=pinned_revision,
        # Ordinary projections hide the retired policy.  Safe unlink still
        # preserves the exact current binding snapshot as its predecessor.
        active_bindings=(),
        active_revisions=(),
        retirement=retirement,
        actor_id="agent-b08",
        actor_type="agent",
        occurred_at=NOW + timedelta(minutes=5),
        event_id="event-unlink-after-retirement-b08",
        idempotency_key="unlink-after-retirement:b08",
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        assert await adapter.unlink_binding_cas(mutation=unlink) == unlink.binding
        await session.commit()
    async with get_session_factory()() as session:
        replay_after_unlink = await CommunitySqlAlchemyGuidelinePolicy(
            session
        ).retire_guideline_cas(
            retirement=retirement,
            expected_head_revision=2,
            idempotency_key="retire:b08",
            request_digest=request_digest,
            actor_type="agent",
        )
        assert replay_after_unlink == retirement

    for model, values in (
        (DomainEventRow, {"payload_json": {"operation": "tampered"}}),
        (ActivityLog, {"details": {"operation": "tampered"}}),
        (GuidelineRetirementImpactRow, {"removed_metric_ids": ["tampered"]}),
    ):
        async with get_session_factory()() as session:
            predicate = (
                model.id == event_id
                if model is DomainEventRow
                else model.id == activity_id
                if model is ActivityLog
                else model.impact_id == impact_id
            )
            with pytest.raises(IntegrityError):
                await session.execute(update(model).where(predicate).values(**values))
            await session.rollback()

    async with get_session_factory()() as session:
        assert await _count(session, GuidelineRetirementImpactRow) == 1
        await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
            session,
            board_id=BOARD_ID,
        )
        assert await _count(session, GuidelineRetirementImpactRow) == 0
        await session.commit()


@pytest.mark.asyncio
async def test_b08_sqlite_manifest_converges_recognized_predecessors(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b08-predecessor.sqlite3")
    predecessor = guideline_impact_immutability_trigger_manifest(
        allow_board_erasure=False,
        include_unlink=False,
        include_retirement=False,
        verify_default_materialization=False,
        verify_full_adoption_evidence=False,
    )
    await _replace_impact_trigger_manifest(predecessor)

    assert await _migrate_guideline_impact_v1_schema() is None
    weak_evidence_predecessor = guideline_impact_immutability_trigger_manifest(
        allow_board_erasure=False,
        verify_default_materialization=False,
        verify_full_adoption_evidence=False,
        verify_full_unlink_evidence=False,
        verify_full_retirement_evidence=False,
    )
    await _replace_impact_trigger_manifest(weak_evidence_predecessor)
    assert await _migrate_guideline_impact_v1_schema() is None
    assert await _migrate_guideline_impact_v1_schema() == "skipped"


class _PostgresqlMetadataInspector:
    def __init__(
        self,
        table,
        *,
        default_overrides: dict[str, object] | None = None,
        check_overrides: dict[str, str] | None = None,
    ) -> None:
        self._table = table
        self._default_overrides = default_overrides or {}
        self._check_overrides = check_overrides or {}

    def get_columns(self, _table_name: str) -> list[dict[str, object]]:
        columns: list[dict[str, object]] = []
        dialect = postgresql.dialect()
        for column in self._table.columns:
            default = column.server_default
            observed_default = (
                None
                if default is None
                else str(
                    default.arg.compile(
                        dialect=dialect,
                        compile_kwargs={"literal_binds": True},
                    )
                )
            )
            if column.name in self._default_overrides:
                observed_default = self._default_overrides[column.name]
            columns.append(
                {
                    "name": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                    "default": observed_default,
                }
            )
        return columns

    def get_pk_constraint(self, _table_name: str) -> dict[str, object]:
        return {
            "constrained_columns": [
                column.name for column in self._table.primary_key.columns
            ]
        }

    def get_unique_constraints(
        self,
        _table_name: str,
    ) -> list[dict[str, object]]:
        return [
            {
                "name": constraint.name,
                "column_names": [column.name for column in constraint.columns],
            }
            for constraint in self._table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        ]

    def get_check_constraints(
        self,
        _table_name: str,
    ) -> list[dict[str, object]]:
        return [
            {
                "name": constraint.name,
                "sqltext": self._check_overrides.get(
                    constraint.name,
                    str(constraint.sqltext),
                ),
            }
            for constraint in self._table.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        ]

    def get_indexes(self, _table_name: str) -> list[dict[str, object]]:
        return [
            {
                "name": index.name,
                "unique": index.unique,
                "column_names": [
                    getattr(expression, "name", str(expression))
                    for expression in index.expressions
                ],
                "duplicates_constraint": False,
            }
            for index in self._table.indexes
        ]

    def get_foreign_keys(
        self,
        _table_name: str,
    ) -> list[dict[str, object]]:
        foreign_keys: list[dict[str, object]] = []
        for constraint in self._table.foreign_key_constraints:
            elements = tuple(constraint.elements)
            remote_table = elements[0].column.table
            options: dict[str, object] = {}
            for name in ("ondelete", "onupdate", "deferrable", "initially"):
                value = getattr(constraint, name)
                if value is not None:
                    options[name] = value
            foreign_keys.append(
                {
                    "name": constraint.name,
                    "constrained_columns": [
                        element.parent.name for element in elements
                    ],
                    "referred_schema": remote_table.schema,
                    "referred_table": remote_table.name,
                    "referred_columns": [element.column.name for element in elements],
                    "options": options,
                }
            )
        return foreign_keys


class _PostgresqlContractConnection:
    dialect = postgresql.dialect()


def _postgresql_contract(
    monkeypatch: pytest.MonkeyPatch,
    table,
    *,
    default_overrides: dict[str, object] | None = None,
    check_overrides: dict[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    inspector = _PostgresqlMetadataInspector(
        table,
        default_overrides=default_overrides,
        check_overrides=check_overrides,
    )
    monkeypatch.setattr(sqlalchemy, "inspect", lambda _connection: inspector)
    return _postgresql_owned_table_contract(
        _PostgresqlContractConnection(),
        table,
    )


@pytest.mark.parametrize(
    ("column_name", "drifted_default"),
    (
        ("requires_explicit_adoption", "false"),
        ("sealed", "true"),
    ),
)
def test_b08_postgresql_contract_rejects_receipt_default_drift(
    monkeypatch: pytest.MonkeyPatch,
    column_name: str,
    drifted_default: str,
) -> None:
    contract = _postgresql_contract(
        monkeypatch,
        GuidelineImpactReceiptRow.__table__,
        default_overrides={column_name: drifted_default},
    )
    assert contract["observed"] != contract["expected"]


def test_b08_postgresql_contract_normalizes_boolean_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _postgresql_contract(
        monkeypatch,
        GuidelineImpactReceiptRow.__table__,
        default_overrides={
            "requires_explicit_adoption": "'true'::boolean",
            "sealed": "('false'::boolean)",
        },
    )
    assert contract["observed"] == contract["expected"]


def test_b08_postgresql_contract_normalizes_json_catalog_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters.sqlalchemy_models import (
        SemanticGuidelineWaiverEventRow,
        SemanticGuidelineWaiverRow,
    )

    with monkeypatch.context() as waiver_context:
        waiver_contract = _postgresql_contract(
            waiver_context,
            SemanticGuidelineWaiverRow.__table__,
            default_overrides={
                "evidence_refs": "'[]'::json",
                "last_revalidation_currentness_reasons": (
                    "('[]'::json)"
                ),
            },
        )
    assert waiver_contract["observed"] == waiver_contract["expected"]

    with monkeypatch.context() as event_context:
        event_contract = _postgresql_contract(
            event_context,
            SemanticGuidelineWaiverEventRow.__table__,
            default_overrides={
                "evidence_refs": "'[]'::json",
                "currentness_reasons": "('[]'::json)",
            },
        )
    assert event_contract["observed"] == event_contract["expected"]


def test_b08_postgresql_contract_normalizes_catalog_check_rewrites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _postgresql_contract(
        monkeypatch,
        GuidelineImpactItemRow.__table__,
        check_overrides={
            "ck_guideline_impact_item_kind": (
                "((item_kind)::text = ANY "
                "((ARRAY['binding'::character varying, "
                "'target'::character varying, "
                "'artifact'::character varying, "
                "'waiver'::character varying])::text[]))"
            ),
            "ck_guideline_impact_item_digest": (
                "(length((details_digest)::text) = 64)"
            ),
        },
    )
    assert contract["observed"] == contract["expected"]


def test_b08_postgresql_contract_normalizes_real_semantic_catalog_rewrites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters.sqlalchemy_models import (
        SemanticGuidelineMetricResultRow,
        SemanticGuidelineSkipRow,
        SemanticGuidelineWaiverEventRow,
    )

    with monkeypatch.context() as metric_context:
        metric_contract = _postgresql_contract(
            metric_context,
            SemanticGuidelineMetricResultRow.__table__,
            check_overrides={
                "ck_sg_metric_result_enums": (
                    "direction::text = ANY "
                    "(ARRAY['minimum'::character varying, "
                    "'maximum'::character varying]::text[])) AND "
                    "(threshold_source::text = ANY "
                    "(ARRAY['default'::character varying, "
                    "'override'::character varying]::text[])) AND "
                    "(outcome::text = ANY "
                    "(ARRAY['pass'::character varying, "
                    "'fail'::character varying]::text[])"
                ),
            },
        )
    assert metric_contract["observed"] == metric_contract["expected"]

    with monkeypatch.context() as event_context:
        event_contract = _postgresql_contract(
            event_context,
            SemanticGuidelineWaiverEventRow.__table__,
            check_overrides={
                "ck_sg_waiver_event_enums": (
                    "event_type::text = ANY "
                    "(ARRAY['request'::character varying, "
                    "'approve'::character varying, "
                    "'reject'::character varying, "
                    "'revoke'::character varying, "
                    "'expire'::character varying, "
                    "'revalidate'::character varying]::text[])) AND "
                    "(to_status::text = ANY "
                    "(ARRAY['requested'::character varying, "
                    "'approved'::character varying, "
                    "'rejected'::character varying, "
                    "'revoked'::character varying, "
                    "'expired'::character varying]::text[])) AND "
                    "(from_status IS NULL OR "
                    "(from_status::text = ANY "
                    "(ARRAY['requested'::character varying, "
                    "'approved'::character varying, "
                    "'rejected'::character varying, "
                    "'revoked'::character varying, "
                    "'expired'::character varying]::text[]))"
                ),
            },
        )
    assert event_contract["observed"] == event_contract["expected"]

    with monkeypatch.context() as skip_context:
        skip_contract = _postgresql_contract(
            skip_context,
            SemanticGuidelineSkipRow.__table__,
            check_overrides={
                "ck_sg_skip_digests": (
                    "length(subject_content_digest::text) = 64 AND "
                    "length(revision_digest::text) = 64 AND "
                    "length(configuration_digest::text) = 64 AND "
                    "length(scope_digest::text) = 64 AND "
                    "length(event_id::text) = 64 AND "
                    "length(skip_digest::text) = 64 AND "
                    "length(request_digest::text) = 64 AND "
                    "length(TRIM(BOTH FROM reason)) > 0 AND "
                    "length(TRIM(BOTH FROM actor_id)) > 0"
                ),
            },
        )
    assert skip_contract["observed"] == skip_contract["expected"]


def test_b08_postgresql_contract_normalizes_driver_generated_fk_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_pulse.community.adapters.sqlalchemy_models import (
        SemanticGuidelineBindingConfigurationRow,
    )

    table = SemanticGuidelineBindingConfigurationRow.__table__
    inspector = _PostgresqlMetadataInspector(table)
    original_get_foreign_keys = inspector.get_foreign_keys

    def generated_foreign_keys(table_name: str) -> list[dict[str, object]]:
        rows = original_get_foreign_keys(table_name)
        for row in rows:
            if row["name"] is None:
                columns = "_".join(row["constrained_columns"])
                row["name"] = f"{table_name}_{columns}_fkey"
        return rows

    monkeypatch.setattr(inspector, "get_foreign_keys", generated_foreign_keys)
    monkeypatch.setattr(sqlalchemy, "inspect", lambda _connection: inspector)
    contract = _postgresql_owned_table_contract(
        _PostgresqlContractConnection(),
        table,
    )
    assert contract["observed"] == contract["expected"]


def test_b08_postgresql_contract_normalizes_compound_check_rewrites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = _postgresql_contract(
        monkeypatch,
        GuidelineImpactReceiptRow.__table__,
        check_overrides={
            "ck_guideline_impact_non_negative_counts": (
                "((proposed_priority >= 0) "
                "AND (proposed_minimum_confidence >= 0) "
                "AND (proposed_minimum_confidence <= 100) "
                "AND (item_count >= 0))"
            ),
        },
    )
    assert receipt["observed"] == receipt["expected"]

    retirement = _postgresql_contract(
        monkeypatch,
        GuidelineRetirementImpactRow.__table__,
        check_overrides={
            "ck_guideline_retirement_impact_successor": (
                "(((retirement_status)::text = 'retired'::text) "
                "AND (superseded_by_guideline_id IS NULL)) OR "
                "(((retirement_status)::text = 'superseded'::text) "
                "AND (superseded_by_guideline_id IS NOT NULL) "
                "AND ((superseded_by_guideline_id)::text <> "
                "(guideline_id)::text))"
            ),
        },
    )
    assert retirement["observed"] == retirement["expected"]


def test_b08_postgresql_contract_normalizes_function_predicate_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _postgresql_contract(
        monkeypatch,
        GuidelineImpactAdoptionRow.__table__,
        check_overrides={
            "ck_guideline_impact_adoption_digests": (
                "((length((impact_digest)::text) = 64) "
                "AND (length((binding_digest)::text) = 64) "
                "AND (length((request_digest)::text) = 64) "
                "AND (length((adoption_digest)::text) = 64))"
            ),
        },
    )
    assert contract["observed"] == contract["expected"]


def test_b08_postgresql_contract_normalizes_arithmetic_rhs_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = _postgresql_contract(
        monkeypatch,
        GuidelineImpactUnlinkRow.__table__,
        check_overrides={
            "ck_guideline_impact_unlink_sequence": (
                "((previous_binding_revision >= 1) "
                "AND (binding_revision = "
                "(previous_binding_revision + 1)))"
            ),
        },
    )
    assert contract["observed"] == contract["expected"]


@pytest.mark.parametrize(
    "model",
    (
        GuidelineImpactReceiptRow,
        GuidelineImpactItemRow,
        GuidelineImpactAdoptionRow,
        GuidelineImpactUnlinkRow,
        GuidelineRetirementImpactRow,
    ),
)
def test_b08_postgresql_contract_rejects_check_body_drift(
    monkeypatch: pytest.MonkeyPatch,
    model,
) -> None:
    table = model.__table__
    baseline = _postgresql_contract(monkeypatch, table)
    assert baseline["observed"] == baseline["expected"]
    check = next(
        constraint
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    )
    drifted = _postgresql_contract(
        monkeypatch,
        table,
        check_overrides={check.name: "1 = 1"},
    )
    assert drifted["observed"] != drifted["expected"]
