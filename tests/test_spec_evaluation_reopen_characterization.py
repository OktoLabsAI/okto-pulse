"""Characterize evaluation currentness before changing authority or history.

The real reopen/edit/evaluation/start writers are composed. Admission into
validated is a declared fixture input, not a claim about Spec Validation.
"""
from copy import deepcopy

import httpx
import pytest
from sqlalchemy import update

from okto_pulse.community.adapters.sqlalchemy_models import Spec
import test_verification_start_transition as start

adopted_context = start.adopted_context
classified_context = start.classified_context


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_recommendation', ['approve', 'reject'])
async def test_reopen_preserves_unscoped_evaluation_as_active(classified_context, tmp_path, initial_recommendation):
    db = classified_context
    app, _, _ = await start.four_profiles(db, tmp_path)
    await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated', evaluations=[]))
    await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        initial = await client.post('/api/v1/specs/spec/evaluations', json=start.semantic_evaluation(
            initial_recommendation == 'approve', 'Review of the original 200 ms condition'))
        assert initial.status_code == 201, initial.text
        original = deepcopy(initial.json()['evaluation'])
        before = await db.get(Spec, 'spec', populate_existing=True)
        old_edition = before.edition
        reopened = await client.post('/api/v1/specs/spec/move', json={'status': 'draft'})
        assert reopened.status_code == 200, reopened.text
        await start.patch_requirement(client, db, 'acceptance_criterion', 'ac-tr',
            {'text': 'Procedure view responds within 100 ms'})
        await start.classify_all(client, db, 'reclassify-after-reopen')
        current = await db.get(Spec, 'spec', populate_existing=True)
        assert current.edition == old_edition + 1
        assert current.evaluations == [original]
        assert not original['stale']
        assert 'edition' not in original and 'spec_version' not in original
        await db.execute(update(Spec).where(Spec.id == 'spec').values(status='validated'))
        await db.commit()
        if initial_recommendation == 'reject':
            revised = await client.post('/api/v1/specs/spec/evaluations', json=start.semantic_evaluation(
                True, 'Review of the corrected 100 ms condition'))
            assert revised.status_code == 201, revised.text
        result = await client.post('/api/v1/specs/spec/move', json={'status': 'in_progress'})
        if initial_recommendation == 'approve':
            # Characterization: the prior edition's approval releases the
            # materially changed criterion without a new decomposition review.
            assert result.status_code == 200, result.text
        else:
            # Characterization: a new approval cannot supersede the original
            # rejection, even after authorized reopen and content correction.
            assert result.status_code == 400 and 'reject' in result.text, result.text
    current = await db.get(Spec, 'spec', populate_existing=True)
    assert current.evaluations[0] == original
