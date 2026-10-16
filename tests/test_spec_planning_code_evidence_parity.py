"""U2/UV09/22: native projection integrity survives skips and public writers."""
from dataclasses import replace

import pytest

from okto_pulse.community.adapters.sqlalchemy_code_traceability import CommunitySqlAlchemyCodeTraceabilityStore
from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec
from okto_pulse.community.adapters.sqlalchemy_unit_of_work import CommunityUnitOfWork
from okto_pulse.core.domain.code_traceability import (
    CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS,
    CodeInvestigationCurrentnessUnknown, CodeTraceabilityOmittedContent,
)
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.services.main import SpecService
from test_spec_planning_lint_parity import (
    composition_client, db_factory, current_manifest, _validation_payload, _record_lint,
)
from test_spec_planning_resource_parity import _promote, _state
from test_spec_validation_native_gate import ACTOR, USER_ID, _decomposition_submit_data

__all__ = ['composition_client', 'db_factory']


@pytest.mark.asyncio
@pytest.mark.parametrize('surface', ['rest', 'mcp'])
@pytest.mark.parametrize('quality_enabled', [True, False])
@pytest.mark.parametrize('skip', ['none', 'spec', 'board'])
@pytest.mark.parametrize('projection', ['complete', 'incomplete', 'unavailable'])
async def test_native_code_evidence_integrity_matches_public_promotions(
    composition_client, db_factory, monkeypatch, surface, quality_enabled, skip, projection,
):
    client, spec_id, _ = composition_client
    async with db_factory() as db:
        async with CommunityUnitOfWork(db, actor=ACTOR, realm_scope=RealmScope.local()) as uow:
            spec = await db.get(Spec, spec_id)
            board_id = spec.board_id
            board = await db.get(Board, board_id)
            board.settings = {**board.settings, 'require_spec_validation': quality_enabled,
                              'skip_code_evidence_coverage_global': skip == 'board'}
            spec.skip_code_evidence_coverage = skip == 'spec'
            await SpecService(db).submit_spec_evaluation(
                spec_id, USER_ID, 'Planning reviewer', _decomposition_submit_data(),
            )
            await uow.commit()
    if quality_enabled:
        await _record_lint(db_factory, board_id, spec_id, findings=())

    # Positive reads remain native. Faults are introduced only after the real
    # relational projection has resolved this exact subject, never in the gate.
    native_read = CommunitySqlAlchemyCodeTraceabilityStore.spec_context

    async def read_with_fault(store, query):
        context = await native_read(store, query)
        assert context.subject_id == spec_id and context.board_id == board_id
        if projection == 'unavailable':
            raise CodeInvestigationCurrentnessUnknown(details={'reason': 'injected_read_unavailable'})
        if projection == 'incomplete':
            return replace(context, omitted_content_manifest=(CodeTraceabilityOmittedContent(
                collection='evidence', included_count=0,
                hard_limit=CODE_TRACEABILITY_CONTEXT_COLLECTION_LIMITS['evidence'],
            ),))
        return context

    monkeypatch.setattr(CommunitySqlAlchemyCodeTraceabilityStore, 'spec_context', read_with_fault)
    before = await _state(db_factory, spec_id)
    async with db_factory() as db:
        spec, board = await db.get(Spec, spec_id), await db.get(Board, board_id)
        summary = await SpecService(db).get_planning_readiness(
            spec, board, can_read=lambda _: True, can_act=lambda _: True,
        )
        component = next(item for item in summary.components if item.key == 'code_evidence')
        assert component.state == ('ready' if projection == 'complete' else 'unavailable')
        if projection == 'complete':
            assert component.mode == ('blocking' if skip == 'none' else 'off')
        else:
            assert component.native_result is None and component.next_action is None
            assert component.message is None
        payload = {**_validation_payload(), 'expected_spec_version': spec.version,
                   'expected_planning_manifest': await current_manifest(db, spec_id),
                   'idempotency_key': 'code-evidence-parity-conclusion'}
    preview = client.get(f'/api/v1/boards/{board_id}/allowed-transitions', params={
        'entity_type': 'spec', 'entity_id': spec_id,
    })
    assert preview.status_code == 200, preview.text
    transition = next(item for item in preview.json()['allowed_transitions'] if item['to_status'] == 'validated')
    if projection != 'complete':
        assert component.reason_codes[0] in transition['blocked_reason'], transition
    elif quality_enabled:
        assert 'spec_validation_required' in transition['blocked_reason']
    else:
        assert transition['blocked_reason'] is None
    assert await _state(db_factory, spec_id) == before

    status, response = await _promote(client, monkeypatch, board_id, spec_id, surface, quality_enabled, payload)
    if projection != 'complete':
        assert status in {None, 409}, response
        diagnosis = response['detail'] if surface == 'rest' else response
        assert isinstance(diagnosis, dict), diagnosis
        if quality_enabled:
            assert diagnosis['code'] == 'spec_validation_gate_not_ready'
            assert diagnosis['details']['reason'] == component.reason_codes[0]
        else:
            assert diagnosis['code'] == component.reason_codes[0]
        assert await _state(db_factory, spec_id) == before
    else:
        assert status in {None, 201 if quality_enabled else 200}, response
        if surface == 'mcp':
            assert (response.get('spec_status') == 'validated' if quality_enabled else response.get('success') is True)
        after = await _state(db_factory, spec_id)
        assert after[0] == 'validated' and after[4] == before[4]
        assert len(after[3] or []) == int(quality_enabled)
