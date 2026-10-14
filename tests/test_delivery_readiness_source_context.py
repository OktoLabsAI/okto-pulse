"""Spec source provenance is the native transition prerequisite, not proof credit."""
import pytest
from sqlalchemy import null, update

import test_delivery_evidence_integration as delivery
from test_delivery_readiness_authorization import setup, read, ACTOR
from test_delivery_readiness_batch import statements
from okto_pulse.community.adapters.sqlalchemy_models import Spec
from okto_pulse.core.domain.enums import SpecStatus
from okto_pulse.core.domain.code_traceability import CodeDeliveryContextRequired
from okto_pulse.core.services.main import SpecService
from okto_pulse.core.services.delivery_readiness import EvaluatedDeliveryReadiness

ledger = delivery.ledger
pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('ledger', ['native_schema'], indirect=True)]


@pytest.mark.parametrize('invalid', ['missing', 'digest', 'lineage'])
async def test_source_pin_rejection_matches_native_transition_without_writes_or_score_changes(ledger, invalid):
    session, store, _ = ledger
    await setup(session)
    implementation = await delivery.record(store, delivery.command())
    await delivery.record(store, delivery.command('test', implementation_ids=[implementation['id']]))
    await session.commit()
    spec = await session.get(Spec, delivery.SPEC_ID)
    SpecService(session).validate_transition_source_context(spec, SpecStatus.DONE)
    changes = ({'delivery_context': None, 'delivery_context_provenance': null(),
        'source_context_manifest': null(), 'source_context_sha256': None} if invalid == 'missing' else
        {'source_context_sha256': '0' * 64} if invalid == 'digest' else
        {'delivery_context_provenance': {**spec.delivery_context_provenance, 'source_spec_id': 'foreign-spec'}})
    await session.execute(update(Spec).where(Spec.id == delivery.SPEC_ID).values(**changes))
    await session.commit()
    spec = await session.get(Spec, delivery.SPEC_ID, populate_existing=True)
    with statements(session.bind) as sql:
        with pytest.raises(CodeDeliveryContextRequired) as native:
            SpecService(session).validate_transition_source_context(spec, SpecStatus.DONE)
        batch = await read(session)
    assert all(s.lstrip().upper().startswith(('SELECT', 'PRAGMA DATA_VERSION', 'BEGIN')) for s in sql)
    assert batch.native_lifecycle.blocking_codes == (native.value.code,)
    context = EvaluatedDeliveryReadiness(batch, actor_id=ACTOR.actor_id).context()
    assert context.implementation.percent == context.verification.percent == 100
    assert context.lifecycle.check_status == 'blocked'
    assert spec.status.value == 'in_progress'
    assert not session.new and not session.dirty and not session.deleted
