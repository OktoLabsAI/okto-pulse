"""Native new-schema Decision inspections, without artificial code/Test Cards."""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, func

from okto_pulse.community.adapters.sqlalchemy_database import build_community_engine, install_community_sqlite_pragmas, build_community_session_factory
from okto_pulse.community.adapters.current_relational_schema import current_schema_contract, initialize_current_schema
from okto_pulse.community.adapters.sqlalchemy_models import Board, Spec, SpecHistory, DeliveryEvidenceRecordRow as Record
from okto_pulse.community.adapters.sqlalchemy_delivery_evidence import CommunityDeliveryEvidenceStore
from okto_pulse.core.domain.architecture_adoption import ArchitectureAdoptionScope
from okto_pulse.core.domain.execution_contract import new_execution_contract
from okto_pulse.core.domain.delivery_inventory import COLLECTIONS
from okto_pulse.core.domain.delivery_evidence import DeliveryScope, evaluate_delivery_coverage
from okto_pulse.core.models.decision_review import DecisionReviewQuery, DecisionReviewCommand
from okto_pulse.core.models.delivery_evidence import DeliveryEvidenceCommand
from okto_pulse.community.adapters.test_evidence import CommunityEvidenceLedger, CommunityTestEvidenceWriteVerifier
from okto_pulse.core.ports.test_evidence import register_test_evidence_write_verifier, reset_test_evidence_write_verifier_for_tests

QUERY = DecisionReviewQuery(board_id='board', spec_id='spec')


@pytest_asyncio.fixture
async def reviews(tmp_path):
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(
        ledger=CommunityEvidenceLedger(evidence_root=tmp_path / 'evidence')))
    engine = build_community_engine(f"sqlite+aiosqlite:///{tmp_path / 'reviews.sqlite'}")
    install_community_sqlite_pragmas(engine)
    await initialize_current_schema(engine, current_schema_contract())
    factory = build_community_session_factory(engine)
    async with factory() as session:
        session.add(Board(id='board', name='Review', realm_id='local', owner_id='owner',
                          settings={'reviewer_separation_mode': 'enforce'}))
        await session.flush()
        decision = {'id': 'choice', 'title': 'Only the mock', 'rationale': 'Bounded exercise', 'status': 'active',
            'verification': {'inspection': {'condition': 'The observed delivery is limited to the mock.',
                'scope_refs': [{'kind': 'spec', 'id': 'spec'}]}}}
        values = {field: [] for _, field in COLLECTIONS}
        values['decisions'] = [decision]
        spec = Spec(id='spec', board_id='board', title='Mock', status='in_progress', created_by='owner',
            architecture_adoption=ArchitectureAdoptionScope(board_id='board', spec_id='spec', adopted_in_edition=1,
                actor_id='owner', inherited_resource_ids=()).model_dump(mode='json'),
            execution_contract=new_execution_contract(board_id='board', spec_id='spec', edition=1,
                actor_id='owner', origin='new_spec'), test_scenarios=[], **values)
        session.add(spec)
        await session.flush()
        session.add(SpecHistory(spec_id='spec', action='created', actor_type='user', actor_id='owner',
            actor_name='Owner', version=1, changes=[{'field': 'decisions', 'old': None, 'new': [decision]}]))
        await session.commit()
        yield session, CommunityDeliveryEvidenceStore(session)
    await engine.dispose()
    reset_test_evidence_write_verifier_for_tests()


async def command(store, result='passed', reconciles=(), query=QUERY):
    projection = await store.decision_reviews(query, actor_id='reviewer')
    basis = projection['decisions'][0]['basis']
    return DecisionReviewCommand(board_id=query.board_id, spec_id=query.spec_id, expected_version=projection['version'],
        expected_edition=projection['edition'], expected_review_revision=projection['review_revision'],
        idempotency_key=uuid.uuid4().hex, entries=[{'decision_id': 'choice',
            'expected_scope_sha256': basis['scope_sha256'], 'sources': basis['sources'],
            'observed': 'Inspected the exact native specification and delivery manifest.',
            'result': result, 'reconciles': list(reconciles)}])


async def record(store, cmd):
    return await store.record_decision_reviews(cmd, actor_id='reviewer', actor_kind='agent')


@pytest.mark.asyncio
async def test_native_scope_review_satisfies_without_cards_and_replays(reviews):
    session, store = reviews
    cmd = await command(store)
    first = await record(store, cmd)
    await session.commit()
    assert (await record(store, cmd)) == {'id': first['id'], 'replayed': True}
    evaluation = evaluate_delivery_coverage(await store.load_snapshot(DeliveryScope('board', 'spec', 1)))
    # An otherwise empty Spec still has its separate scope/requirement gate.
    # This inspection satisfies only the Decision, without relaxing that gate.
    assert 'decision_verification_incomplete' not in evaluation.blockers
    assert evaluation.rows[0].test_satisfied
    assert evaluation.rows[0].decision_verification_status == 'verified'
    assert evaluation.rows[0].implementation_ids == ()
    assert await session.scalar(select(func.count()).select_from(Record)) == 1


