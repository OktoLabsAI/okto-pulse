"""Authorized semantic capture through real SQL and signed evidence adapters."""

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.bug_cognitive_context import CommunityBugCognitiveContextAssembler
from okto_pulse.community.adapters.sqlalchemy_kg_cognitive_source import CommunitySqlAlchemyCognitiveSourceStore
from okto_pulse.community.adapters.sqlalchemy_models import Board, Card, Spec
from okto_pulse.community.adapters.test_evidence import (
    CommunityEvidenceLedger, CommunityTestVerificationReportIssuer, CommunityTestEvidenceWriteVerifier,
)
from okto_pulse.core.application.kg_operations import CoreKnowledgeGraphOperations
from okto_pulse.core.application.use_cases.base import ActorContext, PermissionDeniedError
from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase, LEARNING_CAPTURE_CREATE_PERMISSIONS
from okto_pulse.core.ports.bug_cognitive_context import register_bug_cognitive_context_assembler, reset_bug_cognitive_context_ports_for_tests
from okto_pulse.core.ports.kg_cognitive_source import register_cognitive_source_store, reset_cognitive_source_store_for_tests
from okto_pulse.core.ports.learning_capture import CreateLearningCapture
from okto_pulse.core.ports.permission_policy import set_permission_flag
from okto_pulse.core.ports.test_evidence import (
    TestVerificationReportRequest as ReportRequest, register_test_evidence_write_verifier,
    reset_test_evidence_write_verifier_for_tests,
)
from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256
from test_bug_cognitive_context_adapter import _runtime, _seed_full_context, _CanonicalBugReader
from test_verification_report_admission import report

BOARD = 'board-bug-context'


@pytest.fixture
async def runtime(tmp_path):
    engine, factory = await _runtime(tmp_path / 'capture.db')
    await _seed_full_context(factory)
    ledger = CommunityEvidenceLedger(evidence_root=tmp_path / 'evidence')
    issuer = CommunityTestVerificationReportIssuer(ledger=ledger)
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=ledger))
    assembler = CommunityBugCognitiveContextAssembler(_CanonicalBugReader(fail=True))
    store = CommunitySqlAlchemyCognitiveSourceStore(factory)
    register_bug_cognitive_context_assembler(assembler)
    register_cognitive_source_store(store)
    try:
        async with factory() as session:
            spec = await session.get(Spec, 'spec-bug-context')
            scenario = {**spec.test_scenarios[0], 'verification_method': 'inspection'}
            digest = compute_test_scenario_semantic_sha256(board_id=BOARD, spec_id=spec.id,
                scenario=scenario, acceptance_criteria=spec.acceptance_criteria)
            value = report()
            value['observations'][0]['criterion_id'] = 'ac-about'
            issued = await issuer.admit(ReportRequest(board_id=BOARD, spec_id=spec.id,
                scenario_id=scenario['id'], scenario_sha256=digest, actor_id='reviewer', report=value))
            spec.test_scenarios = [{**scenario, 'evidence': dict(issued.evidence)}]
            bug = await session.get(Card, 'bug-context')
            bug.status = 'in_progress'
            await session.commit()
            source = await assembler.assemble_semantic(session, board_id=BOARD, bug_id='bug-context')
        request = CreateLearningCapture(BOARD, 'bug-context', 'capture-1', source.source_digest,
            source.source_policy_version, 'Keep version metadata authoritative.', 'Compiled frontend releases',
            'Builds containing generated version metadata', ('scenario-regression',))
        yield factory, assembler, store, request
    finally:
        reset_bug_cognitive_context_ports_for_tests()
        reset_cognitive_source_store_for_tests()
        reset_test_evidence_write_verifier_for_tests()
        await engine.dispose()


def actor(denied=None):
    flags = {}
    for flag in LEARNING_CAPTURE_CREATE_PERMISSIONS:
        set_permission_flag(flags, flag, flag != denied)
    return ActorContext('author', 'mcp', board_id=BOARD, permissions=flags)


def uow(session):
    async def get_board(identity): return await session.get(Board, identity)
    async def get_card(identity): return await session.get(Card, identity)
    return SimpleNamespace(boards=SimpleNamespace(get=get_board), services=SimpleNamespace(
        cards=SimpleNamespace(get_card=get_card), kg=CoreKnowledgeGraphOperations(session,
            clock=lambda: datetime(2026, 9, 24, 15, tzinfo=timezone.utc))))


