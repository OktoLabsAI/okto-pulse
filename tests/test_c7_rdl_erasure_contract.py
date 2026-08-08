"""C7 regression contracts for RDL-aware subject and board erasure.

These tests deliberately exercise real SQLite foreign keys and immutable-row
triggers.  They are kept separate from the adapter feature tests so a failure
points at lifecycle/erasure ordering instead of ordinary RDL persistence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    JSON,
    delete,
    event,
    func,
    insert,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import okto_pulse.core.services.main as main_services
from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters import relational_schema_steps
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.sqlalchemy_quality_assessment_lifecycle import (
    CommunitySqlAlchemyQualityAssessmentLifecycle,
)
from okto_pulse.community.adapters.sqlalchemy_domain_event_delivery import (
    CommunitySqlAlchemyDomainEventPublisher,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog,
    Base,
    Board,
    BoardErasureJob,
    BoardErasurePermit,
    ChecklistBindingHeadRow,
    ChecklistBindingRow,
    ChecklistExecutionHeadRow,
    ChecklistExecutionRow,
    ChecklistItemResultRow,
    ChecklistReceiptRow,
    ChecklistTemplateVersionRow,
    DomainEventRow,
    Ideation,
    QualityAssessmentHeadRow,
    QualityAssessmentLifecycleStaleTransitionRow,
    QualityAssessmentLifecycleTransitionRow,
    QualityAssessmentOutboxRow,
    QualityAssessmentReceiptRow,
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
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
)
from okto_pulse.core.application.kg_operations import (
    CoreKnowledgeGraphOperations,
)
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.boards_crud import (
    DeleteBoardCommand,
    DeleteBoardUseCase,
)
from okto_pulse.core.application.use_cases.refinements_crud import (
    DeleteRefinementCommand,
    DeleteRefinementUseCase,
)
from okto_pulse.core.domain.enums import (
    IdeationStatus,
    RefinementStatus,
    SpecStatus,
)
from okto_pulse.core.domain.quality_assessment import AssessmentSubjectType
from okto_pulse.core.domain.quality_assessment_lifecycle import (
    AssessmentPurgeResource,
)
from okto_pulse.core.domain.research_decision_ledger import (
    RefinementLedgerContext,
    ResearchDecisionAnchor,
    ResearchDecisionAnchorType,
    ResearchDecisionContent,
    ResearchDecisionStatus,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import (
    register_application_persistence_port,
)
from okto_pulse.core.ports.domain_event_delivery import (
    register_domain_event_publisher,
)
from okto_pulse.core.ports.kg_governance import (
    register_kg_governance_store,
)
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
)
from okto_pulse.core.services.main import (
    ArchiveService,
    GovernedArtifactDeletionReceipt,
)
from okto_pulse.core.services.quality_assessment_lifecycle import (
    QualityAssessmentLifecycleService,
)
from okto_pulse.core.services.research_decision_ledger import (
    AppendResearchDecisionCommand,
    ResearchDecisionLedgerService,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)
DIGEST = "a" * 64
EPOCH = "quality-assessment-legacy-import/v1"
EPOCH_TABLES = (
    "quality_assessment_legacy_import_runs",
    "quality_assessment_legacy_import_candidates",
    "quality_assessment_legacy_import_checkpoints",
    "quality_assessment_legacy_import_resolutions",
    "quality_assessment_legacy_import_completions",
)

RDL_TABLES = (
    ResearchDecisionDerivationRow,
    ResearchDecisionSnapshotRow,
    ResearchDecisionIdempotencyRow,
    ResearchDecisionOutboxRow,
    DomainEventRow,
    ResearchDecisionHistoryRow,
    ResearchDecisionHeadRow,
    ResearchDecisionEntryRow,
)
RDL_EVENT_TYPES = (
    "research_decision.appended",
    "research_decision.superseded",
)

QUALITY_TABLES = (
    QualityAssessmentHeadRow,
    QualityAssessmentOutboxRow,
    QualityAssessmentReceiptRow,
)

CHECKLIST_TABLES = (
    ChecklistExecutionHeadRow,
    ChecklistItemResultRow,
    ChecklistReceiptRow,
    ChecklistExecutionRow,
    ChecklistBindingHeadRow,
    ChecklistBindingRow,
)


class _Ids:
    def __init__(self, namespace: str) -> None:
        self._namespace = namespace
        self._counter = 0

    def __call__(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._namespace}_{self._counter}"


async def _create_factory(
    database_path: Path,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        manifest = (
            relational_schema_steps._quality_c7_sqlite_trigger_manifest()  # noqa: SLF001
        )
        for trigger_name, (_table_name, trigger_sql) in manifest.items():
            await connection.exec_driver_sql(
                f'DROP TRIGGER IF EXISTS "{trigger_name}"'
            )
            await connection.exec_driver_sql(trigger_sql)
    return engine, async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )


@pytest.fixture
async def database(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine, factory = await _create_factory(tmp_path / "c7-erasure.sqlite3")
    try:
        yield engine, factory
    finally:
        await engine.dispose()


def _rdl_content() -> ResearchDecisionContent:
    return ResearchDecisionContent(
        unknown="Which retry policy should be used?",
        status=ResearchDecisionStatus.OPEN,
        anchor=ResearchDecisionAnchor(
            anchor_type=ResearchDecisionAnchorType.FUNCTIONAL_REQUIREMENT,
            anchor_ref="fr_retry",
        ),
        alternatives=("bounded backoff", "fixed delay"),
    )


async def _seed_subject(
    session: AsyncSession,
    *,
    namespace: str,
    board_id: str,
) -> dict[str, str]:
    ideation_id = f"ideation-{namespace}"
    refinement_id = f"refinement-{namespace}"
    spec_id = f"spec-{namespace}"
    session.add_all(
        [
            Board(
                id=board_id,
                name=f"C7 {namespace}",
                owner_id=f"owner-{namespace}",
                realm_id="local",
            ),
            Ideation(
                id=ideation_id,
                board_id=board_id,
                title=f"Ideation {namespace}",
                status=IdeationStatus.DONE,
                version=1,
                created_by=f"owner-{namespace}",
            ),
            Refinement(
                id=refinement_id,
                ideation_id=ideation_id,
                board_id=board_id,
                title=f"Refinement {namespace}",
                status=RefinementStatus.DRAFT,
                version=1,
                created_by=f"owner-{namespace}",
            ),
            Spec(
                id=spec_id,
                board_id=board_id,
                ideation_id=ideation_id,
                refinement_id=refinement_id,
                title=f"Spec {namespace}",
                status=SpecStatus.DRAFT,
                version=1,
                created_by=f"owner-{namespace}",
            ),
        ]
    )
    await session.flush()

    rdl_service = ResearchDecisionLedgerService(
        id_factory=_Ids(namespace),
        clock=lambda: NOW,
    )
    rdl_adapter = CommunitySqlAlchemyResearchDecisionLedger(session)
    rdl_result = await rdl_adapter.apply_bundle_cas(
        rdl_service.prepare_append(
            AppendResearchDecisionCommand(
                board_id=board_id,
                refinement_id=refinement_id,
                expected_refinement_version=1,
                content=_rdl_content(),
                actor_id=f"agent-{namespace}",
                idempotency_key=f"rdl-{namespace}",
            ),
            context=RefinementLedgerContext(
                board_id=board_id,
                refinement_id=refinement_id,
                version=1,
                status=RefinementStatus.DRAFT,
                archived=False,
            ),
        )
    )

    snapshot_id = f"rdls-{namespace}"
    quality_event_id = f"quality-event-{namespace}"
    quality_receipt_id = f"quality-receipt-{namespace}"
    quality_outbox_id = f"quality-outbox-{namespace}"
    template_version = f"template-{namespace}"
    checklist_execution_id = f"checklist-execution-{namespace}"
    checklist_receipt_id = f"checklist-receipt-{namespace}"

    # No ORM relationships are declared between these persistence-only rows,
    # so stage them in their physical FK order instead of relying on mapper
    # dependency sorting.
    session.add_all(
        [
            ResearchDecisionSnapshotRow(
                id=snapshot_id,
                board_id=board_id,
                refinement_id=refinement_id,
                refinement_version=2,
                heads_json=[
                    {
                        "ledger_id": rdl_result.entry.ledger_id,
                        "entry_id": rdl_result.entry.id,
                    }
                ],
                created_at=NOW,
            ),
            DomainEventRow(
                id=quality_event_id,
                event_type="quality.assessment.recorded",
                board_id=board_id,
                actor_id=f"agent-{namespace}",
                actor_type="agent",
                payload_json={"receipt_id": quality_receipt_id},
                occurred_at=NOW,
            ),
            QualityAssessmentReceiptRow(
                id=quality_receipt_id,
                board_id=board_id,
                subject_type="refinement",
                subject_id=refinement_id,
                subject_version=2,
                assessment_kind="ambiguity",
                origin="human_or_agent",
                source="native",
                channel="manual",
                outcome="recorded",
                scale_kind="ambiguity_score",
                scale_minimum=1,
                scale_maximum=5,
                scale_direction="lower_better",
                score=2,
                justification="C7 erasure fixture",
                content_digest=DIGEST,
                clarification_digest=DIGEST,
                ruleset_digest=DIGEST,
                taxonomy_digest=DIGEST,
                policy_digest=DIGEST,
                input_digest=DIGEST,
                canonicalization_version="v1",
                ruleset_version="v1",
                taxonomy_version="v1",
                analyzer_version="v1",
                policy_version="v1",
                run_identity_digest=DIGEST,
                authority_digest=DIGEST,
                idempotency_key=f"quality-{namespace}",
                request_digest=DIGEST,
                created_by=f"agent-{namespace}",
                created_at=NOW,
                predecessor_receipt_id=None,
                contract_version="quality-assessment/v1",
                event_id=quality_event_id,
                history_id=f"quality-history-{namespace}",
                outbox_id=quality_outbox_id,
                head_revision=1,
            ),
            ChecklistTemplateVersionRow(
                version=template_version,
                template_id=f"specify-{namespace}",
                digest=DIGEST,
                items_json=[],
                created_at=NOW,
            ),
        ]
    )
    await session.flush()

    session.add_all(
        [
            ChecklistBindingRow(
                board_id=board_id,
                target_type="spec",
                phase="spec_validation",
                version=1,
                digest=DIGEST,
                mode="advisory",
                template_version=template_version,
                created_at=NOW,
            ),
            ChecklistExecutionRow(
                id=checklist_execution_id,
                board_id=board_id,
                spec_id=spec_id,
                spec_version=1,
                content_digest=DIGEST,
                input_digest=DIGEST,
                template_version=template_version,
                template_digest=DIGEST,
                binding_version=1,
                binding_digest=DIGEST,
                binding_mode="advisory",
                request_digest=DIGEST,
                idempotency_key=f"checklist-start-{namespace}",
                created_by=f"agent-{namespace}",
                created_at=NOW,
                revision=2,
                status="submitted",
                receipt_id=checklist_receipt_id,
            ),
        ]
    )
    await session.flush()

    session.add_all(
        [
            ResearchDecisionDerivationRow(
                id=f"rdld-{namespace}",
                board_id=board_id,
                spec_id=spec_id,
                spec_version=1,
                source_refinement_id=refinement_id,
                source_refinement_version=2,
                source_snapshot_id=snapshot_id,
                references_json=[],
                created_at=NOW,
            ),
            QualityAssessmentHeadRow(
                board_id=board_id,
                subject_type="refinement",
                subject_id=refinement_id,
                assessment_kind="ambiguity",
                receipt_id=quality_receipt_id,
                revision=1,
                updated_at=NOW,
            ),
            QualityAssessmentOutboxRow(
                id=quality_outbox_id,
                event_id=quality_event_id,
                receipt_id=quality_receipt_id,
                board_id=board_id,
                event_type="quality.assessment.recorded",
                payload_json={"receipt_id": quality_receipt_id},
                status="pending",
                occurred_at=NOW,
            ),
            ChecklistBindingHeadRow(
                board_id=board_id,
                target_type="spec",
                phase="spec_validation",
                version=1,
                digest=DIGEST,
                updated_at=NOW,
            ),
            ChecklistReceiptRow(
                id=checklist_receipt_id,
                board_id=board_id,
                spec_id=spec_id,
                execution_id=checklist_execution_id,
                spec_version=1,
                content_digest=DIGEST,
                input_digest=DIGEST,
                template_version=template_version,
                template_digest=DIGEST,
                binding_version=1,
                binding_digest=DIGEST,
                binding_mode="advisory",
                source="native",
                request_digest=DIGEST,
                idempotency_key=f"checklist-submit-{namespace}",
                manual_checklist_ref=None,
                predecessor_receipt_id=None,
                created_by=f"agent-{namespace}",
                created_at=NOW,
                head_revision=1,
            ),
        ]
    )
    await session.flush()

    session.add_all(
        [
            ChecklistItemResultRow(
                receipt_id=checklist_receipt_id,
                item_id="structure",
                execution_id=checklist_execution_id,
                outcome="pass",
                anchor=f"spec://{spec_id}/structure",
                rationale=None,
                order_index=0,
            ),
            ChecklistExecutionHeadRow(
                board_id=board_id,
                spec_id=spec_id,
                phase="spec_validation",
                receipt_id=checklist_receipt_id,
                revision=1,
                updated_at=NOW,
            ),
        ]
    )
    await session.flush()
    return {
        "board_id": board_id,
        "ideation_id": ideation_id,
        "refinement_id": refinement_id,
        "spec_id": spec_id,
        "rdl_entry_id": rdl_result.entry.id,
        "quality_receipt_id": quality_receipt_id,
        "checklist_receipt_id": checklist_receipt_id,
    }


def _epoch_column_value(
    column,
    *,
    namespace: str,
    board_id: str,
    subject_id: str,
    quality_receipt_id: str,
) -> Any:
    """Build a valid one-candidate epoch row without coupling to ORM names.

    The epoch adapter is operation-transactional and its physical models may
    remain intentionally private.  The test therefore targets the five stable
    SQL table names from the C7 contract and fills their public columns.
    """

    name = column.name
    explicit: dict[str, Any] = {
        "board_id": board_id,
        "epoch": EPOCH,
        "cutoff": NOW,
        "ordinal": 0,
        "candidate_ordinal": 0,
        "cursor_ordinal": 0,
        "subject_type": "ideation",
        "last_subject_type": "ideation",
        "subject_id": subject_id,
        "last_subject_id": subject_id,
        "assessment_kind": "ambiguity",
        "last_assessment_kind": "ambiguity",
        "subject_version": 1,
        "resolution": "imported",
        "receipt_id": quality_receipt_id,
        "source": "legacy_migration",
        "origin": "legacy_import",
        "channel": "migration",
        "status": "completed",
        "scale_kind": "ambiguity_score",
        "scale_direction": "lower_better",
        "scale_minimum": 1.0,
        "scale_maximum": 5.0,
        "score": 2.0,
        "justification": "C7 epoch erasure fixture",
        "legacy_source_id": "scope_assessment",
        "contract_version": "quality-assessment-legacy-import-contract/v1",
        "candidate_count": 1,
        "scanned_count": 1,
        "rejected_count": 0,
        "processed_count": 1,
        "imported_count": 1,
        "native_wins_count": 0,
        "head_revision": 1,
        "revision": 1,
    }
    if name in explicit:
        return explicit[name]
    if name.endswith("_digest") or name.endswith("_sha256"):
        return DIGEST
    if name.endswith("_at") or isinstance(column.type, DateTime):
        return NOW
    if name.endswith("_count") or isinstance(column.type, Integer):
        return 1
    if isinstance(column.type, Float):
        return 1.0
    if isinstance(column.type, Boolean):
        return True
    if isinstance(column.type, JSON):
        if "key" in name:
            return {
                "board_id": board_id,
                "subject_type": "ideation",
                "subject_id": subject_id,
                "assessment_kind": "ambiguity",
                "epoch": EPOCH,
            }
        return [] if name.endswith("s_json") else {}
    if name == "idempotency_key":
        return f"{EPOCH}:{namespace}"
    if name.endswith("_id") or name == "id":
        return f"{name}-{namespace}"
    if name.startswith("all_") or name.startswith("zero_") or name.endswith(
        "_consistent"
    ):
        return True
    return f"{name}-{namespace}"


async def _seed_epoch(
    session: AsyncSession,
    *,
    namespace: str,
    subject: dict[str, str],
) -> None:
    missing = [name for name in EPOCH_TABLES if name not in Base.metadata.tables]
    assert not missing, (
        "C7 Community epoch schema is not wired into Base.metadata: "
        f"{missing}"
    )
    for table_name in EPOCH_TABLES:
        table = Base.metadata.tables[table_name]
        values = {}
        for column in table.columns:
            if column.autoincrement is True:
                continue
            values[column.name] = _epoch_column_value(
                column,
                namespace=namespace,
                board_id=subject["board_id"],
                subject_id=subject["ideation_id"],
                quality_receipt_id=subject["quality_receipt_id"],
            )
        await session.execute(insert(table).values(**values))
    await session.flush()


async def _seed_unbound_board_rdl_outbox(
    session: AsyncSession,
    *,
    namespace: str,
    board_id: str,
) -> None:
    """Seed a board-owned outbox row missing its idempotency binding.

    Erasure must use the canonical ``partition_key == board_id`` scope instead
    of deriving coverage only from healthy idempotency rows.
    """

    event_id = f"rdl-orphan-event-{namespace}"
    session.add(
        DomainEventRow(
            id=event_id,
            event_type="research_decision.appended",
            board_id=board_id,
            actor_id=f"agent-{namespace}",
            actor_type="agent",
            payload_json={"fixture": "unbound-rdl-outbox"},
            occurred_at=NOW,
        )
    )
    await session.flush()
    session.add(
        ResearchDecisionOutboxRow(
            id=f"rdl-orphan-outbox-{namespace}",
            event_id=event_id,
            event_type="research_decision.appended",
            partition_key=board_id,
            payload_json={"fixture": "unbound-rdl-outbox"},
            status="pending",
            available_at=NOW,
        )
    )
    await session.flush()


async def _count_subject(
    session: AsyncSession,
    model,
    *,
    board_id: str,
    refinement_id: str,
) -> int:
    table = model.__table__
    if model is DomainEventRow:
        predicates = (
            table.c.board_id == board_id,
            table.c.event_type.in_(RDL_EVENT_TYPES),
        )
    elif "refinement_id" in table.c:
        predicates = (table.c.refinement_id == refinement_id,)
    elif "source_refinement_id" in table.c:
        predicates = (table.c.source_refinement_id == refinement_id,)
    elif "partition_key" in table.c:
        predicates = (table.c.partition_key == board_id,)
    else:
        predicates = (table.c.board_id == board_id,)
    return int(
        await session.scalar(
            select(func.count()).select_from(table).where(*predicates)
        )
        or 0
    )


async def _count_board_table(
    session: AsyncSession,
    table_name: str,
    *,
    board_id: str,
) -> int:
    table = Base.metadata.tables[table_name]
    if table_name == ChecklistItemResultRow.__tablename__:
        receipts = ChecklistReceiptRow.__table__
        return int(
            await session.scalar(
                select(func.count())
                .select_from(
                    table.join(
                        receipts,
                        table.c.receipt_id == receipts.c.id,
                    )
                )
                .where(receipts.c.board_id == board_id)
            )
            or 0
        )
    scope_column = (
        table.c.board_id
        if "board_id" in table.c
        else table.c.partition_key
    )
    return int(
        await session.scalar(
            select(func.count())
            .select_from(table)
            .where(scope_column == board_id)
        )
        or 0
    )


def _delete_index(statements: list[str], table_name: str) -> int:
    marker = f"delete from {table_name}"
    return next(
        index for index, statement in enumerate(statements) if marker in statement
    )


async def test_delete_refinement_purges_complete_rdl_slice_before_parents(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, factory = database
    async with factory() as session:
        subject = await _seed_subject(
            session,
            namespace="subject",
            board_id="board-subject",
        )
        session.add(
            ActivityLog(
                id="quality-history-other-subject",
                board_id=subject["board_id"],
                action="quality_assessment_lifecycle",
                actor_type="agent",
                actor_id="agent-other",
                actor_name="Other agent",
                details={
                    "subject_type": "refinement",
                    "subject_id": "refinement-other",
                },
                created_at=NOW,
            )
        )
        await session.commit()

    async def _governed_delete_stub(
        _context,
        *,
        board_id: str,
        artifact_type: str,
        artifact_id: str,
        occurred_at=None,
    ) -> GovernedArtifactDeletionReceipt:
        del occurred_at
        return GovernedArtifactDeletionReceipt(
            board_id=board_id,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            delete_event_id=f"delete-{artifact_type}-{artifact_id}",
            generation=1,
            reconcile_intent_id=f"intent-{artifact_type}-{artifact_id}",
            delivery_key=f"delivery-{artifact_type}-{artifact_id}",
        )

    monkeypatch.setattr(
        main_services,
        "_prepare_governed_artifact_deletion",
        _governed_delete_stub,
    )
    register_application_persistence_port(
        CommunitySqlAlchemyApplicationPersistence()
    )
    register_relational_application_adapter(
        CommunityRelationalApplicationAdapter()
    )
    register_domain_event_publisher(
        CommunitySqlAlchemyDomainEventPublisher()
    )
    delete_statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture_delete(_conn, _cursor, statement, _params, _context, _many):
        normalized = " ".join(str(statement).lower().split())
        if normalized.startswith("delete from"):
            delete_statements.append(normalized)

    async with factory() as session:
        uow = CommunityUnitOfWork(
            session,
            application_persistence=CommunitySqlAlchemyApplicationPersistence(),
        )
        await DeleteRefinementUseCase().execute(
            DeleteRefinementCommand(subject["refinement_id"]),
            actor=ActorContext(
                "agent-subject",
                "mcp",
                board_id=subject["board_id"],
            ),
            uow=uow,
        )

    async with factory() as session:
        assert await session.get(Refinement, subject["refinement_id"]) is None
        assert await session.get(Spec, subject["spec_id"]) is None
        assert (
            await session.get(ActivityLog, "quality-history-other-subject")
            is not None
        )
        for model in RDL_TABLES:
            assert (
                await _count_subject(
                    session,
                    model,
                    board_id=subject["board_id"],
                    refinement_id=subject["refinement_id"],
                )
                == 0
            ), model.__tablename__
        for model in QUALITY_TABLES:
            assert (
                await _count_board_table(
                    session,
                    model.__tablename__,
                    board_id=subject["board_id"],
                )
                == 0
            ), model.__tablename__
        for model in (
            ChecklistExecutionHeadRow,
            ChecklistItemResultRow,
            ChecklistReceiptRow,
            ChecklistExecutionRow,
        ):
            assert (
                await _count_board_table(
                    session,
                    model.__tablename__,
                    board_id=subject["board_id"],
                )
                == 0
            ), model.__tablename__
        # Binding configuration belongs to the board, not to the deleted Spec.
        for model in (ChecklistBindingHeadRow, ChecklistBindingRow):
            assert (
                await _count_board_table(
                    session,
                    model.__tablename__,
                    board_id=subject["board_id"],
                )
                == 1
            ), model.__tablename__

    # Assert dependency edges, not an arbitrary ordering among independent rows.
    assert _delete_index(
        delete_statements, "research_decision_derivations"
    ) < _delete_index(delete_statements, "specs")
    assert _delete_index(
        delete_statements, "research_decision_idempotency"
    ) < _delete_index(delete_statements, "research_decision_entries")
    assert _delete_index(
        delete_statements, "research_decision_idempotency"
    ) < _delete_index(delete_statements, "research_decision_history")
    assert _delete_index(
        delete_statements, "research_decision_idempotency"
    ) < _delete_index(delete_statements, "research_decision_outbox")
    assert _delete_index(
        delete_statements, "research_decision_heads"
    ) < _delete_index(delete_statements, "research_decision_entries")
    assert _delete_index(
        delete_statements, "research_decision_history"
    ) < _delete_index(delete_statements, "research_decision_entries")
    assert _delete_index(
        delete_statements, "research_decision_derivations"
    ) < _delete_index(delete_statements, "research_decision_snapshots")
    assert _delete_index(
        delete_statements, "research_decision_snapshots"
    ) < _delete_index(delete_statements, "refinements")
    assert _delete_index(
        delete_statements, "research_decision_entries"
    ) < _delete_index(delete_statements, "refinements")


async def test_board_erasure_permit_purges_c7_rows_and_remains_narrow(
    database,
) -> None:
    _engine, factory = database
    async with factory() as session:
        target = await _seed_subject(
            session,
            namespace="target",
            board_id="board-target",
        )
        survivor = await _seed_subject(
            session,
            namespace="survivor",
            board_id="board-survivor",
        )
        await _seed_epoch(session, namespace="target", subject=target)
        await _seed_epoch(session, namespace="survivor", subject=survivor)
        await _seed_unbound_board_rdl_outbox(
            session,
            namespace="target",
            board_id=target["board_id"],
        )
        await _seed_unbound_board_rdl_outbox(
            session,
            namespace="survivor",
            board_id=survivor["board_id"],
        )
        await session.commit()

    store = CommunitySqlAlchemyKGGovernanceStore()
    async with factory() as session:
        await store.purge_board_metadata(
            session,
            board_id=target["board_id"],
        )

        # The inner purge must complete while its transaction-local permit is
        # active; relying on a later Board CASCADE would bypass guarded rows.
        for model in (*RDL_TABLES, *QUALITY_TABLES, *CHECKLIST_TABLES):
            assert (
                await _count_board_table(
                    session,
                    model.__tablename__,
                    board_id=target["board_id"],
                )
                == 0
            ), model.__tablename__
        for table_name in EPOCH_TABLES:
            assert (
                await _count_board_table(
                    session,
                    table_name,
                    board_id=target["board_id"],
                )
                == 0
            ), table_name
        assert (
            await session.scalar(
                select(func.count())
                .select_from(BoardErasurePermit)
                .where(BoardErasurePermit.board_id == target["board_id"])
            )
            == 0
        )

        board = await session.get(Board, target["board_id"])
        assert board is not None
        await session.delete(board)
        await session.commit()

    async with factory() as session:
        assert await session.get(Board, target["board_id"]) is None
        assert await session.get(Board, survivor["board_id"]) is not None
        for model in (*RDL_TABLES, *QUALITY_TABLES, *CHECKLIST_TABLES):
            assert (
                await _count_board_table(
                    session,
                    model.__tablename__,
                    board_id=survivor["board_id"],
                )
                > 0
            ), model.__tablename__
        for table_name in EPOCH_TABLES:
            assert (
                await _count_board_table(
                    session,
                    table_name,
                    board_id=survivor["board_id"],
                )
                > 0
            ), table_name

        # Permit release cannot weaken the survivor's immutable RDL trigger.
        with pytest.raises(
            IntegrityError,
            match="research_decision_entry_immutable",
        ):
            await session.execute(
                delete(ResearchDecisionEntryRow).where(
                    ResearchDecisionEntryRow.id == survivor["rdl_entry_id"]
                )
            )
        await session.rollback()


async def test_delete_board_use_case_purges_c7_epoch_before_source_flush(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real UoW path must purge guarded rows before Board cascades flush."""

    _engine, factory = database
    async with factory() as session:
        target = await _seed_subject(
            session,
            namespace="use-case",
            board_id="board-use-case",
        )
        await _seed_epoch(
            session,
            namespace="use-case",
            subject=target,
        )
        await _seed_unbound_board_rdl_outbox(
            session,
            namespace="use-case",
            board_id=target["board_id"],
        )
        await session.commit()

    register_kg_governance_store(
        CommunitySqlAlchemyKGGovernanceStore()
    )

    class _Lease:
        def ensure_owned(self) -> None:
            return None

    @asynccontextmanager
    async def _board_erasure_scope(
        _operations,
        board_id: str,
        *,
        actor_id: str,
    ):
        assert board_id == target["board_id"]
        assert actor_id == "owner-use-case"
        yield _Lease()

    async def _physical_erasure(
        _operations,
        board_id: str,
        *,
        strict: bool,
        commit: bool,
        global_writer_guarded: bool,
        purge_relational: bool,
    ) -> dict[str, object]:
        assert board_id == target["board_id"]
        assert strict is True
        assert commit is False
        assert global_writer_guarded is True
        assert purge_relational is False
        return {"board_id": board_id, "verified_absent": True}

    monkeypatch.setattr(
        CoreKnowledgeGraphOperations,
        "board_erasure_scope",
        _board_erasure_scope,
    )
    monkeypatch.setattr(
        CoreKnowledgeGraphOperations,
        "right_to_erasure",
        _physical_erasure,
    )

    application_persistence = CommunitySqlAlchemyApplicationPersistence()
    register_application_persistence_port(application_persistence)
    async with factory() as session:
        uow = CommunityUnitOfWork(
            session,
            application_persistence=application_persistence,
        )
        await DeleteBoardUseCase().execute(
            DeleteBoardCommand(target["board_id"]),
            actor=ActorContext(
                "owner-use-case",
                "rest",
                board_id=target["board_id"],
                realm_scope=RealmScope.local(),
            ),
            uow=uow,
        )

    async with factory() as session:
        assert await session.get(Board, target["board_id"]) is None
        assert await session.get(BoardErasurePermit, target["board_id"]) is None
        assert await session.get(BoardErasureJob, target["board_id"]) is None
        for model in (*RDL_TABLES, *QUALITY_TABLES, *CHECKLIST_TABLES):
            assert (
                await _count_board_table(
                    session,
                    model.__tablename__,
                    board_id=target["board_id"],
                )
                == 0
            ), model.__tablename__
        for table_name in EPOCH_TABLES:
            assert (
                await _count_board_table(
                    session,
                    table_name,
                    board_id=target["board_id"],
                )
                == 0
            ), table_name


