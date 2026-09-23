"""Regression for the authorized edition boundary (original repro in Git).

The real reopen/edit/evaluation/start writers are composed. Admission into
validated is a declared fixture input, not a claim about Spec Validation.
"""
from copy import deepcopy
import asyncio

import httpx
import pytest
from sqlalchemy import update, text

from okto_pulse.community.adapters.sqlalchemy_models import Spec
import test_verification_start_transition as start

adopted_context = start.adopted_context
classified_context = start.classified_context


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_recommendation', ['approve', 'reject'])
@pytest.mark.parametrize('legacy', [False, True])
async def test_reopen_requires_new_review_and_preserves_original_verdict(classified_context, tmp_path, initial_recommendation, legacy):
    db = classified_context
    app, _, _ = await start.four_profiles(db, tmp_path)
    await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated', evaluations=[]))
    await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        initial = await client.post('/api/v1/specs/spec/evaluations', json=start.semantic_evaluation(
            initial_recommendation == 'approve', 'Review of the original 200 ms condition'))
        assert initial.status_code == 201, initial.text
        original = deepcopy(initial.json()['evaluation'])
        assert original['spec_edition'] == (await db.get(Spec, 'spec', populate_existing=True)).edition
        if legacy:
            original.pop('spec_edition')
            original.pop('spec_version')
            await db.execute(update(Spec).where(Spec.id == 'spec').values(evaluations=[original]))
            await db.commit()
        before = await db.get(Spec, 'spec', populate_existing=True)
        old_edition = before.edition
        reopened = await client.post('/api/v1/specs/spec/move', json={'status': 'draft'})
        assert reopened.status_code == 200, reopened.text
        await start.patch_requirement(client, db, 'acceptance_criterion', 'ac-tr',
            {'text': 'Procedure view responds within 100 ms'})
        await start.classify_all(client, db, 'reclassify-after-reopen')
        current = await db.get(Spec, 'spec', populate_existing=True)
        assert current.edition == old_edition + 1
        historical = {**original, 'stale': True, 'stale_reason': 'spec_reopened', 'stale_in_edition': current.edition}
        assert current.evaluations == [historical]
        listed = await client.get('/api/v1/specs/spec/evaluations')
        assert listed.status_code == 200, listed.text
        assert listed.json()['active_count'] == 0 and listed.json()['previous_count'] == 1
        assert listed.json()['evaluations'][0]['lifecycle_state'] == 'previous'
        assert listed.json()['evaluations'][0]['edition_origin'] == ('legacy_unknown' if legacy else 'recorded')
        await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated'))
        await db.commit()
        missing = await client.post('/api/v1/specs/spec/move', json={'status': 'in_progress'})
        assert missing.status_code == 400 and "no evaluation with 'approve'" in missing.text, missing.text
        revised = await client.post('/api/v1/specs/spec/evaluations', json=start.semantic_evaluation(
            True, 'Review of the corrected 100 ms condition'))
        assert revised.status_code == 201, revised.text
        assert revised.json()['evaluation']['spec_edition'] == old_edition + 1
        result = await client.post('/api/v1/specs/spec/move', json={'status': 'in_progress'})
        assert result.status_code == 200, result.text
    current = await db.get(Spec, 'spec', populate_existing=True)
    assert current.evaluations[0] == historical


@pytest.mark.asyncio
async def test_new_approval_does_not_supersede_same_edition_rejection(classified_context, tmp_path):
    db = classified_context
    app, _, _ = await start.four_profiles(db, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        await start.classify_all(client, db, 'same-edition')
        await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated', evaluations=[]))
        await db.commit()
        for approve in (False, True):
            response = await client.post('/api/v1/specs/spec/evaluations', json=start.semantic_evaluation(approve, 'Independent verdict'))
            assert response.status_code == 201, response.text
        listed = await client.get('/api/v1/specs/spec/evaluations')
        assert listed.json()['active_count'] == 2
        result = await client.post('/api/v1/specs/spec/move', json={'status': 'in_progress'})
        assert result.status_code == 400 and 'reject' in result.text, result.text


@pytest.mark.asyncio
async def test_failed_reopen_rolls_back_edition_and_evaluation_currentness(classified_context, tmp_path):
    db = classified_context
    app, _, _ = await start.four_profiles(db, tmp_path)
    await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated'))
    await db.commit()
    spec = await db.get(Spec, 'spec', populate_existing=True)
    before = (spec.edition, deepcopy(spec.evaluations))
    await db.execute(text("""CREATE TRIGGER reject_reopen_history BEFORE INSERT ON spec_history
        WHEN NEW.action='status_changed' BEGIN SELECT RAISE(ABORT,'injected_reopen_failure'); END"""))
    await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test') as client:
        response = await client.post('/api/v1/specs/spec/move', json={'status': 'draft'})
        assert response.status_code == 500, response.text
    current = await db.get(Spec, 'spec', populate_existing=True)
    assert current.status == 'validated'
    assert (current.edition, current.evaluations) == before


@pytest.mark.asyncio
async def test_concurrent_reviews_do_not_overwrite_an_accepted_verdict(classified_context, tmp_path):
    db = classified_context
    app, _, _ = await start.four_profiles(db, tmp_path)
    await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated', evaluations=[]))
    await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        responses = await asyncio.gather(*(
            client.post('/api/v1/specs/spec/evaluations', json=start.semantic_evaluation(approve, 'Concurrent independent judgment'))
            for approve in (True, False)
        ))
        assert all(response.status_code in {201, 409} for response in responses), [response.text for response in responses]
        accepted = [response.json()['evaluation'] for response in responses if response.status_code == 201]
        assert accepted
        listed = await client.get('/api/v1/specs/spec/evaluations')
        assert listed.status_code == 200
        assert {item['id'] for item in listed.json()['evaluations']} == {item['id'] for item in accepted}
        assert listed.json()['active_count'] == len(accepted)