@pytest.mark.asyncio
async def test_native_review_agrees_with_coverage_and_does_not_add_implementation_credit(reviews):
    from datetime import datetime, timezone, timedelta
    from okto_pulse.core.ports.spec_coverage_query import SpecCoverageQuery, SpecCoverageSnapshot
    from okto_pulse.core.services.spec_coverage_query import project_spec_coverage
    from okto_pulse.core.ports.analytics_foundation import AnalyticsFoundationQuery, AnalyticsUtcWindow
    from okto_pulse.core.services.coverage_traceability_read_model import build_coverage_traceability_projection
    session, store = reviews
    spec = await session.get(Spec, 'spec')
    now = datetime.now(timezone.utc)
    read = SpecCoverageQuery('board', 'spec', 'reviewer')
    async def coverage():
        snapshot = await store.load_snapshot(DeliveryScope('board', 'spec', 1))
        return project_spec_coverage(read, SpecCoverageSnapshot(snapshot.scope, 'reviewer',
            'native-source', now, spec, (), True, snapshot)), evaluate_delivery_coverage(snapshot)
    before, _ = await coverage()
    receipt = await record(store, await command(store))
    after, evaluation = await coverage()
    # Empty executable inventory remains incomplete, even after its Decision is inspected.
    assert after['delivery']['counts']['decisions_verified'] is None
    assert before['delivery']['counts']['decisions_verified'] is None
    assert after['delivery']['counts']['implementation_proven'] is before['delivery']['counts']['implementation_proven'] is None
    row = next(r for r in after['items'] if r.get('obligation_ref') == 'decision:choice')
    assert row['implementation'] == 'not_applicable'
    assert row['decision_verification_status'] == 'verified'
    query = AnalyticsFoundationQuery(board_id='board', actor_scope_ref='reviewer',
        window=AnalyticsUtcWindow(now - timedelta(days=1), now + timedelta(seconds=1)), as_of=now)
    effective = next(row for row in evaluation.rows if row.obligation.binding.obligation_ref == 'decision:choice')
    projection = build_coverage_traceability_projection(query=query, as_of=now,
        specs=[spec], cards=[], decision_delivery={'spec': {'choice': effective}})
    decision = next(g for g in projection.coverage if g.obligation_type.value == 'decision').rows[0]
    assert decision.covered and decision.evidence == ()
    assert decision.decision_proof_refs == (receipt['id'],)


@pytest.mark.asyncio
async def test_conflicting_result_is_not_hidden_until_explicit_reconciliation(reviews):
    _, store = reviews
    a = await record(store, await command(store))
    b = await record(store, await command(store, 'failed'))
    c = await record(store, await command(store))
    projection = await store.decision_reviews(QUERY, actor_id='reviewer')
    assert projection['decisions'][0]['status'] == 'conflict'
    cmd = await command(store, reconciles=[a['id']])
    with pytest.raises(ValueError, match='reconciliation_conflict'):
        await record(store, cmd)
    await record(store, await command(store, reconciles=[a['id'], b['id'], c['id']]))
    projection = await store.decision_reviews(QUERY, actor_id='reviewer')
    assert projection['decisions'][0]['status'] == 'verified'
    assert len(projection['history']) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('change,error', [('source','source_unresolved'), ('scope','scope_conflict'),
    ('version','version_conflict'), ('revision','revision_conflict'), ('author','reviewer_separation_required')])
async def test_forgery_and_stale_fences_rejected_before_insert(reviews, change, error):
    session, store = reviews
    cmd = await command(store)
    payload = cmd.model_dump()
    if change == 'source':
        payload['entries'][0]['sources'][0]['sha256'] = 'f' * 64
    elif change == 'scope':
        payload['entries'][0]['expected_scope_sha256'] = 'f' * 64
    elif change == 'version':
        payload['expected_version'] += 1
    elif change == 'revision':
        payload['expected_review_revision'] = 'f' * 64
    with pytest.raises(ValueError, match=error):
        await store.record_decision_reviews(DecisionReviewCommand.model_validate(payload),
            actor_id='owner' if change == 'author' else 'reviewer', actor_kind='agent')
    assert await session.scalar(select(func.count()).select_from(Record)) == 0


@pytest.mark.asyncio
async def test_material_change_and_human_revocation_block_without_erasing_history(reviews):
    session, store = reviews
    first = await record(store, await command(store))
    await store.record(DeliveryEvidenceCommand(board_id='board', spec_id='spec', expected_version=1,
        expected_edition=1, idempotency_key='revoke', kind='revoke', record_id=first['id'],
        justification='Observation withdrawn'), actor_id='owner', actor_kind='human')
    projection = await store.decision_reviews(QUERY, actor_id='reviewer')
    assert projection['decisions'][0]['status'] == 'revoked'
    await record(store, await command(store, reconciles=[first['id']]))
    spec = await session.get(Spec, 'spec')
    spec.description = 'A materially different delivery'
    await session.flush()
    projection = await store.decision_reviews(QUERY, actor_id='reviewer')
    assert projection['decisions'][0]['status'] == 'inspection_pending'
    assert len(projection['history']) == 2