async def test_capture_persists_before_done_replays_in_same_uow_and_conflicts_without_overwriting(runtime):
    factory, _, store, request = runtime
    case = StageLearningCaptureUseCase()
    async with factory() as session:
        first = await case.execute(request, actor=actor(), uow=uow(session))
        replay = await case.execute(request, actor=actor(), uow=uow(session))
        assert replay.record_fingerprint == first.record_fingerprint
        assert (await session.get(Card, request.bug_id)).status.value == 'in_progress'
        await session.commit()
    async with factory() as session:
        retry = await case.execute(request, actor=actor(), uow=uow(session))
        assert retry.record_fingerprint == first.record_fingerprint
        with pytest.raises(ValueError, match='idempotency_conflict'):
            await case.execute(replace(request, content='A different lesson.'), actor=actor(), uow=uow(session))
        await session.rollback()
    records = await store.enumerate(BOARD)
    assert len(records) == 1
    assert records[0].payload['author_id'] == 'author'
    assert records[0].payload['content'] == request.content
    assert records[0].evidence_refs == ('spec:spec-bug-context:test_scenario:scenario-regression',)


async def test_capture_rollback_leaves_no_durable_record(runtime):
    factory, _, store, request = runtime
    async with factory() as session:
        await StageLearningCaptureUseCase().execute(request, actor=actor(), uow=uow(session))
        await session.rollback()
    assert await store.enumerate(BOARD) == ()


async def test_concurrent_same_capture_request_commits_one_source(runtime):
    import asyncio
    factory, _, store, request = runtime
    async def submit():
        async with factory() as session:
            result = await StageLearningCaptureUseCase().execute(request, actor=actor(), uow=uow(session))
            await session.commit()
            return result.record_fingerprint
    left, right = await asyncio.wait_for(asyncio.gather(submit(), submit()), timeout=30)
    assert left == right
    assert len(await store.enumerate(BOARD)) == 1


@pytest.mark.parametrize('damage', ['board', 'realm', 'missing_bug'])
async def test_capture_denies_scope_mismatch_before_staging(runtime, damage):
    from okto_pulse.core.application.use_cases.base import EntityNotFoundError
    factory, _, store, request = runtime
    principal = actor()
    if damage == 'board':
        request = replace(request, board_id='foreign-board')
    elif damage == 'realm':
        async with factory() as session:
            board = await session.get(Board, BOARD)
            board.realm_id = 'other-realm'
            await session.commit()
    else:
        request = replace(request, bug_id='missing-bug')
    async with factory() as session:
        with pytest.raises((PermissionDeniedError, EntityNotFoundError)):
            await StageLearningCaptureUseCase().execute(request, actor=principal, uow=uow(session))
        await session.rollback()
    assert await store.enumerate(BOARD) == ()


@pytest.mark.parametrize('denied', LEARNING_CAPTURE_CREATE_PERMISSIONS)
async def test_each_required_authority_is_enforced_before_any_source_or_writer_access(denied):
    request = CreateLearningCapture(BOARD, 'bug', 'request', 'a' * 64, 1, 'lesson', 'context', 'scope', ('scenario',))
    with pytest.raises(PermissionDeniedError):
        await StageLearningCaptureUseCase().execute(request, actor=actor(denied), uow=SimpleNamespace())


@pytest.mark.parametrize('damage', ['stale_digest', 'stale_version', 'unlinked_scenario', 'forged_receipt'])
async def test_capture_refuses_stale_or_unauthenticated_basis_without_staging(runtime, damage):
    factory, assembler, store, request = runtime
    async with factory() as session:
        if damage == 'stale_digest': request = replace(request, expected_source_digest='b' * 64)
        elif damage == 'stale_version': request = replace(request, expected_source_version=request.expected_source_version + 1)
        elif damage == 'unlinked_scenario': request = replace(request, scenario_ids=('unrelated',))
        else:
            spec = await session.get(Spec, 'spec-bug-context')
            scenario = spec.test_scenarios[0]
            spec.test_scenarios = [{**scenario, 'evidence': {**scenario['evidence'], 'execution_receipt': 'forged'}}]
            await session.commit()
            fresh = await assembler.assemble_semantic(session, board_id=BOARD, bug_id=request.bug_id)
            request = replace(request, expected_source_digest=fresh.source_digest)
        with pytest.raises(ValueError, match='learning_capture_(source_changed_or_unavailable|evidence_not_authenticated)'):
            await StageLearningCaptureUseCase().execute(request, actor=actor(), uow=uow(session))
        await session.rollback()
    assert await store.enumerate(BOARD) == ()
