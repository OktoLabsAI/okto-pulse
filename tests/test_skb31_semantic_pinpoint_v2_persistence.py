"""SK-B3.1 relational ledger and cross-database guard regressions."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import okto_pulse.community.adapters.relational_schema_steps as schema_steps
from okto_pulse.community.adapters.semantic_assessment_v2_capabilities import (
    CommunitySemanticAssessmentV2Capabilities,
)
from okto_pulse.community.adapters.relational_schema_steps import (
    semantic_pinpoint_v2_postgresql_ddl,
    semantic_pinpoint_v2_sqlite_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Base,
    SemanticGuidelineAssessmentV2Row,
    SemanticGuidelineFindingV2Row,
    SemanticGuidelineMetricResultV2Row,
)
from okto_pulse.community.adapters.sqlalchemy_policy_subject_versioning import (
    CommunitySemanticSession,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import (
    ASSESSMENT_CONTRACT_V2,
    FINDING_CONTRACT_V2,
    METRIC_RESULT_CONTRACT_V2,
    CommunitySqlAlchemySemanticGuidelineAssessmentV2,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_subject_projection import (
    CommunitySqlAlchemySemanticSubjectProjection,
)
from okto_pulse.core.domain.guideline_policy import PolicySubjectRef
from okto_pulse.core.domain.guideline_semantic_assessment import (
    SemanticAssessmentAssessor,
)
from okto_pulse.core.domain.guideline_semantic_v2 import (
    AnchorSnapshot,
    SemanticAnchorAvailability,
    SemanticAssessmentRequestV2,
    SemanticMetricAssessmentV2,
    SemanticPinpointKind,
    SemanticPinpointV2,
)
from okto_pulse.core.domain.quality_assessment import (
    EvidenceRef,
    FindingAnchorType,
    FindingSeverity,
    UnboundFindingAnchor,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.infra.config import configure_settings, get_settings
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyIdempotencyConflict,
)
from okto_pulse.core.ports.semantic_subject_projection import (
    SemanticAssessmentV2PersistencePort,
    SemanticAssessmentV2ReadPort,
    SemanticSubjectProjectionError,
    SemanticSubjectProjectionFailure,
    SemanticSubjectProjectionPort,
    SemanticSubjectProjectionRequest,
)

from test_skb3_semantic_guideline_persistence import (
    _seed_semantic_authority,
)


def _engine(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def _pinpoint(*, key: str, issue: bool) -> SemanticPinpointV2:
    excerpt = "Coupling makes change expensive."
    return SemanticPinpointV2(
        pinpoint_key=key,
        kind=(SemanticPinpointKind.ISSUE if issue else SemanticPinpointKind.EVIDENCE),
        title=("Separate the infrastructure concern" if issue else "Explicit problem"),
        detail=(
            "The proposed boundary still leaves persistence ownership implicit."
            if issue
            else "The problem statement identifies the concrete coupling cost."
        ),
        severity=FindingSeverity.HIGH if issue else None,
        remediation=("Name the outbound port and its owner." if issue else None),
        anchor=UnboundFindingAnchor(
            anchor_type=FindingAnchorType.FIELD,
            anchor_ref="problem_statement",
            excerpt_hash=canonical_sha256(excerpt),
        ),
        anchor_snapshot=AnchorSnapshot(
            label="Problem statement",
            excerpt=excerpt,
            source_version="ideation:1",
            availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
        ),
    )


def _request(board_id, ideation_id, revision, binding, *, key="v2-request"):
    evidence = EvidenceRef(
        source_type="ideation",
        source_id=ideation_id,
        source_version=1,
        content_hash=canonical_sha256({"subject": ideation_id}),
    )
    return SemanticAssessmentRequestV2(
        subject=PolicySubjectRef(
            board_id=board_id,
            entity_type=revision.metrics[0].target_entity_types[0],
            subject_id=ideation_id,
            subject_version=1,
        ),
        binding_id=binding.binding_id,
        expected_binding_revision=binding.binding_revision,
        guideline_revision_id=revision.revision_id,
        idempotency_key=key,
        confidence=95,
        assessor=SemanticAssessmentAssessor(
            agent_id="independent-reviewer",
            model_id="test-model",
        ),
        metric_results=(
            SemanticMetricAssessmentV2(
                metric_id=revision.metrics[0].metric_id,
                score=90,
                rationale="The boundary is explicit and independently verifiable.",
                evidence_refs=(evidence,),
                pinpoints=(_pinpoint(key="evidence-boundary", issue=False),),
            ),
            SemanticMetricAssessmentV2(
                metric_id=revision.metrics[1].metric_id,
                score=60,
                rationale="Persistence ownership remains implicit.",
                evidence_refs=(evidence,),
                pinpoints=(_pinpoint(key="issue-persistence", issue=True),),
            ),
        ),
    )


def test_v2_adapter_satisfies_public_core_persistence_port():
    adapter = CommunitySqlAlchemySemanticGuidelineAssessmentV2(
        object()  # type: ignore[arg-type]
    )
    assert isinstance(adapter, SemanticAssessmentV2PersistencePort)
    assert isinstance(adapter, SemanticAssessmentV2ReadPort)


def test_subject_projection_adapter_satisfies_public_core_port():
    assert isinstance(
        CommunitySqlAlchemySemanticSubjectProjection(
            object()  # type: ignore[arg-type]
        ),
        SemanticSubjectProjectionPort,
    )


@pytest.mark.asyncio
async def test_v2_capability_resolver_enforces_readers_first_and_runtime_probes(
    tmp_path,
):
    engine = _engine(tmp_path / "semantic-pinpoint-v2-capabilities.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    original_settings = get_settings()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        for _name, (_table, ddl) in (
            semantic_pinpoint_v2_sqlite_trigger_manifest().items()
        ):
            await connection.execute(text(ddl))

    try:
        async with factory() as session:
            adapter = CommunitySemanticAssessmentV2Capabilities(session)

            configure_settings(
                original_settings.model_copy(
                    update={
                        "semantic_assessment_v2_readers_ready": False,
                        "semantic_assessment_v2_writer_enabled": False,
                    }
                )
            )
            disabled = await adapter.semantic_assessment_v2_capabilities()
            assert disabled.storage_ready is True
            assert disabled.triggers_ready is True
            assert disabled.writer_active is False
            assert disabled.reason_code == "unsupported_contract_version"
            assert disabled.state == "disabled"

            configure_settings(
                original_settings.model_copy(
                    update={
                        "semantic_assessment_v2_readers_ready": False,
                        "semantic_assessment_v2_writer_enabled": True,
                    }
                )
            )
            readers_missing = await adapter.semantic_assessment_v2_capabilities()
            assert readers_missing.writer_active is False
            assert readers_missing.reason_code == "v2_writer_not_ready"
            assert readers_missing.state == "readers_not_ready"

            configure_settings(
                original_settings.model_copy(
                    update={
                        "semantic_assessment_v2_readers_ready": True,
                        "semantic_assessment_v2_writer_enabled": True,
                    }
                )
            )
            ready = await adapter.semantic_assessment_v2_capabilities()
            assert ready.writer_active is True
            assert ready.reason_code is None
            assert ready.state == "active"

        trigger = next(iter(semantic_pinpoint_v2_sqlite_trigger_manifest()))
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP TRIGGER "{trigger}"'))
        async with factory() as session:
            incomplete = await CommunitySemanticAssessmentV2Capabilities(
                session
            ).semantic_assessment_v2_capabilities()
            assert incomplete.triggers_ready is False
            assert incomplete.writer_active is False
            assert incomplete.reason_code == "v2_writer_not_ready"
            assert incomplete.state == "triggers_not_ready"
    finally:
        configure_settings(original_settings)
        await engine.dispose()


@pytest.mark.asyncio
async def test_subject_projection_resolves_human_field_and_denies_other_actor(
    tmp_path,
):
    engine = _engine(tmp_path / "semantic-pinpoint-v2-projection.db")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with factory() as session, session.begin():
        board_id, ideation_id, revision, _binding = (
            await _seed_semantic_authority(session, metric_count=1)
        )
        subject = PolicySubjectRef(
            board_id=board_id,
            entity_type=revision.metrics[0].target_entity_types[0],
            subject_id=ideation_id,
            subject_version=1,
        )
        anchor = UnboundFindingAnchor(
            anchor_type=FindingAnchorType.FIELD,
            anchor_ref="problem_statement",
        )
        adapter = CommunitySqlAlchemySemanticSubjectProjection(session)
        snapshot = await adapter.resolve_semantic_anchor(
            SemanticSubjectProjectionRequest(
                subject=subject,
                anchor=anchor,
                actor_id="board-owner",
            )
        )
        assert snapshot.label == "Problem Statement"
        assert snapshot.excerpt == "Coupling makes change expensive."
        assert snapshot.source_version == "1"

        with pytest.raises(SemanticSubjectProjectionError) as denied:
            await adapter.resolve_semantic_anchor(
                SemanticSubjectProjectionRequest(
                    subject=subject,
                    anchor=anchor,
                    actor_id="other-user",
                )
            )
        assert denied.value.reason is SemanticSubjectProjectionFailure.FORBIDDEN
    await engine.dispose()


def test_sqlite_and_postgresql_manifests_cover_parallel_v2_ledger():
    sqlite_manifest = semantic_pinpoint_v2_sqlite_trigger_manifest()
    function_sql, postgres_specs = semantic_pinpoint_v2_postgresql_ddl()

    assert {table for table, _ddl in sqlite_manifest.values()} == {
        "semantic_guideline_assessment_receipts",
        "semantic_guideline_assessments_v2",
        "semantic_guideline_metric_results_v2",
        "semantic_guideline_findings_v2",
    }
    assert {table for table, _operation, _kind in postgres_specs.values()} == {
        "semantic_guideline_assessment_receipts",
        "semantic_guideline_assessments_v2",
        "semantic_guideline_metric_results_v2",
        "semantic_guideline_findings_v2",
    }
    assert "semantic_assessment_idempotency_contract_conflict" in function_sql
    assert "semantic_pinpoint_v2_immutable" in function_sql
    assert all(len(name.encode("utf-8")) <= 63 for name in postgres_specs)


@pytest.mark.asyncio
async def test_v2_migration_is_idempotent_and_preserves_v1_shape(tmp_path, monkeypatch):
    engine = _engine(tmp_path / "semantic-pinpoint-v2-migration.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        before = tuple(
            row[1]
            for row in (
                await connection.exec_driver_sql(
                    'PRAGMA table_info("semantic_guideline_assessment_receipts")'
                )
            ).all()
        )
    monkeypatch.setattr(schema_steps, "get_engine", lambda: engine)

    assert await schema_steps._migrate_semantic_pinpoint_v2_schema() is None
    assert await schema_steps._migrate_semantic_pinpoint_v2_schema() == "skipped"

    async with engine.connect() as connection:
        after = tuple(
            row[1]
            for row in (
                await connection.exec_driver_sql(
                    'PRAGMA table_info("semantic_guideline_assessment_receipts")'
                )
            ).all()
        )
        triggers = {
            row[0]
            for row in (
                await connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE 'trg_semantic_pinpoint_v2%'"
                )
            ).all()
        }
    assert after == before
    assert triggers == set(semantic_pinpoint_v2_sqlite_trigger_manifest())
    await engine.dispose()


@pytest.mark.asyncio
async def test_v2_round_trip_findings_idempotency_and_immutability(tmp_path):
    engine = _engine(tmp_path / "semantic-pinpoint-v2-roundtrip.db")
    factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        sync_session_class=CommunitySemanticSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        for _name, (_table, ddl) in (
            semantic_pinpoint_v2_sqlite_trigger_manifest().items()
        ):
            await connection.execute(text(ddl))

    async with factory() as session, session.begin():
        board_id, ideation_id, revision, binding = await _seed_semantic_authority(
            session, metric_count=2
        )
        request = _request(board_id, ideation_id, revision, binding)
        adapter = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
        created = await adapter.save_semantic_assessment_v2(request)
        replayed = await adapter.save_semantic_assessment_v2(request)
        assert replayed == created
        projection = await adapter.get_semantic_assessment_v2(
            board_id=board_id, receipt_id=created.receipt_id
        )
        assert projection is not None
        assert projection.contract_version == 2
        by_metric = {item.metric_id: item for item in projection.metric_results}
        assert by_metric[revision.metrics[0].metric_id].outcome.value == "pass"
        failed = by_metric[revision.metrics[1].metric_id]
        assert failed.outcome.value == "fail"
        assert failed.pinpoints[0].anchor_snapshot.label == (
            "Problem statement"
        )
        assert failed.pinpoints[0].blocking_for(failed.outcome)
        current = await adapter.get_current_semantic_assessment_v2(
            board_id=board_id,
            entity_type="ideation",
            subject_id=ideation_id,
            binding_id=binding.binding_id,
        )
        assert current == projection
        with pytest.raises(GuidelinePolicyIdempotencyConflict):
            await adapter.save_semantic_assessment_v2(
                replace(request, confidence=94)
            )

    async with factory() as session:
        receipt = (
            await session.execute(select(SemanticGuidelineAssessmentV2Row))
        ).scalar_one()
        metrics = tuple(
            (await session.execute(select(SemanticGuidelineMetricResultV2Row)))
            .scalars()
            .all()
        )
        findings = tuple(
            (await session.execute(select(SemanticGuidelineFindingV2Row)))
            .scalars()
            .all()
        )
        assert receipt.contract_version == ASSESSMENT_CONTRACT_V2
        assert {item.contract_version for item in metrics} == {
            METRIC_RESULT_CONTRACT_V2
        }
        assert len(findings) == 1
        assert findings[0].contract_version == FINDING_CONTRACT_V2
        assert findings[0].payload["pinpoints"][0]["title"] == (
            "Separate the infrastructure concern"
        )

    async with engine.begin() as connection:
        with pytest.raises(Exception, match="semantic_assessment_v2_immutable"):
            await connection.exec_driver_sql(
                "UPDATE semantic_guideline_assessments_v2 SET confidence=99"
            )
        legacy_columns = tuple(
            row[1]
            for row in (
                await connection.exec_driver_sql(
                    'PRAGMA table_info("semantic_guideline_assessment_receipts")'
                )
            ).all()
        )
    assert "contract_version" not in legacy_columns
    await engine.dispose()
