"""Compound report/capture SQL writes; independent completion gates are fixtures.

These tests exercise real source fencing, signed receipts, report/status/outbox
and source storage. They do not certify the separately mocked gate admission.
"""

from dataclasses import asdict
from contextlib import asynccontextmanager
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, DomainEventRow, Spec
from okto_pulse.core.application.use_cases.base import ActorContext, PermissionDeniedError
from okto_pulse.core.application.use_cases.card_crud import MoveCardCommand, MoveCardUseCase
from okto_pulse.core.application.use_cases.learning_capture import LEARNING_CAPTURE_CREATE_PERMISSIONS
from okto_pulse.core.domain.learning_closeout import closeout_binding_is_current
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.models.schemas import CardMove
from okto_pulse.core.ports.permission_policy import set_permission_flag
from okto_pulse.core.services.main import CardService
from test_delivery_reused_impact import register_report_adapters
from test_learning_capture_writer import BOARD, runtime as _runtime

runtime = _runtime


def author(denied=None):
    flags = {}
    for flag in (*LEARNING_CAPTURE_CREATE_PERMISSIONS, 'card.conclusion.write'):
        set_permission_flag(flags, flag, flag != denied)
    return ActorContext('owner', 'mcp', board_id=BOARD, actor_kind='agent',
        actor_name='Author', permissions=flags)


@pytest.fixture
def independent_gates(monkeypatch):
    from okto_pulse.core.services import main
    from okto_pulse.core.services import delivery_evidence
    from okto_pulse.core.application.use_cases import card_crud

    monkeypatch.setattr(main, 'evaluate_code_traceability_transition', AsyncMock())
    monkeypatch.setattr(main.CardService, '_validate_cognitive_done', AsyncMock())
    monkeypatch.setattr(main.GuidelineService, 'enforce_policy_transition', AsyncMock())
    monkeypatch.setattr(main, 'ResourceGateService', lambda session: SimpleNamespace(validate_or_raise_entity_completion=AsyncMock()))
    monkeypatch.setattr(delivery_evidence, 'require_card_delivery', AsyncMock())
    async def knowledge(services, card, **kwargs): return card
    async def visibility(card, **kwargs): return card
    monkeypatch.setattr(card_crud, 'project_effective_knowledge', knowledge)
    monkeypatch.setattr(card_crud, 'project_card_validation_visibility', visibility)


async def prepare(runtime, target):
    factory, assembler, _, request = runtime
    register_report_adapters()
    async with factory() as session:
        board = await session.get(Board, BOARD)
        board.realm_id = 'local'
        board.settings = {'require_task_validation': target == 'validation',
            'require_full_context_for_critical_actions': False, 'impact_evidence_mode': 'off'}
        spec = await session.get(Spec, 'spec-bug-context')
        spec.status = 'in_progress'
        bug = await session.get(Card, request.bug_id)
        bug.status = 'validation' if target == 'done' else 'in_progress'
        bug.conclusions = []
        await session.commit()
        source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=bug.id)
    draft = {key: value for key, value in asdict(request).items() if key not in {'board_id', 'bug_id'}}
    draft.update(expected_source_digest=source.source_digest, expected_source_version=source.source_policy_version,
        scenario_ids=list(request.scenario_ids))
    return request.bug_id, CardMove(status=target, conclusion='The authored correction report.', completeness=100,
        completeness_justification='Assigned work completed.', drift=0, drift_justification='Within scope.',
        learning_submission=draft)


def unit(session):
    session.info['realm_scope'] = RealmScope.local()
    from okto_pulse.core.ports.application_persistence import get_application_persistence_port
    port = get_application_persistence_port()
    async def board(identity): return await session.get(Board, identity)
    async def synchronize(): await port.flush(session)
    async def reload(record, *, fields): await port.refresh(session, record)
    return SimpleNamespace(boards=SimpleNamespace(get=board),
        services=SimpleNamespace(cards=CardService(session)),
        synchronize=synchronize, reload=reload,
        commit=AsyncMock(side_effect=session.commit), rollback=AsyncMock(side_effect=session.rollback))


