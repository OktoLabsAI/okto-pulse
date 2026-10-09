"""SK-B3 Community semantic guideline native authority tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)

import okto_pulse.community.app as _community_app  # noqa: F401
from okto_pulse.community.adapters.relational_application import (
    CommunityRelationalApplicationAdapter,
)
from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
    initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
    install_community_sqlite_pragmas,
)
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract

from okto_pulse.community.adapters.sqlalchemy_models import (
    ArchitectureDesign,
    ArchitectureDiagramPayload,
    Board,
    DomainEventHandlerExecution,
    DomainEventRow,
    Guideline,
    GuidelineBoardBindingRow,
    GuidelineImpactReceiptRow,
    GuidelineRevisionRow,
    Ideation,
    IdeationKnowledgeBase,
    IdeationQAItem,
    Refinement,
    SemanticGuidelineBindingConfigurationRow,
    SemanticGuidelineAssessmentV2Row,
    SemanticGuidelineFindingV2Row,
    SemanticGuidelineMetricResultV2Row,
    SemanticGuidelineRevisionRow,
    SemanticGuidelineSkipRow,
    SemanticGuidelineWaiverEventRow,
    SemanticGuidelineWaiverRow,
    SemanticSubjectVersionEventRow,
    SemanticSubjectVersionRow,
    Spec,
)
from okto_pulse.community.adapters.semantic_guideline_kg_events import (
    SEMANTIC_GUIDELINE_PROJECTION_HANDLER,
)
from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
    CommunitySqlAlchemyKGGovernanceStore,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import (
    CommunityUnitOfWork,
)
from okto_pulse.community.adapters.sqlalchemy_policy_constraint_projection import (
    CommunitySqlAlchemyPolicyConstraintProjection,
)
from okto_pulse.community.adapters.sqlalchemy_guideline_policy import (
    CommunitySqlAlchemyGuidelinePolicy,
)
from okto_pulse.core.domain.guideline_policy import (
    BoardGuidelineBinding,
    Guideline as DomainGuideline,
    GuidelineEnforcement,
    GuidelineHead,
    GuidelineMetric,
    GuidelineMetricDirection,
    GuidelineRevision,
    GuidelineScope,
    PolicyEntityType,
)
from okto_pulse.core.domain.guideline_impact import (
    GuidelineImpactPreviewCommand,
    impact_fence_from_receipt,
    plan_guideline_adoption,
    plan_guideline_impact_preview,
    plan_guideline_unlink,
)
from okto_pulse.core.domain.guideline_semantic_assessment import (
    SemanticAssessmentAssessor,
    semantic_binding_head_digest_v1,
)
from okto_pulse.core.domain.guideline_semantic_currentness import (
    SemanticAssessmentCurrentnessReason,
)
from okto_pulse.core.domain.guideline_semantic_exceptions import (
    SemanticExceptionActorKind,
    SemanticMetricWaiverAnchor,
    SemanticMetricWaiverEventType,
    SemanticMetricWaiverExpireReason,
    SemanticMetricWaiverRevalidationReason,
    SemanticMetricWaiverRevalidationStatus,
    SemanticPolicySkipScope,
    SemanticPolicySkipStatus,
    create_semantic_policy_skip,
    request_semantic_metric_waiver,
    revalidate_semantic_metric_waiver,
    revoke_semantic_policy_skip,
    transition_semantic_metric_waiver,
)
from okto_pulse.core.domain.guideline_semantic_transition import (
    PolicyTransitionReasonCode,
    PolicyTransitionRejected,
)
from okto_pulse.core.domain.quality_assessment import (
    EvidenceRef,
    FindingAnchorType,
    UnboundFindingAnchor,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyDigestConflict,
    GuidelinePolicyHeadConflict,
    GuidelinePolicyPersistencePort,
    SemanticGuidelineAssessmentPersistencePort,
)
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.policy_governance import (
    ASSESSMENTS_RECORD,
)
from okto_pulse.core.ports.relational_application import (
    register_relational_application_adapter,
    reset_relational_application_adapter_for_tests,
)
from okto_pulse.core.services.main import GuidelineService
from okto_pulse.core.events.types import (
    SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE,
)
from repo_layout import resolve_core_repo


def _id() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_semantic_adapters_satisfy_their_runtime_persistence_ports() -> None:
    session = object()

    assert isinstance(
        CommunitySqlAlchemyGuidelinePolicy(session),  # type: ignore[arg-type]
        GuidelinePolicyPersistencePort,
    )
    assert isinstance(
        CommunitySqlAlchemySemanticGuidelineAssessment(
            session,  # type: ignore[arg-type]
        ),
        SemanticGuidelineAssessmentPersistencePort,
    )


@pytest.fixture
def semantic_relational_application_adapter():
    register_relational_application_adapter(CommunityRelationalApplicationAdapter())
    try:
        yield
    finally:
        reset_relational_application_adapter_for_tests()


def test_semantic_waiver_fences_exact_result_and_finding_pair():
    constraint = next(item for item in SemanticGuidelineWaiverRow.__table__.foreign_key_constraints
                      if item.name == "fk_sg_waiver_native_finding")
    assert tuple(item.parent.name for item in constraint.elements) == (
        "finding_id", "metric_result_id", "receipt_id", "board_id", "subject_type",
        "subject_id", "metric_code", "metric_result_digest", "finding_digest")
    assert {item.column.table.name for item in constraint.elements} == {"semantic_guideline_findings_v2"}


@pytest.mark.asyncio
async def test_semantic_metric_json_guard_matches_core_closed_shape(tmp_path):
    engine = _sqlite_engine(tmp_path / "semantic-metric-json-guard.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    baseline = GuidelineMetric(
        metric_id="segregation",
        code="segregation",
        title="Segregation",
        description="Measure business and infrastructure separation.",
        evaluation_rubric="Score the authored artifact from 0 to 100.",
        target_entity_types=(PolicyEntityType.IDEATION,),
        direction=GuidelineMetricDirection.MINIMUM,
        default_threshold=70,
    ).digest_payload()
    invalid_payloads = []
    reserved = deepcopy(baseline)
    reserved["metric_id"] = "Confidence"
    invalid_payloads.append([reserved])
    invalid_code = deepcopy(baseline)
    invalid_code["code"] = "invalid code"
    invalid_payloads.append([invalid_code])
    duplicate_code = deepcopy(baseline)
    duplicate_code["metric_id"] = "secondary"
    duplicate_code["code"] = "SEGREGATION"
    invalid_payloads.append([baseline, duplicate_code])
    duplicate_target = deepcopy(baseline)
    duplicate_target["target_entity_types"] = ["ideation", "ideation"]
    invalid_payloads.append([duplicate_target])
    unsorted_target = deepcopy(baseline)
    unsorted_target["target_entity_types"] = ["spec", "ideation"]
    invalid_payloads.append([unsorted_target])
    padded_title = deepcopy(baseline)
    padded_title["title"] = " Segregation "
    invalid_payloads.append([padded_title])

    async with factory() as session, session.begin():
        for index, metrics in enumerate(invalid_payloads):
            guideline_id = _id()
            revision_id = _id()
            source_digest = canonical_sha256({"invalid_metric_source": index})
            session.add(
                Guideline(
                    id=guideline_id,
                    title=f"Semantic metrics {index}",
                    content="Evaluate semantic adherence.",
                    tags=[],
                    scope="global",
                    owner_id="owner",
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
                    title=f"Invalid metric {index}",
                    content="Invalid semantic metric fixture.",
                    content_digest=source_digest,
                    tags=[],
                    created_by="owner",
                    created_at=_now(),
                    published_head_revision=1,
                    published_head_updated_at=_now(),
                )
            )
            await session.flush()
            with pytest.raises(
                IntegrityError,
                match="semantic_guideline_metrics_invalid",
            ):
                async with session.begin_nested():
                    session.add(
                        SemanticGuidelineRevisionRow(
                            revision_id=revision_id,
                            guideline_id=guideline_id,
                            metrics=metrics,
                            revision_digest=canonical_sha256(
                                {"invalid_metric_revision": index}
                            ),
                            source_revision_digest=source_digest,
                            created_by="owner",
                            created_at=_now(),
                        )
                    )
                    await session.flush()

    await engine.dispose()


@pytest.mark.asyncio
async def test_guideline_lifecycle_persists_and_rehydrates_semantic_overlay(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-guideline-lifecycle.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    guideline_id = _id()
    created_at = _now()
    metric = GuidelineMetric(
        metric_id="segregation",
        code="architecture.segregation",
        title="Segregation",
        description="Measure separation of business and infrastructure.",
        evaluation_rubric="Score the authored artifact from 0 to 100.",
        target_entity_types=(PolicyEntityType.SPEC,),
        direction=GuidelineMetricDirection.MINIMUM,
        default_threshold=70,
    )
    revision_one = GuidelineRevision(
        revision_id=_id(),
        guideline_id=guideline_id,
        revision_number=1,
        semantic_version="1.0.0",
        title="Hexagonal architecture",
        content="Keep the domain independent from adapters.",
        metrics=(metric,),
        created_by="owner",
        created_at=created_at,
    )
    head_one = GuidelineHead(
        guideline_id=guideline_id,
        revision_id=revision_one.revision_id,
        revision_number=1,
        semantic_version="1.0.0",
        head_revision=1,
        updated_at=created_at,
    )
    identity = DomainGuideline(
        guideline_id=guideline_id,
        owner_id="owner",
        scope=GuidelineScope.GLOBAL,
        created_at=created_at,
    )
    async with factory() as session, session.begin():
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        created = await adapter.create_guideline(
            guideline=identity,
            initial_revision=revision_one,
            initial_head=head_one,
            idempotency_key="semantic-lifecycle-create",
            request_digest=canonical_sha256({"create": guideline_id}),
        )
        assert created == (identity, revision_one, head_one)
        stored_revision = await session.get(
            GuidelineRevisionRow,
            revision_one.revision_id,
        )
        semantic = await session.get(
            SemanticGuidelineRevisionRow,
            revision_one.revision_id,
        )
        assert stored_revision is not None
        assert stored_revision.content_digest == revision_one.revision_digest
        assert semantic is not None
        assert semantic.metrics == [metric.digest_payload()]
        assert semantic.revision_digest == revision_one.revision_digest
        assert (
            await adapter.get_revision(
                guideline_id=guideline_id,
                revision_id=revision_one.revision_id,
            )
            == revision_one
        )

        revision_two = GuidelineRevision(
            revision_id=_id(),
            guideline_id=guideline_id,
            revision_number=2,
            semantic_version="1.1.0",
            title=revision_one.title,
            content="Keep the domain independent and document every port.",
            metrics=(metric,),
            created_by="owner",
            created_at=_now(),
            parent_revision_id=revision_one.revision_id,
        )
        head_two = GuidelineHead(
            guideline_id=guideline_id,
            revision_id=revision_two.revision_id,
            revision_number=2,
            semantic_version="1.1.0",
            head_revision=2,
            updated_at=revision_two.created_at,
        )
        assert await adapter.append_revision_cas(
            revision=revision_two,
            next_head=head_two,
            expected_head_revision=1,
            idempotency_key="semantic-lifecycle-append",
            request_digest=canonical_sha256({"append": guideline_id}),
        ) == (revision_two, head_two)
        assert (
            await adapter.get_revision(
                guideline_id=guideline_id,
                revision_id=revision_two.revision_id,
            )
            == revision_two
        )
        replay = await adapter.get_revision_result_by_idempotency(
            guideline_id=guideline_id,
            idempotency_key="semantic-lifecycle-append",
        )
        assert replay is not None
        assert replay.revision == revision_two
        assert replay.published_head == head_two

        board_id = _id()
        session.add(
            Board(
                id=board_id,
                realm_id="local",
                name="Semantic binding lifecycle",
                owner_id="owner",
            )
        )
        await session.flush()
        binding = await _native_guideline_binding(
            session,
            board_id=board_id,
            revision=revision_two,
            priority=3,
            overrides={"architecture.segregation": 75},
        )
        stored_binding = await session.get(
            GuidelineBoardBindingRow,
            (binding.binding_id, binding.binding_revision),
        )
        stored_semantic_binding = await session.get(
            SemanticGuidelineBindingConfigurationRow,
            (binding.binding_id, binding.binding_revision),
        )
        assert stored_binding is not None
        assert stored_binding.enforcement == "blocking"
        assert stored_semantic_binding is not None
        assert stored_semantic_binding.configuration_digest == (
            binding.configuration_digest
        )
        assert stored_semantic_binding.minimum_confidence == 80
        assert stored_semantic_binding.metric_threshold_overrides == {
            "architecture.segregation": 75
        }
        assert (
            await adapter.get_binding(
                board_id=board_id,
                guideline_id=guideline_id,
            )
            == binding
        )
        assert await adapter.list_bindings(board_id=board_id) == (binding,)

    await engine.dispose()


@pytest.mark.asyncio
async def test_impact_preview_and_adoption_persist_v2_semantic_configuration(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-impact-v2.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)
    await _install_guideline_impact_triggers(engine)

    guideline_id = _id()
    board_id = _id()
    timestamp = _now()
    metric = GuidelineMetric(
        metric_id="segregation",
        code="architecture.segregation",
        title="Segregation",
        description="Measure separation of business and infrastructure.",
        evaluation_rubric="Score the authored artifact from 0 to 100.",
        target_entity_types=(PolicyEntityType.SPEC,),
        direction=GuidelineMetricDirection.MINIMUM,
        default_threshold=70,
    )
    revision = GuidelineRevision(
        revision_id=_id(),
        guideline_id=guideline_id,
        revision_number=1,
        semantic_version="1.0.0",
        title="Hexagonal architecture",
        content="Keep the domain independent from adapters.",
        metrics=(metric,),
        created_by="owner",
        created_at=timestamp,
    )
    head = GuidelineHead(
        guideline_id=guideline_id,
        revision_id=revision.revision_id,
        revision_number=1,
        semantic_version=revision.semantic_version,
        head_revision=1,
        updated_at=timestamp,
    )
    identity = DomainGuideline(
        guideline_id=guideline_id,
        owner_id="owner",
        scope=GuidelineScope.GLOBAL,
        created_at=timestamp,
    )

    async with factory() as session, session.begin():
        session.add(
            Board(
                id=board_id,
                realm_id="local",
                name="Semantic impact v2",
                owner_id="owner",
            )
        )
        await session.flush()
        adapter = CommunitySqlAlchemyGuidelinePolicy(session)
        await adapter.create_guideline(
            guideline=identity,
            initial_revision=revision,
            initial_head=head,
            idempotency_key="semantic-impact-create",
            request_digest=canonical_sha256({"create": guideline_id}),
        )
        preview = plan_guideline_impact_preview(
            GuidelineImpactPreviewCommand(
                impact_receipt_id=f"impact-{_id()}",
                board_id=board_id,
                guideline_id=guideline_id,
                head=head,
                to_revision=revision,
                current_binding=None,
                from_revision=None,
                active_bindings=(),
                active_revisions=(),
                subjects=(),
                waivers=(),
                proposed_priority=2,
                proposed_enforcement=GuidelineEnforcement.BLOCKING,
                proposed_minimum_confidence=80,
                proposed_metric_threshold_overrides={"architecture.segregation": 75},
                requested_by="owner",
                created_at=timestamp,
                idempotency_key="semantic-impact-preview",
                requested_to_revision_id=revision.revision_id,
            )
        )
        saved_receipt = await adapter.save_impact_preview(plan=preview)
        receipt_row = await session.get(
            GuidelineImpactReceiptRow,
            saved_receipt.impact_receipt_id,
        )
        assert receipt_row is not None
        assert receipt_row.proposed_enforcement == "blocking"
        assert receipt_row.proposed_minimum_confidence == 80
        assert receipt_row.proposed_metric_threshold_overrides == {
            "architecture.segregation": 75
        }
        assert receipt_row.added_metric_ids == ["segregation"]
        assert not hasattr(receipt_row, "added_rule_ids")

        adoption_event_id = _id()
        adoption = plan_guideline_adoption(
            receipt=saved_receipt,
            current_snapshot=impact_fence_from_receipt(saved_receipt),
            current_binding=None,
            retirement=None,
            actor_id="owner",
            actor_type="user",
            occurred_at=_now(),
            event_id=adoption_event_id,
            idempotency_key="semantic-impact-adopt",
        )
        adopted_binding, adopted_receipt = await adapter.adopt_revision_cas(
            mutation=adoption
        )
        assert adopted_receipt == saved_receipt
        assert adopted_binding.enforcement is GuidelineEnforcement.BLOCKING
        assert adopted_binding.minimum_confidence == 80
        assert dict(adopted_binding.metric_threshold_overrides) == {
            "architecture.segregation": 75
        }
        semantic_binding = await session.get(
            SemanticGuidelineBindingConfigurationRow,
            (
                adopted_binding.binding_id,
                adopted_binding.binding_revision,
            ),
        )
        assert semantic_binding is not None
        assert semantic_binding.configuration_digest == (
            adopted_binding.configuration_digest
        )
        replay = await adapter.get_adoption_result_by_idempotency(
            board_id=board_id,
            idempotency_key="semantic-impact-adopt",
        )
        assert replay is not None
        assert replay.binding == adopted_binding
        assert replay.receipt == saved_receipt

        projection_rows = tuple(
            (
                await session.execute(
                    select(DomainEventRow).where(
                        DomainEventRow.board_id == board_id,
                        DomainEventRow.event_type
                        == SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE,
                    )
                )
            ).scalars()
        )
        assert {
            (row.payload_json["entity_kind"], row.payload_json["causation_id"])
            for row in projection_rows
        } == {
            ("revision", adoption_event_id),
            ("metric_definition", adoption_event_id),
            ("binding_configuration", adoption_event_id),
        }
        projection_ids = {row.id for row in projection_rows}
        executions = tuple(
            (
                await session.execute(
                    select(DomainEventHandlerExecution).where(
                        DomainEventHandlerExecution.event_id.in_(projection_ids)
                    )
                )
            ).scalars()
        )
        assert len(executions) == len(projection_ids) == 3
        assert all(
            execution.handler_name == SEMANTIC_GUIDELINE_PROJECTION_HANDLER
            and execution.status == "pending"
            for execution in executions
        )

    await engine.dispose()


def _sqlite_engine(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    install_community_sqlite_pragmas(engine)
    return engine


async def _install_semantic_triggers(engine) -> None:
    await initialize_current_schema(engine, current_schema_contract())


async def _install_guideline_impact_triggers(engine) -> None:
    await initialize_current_schema(engine, current_schema_contract())


async def _native_guideline_binding(
    session, *, board_id, revision, priority=0, minimum_confidence=80, overrides=None
):
    adapter = CommunitySqlAlchemyGuidelinePolicy(session)
    head = await adapter.get_head(guideline_id=revision.guideline_id)
    if head is None:
        head = GuidelineHead(
            guideline_id=revision.guideline_id,
            revision_id=revision.revision_id,
            revision_number=1,
            semantic_version=revision.semantic_version,
            head_revision=1,
            updated_at=revision.created_at,
        )
        await adapter.create_guideline(
            guideline=DomainGuideline(
                guideline_id=revision.guideline_id,
                owner_id=revision.created_by,
                scope=GuidelineScope.GLOBAL,
                created_at=revision.created_at,
            ),
            initial_revision=revision,
            initial_head=head,
            idempotency_key=f"create:{revision.revision_id}",
            request_digest=canonical_sha256({"native_create": revision.revision_id}),
        )
    bindings = await adapter.list_bindings(board_id=board_id)
    revisions = tuple(
        [
            await adapter.get_revision(
                guideline_id=b.guideline_id, revision_id=b.revision_id
            )
            for b in bindings
        ]
    )
    preview = plan_guideline_impact_preview(
        GuidelineImpactPreviewCommand(
            impact_receipt_id=_id(),
            board_id=board_id,
            guideline_id=revision.guideline_id,
            head=head,
            to_revision=revision,
            current_binding=None,
            from_revision=None,
            active_bindings=bindings,
            active_revisions=revisions,
            subjects=await adapter.list_policy_subjects(board_id=board_id),
            waivers=(),
            proposed_priority=priority,
            proposed_enforcement=GuidelineEnforcement.BLOCKING,
            proposed_minimum_confidence=minimum_confidence,
            proposed_metric_threshold_overrides=overrides or {},
            requested_by="board-owner",
            created_at=_now(),
            idempotency_key=_id(),
        )
    )
    await adapter.save_impact_preview(plan=preview)
    adoption = plan_guideline_adoption(
        receipt=preview.receipt,
        current_snapshot=impact_fence_from_receipt(preview.receipt),
        current_binding=None,
        retirement=None,
        actor_id="board-owner",
        actor_type="user",
        occurred_at=_now(),
        event_id=_id(),
        idempotency_key=_id(),
    )
    binding, receipt = await adapter.adopt_revision_cas(mutation=adoption)
    assert await adapter.adopt_revision_cas(mutation=adoption) == (binding, receipt)
    return binding


async def _seed_semantic_authority(
    session: AsyncSession,
    *,
    metric_count: int = 2,
    entity_type: PolicyEntityType = PolicyEntityType.IDEATION,
    metric_directions: tuple[GuidelineMetricDirection, ...] | None = None,
    non_applicable_metric: bool = False,
) -> tuple[
    str,
    str,
    GuidelineRevision,
    BoardGuidelineBinding,
]:
    board_id = _id()
    subject_id = _id()
    guideline_id = _id()
    revision_id = _id()
    _id()
    timestamp = _now()
    metrics = tuple(
        GuidelineMetric(
            metric_id=f"metric-{index}",
            code=f"metric_{index}",
            title=f"Metric {index}",
            description=f"Semantic dimension {index}.",
            evaluation_rubric="Score the authored artifact from 0 to 100.",
            target_entity_types=(PolicyEntityType.REFINEMENT,) if non_applicable_metric and index == metric_count - 1 else (entity_type,),
            direction=metric_directions[index] if metric_directions else GuidelineMetricDirection.MINIMUM,
            default_threshold=70,
        )
        for index in range(metric_count)
    )
    revision = GuidelineRevision(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=1,
        semantic_version="1.0.0",
        title="Hexagonal architecture",
        content="Keep business rules independent from technical adapters.",
        metrics=metrics,
        created_by="guideline-author",
        created_at=timestamp,
    )
    session.add(
        Board(id=board_id, realm_id="local", name="SK-B3", owner_id="board-owner")
    )
    await session.flush()
    if entity_type is PolicyEntityType.IDEATION:
        subject = Ideation(
            id=subject_id,
            board_id=board_id,
            title="Ports and adapters",
            description="Separate business capabilities from infrastructure.",
            problem_statement="Coupling makes change expensive.",
            proposed_approach="Use explicit ports.",
            # Human policy assessment is admitted only in the lifecycle's
            # validation stage.
            status="evaluating",
            edition=1,
            version=1,
            created_by="artifact-author",
        )
        session.add(subject)
    elif entity_type is PolicyEntityType.REFINEMENT:
        parent_ideation_id = _id()
        session.add(
            Ideation(
                id=parent_ideation_id,
                board_id=board_id,
                title="Parent ideation",
                status="evaluating",
                edition=1,
                version=1,
                created_by="artifact-author",
            )
        )
        session.add(
            Refinement(
                id=subject_id,
                ideation_id=parent_ideation_id,
                board_id=board_id,
                title="Boundary refinement",
                description="Resolve the application boundary.",
                analysis="The use case owns orchestration.",
                status="approved",
                edition=1,
                version=1,
                created_by="artifact-author",
            )
        )
    elif entity_type is PolicyEntityType.SPEC:
        session.add(
            Spec(
                id=subject_id,
                board_id=board_id,
                title="Boundary specification",
                architecture_adoption=ArchitectureAdoptionScope(
                    board_id=board_id,
                    spec_id=subject_id,
                    adopted_in_edition=1,
                    actor_id="artifact-author",
                    inherited_resource_ids=(),
                ).model_dump(mode="json"),
                execution_contract=new_execution_contract(
                    board_id=board_id,
                    spec_id=subject_id,
                    edition=1,
                    actor_id="artifact-author",
                    origin="new_spec",
                ),
                description="Specify the application boundary.",
                context="The use case owns orchestration.",
                status="approved",
                edition=1,
                version=1,
                created_by="artifact-author",
            )
        )
    else:  # pragma: no cover - this helper only seeds edition-capable subjects
        raise AssertionError(f"unsupported edition subject: {entity_type.value}")
    await session.flush()
    binding = await _native_guideline_binding(
        session,
        board_id=board_id,
        revision=revision,
        overrides={"metric_1": 75} if metric_count > 1 else {},
    )
    await CommunitySqlAlchemySemanticGuidelineAssessment(
        session
    ).record_semantic_subject_mutation(
        board_id=board_id,
        entity_type=entity_type,
        subject_id=subject_id,
        actor_id="artifact-author",
        idempotency_key=f"subject-created:{subject_id}",
        request_digest=canonical_sha256({"subject_created": subject_id}),
        changed_at=_now(),
    )
    return board_id, subject_id, revision, binding


async def _seed_unlinked_semantic_binding_head(
    session: AsyncSession,
    *,
    board_id: str,
) -> BoardGuidelineBinding:
    guideline_id = _id()
    revision_id = _id()
    _id()
    timestamp = _now()
    revision = GuidelineRevision(
        revision_id=revision_id,
        guideline_id=guideline_id,
        revision_number=1,
        semantic_version="1.0.0",
        title="Unlinked historical policy",
        content="Preserve this binding head as an authority fence.",
        metrics=(),
        created_by="guideline-author",
        created_at=timestamp,
    )
    binding = await _native_guideline_binding(
        session, board_id=board_id, revision=revision, priority=1
    )
    adapter = CommunitySqlAlchemyGuidelinePolicy(session)
    bindings = await adapter.list_bindings(board_id=board_id)
    revisions = tuple(
        [
            await adapter.get_revision(
                guideline_id=b.guideline_id, revision_id=b.revision_id
            )
            for b in bindings
        ]
    )
    unlink = plan_guideline_unlink(
        current_binding=binding,
        current_revision=revision,
        active_bindings=bindings,
        active_revisions=revisions,
        retirement=None,
        actor_id="board-owner",
        actor_type="user",
        occurred_at=_now(),
        event_id=_id(),
        idempotency_key=_id(),
    )
    return await adapter.unlink_binding_cas(mutation=unlink)


async def _record_subject_digest(
    session: AsyncSession,
    *,
    board_id: str,
    ideation_id: str,
) -> str:
    snapshot = await CommunitySqlAlchemySemanticGuidelineAssessment(
        session
    ).record_semantic_subject_mutation(
        board_id=board_id,
        entity_type=PolicyEntityType.IDEATION,
        subject_id=ideation_id,
        actor_id="artifact-author",
        idempotency_key=_id(),
        request_digest=canonical_sha256({"mutation": _id()}),
        changed_at=_now(),
    )
    assert snapshot is not None
    return snapshot.content_digest


async def _record_failed_semantic_assessment(
    session: AsyncSession,
    *,
    board_id: str,
    ideation_id: str,
    revision: GuidelineRevision,
    binding: BoardGuidelineBinding,
    idempotency_key: str,
    snapshot=None,
    score: int = 60,
    load_findings: bool = True,
    persist_result: bool = True,
):
    adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
    if snapshot is None:
        snapshot = await adapter.record_semantic_subject_mutation(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            actor_id="artifact-author",
            idempotency_key=f"subject-{idempotency_key}",
            request_digest=canonical_sha256({"subject_mutation": idempotency_key}),
            changed_at=_now(),
        )
    from okto_pulse.core.domain.guideline_semantic_v2 import SemanticAssessmentRequestV2, SemanticMetricAssessmentV2
    from okto_pulse.core.domain.guideline_semantic_findings_v2 import project_semantic_metric_findings_v2
    from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import CommunitySqlAlchemySemanticGuidelineAssessmentV2
    from test_skb31_semantic_pinpoint_v2_persistence import _pinpoint
    request = SemanticAssessmentRequestV2(
        subject=snapshot.subject, binding_id=binding.binding_id,
        expected_binding_revision=binding.binding_revision,
        guideline_revision_id=revision.revision_id, idempotency_key=idempotency_key,
        confidence=92, assessor=SemanticAssessmentAssessor(
            agent_id="independent-reviewer", model_id="test-model"),
        metric_results=tuple(SemanticMetricAssessmentV2(
            metric_id=metric.metric_id, score=score,
            rationale=f"{metric.title} independently assessed at {score}.",
            evidence_refs=(EvidenceRef(source_type="ideation", source_id=ideation_id,
                source_version=snapshot.subject.subject_version, content_hash=snapshot.content_digest),),
            pinpoints=(_pinpoint(key=metric.metric_id, issue=score < 75),),
        ) for metric in revision.metrics),
    )
    if not persist_result:
        return snapshot, request, ()
    persisted = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).save_semantic_assessment_v2(request)
    findings = project_semantic_metric_findings_v2(persisted.receipt) if load_findings else ()
    return snapshot, persisted, findings




@pytest.mark.asyncio
async def test_native_assessment_uses_active_authority_with_unlinked_heads(
    tmp_path,
    semantic_relational_application_adapter,
):
    engine = _sqlite_engine(tmp_path / "semantic-unlinked-head-fence.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    async with factory() as session, session.begin():
        (
            board_id,
            ideation_id,
            revision,
            active_binding,
        ) = await _seed_semantic_authority(session, metric_count=1)
        board = await session.get(Board, board_id)
        assert board is not None
        board.realm_id = "local"
        unlinked_binding = await _seed_unlinked_semantic_binding_head(
            session,
            board_id=board_id,
        )
        subject = await CommunitySqlAlchemySemanticGuidelineAssessment(
            session
        ).record_semantic_subject_mutation(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            actor_id="artifact-author",
            idempotency_key="subject-before-unlinked-head-assessment",
            request_digest=canonical_sha256(
                {"subject": "before-unlinked-head-assessment"}
            ),
            changed_at=_now(),
        )

    actor = ActorContext(
        "board-owner",
        "mcp",
        board_id=board_id,
        permissions=(ASSESSMENTS_RECORD, "guidelines.read"),
    )
    async with factory() as session:
        policy = CommunitySqlAlchemyGuidelinePolicy(session)
        semantic = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        active_bindings = await policy.list_bindings(board_id=board_id)
        assert tuple(item.binding_id for item in active_bindings) == (
            active_binding.binding_id,
        )
        assert unlinked_binding.binding_id not in {
            item.binding_id for item in active_bindings
        }
        (
            _policy_set_digest,
            authoritative_head_digest,
        ) = await semantic.semantic_current_fences(board_id=board_id)
        assert authoritative_head_digest != semantic_binding_head_digest_v1(
            active_bindings
        )

        metric = revision.metrics[0]
        from okto_pulse.core.application.use_cases.semantic_guideline_v2 import (
            SealSemanticGuidelineAssessmentV2Command, SealSemanticGuidelineAssessmentV2UseCase,
        )
        from okto_pulse.core.domain.guideline_semantic_v2 import (
            SemanticAssessmentDraftV2, SemanticMetricAssessmentDraftV2,
            SemanticPinpointDraftV2, SemanticPinpointKind,
        )
        from okto_pulse.community.adapters.sqlalchemy_models import SemanticGuidelineAssessmentV2Row
        result = await SealSemanticGuidelineAssessmentV2UseCase().execute(
            SealSemanticGuidelineAssessmentV2Command(
                board_id=board_id, actor_id=actor.actor_id,
                draft=SemanticAssessmentDraftV2(
                    subject=subject.subject,
                    binding_id=active_binding.binding_id,
                    expected_binding_revision=active_binding.binding_revision,
                    guideline_revision_id=revision.revision_id,
                    idempotency_key="assessment-unlinked-head-fence",
                    confidence=92,
                    assessor=SemanticAssessmentAssessor(agent_id=actor.actor_id, model_id="test-model"),
                    metric_results=(SemanticMetricAssessmentDraftV2(
                        metric_id=metric.metric_id, score=80,
                        rationale="The ideation provides independently verifiable evidence.",
                        evidence_refs=(EvidenceRef(source_type="ideation", source_id=ideation_id,
                            source_version=subject.subject.subject_version, content_hash=subject.content_digest),),
                        pinpoints=(SemanticPinpointDraftV2(
                            pinpoint_key="boundary", kind=SemanticPinpointKind.EVIDENCE,
                            title="Explicit boundary", detail="The ideation states the responsibility.",
                            severity=None, remediation=None,
                            anchor=UnboundFindingAnchor(anchor_type=FindingAnchorType.WHOLE_ARTIFACT),
                        ),),
                    ),),
                ),
            ),
            actor=actor, uow=CommunityUnitOfWork(session, actor=actor),
        )

        receipt = result.persistence.receipt
        assert receipt.binding_id == active_binding.binding_id
        assert receipt.binding_revision == active_binding.binding_revision
        assert receipt.binding_configuration_digest == active_binding.configuration_digest
        assert receipt.guideline_revision_digest == revision.revision_digest
        stored = await session.get(SemanticGuidelineAssessmentV2Row, receipt.receipt_id)
        assert stored is not None
        assert stored.contract_version == "semantic-guideline-assessment/v2"

    await engine.dispose()


@pytest.mark.asyncio
async def test_transition_preview_reads_during_writer_without_relaxing_enforcement(
    tmp_path, semantic_relational_application_adapter,
):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    engine = _sqlite_engine(tmp_path / "preview-concurrent-writer.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            board_id, ideation_id, _, _ = await _seed_semantic_authority(session, metric_count=1)
            ideation = await session.get(Ideation, ideation_id)
            ideation.status = "evaluating"
        args = dict(board_id=board_id, entity_type="ideation", subject_id=ideation_id,
                    from_status="evaluating", to_status="done")
        async with engine.connect() as writer:
            await writer.execute(text("UPDATE boards SET id=id WHERE id=:id"), {"id": board_id})
            statements = []
            def capture(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement)
            event.listen(engine.sync_engine, "before_cursor_execute", capture)
            try:
                async with factory() as reader:
                    await reader.execute(text("PRAGMA busy_timeout=1"))
                    await reader.execute(select(Board.id).where(Board.id == board_id))
                    preview = await GuidelineService(reader).preview_policy_transition(**args)
                    assert preview.allowed is False
                    assert preview.reason_codes == (PolicyTransitionReasonCode.POLICY_COMPLIANCE_RECEIPT_MISSING,)
                    assert not any(s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for s in statements)
                    await reader.rollback()
                async with factory() as mutation:
                    await mutation.execute(text("PRAGMA busy_timeout=1"))
                    with pytest.raises(OperationalError, match="database is locked"):
                        await GuidelineService(mutation).enforce_policy_transition(**args)
                    await mutation.rollback()
                assert any(s.startswith("UPDATE boards") for s in statements)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", capture)
                await writer.rollback()
        async with factory() as session:
            with pytest.raises(PolicyTransitionRejected):
                await GuidelineService(session).enforce_policy_transition(**args)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_transition_runtime_is_authoritative_end_to_end(
    tmp_path,
    semantic_relational_application_adapter,
):
    engine = _sqlite_engine(tmp_path / "semantic-transition-runtime.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    async with factory() as session, session.begin():
        board_id, ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=1
        )
        ideation = await session.get(Ideation, ideation_id)
        assert ideation is not None
        ideation.status = "evaluating"
        await session.flush((ideation,))
        service = GuidelineService(session)
        composed = CommunityRelationalApplicationAdapter().guideline_policy(session)
        assert isinstance(
            composed._transition_snapshot_resolver,  # noqa: SLF001
            CommunitySqlAlchemySemanticGuidelineAssessment,
        )
        missing = await service.preview_policy_transition(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION.value,
            subject_id=ideation_id,
            from_status="evaluating",
            to_status="done",
        )
        assert missing is not None
        assert missing.allowed is False
        assert missing.reason_codes == (
            PolicyTransitionReasonCode.POLICY_COMPLIANCE_RECEIPT_MISSING,
        )
        with pytest.raises(PolicyTransitionRejected):
            await service.enforce_policy_transition(
                board_id=board_id,
                entity_type=PolicyEntityType.IDEATION.value,
                subject_id=ideation_id,
                from_status="evaluating",
                to_status="done",
            )

        current_subject, passed, _findings = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision,
            binding=binding,
            idempotency_key="transition-pass",
            score=90,
        )
        assert sum(metric.outcome.value == "fail" for metric in passed.receipt.metric_results) == 0
        ready = await service.enforce_policy_transition(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION.value,
            subject_id=ideation_id,
            from_status="evaluating",
            to_status="done",
        )
        assert ready is not None
        assert ready.allowed is True
        assert ready.reason_codes == (
            PolicyTransitionReasonCode.POLICY_COMPLIANCE_READY,
        )

        ideation.description = "The semantic subject changed."
        ideation.version = 2
        await session.flush((ideation,))
        semantic = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        changed_subject = await semantic.record_semantic_subject_mutation(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            actor_id="second-artifact-author",
            idempotency_key="transition-subject-change",
            request_digest=canonical_sha256({"transition": "subject-change"}),
            changed_at=_now(),
        )
        assert changed_subject != current_subject
        same_edition = await service.preview_policy_transition(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION.value,
            subject_id=ideation_id,
            from_status="evaluating",
            to_status="done",
        )
        assert same_edition is not None
        assert same_edition.allowed is True
        assert same_edition.reason_codes == (
            PolicyTransitionReasonCode.POLICY_COMPLIANCE_READY,
        )

        _subject, failed, findings = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision,
            binding=binding,
            idempotency_key="transition-fail",
            snapshot=changed_subject,
        )
        assert sum(metric.outcome.value == "fail" for metric in failed.receipt.metric_results) == 1
        blocked = await service.preview_policy_transition(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION.value,
            subject_id=ideation_id,
            from_status="evaluating",
            to_status="done",
        )
        assert blocked is not None
        assert blocked.allowed is False
        assert blocked.reason_codes == (
            PolicyTransitionReasonCode.POLICY_COMPLIANCE_BLOCKED,
        )

        skip_scope = SemanticPolicySkipScope.from_authority(
            subject_snapshot=changed_subject,
            binding=binding,
            revision=revision,
        )
        skip_mutation = create_semantic_policy_skip(
            skip_id=canonical_sha256({"transition": "skip"}),
            event_id=canonical_sha256({"transition": "skip", "revision": 1}),
            scope=skip_scope,
            reason="A human explicitly accepts this governed transition.",
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=_now(),
            idempotency_key="transition-skip-create",
        )
        await semantic.save_semantic_policy_skip_mutation(mutation=skip_mutation)
        skipped = await service.preview_policy_transition(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION.value,
            subject_id=ideation_id,
            from_status="evaluating",
            to_status="done",
        )
        assert skipped is not None
        assert skipped.allowed is True
        assert skipped.reason_codes == (
            PolicyTransitionReasonCode.POLICY_COMPLIANCE_READY_WITH_WAIVERS,
        )
        revoked_skip = revoke_semantic_policy_skip(
            skip_mutation.skip,
            event_id=canonical_sha256({"transition": "skip", "revision": 2}),
            expected_skip_revision=1,
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=_now(),
            reason="The assessment waiver now owns the exception.",
            idempotency_key="transition-skip-revoke",
        )
        await semantic.save_semantic_policy_skip_mutation(mutation=revoked_skip)

        evidence = (
            EvidenceRef(
                source_type="review",
                source_id="transition-waiver",
                source_version=1,
                content_hash=canonical_sha256({"transition": "waiver-evidence"}),
            ),
        )
        requested = request_semantic_metric_waiver(
            waiver_id=canonical_sha256({"transition": "waiver"}),
            event_id=canonical_sha256({"transition": "waiver", "revision": 1}),
            anchor=SemanticMetricWaiverAnchor.from_finding(
                findings[0],
                assessment_assessor_id="independent-reviewer",
            ),
            justification="A bounded exception is independently reviewed.",
            evidence_refs=evidence,
            requested_by="waiver-requester",
            requested_at=_now(),
            expires_at=_now() + timedelta(hours=1),
            idempotency_key="transition-waiver-request",
        )
        await semantic.save_semantic_metric_waiver_mutation(mutation=requested)
        approved = transition_semantic_metric_waiver(
            requested.waiver,
            event_id=canonical_sha256({"transition": "waiver", "revision": 2}),
            expected_waiver_revision=1,
            event_type=SemanticMetricWaiverEventType.APPROVE,
            actor_id="waiver-reviewer",
            occurred_at=_now(),
            reason="The exception is justified for this exact finding.",
            evidence_refs=evidence,
            idempotency_key="transition-waiver-approve",
        )
        await semantic.save_semantic_metric_waiver_mutation(mutation=approved)
        waived = await service.enforce_policy_transition(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION.value,
            subject_id=ideation_id,
            from_status="evaluating",
            to_status="done",
        )
        assert waived is not None
        assert waived.allowed is True
        assert waived.reason_codes == (
            PolicyTransitionReasonCode.POLICY_COMPLIANCE_READY_WITH_WAIVERS,
        )

        projection_rows = tuple(
            (
                await session.execute(
                    select(DomainEventRow).where(
                        DomainEventRow.board_id == board_id,
                        DomainEventRow.event_type
                        == SEMANTIC_GUIDELINE_PROJECTION_EVENT_TYPE,
                    )
                )
            ).scalars()
        )
        projection_pairs = {
            (row.payload_json["entity_kind"], row.payload_json["causation_id"])
            for row in projection_rows
        }
        assert {
            ("assessment_receipt", passed.receipt.receipt_id),
            ("metric_result", passed.receipt.receipt_id),
            ("assessment_receipt", failed.receipt.receipt_id),
            ("metric_result", failed.receipt.receipt_id),
            ("skip", skip_mutation.event.event_id),
            ("skip", revoked_skip.event.event_id),
            ("waiver", requested.event.event_id),
            ("waiver", approved.event.event_id),
        }.issubset(projection_pairs)
        assert any(
            row.payload_json["entity_kind"] == "skip"
            and row.payload_json["causation_id"] == revoked_skip.event.event_id
            and row.payload_json["operation"] == "terminate"
            for row in projection_rows
        )
        projection_ids = {row.id for row in projection_rows}
        projection_executions = tuple(
            (
                await session.execute(
                    select(DomainEventHandlerExecution).where(
                        DomainEventHandlerExecution.event_id.in_(projection_ids)
                    )
                )
            ).scalars()
        )
        assert len(projection_executions) == len(projection_ids)
        assert all(
            execution.handler_name == SEMANTIC_GUIDELINE_PROJECTION_HANDLER
            and execution.status == "pending"
            for execution in projection_executions
        )

        (
            context_board,
            context_ideation,
            _revision,
            _binding,
        ) = await _seed_semantic_authority(session, metric_count=0)
        context_subject = await session.get(Ideation, context_ideation)
        assert context_subject is not None
        context_subject.status = "evaluating"
        await session.flush((context_subject,))
        context_only = await service.enforce_policy_transition(
            board_id=context_board,
            entity_type=PolicyEntityType.IDEATION.value,
            subject_id=context_ideation,
            from_status="evaluating",
            to_status="done",
        )
        assert context_only is not None
        assert context_only.allowed is True
        assert context_only.reason_codes == (
            PolicyTransitionReasonCode.POLICY_COMPLIANCE_NOT_APPLICABLE,
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_canonical_resource_agent_journey_reassesses_same_edition_subject(
    tmp_path,
):
    from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import CommunitySqlAlchemySemanticGuidelineAssessmentV2
    core_repo = resolve_core_repo(Path(__file__).resolve().parents[1])
    protocol = (
        core_repo
        / "src"
        / "okto_pulse"
        / "core"
        / "mcp"
        / "resources"
        / "reference"
        / "policy-compliance.md"
    ).read_text(encoding="utf-8")
    ordered_tools = (
        "okto_pulse_get_guideline_revision",
        "okto_pulse_record_semantic_guideline_assessment",
        "okto_pulse_get_current_semantic_guideline_assessment",
    )
    assert tuple(protocol.index(tool) for tool in ordered_tools) == tuple(
        sorted(protocol.index(tool) for tool in ordered_tools)
    )

    engine = _sqlite_engine(tmp_path / "semantic-resource-journey.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    async with factory() as session, session.begin():
        board_id, ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=1
        )
        first_snapshot, first, _ = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision,
            binding=binding,
            idempotency_key="resource-journey-v1",
            score=90,
        )
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        assert (
            await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).get_current_semantic_assessment_v2(
                board_id=board_id,
                entity_type=PolicyEntityType.IDEATION,
                subject_id=ideation_id,
                binding_id=binding.binding_id,
            )
            == first.receipt
        )

        ideation = await session.get(Ideation, ideation_id)
        assert ideation is not None
        ideation.description = "The canonical journey now has changed content."
        ideation.version = 2
        await session.flush((ideation,))
        refreshed_snapshot = await adapter.record_semantic_subject_mutation(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            actor_id="second-artifact-author",
            idempotency_key="resource-journey-subject-v2",
            request_digest=canonical_sha256({"resource_journey": "subject-v2"}),
            changed_at=_now(),
        )
        assert refreshed_snapshot != first_snapshot
        assert (
            await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).get_current_semantic_assessment_v2(
                board_id=board_id,
                entity_type=PolicyEntityType.IDEATION,
                subject_id=ideation_id,
                binding_id=binding.binding_id,
            )
            == first.receipt
        )

        _snapshot, second, _ = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision,
            binding=binding,
            idempotency_key="resource-journey-v2",
            snapshot=refreshed_snapshot,
            score=90,
        )
        current = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).get_current_semantic_assessment_v2(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            binding_id=binding.binding_id,
        )
        assert current == second.receipt
        assert current.receipt_id != first.receipt.receipt_id
        history, cursor = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(session).list_semantic_assessment_v2_receipts(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            binding_id=binding.binding_id,
            limit=10,
        )
        assert {receipt.receipt_id for receipt in history} == {
            first.receipt.receipt_id,
            second.receipt.receipt_id,
        }
        assert cursor is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_composite_revision_adoption_assessment_cas_and_replay(
    tmp_path,
):
    from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import CommunitySqlAlchemySemanticGuidelineAssessmentV2
    from okto_pulse.core.ports.guideline_policy import GuidelinePolicyEditionConflict
    engine = _sqlite_engine(tmp_path / "semantic-composite-cas.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    async with factory() as seed_session, seed_session.begin():
        (
            board_id,
            ideation_id,
            revision_one,
            binding_one,
        ) = await _seed_semantic_authority(seed_session, metric_count=1)

    await _install_guideline_impact_triggers(engine)

    async with factory() as session, session.begin():
        snapshot, stale_candidate, _ = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision_one,
            binding=binding_one,
            idempotency_key="composite-assessment-stale",
            score=90,
            load_findings=False,
            persist_result=False,
        )
        revision_two = GuidelineRevision(
            revision_id=_id(),
            guideline_id=revision_one.guideline_id,
            revision_number=2,
            semantic_version="1.0.1",
            title=revision_one.title,
            content=f"{revision_one.content} Document the ports explicitly.",
            metrics=revision_one.metrics,
            created_by="guideline-author",
            created_at=_now(),
            parent_revision_id=revision_one.revision_id,
        )
        head_two = GuidelineHead(
            guideline_id=revision_two.guideline_id,
            revision_id=revision_two.revision_id,
            revision_number=2,
            semantic_version=revision_two.semantic_version,
            head_revision=2,
            updated_at=revision_two.created_at,
        )
        policy = CommunitySqlAlchemyGuidelinePolicy(session)
        revision_request_digest = canonical_sha256({"composite": "revision-two"})
        appended = await policy.append_revision_cas(
            revision=revision_two,
            next_head=head_two,
            expected_head_revision=1,
            idempotency_key="composite-revision-two",
            request_digest=revision_request_digest,
        )
        assert appended == (revision_two, head_two)

        preview = plan_guideline_impact_preview(
            GuidelineImpactPreviewCommand(
                impact_receipt_id=f"impact-{_id()}",
                board_id=board_id,
                guideline_id=revision_two.guideline_id,
                head=head_two,
                to_revision=revision_two,
                current_binding=binding_one,
                from_revision=revision_one,
                active_bindings=(binding_one,),
                active_revisions=(revision_one,),
                subjects=(snapshot.subject,),
                waivers=(),
                proposed_priority=binding_one.priority,
                proposed_enforcement=binding_one.enforcement,
                proposed_minimum_confidence=binding_one.minimum_confidence,
                proposed_metric_threshold_overrides=dict(
                    binding_one.metric_threshold_overrides
                ),
                requested_by="board-owner",
                created_at=_now(),
                idempotency_key="composite-impact-preview",
                requested_to_revision_id=revision_two.revision_id,
            )
        )
        saved_preview = await policy.save_impact_preview(plan=preview)
        adoption = plan_guideline_adoption(
            receipt=saved_preview,
            current_snapshot=impact_fence_from_receipt(saved_preview),
            current_binding=binding_one,
            retirement=None,
            actor_id="board-owner",
            actor_type="user",
            occurred_at=_now(),
            event_id=_id(),
            idempotency_key="composite-adoption",
        )
        binding_two, adopted_receipt = await policy.adopt_revision_cas(
            mutation=adoption
        )
        assert adopted_receipt == saved_preview
        assert binding_two.binding_revision == 2
        assert binding_two.revision_id == revision_two.revision_id

        semantic = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        native = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
        # Board adoption applies to the next edition; keep the first snapshot immutable.
        ideation = await session.get(Ideation, ideation_id)
        ideation.edition = 2
        await session.flush((ideation,))
        snapshot = await semantic.record_semantic_subject_mutation(
            board_id=board_id, entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id, actor_id="artifact-author",
            idempotency_key="composite-new-edition", request_digest=canonical_sha256({"edition": 2}),
            changed_at=_now(),
        )
        await semantic.freeze_validation_policy_scope(
            board_id=board_id, entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id, subject_edition=2,
        )
        with pytest.raises(GuidelinePolicyEditionConflict):
            await native.save_semantic_assessment_v2(stale_candidate)

        stale_revision_two = GuidelineRevision(
            revision_id=_id(),
            guideline_id=revision_two.guideline_id,
            revision_number=2,
            semantic_version="1.0.1",
            title=revision_two.title,
            content=f"{revision_one.content} Competing revision intent.",
            metrics=revision_two.metrics,
            created_by="guideline-author",
            created_at=_now(),
            parent_revision_id=revision_one.revision_id,
        )
        with pytest.raises(GuidelinePolicyHeadConflict):
            await policy.append_revision_cas(
                revision=stale_revision_two,
                next_head=GuidelineHead(
                    guideline_id=stale_revision_two.guideline_id,
                    revision_id=stale_revision_two.revision_id,
                    revision_number=2,
                    semantic_version=stale_revision_two.semantic_version,
                    head_revision=2,
                    updated_at=stale_revision_two.created_at,
                ),
                expected_head_revision=1,
                idempotency_key="composite-stale-revision",
                request_digest=canonical_sha256({"composite": "stale-revision-three"}),
            )

        assert (
            await policy.append_revision_cas(
                revision=revision_two,
                next_head=head_two,
                expected_head_revision=1,
                idempotency_key="composite-revision-two",
                request_digest=revision_request_digest,
            )
            == appended
        )
        assert await policy.adopt_revision_cas(mutation=adoption) == (
            binding_two,
            adopted_receipt,
        )

        _snapshot, current_result, _ = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision_two,
            binding=binding_two,
            idempotency_key="composite-assessment-current",
            snapshot=snapshot,
            score=90,
            load_findings=False,
        )
        _, replay_request, _ = await _record_failed_semantic_assessment(
            session, board_id=board_id, ideation_id=ideation_id,
            revision=revision_two, binding=binding_two, snapshot=snapshot,
            idempotency_key="composite-assessment-current", score=90,
            load_findings=False, persist_result=False,
        )
        replay = await native.save_semantic_assessment_v2(replay_request)
        assert replay.receipt_id == current_result.receipt_id
        assert replay.request_digest == current_result.request_digest
        assert replay.receipt == current_result.receipt

        assert (
            await session.scalar(
                select(func.count())
                .select_from(SemanticGuidelineAssessmentV2Row)
                .where(SemanticGuidelineAssessmentV2Row.board_id == board_id)
            )
            == 1
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_subject_q_and_a_change_updates_digest(tmp_path):
    engine = _sqlite_engine(tmp_path / "semantic-q-and-a.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

    async with factory() as session, session.begin():
        board_id, ideation_id, _revision, _binding = await _seed_semantic_authority(
            session
        )
        baseline = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        question = IdeationQAItem(
            id="qa-semantic-1",
            ideation_id=ideation_id,
            question="Which boundary owns persistence?",
            question_type="choice",
            choices=[
                {"id": "domain", "label": "Domain"},
                {"id": "adapter", "label": "Adapter"},
            ],
            allow_free_text=False,
            answer="Adapter",
            selected=["adapter"],
            asked_by="artifact-author",
            answered_by="reviewer",
            revision=1,
            lifecycle="active",
            tombstoned=False,
        )
        session.add(question)
        await session.flush((question,))
        attached = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        assert attached != baseline

        question.answer = "Domain"
        question.selected = ["domain"]
        question.revision = 2
        await session.flush((question,))
        answered = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        assert answered != attached

    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_subject_kb_attach_content_version_and_unlink(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-kb.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

    async with factory() as session, session.begin():
        board_id, ideation_id, _revision, _binding = await _seed_semantic_authority(
            session
        )
        baseline = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        kb = IdeationKnowledgeBase(
            id="kb-semantic-1",
            ideation_id=ideation_id,
            title="Architecture decision",
            description="Authoritative context.",
            content="Business rules stay inside the domain.",
            mime_type="text/markdown",
            source_type="global",
            source_id=_id(),
            source_title="Architecture handbook",
            source_version=1,
            source_kb_id=_id(),
            root_source_kb_id=_id(),
            immediate_parent_kb_id=_id(),
            governance_metadata={"classification": "architecture"},
            created_by="artifact-author",
        )
        session.add(kb)
        await session.flush((kb,))
        attached = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        assert attached != baseline

        kb.content = "Business rules and use cases stay inside the domain."
        await session.flush((kb,))
        content_changed = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        assert content_changed != attached

        kb.source_version = 2
        await session.flush((kb,))
        version_changed = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        assert version_changed != content_changed

        await session.delete(kb)
        await session.flush()
        unlinked = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        assert unlinked == baseline

    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_subject_architecture_includes_diagram_payload_hashes(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-architecture.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

    async with factory() as session, session.begin():
        board_id, ideation_id, _revision, _binding = await _seed_semantic_authority(
            session
        )
        baseline = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        design = ArchitectureDesign(
            id="architecture-semantic-1",
            board_id=board_id,
            parent_type="ideation",
            ideation_id=ideation_id,
            title="Hexagonal boundaries",
            global_description="Domain in the center; adapters outside.",
            entities=[{"id": "domain", "description": "Business rules"}],
            interfaces=[{"id": "port", "description": "Inbound port"}],
            diagrams=[
                {
                    "id": "diagram-1",
                    "title": "Ports and adapters",
                    "adapter_payload_ref": "payload-1",
                }
            ],
            version=1,
            source_ref="architecture-template",
            source_version=1,
            source_design_id=None,
            created_by="architect",
        )
        session.add(design)
        await session.flush((design,))
        payload = ArchitectureDiagramPayload(
            id="payload-semantic-1",
            design_id=design.id,
            diagram_id="diagram-1",
            board_id=board_id,
            storage_backend="database",
            storage_key=f"architecture/{design.id}/diagram-1",
            format="json",
            adapter_payload_json={"nodes": [{"id": "domain"}]},
            payload_text=None,
            content_hash=canonical_sha256({"nodes": [{"id": "domain"}]}),
            size_bytes=30,
        )
        session.add(payload)
        await session.flush((payload,))
        attached = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        assert attached != baseline

        payload.content_hash = canonical_sha256(
            {"nodes": [{"id": "domain"}, {"id": "adapter"}]}
        )
        await session.flush((payload,))
        payload_changed = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )
        assert payload_changed != attached

    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_subject_mockup_tracks_authored_and_lineage_fields(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-mockup.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

    async with factory() as session, session.begin():
        board_id, ideation_id, _revision, _binding = await _seed_semantic_authority(
            session
        )
        ideation = await session.get(Ideation, ideation_id)
        assert ideation is not None
        mockup = {
            "id": "mockup-semantic-1",
            "title": "Policy assessment",
            "description": "Cognitive metric entry.",
            "screen_type": "modal",
            "html_content": "<main>Assessment</main>",
            "annotations": [{"target": "score", "text": "0–100"}],
            "order": 1,
            "version": 1,
            "source_ref": "design-system",
            "source_id": "source-1",
            "source_version": 1,
            "source_mockup_id": "source-mockup-1",
            "root_source_mockup_id": "root-mockup-1",
            "immediate_parent_mockup_id": "parent-mockup-1",
            "origin": "native",
        }
        ideation.screen_mockups = [mockup]
        await session.flush((ideation,))
        previous = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )

        changes = (
            ("html_content", "<main>Updated assessment</main>"),
            (
                "annotations",
                [{"target": "confidence", "text": "Required"}],
            ),
            ("order", 2),
            ("source_ref", "revised-design-system"),
        )
        for field, value in changes:
            updated = deepcopy(ideation.screen_mockups)
            assert updated is not None
            updated[0][field] = value
            ideation.screen_mockups = updated
            await session.flush((ideation,))
            current = await _record_subject_digest(
                session,
                board_id=board_id,
                ideation_id=ideation_id,
            )
            assert current != previous
            previous = current

    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_subject_ignores_timestamps_actors_and_quality_flags(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-volatile.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

    async with factory() as session, session.begin():
        board_id, ideation_id, _revision, _binding = await _seed_semantic_authority(
            session
        )
        ideation = await session.get(Ideation, ideation_id)
        assert ideation is not None
        ideation.screen_mockups = [
            {
                "id": "mockup-volatile-1",
                "title": "Stable mockup",
                "description": "Authored content.",
                "screen_type": "screen",
                "html_content": "<main>Stable</main>",
                "annotations": [],
                "order": 1,
                "version": 1,
                "origin": "native",
                "updated_at": "2026-01-01T00:00:00Z",
                "updated_by": "first-actor",
                "quality_score": 10,
            }
        ]
        question = IdeationQAItem(
            id="qa-volatile-1",
            ideation_id=ideation_id,
            question="Is the domain isolated?",
            question_type="text",
            choices=[],
            allow_free_text=True,
            answer="Yes.",
            selected=[],
            asked_by="first-questioner",
            answered_by="first-reviewer",
            revision=1,
            lifecycle="active",
            tombstoned=False,
        )
        kb = IdeationKnowledgeBase(
            id="kb-volatile-1",
            ideation_id=ideation_id,
            title="Stable knowledge",
            description=None,
            content="Authored knowledge.",
            mime_type="text/markdown",
            source_version=1,
            governance_metadata={
                "classification": "architecture",
                "updated_at": "2026-01-01T00:00:00Z",
                "updated_by": "first-actor",
                "quality_score": 10,
                "quality_findings": ["old"],
            },
            created_by="first-author",
        )
        design = ArchitectureDesign(
            id="architecture-volatile-1",
            board_id=board_id,
            parent_type="ideation",
            ideation_id=ideation_id,
            title="Stable architecture",
            global_description="Authored architecture.",
            entities=[],
            interfaces=[],
            diagrams=[],
            version=1,
            stale=False,
            breaking_change_flag=False,
            requires_arch_review=False,
            created_by="first-architect",
        )
        session.add_all([question, kb, design])
        await session.flush()
        baseline = await _record_subject_digest(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
        )

        question.asked_by = "second-questioner"
        question.answered_by = "second-reviewer"
        question.answered_at = _now()
        kb.created_by = "second-author"
        kb.updated_at = _now()
        kb.governance_metadata = {
            "classification": "architecture",
            "updated_at": "2026-07-30T12:00:00Z",
            "updated_by": "second-actor",
            "quality_score": 100,
            "quality_findings": ["new"],
        }
        design.created_by = "second-architect"
        design.updated_at = _now()
        design.stale = True
        design.breaking_change_flag = True
        design.requires_arch_review = True
        updated_mockups = deepcopy(ideation.screen_mockups)
        assert updated_mockups is not None
        updated_mockups[0].update(
            {
                "updated_at": "2026-07-30T12:00:00Z",
                "updated_by": "second-actor",
                "quality_score": 100,
            }
        )
        ideation.screen_mockups = updated_mockups
        await session.flush()

        assert (
            await _record_subject_digest(
                session,
                board_id=board_id,
                ideation_id=ideation_id,
            )
            == baseline
        )

    await engine.dispose()









@pytest.mark.asyncio
async def test_semantic_schema_rejects_incompatible_binding_and_mutation(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-guards.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    async with factory() as session, session.begin():
        board_id, _ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=1
        )
        semantic_row = await session.get(
            SemanticGuidelineRevisionRow,
            revision.revision_id,
        )
        assert semantic_row is not None
        with pytest.raises(
            IntegrityError,
            match="semantic_guideline_revision_immutable",
        ):
            await session.execute(
                text(
                    "UPDATE semantic_guideline_revisions "
                    "SET metrics = '[]' WHERE revision_id = :revision_id"
                ),
                {"revision_id": revision.revision_id},
            )

    async with factory() as session, session.begin():
        with pytest.raises(
            IntegrityError, match="semantic_guideline_binding_configuration_invalid"
        ):
            async with session.begin_nested():
                session.add(
                    SemanticGuidelineBindingConfigurationRow(
                        binding_id=binding.binding_id,
                        binding_revision=binding.binding_revision,
                        board_id=board_id,
                        guideline_id=revision.guideline_id,
                        revision_id=revision.revision_id,
                        revision_digest="0" * 64,
                        enforcement="blocking",
                        minimum_confidence=80,
                        metric_threshold_overrides={},
                        configuration_digest=binding.configuration_digest,
                        configured_by="board-owner",
                        configured_at=_now(),
                    )
                )
                await session.flush()

    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_waiver_lifecycle_replay_nullable_expiry_and_listing(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-waiver-lifecycle.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    evidence = (
        EvidenceRef(
            source_type="ideation",
            source_id="waiver-review",
            source_version=1,
            content_hash=canonical_sha256({"waiver": "evidence"}),
        ),
    )
    async with factory() as session, session.begin():
        board_id, ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=1
        )
        snapshot, _result, findings = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision,
            binding=binding,
            idempotency_key="waiver-assessment-1",
        )
        assert len(findings) == 1
        anchor = SemanticMetricWaiverAnchor.from_finding(
            findings[0],
            assessment_assessor_id="independent-reviewer",
        )
        requested = request_semantic_metric_waiver(
            waiver_id=canonical_sha256({"waiver": 1}),
            event_id=canonical_sha256({"waiver": 1, "revision": 1}),
            anchor=anchor,
            justification="A bounded exception is required for this delivery.",
            evidence_refs=evidence,
            requested_by="requester",
            requested_at=_now(),
            expires_at=None,
            idempotency_key="waiver-request-1",
        )
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        saved_request = await adapter.save_semantic_metric_waiver_mutation(
            mutation=requested
        )
        assert saved_request == requested
        assert (
            await adapter.save_semantic_metric_waiver_mutation(mutation=requested)
            == requested
        )
        assert saved_request.waiver.expires_at is None

        duplicate = request_semantic_metric_waiver(
            waiver_id=canonical_sha256({"waiver": "duplicate"}),
            event_id=canonical_sha256({"waiver": "duplicate", "revision": 1}),
            anchor=anchor,
            justification="The exact active scope must remain exclusive.",
            evidence_refs=evidence,
            requested_by="another-requester",
            requested_at=_now(),
            expires_at=None,
            idempotency_key="waiver-request-duplicate",
        )
        with pytest.raises(
            GuidelinePolicyDigestConflict,
            match="semantic_waiver_scope_conflict",
        ):
            await adapter.save_semantic_metric_waiver_mutation(mutation=duplicate)

        approved = transition_semantic_metric_waiver(
            requested.waiver,
            event_id=canonical_sha256({"waiver": 1, "revision": 2}),
            expected_waiver_revision=1,
            event_type=SemanticMetricWaiverEventType.APPROVE,
            actor_id="waiver-reviewer",
            occurred_at=_now(),
            reason="Evidence supports a temporary exception.",
            evidence_refs=evidence,
            idempotency_key="waiver-approve-1",
        )
        saved_approval = await adapter.save_semantic_metric_waiver_mutation(
            mutation=approved
        )
        assert saved_approval == approved
        assert (
            await adapter.get_semantic_waiver_by_idempotency(
                board_id=board_id,
                idempotency_key="waiver-request-1",
            )
            == requested
        )
        assert (
            await adapter.get_semantic_waiver_by_idempotency(
                board_id=board_id,
                idempotency_key="waiver-approve-1",
            )
            == approved
        )

        revalidated_current = revalidate_semantic_metric_waiver(
            approved.waiver,
            event_id=canonical_sha256({"waiver": 1, "revision": 3}),
            expected_waiver_revision=2,
            actor_id="currentness-reviewer",
            occurred_at=_now(),
            evaluated_at=_now(),
            status=SemanticMetricWaiverRevalidationStatus.APPROVED,
            reason_code=SemanticMetricWaiverRevalidationReason.CURRENT,
            currentness_reasons=(),
            scheduled_expiry_observed=False,
            evidence_refs=evidence,
            idempotency_key="waiver-revalidate-current-1",
        )
        assert (
            await adapter.save_semantic_metric_waiver_mutation(
                mutation=revalidated_current
            )
            == revalidated_current
        )

        revoked = transition_semantic_metric_waiver(
            revalidated_current.waiver,
            event_id=canonical_sha256({"waiver": 1, "revision": 4}),
            expected_waiver_revision=3,
            event_type=SemanticMetricWaiverEventType.REVOKE,
            actor_id="board-owner",
            occurred_at=_now(),
            reason="The delivery exception is no longer necessary.",
            evidence_refs=evidence,
            idempotency_key="waiver-revoke-1",
        )
        assert (
            await adapter.save_semantic_metric_waiver_mutation(mutation=revoked)
            == revoked
        )
        waiver_events, event_cursor = await adapter.list_semantic_waiver_events(
            board_id=board_id,
            waiver_id=revoked.waiver.waiver_id,
            limit=2,
        )
        assert waiver_events == (
            revoked.event,
            revalidated_current.event,
        )
        assert event_cursor is not None
        older_events, final_event_cursor = await adapter.list_semantic_waiver_events(
            board_id=board_id,
            waiver_id=revoked.waiver.waiver_id,
            after=event_cursor,
            limit=2,
        )
        assert older_events == (approved.event, requested.event)
        assert final_event_cursor is None
        assert (
            await adapter.get_semantic_waiver_event(
                board_id=board_id,
                event_id=approved.event.event_id,
            )
            == approved.event
        )
        assert (
            await adapter.get_semantic_waiver(
                board_id=board_id,
                waiver_id=revoked.waiver.waiver_id,
            )
            == revoked.waiver
        )
        assert (
            await adapter.get_semantic_waiver_by_idempotency(
                board_id=board_id,
                idempotency_key="waiver-revalidate-current-1",
            )
            == revalidated_current
        )

        revalidated_revoked = revalidate_semantic_metric_waiver(
            revoked.waiver,
            event_id=canonical_sha256({"waiver": 1, "revision": 5}),
            expected_waiver_revision=4,
            actor_id="revocation-currentness-reviewer",
            occurred_at=_now(),
            evaluated_at=_now(),
            status=SemanticMetricWaiverRevalidationStatus.REVOKED,
            reason_code=SemanticMetricWaiverRevalidationReason.REVOKED,
            currentness_reasons=(),
            scheduled_expiry_observed=False,
            evidence_refs=evidence,
            idempotency_key="waiver-revalidate-revoked-1",
        )
        assert (
            await adapter.save_semantic_metric_waiver_mutation(
                mutation=revalidated_revoked
            )
            == revalidated_revoked
        )

        second = request_semantic_metric_waiver(
            waiver_id=canonical_sha256({"waiver": 2}),
            event_id=canonical_sha256({"waiver": 2, "revision": 1}),
            anchor=anchor,
            justification="A new independently traceable request.",
            evidence_refs=evidence,
            requested_by="requester",
            requested_at=_now() + timedelta(microseconds=1),
            expires_at=None,
            idempotency_key="waiver-request-2",
        )
        assert (
            await adapter.save_semantic_metric_waiver_mutation(mutation=second)
            == second
        )
        first_page, cursor = await adapter.list_board_semantic_waivers(
            board_id=board_id,
            evaluated_at=_now(),
            guideline_id=revision.guideline_id,
            limit=1,
        )
        assert first_page == (second.waiver,)
        assert cursor is not None
        second_page, final_cursor = await adapter.list_board_semantic_waivers(
            board_id=board_id,
            evaluated_at=_now(),
            guideline_id=revision.guideline_id,
            after=cursor,
            limit=1,
        )
        assert second_page == (revalidated_revoked.waiver,)
        assert final_cursor is None
        (
            exact_anchor_page,
            exact_anchor_cursor,
        ) = await adapter.list_board_semantic_waivers(
            board_id=board_id,
            evaluated_at=_now(),
            finding_id=anchor.finding_id,
            metric_result_id=anchor.metric_result_id,
            receipt_id=anchor.receipt_id,
            guideline_id=anchor.guideline_id,
            binding_id=anchor.binding_id,
            metric_id=anchor.metric_id,
            entity_type=anchor.subject.entity_type,
            subject_id=anchor.subject.subject_id,
            status=revoked.waiver.status,
            limit=200,
        )
        assert exact_anchor_page == (revalidated_revoked.waiver,)
        assert exact_anchor_cursor is None
        wrong_metric_result_page, _ = await adapter.list_board_semantic_waivers(
            board_id=board_id,
            evaluated_at=_now(),
            metric_result_id="missing-metric-result",
            limit=200,
        )
        assert wrong_metric_result_page == ()
        with pytest.raises(ValueError, match="semantic_waiver_limit_invalid"):
            await adapter.list_board_semantic_waivers(
                board_id=board_id,
                evaluated_at=_now(),
                limit=201,
            )

    async with factory() as session, session.begin():
        with pytest.raises(
            IntegrityError,
            match="semantic_guideline_waiver_event_immutable",
        ):
            await session.execute(
                text(
                    "UPDATE semantic_guideline_waiver_events "
                    "SET reason = 'rewritten' WHERE event_id = :event_id"
                ),
                {"event_id": requested.event.event_id},
            )

    assert snapshot.subject.subject_id == ideation_id
    await engine.dispose()


@pytest.mark.parametrize("approve_competitor", [False, True])
@pytest.mark.asyncio
async def test_expired_waiver_revalidation_never_conflicts_or_reactivates(
    tmp_path,
    approve_competitor,
):
    engine = _sqlite_engine(
        tmp_path / f"semantic-waiver-revalidate-race-{approve_competitor}.db"
    )
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    evidence = (
        EvidenceRef(
            source_type="spec",
            source_id="waiver-revalidation-race",
            source_version=1,
            content_hash=canonical_sha256({"waiver": "race-evidence"}),
        ),
    )
    async with factory() as session, session.begin():
        board_id, ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=1
        )
        _snapshot, _result, findings = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision,
            binding=binding,
            idempotency_key=(f"waiver-race-assessment-{approve_competitor}"),
        )
        anchor = SemanticMetricWaiverAnchor.from_finding(
            findings[0],
            assessment_assessor_id="independent-reviewer",
        )
        started_at = _now()
        scheduled_expiry = started_at + timedelta(hours=1)
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        first = request_semantic_metric_waiver(
            waiver_id=canonical_sha256(
                {"waiver": "race-first", "approved": approve_competitor}
            ),
            event_id=canonical_sha256(
                {
                    "waiver": "race-first",
                    "revision": 1,
                    "approved": approve_competitor,
                }
            ),
            anchor=anchor,
            justification="First bounded exception.",
            evidence_refs=evidence,
            requested_by="first-requester",
            requested_at=started_at,
            expires_at=scheduled_expiry,
            idempotency_key=f"waiver-race-first-{approve_competitor}",
        )
        await adapter.save_semantic_metric_waiver_mutation(mutation=first)
        first_approved = transition_semantic_metric_waiver(
            first.waiver,
            event_id=canonical_sha256(
                {
                    "waiver": "race-first",
                    "revision": 2,
                    "approved": approve_competitor,
                }
            ),
            expected_waiver_revision=1,
            event_type=SemanticMetricWaiverEventType.APPROVE,
            actor_id="first-reviewer",
            occurred_at=started_at + timedelta(minutes=1),
            reason="Approve the first bounded exception.",
            evidence_refs=evidence,
            idempotency_key=f"waiver-race-first-approve-{approve_competitor}",
        )
        await adapter.save_semantic_metric_waiver_mutation(mutation=first_approved)
        projection = CommunitySqlAlchemyPolicyConstraintProjection()
        rebuild_desired = await projection._desired_for_board(  # noqa: SLF001
            session,
            board_id=board_id,
            projected_at=scheduled_expiry + timedelta(microseconds=1),
        )
        projected_waiver = next(
            node
            for node in rebuild_desired
            if node.kind == "waiver" and node.node_id.endswith(first.waiver.waiver_id)
        )
        assert projected_waiver.active is False
        assert projected_waiver.reason == "semantic_guideline_waiver_scheduled_expiry"
        first_expired = transition_semantic_metric_waiver(
            first_approved.waiver,
            event_id=canonical_sha256(
                {
                    "waiver": "race-first",
                    "revision": 3,
                    "approved": approve_competitor,
                }
            ),
            expected_waiver_revision=2,
            event_type=SemanticMetricWaiverEventType.EXPIRE,
            actor_id="expiry-worker",
            occurred_at=scheduled_expiry,
            reason="The approved exception reached its scheduled expiry.",
            evidence_refs=evidence,
            expire_reason=(SemanticMetricWaiverExpireReason.SCHEDULED_EXPIRY),
            idempotency_key=f"waiver-race-first-expire-{approve_competitor}",
        )
        await adapter.save_semantic_metric_waiver_mutation(mutation=first_expired)

        second_requested_at = scheduled_expiry + timedelta(minutes=1)
        second = request_semantic_metric_waiver(
            waiver_id=canonical_sha256(
                {"waiver": "race-second", "approved": approve_competitor}
            ),
            event_id=canonical_sha256(
                {
                    "waiver": "race-second",
                    "revision": 1,
                    "approved": approve_competitor,
                }
            ),
            anchor=anchor,
            justification="A new request now owns the exact scope.",
            evidence_refs=evidence,
            requested_by="second-requester",
            requested_at=second_requested_at,
            expires_at=None,
            idempotency_key=f"waiver-race-second-{approve_competitor}",
        )
        await adapter.save_semantic_metric_waiver_mutation(mutation=second)
        competitor = second
        if approve_competitor:
            competitor = transition_semantic_metric_waiver(
                second.waiver,
                event_id=canonical_sha256(
                    {
                        "waiver": "race-second",
                        "revision": 2,
                        "approved": approve_competitor,
                    }
                ),
                expected_waiver_revision=1,
                event_type=SemanticMetricWaiverEventType.APPROVE,
                actor_id="second-reviewer",
                occurred_at=second_requested_at + timedelta(minutes=1),
                reason="Approve the replacement exception.",
                evidence_refs=evidence,
                idempotency_key=(f"waiver-race-second-approve-{approve_competitor}"),
            )
            await adapter.save_semantic_metric_waiver_mutation(mutation=competitor)

        revalidation_at = second_requested_at + timedelta(minutes=2)
        revalidated = revalidate_semantic_metric_waiver(
            first_expired.waiver,
            event_id=canonical_sha256(
                {
                    "waiver": "race-first",
                    "revision": 4,
                    "approved": approve_competitor,
                }
            ),
            expected_waiver_revision=3,
            actor_id="revalidation-reviewer",
            occurred_at=revalidation_at,
            evaluated_at=revalidation_at,
            status=SemanticMetricWaiverRevalidationStatus.EXPIRED,
            reason_code=(SemanticMetricWaiverRevalidationReason.SCHEDULED_EXPIRY),
            currentness_reasons=(),
            scheduled_expiry_observed=True,
            evidence_refs=evidence,
            idempotency_key=(f"waiver-race-first-revalidate-{approve_competitor}"),
        )
        saved = await adapter.save_semantic_metric_waiver_mutation(mutation=revalidated)
        assert saved == revalidated
        assert saved.waiver.status.value == "expired"
        assert saved.waiver.expires_at == scheduled_expiry
        assert competitor.waiver.status.value in {
            "requested",
            "approved",
        }
        assert (
            await adapter.get_semantic_waiver_by_idempotency(
                board_id=board_id,
                idempotency_key=(f"waiver-race-first-revalidate-{approve_competitor}"),
            )
            == revalidated
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_anchor_stale_revalidation_round_trips_complete_decision(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-waiver-revalidate-free.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    evidence = (
        EvidenceRef(
            source_type="spec",
            source_id="waiver-revalidation-free",
            source_version=1,
            content_hash=canonical_sha256({"waiver": "free-evidence"}),
        ),
    )
    async with factory() as session, session.begin():
        board_id, ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=1
        )
        _snapshot, _result, findings = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision,
            binding=binding,
            idempotency_key="waiver-free-assessment",
        )
        anchor = SemanticMetricWaiverAnchor.from_finding(
            findings[0],
            assessment_assessor_id="independent-reviewer",
        )
        started_at = _now()
        scheduled_expiry = started_at + timedelta(hours=1)
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        requested = request_semantic_metric_waiver(
            waiver_id=canonical_sha256({"waiver": "free"}),
            event_id=canonical_sha256({"waiver": "free", "revision": 1}),
            anchor=anchor,
            justification="A free-scope bounded exception.",
            evidence_refs=evidence,
            requested_by="requester",
            requested_at=started_at,
            expires_at=scheduled_expiry,
            idempotency_key="waiver-free-request",
        )
        await adapter.save_semantic_metric_waiver_mutation(mutation=requested)
        approved = transition_semantic_metric_waiver(
            requested.waiver,
            event_id=canonical_sha256({"waiver": "free", "revision": 2}),
            expected_waiver_revision=1,
            event_type=SemanticMetricWaiverEventType.APPROVE,
            actor_id="waiver-reviewer",
            occurred_at=started_at + timedelta(minutes=1),
            reason="Approve the free-scope exception.",
            evidence_refs=evidence,
            idempotency_key="waiver-free-approve",
        )
        await adapter.save_semantic_metric_waiver_mutation(mutation=approved)
        expired = transition_semantic_metric_waiver(
            approved.waiver,
            event_id=canonical_sha256({"waiver": "free", "revision": 3}),
            expected_waiver_revision=2,
            event_type=SemanticMetricWaiverEventType.EXPIRE,
            actor_id="expiry-worker",
            occurred_at=scheduled_expiry,
            reason="The exception reached its scheduled expiry.",
            evidence_refs=evidence,
            expire_reason=(SemanticMetricWaiverExpireReason.SCHEDULED_EXPIRY),
            idempotency_key="waiver-free-expire",
        )
        await adapter.save_semantic_metric_waiver_mutation(mutation=expired)
        revalidation_at = scheduled_expiry + timedelta(minutes=1)
        revalidated = revalidate_semantic_metric_waiver(
            expired.waiver,
            event_id=canonical_sha256({"waiver": "free", "revision": 4}),
            expected_waiver_revision=3,
            actor_id="revalidation-reviewer",
            occurred_at=revalidation_at,
            evaluated_at=revalidation_at,
            status=SemanticMetricWaiverRevalidationStatus.ANCHOR_STALE,
            reason_code=(SemanticMetricWaiverRevalidationReason.SUBJECT_SCOPE_CHANGED),
            currentness_reasons=(
                SemanticAssessmentCurrentnessReason.SUBJECT_VERSION_CHANGED,
            ),
            scheduled_expiry_observed=True,
            evidence_refs=evidence,
            idempotency_key="waiver-free-revalidate",
        )
        saved = await adapter.save_semantic_metric_waiver_mutation(mutation=revalidated)
        assert saved == revalidated
        assert saved.waiver.status.value == "expired"
        assert (
            saved.waiver.last_revalidation_status
            is SemanticMetricWaiverRevalidationStatus.ANCHOR_STALE
        )
        assert saved.event.currentness_reasons == (
            SemanticAssessmentCurrentnessReason.SUBJECT_VERSION_CHANGED,
        )
        assert (
            await adapter.get_semantic_waiver_by_idempotency(
                board_id=board_id,
                idempotency_key="waiver-free-revalidate",
            )
            == revalidated
        )

    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_list_limits_share_closed_one_to_two_hundred_contract(
    tmp_path,
):
    from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import CommunitySqlAlchemySemanticGuidelineAssessmentV2
    engine = _sqlite_engine(tmp_path / "semantic-list-limits.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

    async with factory() as session, session.begin():
        board_id = _id()
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        native = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
        calls = (
            (
                native.list_semantic_assessment_v2_receipts,
                "semantic_assessment_receipt_limit_invalid",
            ),
            (
                native.list_semantic_findings_v2,
                "semantic_finding_limit_invalid",
            ),
            (
                adapter.list_semantic_waiver_events,
                "semantic_waiver_event_limit_invalid",
            ),
            (
                adapter.list_board_semantic_waivers,
                "semantic_waiver_limit_invalid",
            ),
            (
                adapter.list_semantic_policy_skips,
                "semantic_skip_limit_invalid",
            ),
            (
                adapter.list_semantic_skip_events,
                "semantic_skip_event_limit_invalid",
            ),
        )
        for list_method, error_code in calls:
            kwargs = {"board_id": board_id, "limit": 200}
            if list_method.__name__ == "list_board_semantic_waivers":
                kwargs["evaluated_at"] = _now()
            page, cursor = await list_method(**kwargs)
            assert page == ()
            assert cursor is None
            with pytest.raises(ValueError, match=error_code):
                kwargs["limit"] = 201
                await list_method(**kwargs)
        with pytest.raises(TypeError, match="guideline_id"):
            await adapter.list_semantic_policy_skips(
                board_id=board_id,
                guideline_id="unsupported-filter",
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_skip_is_append_only_revocable_lifecycle(tmp_path):
    engine = _sqlite_engine(tmp_path / "semantic-skip-lifecycle.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    async with factory() as session, session.begin():
        board_id, ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=1
        )
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        snapshot = await adapter.record_semantic_subject_mutation(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            actor_id="artifact-author",
            idempotency_key="skip-subject-1",
            request_digest=canonical_sha256({"skip_subject": 1}),
            changed_at=_now(),
        )
        scope = SemanticPolicySkipScope.from_authority(
            subject_snapshot=snapshot,
            binding=binding,
            revision=revision,
        )
        created = create_semantic_policy_skip(
            skip_id=canonical_sha256({"skip": "lifecycle"}),
            event_id=canonical_sha256({"skip": "lifecycle", "revision": 1}),
            scope=scope,
            reason="Human explicitly accepts this semantic exception.",
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=_now(),
            idempotency_key="skip-create-1",
        )
        saved = await adapter.save_semantic_policy_skip_mutation(mutation=created)
        assert saved == created
        replay = await adapter.save_semantic_policy_skip_mutation(mutation=created)
        assert replay == created

    async with factory() as session, session.begin():
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        current = await adapter.get_active_semantic_skip(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            subject_version=snapshot.subject.subject_version,
            subject_content_digest=snapshot.content_digest,
            binding_id=binding.binding_id,
            binding_revision=binding.binding_revision,
            configuration_digest=binding.configuration_digest,
            guideline_id=revision.guideline_id,
            revision_id=revision.revision_id,
            revision_digest=revision.revision_digest,
            subject_edition=snapshot.subject.subject_edition,
        )
        assert current == created.skip
        listed, listed_cursor = await adapter.list_semantic_policy_skips(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            status=SemanticPolicySkipStatus.ACTIVE,
            limit=1,
        )
        assert listed == (created.skip,)
        assert listed_cursor is None
        assert (
            await adapter.get_semantic_skip(
                board_id=board_id,
                skip_id=created.skip.skip_id,
            )
            == created.skip
        )
        replay = await adapter.get_semantic_skip_event_by_idempotency(
            board_id=board_id,
            idempotency_key="skip-create-1",
        )
        assert replay == created
        revoked = revoke_semantic_policy_skip(
            current,
            event_id=canonical_sha256({"skip": "lifecycle", "revision": 2}),
            expected_skip_revision=current.skip_revision,
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=_now(),
            reason="The board now requires an independent assessment.",
            idempotency_key="skip-revoke-1",
        )
        saved_revoke = await adapter.save_semantic_policy_skip_mutation(
            mutation=revoked
        )
        assert saved_revoke == revoked
        assert (
            await adapter.get_semantic_skip_event_by_idempotency(
                board_id=board_id,
                idempotency_key="skip-revoke-1",
            )
            == revoked
        )
        assert (
            await adapter.get_active_semantic_skip(
                board_id=board_id,
                entity_type=PolicyEntityType.IDEATION,
                subject_id=ideation_id,
                subject_version=snapshot.subject.subject_version,
                subject_content_digest=snapshot.content_digest,
                binding_id=binding.binding_id,
                binding_revision=binding.binding_revision,
                configuration_digest=binding.configuration_digest,
                guideline_id=revision.guideline_id,
                revision_id=revision.revision_id,
                revision_digest=revision.revision_digest,
                subject_edition=snapshot.subject.subject_edition,
            )
            is None
        )
        current_heads, current_cursor = await adapter.list_semantic_policy_skips(
            board_id=board_id,
            status=SemanticPolicySkipStatus.REVOKED,
            limit=1,
        )
        assert current_heads == (revoked.skip,)
        assert current_cursor is None
        event_page, event_cursor = await adapter.list_semantic_skip_events(
            board_id=board_id,
            skip_id=created.skip.skip_id,
            limit=1,
        )
        assert event_page == (revoked.event,)
        assert event_cursor is not None
        older_page, final_event_cursor = await adapter.list_semantic_skip_events(
            board_id=board_id,
            skip_id=created.skip.skip_id,
            after=event_cursor,
            limit=1,
        )
        assert older_page == (created.event,)
        assert final_event_cursor is None
        assert (
            await adapter.get_semantic_skip(
                board_id=board_id,
                skip_id=created.skip.skip_id,
            )
            == revoked.skip
        )

        conflicting_branch = revoke_semantic_policy_skip(
            current,
            event_id=canonical_sha256({"skip": "concurrent-branch"}),
            expected_skip_revision=current.skip_revision,
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=_now(),
            reason="Concurrent branch must fail.",
            idempotency_key="skip-revoke-branch",
        )
        with pytest.raises(
            GuidelinePolicyDigestConflict,
            match="semantic_skip_revision_conflict",
        ):
            await adapter.save_semantic_policy_skip_mutation(
                mutation=conflicting_branch
            )

    async with factory() as session, session.begin():
        with pytest.raises(
            IntegrityError,
            match="semantic_guideline_skip_immutable",
        ):
            await session.execute(
                text(
                    "UPDATE semantic_guideline_skips "
                    "SET reason = 'rewritten' WHERE event_id = :event_id"
                ),
                {"event_id": created.event.event_id},
            )

    async with factory() as session, session.begin():
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        ideation = await session.get(Ideation, ideation_id)
        assert ideation is not None
        ideation.description = "The semantic subject has changed."
        ideation.version += 1
        await session.flush((ideation,))
        stale = await adapter.record_semantic_subject_mutation(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            actor_id="artifact-author",
            idempotency_key="skip-subject-2",
            request_digest=canonical_sha256({"skip_subject": 2}),
            changed_at=_now(),
        )
        stale_scope = SemanticPolicySkipScope.from_authority(
            subject_snapshot=snapshot,
            binding=binding,
            revision=revision,
        )
        assert stale.content_digest != snapshot.content_digest
        stale_create = create_semantic_policy_skip(
            skip_id=canonical_sha256({"skip": "stale"}),
            event_id=canonical_sha256({"skip": "stale", "revision": 1}),
            scope=stale_scope,
            reason="A stale subject fence cannot be skipped.",
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=_now(),
            idempotency_key="skip-stale",
        )
        with pytest.raises(
            GuidelinePolicyDigestConflict,
            match="semantic_skip_scope_stale",
        ):
            await adapter.save_semantic_policy_skip_mutation(mutation=stale_create)

    await engine.dispose()


@pytest.mark.asyncio
async def test_semantic_skip_head_cursor_uses_immutable_creation_key(
    tmp_path,
):
    engine = _sqlite_engine(tmp_path / "semantic-skip-head-cursor.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    async with factory() as session, session.begin():
        board_id, ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=1
        )
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        snapshot = await adapter.record_semantic_subject_mutation(
            board_id=board_id,
            entity_type=PolicyEntityType.IDEATION,
            subject_id=ideation_id,
            actor_id="artifact-author",
            idempotency_key="skip-cursor-subject",
            request_digest=canonical_sha256({"skip_cursor_subject": 1}),
            changed_at=_now(),
        )
        scope = SemanticPolicySkipScope.from_authority(
            subject_snapshot=snapshot,
            binding=binding,
            revision=revision,
        )
        created_at = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)
        older = create_semantic_policy_skip(
            skip_id=canonical_sha256({"skip_cursor": "older"}),
            event_id=canonical_sha256({"skip_cursor": "older", "revision": 1}),
            scope=scope,
            reason="First exact human exception.",
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=created_at,
            idempotency_key="skip-cursor-older-create",
        )
        await adapter.save_semantic_policy_skip_mutation(mutation=older)
        older_revoked = revoke_semantic_policy_skip(
            older.skip,
            event_id=canonical_sha256({"skip_cursor": "older", "revision": 2}),
            expected_skip_revision=older.skip.skip_revision,
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=created_at + timedelta(hours=3),
            reason="Close the first exception before creating its successor.",
            idempotency_key="skip-cursor-older-revoke",
        )
        await adapter.save_semantic_policy_skip_mutation(mutation=older_revoked)
        newer = create_semantic_policy_skip(
            skip_id=canonical_sha256({"skip_cursor": "newer"}),
            event_id=canonical_sha256({"skip_cursor": "newer", "revision": 1}),
            scope=scope,
            reason="Second exact human exception.",
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=created_at + timedelta(hours=1),
            idempotency_key="skip-cursor-newer-create",
        )
        await adapter.save_semantic_policy_skip_mutation(mutation=newer)

        first_page, cursor = await adapter.list_semantic_policy_skips(
            board_id=board_id,
            limit=1,
        )
        assert first_page == (newer.skip,)
        assert cursor == (newer.skip.created_at, newer.skip.skip_id)
        second_page, final_cursor = await adapter.list_semantic_policy_skips(
            board_id=board_id,
            after=cursor,
            limit=1,
        )
        assert second_page == (older_revoked.skip,)
        assert final_cursor is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_board_erasure_purges_every_semantic_guideline_row(tmp_path):
    engine = _sqlite_engine(tmp_path / "semantic-board-erasure.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    await _install_semantic_triggers(engine)

    semantic_models = (
        SemanticGuidelineAssessmentV2Row,
        SemanticGuidelineBindingConfigurationRow,
        SemanticGuidelineFindingV2Row,
        SemanticGuidelineMetricResultV2Row,
        SemanticGuidelineSkipRow,
        SemanticGuidelineWaiverEventRow,
        SemanticGuidelineWaiverRow,
        SemanticSubjectVersionEventRow,
        SemanticSubjectVersionRow,
    )
    async with factory() as session, session.begin():
        board_id, ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=1
        )
        snapshot, _result, findings = await _record_failed_semantic_assessment(
            session,
            board_id=board_id,
            ideation_id=ideation_id,
            revision=revision,
            binding=binding,
            idempotency_key="erasure-assessment",
        )
        adapter = CommunitySqlAlchemySemanticGuidelineAssessment(session)
        waiver = request_semantic_metric_waiver(
            waiver_id=canonical_sha256({"erasure": "waiver"}),
            event_id=canonical_sha256({"erasure": "waiver", "revision": 1}),
            anchor=SemanticMetricWaiverAnchor.from_finding(
                findings[0],
                assessment_assessor_id="independent-reviewer",
            ),
            justification="Exercise the complete semantic erasure graph.",
            evidence_refs=(
                EvidenceRef(
                    source_type="ideation",
                    source_id=ideation_id,
                    source_version=snapshot.subject.subject_version,
                    content_hash=snapshot.content_digest,
                ),
            ),
            requested_by="requester",
            requested_at=_now(),
            expires_at=None,
            idempotency_key="erasure-waiver",
        )
        await adapter.save_semantic_metric_waiver_mutation(mutation=waiver)
        skip = create_semantic_policy_skip(
            skip_id=canonical_sha256({"erasure": "skip"}),
            event_id=canonical_sha256({"erasure": "skip", "revision": 1}),
            scope=SemanticPolicySkipScope.from_authority(
                subject_snapshot=snapshot,
                binding=binding,
                revision=revision,
            ),
            reason="Exercise permit-scoped skip erasure.",
            actor_id="board-owner",
            actor_kind=SemanticExceptionActorKind.HUMAN,
            occurred_at=_now(),
            idempotency_key="erasure-skip",
        )
        await adapter.save_semantic_policy_skip_mutation(mutation=skip)
        await session.flush()
        for model in semantic_models:
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(model)
                            .where(model.board_id == board_id)
                        )
                    ).scalar_one()
                )
                > 0
            )

        await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
            session,
            board_id=board_id,
        )

        for model in semantic_models:
            assert (
                int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(model)
                            .where(model.board_id == board_id)
                        )
                    ).scalar_one()
                )
                == 0
            )
        assert await session.get(Board, board_id) is not None

    await engine.dispose()
