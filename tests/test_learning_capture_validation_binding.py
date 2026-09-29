"""Real validation/status/binding/outbox UOW, with independent gates admitted.

The completion gate aggregate is an explicit fixture: these cases prove the
compound write and authority, not independent implementation/graph admission.
"""
from dataclasses import replace
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, DomainEventRow, Spec
from okto_pulse.core.application.use_cases.base import ActorContext, PermissionDeniedError
from okto_pulse.core.application.use_cases.card_crud import SubmitTaskValidationCommand, SubmitTaskValidationUseCase
from okto_pulse.core.application.use_cases.learning_capture import (
    LEARNING_CAPTURE_HISTORY_PERMISSIONS, StageLearningCaptureUseCase,
)
from okto_pulse.core.domain.card_completion import CompletionGateFailure
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.domain.learning_closeout import closeout_binding_is_current
from okto_pulse.core.ports.permission_policy import set_permission_flag
from okto_pulse.core.services.main import CardService
from test_delivery_reused_impact import register_report_adapters
from test_learning_capture_writer import BOARD, actor as capture_actor, runtime as _runtime_fixture, uow as capture_uow

runtime = _runtime_fixture


def reviewer(denied=None):
    flags = {}
    for flag in (*LEARNING_CAPTURE_HISTORY_PERMISSIONS, 'card.validation.submit'):
        set_permission_flag(flags, flag, flag != denied)
    return ActorContext('owner', 'mcp', board_id=BOARD, actor_kind='agent',
        actor_name='Reviewer', permissions=flags)


async def prepare(runtime, *, legacy=False, learning_policy=None):
    factory, assembler, _, request = runtime
    register_report_adapters()
    async with factory() as session:
        bug = await session.get(Card, request.bug_id)
        bug.status = 'validation'
        bug.validations = []
        bug.conclusions = [{'text': 'The executor supplied the correction report.',
            'source': 'legacy' if legacy else 'move_to_validation', 'author_id': 'executor'}]
        spec = await session.get(Spec, bug.spec_id)
        spec.status = 'in_progress'
        board = await session.get(Board, BOARD)
        board.realm_id = 'local'
        board.settings = {'reviewer_separation_mode': 'off', 'require_full_context_for_critical_actions': False}
        if learning_policy is not None:
            board.settings = {**board.settings, 'bug_learning_closeout': learning_policy}
        await session.commit()
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=bug.id)
        request = replace(request, expected_source_digest=source.source_digest,
            expected_source_version=source.source_policy_version)
        record = await StageLearningCaptureUseCase().execute(request, actor=capture_actor(), uow=capture_uow(session))
        await session.commit()
    data = dict(expected_subject_version=source.source_policy_version, idempotency_key='review-one',
        confidence=99, confidence_justification='Reviewed the actual implementation.',
        estimated_completeness=100, completeness_justification='All assigned work was checked.',
        estimated_drift=0, drift_justification='Implementation stayed within scope.',
        general_justification='The implementation is accepted by this independent review.', recommendation='approve',
        learning_capture=dict(learning_id=record.node_id, generation=record.generation,
            fingerprint=record.record_fingerprint))
    return request, data


def validation_uow(session, *, failures=()):
    session.info['realm_scope'] = RealmScope.local()
    service = CardService(session)
    service._task_completion_gate_failures = AsyncMock(return_value=failures)
    async def board(identity):
        return await session.get(Board, identity)
    return SimpleNamespace(boards=SimpleNamespace(get=board),
        services=SimpleNamespace(cards=service, boards=SimpleNamespace(get_board=board)),
        commit=AsyncMock(side_effect=session.commit), rollback=AsyncMock(side_effect=session.rollback))


@pytest.mark.parametrize('policy', [None, 'advisory', 'blocking'])
async def test_selected_capture_binds_real_validation_done_and_outbox_with_exact_retry(runtime, policy):
    factory, assembler, _, _ = runtime
    request, data = await prepare(runtime, learning_policy=policy)
    async with factory() as session:
        uow = validation_uow(session)
        case = SubmitTaskValidationUseCase()
        result = await case.execute(SubmitTaskValidationCommand(request.bug_id, data), actor=reviewer(), uow=uow)
        assert result.validation['completion_outcome'] == 'completed'
        bug = await session.get(Card, request.bug_id)
        assert bug.status.value == 'done'
        assert len(bug.learning_closeout_bindings) == len(bug.validations) == 1
        binding = bug.learning_closeout_bindings[0]
        closed = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=bug.id)
        assert closeout_binding_is_current(binding, closed)
        count = await session.scalar(select(func.count()).select_from(DomainEventRow))
        assert count > 0
        repeated = await case.execute(SubmitTaskValidationCommand(request.bug_id, data), actor=reviewer(), uow=uow)
        assert repeated.validation['replayed'] is True
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == count
        uow.services.cards._task_completion_gate_failures.assert_awaited_once()
    async with factory() as session:
        bug = await session.get(Card, request.bug_id)
        assert len(bug.learning_closeout_bindings) == 1


async def test_selected_capture_does_not_bypass_other_completion_rejection(runtime):
    factory, _, _, _ = runtime
    request, data = await prepare(runtime)
    async with factory() as session:
        failure = CompletionGateFailure(code='existing_gate', summary='Independent gate refuses.', reason_codes=('existing_gate',))
        result = await SubmitTaskValidationUseCase().execute(SubmitTaskValidationCommand(request.bug_id, data),
            actor=reviewer(), uow=validation_uow(session, failures=(failure,)))
        assert result.validation['completion_outcome'] == 'rejected'
        bug = await session.get(Card, request.bug_id)
        assert bug.status.value == 'rejected' and not bug.learning_closeout_bindings