@pytest.mark.parametrize('target', ['validation', 'done'])
async def test_report_and_capture_share_atomic_write_and_exact_retry(runtime, independent_gates, target):
    factory, assembler, store, _ = runtime
    bug_id, data = await prepare(runtime, target)
    async with factory() as session:
        uow = unit(session)
        case = MoveCardUseCase()
        await case.execute(MoveCardCommand(bug_id, data), actor=author(), uow=uow)
        bug = await session.get(Card, bug_id)
        assert bug.status.value == target and len(bug.conclusions) == 1
        count = await session.scalar(select(func.count()).select_from(DomainEventRow))
        assert count > 0
        await case.execute(MoveCardCommand(bug_id, data), actor=author(), uow=uow)
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == count
        observed = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=bug_id)
        record, = await store.enumerate(BOARD)
        assert record.payload['author_id'] == 'owner'
        assert record.payload['content'] == data.learning_submission.content
        if target == 'done':
            assert len(bug.learning_closeout_bindings) == 1
            assert closeout_binding_is_current(bug.learning_closeout_bindings[0], observed)
            assert record.payload['source']['digest'] == bug.learning_closeout_bindings[0]['before_digest']
        else:
            assert not bug.learning_closeout_bindings
            assert record.payload['source']['digest'] == observed.source_digest
        changed = data.model_copy(update={'conclusion': 'Changed report'})
        with pytest.raises(ValueError, match='idempotency_conflict'):
            await case.execute(MoveCardCommand(bug_id, changed), actor=author(), uow=uow)


@pytest.mark.parametrize('damage', ['stale', 'unsigned', 'late'])
async def test_failed_compound_write_rolls_back_even_if_outer_caller_commits(runtime, independent_gates, monkeypatch, damage):
    factory, _, store, _ = runtime
    bug_id, data = await prepare(runtime, 'done')
    if damage == 'late':
        from okto_pulse.core.domain import learning_closeout
        def fail(**kwargs): raise RuntimeError('late_binding_failure')
        monkeypatch.setattr(learning_closeout, 'bind_learning_capture_to_closed_source', fail)
    elif damage == 'unsigned':
        data = data.model_copy(update={'learning_submission': data.learning_submission.model_copy(update={'scenario_ids': ['unknown']})})
    else:
        data = data.model_copy(update={'learning_submission': data.learning_submission.model_copy(update={'expected_source_digest': '0' * 64})})
    async with factory() as session:
        uow = unit(session)
        expected = {'stale': 'learning_capture_source_changed_or_unavailable',
            'unsigned': 'learning_capture_evidence_not_authenticated', 'late': 'late_binding_failure'}[damage]
        with pytest.raises((ValueError, RuntimeError), match=expected):
            await MoveCardUseCase().execute(MoveCardCommand(bug_id, data), actor=author(), uow=uow)
        uow.rollback.assert_awaited_once()
        await session.commit()
    assert await store.enumerate(BOARD) == ()
    async with factory() as session:
        bug = await session.get(Card, bug_id)
        assert bug.status.value == 'validation' and not bug.conclusions and not bug.learning_closeout_bindings
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == 0


@pytest.mark.parametrize('transport', ['rest', 'mcp'])
@pytest.mark.parametrize('denied', [False, True])
async def test_joint_transports_preserve_authority_and_the_same_writer(runtime, independent_gates, monkeypatch, transport, denied):
    factory, _, store, _ = runtime
    bug_id, data = await prepare(runtime, 'validation')
    principal = author('kg.session.commit' if denied else None)
    @asynccontextmanager
    async def units(**kwargs):
        async with factory() as session:
            try: yield unit(session)
            finally: await session.rollback()
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
        async def dependency():
            async with units() as value: yield value
        app.dependency_overrides[get_unit_of_work] = dependency
        app.dependency_overrides[require_user] = lambda: 'owner'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/v1/cards/{bug_id}/move', json=data.model_dump(mode='json'))
        assert response.status_code == (403 if denied else 200), response.text
    else:
        from okto_pulse.core.mcp import server
        context = SimpleNamespace(agent_id='owner', agent_name='Author', permissions=principal.permissions)
        monkeypatch.setattr(server, '_get_agent_ctx', AsyncMock(return_value=context))
        monkeypatch.setattr(server, 'get_unit_of_work_factory_for_mcp', lambda: units)
        tool = (await server.mcp.get_tools())['okto_pulse_move_card']
        payload = data.model_dump(mode='json', exclude_none=True)
        payload['learning_submission'] = data.learning_submission
        response = json.loads(await tool.fn(board_id=BOARD, card_id=bug_id, **payload))
        if denied:
            assert 'kg.session.commit' in response['error']
        else:
            assert response['success'] and response['card']['status'] == 'validation'
    assert len(await store.enumerate(BOARD)) == (0 if denied else 1)


@pytest.mark.parametrize('denied', (*LEARNING_CAPTURE_CREATE_PERMISSIONS, 'card.conclusion.write'))
async def test_joint_submission_requires_every_capture_and_report_authority(runtime, independent_gates, denied):
    factory, _, store, _ = runtime
    bug_id, data = await prepare(runtime, 'validation')
    async with factory() as session:
        uow = unit(session)
        uow.services.cards.move_card = AsyncMock()
        with pytest.raises(PermissionDeniedError):
            await MoveCardUseCase().execute(MoveCardCommand(bug_id, data), actor=author(denied), uow=uow)
        uow.services.cards.move_card.assert_not_awaited()
        uow.commit.assert_not_awaited()
    assert await store.enumerate(BOARD) == ()


