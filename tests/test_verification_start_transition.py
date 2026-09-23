"""Composed AC-VER-01/07/10 start gates with four profiles and no execution."""
from copy import deepcopy

import httpx
import pytest
from sqlalchemy import update

from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.core.domain.requirement_verification import requirement_verification_digest

import test_architecture_start_transition as start

adopted_context = start.adopted_context
classified_context = start.classified_context


async def four_profiles(db, tmp_path):
    app, _, fields = await start.complete_start_fixture(db, tmp_path)
    criteria = deepcopy(fields['acceptance_criteria'])
    scenarios = deepcopy(fields['test_scenarios'])
    requirements = {}
    for kind, field, identity, profile, condition in [
        ('technical_requirement', 'technical_requirements', 'tr', 'technical', 'Procedure view responds within 200 ms'),
        ('integration_requirement', 'integration_requirements', 'ir', 'integration', 'Procedure response contains a steps array'),
        ('observability_requirement', 'observability_requirements', 'or', 'operational', 'Five consecutive errors trigger the service alert'),
    ]:
        requirements[field] = [{'id': identity, 'text': condition, 'title': condition,
            'description': condition, 'status': 'active', 'linked_task_ids': ['task'],
            'verification': {'mode': 'explicit', 'required_profiles': [profile]},
            'implementation_plan': {'contributions': [{'card_id': 'task', 'scope': 'whole_requirement'}]}}]
        criteria.append({'id': 'ac-' + identity, 'text': condition, 'verification_profile': profile,
            'linked_task_ids': ['task'], 'requirement_links': [{'requirement_type': kind, 'requirement_id': identity}]})
        scenarios.append({'id': 'ts-' + identity, 'title': condition, 'scenario_type': 'integration',
            'status': 'ready', 'given': 'A controlled service environment', 'when': 'The specified condition is exercised',
            'then': condition, 'verification_method': 'automated_test', 'linked_criteria': ['ac-' + identity],
            'linked_task_ids': ['test-or' if identity == 'or' else 'test']})
    requirements['integration_requirements'][0].update(integration_type='api', provider='procedure-service', consumer='procedure-ui')
    requirements['observability_requirements'][0].update(signal_type='alert', target='procedure-service', threshold='five errors')
    await db.execute(update(Spec).where(Spec.id == 'spec').values(**requirements,
        acceptance_criteria=criteria, test_scenarios=scenarios))
    await db.execute(update(Card).where(Card.id == 'test').values(test_scenario_ids=['scenario', 'ts-tr', 'ts-ir']))
    db.add(Card(id='test-or', board_id='board', spec_id='spec', title='Exercise service alert',
        card_type='test', status='not_started', created_by='author', test_scenario_ids=['ts-or']))
    await db.commit()
    return app, requirements, fields


async def classify_all(client, db, key):
    row = await db.get(Spec, 'spec', populate_existing=True)
    population = await start.read(db)
    response = await client.post('/api/v1/boards/board/specs/spec/architecture-classifications', json={
        'expected_spec_version': row.version, 'expected_spec_edition': row.edition, 'idempotency_key': key,
        'decisions': [{'candidate_ref': item.id, 'expected_source_digest': item.source_digest,
            'disposition': 'context_only', 'reason': 'External interface outside this procedure implementation'}
            for item in population.candidates]})
    assert response.status_code == 200, response.text


