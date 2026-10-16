"""Native semantic pinpoint persistence and SQLite guard regressions."""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    create_async_engine,
)

from okto_pulse.community.adapters.semantic_assessment_v2_capabilities import (
    CommunitySemanticAssessmentV2Capabilities,
)
from okto_pulse.community.adapters.current_schema_guards import (
    semantic_pinpoint_v2_sqlite_trigger_manifest,
)
from okto_pulse.community.adapters.sqlalchemy_models import (
    Ideation,
    Refinement,
    SemanticGuidelineAssessmentV2Row,
    SemanticGuidelineFindingV2Row,
    SemanticGuidelineMetricResultV2Row,
    Spec,
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
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.core.domain.guideline_policy import (
    PolicyEntityType,
    PolicySubjectRef,
)
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
from okto_pulse.core.ports.guideline_policy import (
    GuidelinePolicyEditionConflict,
    GuidelinePolicyIdempotencyConflict,
)
from okto_pulse.core.ports.semantic_subject_projection import (
    SemanticAssessmentV2PersistencePort,
    SemanticAssessmentV2ReadPort,
    SemanticFindingV2ReadPort,
    SemanticSubjectProjectionError,
    SemanticSubjectProjectionFailure,
    SemanticSubjectProjectionPort,
    SemanticSubjectProjectionRequest,
)

from okto_pulse.community.adapters.current_relational_schema import (
    current_schema_contract,
)
from okto_pulse.community.adapters.current_relational_schema import (
    initialize_current_schema,
)
from okto_pulse.community.adapters.sqlalchemy_database import (
    build_community_session_factory,
    install_community_sqlite_pragmas,
)

from test_skb3_semantic_guideline_persistence import (
    _now,
    _seed_semantic_authority,
)


def _engine(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    install_community_sqlite_pragmas(engine)

    return engine


def _pinpoint(
    *,
    key: str,
    issue: bool,
    entity_type: PolicyEntityType = PolicyEntityType.IDEATION,
) -> SemanticPinpointV2:
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
            source_version=f"{entity_type.value}:1",
            availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
        ),
    )


def _request(
    board_id,
    subject_id,
    revision,
    binding,
    *,
    key="v2-request",
    entity_type: PolicyEntityType = PolicyEntityType.IDEATION,
):
    evidence = EvidenceRef(
        source_type=entity_type.value,
        source_id=subject_id,
        source_version=1,
        content_hash=canonical_sha256({"subject": subject_id}),
    )
    return SemanticAssessmentRequestV2(
        subject=PolicySubjectRef(
            board_id=board_id,
            entity_type=entity_type,
            subject_id=subject_id,
            subject_version=1,
            subject_edition=1,
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
                pinpoints=(
                    _pinpoint(
                        key="evidence-boundary",
                        issue=False,
                        entity_type=entity_type,
                    ),
                ),
            ),
            SemanticMetricAssessmentV2(
                metric_id=revision.metrics[1].metric_id,
                score=60,
                rationale="Persistence ownership remains implicit.",
                evidence_refs=(evidence,),
                pinpoints=(
                    _pinpoint(
                        key="issue-persistence",
                        issue=True,
                        entity_type=entity_type,
                    ),
                ),
            ),
        ),
    )


def test_v2_adapter_satisfies_public_core_persistence_port():
    adapter = CommunitySqlAlchemySemanticGuidelineAssessmentV2(
        object()  # type: ignore[arg-type]
    )
    assert isinstance(adapter, SemanticAssessmentV2PersistencePort)
    assert isinstance(adapter, SemanticAssessmentV2ReadPort)
    assert isinstance(adapter, SemanticFindingV2ReadPort)


@pytest.mark.parametrize("method", ["get_semantic_metric_result_v2", "get_semantic_finding_v2", "list_semantic_findings_v2"])
def test_native_finding_read_signature_matches_public_port(method):
    assert inspect.signature(getattr(CommunitySqlAlchemySemanticGuidelineAssessmentV2, method)) == inspect.signature(
        getattr(SemanticFindingV2ReadPort, method)
    )