async def test_direct_rdl_delete_without_permit_stays_blocked(database) -> None:
    _engine, factory = database
    async with factory() as session:
        subject = await _seed_subject(
            session,
            namespace="direct",
            board_id="board-direct",
        )
        await session.commit()

        with pytest.raises(
            IntegrityError,
            match="research_decision_entry_immutable",
        ):
            await session.execute(
                delete(ResearchDecisionEntryRow).where(
                    ResearchDecisionEntryRow.id == subject["rdl_entry_id"]
                )
            )
        await session.rollback()

        assert (
            await session.scalar(
                select(func.count())
                .select_from(ResearchDecisionEntryRow)
                .where(
                    ResearchDecisionEntryRow.id == subject["rdl_entry_id"]
                )
            )
            == 1
        )


async def test_refinement_purge_residual_audit_counts_derivations(database) -> None:
    """The postcondition must not mistake ``source_refinement_id`` for a
    conventional ``refinement_id`` and silently report zero derivations."""

    _engine, factory = database
    async with factory() as session:
        subject = await _seed_subject(
            session,
            namespace="derivation-residual",
            board_id="board-derivation-residual",
        )
        await session.commit()

    async with factory() as session:
        plan = QualityAssessmentLifecycleService().prepare_subject_purge(
            board_id=subject["board_id"],
            subject_type=AssessmentSubjectType.REFINEMENT,
            subject_id=subject["refinement_id"],
        )
        adapter = CommunitySqlAlchemyQualityAssessmentLifecycle(session)
        context = await adapter._prepare_context(plan)  # noqa: SLF001

        assert (
            await adapter._residual_count(  # noqa: SLF001
                AssessmentPurgeResource.RESEARCH_DERIVATIONS,
                plan,
                context,
            )
            == 1
        )


