"""U2/UV09: one persisted policy state across planning and public promotion.

HTTP/MCP actor injection isolates wire authentication (qualified by U4).
Binding adoption, sealed assessment, currentness and transition gates are native.
"""
from datetime import datetime, timezone
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog, Board, DomainEventRow, Spec, SpecHistory,
    SpecValidationOperationReceiptRow, SemanticGuidelineAssessmentV2Row,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_assessment import (
    CommunitySqlAlchemySemanticGuidelineAssessment,
)
from okto_pulse.community.adapters.sqlalchemy_semantic_guideline_v2 import (
    CommunitySqlAlchemySemanticGuidelineAssessmentV2,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.domain.guideline_policy import (
    GuidelineEnforcement, GuidelineMetric, GuidelineMetricDirection,
    GuidelineRevision, PolicyEntityType,
)
from okto_pulse.core.domain.guideline_semantic_assessment import SemanticAssessmentAssessor
from okto_pulse.core.domain.guideline_semantic_v2 import (
    AnchorSnapshot, SemanticAnchorAvailability, SemanticAssessmentRequestV2,
    SemanticMetricAssessmentV2, SemanticPinpointKind, SemanticPinpointV2,
)
from okto_pulse.core.domain.quality_assessment import (
    EvidenceRef, FindingAnchorType, FindingSeverity, UnboundFindingAnchor,
)
from okto_pulse.core.domain.quality_canonicalization import canonical_sha256
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.spec_planning import SpecPlanningManifest
from okto_pulse.core.mcp import server
from okto_pulse.core.services.main import SpecService
from test_skb3_semantic_guideline_persistence import _native_guideline_binding
from test_spec_planning_lint_parity import (
    composition_client, db_factory, current_manifest, _validation_payload, _record_lint,
)
from test_spec_validation_native_gate import ACTOR, USER_ID, _decomposition_submit_data

__all__ = ['composition_client', 'db_factory']


async def _policy(db_factory, board_id, spec_id, enforcement, score):
    revision = GuidelineRevision(
        revision_id=str(uuid4()), guideline_id=str(uuid4()), revision_number=1,
        semantic_version='1.0.0', title='Mock planning clarity',
        content='State the observable objective of this mock plan.',
        metrics=(GuidelineMetric(
            metric_id='clarity', code='clarity', title='Observable objective',
            description='Clarity of the mock objective.',
            evaluation_rubric='Score the clarity of the observable objective from zero to 100.',
            target_entity_types=(PolicyEntityType.SPEC,),
            direction=GuidelineMetricDirection.MINIMUM, default_threshold=80,
        ),), created_by=USER_ID, created_at=datetime.now(timezone.utc),
    )
    async with db_factory() as db:
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            binding = await _native_guideline_binding(
                db, board_id=board_id, revision=revision,
                enforcement=enforcement, actor_id=USER_ID,
            )
            await uow.commit()
    if score is None:
        return None
    async with db_factory() as db:
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            snapshot = await CommunitySqlAlchemySemanticGuidelineAssessment(db).resolve_policy_subject_snapshot(
                board_id=board_id, entity_type=PolicyEntityType.SPEC, subject_id=spec_id,
            )
            title = (await db.get(Spec, spec_id)).title
            request = SemanticAssessmentRequestV2(
                subject=snapshot.subject, binding_id=binding.binding_id,
                expected_binding_revision=binding.binding_revision,
                guideline_revision_id=revision.revision_id, idempotency_key='policy-parity-assessment',
                confidence=95, assessor=SemanticAssessmentAssessor(
                    agent_id='independent-policy-reviewer', model_id='mock-model'),
                metric_results=(SemanticMetricAssessmentV2(
                    metric_id='clarity', score=score,
                    rationale='Independent assessment of the observable mock objective.',
                    evidence_refs=(EvidenceRef(
                        source_type='spec', source_id=spec_id,
                        source_version=snapshot.subject.subject_version,
                        content_hash=snapshot.content_digest,
                    ),), pinpoints=(SemanticPinpointV2(
                        pinpoint_key='objective-clarity',
                        kind=SemanticPinpointKind.ISSUE if score < 80 else SemanticPinpointKind.EVIDENCE,
                        title='Review of the mock objective',
                        severity=FindingSeverity.HIGH if score < 80 else None,
                        detail=('The title does not identify an observable outcome.' if score < 80
                                else 'The title explicitly identifies the mock planning subject.'),
                        remediation='State the observable outcome in the mock plan.' if score < 80 else None,
                        anchor=UnboundFindingAnchor(
                            FindingAnchorType.FIELD, anchor_ref='title',
                            excerpt_hash=canonical_sha256(title),
                        ), anchor_snapshot=AnchorSnapshot(
                            label='Title', excerpt=title,
                            source_version=f'spec:{snapshot.subject.subject_version}',
                            availability_at_seal=SemanticAnchorAvailability.AVAILABLE,
                        ),
                    ),),
                ),),
            )
            result = await CommunitySqlAlchemySemanticGuidelineAssessmentV2(db).save_semantic_assessment_v2(request)
            await uow.commit()
            return result.receipt.receipt_id


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['rest', 'mcp'])
@pytest.mark.parametrize('quality_enabled', [True, False])
@pytest.mark.parametrize('mode,score', [
    ('off', None), ('advisory', None), ('advisory', 60),
    ('blocking', None), ('blocking', 60), ('blocking', 95),
    ('unavailable', 95),
])
async def test_native_policy_readiness_and_public_promotions(
    composition_client, db_factory, monkeypatch, surface, quality_enabled, mode, score,
):
    client, spec_id, _ = composition_client
    async with db_factory() as db:
        board_id = (await db.get(Spec, spec_id)).board_id
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            board = await db.get(Board, board_id)
            board.settings = {**board.settings, 'require_spec_validation': quality_enabled}
            await SpecService(db).submit_spec_evaluation(
                spec_id, USER_ID, 'Planning reviewer', _decomposition_submit_data(),
            )
            await uow.commit()
    receipt_id = None if mode == 'off' else await _policy(
        db_factory, board_id, spec_id,
        GuidelineEnforcement.BLOCKING if mode == 'unavailable' else GuidelineEnforcement(mode), score,
    )
    if quality_enabled:
        await _record_lint(db_factory, board_id, spec_id, findings=())

    if mode == 'unavailable':
        native_resolve = CommunitySqlAlchemySemanticGuidelineAssessment.resolve_transition_snapshot

        async def unavailable_snapshot(adapter, **kwargs):
            # Fault injection at the persistence boundary, after native scoped
            # authority resolution. No positive judgment/gate is substituted.
            snapshot = await native_resolve(adapter, **kwargs)
            return replace(snapshot, bindings=tuple(replace(
                binding, assessment_available=False,
                assessment_error_code='assessment_service_unavailable',
                receipt=None, findings=(),
            ) for binding in snapshot.bindings))

        monkeypatch.setattr(CommunitySqlAlchemySemanticGuidelineAssessment,
                            'resolve_transition_snapshot', unavailable_snapshot)

    async def persisted():
        async with db_factory() as db:
            row = await db.get(Spec, spec_id)
            return (row.status.value, row.version, row.edition, row.validations, row.evaluations,
                    [(await db.execute(select(func.count()).select_from(model))).scalar_one()
                     for model in (ActivityLog, DomainEventRow, SpecHistory,
                                   SpecValidationOperationReceiptRow, SemanticGuidelineAssessmentV2Row)])

    before = await persisted()
    # Receipt availability is mandatory for applicable policies, including
    # Advisory. Only an accepted negative judgment is non-blocking in Advisory.
    blocked = mode == 'unavailable' or (mode != 'off' and (score is None or (mode == 'blocking' and score < 80)))
    async with db_factory() as db:
        spec, board = await db.get(Spec, spec_id), await db.get(Board, board_id)
        summary = await SpecService(db).get_planning_readiness(
            spec, board, can_read=lambda _: True, can_act=lambda _: True,
        )
        component = next(item for item in summary.components if item.key == 'policy_compliance')
        assert component.state == ('unavailable' if mode == 'unavailable' else 'blocked' if blocked else 'ready'), component
        assert component.mode == (None if mode == 'unavailable' else 'blocking' if blocked else mode), component
        if receipt_id and mode != 'unavailable':
            assert receipt_id in component.result_ids
        redacted = await SpecService(db).get_planning_readiness(
            spec, board, can_read=lambda permission: permission != 'guidelines.assessments.read',
            can_act=lambda _: True,
        )
        hidden = next(item for item in redacted.components if item.key == 'policy_compliance')
        assert hidden.state == 'unavailable' and hidden.reason_codes == ('read_access_required',), hidden
        assert hidden.result_ids == () and hidden.native_result is None and hidden.next_action is None
        payload = {**_validation_payload(), 'expected_spec_version': spec.version,
                   'expected_planning_manifest': await current_manifest(db, spec_id),
                   'idempotency_key': 'policy-parity-conclusion'}

    preview = client.get(f'/api/v1/boards/{board_id}/allowed-transitions', params={
        'entity_type': 'spec', 'entity_id': spec_id,
    })
    assert preview.status_code == 200, preview.text
    transition = next(item for item in preview.json()['allowed_transitions'] if item['to_status'] == 'validated')
    assert bool(transition['blocked_reason']) == (blocked or quality_enabled), transition
    if not quality_enabled:
        assert transition['policy_compliance_decision']['allowed'] == (not blocked), transition
    assert await persisted() == before

    if surface == 'rest':
        response = client.post(
            f'/api/v1/specs/{spec_id}/validation' if quality_enabled else f'/api/v1/specs/{spec_id}/move',
            json=payload if quality_enabled else {'status': 'validated'},
        )
        assert response.status_code == (409 if blocked else 201 if quality_enabled else 200), response.text
        if blocked:
            assert response.json()['detail']['reason_codes'] == list(component.reason_codes)
    else:
        monkeypatch.setattr(server, '_get_agent_ctx', AsyncMock(return_value=SimpleNamespace(
            agent_id=USER_ID, agent_name='Policy parity reviewer', realm_id='local',
            permissions=['spec.read', 'spec.update', 'spec.entity.read', 'spec.entity.edit_fields',
                         'spec.interact_in.approved', 'spec.validation.submit',
                         'spec.move.approved_to_validated', 'guidelines.assessments.read'],
        )))
        if quality_enabled:
            tool = await server.mcp.get_tool('okto_pulse_submit_spec_validation')
            result = json.loads(await tool.fn(board_id=board_id, spec_id=spec_id, **{
                **payload, 'expected_planning_manifest': SpecPlanningManifest.model_validate(payload['expected_planning_manifest']),
            }))
        else:
            tool = await server.mcp.get_tool('okto_pulse_move_spec')
            result = json.loads(await tool.fn(board_id=board_id, spec_id=spec_id, status='validated'))
        if blocked:
            assert result['reason_codes'] == list(component.reason_codes), result
        elif quality_enabled:
            assert result['planning_result'] == 'ready' and result['spec_status'] == 'validated', result
        else:
            assert result['success'] is True and result['to_status'] == 'validated', result
    after = await persisted()
    if blocked:
        assert after == before
    else:
        assert after[0] == 'validated'
        assert after[4] == before[4]
        assert len(after[3] or []) == int(quality_enabled)
        assert after[5][-1] == before[5][-1]