@pytest.mark.asyncio
async def test_comment_or_technical_version_preserves_base_but_new_edition_does_not(reviews):
    session, store = reviews
    await record(store, await command(store))
    spec = await session.get(Spec, 'spec')
    spec.version += 1
    await session.flush()
    assert (await store.decision_reviews(QUERY, actor_id='reviewer'))['decisions'][0]['status'] == 'verified'
    spec.edition += 1
    await session.flush()
    assert (await store.decision_reviews(QUERY, actor_id='reviewer'))['decisions'][0]['status'] == 'inspection_pending'
    assert await session.scalar(select(func.count()).select_from(Record)) == 1


@pytest.mark.asyncio
async def test_atomic_batch_and_changed_idempotency_payload(reviews):
    session, store = reviews
    cmd = await command(store)
    bad = cmd.model_dump()
    bad['entries'].append({**bad['entries'][0], 'decision_id': 'foreign'})
    with pytest.raises(ValueError, match='scope_conflict'):
        await record(store, DecisionReviewCommand.model_validate(bad))
    assert await session.scalar(select(func.count()).select_from(Record)) == 0
    await record(store, cmd)
    bad = cmd.model_dump()
    bad['entries'][0]['observed'] = 'Different observation'
    with pytest.raises(ValueError, match='idempotency_conflict'):
        await record(store, DecisionReviewCommand.model_validate(bad))
    assert await session.scalar(select(func.count()).select_from(Record)) == 1


@pytest.mark.asyncio
async def test_competing_edit_before_fence_cannot_admit_old_scope(reviews, monkeypatch):
    session, store = reviews
    cmd = await command(store)
    await session.commit()
    original = store.lock_scope
    factory = build_community_session_factory(session.bind)
    async def interleave(scope):
        async with factory() as other:
            spec = await other.get(Spec, 'spec')
            spec.description = 'Changed by competing writer'
            await other.commit()
        await original(scope)
    monkeypatch.setattr(store, 'lock_scope', interleave)
    with pytest.raises(ValueError, match='(version|scope)_conflict'):
        await record(store, cmd)
    assert await session.scalar(select(func.count()).select_from(Record)) == 0


from test_delivery_evidence_integration import ledger as ledger_fixture  # noqa: E402

ledger = ledger_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)
async def test_native_obligations_and_inspection_are_and_without_extra_card(ledger):
    import test_delivery_evidence_integration as delivery
    session, store, _ = ledger
    query = DecisionReviewQuery(board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID)
    spec = await session.get(Spec, query.spec_id)
    decision = {'id': 'choice', 'title': 'The installed version', 'rationale': 'Trace the release', 'status': 'active',
        'verification': {'obligation_refs': ['fr:fr-about'], 'inspection': {'condition': 'The observed delivery is the mock.',
            'scope_refs': [{'kind': 'spec', 'id': query.spec_id}]}}}
    spec.decisions = [decision]
    session.add(SpecHistory(spec_id=spec.id, action='updated', actor_type='user', actor_id='owner',
        actor_name='Owner', version=1, changes=[{'field': 'decisions', 'old': [], 'new': [decision]}]))
    await session.flush()
    impl = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[impl['id']]))
    before = await store.projection(query.board_id, query.spec_id)
    assert not before['allowed'] and 'decision_verification_incomplete' in before['blockers']
    await record(store, await command(store, query=query))
    after = await store.projection(query.board_id, query.spec_id)
    assert after['allowed'], after['blockers']
    assert next(r for r in after['rows'] if r['decision_verification_status'])['decision_verification_status'] == 'verified'
    assert len(after['per_card']) == 2  # Existing implementation + Test Card only.
    from datetime import datetime, timezone
    from okto_pulse.core.ports.spec_coverage_query import SpecCoverageQuery, SpecCoverageSnapshot
    from okto_pulse.core.services.spec_coverage_query import project_spec_coverage
    snapshot = await store.load_snapshot(DeliveryScope(query.board_id, query.spec_id, 1))
    coverage = project_spec_coverage(SpecCoverageQuery(query.board_id, query.spec_id, 'reviewer'),
        SpecCoverageSnapshot(snapshot.scope, 'reviewer', 'native', datetime.now(timezone.utc), spec, (), True, snapshot))
    assert coverage['delivery']['counts']['decisions_verified'] == 1
    assert coverage['delivery']['counts']['decisions'] == 1
    assert coverage['delivery']['counts']['implementation_proven'] == coverage['delivery']['counts']['obligations']