async def test_archive_restore_reconciles_quality_lifecycle_in_same_uow(
    database,
) -> None:
    _engine, factory = database
    async with factory() as session:
        subject = await _seed_subject(
            session,
            namespace="lifecycle",
            board_id="board-lifecycle",
        )
        refinement = await session.get(
            Refinement,
            subject["refinement_id"],
        )
        assert refinement is not None
        refinement.status = RefinementStatus.DONE
        refinement.version = 2
        await session.commit()

    register_application_persistence_port(
        CommunitySqlAlchemyApplicationPersistence()
    )
    register_relational_application_adapter(
        CommunityRelationalApplicationAdapter()
    )
    register_domain_event_publisher(
        CommunitySqlAlchemyDomainEventPublisher()
    )
    async with factory() as session:
        session.info["realm_scope"] = RealmScope.local()
        counts = await ArchiveService(session).archive_tree(
            "refinement",
            subject["refinement_id"],
        )
        assert counts["refinements"] == 1
        assert counts["specs"] == 1
        await session.commit()

    async with factory() as session:
        transitions = list(
            (
                await session.execute(
                    select(QualityAssessmentLifecycleTransitionRow)
                    .where(
                        QualityAssessmentLifecycleTransitionRow.board_id
                        == subject["board_id"],
                        QualityAssessmentLifecycleTransitionRow.action
                        == "archive",
                    )
                    .order_by(
                        QualityAssessmentLifecycleTransitionRow.subject_type
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {
            (row.subject_type, row.subject_id) for row in transitions
        } == {
            ("refinement", subject["refinement_id"]),
            ("spec", subject["spec_id"]),
        }
        head = await session.get(
            QualityAssessmentHeadRow,
            (
                subject["board_id"],
                "refinement",
                subject["refinement_id"],
                "ambiguity",
            ),
        )
        assert head is not None
        assert head.receipt_id == subject["quality_receipt_id"]

    async with factory() as session:
        session.info["realm_scope"] = RealmScope.local()
        counts = await ArchiveService(session).restore_tree(
            "refinement",
            subject["refinement_id"],
        )
        assert counts["refinements"] == 1
        assert counts["specs"] == 1
        await session.commit()

    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(QualityAssessmentLifecycleTransitionRow)
                .where(
                    QualityAssessmentLifecycleTransitionRow.board_id
                    == subject["board_id"],
                    QualityAssessmentLifecycleTransitionRow.action
                    == "restore",
                )
            )
            == 2
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(
                    QualityAssessmentLifecycleStaleTransitionRow
                )
                .where(
                    QualityAssessmentLifecycleStaleTransitionRow.board_id
                    == subject["board_id"]
                )
            )
            == 0
        )
