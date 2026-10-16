"""KG-27/29: native source diagnostics retain identities and close on unlink."""

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_audit_repo import CommunityAuditRepository
from okto_pulse.community.adapters.sqlalchemy_models import (
    Card, ConsolidationAudit, ConsolidationQueue, QAItem, SpecQAItem,
)
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.ports.projection_findings import ProjectionFindingSnapshot
from test_projection_materialized_parity import materialize


@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_two_invalid_targets_replay_and_intentional_unlink_are_native(tmp_path):
    async def exercise(factory, graph):
        repository = CommunityAuditRepository(factory)
        processor = ConsolidationProcessor(relational_scope_factory=factory)

        async def latest():
            return await repository.get_latest_reference_findings(
                board_id='board', artifact_id='card', artifact_type='card', namespace='card_scenarios')

        async def counts():
            async with factory() as session:
                return tuple([await session.scalar(select(func.count()).select_from(model))
                              for model in (Card, QAItem, SpecQAItem)])

        async def project(number, targets):
            async with factory() as session:
                card = await session.get(Card, 'card')
                card.test_scenario_ids = targets
                session.add(ConsolidationQueue(id=f'finding-{number}', board_id='board',
                    artifact_type='card', artifact_id='card', source='state_transition'))
                await session.commit()
            await processor.process_batch()
            async with factory() as session:
                assert await session.get(ConsolidationQueue, f'finding-{number}') is None
            return await latest()

        original_counts = await counts()
        async with factory() as session:
            baseline_sessions = set((await session.execute(
                select(ConsolidationAudit.session_id))).scalars())
        first = await project(1, ['missing-one', 'missing-two'])
        assert first is not None
        assert len(first.findings) == 2
        identities = {finding.target_ref: finding.finding_id for finding in first.findings}
        assert len(set(identities.values())) == 2
        assert len({finding.source_selector for finding in first.findings}) == 1
        for number in (2, 3):
            assert await project(number, ['missing-one', 'missing-two']) == first
        # Each new queue event may retain an audit receipt. Finding identity,
        # rather than audit-row count, measures whether reevaluation adds debt.
        async with factory() as session:
            rows = (await session.execute(select(ConsolidationAudit).where(
                ConsolidationAudit.artifact_id == 'card'))).scalars().all()
            observed = set()
            for row in rows:
                if row.session_id not in baseline_sessions and row.reference_findings is not None:
                    observed.update(finding.finding_id for finding in
                        ProjectionFindingSnapshot.from_payload(row.reference_findings).findings)
            assert observed == set(identities.values())
        remaining = await project(4, ['missing-two'])
        assert len(remaining.findings) == 1
        assert remaining.findings[0].finding_id == identities[remaining.findings[0].target_ref]
        closed = await project(5, [])
        assert closed is not None and closed.findings == ()
        assert await project(6, []) == closed
        assert await counts() == original_counts

    await materialize(tmp_path / 'references', incremental=False, card_type='normal',
                      native_schema=True, exercise=exercise)
