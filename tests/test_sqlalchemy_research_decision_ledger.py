"""Real-SQLite contract tests for the Community Research Decision Ledger."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import okto_pulse.community.adapters.sqlalchemy_research_decision_ledger as rdl_adapter_module
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
    Ideation,
    Refinement,
    ResearchDecisionDerivationRow,
    ResearchDecisionEntryRow,
    ResearchDecisionHeadRow,
    ResearchDecisionHistoryRow,
    ResearchDecisionIdempotencyRow,
    ResearchDecisionOutboxRow,
    ResearchDecisionSnapshotRow,
    Spec,
)
from okto_pulse.community.adapters.sqlalchemy_research_decision_ledger import (
    CommunitySqlAlchemyResearchDecisionLedger,
)
from okto_pulse.community.adapters.sqlalchemy_consolidation import (
    CommunitySqlAlchemyConsolidationPersistence,
)
from okto_pulse.core.application.domain_event_delivery import (
    event_from_stored,
)
from okto_pulse.core.domain.enums import (
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
)
from okto_pulse.core.domain.research_decision_ledger import (
    RefinementLedgerContext,
    ResearchDecisionAnchor,
    ResearchDecisionAnchorType,
    ResearchDecisionContent,
    ResearchDecisionStatus,
)
from okto_pulse.core.ports.research_decision_ledger import (
    ResearchDecisionIdempotencyConflict,
    ResearchDecisionListQuery,
    ResearchDecisionOffsetListQuery,
    ResearchDecisionPersistenceError,
    ResearchDecisionRefinementVersionConflict,
)
from okto_pulse.core.ports.domain_event_delivery import StoredDomainEvent
from okto_pulse.core.services.research_decision_ledger import (
    AppendResearchDecisionCommand,
    ResearchDecisionLedgerService,
    SupersedeResearchDecisionCommand,
)
from okto_pulse.core.services.ska_observability import (
    reset_ska_metric_samples_for_tests,
    ska_metric_samples,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)
BOARD_ID = "board-rdl"
IDEATION_ID = "ideation-rdl"
REFINEMENT_ID = "refinement-rdl"


class _Ids:
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._counter = 0

    def __call__(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._namespace}_{self._counter}"


def _service(namespace: str, *, offset_seconds: int = 0):
    return ResearchDecisionLedgerService(
        id_factory=_Ids(namespace),
        clock=lambda: NOW + timedelta(seconds=offset_seconds),
    )


def _context(version: int = 1) -> RefinementLedgerContext:
    return RefinementLedgerContext(
        board_id=BOARD_ID,
        refinement_id=REFINEMENT_ID,
        version=version,
        status=RefinementStatus.DRAFT,
        archived=False,
    )


def _content(
    *,
    unknown: str = "Which retry policy should be used?",
    status: ResearchDecisionStatus = ResearchDecisionStatus.OPEN,
) -> ResearchDecisionContent:
    resolved = status is ResearchDecisionStatus.RESOLVED
    return ResearchDecisionContent(
        unknown=unknown,
        status=status,
        anchor=ResearchDecisionAnchor(
            anchor_type=ResearchDecisionAnchorType.FUNCTIONAL_REQUIREMENT,
            anchor_ref="fr_checkout",
        ),
        evidence_refs=("kb:retry-analysis",) if resolved else (),
        alternatives=("bounded backoff", "fixed delay"),
        decision="Use bounded backoff." if resolved else None,
        rationale="It bounds pressure." if resolved else None,
        confidence=0.9 if resolved else None,
    )


def _append_bundle(
    namespace: str,
    *,
    version: int = 1,
    idempotency_key: str | None = None,
    unknown: str = "Which retry policy should be used?",
    offset_seconds: int = 0,
):
    return _service(namespace, offset_seconds=offset_seconds).prepare_append(
        AppendResearchDecisionCommand(
            board_id=BOARD_ID,
            refinement_id=REFINEMENT_ID,
            expected_refinement_version=version,
            content=_content(unknown=unknown),
            actor_id="agent-rdl",
            idempotency_key=idempotency_key,
        ),
        context=_context(version),
    )


async def _schema_engine(path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine


@pytest_asyncio.fixture(loop_scope="function")
async def rig(tmp_path: Path):
    engine = await _schema_engine(tmp_path / "rdl.db")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        session.add_all(
            [
                Board(id=BOARD_ID, name="RDL", owner_id="owner"),
                Ideation(
                    id=IDEATION_ID,
                    board_id=BOARD_ID,
                    title="RDL parent",
                    status=IdeationStatus.DONE,
                    version=1,
                    created_by="owner",
                ),
                Refinement(
                    id=REFINEMENT_ID,
                    ideation_id=IDEATION_ID,
                    board_id=BOARD_ID,
                    title="RDL refinement",
                    status=RefinementStatus.DRAFT,
                    version=1,
                    created_by="owner",
                ),
            ]
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def _counts(session: AsyncSession) -> tuple[int, ...]:
    tables = (
        ResearchDecisionEntryRow,
        ResearchDecisionHeadRow,
        ResearchDecisionHistoryRow,
        DomainEventRow,
        ResearchDecisionOutboxRow,
        ResearchDecisionIdempotencyRow,
    )
    values = []
    for table in tables:
        values.append(
            int(await session.scalar(select(func.count()).select_from(table)) or 0)
        )
    return tuple(values)


async def test_bulk_refinement_write_acquires_policy_board_mutex(
    rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = rdl_adapter_module.lock_policy_board

    async def recording_mutex(
        session: AsyncSession,
        *,
        board_id: str,
    ) -> None:
        calls.append(board_id)
        await original(session, board_id=board_id)

    monkeypatch.setattr(
        rdl_adapter_module,
        "lock_policy_board",
        recording_mutex,
    )
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        await adapter.apply_bundle_cas(
            _append_bundle(
                "policy-board-mutex",
                idempotency_key="rdl-policy-board-mutex",
            )
        )
        await session.commit()

    assert calls == [BOARD_ID]


async def test_append_commits_complete_bundle_and_exact_replay(rig) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        bundle = _append_bundle("append", idempotency_key="rdl-create-1")

        result = await adapter.apply_bundle_cas(bundle)
        await session.commit()

        assert result.replayed is False
        assert result.refinement_version == 2
        assert await _counts(session) == (1, 1, 1, 1, 1, 1)
        event_row = await session.scalar(select(DomainEventRow))
        assert event_row is not None
        assert event_row.actor_type == "agent"
        assert event_row.payload_json["event_schema_version"] == 1
        reconstructed = event_from_stored(
            StoredDomainEvent(
                event_id=event_row.id,
                event_type=event_row.event_type,
                board_id=event_row.board_id,
                actor_id=event_row.actor_id,
                actor_type=event_row.actor_type,
                occurred_at=event_row.occurred_at,
                payload=dict(event_row.payload_json),
            )
        )
        assert reconstructed.refinement_id == REFINEMENT_ID
        execution = await session.scalar(
            select(DomainEventHandlerExecution).where(
                DomainEventHandlerExecution.event_id == event_row.id,
                DomainEventHandlerExecution.handler_name == "ConsolidationEnqueuer",
            )
        )
        assert execution is not None
        assert execution.status == "pending"
        refinement_version = await session.scalar(
            select(Refinement.version).where(Refinement.id == REFINEMENT_ID)
        )
        assert refinement_version == 2

        replay_bundle = _append_bundle(
            "different-generated-identities",
            idempotency_key="rdl-create-1",
            offset_seconds=99,
        )
        replay = await adapter.apply_bundle_cas(replay_bundle)

        assert replay.replayed is True
        assert replay.entry.id == result.entry.id
        assert replay.head.revision == 1
        assert await _counts(session) == (1, 1, 1, 1, 1, 1)


async def test_same_idempotency_key_with_different_payload_fails_closed(rig) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        await adapter.apply_bundle_cas(
            _append_bundle("first", idempotency_key="rdl-key")
        )
        await session.commit()
        before = await _counts(session)

        with pytest.raises(ResearchDecisionIdempotencyConflict):
            await adapter.apply_bundle_cas(
                _append_bundle(
                    "second",
                    idempotency_key="rdl-key",
                    unknown="A different unknown",
                )
            )

        assert await _counts(session) == before
        assert (
            await session.scalar(
                select(Refinement.version).where(Refinement.id == REFINEMENT_ID)
            )
            == 2
        )


async def test_supersede_inserts_successor_and_advances_head_cas(rig) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        first = await adapter.apply_bundle_cas(
            _append_bundle("first", idempotency_key="rdl-first")
        )
        await session.commit()
        current = await adapter.get_current(
            board_id=BOARD_ID,
            refinement_id=REFINEMENT_ID,
            ledger_id=first.entry.ledger_id,
        )
        assert current is not None
        predecessor, head = current
        bundle = _service("successor", offset_seconds=1).prepare_supersede(
            SupersedeResearchDecisionCommand(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                ledger_id=first.entry.ledger_id,
                predecessor_entry_id=first.entry.id,
                expected_refinement_version=2,
                expected_head_revision=1,
                content=_content(status=ResearchDecisionStatus.RESOLVED),
                actor_id="agent-rdl",
                idempotency_key="rdl-successor",
            ),
            context=_context(2),
            current_head=head,
            predecessor=predecessor,
        )

        successor = await adapter.apply_bundle_cas(bundle)
        await session.commit()

        assert successor.head.revision == 2
        assert successor.entry.predecessor_entry_id == first.entry.id
        assert successor.refinement_version == 3
        assert await _counts(session) == (2, 1, 2, 2, 2, 2)
        original = await adapter.get_entry(
            board_id=BOARD_ID,
            refinement_id=REFINEMENT_ID,
            entry_id=first.entry.id,
        )
        assert original == first.entry


async def test_stale_refinement_cas_produces_zero_delta(rig) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        winner = _append_bundle("winner", idempotency_key="winner")
        loser = _append_bundle("loser", idempotency_key="loser")
        await adapter.apply_bundle_cas(winner)
        await session.commit()
        before = await _counts(session)

        with pytest.raises(ResearchDecisionRefinementVersionConflict):
            await adapter.apply_bundle_cas(loser)

        assert await _counts(session) == before
        assert (
            await session.scalar(
                select(Refinement.version).where(Refinement.id == REFINEMENT_ID)
            )
            == 2
        )


@pytest.mark.parametrize(
    "fault_stage",
    [
        "after_refinement_version",
        "after_entry",
        "after_head",
        "after_history",
        "after_event",
        "after_outbox",
        "after_idempotency",
    ],
)
async def test_fault_after_each_write_rolls_back_complete_bundle(
    rig,
    fault_stage: str,
) -> None:
    def fail_at(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError(f"injected:{stage}")

    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(
            session,
            fault_injector=fail_at,
        )
        with pytest.raises(RuntimeError, match=f"injected:{fault_stage}"):
            await adapter.apply_bundle_cas(
                _append_bundle("fault", idempotency_key="fault-key")
            )

        assert await _counts(session) == (0, 0, 0, 0, 0, 0)
        assert (
            await session.scalar(
                select(func.count()).select_from(DomainEventHandlerExecution)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(Refinement.version).where(Refinement.id == REFINEMENT_ID)
            )
            == 1
        )


async def test_entry_update_and_delete_are_rejected_by_storage(rig) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        result = await adapter.apply_bundle_cas(
            _append_bundle("immutable", idempotency_key="immutable")
        )
        await session.commit()

        with pytest.raises(
            IntegrityError,
            match="research_decision_entry_immutable",
        ):
            await session.execute(
                update(ResearchDecisionEntryRow)
                .where(ResearchDecisionEntryRow.id == result.entry.id)
                .values(unknown="mutated")
            )
        await session.rollback()

        with pytest.raises(
            IntegrityError,
            match="research_decision_entry_immutable",
        ):
            await session.execute(
                delete(ResearchDecisionEntryRow).where(
                    ResearchDecisionEntryRow.id == result.entry.id
                )
            )
        await session.rollback()

        persisted = await adapter.get_entry(
            board_id=BOARD_ID,
            refinement_id=REFINEMENT_ID,
            entry_id=result.entry.id,
        )
        assert persisted == result.entry


async def test_list_uses_stable_bounded_keyset_pagination(rig) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        for index in range(3):
            await adapter.apply_bundle_cas(
                _append_bundle(
                    f"page-{index}",
                    version=index + 1,
                    idempotency_key=f"page-{index}",
                    unknown=f"Unknown {index}",
                    offset_seconds=index,
                )
            )
            await session.commit()

        first = await adapter.list_entries(
            ResearchDecisionListQuery(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                limit=2,
            )
        )
        second = await adapter.list_entries(
            ResearchDecisionListQuery(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                limit=2,
                cursor=first.next_cursor,
            )
        )

        assert [entry.content.unknown for entry in first.items] == [
            "Unknown 2",
            "Unknown 1",
        ]
        assert first.has_more is True
        assert [entry.content.unknown for entry in second.items] == ["Unknown 0"]
        assert second.has_more is False


async def test_rest_offset_page_has_exact_filtered_and_overall_totals(
    rig,
) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        entries = []
        for index in range(3):
            entries.append(
                await adapter.apply_bundle_cas(
                    _append_bundle(
                        f"offset-{index}",
                        version=index + 1,
                        idempotency_key=f"offset-{index}",
                        unknown=f"Offset {index}",
                        offset_seconds=index,
                    )
                )
            )
            await session.commit()

        statements: list[str] = []

        def record_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(statement)

        assert session.bind is not None
        reset_ska_metric_samples_for_tests()
        event.listen(
            session.bind.sync_engine,
            "before_cursor_execute",
            record_statement,
        )
        try:
            page = await adapter.list_entries_offset(
                ResearchDecisionOffsetListQuery(
                    board_id=BOARD_ID,
                    refinement_id=REFINEMENT_ID,
                    offset=0,
                    limit=25,
                    ledger_id=entries[1].entry.ledger_id,
                )
            )
        finally:
            event.remove(
                session.bind.sync_engine,
                "before_cursor_execute",
                record_statement,
            )
        sample = ska_metric_samples()[-1]
        assert sample["surface"] == "research_decisions"
        assert sample["subject_type"] == "refinement"
        assert sample["outcome"] == "success"
        assert sample["value"] == 3
        empty_window = await adapter.list_entries_offset(
            ResearchDecisionOffsetListQuery(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                offset=99,
                limit=25,
            )
        )

        assert [item.id for item in page.items] == [entries[1].entry.id]
        assert page.total_filtered == 1
        assert page.total_overall == 3
        assert len(statements) == 3
        assert empty_window.items == ()
        assert empty_window.total_filtered == 3
        assert empty_window.total_overall == 3


async def test_domain_event_actor_type_preserves_human_source(rig) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        bundle = _service("human").prepare_append(
            AppendResearchDecisionCommand(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                expected_refinement_version=1,
                content=_content(),
                actor_id="human-owner",
                actor_type="user",
                idempotency_key="human-rdl",
            ),
            context=_context(),
        )
        await adapter.apply_bundle_cas(bundle)
        await session.commit()

        event_row = await session.scalar(select(DomainEventRow))
        assert event_row is not None
        assert event_row.actor_type == "user"


async def test_consolidation_projection_loads_current_rdl_heads_with_two_queries(
    rig,
) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        first = await adapter.apply_bundle_cas(
            _append_bundle("projection-open", idempotency_key="projection-open")
        )
        current = await adapter.get_current(
            board_id=BOARD_ID,
            refinement_id=REFINEMENT_ID,
            ledger_id=first.entry.ledger_id,
        )
        assert current is not None
        predecessor, head = current
        successor_bundle = _service(
            "projection-resolved",
            offset_seconds=1,
        ).prepare_supersede(
            SupersedeResearchDecisionCommand(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                ledger_id=first.entry.ledger_id,
                predecessor_entry_id=first.entry.id,
                expected_refinement_version=2,
                expected_head_revision=1,
                content=_content(status=ResearchDecisionStatus.RESOLVED),
                actor_id="agent-rdl",
                idempotency_key="projection-resolved",
            ),
            context=_context(2),
            current_head=head,
            predecessor=predecessor,
        )
        successor = await adapter.apply_bundle_cas(successor_bundle)
        await session.commit()

    async with rig() as session:
        artifact = await session.get(Refinement, REFINEMENT_ID)
        assert artifact is not None
        statements: list[str] = []

        def record_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            statements.append(statement)

        assert session.bind is not None
        database_path = Path(str(session.bind.url.database))
        event.listen(
            session.bind.sync_engine,
            "before_cursor_execute",
            record_statement,
        )
        try:
            projection = await CommunitySqlAlchemyConsolidationPersistence().load_projection_inputs(
                session,
                board_id=BOARD_ID,
                artifact_type="refinement",
                artifact_id=REFINEMENT_ID,
                artifact=artifact,
            )
        finally:
            event.remove(
                session.bind.sync_engine,
                "before_cursor_execute",
                record_statement,
            )

        assert len(statements) == 2
        assert projection.quality_assessments == ()
        assert len(projection.research_decisions) == 1
        current_summary = projection.research_decisions[0]
        assert current_summary.entry_id == successor.entry.id
        assert current_summary.head_revision == 2
        assert current_summary.status == "resolved"
        assert current_summary.decision == "Use bounded backoff."
        assert len(current_summary.projection_fingerprint) == 64
        assert current_summary.to_worker_dict()["alternatives"] == [
            "bounded backoff",
            "fixed delay",
        ]

    from okto_pulse.community.adapters.board_source_reader import (
        _current_research_decision_head_fingerprints,
    )
    from okto_pulse.core.kg.board_source_store import (
        projected_root_content_hash,
    )

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rebuild_fingerprints = _current_research_decision_head_fingerprints(
            connection,
            board_id=BOARD_ID,
        )
    projection_key = (BOARD_ID, "refinement", REFINEMENT_ID)
    assert rebuild_fingerprints[projection_key] == (
        current_summary.projection_fingerprint,
    )
    assert projected_root_content_hash(
        "b" * 64,
        research_decision_head_fingerprints=(current_summary.projection_fingerprint,),
    ) == projected_root_content_hash(
        "b" * 64,
        research_decision_head_fingerprints=(rebuild_fingerprints[projection_key]),
    )


async def test_snapshot_and_spec_derivation_are_version_bound_references_only(
    rig,
) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        first = await adapter.apply_bundle_cas(
            _append_bundle("snapshot-open", idempotency_key="snapshot-open")
        )
        current = await adapter.get_current(
            board_id=BOARD_ID,
            refinement_id=REFINEMENT_ID,
            ledger_id=first.entry.ledger_id,
        )
        assert current is not None
        predecessor, head = current
        resolved = _service("snapshot-resolved", offset_seconds=1).prepare_supersede(
            SupersedeResearchDecisionCommand(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                ledger_id=first.entry.ledger_id,
                predecessor_entry_id=first.entry.id,
                expected_refinement_version=2,
                expected_head_revision=1,
                content=_content(status=ResearchDecisionStatus.RESOLVED),
                actor_id="agent-rdl",
                idempotency_key="snapshot-resolved",
            ),
            context=_context(2),
            current_head=head,
            predecessor=predecessor,
        )
        await adapter.apply_bundle_cas(resolved)
        await adapter.apply_bundle_cas(
            _append_bundle(
                "snapshot-other-open",
                version=3,
                idempotency_key="snapshot-other-open",
                unknown="Should this remain open?",
                offset_seconds=2,
            )
        )
        refinement = await session.get(Refinement, REFINEMENT_ID)
        assert refinement is not None
        refinement.status = RefinementStatus.DONE
        current = await adapter.list_current_entries_with_heads(
            board_id=BOARD_ID,
            refinement_id=REFINEMENT_ID,
        )
        service = _service("snapshot", offset_seconds=3)
        snapshot = service.freeze_heads(
            context=RefinementLedgerContext(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                version=4,
                status=RefinementStatus.DONE,
                archived=False,
            ),
            current=current,
        )
        stored_snapshot = await adapter.save_snapshot(snapshot)
        sentinel_decisions = [{"id": "decision-existing", "title": "Keep me"}]
        spec = Spec(
            id="spec-rdl",
            board_id=BOARD_ID,
            ideation_id=IDEATION_ID,
            refinement_id=REFINEMENT_ID,
            title="Derived RDL spec",
            decisions=sentinel_decisions,
            status=SpecStatus.DRAFT,
            version=1,
            created_by="owner",
        )
        spec_id = spec.id
        snapshot_id = snapshot.id
        session.add(spec)
        await session.flush()
        derivation = service.derive_resolved_references(
            snapshot=stored_snapshot,
            board_id=BOARD_ID,
            spec_id=spec.id,
            spec_version=spec.version,
        )
        stored_derivation = await adapter.save_derivation(derivation)
        await session.commit()

        assert len(stored_snapshot.heads) == 2
        assert len(stored_derivation.references) == 1
        assert stored_derivation.references[0].entry_id == resolved.entry.id
        assert stored_derivation.references[0].content_digest == next(
            head.content_digest
            for head in stored_snapshot.heads
            if head.entry_id == resolved.entry.id
        )
        assert stored_derivation.source_refinement_version == 4
        assert stored_derivation.spec_version == 1
        snapshot_row = await session.get(
            ResearchDecisionSnapshotRow,
            snapshot_id,
        )
        assert snapshot_row is not None
        assert all(
            len(item["content_digest"]) == 64 for item in snapshot_row.heads_json
        )
        derivation_row = await session.scalar(
            select(ResearchDecisionDerivationRow).where(
                ResearchDecisionDerivationRow.spec_id == spec_id
            )
        )
        assert derivation_row is not None
        assert len(derivation_row.references_json[0]["content_digest"]) == 64
        assert (
            await adapter.get_snapshot_for_version(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                refinement_version=4,
            )
            == stored_snapshot
        )
        assert (
            await adapter.get_derivation(
                board_id=BOARD_ID,
                spec_id=spec.id,
                spec_version=1,
            )
            == stored_derivation
        )
        assert (
            await session.scalar(select(Spec.decisions).where(Spec.id == spec.id))
            == sentinel_decisions
        )

        with pytest.raises(
            IntegrityError,
            match="research_decision_entry_immutable",
        ):
            await session.execute(
                update(ResearchDecisionSnapshotRow)
                .where(ResearchDecisionSnapshotRow.id == snapshot_id)
                .values(heads_json=[])
            )
        await session.rollback()
        with pytest.raises(
            IntegrityError,
            match="research_decision_entry_immutable",
        ):
            await session.execute(
                delete(ResearchDecisionDerivationRow).where(
                    ResearchDecisionDerivationRow.spec_id == spec_id
                )
            )


async def test_snapshot_hydration_rejects_content_digest_tampering(rig) -> None:
    async with rig() as session:
        adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
        result = await adapter.apply_bundle_cas(
            _append_bundle("tamper", idempotency_key="tamper")
        )
        session.add(
            ResearchDecisionSnapshotRow(
                id="snapshot-tampered",
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                refinement_version=2,
                heads_json=[
                    {
                        "ledger_id": result.entry.ledger_id,
                        "entry_id": result.entry.id,
                        "head_revision": result.head.revision,
                        "head_refinement_version": (result.head.refinement_version),
                        "status": result.entry.status.value,
                        "content_digest": "0" * 64,
                    }
                ],
                created_at=NOW,
            )
        )
        await session.commit()

        with pytest.raises(
            ResearchDecisionPersistenceError,
            match="research_decision_content_digest_mismatch",
        ):
            await adapter.get_snapshot(
                board_id=BOARD_ID,
                refinement_id=REFINEMENT_ID,
                snapshot_id="snapshot-tampered",
            )