@pytest.mark.parametrize('legacy', [False, True])
async def test_stale_or_legacy_fallback_refusal_leaves_no_partial_review_after_outer_commit(runtime, legacy):
    factory, _, _, _ = runtime
    request, data = await prepare(runtime, legacy=legacy)
    if not legacy:
        data['learning_capture']['fingerprint'] = 'f' * 64
    async with factory() as session:
        uow = validation_uow(session)
        with pytest.raises(ValueError, match='learning_(capture_selection_changed|closeout_requires_current_execution_report)'):
            await SubmitTaskValidationUseCase().execute(SubmitTaskValidationCommand(request.bug_id, data),
                actor=reviewer(), uow=uow)
        uow.rollback.assert_awaited_once()
        await session.commit()
    async with factory() as session:
        bug = await session.get(Card, request.bug_id)
        assert bug.status.value == 'validation' and not bug.validations and not bug.learning_closeout_bindings
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == 0


async def test_late_binding_failure_rolls_back_status_review_and_outbox(runtime, monkeypatch):
    import okto_pulse.core.domain.learning_closeout as domain
    factory, _, _, _ = runtime
    request, data = await prepare(runtime)
    def fail(**kwargs):
        assert kwargs['closed'].status == 'done'
        raise RuntimeError('injected_late_binding_failure')
    monkeypatch.setattr(domain, 'bind_learning_capture_to_closed_source', fail)
    async with factory() as session:
        uow = validation_uow(session)
        with pytest.raises(RuntimeError, match='injected_late_binding_failure'):
            await SubmitTaskValidationUseCase().execute(SubmitTaskValidationCommand(request.bug_id, data),
                actor=reviewer(), uow=uow)
        await session.commit()
    async with factory() as session:
        bug = await session.get(Card, request.bug_id)
        assert bug.status.value == 'validation' and not bug.validations and not bug.learning_closeout_bindings
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == 0


@pytest.mark.parametrize('denied', LEARNING_CAPTURE_HISTORY_PERMISSIONS)
async def test_capture_selection_requires_every_existing_read_authority(runtime, denied):
    factory, _, _, _ = runtime
    request, data = await prepare(runtime)
    async with factory() as session:
        uow = validation_uow(session)
        uow.services.cards.submit_task_validation = AsyncMock()
        with pytest.raises(PermissionDeniedError):
            await SubmitTaskValidationUseCase().execute(SubmitTaskValidationCommand(request.bug_id, data),
                actor=reviewer(denied), uow=uow)
        uow.services.cards.submit_task_validation.assert_not_awaited()
        uow.commit.assert_not_awaited()


@pytest.mark.parametrize('transport', ['rest', 'mcp'])
@pytest.mark.parametrize('denied', [False, True])
async def test_transports_preserve_capture_selection_and_authority(runtime, monkeypatch, transport, denied):
    factory, _, _, _ = runtime
    request, data = await prepare(runtime)
    principal = reviewer('kg.query.learning_from_bugs' if denied else None)
    @asynccontextmanager
    async def units(**kwargs):
        async with factory() as session:
            try:
                yield validation_uow(session)
            finally:
                await session.rollback()
    if transport == 'rest':
        import httpx
        from fastapi import FastAPI
        from okto_pulse.community.api.cards import router
        from okto_pulse.community.api.auth_deps import require_user
        from okto_pulse.community.api.deps import get_unit_of_work
        from okto_pulse.community.inbound.rest_adapter import RESTAdapterContract
        monkeypatch.setattr(RESTAdapterContract, 'actor', staticmethod(lambda *args, **kwargs: principal))
        app = FastAPI()
        app.include_router(router, prefix='/api/v1/cards')
        async def unit():
            async with units() as value:
                yield value
        app.dependency_overrides[get_unit_of_work] = unit
        app.dependency_overrides[require_user] = lambda: 'owner'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            result = await client.post(f'/api/v1/cards/{request.bug_id}/validate', json=data)
        assert result.status_code == (403 if denied else 201), result.text
        if not denied:
            assert result.json()['completion_outcome'] == 'completed'
    else:
        from okto_pulse.core.mcp import server
        from okto_pulse.core.domain.learning_closeout import LearningCaptureSelection
        context = SimpleNamespace(agent_id='owner', agent_name='Reviewer', permissions=principal.permissions)
        monkeypatch.setattr(server, '_get_agent_ctx', AsyncMock(return_value=context))
        monkeypatch.setattr(server, 'get_unit_of_work_factory_for_mcp', lambda: units)
        tool = (await server.mcp.get_tools())['okto_pulse_submit_task_validation']
        result = json.loads(await tool.fn(board_id=BOARD, card_id=request.bug_id,
            **{**data, 'learning_capture': LearningCaptureSelection.model_validate(data['learning_capture'])}))
        if denied:
            assert 'kg.query.learning_from_bugs' in result['error']
            assert 'completion_outcome' not in result
        else:
            assert result['completion_outcome'] == 'completed'
    async with factory() as session:
        bug = await session.get(Card, request.bug_id)
        assert bool(bug.learning_closeout_bindings) is not denied
        assert bug.status.value == ('validation' if denied else 'done')
