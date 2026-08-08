"""Restart-safe grandfathering of pre-v2 Knowledge Base attachments."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import okto_pulse.community.adapters.relational_schema_steps as schema_steps
from okto_pulse.community.adapters.knowledge_propagation_backfill import (
    KNOWLEDGE_PROPAGATION_BACKFILL_ACTOR_ID,
    KnowledgePropagationBackfillError,
    _latest_grandfather_details,
    backfill_knowledge_propagation_v2,
)
from okto_pulse.community.adapters.sqlalchemy_base import Base
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    install_community_sqlite_pragmas,
)
from okto_pulse.community.adapters.sqlalchemy_knowledge_propagation import (
    CommunitySqlAlchemyKnowledgePropagationStore,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Board,
    Card,
    KnowledgeMutationAttemptRecord,
    KnowledgeMutationLedgerRecord,
    KnowledgePropagationScopeRecord,
    Spec,
    SpecKnowledgeBase,
)
from okto_pulse.core.domain.knowledge_selection import KnowledgeTargetType
from okto_pulse.core.ports.knowledge_propagation import (
    KnowledgeMutationAttempt,
    KnowledgeTargetKey,
)
from okto_pulse.core.services.knowledge_propagation import (
    KnowledgePropagationService,
    KnowledgePropagationServiceError,
)


BOARD_ID = "board-backfill"
SPEC_ID = "spec-backfill"
CARD_ID = "card-backfill"


@pytest.fixture
async def legacy_database(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'legacy-backfill.sqlite3'}"
    )
    install_community_sqlite_pragmas(engine)
    sessions = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)
    await schema_steps._migrate_knowledge_propagation_v2_schema()

    card_legacy_json = [
        {
            "id": "card-legacy-cycle",
            "title": "Cycle",
            "content": "historical card content",
            "source_kb_id": "card-legacy-cycle",
            "governance_metadata": {
                "origin_class": "selected_legacy",
                "durable_selection_evidence": True,
            },
        }
    ]
    async with sessions() as session:
        session.add(
            Board(
                id=BOARD_ID,
                name="Legacy propagation backfill",
                owner_id="owner",
            )
        )
        session.add(
            Spec(
                id=SPEC_ID,
                board_id=BOARD_ID,
                title="Legacy spec",
                created_by="legacy-user",
            )
        )
        session.add(
            Card(
                id=CARD_ID,
                board_id=BOARD_ID,
                spec_id=SPEC_ID,
                title="Legacy card",
                created_by="legacy-user",
                knowledge_bases=copy.deepcopy(card_legacy_json),
            )
        )
        await session.flush()
        session.add(
            SpecKnowledgeBase(
                id="spec-legacy-all",
                spec_id=SPEC_ID,
                title="Legacy all",
                content="historical spec content",
                created_by="legacy-user",
            )
        )
        await session.commit()
    try:
        yield engine, sessions, card_legacy_json
    finally:
        await engine.dispose()


async def _counts(sessions) -> tuple[int, int, int]:
    async with sessions() as session:
        return (
            int(
                await session.scalar(
                    select(func.count(KnowledgePropagationScopeRecord.id))
                )
                or 0
            ),
            int(
                await session.scalar(
                    select(func.count(KnowledgeMutationLedgerRecord.operation_id))
                )
                or 0
            ),
            int(
                await session.scalar(
                    select(func.count(KnowledgeMutationAttemptRecord.attempt_id))
                )
                or 0
            ),
        )


async def test_backfill_commits_each_target_and_replay_is_a_read_only_noop(
    legacy_database,
) -> None:
    _engine, sessions, original_card_json = legacy_database
    store = CommunitySqlAlchemyKnowledgePropagationStore(sessions)
    service = KnowledgePropagationService(port=store)

    first = await backfill_knowledge_propagation_v2(
        session_factory=sessions,
        store=store,
        service=service,
    )
    assert first.scanned_targets == 2
    assert first.applied_targets == 2
    assert first.already_current_targets == 0
    assert await _counts(sessions) == (2, 2, 0)

    async with sessions() as session:
        scopes = (
            (
                await session.execute(
                    select(KnowledgePropagationScopeRecord).order_by(
                        KnowledgePropagationScopeRecord.target_type,
                        KnowledgePropagationScopeRecord.target_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        ledgers = (
            (
                await session.execute(
                    select(KnowledgeMutationLedgerRecord).order_by(
                        KnowledgeMutationLedgerRecord.target_type,
                        KnowledgeMutationLedgerRecord.target_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        stored_card_json = await session.scalar(
            select(Card.knowledge_bases).where(Card.id == CARD_ID)
        )

    assert all(
        scope.scope_revision == 1
        and scope.v2_active is False
        and scope.selection_state is None
        for scope in scopes
    )
    assert all(
        ledger.operation_kind == "grandfather"
        and ledger.outcome == "grandfathered"
        and ledger.actor_id == KNOWLEDGE_PROPAGATION_BACKFILL_ACTOR_ID
        and ledger.idempotency_key.startswith("kb-grandfather-v2:")
        for ledger in ledgers
    )
    assert stored_card_json == original_card_json

    by_target_type = {ledger.target_type: ledger.details for ledger in ledgers}
    spec_item = by_target_type["spec"]["grandfathered_attachments"][0]
    assert spec_item["origin_class"] == "legacy_all"
    assert spec_item["effective"] is True
    assert spec_item["source_revision"] is None
    assert spec_item["source_content_sha256"] is None
    card_item = by_target_type["card"]["grandfathered_attachments"][0]
    assert card_item["origin_class"] == "legacy_unresolved"
    assert card_item["effective"] is False
    assert card_item["evidence"]["origin_cycle"] is True
    # Arbitrary pre-v2 JSON cannot self-assert selected_legacy.
    assert card_item["evidence"]["durable_selection_evidence"] is False

    second = await backfill_knowledge_propagation_v2(
        session_factory=sessions,
        store=store,
        service=service,
    )
    assert second.scanned_targets == 2
    assert second.applied_targets == 0
    assert second.already_current_targets == 2
    assert await _counts(sessions) == (2, 2, 0)


async def test_failure_after_one_target_is_resumed_without_duplicate_history(
    legacy_database,
) -> None:
    _engine, sessions, _original_card_json = legacy_database
    store = CommunitySqlAlchemyKnowledgePropagationStore(sessions)
    real_service = KnowledgePropagationService(port=store)

    class FailSecondTarget:
        def __init__(self) -> None:
            self.calls = 0

        async def grandfather(self, context, command):
            self.calls += 1
            if self.calls == 2:
                raise KnowledgePropagationServiceError(
                    "injected_backfill_failure",
                    "fault after first target commit",
                )
            return await real_service.grandfather(context, command)

    with pytest.raises(KnowledgePropagationBackfillError) as exc_info:
        await backfill_knowledge_propagation_v2(
            session_factory=sessions,
            store=store,
            service=FailSecondTarget(),  # type: ignore[arg-type]
        )
    # Deterministic order is card before spec; the card transaction committed.
    assert exc_info.value.target.target_type is KnowledgeTargetType.SPEC
    assert await _counts(sessions) == (1, 1, 0)

    resumed = await backfill_knowledge_propagation_v2(
        session_factory=sessions,
        store=store,
        service=real_service,
    )
    assert resumed.scanned_targets == 2
    assert resumed.applied_targets == 1
    assert resumed.already_current_targets == 1
    assert await _counts(sessions) == (2, 2, 0)


async def test_rejection_attempt_is_appended_only_after_failed_uow_is_closed(
    legacy_database,
) -> None:
    _engine, sessions, _original_card_json = legacy_database
    store = CommunitySqlAlchemyKnowledgePropagationStore(sessions)

    class RejectFirstTarget:
        async def grandfather(self, _context, command):
            attempt = KnowledgeMutationAttempt(
                attempt_id="backfill-rejection-attempt",
                target=command.target,
                idempotency_key=command.idempotency_key,
                request_hash="f" * 64,
                operation_kind="grandfather",
                actor_id=command.actor_id,
                outcome="rejected",
                recorded_at=datetime(2026, 7, 23, tzinfo=timezone.utc),
                reason_code="injected_rejection",
                reason_detail="rejected after inventory validation",
            )
            raise KnowledgePropagationServiceError(
                "injected_rejection",
                "rejected after inventory validation",
                ledger_attempt=attempt,
            )

    with pytest.raises(KnowledgePropagationBackfillError):
        await backfill_knowledge_propagation_v2(
            session_factory=sessions,
            store=store,
            service=RejectFirstTarget(),  # type: ignore[arg-type]
        )
    assert await _counts(sessions) == (0, 0, 1)
    async with sessions() as session:
        attempt = await session.get(
            KnowledgeMutationAttemptRecord,
            "backfill-rejection-attempt",
        )
    assert attempt is not None
    assert attempt.scope_id is None
    assert attempt.outcome == "rejected"


async def test_latest_grandfather_uses_revision_and_rejects_ambiguous_tie(
    legacy_database,
) -> None:
    _engine, sessions, _original_card_json = legacy_database
    store = CommunitySqlAlchemyKnowledgePropagationStore(sessions)
    service = KnowledgePropagationService(port=store)
    await backfill_knowledge_propagation_v2(
        session_factory=sessions,
        store=store,
        service=service,
    )
    target = KnowledgeTargetKey(
        board_id=BOARD_ID,
        target_type="card",
        target_id=CARD_ID,
    )
    async with sessions() as session:
        scope = (
            await session.execute(
                select(KnowledgePropagationScopeRecord).where(
                    KnowledgePropagationScopeRecord.target_type == "card"
                )
            )
        ).scalar_one()
        higher_details = {
            "contract_version": 2,
            "legacy_content_preserved": True,
            "grandfathered_attachments": [{"marker": "higher-revision"}],
        }
        session.add(
            KnowledgeMutationLedgerRecord(
                operation_id="higher-revision",
                scope_id=scope.id,
                board_id=BOARD_ID,
                target_type="card",
                target_id=CARD_ID,
                idempotency_key="higher-revision",
                request_hash="1" * 64,
                operation_kind="grandfather",
                actor_id=KNOWLEDGE_PROPAGATION_BACKFILL_ACTOR_ID,
                previous_revision=1,
                revision=2,
                outcome="grandfathered",
                details=higher_details,
                # Older clock evidence must not outrank revision.
                applied_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                recorded_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        await session.commit()

    async with sessions() as session:
        assert await _latest_grandfather_details(session, target) == higher_details
        session.add(
            KnowledgeMutationLedgerRecord(
                operation_id="higher-revision-conflict",
                scope_id=scope.id,
                board_id=BOARD_ID,
                target_type="card",
                target_id=CARD_ID,
                idempotency_key="higher-revision-conflict",
                request_hash="2" * 64,
                operation_kind="grandfather",
                actor_id=KNOWLEDGE_PROPAGATION_BACKFILL_ACTOR_ID,
                previous_revision=1,
                revision=2,
                outcome="grandfathered",
                details={
                    "contract_version": 2,
                    "legacy_content_preserved": True,
                    "grandfathered_attachments": [{"marker": "conflicting-tie"}],
                },
                applied_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
                recorded_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
            )
        )
        await session.commit()
        with pytest.raises(
            ValueError,
            match="grandfather_revision_ambiguous",
        ):
            await _latest_grandfather_details(session, target)