@pytest.mark.parametrize('denied', (*LEARNING_CAPTURE_CREATE_PERMISSIONS, 'card.conclusion.write'))
async def test_delivery_report_cannot_inherit_capture_without_its_authority(runtime, independent_gates, denied):
    from okto_pulse.core.application.use_cases.delivery_evidence import RecordCardDeliveryEvidenceUseCase
    factory, _, store, _ = runtime
    _, data = await prepare(runtime, 'validation')
    command = SimpleNamespace(board_id=BOARD, report=data, batch_command=Mock())
    async with factory() as session:
        uow = unit(session)
        with pytest.raises(PermissionDeniedError):
            await RecordCardDeliveryEvidenceUseCase().submit_report(command, actor=author(denied), uow=uow)
        command.batch_command.assert_not_called()
        uow.commit.assert_not_awaited()
    assert await store.enumerate(BOARD) == ()


@pytest.mark.parametrize('late_failure', [False, True])
async def test_last_delivery_batch_and_learning_share_one_report_and_rollback(runtime, independent_gates, late_failure):
    from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
    from okto_pulse.community.adapters.sqlalchemy_models import CardDeliveryEvidenceRecordRow
    from okto_pulse.core.application.use_cases.delivery_evidence import RecordCardDeliveryEvidenceUseCase
    from okto_pulse.core.models.delivery_evidence import card_delivery_command
    from test_delivery_report import request as report_request
    factory, _, store, _ = runtime
    bug_id, data = await prepare(runtime, 'validation')
    payload = report_request().model_dump(mode='json', exclude={'board_id', 'card_id', 'spec_id'})
    payload['batch']['expected_card_version'] = data.learning_submission.expected_source_version
    payload['report'] = data.model_dump(mode='json', exclude_none=True)
    command = card_delivery_command(board_id=BOARD, card_id=bug_id, spec_id='spec-bug-context', evidence=payload)
    async with factory() as session:
        uow = unit(session)
        uow.services.delivery_evidence = CommunityDeliveryEvidenceStore(session)
        writer = uow.services.cards
        if late_failure:
            async def fail(*args, **kwargs):
                await writer.move_card(*args, **kwargs)
                raise RuntimeError('late_joint_delivery_failure')
            uow.services.cards = SimpleNamespace(move_card=fail, get_card=writer.get_card)
        case = RecordCardDeliveryEvidenceUseCase()
        if late_failure:
            with pytest.raises(RuntimeError, match='late_joint_delivery_failure'):
                await case.execute(command, actor=author(), uow=uow)
            await session.commit()
        else:
            saved = await case.execute(command, actor=author(), uow=uow)
            assert saved['report']
            assert (await case.execute(command, actor=author(), uow=uow))['replayed']
    assert len(await store.enumerate(BOARD)) == (0 if late_failure else 1)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CardDeliveryEvidenceRecordRow)) == (0 if late_failure else 1)
        bug = await session.get(Card, bug_id)
        assert len(bug.conclusions) == (0 if late_failure else 1)
        assert bug.status.value == ('in_progress' if late_failure else 'validation')


async def test_concurrent_joint_retries_and_retry_after_reopen_do_not_repeat_work(runtime, independent_gates):
    factory, assembler, store, _ = runtime
    bug_id, data = await prepare(runtime, 'done')
    async def submit():
        async with factory() as session:
            await MoveCardUseCase().execute(MoveCardCommand(bug_id, data), actor=author(), uow=unit(session))
    await asyncio.wait_for(asyncio.gather(submit(), submit()), timeout=30)
    assert len(await store.enumerate(BOARD)) == 1
    async with factory() as session:
        bug = await session.get(Card, bug_id)
        assert len(bug.conclusions) == len(bug.learning_closeout_bindings) == 1
        binding = bug.learning_closeout_bindings[0]
        count = await session.scalar(select(func.count()).select_from(DomainEventRow))
        # Simulate an already authorized later reopen; this case tests retry,
        # not admission of that separate lifecycle operation.
        bug.status = 'in_progress'
        await session.commit()
        await MoveCardUseCase().execute(MoveCardCommand(bug_id, data), actor=author(), uow=unit(session))
        assert (await session.get(Card, bug_id)).status.value == 'in_progress'
        current = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=bug_id)
        assert not closeout_binding_is_current(binding, current)
        assert await session.scalar(select(func.count()).select_from(DomainEventRow)) == count
