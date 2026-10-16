from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import event, update

from okto_pulse.community.adapters.grafx_graph_store import CommunityGrafxGraphStore
from okto_pulse.community.adapters.grafx_query_execution import CommunityGraphQueryExecution
from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec
from okto_pulse.community.adapters.sqlalchemy_spec_coverage import CommunitySpecCoverageReader
from okto_pulse.core.application.service_catalog import CoreAnalyticsOperations
from okto_pulse.core.ports.decision_impact import DecisionImpactQuery
from okto_pulse.core.services.decision_impact import build_decision_impact_scope
import okto_pulse.core.ports.relational_application as composition
import test_grafx_graph_store as native
import test_spec_coverage_read as relational

ledger = relational.ledger
real_store = native.real_store


@pytest.mark.asyncio
async def test_kg57_real_sql_and_grafx_explain_potential_scenario_without_reading_proof(ledger, real_store, monkeypatch):
    session, _, _ = ledger
    _, database, fence, _ = real_store
    board, spec_id = relational.BOARD, relational.SPEC
    await session.execute(update(Spec).where(Spec.id == spec_id).values(
        decisions=[{'id':'decision','title':'Choice','status':'active','linked_requirements':['fr']}],
        functional_requirements=[{'id':'fr','text':'Changed requirement','linked_task_ids':['task']}],
        acceptance_criteria=[{'id':'other_ac','text':'Different condition'}],
        test_scenarios=[{'id':'unrelated','title':'Different scenario','status':'ready',
            'linked_task_ids':['task'],'linked_criteria':['other_ac']}]))
    await session.execute(update(Card).where(Card.id == 'task').values(test_scenario_ids=['unrelated']))
    await session.commit()
    execution = CommunityGraphQueryExecution()
    store = CommunityGrafxGraphStore(lambda key: {board:database}[key], fence, query_timeout=execution.remaining)
    proof = SimpleNamespace(load_rollup_snapshot=AsyncMock(side_effect=AssertionError('proof read forbidden')))
    reader = CommunitySpecCoverageReader(session, delivery_store=proof, graph_reader=store, query_execution=execution)
    query = DecisionImpactQuery(board,spec_id,'decision','actor')
    snapshot = await reader.read(query.source_query(),timeout_ms=15000)
    scope = build_decision_impact_scope(snapshot)
    identities = {key:f'impact-{index}' for index,key in enumerate(scope.nodes)}
    for (kind,ref),identity in identities.items():
        store.create_node(board,kind,identity,native._attrs(ref,ref,'impact-seed'))
    for edge in scope.relations:
        store.create_edge(board,edge[2],identities[tuple(edge[:2])],identities[tuple(edge[3:5])],
            {'confidence':1.0,'rule_id':edge[5],'layer':edge[6],'created_by':edge[7]},
            from_type=edge[0],to_type=edge[3])
    monkeypatch.setattr(composition,'require_relational_application_adapter',
        lambda: SimpleNamespace(spec_coverage_read=lambda context:reader))
    statements = []
    def observe(conn,cursor,statement,*args):
        statements.append(statement)
    event.listen(session.bind.sync_engine,'before_cursor_execute',observe)
    before = database.transactions
    try:
        result = await CoreAnalyticsOperations(session).decision_impact(query,timeout_ms=15000)
    finally:
        event.remove(session.bind.sync_engine,'before_cursor_execute',observe)
    from okto_pulse.core.models.decision_impact import DecisionImpactResponse
    DecisionImpactResponse.model_validate(result)
    row = next(item for item in result['items'] if item['target_ref'].endswith(':test_scenario:unrelated'))
    assert row['certainty'] == 'potential'
    assert row['interpretation'] == 'potential_shared_card_reach'
    assert [step['direction'] for step in row['path']] == ['outgoing','incoming','outgoing']
    assert all(step['source_confirmed'] and step['graph_observed'] for step in row['path'])
    assert row['status'] == 'ready'
    assert database.transactions == before
    assert not any(sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','DROP')) for sql in statements)
    assert not any('delivery_evidence_records' in sql for sql in statements)
    proof.load_rollup_snapshot.assert_not_called()
