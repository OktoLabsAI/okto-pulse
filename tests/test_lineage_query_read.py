from dataclasses import asdict, replace
import json
import pytest
from sqlalchemy import event
from okto_pulse.community.adapters.sqlalchemy_models import (
    AmendmentHotfixRevision, Board, CardDependency, Ideation, Refinement,
)
from okto_pulse.community.adapters.sqlalchemy_lineage_query import read_lineage_snapshot
from okto_pulse.core.domain.enums import CardType, SpecStatus
from okto_pulse.core.ports.lineage_query import LineageQuery
from okto_pulse.core.services.lineage_query import project_lineage
from test_lineage_dependency_graph import _database, _spec, _spec_dependency, _card, BOARD_ID, OTHER_BOARD_ID


@pytest.mark.asyncio
async def test_real_dependency_chain_beyond_three_hops_and_declared_ancestry(tmp_path, monkeypatch):
    from okto_pulse.community.adapters import sqlalchemy_traceability_read_model as adapter
    from okto_pulse.core.application.service_catalog import CoreAnalyticsOperations
    from okto_pulse.core.ports import relational_services
    monkeypatch.setattr(relational_services, 'resolve_traceability_adapter', lambda: adapter)
    engine, factory = await _database(tmp_path / 'lineage-query.db')
    try:
        async with factory() as session, session.begin():
            session.add(Board(realm_id="local", id=BOARD_ID,name='Board',owner_id='owner'))
            session.add(Ideation(id='idea',board_id=BOARD_ID,title='Original idea',created_by='owner'))
            session.add(Refinement(id='refine',board_id=BOARD_ID,ideation_id='idea',title='Refined scope',created_by='owner'))
            specs = [_spec(str(index)) for index in range(6)]
            specs[0].refinement_id = 'refine'
            session.add_all(specs)
            await session.flush()
            session.add_all(_spec_dependency(f'dep{index}',prerequisite_id=str(index),dependent_id=str(index+1)) for index in range(5))
            session.add_all([_card('child',CardType.NORMAL,spec_id='0'), _card('downstream',CardType.NORMAL)])
            await session.flush()
            session.add(CardDependency(id='card-dep',card_id='downstream',depends_on_id='child'))
        statements = []
        def record(conn,cursor,statement,*args):
            statements.append(statement)
        event.listen(engine.sync_engine,'before_cursor_execute',record)
        async with factory() as session:
            query = LineageQuery(BOARD_ID,'spec:0','actor')
            snapshot = await read_lineage_snapshot(session,query,timeout_ms=15000)
            first = project_lineage(query,snapshot)
            assert first['completeness']['truncated']
            assert 'spec:4' in first['frontier_refs']
            assert next(row for row in first['items'] if row['subject_ref'] == 'ideation:idea')['depth'] == 2
            expanded = await CoreAnalyticsOperations(session).lineage(replace(query,max_depth=8),timeout_ms=15000)
            from okto_pulse.core.models.lineage_query import LineageResponse
            LineageResponse.model_validate(expanded)
            end = next(row for row in expanded['items'] if row['subject_ref'] == 'spec:5')
            assert len(end['path']) == 5
            assert all(edge['relation'] == 'precedes' and edge['direction'] == 'outgoing' for edge in end['path'])
            assert expanded['completeness']['complete_for_scope']
            downstream = next(row for row in expanded['items'] if row['subject_ref'] == 'card:downstream')
            assert [edge['relation'] for edge in downstream['path']] == ['belongs_to','precedes']
            assert expanded['projection_freshness']['state'] == 'unknown'
        event.remove(engine.sync_engine,'before_cursor_execute',record)
        assert not any(sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','DROP')) for sql in statements)
        assert not any('code_evidence' in sql or 'delivery_evidence_records' in sql for sql in statements)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_partial_amendment_preserves_original_and_redacts_foreign_scope(tmp_path):
    engine, factory = await _database(tmp_path / 'amendment-lineage.db')
    try:
        async with factory() as session, session.begin():
            original = _spec('original')
            original.status = SpecStatus.DONE
            session.add_all([Board(realm_id="local", id=BOARD_ID,name='Board',owner_id='owner'),
                Board(realm_id="local", id=OTHER_BOARD_ID,name='Other',owner_id='owner'), original, _spec('revision')])
            await session.flush()
            session.add_all([_card('bug',CardType.BUG,spec_id='original'),
                _card('affected',CardType.NORMAL,spec_id='original'),
                _card('regression',CardType.TEST,spec_id='original'),
                _card('hidden',CardType.NORMAL,board_id=OTHER_BOARD_ID)])
            session.add(AmendmentHotfixRevision(id='partial',board_id=BOARD_ID,original_spec_id='original',
                revision_spec_id='revision',origin_bug_id='bug',origin_task_ids=['affected'],
                affected_task_ids=['affected','hidden'],regression_test_task_ids=['regression'],created_by='owner'))
        async with factory() as session:
            query = LineageQuery(BOARD_ID,'spec:original','actor',max_depth=8)
            snapshot = await read_lineage_snapshot(session,query,timeout_ms=15000)
            result = project_lineage(query,snapshot)
            from okto_pulse.core.models.lineage_query import LineageResponse
            LineageResponse.model_validate(result)
            assert {'spec:revision','amendment_hotfix_revision:partial','card:affected'} <= {row['subject_ref'] for row in result['items']}
            assert 'card:hidden' not in json.dumps(asdict(snapshot),default=str)
            assert 'supersedes' not in json.dumps(result)
            assert 'source_endpoint_unavailable' in result['completeness']['limitations']
            edge = next(edge for edge in snapshot.relations if edge.relation == 'amendment_of')
            assert edge.source_ref == 'amendment_hotfix_revision:partial' and edge.target_ref == 'spec:original'
            assert any(edge.relation == 'regression_test' and edge.target_ref == 'card:regression' for edge in snapshot.relations)
            assert (await session.get(type(original),'original')).status.value == 'done'
    finally:
        await engine.dispose()
