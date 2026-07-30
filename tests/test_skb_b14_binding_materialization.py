"""B14 transactional inline/default binding materialization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

import okto_pulse.core.infra.database as database_module
from okto_pulse.community.adapters.relational_schema_steps import (
    _migrate_guideline_impact_substrate,
    _migrate_guideline_impact_v1_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    get_engine,
    get_session_factory,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
    guideline_revision_content_digest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    BoardErasurePermit,
    DefaultBoardConfiguration,
    DomainEventHandlerExecution,
    DomainEventRow,
)
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    Guideline,
    GuidelineBindingProvenance,
    GuidelineBindingState,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelineRevision,
    GuidelineScope,
)
from okto_pulse.core.events.types import (
    POLICY_BINDING_MATERIALIZED_EVENT_TYPE,
)
from okto_pulse.core.ports.guideline_policy import (
    GuidelineDefaultMaterializationProof,
)


NOW = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
BOARD_ID = "board-b14-materialization"


async def _fresh_database(path: Path) -> None:
    database_module.create_database(f"sqlite+aiosqlite:///{path.as_posix()}")
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    assert await _migrate_guideline_impact_substrate() == "skipped"
    assert await _migrate_guideline_impact_v1_schema() is None


def _revision(
    guideline_id: str,
    revision_id: str,
    *,
    number: int = 1,
    parent_revision_id: str | None = None,
) -> GuidelineRevision:
    title = f"Guideline {guideline_id} v{number}"
    content = f"Policy content {number}."
    return GuidelineRevision(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=number,
        semantic_version=f"{number}.0.0",
        title=title,
        content=content,
        content_digest=guideline_revision_content_digest(
            title=title,
            content=content,
            tags=(),
        ),
        rules=(),
        created_by="agent-b14",
        created_at=NOW + timedelta(minutes=number),
        parent_revision_id=parent_revision_id,
    )


def _head(revision: GuidelineRevision) -> GuidelineHead:
    return GuidelineHead(
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        revision_number=revision.revision_number,
        semantic_version=revision.semantic_version,
        head_revision=revision.revision_number,
        updated_at=revision.created_at + timedelta(seconds=1),
    )


def _binding(
    *,
    binding_id: str,
    revision: GuidelineRevision,
    binding_revision: int,
    adopted_at: datetime,
    source_kind: GuidelineBindingProvenance,
    state: GuidelineBindingState = GuidelineBindingState.ACTIVE,
) -> BoardGuidelineBinding:
    return BoardGuidelineBinding(
        binding_id=binding_id,
        board_id=BOARD_ID,
        guideline_id=revision.guideline_id,
        revision_id=revision.revision_id,
        semantic_version=revision.semantic_version,
        revision_digest=revision.content_digest,
        priority=1,
        binding_revision=binding_revision,
        adopted_by="agent-b14",
        adopted_at=adopted_at,
        default_enforcement=GuidelineEnforcement.ADVISORY,
        state=state,
        source_kind=source_kind,
    )


@pytest.mark.asyncio
async def test_b14_inline_default_materialization_is_atomic_closed_and_erasable(
    tmp_path: Path,
) -> None:
    await _fresh_database(tmp_path / "b14-materialization.sqlite3")
    inline_id = "guideline-b14-inline"
    default_id = "guideline-b14-default"
    inline_v1 = _revision(inline_id, "revision-b14-inline-1")
    default_v1 = _revision(default_id, "revision-b14-default-1")
    template_id = "template-b14"
    template_version = 1
    async with get_session_factory()() as session:
        session.add(
            Board(
                id=BOARD_ID,
                name="B14 materialization",
                owner_id="agent-b14",
                default_config_snapshot={
                    "template_id": template_id,
                    "template_version": template_version,
                },
            )
        )
        session.add(
            DefaultBoardConfiguration(
                id=template_id,
                version=template_version,
                status="active",
                is_active=True,
                scope="global",
                settings_payload={},
                guideline_default_refs=[
                    {
                        "guideline_id": default_id,
                        "revision_id": default_v1.revision_id,
                        "revision_number": 1,
                        "semantic_version": default_v1.semantic_version,
                        "revision_digest": default_v1.content_digest,
                        "priority": 1,
                    }
                ],
                created_by="agent-b14",
            )
        )
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=inline_id,
                owner_id="agent-b14",
                scope=GuidelineScope.INLINE,
                board_id=BOARD_ID,
                created_at=NOW,
            ),
            initial_revision=inline_v1,
            initial_head=_head(inline_v1),
            idempotency_key="create:inline:b14",
            request_digest="1" * 64,
        )
        await adapter.create_guideline(
            guideline=Guideline(
                guideline_id=default_id,
                owner_id="agent-b14",
                scope=GuidelineScope.GLOBAL,
                created_at=NOW,
            ),
            initial_revision=default_v1,
            initial_head=_head(default_v1),
            idempotency_key="create:default:b14",
            request_digest="2" * 64,
        )
        await session.commit()

    inline_binding = _binding(
        binding_id="binding-b14-inline",
        revision=inline_v1,
        binding_revision=1,
        adopted_at=NOW + timedelta(minutes=10),
        source_kind=GuidelineBindingProvenance.NATIVE,
    )
    default_binding = _binding(
        binding_id="binding-b14-default",
        revision=default_v1,
        binding_revision=1,
        adopted_at=NOW + timedelta(minutes=11),
        source_kind=GuidelineBindingProvenance.DEFAULT_MATERIALIZATION,
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.append_binding_cas(
            binding=inline_binding,
            expected_binding_revision=None,
            idempotency_key="bind:inline:b14",
            request_digest="3" * 64,
            actor_type="agent",
        )
        await adapter.append_binding_cas(
            binding=default_binding,
            expected_binding_revision=None,
            idempotency_key="bind:default:b14",
            request_digest="4" * 64,
            materialization_proof=GuidelineDefaultMaterializationProof(
                template_id=template_id,
                template_version=template_version,
                guideline_revision_number=1,
            ),
            actor_type="system",
        )
        await session.commit()

    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.append_binding_cas(
            binding=inline_binding,
            expected_binding_revision=None,
            idempotency_key="bind:inline:b14",
            request_digest="3" * 64,
            actor_type="agent",
        )
        events = tuple(
            (
                await session.execute(
                    select(DomainEventRow)
                    .where(
                        DomainEventRow.event_type
                        == POLICY_BINDING_MATERIALIZED_EVENT_TYPE
                    )
                    .order_by(DomainEventRow.occurred_at)
                )
            ).scalars()
        )
        executions = tuple(
            (
                await session.execute(
                    select(DomainEventHandlerExecution).where(
                        DomainEventHandlerExecution.handler_name
                        == "PolicyConstraintProjectionHandler"
                    )
                )
            ).scalars()
        )
        assert len(events) == len(executions) == 2
        assert [(row.actor_type, row.payload_json["source_kind"]) for row in events] == [
            ("agent", "native"),
            ("system", "default_materialization"),
        ]

    inline_v2 = _revision(
        inline_id,
        "revision-b14-inline-2",
        number=2,
        parent_revision_id=inline_v1.revision_id,
    )
    async with get_session_factory()() as session:
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.append_revision_cas(
            revision=inline_v2,
            next_head=_head(inline_v2),
            expected_head_revision=1,
            idempotency_key="revision:inline:2:b14",
            request_digest="5" * 64,
        )
        await adapter.append_binding_cas(
            binding=_binding(
                binding_id=inline_binding.binding_id,
                revision=inline_v2,
                binding_revision=2,
                adopted_at=NOW + timedelta(minutes=12),
                source_kind=GuidelineBindingProvenance.NATIVE,
            ),
            expected_binding_revision=1,
            idempotency_key="bind:inline:2:b14",
            request_digest="6" * 64,
            actor_type="agent",
        )
        assert int(
            await session.scalar(
                select(func.count())
                .select_from(DomainEventRow)
                .where(
                    DomainEventRow.event_type
                    == POLICY_BINDING_MATERIALIZED_EVENT_TYPE
                )
            )
            or 0
        ) == 3
        await session.rollback()

    async with get_session_factory()() as session:
        assert int(
            await session.scalar(
                select(func.count())
                .select_from(DomainEventRow)
                .where(
                    DomainEventRow.event_type
                    == POLICY_BINDING_MATERIALIZED_EVENT_TYPE
                )
            )
            or 0
        ) == 2
        with pytest.raises(
            IntegrityError,
            match="guideline_impact_audit_evidence_immutable",
        ):
            await session.execute(
                update(DomainEventRow)
                .where(
                    DomainEventRow.event_type
                    == POLICY_BINDING_MATERIALIZED_EVENT_TYPE
                )
                .values(payload_json={"tampered": True})
            )
        await session.rollback()
        with pytest.raises(
            IntegrityError,
            match="guideline_impact_audit_evidence_immutable",
        ):
            await session.execute(
                delete(DomainEventRow).where(
                    DomainEventRow.event_type
                    == POLICY_BINDING_MATERIALIZED_EVENT_TYPE
                )
            )
        await session.rollback()

    async with get_session_factory()() as session:
        unrelated = DomainEventRow(
            id="event-b14-unrelated",
            event_type="unrelated.event",
            board_id=BOARD_ID,
            actor_id="agent-b14",
            actor_type="agent",
            payload_json={},
            occurred_at=NOW + timedelta(minutes=20),
        )
        session.add(unrelated)
        await session.flush((unrelated,))
        session.add(
            DomainEventHandlerExecution(
                id="execution-b14-invalid",
                event_id=unrelated.id,
                handler_name="PolicyConstraintProjectionHandler",
                status="pending",
                attempts=0,
            )
        )
        with pytest.raises(
            IntegrityError,
            match="policy_constraint_execution_event_invalid",
        ):
            await session.flush()
        await session.rollback()

    async with get_session_factory()() as session:
        session.add(
            BoardErasurePermit(
                board_id=BOARD_ID,
                permit_token="permit-b14-materialization",
            )
        )
        await session.flush()
        await session.execute(
            delete(DomainEventRow).where(
                DomainEventRow.event_type
                == POLICY_BINDING_MATERIALIZED_EVENT_TYPE
            )
        )
        await session.delete(await session.get(BoardErasurePermit, BOARD_ID))
        await session.commit()
    async with get_session_factory()() as session:
        assert int(
            await session.scalar(
                select(func.count())
                .select_from(DomainEventRow)
                .where(
                    DomainEventRow.event_type
                    == POLICY_BINDING_MATERIALIZED_EVENT_TYPE
                )
            )
            or 0
        ) == 0
