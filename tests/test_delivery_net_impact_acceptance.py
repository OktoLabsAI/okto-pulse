"""DEI-T48: persisted rename chains keep repository identity and raw history."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from okto_pulse.community.adapters.sqlalchemy_models import (
    CardDeliveryEvidenceRecordRow as Record,
    ImplementationTargetRow as Target,
)
from okto_pulse.core.models.delivery_evidence import CardDeliveryEvidenceCommand
from test_delivery_net_impact import composed as _composed, db as _db, delta, A, B, C

composed = _composed
db = _db


def renamed(source, result, *, repo='core'):
    return {'repo': repo, 'path': result, 'previous_path': source, 'change_kind': 'renamed'}


@pytest.mark.asyncio
async def test_persisted_rename_chain_does_not_merge_identical_paths_in_two_repos(composed):
    session, uow, use_case, actor = composed
    source = (await session.get(Target, 'target')).source_ref
    payloads = []
    identities = []
    for key, base, result, changes in [
        ('rename-one', A, B, [renamed('a.py', 'b.py'),
            {'repo': 'community', 'path': 'a.py', 'change_kind': 'modified'}]),
        ('rename-two', B, C, [renamed('b.py', 'c.py'),
            {'repo': 'community', 'path': 'a.py', 'change_kind': 'modified'}]),
    ]:
        payload = delta(key, source, base, result, 'modified').model_dump(mode='json')
        payload['progress']['impact_delta']['files'] = changes
        command = CardDeliveryEvidenceCommand.model_validate(payload)
        payloads.append(deepcopy(command.model_dump(mode='json')['progress']['impact_delta']['files']))
        stored = await use_case.execute(command, actor=actor, uow=uow)
        identities.append(stored['id'])
    await session.close()
    projection = await uow.services.delivery_evidence.projection('b', 's')
    accumulated = projection['per_card'][0]['accumulated_impact']
    assert accumulated['status'] == 'composed'
    assert accumulated['history_count'] == 2 and accumulated['claim_only']
    assert len(accumulated['sources']) == 1
    material = accumulated['sources'][0]
    assert set(material['record_ids']) == set(identities)
    assert sorted(material['impact_evidence']['files'], key=lambda value: value['repo']) == [
        {'repo': 'community', 'path': 'a.py', 'change_kind': 'modified'},
        renamed('a.py', 'c.py'),
    ]
    records = {row.id: row for row in (await session.scalars(select(Record).where(Record.id.in_(identities)))).all()}
    for identity, changes in zip(identities, payloads):
        assert records[identity].payload['progress']['impact_delta']['files'] == changes
    assert not projection['allowed'] and not projection['implementations']
