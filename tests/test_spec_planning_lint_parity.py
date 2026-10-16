"""U2/UV07: accepted native lint is required; its findings remain advisory."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from okto_pulse.community.adapters.relational_application import CommunityRelationalApplicationAdapter
from okto_pulse.community.adapters.sqlalchemy_models import (
    ActivityLog, Board, DomainEventRow, QualityAssessmentReceiptRow,
    Spec, SpecHistory, SpecValidationOperationReceiptRow,
)
from okto_pulse.community.adapters.sqlalchemy_quality_assessment import (
    CommunitySqlAlchemyQualityAssessmentPreflightReader,
)
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.application.use_cases.base import ActorContext
from okto_pulse.core.application.use_cases.quality_assessment import (
    GetRequirementLintPreflightCommand, GetRequirementLintPreflightUseCase,
    RecordRequirementLintCommand, RecordRequirementLintUseCase,
)
from okto_pulse.core.domain.quality_assessment import (
    FindingAnchorType, FindingSeverity, UnboundFindingAnchor, UnboundQualityFindingDraft,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.spec_planning import SpecPlanningManifest
from okto_pulse.core.mcp import server
from okto_pulse.core.ports import relational_application
from okto_pulse.core.services.main import SpecService
from test_spec_validation_native_gate import (
    db_factory, USER_ID, ACTOR, _decomposition_submit_data as decomposition_payload,
    _seed_native_checklist_board, _canonical_submit_data,
)
from test_spec_validation_lifecycle_gate import _new_spec

__all__ = ['db_factory']
_NATIVE_LINT_GATE = SpecService._enforce_spec_requirement_lint_gate


async def current_manifest(db, spec_id):
    from okto_pulse.community.adapters.sqlalchemy_spec_planning_manifest import CommunitySpecPlanningManifest
    spec = await db.get(Spec, spec_id)
    manifest, = await CommunitySpecPlanningManifest(db, RealmScope.local()).read_many(
        board_id=spec.board_id, spec_ids=(spec_id,),
    )
    return manifest.model_dump(mode='json')


def _validation_payload():
    return {**_canonical_submit_data(), 'pinpoints': [],
            'expected_validation_edition': 1, 'expected_head_revision': 0}


@pytest_asyncio.fixture
async def composition_client(db_factory, monkeypatch):
    from okto_pulse.community.api import specs, allowed_transitions, auth_deps
    # The reusable fixture bypasses lint for unrelated gate cases. This suite
    # restores the actual requirement-lint predicate before doing any work.
    monkeypatch.setattr(SpecService, '_enforce_spec_requirement_lint_gate', _NATIVE_LINT_GATE)
    monkeypatch.setattr(relational_application, 'require_relational_application_adapter',
                        lambda: CommunityRelationalApplicationAdapter())
    board_id, spec_id = 'planning-native-board', 'planning-native-spec'
    async with db_factory() as db:
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            db.add(Board(id=board_id, realm_id='local', name='Native planning parity', owner_id=USER_ID, settings={}))
            db.add(_new_spec(id=spec_id, board_id=board_id, title='Native mock planning',
                            created_by=USER_ID, status='approved', version=1, edition=1,
                            functional_requirements=[], business_rules=[], technical_requirements=[],
                            acceptance_criteria=[], test_scenarios=[], api_contracts=[],
                            integration_requirements=[], observability_requirements=[], decisions=[]))
            await db.flush()
            await _seed_native_checklist_board(db, board_id)
            await uow.commit()

    @asynccontextmanager
    async def uow_factory(*, actor, realm_scope=None):
        async with db_factory() as db:
            async with CommunityUnitOfWork(db, actor=actor, realm_scope=realm_scope or RealmScope.local()) as uow:
                yield uow

    async def uow_dependency():
        async with uow_factory(actor=ACTOR) as uow:
            yield uow

    app = FastAPI()
    app.include_router(specs.router, prefix='/api/v1')
    app.include_router(allowed_transitions.router, prefix='/api/v1')
    app.dependency_overrides[auth_deps.require_user] = lambda: USER_ID
    app.dependency_overrides[auth_deps.get_realm_id] = lambda: 'local'
    app.dependency_overrides[specs.get_unit_of_work] = uow_dependency
    monkeypatch.setattr(server, 'get_unit_of_work_factory_for_mcp', lambda: uow_factory)
    with TestClient(app) as client:
        yield client, spec_id, None


async def _record_lint(db_factory, board_id, spec_id, *, findings):
    """Use the public coordinator with real preflight, authority, CAS and commit."""
    actor = ActorContext(USER_ID, 'rest', board_id=board_id, actor_kind='human',
                         realm_scope=RealmScope.local())
    reader = CommunitySqlAlchemyQualityAssessmentPreflightReader(db_factory)
    preflight = await GetRequirementLintPreflightUseCase(preflight_reader=reader).execute(
        GetRequirementLintPreflightCommand(spec_id), actor=actor,
    )

    @asynccontextmanager
    async def uow_factory(*, realm_scope, actor):
        async with db_factory() as db:
            async with CommunityUnitOfWork(db, actor=actor, realm_scope=realm_scope) as uow:
                yield uow

    return await RecordRequirementLintUseCase(preflight_reader=reader, uow_factory=uow_factory).execute(
        RecordRequirementLintCommand(
            board_id=board_id, spec_id=spec_id, idempotency_key='native-lint-parity',
            expected_subject_version=preflight.submission_fence.expected_subject_version,
            expected_subject_edition=preflight.subject_edition,
            expected_head_revision=preflight.submission_fence.expected_head_revision,
            ruleset_digest=preflight.ruleset_digest, score=len(findings),
            summary='External mock review of this planning edition.', findings=findings,
        ), actor=actor,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['rest', 'mcp'])
@pytest.mark.parametrize('quality_enabled', [True, False])
@pytest.mark.parametrize('lint', ['missing', 'accepted_clean', 'accepted_findings'])
async def test_native_lint_readiness_and_public_promotions(
    composition_client, db_factory, monkeypatch, surface, quality_enabled, lint,
):
    client, spec_id, _ = composition_client
    async with db_factory() as db:
        spec = await db.get(Spec, spec_id)
        board_id = spec.board_id
        board = await db.get(Board, board_id)
        board.settings = {**board.settings, 'require_spec_validation': quality_enabled}
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            await SpecService(db).submit_spec_evaluation(
                spec_id, USER_ID, 'Planning reviewer', decomposition_payload(),
            )
            await uow.commit()

    findings = () if lint != 'accepted_findings' else (UnboundQualityFindingDraft(
        finding_key='mock-scope-clarity', category_code='functional_scope_behavior',
        severity=FindingSeverity.HIGH, confidence=0.95, deterministic=False,
        title='Clarify the mock objective', detail='The mock plan has no stated observable objective.',
        remediation='State the intended observable behavior.',
        anchor=UnboundFindingAnchor(FindingAnchorType.WHOLE_ARTIFACT),
    ),)
    accepted = await _record_lint(db_factory, board_id, spec_id, findings=findings) if lint != 'missing' else None

    async def persisted():
        async with db_factory() as db:
            row = await db.get(Spec, spec_id)
            return (row.status.value, row.version, row.edition, row.validations, row.evaluations,
                    [(await db.execute(select(func.count()).select_from(model))).scalar_one()
                     for model in (ActivityLog, DomainEventRow, SpecHistory, SpecValidationOperationReceiptRow,
                                   QualityAssessmentReceiptRow)])

    before = await persisted()
    async with db_factory() as db:
        spec = await db.get(Spec, spec_id)
        board = await db.get(Board, board_id)
        summary = await SpecService(db).get_planning_readiness(
            spec, board, can_read=lambda _: True, can_act=lambda _: True,
        )
        component = next(item for item in summary.components if item.key == 'requirement_lint')
        blocked = quality_enabled and lint == 'missing'
        assert component.state == ('blocked' if blocked else 'ready'), component
        assert component.mode == ('blocking' if quality_enabled else 'off')
        if accepted and quality_enabled:
            assert component.result_ids == (accepted.receipt_id,)
        payload = {**_validation_payload(), 'expected_spec_version': spec.version,
                   'expected_planning_manifest': await current_manifest(db, spec_id),
                   'idempotency_key': 'lint-parity-conclusion'}

    preview = client.get(f'/api/v1/boards/{board_id}/allowed-transitions', params={
        'entity_type': 'spec', 'entity_id': spec_id,
    })
    assert preview.status_code == 200, preview.text
    transition = next(item for item in preview.json()['allowed_transitions'] if item['to_status'] == 'validated')
    assert ('spec_validation_required' in transition['blocked_reason']) if quality_enabled else transition['blocked_reason'] is None
    assert await persisted() == before

    if surface == 'rest':
        response = client.post(
            f'/api/v1/specs/{spec_id}/validation' if quality_enabled else f'/api/v1/specs/{spec_id}/move',
            json=payload if quality_enabled else {'status': 'validated'},
        )
        if blocked:
            assert response.status_code == 409, response.text
            assert 'requirement_lint_required' in response.text
        else:
            assert response.status_code == (201 if quality_enabled else 200), response.text
    else:
        monkeypatch.setattr(server, '_get_agent_ctx', AsyncMock(return_value=SimpleNamespace(
            agent_id=USER_ID, agent_name='Lint parity reviewer', realm_id='local',
            permissions=['spec.read', 'spec.update', 'spec.entity.read', 'spec.entity.edit_fields',
                         'spec.interact_in.approved', 'spec.validation.submit', 'spec.move.approved_to_validated'],
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
            assert result['code'] == 'requirement_lint_required', result
        elif quality_enabled:
            assert result['planning_result'] == 'ready' and result['spec_status'] == 'validated', result
        else:
            assert result['success'] is True and result['to_status'] == 'validated', result

    after = await persisted()
    if blocked:
        assert after == before
    else:
        assert after[0] == 'validated'
        assert after[4] == before[4]  # original decomposition is reused
        assert len(after[3] or []) == int(quality_enabled)
        assert after[5][-1] == before[5][-1]  # conclusion never rewrites the lint evidence
