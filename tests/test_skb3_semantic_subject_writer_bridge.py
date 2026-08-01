"""Transactional semantic-subject attribution through real Community writers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import okto_pulse.community.app as _community_app  # noqa: F401
from okto_pulse.community.adapters.relational_schema_steps import (
    semantic_guideline_sqlite_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_architecture_persistence import (
    CommunitySqlAlchemyArchitecturePersistence,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    Base,
    Board,
    Card,
    Guideline,
    GuidelineBoardBindingRow,
    GuidelineRevisionRow,
    Ideation,
    Refinement,
    SemanticGuidelineBindingConfigurationRow,
    SemanticGuidelineRevisionRow,
    SemanticSubjectVersionEventRow,
    SemanticSubjectVersionRow,
    Spec,
    Sprint,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    install_policy_subject_versioning,
)
from okto_pulse.community.adapters.sqlalchemy_research_decision_ledger import (
    CommunitySqlAlchemyResearchDecisionLedger,
)
from okto_pulse.community.adapters.sqlalchemy_application_persistence import (
    CommunitySqlAlchemyApplicationPersistence,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
    CommunityUnitOfWorkFactory,
)
from okto_pulse.community.api.auth_deps import require_principal
from okto_pulse.community.api.deps import get_unit_of_work
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    GuidelineBindingProvenance,
    GuidelineBindingState,
    GuidelineEnforcement,
    GuidelineMetric,
    GuidelineMetricDirection,
    GuidelineRevision,
    PolicyEntityType,
)
from okto_pulse.core.domain.guideline_semantic_assessment import (
    SemanticAssessmentAssessor,
    SemanticAssessmentState,
    SemanticGuidelineAssessmentContext,
    SemanticGuidelineAssessmentSubmission,
    SemanticMetricAssessment,
    record_semantic_guideline_assessment,
)
from okto_pulse.core.domain.enums import RefinementStatus
from okto_pulse.core.domain.quality_assessment import (
    EvidenceRef,
    FindingAnchorType,
    UnboundFindingAnchor,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.domain.research_decision_ledger import (
    RefinementLedgerContext,
    ResearchDecisionAnchor,
    ResearchDecisionAnchorType,
    ResearchDecisionContent,
    ResearchDecisionStatus,
)
from okto_pulse.core.ports.authentication import Principal
from okto_pulse.core.ports.application_persistence import ApplicationRecord
from okto_pulse.core.ports.architecture_persistence import (
    register_architecture_persistence_port,
)
from okto_pulse.core.services.research_decision_ledger import (
    AppendResearchDecisionCommand,
    ResearchDecisionLedgerService,
)


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class _Seed:
    board_id: str
    ideation_id: str
    refinement_id: str
    spec_id: str
    sprint_id: str
    card_id: str
    scenario_id: str
    design_id: str


def _sqlite_engine(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


async def _database(path, *, semantic_triggers: bool = False):
    install_policy_subject_versioning()
    engine = _sqlite_engine(path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if semantic_triggers:
            for _name, (_table, ddl) in (
                semantic_guideline_sqlite_trigger_manifest().items()
            ):
                await connection.execute(text(ddl))
    return engine, async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _seed_subjects(session: AsyncSession) -> _Seed:
    seed = _Seed(
        board_id=_id(),
        ideation_id=_id(),
        refinement_id=_id(),
        spec_id=_id(),
        sprint_id=_id(),
        card_id=_id(),
        scenario_id=_id(),
        design_id=_id(),
    )
    session.add(
        Board(
            id=seed.board_id,
            name="Semantic writer bridge",
            owner_id="owner",
            realm_id="local",
        )
    )
    await session.flush()
    session.add(
        Ideation(
            id=seed.ideation_id,
            board_id=seed.board_id,
            title="Hexagonal capability",
            description="Initial ideation",
            status="draft",
            version=1,
            created_by="seed",
        )
    )
    await session.flush()
    session.add(
        Refinement(
            id=seed.refinement_id,
            board_id=seed.board_id,
            ideation_id=seed.ideation_id,
            title="Boundary analysis",
            description="Initial refinement",
            status="draft",
            version=1,
            created_by="seed",
        )
    )
    await session.flush()
    session.add(
        Spec(
            id=seed.spec_id,
            board_id=seed.board_id,
            ideation_id=seed.ideation_id,
            refinement_id=seed.refinement_id,
            title="Ports and adapters",
            description="Initial spec",
            context="Initial context",
            test_scenarios=[
                {
                    "id": seed.scenario_id,
                    "title": "Persist through a port",
                    "scenario_type": "positive",
                    "given": "an application use case",
                    "when": "it changes state",
                    "then": "the port owns persistence",
                }
            ],
            status="draft",
            version=1,
            test_scenario_policy_epoch=1,
            created_by="seed",
        )
    )
    await session.flush()
    session.add(
        Sprint(
            id=seed.sprint_id,
            board_id=seed.board_id,
            spec_id=seed.spec_id,
            title="Semantic delivery",
            description="Initial sprint",
            spec_version=1,
            status="draft",
            version=1,
            created_by="seed",
        )
    )
    await session.flush()
    session.add(
        Card(
            id=seed.card_id,
            board_id=seed.board_id,
            spec_id=seed.spec_id,
            sprint_id=seed.sprint_id,
            title="Implement port",
            description="Initial card",
            status="not_started",
            policy_version=1,
            created_by="seed",
        )
    )
    session.add(
        ArchitectureDesign(
            id=seed.design_id,
            board_id=seed.board_id,
            parent_type="ideation",
            ideation_id=seed.ideation_id,
            title="Context diagram",
            global_description="Initial architecture",
            entities=[],
            interfaces=[],
            diagrams=[
                {
                    "id": "diagram-1",
                    "title": "Context",
                    "format": "raw",
                }
            ],
            version=1,
            created_by="seed",
        )
    )
    await session.flush()
    return seed


async def _seed_authority(
    session: AsyncSession,
    *,
    board_id: str,
) -> tuple[GuidelineRevision, BoardGuidelineBinding]:
    guideline_id = _id()
    revision_id = _id()
    binding_id = _id()
    timestamp = _now()
    metric = GuidelineMetric(
        metric_id="metric-segregation",
        code="segregation",
        title="Segregation",
        description="Business rules remain independent from adapters.",
        evaluation_rubric="Score semantic segregation from 0 to 100.",
        target_entity_types=(
            PolicyEntityType.IDEATION,
            PolicyEntityType.REFINEMENT,
            PolicyEntityType.SPEC,
            PolicyEntityType.SPRINT,
            PolicyEntityType.CARD,
            PolicyEntityType.TEST_SCENARIO,
        ),
        direction=GuidelineMetricDirection.MINIMUM,
        default_threshold=70,
    )
    revision = GuidelineRevision(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=1,
        semantic_version="1.0.0",
        title="Hexagonal architecture",
        content="Keep business capabilities independent from technology.",
        metrics=(metric,),
        created_by="guideline-author",
        created_at=timestamp,
    )
    source_digest = canonical_sha256(
        {
            "contract": "semantic-writer-bridge-source/v1",
            "revision_id": revision_id,
        }
    )
    binding = BoardGuidelineBinding(
        binding_id=binding_id,
        board_id=board_id,
        guideline_id=guideline_id,
        revision_id=revision_id,
        semantic_version="1.0.0",
        revision_digest=revision.revision_digest,
        priority=0,
        binding_revision=1,
        adopted_by="owner",
        adopted_at=timestamp,
        enforcement=GuidelineEnforcement.BLOCKING,
        minimum_confidence=80,
        metric_threshold_overrides={},
        state=GuidelineBindingState.ACTIVE,
        source_kind=GuidelineBindingProvenance.NATIVE,
    )
    session.add(
        Guideline(
            id=guideline_id,
            title=revision.title,
            content=revision.content,
            tags=[],
            scope="global",
            board_id=None,
            owner_id="guideline-author",
            version=1,
        )
    )
    await session.flush()
    session.add(
        GuidelineRevisionRow(
            revision_id=revision_id,
            guideline_id=guideline_id,
            revision_number=1,
            semantic_version="1.0.0",
            title=revision.title,
            content=revision.content,
            content_digest=source_digest,
            tags=[],
            rules=[],
            created_by="guideline-author",
            created_at=timestamp,
            published_head_revision=1,
            published_head_updated_at=timestamp,
            parent_revision_id=None,
            legacy_version=None,
            legacy_version_unresolvable=False,
            legacy_tags=None,
            idempotency_key=None,
            request_digest=None,
            legacy_version_text=None,
        )
    )
    await session.flush()
    session.add_all(
        [
            GuidelineBoardBindingRow(
                binding_id=binding_id,
                binding_revision=1,
                board_id=board_id,
                guideline_id=guideline_id,
                revision_id=revision_id,
                semantic_version="1.0.0",
                revision_digest=source_digest,
                priority=0,
                adopted_by="owner",
                adopted_at=timestamp,
                enforcement="blocking",
                source_kind="native",
                binding_origin="native",
                state="active",
            ),
            SemanticGuidelineRevisionRow(
                revision_id=revision_id,
                guideline_id=guideline_id,
                metrics=[metric.digest_payload()],
                revision_digest=revision.revision_digest,
                source_revision_digest=source_digest,
                authority_state="native",
                legacy_rules_digest=None,
                created_by="guideline-author",
                created_at=timestamp,
            ),
        ]
    )
    await session.flush()
    session.add(
        SemanticGuidelineBindingConfigurationRow(
            binding_id=binding_id,
            binding_revision=1,
            board_id=board_id,
            guideline_id=guideline_id,
            revision_id=revision_id,
            revision_digest=revision.revision_digest,
            enforcement="blocking",
            minimum_confidence=80,
            metric_threshold_overrides={},
            configuration_digest=binding.configuration_digest,
            configured_by="owner",
            configured_at=timestamp,
        )
    )
    await session.flush()
    return revision, binding


async def _assert_blocking_assessment_can_be_saved(
    session: AsyncSession,
    *,
    seed: _Seed,
    entity_type: PolicyEntityType,
    subject_id: str,
    revision: GuidelineRevision,
    binding: BoardGuidelineBinding,
    expected_editor: str,
) -> None:
    adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
    snapshot = await adapter.resolve_policy_subject_snapshot(
        board_id=seed.board_id,
        entity_type=entity_type,
        subject_id=subject_id,
    )
    assert snapshot is not None
    assert snapshot.last_semantic_editor_id == expected_editor
    policy_set_digest, binding_head_digest = (
        await adapter.semantic_current_fences(board_id=seed.board_id)
    )
    context = SemanticGuidelineAssessmentContext(
        subject_snapshot=snapshot,
        binding=binding,
        revision=revision,
        policy_set_digest=policy_set_digest,
        binding_head_digest=binding_head_digest,
    )
    submission = SemanticGuidelineAssessmentSubmission(
        subject=snapshot.subject,
        binding_id=binding.binding_id,
        expected_binding_revision=binding.binding_revision,
        guideline_revision_id=revision.revision_id,
        idempotency_key=f"assessment-{entity_type.value}-{subject_id}",
        confidence=95,
        assessor=SemanticAssessmentAssessor(
            agent_id="independent-reviewer",
            model_id="test-model",
        ),
        metric_results=(
            SemanticMetricAssessment(
                metric_id=revision.metrics[0].metric_id,
                score=90,
                rationale="The authored evidence demonstrates segregation.",
                evidence_refs=(
                    EvidenceRef(
                        source_type=entity_type.value,
                        source_id=subject_id,
                        source_version=snapshot.subject.subject_version,
                        content_hash=snapshot.content_digest,
                    ),
                ),
                pinpoints=(
                    UnboundFindingAnchor(
                        anchor_type=FindingAnchorType.WHOLE_ARTIFACT,
                    ),
                ),
            ),
        ),
    )
    result = record_semantic_guideline_assessment(
        submission,
        context,
        receipt_id=_id(),
        recorded_at=_now(),
    )
    saved = await adapter.save_semantic_assessment_result(
        result=result,
        request_digest=result.request_digest,
    )
    assert saved.receipt.state is SemanticAssessmentState.PASSED


@pytest.mark.asyncio
async def test_factory_writers_bridge_all_six_subjects_and_assessment(
    tmp_path,
):
    engine, sessions = await _database(
        tmp_path / "semantic-writers.db",
        semantic_triggers=True,
    )
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)
        revision, binding = await _seed_authority(
            session,
            board_id=seed.board_id,
        )

    factory = CommunityUnitOfWorkFactory(sessions)
    writer = ActorContext("mcp-writer", "mcp", board_id=seed.board_id)
    mutations = (
        (PolicyEntityType.IDEATION, seed.ideation_id, "ideation", "description", "Changed ideation"),
        (PolicyEntityType.REFINEMENT, seed.refinement_id, "refinement", "analysis", "Changed refinement"),
        (PolicyEntityType.SPEC, seed.spec_id, "spec", "context", "Changed spec"),
        (PolicyEntityType.SPRINT, seed.sprint_id, "sprint", "objective", "Changed sprint"),
        (PolicyEntityType.CARD, seed.card_id, "card", "details", "Changed card"),
    )
    for _entity_type, subject_id, entity, field, value in mutations:
        async with factory(actor=writer) as uow:
            record = await uow.services.get_application_record(
                entity=entity,
                record_id=subject_id,
            )
            assert record is not None
            setattr(record, field, value)
            await uow.commit()

    async with factory(actor=writer) as uow:
        spec = await uow.services.get_application_record(
            entity="spec",
            record_id=seed.spec_id,
        )
        assert spec is not None
        scenarios = [dict(value) for value in spec.test_scenarios]
        scenarios[0]["then"] = "the transaction also seals editor evidence"
        spec.test_scenarios = scenarios
        await uow.commit()

    async with sessions() as session, session.begin():
        for entity_type, subject_id in (
            (PolicyEntityType.IDEATION, seed.ideation_id),
            (PolicyEntityType.REFINEMENT, seed.refinement_id),
            (PolicyEntityType.SPEC, seed.spec_id),
            (PolicyEntityType.SPRINT, seed.sprint_id),
            (PolicyEntityType.CARD, seed.card_id),
            (PolicyEntityType.TEST_SCENARIO, seed.scenario_id),
        ):
            await _assert_blocking_assessment_can_be_saved(
                session,
                seed=seed,
                entity_type=entity_type,
                subject_id=subject_id,
                revision=revision,
                binding=binding,
                expected_editor=writer.actor_id,
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_sprint_lane_and_origin_fields_change_digest_and_head(tmp_path):
    engine, sessions = await _database(tmp_path / "sprint-lane-origin.db")
    origin_sprint_id = _id()
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)
        session.add(
            Sprint(
                id=origin_sprint_id,
                board_id=seed.board_id,
                spec_id=seed.spec_id,
                title="Origin sprint",
                spec_version=1,
                status="closed",
                lane_type="normal",
                version=1,
                created_by="seed",
            )
        )
        await session.flush()
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        baseline = await adapter.resolve_policy_subject_snapshot(
            board_id=seed.board_id,
            entity_type=PolicyEntityType.SPRINT,
            subject_id=seed.sprint_id,
        )
        assert baseline is not None

    factory = CommunityUnitOfWorkFactory(sessions)
    actor = ActorContext("hotfix-author", "mcp", board_id=seed.board_id)
    async with factory(actor=actor) as uow:
        sprint = await uow.services.get_application_record(
            entity="sprint",
            record_id=seed.sprint_id,
        )
        assert sprint is not None
        sprint.lane_type = "hotfix"
        sprint.origin_sprint_id = origin_sprint_id
        sprint.origin_bug_id = seed.card_id
        await uow.commit()

    async with sessions() as session:
        current = await CommunitySqlAlchemySemanticGuidelineAssessment(
            session
        ).resolve_policy_subject_snapshot(
            board_id=seed.board_id,
            entity_type=PolicyEntityType.SPRINT,
            subject_id=seed.sprint_id,
        )
        assert current is not None
        assert current.content_digest != baseline.content_digest
        assert current.last_semantic_editor_id == actor.actor_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_subject_mutation_without_authenticated_actor_rolls_back(tmp_path):
    engine, sessions = await _database(tmp_path / "semantic-no-actor.db")
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)

    factory = CommunityUnitOfWorkFactory(sessions)
    with pytest.raises(
        RuntimeError,
        match="^semantic_subject_mutation_actor_required$",
    ):
        async with factory() as uow:
            record = await uow.services.get_application_record(
                entity="ideation",
                record_id=seed.ideation_id,
            )
            assert record is not None
            record.description = "must roll back"
            await uow.commit()

    async with sessions() as session:
        ideation = await session.get(Ideation, seed.ideation_id)
        assert ideation is not None
        assert ideation.description == "Initial ideation"
        assert await session.scalar(
            select(func.count())
            .select_from(SemanticSubjectVersionRow)
            .where(SemanticSubjectVersionRow.board_id == seed.board_id)
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(SemanticSubjectVersionEventRow)
            .where(SemanticSubjectVersionEventRow.board_id == seed.board_id)
        ) == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_savepoint_markers_rollback_merge_and_deduplicate_flushes(
    tmp_path,
):
    engine, sessions = await _database(tmp_path / "semantic-savepoints.db")
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)
        ideation = await session.get(Ideation, seed.ideation_id)
        assert ideation is not None
        baseline_version = int(ideation.version)

    factory = CommunityUnitOfWorkFactory(sessions)
    actor = ActorContext("savepoint-writer", "mcp", board_id=seed.board_id)
    async with factory(actor=actor) as uow:
        sync_session = uow._session.sync_session
        rolled_back = await uow.services.get_application_record(
            entity="ideation",
            record_id=seed.ideation_id,
        )
        assert rolled_back is not None
        with pytest.raises(RuntimeError, match="^rollback-child$"):
            async with uow._session.begin_nested():
                rolled_back.problem_statement = "must not persist"
                await uow.synchronize()
                raise RuntimeError("rollback-child")

        committed = await uow.services.get_application_record(
            entity="ideation",
            record_id=seed.ideation_id,
        )
        assert committed is not None
        async with uow._session.begin_nested():
            committed.proposed_approach = "committed savepoint"
            await uow.synchronize()

        # A second effective flush in the outer UoW must update content without
        # advancing the subject fence or appending a second editor event.
        committed.description = "second flush in the same UoW"
        await uow.synchronize()
        await uow.commit()
        assert not sync_session.info.get("semantic_subject_bridge_pending")

    assert not any(
        key.startswith("semantic_subject_bridge")
        for key in sync_session.info
    )
    async with sessions() as session:
        ideation = await session.get(Ideation, seed.ideation_id)
        assert ideation is not None
        assert ideation.problem_statement is None
        assert ideation.proposed_approach == "committed savepoint"
        assert ideation.description == "second flush in the same UoW"
        assert ideation.version == baseline_version + 1
        events = tuple(
            (
                await session.scalars(
                    select(SemanticSubjectVersionEventRow).where(
                        SemanticSubjectVersionEventRow.board_id
                        == seed.board_id,
                        SemanticSubjectVersionEventRow.subject_type
                        == PolicyEntityType.IDEATION.value,
                        SemanticSubjectVersionEventRow.subject_id
                        == seed.ideation_id,
                    )
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].last_semantic_editor_id == actor.actor_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_materialization_failure_rolls_back_writer_and_prior_event(
    tmp_path,
    monkeypatch,
):
    engine, sessions = await _database(tmp_path / "semantic-atomicity.db")
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)

    original = (
        CommunitySqlAlchemySemanticGuidelineAssessment
        .record_semantic_subject_mutation
    )
    calls = 0

    async def fail_second_materialization(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected-materialization-failure")
        return await original(self, **kwargs)

    monkeypatch.setattr(
        CommunitySqlAlchemySemanticGuidelineAssessment,
        "record_semantic_subject_mutation",
        fail_second_materialization,
    )
    factory = CommunityUnitOfWorkFactory(sessions)
    actor = ActorContext("atomic-writer", "mcp", board_id=seed.board_id)
    with pytest.raises(
        RuntimeError,
        match="^injected-materialization-failure$",
    ):
        async with factory(actor=actor) as uow:
            ideation = await uow.services.get_application_record(
                entity="ideation",
                record_id=seed.ideation_id,
            )
            refinement = await uow.services.get_application_record(
                entity="refinement",
                record_id=seed.refinement_id,
            )
            assert ideation is not None and refinement is not None
            ideation.description = "must roll back"
            refinement.analysis = "must also roll back"
            await uow.commit()

    async with sessions() as session:
        ideation = await session.get(Ideation, seed.ideation_id)
        refinement = await session.get(Refinement, seed.refinement_id)
        assert ideation is not None and refinement is not None
        assert ideation.description == "Initial ideation"
        assert refinement.analysis is None
        assert await session.scalar(
            select(func.count())
            .select_from(SemanticSubjectVersionRow)
            .where(SemanticSubjectVersionRow.board_id == seed.board_id)
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(SemanticSubjectVersionEventRow)
            .where(SemanticSubjectVersionEventRow.board_id == seed.board_id)
        ) == 0

    await engine.dispose()


@pytest.mark.asyncio
async def test_research_decision_bulk_version_writer_queues_refinement_head(
    tmp_path,
):
    engine, sessions = await _database(tmp_path / "rdl-bulk-writer.db")
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)
        refinement = await session.get(Refinement, seed.refinement_id)
        assert refinement is not None
        baseline_version = int(refinement.version)

    sequence = 0

    def next_id(prefix: str) -> str:
        nonlocal sequence
        sequence += 1
        return f"{prefix}-bridge-{sequence}"

    actor = ActorContext("rdl-writer", "mcp", board_id=seed.board_id)
    service = ResearchDecisionLedgerService(
        id_factory=next_id,
        clock=_now,
    )
    bundle = service.prepare_append(
        AppendResearchDecisionCommand(
            board_id=seed.board_id,
            refinement_id=seed.refinement_id,
            expected_refinement_version=baseline_version,
            content=ResearchDecisionContent(
                unknown="Which boundary owns retries?",
                status=ResearchDecisionStatus.OPEN,
                anchor=ResearchDecisionAnchor(
                    anchor_type=(
                        ResearchDecisionAnchorType.FUNCTIONAL_REQUIREMENT
                    ),
                    anchor_ref="fr_retry",
                ),
                alternatives=("domain policy", "adapter policy"),
            ),
            actor_id=actor.actor_id,
            idempotency_key="rdl-bridge-1",
        ),
        context=RefinementLedgerContext(
            board_id=seed.board_id,
            refinement_id=seed.refinement_id,
            version=baseline_version,
            status=RefinementStatus.DRAFT,
            archived=False,
        ),
    )
    factory = CommunityUnitOfWorkFactory(sessions)
    async with factory(actor=actor) as uow:
        result = await CommunitySqlAlchemyResearchDecisionLedger(
            uow._session
        ).apply_bundle_cas(bundle)
        await uow.commit()
    assert result.refinement_version == baseline_version + 1

    async with sessions() as session:
        head = await session.get(
            SemanticSubjectVersionRow,
            {
                "board_id": seed.board_id,
                "subject_type": PolicyEntityType.REFINEMENT.value,
                "subject_id": seed.refinement_id,
            },
        )
        assert head is not None
        assert head.subject_version == baseline_version + 1
        assert head.last_semantic_editor_id == actor.actor_id

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_type", "subject_attr", "qa_entity", "owner_field"),
    (
        (
            PolicyEntityType.IDEATION,
            "ideation_id",
            "ideation_qa_item",
            "ideation_id",
        ),
        (
            PolicyEntityType.REFINEMENT,
            "refinement_id",
            "refinement_qa_item",
            "refinement_id",
        ),
        (
            PolicyEntityType.SPEC,
            "spec_id",
            "spec_qa_item",
            "spec_id",
        ),
        (
            PolicyEntityType.CARD,
            "card_id",
            "qa_item",
            "card_id",
        ),
        (
            PolicyEntityType.SPRINT,
            "sprint_id",
            "sprint_qa_item",
            "sprint_id",
        ),
    ),
)
async def test_q_and_a_writer_updates_owner_head(
    tmp_path,
    entity_type,
    subject_attr,
    qa_entity,
    owner_field,
):
    engine, sessions = await _database(
        tmp_path / f"qa-{entity_type.value}.db"
    )
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)

    actor = ActorContext("qa-writer", "mcp", board_id=seed.board_id)
    subject_id = getattr(seed, subject_attr)
    model = {
        PolicyEntityType.IDEATION: Ideation,
        PolicyEntityType.REFINEMENT: Refinement,
        PolicyEntityType.SPEC: Spec,
        PolicyEntityType.SPRINT: Sprint,
        PolicyEntityType.CARD: Card,
    }[entity_type]
    async with sessions() as session:
        owner = await session.get(model, subject_id)
        assert owner is not None
        baseline_version = int(
            owner.policy_version
            if entity_type is PolicyEntityType.CARD
            else owner.version
        )
    persistence = CommunitySqlAlchemyApplicationPersistence()
    async with sessions() as session:
        uow = CommunityUnitOfWork(
            session,
            actor=actor,
            application_persistence=persistence,
        )
        async with uow:
            await persistence.add(
                session,
                ApplicationRecord(
                    entity=qa_entity,
                    values={
                        "id": _id(),
                        owner_field: subject_id,
                        "question": "Why?",
                        "question_type": "text",
                        "allow_free_text": False,
                        "asked_by": actor.actor_id,
                    },
                ),
            )
            await uow.commit()

    async with sessions() as session:
        head = await session.get(
            SemanticSubjectVersionRow,
            {
                "board_id": seed.board_id,
                "subject_type": entity_type.value,
                "subject_id": subject_id,
            },
        )
        assert head is not None
        assert head.last_semantic_editor_id == actor.actor_id
        assert head.subject_version == baseline_version + 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_architecture_payload_writer_updates_parent_head(tmp_path):
    engine, sessions = await _database(tmp_path / "architecture-payload.db")
    register_architecture_persistence_port(
        CommunitySqlAlchemyArchitecturePersistence()
    )
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)
    async with sessions() as session:
        ideation = await session.get(Ideation, seed.ideation_id)
        assert ideation is not None
        baseline_version = int(ideation.version)

    factory = CommunityUnitOfWorkFactory(sessions)
    actor = ActorContext("architect", "mcp", board_id=seed.board_id)
    async with factory(actor=actor) as uow:
        await uow.services.architecture_diagrams.save_payload(
            seed.board_id,
            seed.design_id,
            "diagram-1",
            "raw",
            {"nodes": [{"id": "domain"}]},
        )
        await uow.commit()

    async with sessions() as session:
        head = await session.get(
            SemanticSubjectVersionRow,
            {
                "board_id": seed.board_id,
                "subject_type": PolicyEntityType.IDEATION.value,
                "subject_id": seed.ideation_id,
            },
        )
        assert head is not None
        assert head.last_semantic_editor_id == actor.actor_id
        assert head.subject_version == baseline_version + 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_real_mcp_update_handler_propagates_actor_to_subject_head(
    tmp_path,
    monkeypatch,
):
    from okto_pulse.core.mcp import server

    engine, sessions = await _database(tmp_path / "mcp-handler.db")
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)

    factory = CommunityUnitOfWorkFactory(sessions)

    async def agent_context(_board_id):
        return server.AgentContext(
            agent_id="mcp-handler-writer",
            agent_name="Writer",
            board_id=seed.board_id,
            permissions=[server.Permissions.SPECS_UPDATE],
            realm_id="local",
        )

    monkeypatch.setattr(server, "_get_agent_ctx", agent_context)
    monkeypatch.setattr(
        server,
        "get_unit_of_work_factory_for_mcp",
        lambda: factory,
    )
    payload = await server.okto_pulse_update_ideation.fn(
        board_id=seed.board_id,
        ideation_id=seed.ideation_id,
        description="Changed by the registered MCP mutation handler",
    )
    assert '"success": true' in payload.lower()

    async with sessions() as session:
        head = await session.get(
            SemanticSubjectVersionRow,
            {
                "board_id": seed.board_id,
                "subject_type": PolicyEntityType.IDEATION.value,
                "subject_id": seed.ideation_id,
            },
        )
        assert head is not None
        assert head.last_semantic_editor_id == "mcp-handler-writer"

    await engine.dispose()


@pytest.mark.asyncio
async def test_rest_dependency_propagates_authenticated_actor_to_real_writer(
    tmp_path,
):
    engine, sessions = await _database(tmp_path / "rest-writer.db")
    async with sessions() as session, session.begin():
        seed = await _seed_subjects(session)

    factory = CommunityUnitOfWorkFactory(sessions)
    app = FastAPI()
    app.state.runtime_composition = type(
        "Composition",
        (),
        {"uow_factory": factory},
    )()
    app.dependency_overrides[require_principal] = lambda: Principal(
        subject="rest-writer",
        realm_id="local",
    )

    @app.put("/ideations/{ideation_id}")
    async def update_ideation(
        ideation_id: str,
        uow=Depends(get_unit_of_work),
    ):
        record = await uow.services.get_application_record(
            entity="ideation",
            record_id=ideation_id,
        )
        assert record is not None
        record.description = "Changed through REST dependency"
        await uow.commit()
        return {"id": record.id}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.put(f"/ideations/{seed.ideation_id}")
    assert response.status_code == 200

    async with sessions() as session:
        head = await session.get(
            SemanticSubjectVersionRow,
            {
                "board_id": seed.board_id,
                "subject_type": PolicyEntityType.IDEATION.value,
                "subject_id": seed.ideation_id,
            },
        )
        assert head is not None
        assert head.last_semantic_editor_id == "rest-writer"

    await engine.dispose()
