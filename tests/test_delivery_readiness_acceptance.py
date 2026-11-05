"""Named plan acceptance over canonical inventories and native proof readers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

import test_delivery_evidence_integration as delivery
import test_multicard_delivery_integration as multi
from test_delivery_readiness_batch import Clock, read, reader, statements
from test_native_delivery_currentness import advance_source
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, Spec, CardDeliveryEvidenceRecordRow as Record, CodeInvestigationReceiptRow as Receipt,
    VerificationAcceptanceRow as Acceptance, VerificationScenarioStateRow as ScenarioState,
)
from okto_pulse.core.domain.delivery_assessment import assess_obligation_delivery, aggregate_delivery_metrics
from okto_pulse.core.services.delivery_readiness_diagnostics import compose_delivery_diagnostics

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


async def test_M15_inherited_business_rule_preserves_path_and_reuses_actual_proof(ledger, tmp_path):
    session, store = await multi.setup(ledger)
    ui = await delivery.record(store, multi.implementation())
    auth = await delivery.record(store, multi.implementation('authorization'))
    await multi.bind_test(session, store, tmp_path, 'ui', [ui['id']])
    await session.commit()
    card_ids = set(await session.scalars(select(Card.id)))
    batch = await read(reader(session))
    assessment = assess_obligation_delivery(batch.facts)
    rule = next(row for row in assessment.obligations if row.binding.obligation_ref == 'br:br')
    assert rule.implementation.proof_satisfied and not rule.verification.proof_satisfied
    diagnostics = compose_delivery_diagnostics(assessment, batch.facts)
    rule_causes = {cause.id for cause in diagnostics.graph.causes
        if any(unit.ref == 'br:br' for unit in cause.units)}
    derived = [fact for detail in diagnostics.details if detail.issue_id in rule_causes
        for fact in detail.expected if getattr(fact, 'source_obligation_ref', None) == 'fr:fr']
    assert derived, 'Readiness must expose the actual FR -> BR responsibility path'
    contribution = next(row for row in batch.facts.effective_context.inventory.rows
        if row.binding.obligation_ref == 'br:br').contributions[0]
    assert contribution.origin == 'inherited'
    assert all(fact.obligation_ref == 'br:br' and fact.card_id == 'authorization'
        and fact.scope_sha256 == contribution.scope_sha256
        and fact.source_scope_sha256 == contribution.sources[0].scope_sha256 for fact in derived)
    await multi.bind_test(session, store, tmp_path, 'auth', [auth['id']])
    await session.commit()
    completed = assess_obligation_delivery((await read(reader(session))).facts)
    metrics = aggregate_delivery_metrics(completed)
    assert metrics.implementation.percent == metrics.verification.percent == 100
    assert next(row for row in completed.obligations if row.binding.obligation_ref == 'br:br').verification.proof_satisfied
    assert set(await session.scalars(select(Card.id))) == card_ids
    assert len(list(await session.scalars(select(Record).where(Record.kind == 'test')))) == 2


@pytest.mark.parametrize('change', ['independent_source', 'expired_investigation'], ids=['V22', 'V24'])
async def test_V22_V24_current_delivery_is_not_invalidated_by_unrelated_head_or_investigation_expiry(ledger, change):
    session, store, _ = ledger
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(Record))}
    before = aggregate_delivery_metrics(assess_obligation_delivery((await read(reader(session))).facts))
    clock = Clock()
    if change == 'independent_source':
        await advance_source(session, independent_target=True)
    else:
        expiration = max(await session.scalars(select(Receipt.expires_at)))
        clock.value = expiration + timedelta(days=1)
        assert clock.now() > expiration
    await session.rollback()
    with statements(session.bind) as sql:
        batch = await read(reader(session, clock=clock))
    assessed = assess_obligation_delivery(batch.facts)
    assert aggregate_delivery_metrics(assessed) == before
    assert before.implementation.percent == before.verification.percent == 100
    assert all(statement.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for statement in sql)
    assert {row.id: row.payload for row in await session.scalars(select(Record))} == history


async def test_V18_one_authenticated_result_verifies_two_compatible_implementations(ledger, tmp_path):
    from verification_report_fixtures import report, bind_report_sources
    from okto_pulse.community.adapters.test_evidence import (
        CommunityEvidenceLedger, CommunityTestVerificationReportIssuer,
        CommunityTestEvidenceWriteVerifier,
    )
    from okto_pulse.core.ports.test_evidence import (
        TestVerificationReportRequest as ReportRequest, register_test_evidence_write_verifier,
    )
    from okto_pulse.core.services.test_scenario_lifecycle import compute_test_scenario_semantic_sha256

    session, store = await multi.setup(ledger)
    spec = await session.get(Spec, delivery.SPEC_ID)
    scenario = {**spec.test_scenarios[0], 'verification_method': 'inspection',
                'linked_criteria': ['ac-ui', 'ac-auth']}
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(test_scenarios=[scenario]))
    await session.execute(update(Card).where(Card.id == 'test').values(test_scenario_ids=[scenario['id']]))
    await session.commit()
    card_ids = set(await session.scalars(select(Card.id)))
    ui = await delivery.record(store, multi.implementation())
    auth = await delivery.record(store, multi.implementation('authorization'))
    value = report()
    await bind_report_sources(session, value, ('execution', 'execution-auth'))
    value['observed_at'] = datetime.now(timezone.utc).isoformat()
    value['result'] = 'passed'
    value['observations'] = [
        {**value['observations'][0], 'observation_id': f'observation-{criterion}',
         'criterion_id': criterion, 'outcome': 'passed'}
        for criterion in ('ac-ui', 'ac-auth')
    ]
    native = CommunityEvidenceLedger(evidence_root=tmp_path / 'reused-report')
    digest = compute_test_scenario_semantic_sha256(board_id=delivery.BOARD_ID,
        spec_id=delivery.SPEC_ID, scenario=scenario, acceptance_criteria=spec.acceptance_criteria)
    context = await delivery.reserve_verification(session, scenario['id'], actor_id='tester')
    issued = await CommunityTestVerificationReportIssuer(ledger=native).admit(ReportRequest(
        board_id=delivery.BOARD_ID, spec_id=delivery.SPEC_ID, scenario_id=scenario['id'],
        scenario_sha256=digest, actor_id='tester', report=value, verification_context=context))
    register_test_evidence_write_verifier(CommunityTestEvidenceWriteVerifier(ledger=native))
    await delivery.publish_verification(session, dict(issued.evidence), 'passed', actor_id='tester')
    proof = await delivery.record(store, delivery.command('test', scenario_id=scenario['id'],
        idempotency_key='one-result-two-implementations', implementation_ids=[ui['id'], auth['id']],
        obligation_refs=list(dict.fromkeys(multi.UI_REFS + multi.AUTH_REFS))))
    await session.commit()

    batch = await read(reader(session))
    for card_id in (None, 'task', 'authorization'):
        assessment = assess_obligation_delivery(batch.facts,
            **({'card_id': card_id, 'card_type': 'normal'} if card_id else {}))
        metrics = aggregate_delivery_metrics(assessment)
        assert metrics.implementation.percent == metrics.verification.percent == 100
    tests = list(await session.scalars(select(Record).where(Record.kind == 'test')))
    assert len(tests) == 1 and tests[0].id == proof['id']
    assert set(tests[0].payload['implementation_ids']) == {ui['id'], auth['id']}
    assert len(list(native.receipt_root.glob('*.json'))) == 1
    assert set(await session.scalars(select(Card.id))) == card_ids


async def test_V19_semantic_scenario_change_removes_credit_and_preserves_signed_history(ledger):
    session, store, _ = ledger
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    before = aggregate_delivery_metrics(assess_obligation_delivery((await read(reader(session))).facts))
    assert before.verification.percent == 100
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(Record))}
    accepted_history = {row.id: deepcopy(row.evidence) for row in await session.scalars(select(Acceptance))}
    generation = await session.scalar(select(ScenarioState.generation).where(
        ScenarioState.scenario_id == delivery.SCENARIO['id']))
    spec = await session.get(Spec, delivery.SPEC_ID)
    scenarios = deepcopy(spec.test_scenarios)
    evidence = deepcopy(scenarios[0]['evidence'])
    scenarios[0]['then'] = 'A different business outcome must be observed'
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(test_scenarios=scenarios))
    await session.commit()

    batch = await read(reader(session))
    assessment = assess_obligation_delivery(batch.facts)
    metrics = aggregate_delivery_metrics(assessment)
    assert metrics.implementation.percent == 100 and metrics.verification.percent == 0
    population = batch.facts.effective_context.native_verification
    assert population.complete and population.runs == ()
    assert await session.scalar(select(ScenarioState.generation).where(
        ScenarioState.scenario_id == delivery.SCENARIO['id'])) == generation + 1
    diagnostics = compose_delivery_diagnostics(assessment, batch.facts)
    assert diagnostics.graph.causes
    assert all(not fact.current_verified_run for fact in batch.facts.tests)
    assert {row.id: row.payload for row in await session.scalars(select(Record))} == history
    assert {row.id: row.evidence for row in await session.scalars(select(Acceptance))} == accepted_history
    await session.refresh(spec)
    assert spec.test_scenarios[0]['evidence'] == evidence


async def two_basis_delivery(ledger, tmp_path):
    """Two real HTTP observations, one scenario, disjoint current source bases."""
    session, store = await multi.setup(ledger, authorization_source_ref='source-independent',
        authorization_revision='b' * 40)
    spec = await session.get(Spec, delivery.SPEC_ID, populate_existing=True)
    criteria = deepcopy(spec.acceptance_criteria)
    requirements = []
    for criterion, name, card in zip(criteria, ('ui', 'auth'), ('task', 'authorization'), strict=True):
        criterion['requirement_links'] = [{'requirement_type': 'functional_requirement', 'requirement_id': 'fr-' + name}]
        requirements.append({'id': 'fr-' + name, 'text': criterion['text'], 'linked_task_ids': [card],
            'verification': {'mode': 'explicit', 'required_profiles': [criterion['verification_profile']]},
            'implementation_plan': {'contributions': [{'card_id': card, 'scope': 'whole_requirement'}]}})
    scenario = {**spec.test_scenarios[0], 'id': 'ts-shared', 'linked_criteria': ['ac-ui', 'ac-auth'],
        'given': 'Independently implemented payment display and authorization',
        'when': 'The shared acceptance scenario runs against one exact source basis'}
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(
        functional_requirements=requirements, acceptance_criteria=criteria, business_rules=[], test_scenarios=[scenario]))
    await session.execute(update(Card).where(Card.id == 'test').values(test_scenario_ids=['ts-shared']))
    await session.commit()
    results = {}
    for name, card, execution_id in (('ui', 'task', 'execution'), ('auth', 'authorization', 'execution-auth')):
        refs = ['fr:fr-' + name, 'ac:ac-' + name]
        implemented = await delivery.record(store, delivery.command(card_id=card, execution_id=execution_id,
            idempotency_key='two-basis-implementation-' + name, obligation_refs=refs))
        results[name] = {'card_id': card, 'execution_id': execution_id, 'refs': refs, 'implementation_id': implemented['id']}
    for name, basis in results.items():
        basis['accepted'] = await run_and_bind_basis(session, store, tmp_path, basis, name)
    return session, store, results


async def run_and_bind_basis(session, store, tmp_path, basis, key):
    _, accepted = await multi.run_result(session, tmp_path, 'shared', execution_id=basis['execution_id'])
    association = await delivery.record(store, delivery.command('test', scenario_id='ts-shared',
        accepted_result_id=accepted.accepted_result_id, implementation_ids=[basis['implementation_id']],
        obligation_refs=basis['refs'], idempotency_key='two-basis-test-' + key))
    await session.commit()
    stored = await session.get(Record, association['id'])
    assert stored.payload['accepted_result_id'] == accepted.accepted_result_id
    return accepted


async def current_shared_runs(session):
    batch = await read(reader(session))
    population = batch.facts.effective_context.native_verification
    assert population.complete
    runs = tuple(run for run in population.runs if run.scenario_id == 'ts-shared')
    assert all(run.current and run.authenticated for run in runs)
    return batch, runs


async def proof_by_requirement(store):
    projection = await store.projection(delivery.BOARD_ID, delivery.SPEC_ID)
    return {ref: (row['implementation_satisfied'], row['test_satisfied'])
        for ref, row in multi.rows(projection).items()}


def expected_two_basis_proof(ui=True, auth=True):
    return {kind + ':' + kind + '-' + name: (True, passed)
        for kind in ('fr', 'ac') for name, passed in (('ui', ui), ('auth', auth))}


async def test_R6_independent_bases_remain_verified_in_one_final_projection(ledger, tmp_path):
    session, store, bases = await two_basis_delivery(ledger, tmp_path)
    batch, runs = await current_shared_runs(session)
    assert len(runs) == 2 and len({run.basis_sha256 for run in runs}) == 2
    assert {run.accepted_result_id for run in runs} == {basis['accepted'].accepted_result_id for basis in bases.values()}
    assert {tuple((s.source_ref, s.revision) for s in run.sources) for run in runs} == {
        (('source-main', 'a' * 40),), (('source-independent', 'b' * 40),)}
    assert len(batch.facts.tests) == 2 and all(f.current_verified_run for f in batch.facts.tests)
    observed = {}
    for card_id in (None, 'task', 'authorization'):
        assessment = assess_obligation_delivery(batch.facts,
            **({'card_id': card_id, 'card_type': 'normal'} if card_id else {}))
        metrics = aggregate_delivery_metrics(assessment)
        observed[card_id or 'spec'] = (metrics.implementation.percent, metrics.verification.percent)
    assert observed == {'spec': (100, 100), 'task': (100, 100), 'authorization': (100, 100)}
    assert await proof_by_requirement(store) == expected_two_basis_proof()


async def test_R6_same_basis_failure_only_impacts_its_requirement_and_recovery_requires_binding(ledger, tmp_path):
    session, store, bases = await two_basis_delivery(ledger, tmp_path)
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(Record))}
    _, failed = await multi.run_result(session, tmp_path, 'shared', passed=False, execution_id='execution-auth')
    _, runs = await current_shared_runs(session)
    assert {(run.sources[0].source_ref, run.outcome) for run in runs} == {
        ('source-main', 'passed'), ('source-independent', 'failed')}
    assert failed.accepted_result_id in {run.accepted_result_id for run in runs}
    observed = {'failure_before_binding': await proof_by_requirement(store)}
    _, fresh = await multi.run_result(session, tmp_path, 'shared', execution_id='execution-auth')
    observed['fresh_pass_before_binding'] = await proof_by_requirement(store)
    await delivery.record(store, delivery.command('test', scenario_id='ts-shared',
        accepted_result_id=fresh.accepted_result_id, implementation_ids=[bases['auth']['implementation_id']],
        obligation_refs=bases['auth']['refs'], idempotency_key='two-basis-recovered'))
    await session.commit()
    observed['fresh_pass_bound'] = await proof_by_requirement(store)
    assert all([(await session.get(Record, identity)).payload == value for identity, value in history.items()])
    assert observed == {
        'failure_before_binding': expected_two_basis_proof(auth=False),
        'fresh_pass_before_binding': expected_two_basis_proof(auth=False),
        'fresh_pass_bound': expected_two_basis_proof(),
    }


@pytest.mark.parametrize('change', ['reset', 'semantic'])
async def test_R6_reset_never_revives_old_bases_and_fresh_bindings_restore_each_requirement(ledger, tmp_path, change):
    session, store, bases = await two_basis_delivery(ledger, tmp_path)
    history = {row.id: deepcopy(row.payload) for row in await session.scalars(select(Record))}
    accepted_history = {row.id: deepcopy(row.evidence) for row in await session.scalars(select(Acceptance))}
    spec = await session.get(Spec, delivery.SPEC_ID, populate_existing=True)
    original = deepcopy(spec.test_scenarios)
    changed = deepcopy(original)
    if change == 'reset':
        changed[0].update(status='ready', evidence=None)
    else:
        changed[0]['then'] = 'A new business condition must be observed'
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(test_scenarios=changed))
    await session.commit()
    _, runs = await current_shared_runs(session)
    assert runs == ()
    assert await proof_by_requirement(store) == expected_two_basis_proof(ui=False, auth=False)
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(test_scenarios=original))
    await session.commit()
    _, runs = await current_shared_runs(session)
    assert runs == (), 'Restoring identical semantics or a signed summary must not revive accepted history'
    assert await proof_by_requirement(store) == expected_two_basis_proof(ui=False, auth=False)
    await run_and_bind_basis(session, store, tmp_path, bases['ui'], 'ui-after-reset')
    observed = {'first_basis_restored': await proof_by_requirement(store)}
    await run_and_bind_basis(session, store, tmp_path, bases['auth'], 'auth-after-reset')
    observed['both_bases_restored'] = await proof_by_requirement(store)
    assert all([(await session.get(Record, identity)).payload == value for identity, value in history.items()])
    assert all([(await session.get(Acceptance, identity)).evidence == value for identity, value in accepted_history.items()])
    assert set(await session.scalars(select(Card.status))) == {'done'}
    assert observed == {'first_basis_restored': expected_two_basis_proof(auth=False),
                        'both_bases_restored': expected_two_basis_proof()}
