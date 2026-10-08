"""KG28 over native DDL and the actual edition's persistence adapter."""
from copy import deepcopy

import pytest

import test_architecture_candidates_integration as fixtures
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.community.adapters.sqlalchemy_application_persistence import CommunitySqlAlchemyApplicationPersistence
from okto_pulse.core.domain.missing_link_gate import SPEC_LINK_FIELDS
from okto_pulse.core.domain.realm import RealmScope
from okto_pulse.core.ports.application_persistence import register_application_persistence_port
from okto_pulse.core.services.missing_link_gate import evaluate_missing_links, require_missing_links_closed
from okto_pulse.core.services.gate_contracts import GateContractError

adopted_context = fixtures.adopted_context


@pytest.mark.asyncio
@pytest.mark.parametrize('adopted_context', ['native_schema'], indirect=True)
async def test_current_reference_gate_over_native_source_does_not_wait_on_graph(adopted_context):
    db = adopted_context
    db.info['realm_scope'] = RealmScope.local()
    register_application_persistence_port(CommunitySqlAlchemyApplicationPersistence())
    spec = await db.get(Spec, 'spec')
    for field in {name for origin, _, targets in SPEC_LINK_FIELDS for name in (origin, *targets)}:
        setattr(spec, field, [])
    spec.decisions = [{'id': 'choice', 'linked_requirements': ['not-present']}]
    card = Card(id='card-reference', board_id='board', spec_id='spec', title='Reference',
                card_type='test', status='in_progress', created_by='author', test_scenario_ids=['scenario'])
    db.add(card)
    await db.commit()
    before = deepcopy(spec.decisions)
    for subject, kind in ((spec, 'spec'), (card, 'card')):
        blocking = await evaluate_missing_links(db, subject=subject, entity_type=kind,
                                                settings={'missing_link_gate': 'blocking'})
        assert blocking.status == 'available' and blocking.blocked and len(blocking.findings) == 1
        with pytest.raises(GateContractError) as caught:
            require_missing_links_closed(blocking, entity_type=kind, subject=subject)
        assert caught.value.code == 'missing_links_open'
        advisory = await evaluate_missing_links(db, subject=subject, entity_type=kind, settings={})
        assert advisory.findings == blocking.findings and not advisory.blocked
    assert spec.decisions == before and not db.dirty and not db.new
    spec.functional_requirements = [{'id': 'not-present', 'text': 'Current requirement'}]
    spec.test_scenarios = [{'id': 'scenario', 'title': 'Current scenario'}]
    await db.commit()
    for subject, kind in ((spec, 'spec'), (card, 'card')):
        resolved = await evaluate_missing_links(db, subject=subject, entity_type=kind,
                                                settings={'missing_link_gate': 'blocking'})
        assert resolved.status == 'available' and not resolved.findings and not resolved.blocked