async def patch_requirement(client, db, kind, identity, payload):
    row = await db.get(Spec, 'spec', populate_existing=True)
    response = await client.patch(f'/api/v1/specs/spec/structured-entities/{kind}/{identity}',
        json={'expected_spec_version': row.version, 'payload': payload})
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['missing_profile', 'missing_operational_work', 'cycle', 'missing_terminal', 'orphan_criterion'])
async def test_real_start_requires_four_profile_plan_and_valid_inheritance(classified_context, tmp_path, damage):
    db = classified_context
    app, original, fields = await four_profiles(db, tmp_path)
    changed = deepcopy(original)
    if damage == 'missing_profile':
        changed['observability_requirements'][0].pop('verification')
    elif damage in {'cycle', 'missing_terminal'}:
        tr, operational = changed['technical_requirements'][0], changed['observability_requirements'][0]
        operational['verification'] = {'mode': 'inherited', 'required_profiles': ['operational'], 'inheritance': [{
            'source': {'requirement_type': 'technical_requirement', 'requirement_id': 'tr'},
            'source_digest': requirement_verification_digest('spec', 'technical_requirement', tr),
            'criterion_ids': ['missing' if damage == 'missing_terminal' else 'ac-tr'], 'covered_aspect': 'Alert condition'}]}
        if damage == 'cycle':
            tr['verification'] = {'mode': 'inherited', 'required_profiles': ['technical'], 'inheritance': [{
                'source': {'requirement_type': 'observability_requirement', 'requirement_id': 'or'},
                'source_digest': requirement_verification_digest('spec', 'observability_requirement', operational),
                'criterion_ids': ['ac-or'], 'covered_aspect': 'Service condition'}]}
    await db.execute(update(Spec).where(Spec.id == 'spec').values(**changed))
    if damage == 'orphan_criterion':
        current = await db.get(Spec, 'spec', populate_existing=True)
        criteria = deepcopy(current.acceptance_criteria)
        criteria[0]['requirement_links'] = []
        await db.execute(update(Spec).where(Spec.id == 'spec').values(acceptance_criteria=criteria))
    if damage == 'missing_operational_work':
        await db.execute(update(Card).where(Card.id == 'test-or').values(test_scenario_ids=[]))
    await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        if damage == 'missing_profile':
            await patch_requirement(client, db, 'observability_requirement', 'or', {'notes': 'Qualification pending in Draft'})
        diagnostic = await client.get('/api/v1/boards/board/specs/spec/requirement-verification')
        assert diagnostic.status_code == 200, diagnostic.text
        assert not diagnostic.json()['verification_work_complete']
        if damage == 'missing_profile':
            row = next(item for item in diagnostic.json()['items'] if item['requirement_id'] == 'or')
            assert row['default_proposal']['verification']['required_profiles'] == ['operational']
        if damage in {'cycle', 'missing_terminal'}:
            expected = 'verification_inheritance_cycle' if damage == 'cycle' else 'verification_inheritance_terminal_missing'
            assert expected in diagnostic.text
        if damage == 'orphan_criterion':
            assert 'criterion_requirement_link_missing' in diagnostic.text
        await classify_all(client, db, 'before-plan-repair')
        # Validation outcome is fixture input; the start admission itself is real.
        await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated'))
        await db.commit()
        denied = await client.post('/api/v1/specs/spec/move', json={'status': 'in_progress'})
        assert denied.status_code in {400, 409} and 'spec_execution_plan_incomplete' in denied.text, denied.text
        assert (await db.get(Spec, 'spec', populate_existing=True)).status == 'validated'
        reopened = await client.post('/api/v1/specs/spec/move', json={'status': 'draft'})
        assert reopened.status_code == 200, reopened.text
        if damage == 'cycle':
            # Repair both ends together through the governed whole-Spec writer:
            # an individually updated end would still reference the stale peer.
            repaired = await client.patch('/api/v1/specs/spec', json={
                'technical_requirements': original['technical_requirements'],
                'observability_requirements': original['observability_requirements']})
            assert repaired.status_code == 200, repaired.text
        elif damage == 'orphan_criterion':
            await patch_requirement(client, db, 'acceptance_criterion', 'ac-procedure',
                {'requirement_links': fields['acceptance_criteria'][0]['requirement_links']})
        else:
            await patch_requirement(client, db, 'observability_requirement', 'or',
                {'verification': original['observability_requirements'][0]['verification']})
        if damage == 'missing_operational_work':
            # Card assignment is a fixture input to this start-gate test.
            await db.execute(update(Card).where(Card.id == 'test-or').values(test_scenario_ids=['ts-or']))
            await db.commit()
        await classify_all(client, db, 'after-plan-repair')
        await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated', evaluations=fields['evaluations']))
        await db.commit()
        started = await client.post('/api/v1/specs/spec/move', json={'status': 'in_progress'})
        assert started.status_code == 200, started.text
    spec = await db.get(Spec, 'spec', populate_existing=True)
    assert spec.status == 'in_progress'
    assert all(item['status'] == 'ready' and not item.get('evidence') for item in spec.test_scenarios)
    for identity in ('task', 'test', 'test-or'):
        assert (await db.get(Card, identity, populate_existing=True)).status == 'not_started'