@pytest.mark.asyncio
async def test_native_finding_read_preserves_sealed_identity_and_board_scope(tmp_path):
    from okto_pulse.core.domain.guideline_semantic_findings_v2 import project_semantic_metric_findings_v2
    from okto_pulse.core.ports.guideline_policy import GuidelinePolicyDigestConflict

    engine = _engine(tmp_path / "native-finding-read.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            authority = await _seed_semantic_authority(session)
            adapter = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
            sealed = await adapter.save_semantic_assessment_v2(_request(*authority))
            finding = project_semantic_metric_findings_v2(sealed.receipt)[0]
            assert await adapter.get_semantic_finding_v2(board_id=authority[0], finding_id=finding.finding_id) == finding
            metric = await adapter.get_semantic_metric_result_v2(
                board_id=authority[0], metric_result_id=finding.metric_result_id,
            )
            assert metric in sealed.receipt.metric_results
            assert await adapter.get_semantic_finding_v2(board_id="other", finding_id=finding.finding_id) is None
            assert await adapter.get_semantic_metric_result_v2(board_id="other", metric_result_id=finding.metric_result_id) is None
            second = await adapter.save_semantic_assessment_v2(_request(*authority, key="second"))
            second_finding = project_semantic_metric_findings_v2(second.receipt)[0]
            page, cursor = await adapter.list_semantic_findings_v2(board_id=authority[0], limit=1)
            assert cursor is not None
            tail, end = await adapter.list_semantic_findings_v2(board_id=authority[0], limit=1, after=cursor)
            assert end is None
            assert {item.finding_id for item in (*page, *tail)} == {finding.finding_id, second_finding.finding_id}
            filtered, end = await adapter.list_semantic_findings_v2(
                board_id=authority[0], entity_type=finding.subject.entity_type,
                subject_id=finding.subject.subject_id, subject_edition=1,
                receipt_id=sealed.receipt_id, guideline_id=finding.guideline_id,
                binding_id=finding.binding_id, metric_id=finding.metric_id,
            )
            assert filtered == (finding,) and end is None
            assert await adapter.list_semantic_findings_v2(board_id="other") == ((), None)
            assert await adapter.list_semantic_findings_v2(board_id=authority[0], subject_edition=2) == ((), None)
            row = await session.get(SemanticGuidelineFindingV2Row, finding.finding_id)
            # Corrupt only the identity-map projection, bypassing neither SQL guards nor commits.
            with session.no_autoflush:
                row.finding_digest = "0" * 64
                with pytest.raises(GuidelinePolicyDigestConflict, match="semantic_finding_v2_receipt_mismatch"):
                    await adapter.get_semantic_finding_v2(board_id=authority[0], finding_id=finding.finding_id)
                row.finding_digest = finding.finding_digest
    finally:
        await engine.dispose()


@pytest.mark.parametrize("method", [
    "get_semantic_assessment_v2", "list_semantic_assessment_v2_receipts",
    "get_semantic_assessment_v2_currentness", "get_current_semantic_assessment_v2",
])
def test_v2_read_adapter_signature_matches_public_core_port(method) -> None:
    adapter_signature = inspect.signature(getattr(CommunitySqlAlchemySemanticGuidelineAssessmentV2, method))
    port_signature = inspect.signature(getattr(SemanticAssessmentV2ReadPort, method))
    assert adapter_signature == port_signature


@pytest.mark.asyncio
async def test_native_history_keyset_filters_board_and_preserves_previous_edition(tmp_path, monkeypatch):
    from datetime import datetime
    from okto_pulse.core.domain.guideline_semantic_assessment import SemanticAssessmentState
    from okto_pulse.community.adapters import sqlalchemy_semantic_guideline_v2 as module

    engine = _engine(tmp_path / "native-semantic-history.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            board_id, subject_id, revision, binding = await _seed_semantic_authority(session)
            adapter = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
            failed = _request(board_id, subject_id, revision, binding, key="failed")
            passed = replace(failed, idempotency_key="passed", metric_results=tuple(
                replace(metric, score=95) for metric in failed.metric_results
            ))

            recorded_at = _now()

            class FixedClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return recorded_at

            with monkeypatch.context() as clock:
                clock.setattr(module, "datetime", FixedClock)
                first = await adapter.save_semantic_assessment_v2(failed)
                second = await adapter.save_semantic_assessment_v2(passed)
                other = _request(*await _seed_semantic_authority(session), key="other-board")
                outsider = await adapter.save_semantic_assessment_v2(other)
            assert first.receipt.recorded_at == second.receipt.recorded_at
            assert (await adapter.get_semantic_assessment_v2_currentness(first.receipt)).is_current
            assert (await adapter.get_semantic_assessment_v2_currentness(second.receipt)).is_current
            filters = dict(board_id=board_id, entity_type=PolicyEntityType.IDEATION,
                           subject_id=subject_id, subject_edition=1,
                           binding_id=binding.binding_id, guideline_id=revision.guideline_id)
            page, cursor = await adapter.list_semantic_assessment_v2_receipts(**filters, limit=1)
            assert cursor is not None
            following, last = await adapter.list_semantic_assessment_v2_receipts(**filters, limit=1, after=cursor)
            ids = [item.receipt_id for item in (*page, *following)]
            assert ids == sorted([first.receipt_id, second.receipt_id], reverse=True)
            assert last is None and outsider.receipt_id not in ids
            for state, expected in ((SemanticAssessmentState.PASSED, second.receipt_id),
                                    (SemanticAssessmentState.METRIC_THRESHOLD_FAILED, first.receipt_id)):
                selected, end = await adapter.list_semantic_assessment_v2_receipts(**filters, outcome=state)
                assert [item.receipt_id for item in selected] == [expected]
                assert end is None
            assert await adapter.get_semantic_assessment_v2(board_id=board_id, receipt_id=outsider.receipt_id) is None
            subject = await session.get(Ideation, subject_id)
            subject.edition = 2
            await session.flush((subject,))
            await CommunitySqlAlchemySemanticGuidelineAssessment(session).record_semantic_subject_mutation(
                board_id=board_id, entity_type=PolicyEntityType.IDEATION,
                subject_id=subject_id, actor_id="artifact-author",
                idempotency_key="history-edition-2",
                request_digest=canonical_sha256({"subject": subject_id, "edition": 2}),
                changed_at=_now(),
            )
            previous = await adapter.get_semantic_assessment_v2_currentness(first.receipt)
            assert [reason.value for reason in previous.reasons] == ["subject_edition_changed"]
            historical, _ = await adapter.list_semantic_assessment_v2_receipts(**filters)
            assert {item.receipt_id for item in historical} == set(ids)
            assert await adapter.list_semantic_assessment_v2_receipts(**{**filters, "subject_edition": 2}) == ((), None)
            assert await adapter.get_current_semantic_assessment_v2(
                board_id=board_id, entity_type="ideation", subject_id=subject_id,
                binding_id=binding.binding_id, subject_edition=2,
            ) is None
    finally:
        await engine.dispose()


def test_subject_projection_adapter_satisfies_public_core_port():
    assert isinstance(
        CommunitySqlAlchemySemanticSubjectProjection(
            object()  # type: ignore[arg-type]
        ),
        SemanticSubjectProjectionPort,
    )


@pytest.mark.asyncio
async def test_native_writer_is_active_on_fresh_schema_and_requires_runtime_probes(
    tmp_path,
):
    engine = _engine(tmp_path / "semantic-pinpoint-v2-capabilities.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

    try:
        async with factory() as session:
            adapter = CommunitySemanticAssessmentV2Capabilities(session)

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
        await engine.dispose()


@pytest.mark.asyncio
async def test_subject_projection_resolves_human_field_and_denies_other_actor(
    tmp_path,
):
    engine = _engine(tmp_path / "semantic-pinpoint-v2-projection.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

    async with factory() as session, session.begin():
        board_id, ideation_id, revision, _binding = await _seed_semantic_authority(
            session, metric_count=1
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


def test_current_sqlite_manifest_covers_semantic_pinpoint_ledger():
    sqlite_manifest = semantic_pinpoint_v2_sqlite_trigger_manifest()

    assert {table for table, _ddl in sqlite_manifest.values()} == {
        "semantic_guideline_assessments_v2",
        "semantic_guideline_metric_results_v2",
        "semantic_guideline_findings_v2",
    }


@pytest.mark.asyncio
async def test_v2_round_trip_findings_idempotency_and_immutability(tmp_path):
    engine = _engine(tmp_path / "semantic-pinpoint-v2-roundtrip.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

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
        assert failed.pinpoints[0].anchor_snapshot.label == ("Problem statement")
        assert failed.pinpoints[0].blocking_for(failed.outcome)
        current = await adapter.get_current_semantic_assessment_v2(
            board_id=board_id,
            entity_type="ideation",
            subject_id=ideation_id,
            binding_id=binding.binding_id,
        )
        assert current == projection
        with pytest.raises(GuidelinePolicyIdempotencyConflict):
            await adapter.save_semantic_assessment_v2(replace(request, confidence=94))

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
    await engine.dispose()


@pytest.mark.asyncio
async def test_native_receipt_trigger_rejects_predecessor_and_missing_payload_version(tmp_path):
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    engine = _engine(tmp_path / "native-semantic-contract-guard.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            request = _request(*await _seed_semantic_authority(session))
            adapter = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
            created = await adapter.save_semantic_assessment_v2(request)
            row = await session.get(SemanticGuidelineAssessmentV2Row, created.receipt_id)
            values = {column.name: getattr(row, column.name)
                      for column in SemanticGuidelineAssessmentV2Row.__table__.columns}
            for version in (1, None):
                payload = dict(row.payload)
                if version is None:
                    payload.pop("contract_version")
                else:
                    payload["contract_version"] = version
                with pytest.raises(IntegrityError, match="semantic_assessment_v2_contract_invalid"):
                    async with session.begin_nested():
                        await session.execute(insert(SemanticGuidelineAssessmentV2Row).values({
                            **values, "receipt_id": f"invalid-{version}",
                            "idempotency_key": f"invalid-{version}", "payload": payload,
                        }))
            assert await adapter.get_semantic_assessment_v2(
                board_id=request.subject.board_id, receipt_id=created.receipt_id,
            ) == created.receipt
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_native_receipt_identity_and_replay_are_board_scoped(tmp_path):
    engine = _engine(tmp_path / "native-semantic-board-scope.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())
    try:
        async with factory() as session, session.begin():
            adapter = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
            requests = [_request(*await _seed_semantic_authority(session), key="same-client-key")
                        for _ in range(2)]
            first = await adapter.save_semantic_assessment_v2(requests[0])
            second = await adapter.save_semantic_assessment_v2(requests[1])
            assert first.receipt_id != second.receipt_id
            assert await adapter.save_semantic_assessment_v2(requests[0]) == first
            assert await adapter.save_semantic_assessment_v2(requests[1]) == second
            assert await adapter.get_semantic_assessment_v2(
                board_id=requests[0].subject.board_id, receipt_id=second.receipt_id,
            ) is None
            assert len((await session.execute(select(SemanticGuidelineAssessmentV2Row))).scalars().all()) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_board_erasure_removes_v2_findings_before_releasing_permit(tmp_path):
    from sqlalchemy import delete, func
    from sqlalchemy.exc import IntegrityError
    from okto_pulse.community.adapters.sqlalchemy_models import (
        Board,
        BoardErasurePermit,
    )
    from okto_pulse.community.adapters.sqlalchemy_kg_governance import (
        CommunitySqlAlchemyKGGovernanceStore,
    )

    engine = _engine(tmp_path / "semantic-v2-erasure.db")
    factory = build_community_session_factory(engine)
    try:
        await initialize_current_schema(engine, current_schema_contract())

        async with factory() as session, session.begin():
            board_id, ideation_id, revision, binding = await _seed_semantic_authority(
                session, metric_count=2
            )
            await CommunitySqlAlchemySemanticGuidelineAssessmentV2(
                session
            ).save_semantic_assessment_v2(
                _request(board_id, ideation_id, revision, binding)
            )
        async with factory() as session:
            with pytest.raises(
                IntegrityError, match="semantic_assessment_v2_immutable"
            ):
                await session.execute(delete(SemanticGuidelineAssessmentV2Row))
            await session.rollback()
            with pytest.raises(IntegrityError, match="semantic_.*_immutable"):
                await session.execute(delete(Board).where(Board.id == board_id))
            await session.rollback()
        async with factory() as session, session.begin():
            await CommunitySqlAlchemyKGGovernanceStore().purge_board_metadata(
                session, board_id=board_id
            )
            for model in (
                SemanticGuidelineAssessmentV2Row,
                SemanticGuidelineMetricResultV2Row,
                SemanticGuidelineFindingV2Row,
            ):
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.board_id == board_id)
                    )
                    == 0
                )
            assert await session.get(BoardErasurePermit, board_id) is None
            await session.execute(delete(Board).where(Board.id == board_id))
        async with factory() as session:
            assert await session.get(Board, board_id) is None
            assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("entity_type", "model"),
    (
        (PolicyEntityType.IDEATION, Ideation),
        (PolicyEntityType.REFINEMENT, Refinement),
        (PolicyEntityType.SPEC, Spec),
    ),
)
@pytest.mark.asyncio
async def test_v2_persistence_records_and_fences_validation_edition(
    tmp_path,
    entity_type: PolicyEntityType,
    model,
):
    engine = _engine(tmp_path / f"semantic-v2-{entity_type.value}-edition-fence.db")
    factory = build_community_session_factory(engine)
    await initialize_current_schema(engine, current_schema_contract())

    try:
        async with factory() as session, session.begin():
            board_id, subject_id, revision, binding = await _seed_semantic_authority(
                session,
                metric_count=2,
                entity_type=entity_type,
            )
            adapter = CommunitySqlAlchemySemanticGuidelineAssessmentV2(session)
            request = _request(
                board_id,
                subject_id,
                revision,
                binding,
                key=f"v2-{entity_type.value}-edition-1",
                entity_type=entity_type,
            )

            created = await adapter.save_semantic_assessment_v2(request)
            stored = await session.get(
                SemanticGuidelineAssessmentV2Row,
                created.receipt_id,
            )
            assert stored is not None
            assert stored.validation_edition == 1

            current_for_edition = await adapter.get_current_semantic_assessment_v2(
                board_id=board_id,
                entity_type=entity_type.value,
                subject_id=subject_id,
                binding_id=binding.binding_id,
                subject_edition=1,
            )
            assert current_for_edition is not None
            assert current_for_edition.receipt_id == created.receipt_id

            subject = await session.get(model, subject_id)
            assert subject is not None
            subject.edition = 2
            await session.flush((subject,))
            await CommunitySqlAlchemySemanticGuidelineAssessment(
                session
            ).record_semantic_subject_mutation(
                board_id=board_id,
                entity_type=entity_type,
                subject_id=subject_id,
                actor_id="artifact-author",
                idempotency_key=f"{entity_type.value}-edition-2",
                request_digest=canonical_sha256({"subject": subject_id, "edition": 2}),
                changed_at=_now(),
            )

            with pytest.raises(
                GuidelinePolicyEditionConflict,
                match="guideline_policy_edition_conflict",
            ):
                await adapter.save_semantic_assessment_v2(
                    replace(
                        request,
                        idempotency_key=(f"v2-{entity_type.value}-stale-edition"),
                    )
                )

            receipt_ids = tuple(
                (
                    await session.execute(
                        select(SemanticGuidelineAssessmentV2Row.receipt_id)
                    )
                )
                .scalars()
                .all()
            )
            assert receipt_ids == (created.receipt_id,)
            # A current-read fence never turns an earlier lifecycle edition
            # into Current after the live subject advances.
            stale_edition = await adapter.get_current_semantic_assessment_v2(
                board_id=board_id,
                entity_type=entity_type.value,
                subject_id=subject_id,
                binding_id=binding.binding_id,
                subject_edition=1,
            )
            current = await adapter.get_current_semantic_assessment_v2(
                board_id=board_id,
                entity_type=entity_type.value,
                subject_id=subject_id,
                binding_id=binding.binding_id,
            )
            assert stale_edition is None
            assert current is None
    finally:
        await engine.dispose()