def semantic_evaluation(corrected, reason):
    # An external reviewer supplies the semantic judgment. Pulse must preserve
    # that authority, not infer sufficiency from links or call an internal LLM.
    return {'breakdown_completeness': 95, 'breakdown_justification': 'All planned Cards are explicitly allocated',
        'granularity': 95, 'granularity_justification': 'Work has a bounded implementation and verification scope',
        'dependency_coherence': 95, 'dependency_justification': 'No unresolved dependency in this controlled example',
        'test_coverage_quality': 95 if corrected else 10, 'test_coverage_justification': reason,
        'overall_score': 95, 'overall_justification': reason,
        'recommendation': 'approve' if corrected else 'reject'}


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['vague_condition', 'unrelated_criterion', 'insufficient_inheritance'])
@pytest.mark.parametrize('corrected', [False, True], ids=['review_rejects', 'condition_corrected'])
async def test_semantic_review_is_distinct_from_structural_readiness(classified_context, tmp_path, case, corrected):
    db = classified_context
    app, _, fields = await four_profiles(db, tmp_path)
    spec = await db.get(Spec, 'spec', populate_existing=True)
    criteria = deepcopy(spec.acceptance_criteria)
    if case == 'vague_condition':
        selected = next(item for item in criteria if item['id'] == 'ac-tr')
        correct_text = selected['text']
        selected['text'] = 'Respond rapidly'
        reason = 'Latency requires an observable limit; rapidly does not define one'
    elif case == 'unrelated_criterion':
        selected = criteria[0]
        correct_text = selected['text']
        selected['text'] = 'Monthly invoice export contains every invoice field'
        reason = 'Invoice export does not verify the required procedure view'
    else:
        selected = criteria[0]
        selected['text'] = 'Successful login returns an authenticated session'
        correct_text = 'After five incorrect passwords, login is blocked'
        reason = 'Successful login does not observe five failures and access blocking'
        frs = deepcopy(spec.functional_requirements)
        frs[0]['text'] = 'Authenticate credentials'
        rules = deepcopy(spec.business_rules)
        rules[0].update(title='Limit failed attempts', rule=correct_text, when='Five incorrect passwords',
            then='Login is blocked', linked_requirements=['Authenticate credentials'], verification={
                'mode': 'inherited', 'required_profiles': ['functional'], 'inheritance': [{
                    'source': {'requirement_type': 'functional_requirement', 'requirement_id': 'fr'},
                    'source_digest': requirement_verification_digest('spec', 'functional_requirement', frs[0]),
                    'criterion_ids': ['ac-procedure'], 'covered_aspect': 'Five failures and blocking'}]})
        await db.execute(update(Spec).where(Spec.id == 'spec').values(functional_requirements=frs, business_rules=rules))
    await db.execute(update(Spec).where(Spec.id == 'spec').values(acceptance_criteria=criteria, evaluations=[]))
    await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        if corrected:
            await patch_requirement(client, db, 'acceptance_criterion', selected['id'], {'text': correct_text})
        diagnostic = await client.get('/api/v1/boards/board/specs/spec/requirement-verification')
        assert diagnostic.status_code == 200, diagnostic.text
        assert diagnostic.json()['verification_work_complete'], diagnostic.text
        assert diagnostic.json()['semantic_review_evaluated'] is False
        await classify_all(client, db, 'semantic-review')
        await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated'))
        await db.commit()
        review = await client.post('/api/v1/specs/spec/evaluations', json=semantic_evaluation(corrected,
            'The corrected criterion observes the required condition' if corrected else reason))
        assert review.status_code == 201, review.text
        evaluation = review.json()['evaluation']
        assert evaluation['evaluator_id'] == 'author'
        assert evaluation['recommendation'] == ('approve' if corrected else 'reject')
        result = await client.post('/api/v1/specs/spec/move', json={'status': 'in_progress'})
        if corrected:
            assert result.status_code == 200, result.text
        else:
            assert result.status_code == 400 and 'reject' in result.text, result.text
    spec = await db.get(Spec, 'spec', populate_existing=True)
    assert spec.status == ('in_progress' if corrected else 'validated')
    assert any(item['id'] == evaluation['id'] for item in spec.evaluations)
    assert all(item['status'] == 'ready' and not item.get('evidence') for item in spec.test_scenarios)
    assert (await db.get(Card, 'test', populate_existing=True)).test_scenario_ids == ['scenario', 'ts-tr', 'ts-ir']


@pytest.mark.asyncio
async def test_existing_functional_and_new_technical_criteria_share_authoring_and_history(classified_context, tmp_path):
    from sqlalchemy import select, func
    from okto_pulse.community.adapters.sqlalchemy_models import SpecHistory
    db = classified_context
    app, _, _ = await four_profiles(db, tmp_path)
    spec = await db.get(Spec, 'spec', populate_existing=True)
    technical = deepcopy(next(item for item in spec.acceptance_criteria if item['id'] == 'ac-tr'))
    existing = deepcopy(spec.acceptance_criteria[0])
    await db.execute(update(Spec).where(Spec.id == 'spec').values(
        acceptance_criteria=[item for item in spec.acceptance_criteria if item['id'] != 'ac-tr']))
    await db.commit()
    before = await db.scalar(select(func.count()).select_from(SpecHistory).where(SpecHistory.spec_id == 'spec'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        spec = await db.get(Spec, 'spec', populate_existing=True)
        created = await client.post('/api/v1/specs/spec/structured-entities/acceptance_criterion',
            json={'expected_spec_version': spec.version, 'payload': technical})
        assert created.status_code == 200, created.text
        observed = await client.get('/api/v1/boards/board/specs/spec/requirement-verification',
            params={'requirement_type': 'technical_requirement', 'requirement_id': 'tr'})
        assert observed.status_code == 200, observed.text
        assert observed.json()['items'][0]['criteria_paths'][0]['criterion_id'] == 'ac-tr'
    spec = await db.get(Spec, 'spec', populate_existing=True)
    assert spec.acceptance_criteria[0] == {**existing, 'status': 'active'}
    authored = next(item for item in spec.acceptance_criteria if item['id'] == 'ac-tr')
    assert authored['verification_profile'] == 'technical'
    assert authored['requirement_links'] == technical['requirement_links']
    assert len(spec.test_scenarios) == 4  # No fictional functional test was created.
    assert await db.scalar(select(func.count()).select_from(SpecHistory).where(SpecHistory.spec_id == 'spec')) > before
