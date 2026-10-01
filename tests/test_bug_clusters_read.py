"""Bounded SQL inventory, native Grafx association reads and route identity."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from okto_pulse.community.adapters.sqlalchemy_models import Card, Spec, DomainEventRow
from okto_pulse.community.adapters.sqlalchemy_bug_clusters import CommunityBugClustersReader
from okto_pulse.community.adapters.grafx_query_execution import CommunityGraphQueryExecution
from okto_pulse.community.adapters.routed_board_graph_facades import CommunityRoutedSemanticGraphStore
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryTimeout, GraphUnavailable
from okto_pulse.core.ports.bug_clusters import BugClustersQuery, ClusterBugFact, BugClusterGraphFacts, BugClusterAssociation
from okto_pulse.core.services.bug_clusters import project_bug_clusters
import test_grafx_graph_store as native_store

real_store = native_store.real_store
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def query(group_by='severity'):
    return BugClustersQuery.recent(board_id='board', actor_scope_ref='authorized-actor', now=NOW, group_by=group_by)


@pytest_asyncio.fixture
async def source():
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        for table in (Spec.__table__, Card.__table__, DomainEventRow.__table__):
            await connection.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session, engine
    finally:
        await engine.dispose()


async def seed(session, *records):
    defaults = dict(board_id='board', title='Bug', card_type='bug', status='done', created_by='actor',
                    created_at=NOW - timedelta(days=2), updated_at=NOW, severity='major',
                    spec_id=None, origin_task_id=None)
    await session.execute(Card.__table__.insert(), [{**defaults, **record} for record in records])
    await session.commit()


@pytest.mark.asyncio
async def test_inventory_uses_source_window_type_status_severity_and_board(source):
    session, _ = source
    await seed(session, {'id': 'one'}, {'id': 'old', 'created_at': NOW - timedelta(days=16)},
        {'id': 'foreign', 'board_id': 'elsewhere'}, {'id': 'task', 'card_type': 'normal'},
        {'id': 'two', 'severity': 'minor', 'status': 'in_progress'})
    reader = CommunityBugClustersReader(session)
    observed = await reader.read(query(), timeout_ms=15000)
    assert observed.expected_bug_count == 2
    assert {bug.bug_id for bug in observed.bugs} == {'one', 'two'}
    narrow = await reader.read(replace(query(), status='done', severity='major'), timeout_ms=15000)
    assert [bug.bug_id for bug in narrow.bugs] == ['one']
    result = project_bug_clusters(query(), observed)
    assert result['distinct_bug_count'] == 2
    assert result['completeness']['complete_for_scope']
    assert result['projection_freshness']['state'] == 'unknown'


@pytest.mark.asyncio
async def test_resolution_uses_latest_two_events_without_per_bug_query(source):
    session, engine = source
    await seed(session, {'id': 'one'}, {'id': 'two'}, {'id': 'reopened', 'status': 'in_progress'})
    events = [dict(id='a', board_id='board', event_type='card.moved', occurred_at=NOW - timedelta(days=1),
        payload_json={'card_id': 'one', 'from_status': 'in_progress', 'to_status': 'done'}),
        dict(id='b', board_id='board', event_type='card.moved', occurred_at=NOW - timedelta(hours=12),
        payload_json={'card_id': 'reopened', 'from_status': 'in_progress', 'to_status': 'done'})]
    await session.execute(DomainEventRow.__table__.insert(), events)
    await session.commit()
    statements = []
    event.listen(engine.sync_engine, 'before_cursor_execute', lambda conn, cursor, sql, *args: statements.append(sql))
    observed = await CommunityBugClustersReader(session).read(query(), timeout_ms=15000)
    assert {bug.bug_id: bug.resolved_at for bug in observed.bugs} == {
        'one': NOW - timedelta(days=1), 'two': None, 'reopened': None}
    assert len(statements) == 3  # one inventory, one batched history, one recheck


@pytest.mark.asyncio
async def test_global_denominator_not_replaced_by_bounded_inventory(source):
    session, _ = source
    await seed(session, *({'id': f'bug-{i:04d}'} for i in range(1001)))
    observed = await CommunityBugClustersReader(session).read(query(), timeout_ms=15000)
    result = project_bug_clusters(query(), observed)
    assert len(observed.bugs) == 1000
    assert result['distinct_bug_count'] == 1001
    assert result['completeness']['complete_for_scope'] is False
    assert result['items'][0]['distinct_bug_count'] is None


@pytest.mark.asyncio
async def test_spec_grouping_follows_origin_card_not_regression_spec(source):
    session, _ = source
    await session.execute(Spec.__table__.insert(), [dict(id=key, board_id='board', title=key, created_by='actor')
        for key in ('origin-spec', 'regression-spec')])
    await seed(session, {'id': 'origin', 'card_type': 'normal', 'spec_id': 'origin-spec'},
        {'id': 'one', 'spec_id': 'regression-spec', 'origin_task_id': 'origin'})
    observed = await CommunityBugClustersReader(session).read(query('spec'), timeout_ms=15000)
    result = project_bug_clusters(query('spec'), observed)
    assert result['distinct_bug_count'] == 1
    assert result['items'][0]['target_ref'] == 'spec:origin-spec'
    assert result['items'][0]['assertion_basis'] == 'origin_spec'
    # Fixture mutation deliberately leaves the Bug unchanged: Q06/cursor inputs
    # must observe origin changes even without a Bug or Spec version increment.
    await session.execute(Card.__table__.update().where(Card.id == 'origin').values(spec_id='regression-spec'))
    await session.commit()
    changed = await CommunityBugClustersReader(session).read(query('spec'), timeout_ms=15000)
    assert changed.bugs[0].source_revision != observed.bugs[0].source_revision
    assert changed.bugs[0].spec_ref == 'spec:regression-spec'


@pytest.mark.asyncio
async def test_origin_in_another_board_does_not_expand_query_scope(source):
    session, _ = source
    await seed(session, {'id': 'origin', 'card_type': 'normal', 'board_id': 'foreign'},
        {'id': 'one', 'origin_task_id': 'origin'})
    with pytest.raises(ValueError, match='source_link_outside_scope'):
        await CommunityBugClustersReader(session).read(query('spec'), timeout_ms=15000)


@pytest.mark.asyncio
async def test_unavailable_graph_keeps_authoritative_inventory_but_timeout_is_not_empty(source):
    session, _ = source
    await seed(session, {'id': 'one'})
    class Reader:
        failure = GraphUnavailable('offline')
        def read_bug_cluster_graph(self, *args, **kwargs):
            raise self.failure
    graph = Reader()
    reader = CommunityBugClustersReader(session, graph_reader=graph, query_execution=CommunityGraphQueryExecution())
    observed = await reader.read(query('proxy'), timeout_ms=15000)
    assert observed.expected_bug_count == 1 and not observed.graph_available
    graph.failure = GraphQueryTimeout('expired')
    with pytest.raises(GraphQueryTimeout):
        await reader.read(query('proxy'), timeout_ms=15000)


@pytest.mark.asyncio
async def test_invalid_parent_is_not_exposed_as_an_authorized_spec(source):
    session, _ = source
    await seed(session, {'id': 'one', 'spec_id': 'unavailable-spec'})
    with pytest.raises(ValueError, match='source_link_outside_scope'):
        await CommunityBugClustersReader(session).read(query('spec'), timeout_ms=15000)


@pytest.mark.asyncio
async def test_shared_graph_endpoint_does_not_grant_access_to_a_foreign_spec(source):
    session, _ = source
    await session.execute(Spec.__table__.insert(), dict(id='foreign', board_id='other-board',
        title='Confidential title', created_by='another-owner'))
    await seed(session, {'id': 'one'})
    class Reader:
        def read_bug_cluster_graph(self, *args, **kwargs):
            return BugClusterGraphFacts(('one',), (BugClusterAssociation('one', 'proxy',
                'spec:foreign:tr:hidden', 'Confidential title', 'closed-rule', 'unknown'),), 'generation')
    reader = CommunityBugClustersReader(session, graph_reader=Reader(), query_execution=CommunityGraphQueryExecution())
    with pytest.raises(ValueError, match='target_outside_scope'):
        await reader.read(query('proxy'), timeout_ms=15000)


def test_native_clusters_preserve_two_bugs_and_exclude_stale_or_unclassified_facts(real_store):
    store, database, _, _ = real_store
    board = native_store.BOARD_ID
    store.create_node(board, 'Constraint', 'cluster-target', native_store._attrs('Association', 'spec:one:tr:one', 'seed'))
    facts = []
    for identity in ('cluster-one', 'cluster-two', 'cluster-stale'):
        fact = ClusterBugFact(identity, identity, NOW - timedelta(days=2), 'done', 'major', None, 'revision')
        attrs = {**native_store._attrs(identity, 'card:' + identity, 'seed'),
                 'source_created_at': fact.source_created_at.isoformat(), 'source_status': 'done', 'severity': 'major'}
        if identity == 'cluster-stale':
            attrs['source_status'] = 'in_progress'
        store.create_node(board, 'Bug', identity, attrs)
        store.create_edge(board, 'violates', identity, 'cluster-target', {
            'confidence': 0.8, 'rule_id': 'violates/bug_origin_proxy_tr/origin@v2.1',
            'layer': 'deterministic', 'created_by': 'worker_layer1', 'fallback_reason': 'inferred_origin_proxy:card:origin',
        }, from_type='Bug', to_type='Constraint')
        facts.append(fact)
    store.create_edge(board, 'violates', 'cluster-one', 'cluster-target', {
        'confidence': 0.8, 'rule_id': 'human-stated-violation', 'layer': 'cognitive', 'created_by': 'reviewer',
    }, from_type='Bug', to_type='Constraint')
    store.create_node(board, 'Learning', 'cluster-learning', native_store._attrs('Recorded interpretation', 'card:cluster-one', 'seed'))
    store.create_edge(board, 'validates', 'cluster-learning', 'cluster-one', {
        'confidence': 0.9, 'rule_id': 'recorded-learning', 'layer': 'cognitive', 'created_by': 'reviewer',
    }, from_type='Learning', to_type='Bug')
    before = database.transactions
    observed = store.read_bug_cluster_graph(board, tuple(facts), group_by='proxy')
    assert observed.projected_bug_ids == ('cluster-one', 'cluster-two')
    assert {item.bug_id for item in observed.associations} == {'cluster-one', 'cluster-two'}
    assert all(item.target_ref == 'spec:one:tr:one' and item.validity == 'unknown' for item in observed.associations)
    assert len(observed.associations) == 2
    learning = store.read_bug_cluster_graph(board, tuple(facts), group_by='learning')
    assert len(learning.associations) == 1
    assert learning.associations[0].target_ref == 'learning:cluster-learning'
    assert learning.associations[0].validity == 'unknown'
    assert 'recorded-learning' in learning.associations[0].provenance_ref
    assert database.transactions == before


def test_routed_cluster_read_uses_current_route_generation():
    generation = ['first']
    class Resolver:
        def acquire_board_route(self, board):
            return SimpleNamespace(scope='board', scope_id=board, backend='grafx', generation=generation[0])
    class Reader:
        def read_bug_cluster_graph(self, *args, **kwargs):
            return BugClusterGraphFacts((), ())
    store = CommunityRoutedSemanticGraphStore(Resolver(), grafx=Reader(), operation_window=lambda _: nullcontext())
    assert store.read_bug_cluster_graph('board', (), group_by='proxy').graph_generation == 'first'
    generation[0] = 'second'
    assert store.read_bug_cluster_graph('board', (), group_by='proxy').graph_generation == 'second'


@pytest.mark.asyncio
async def test_sql_and_native_graph_keep_missing_bug_in_denominator(source, real_store, monkeypatch):
    session, _ = source
    store, database, _, _ = real_store
    board = native_store.BOARD_ID
    await session.execute(Spec.__table__.insert(), dict(id='integrated-spec', board_id=board,
        title='Source Spec', created_by='actor'))
    await seed(session, {'id': 'mixed-origin', 'board_id': board, 'card_type': 'normal', 'spec_id': 'integrated-spec'},
        {'id': 'mixed-one', 'board_id': board, 'origin_task_id': 'mixed-origin'},
        {'id': 'mixed-missing', 'board_id': board, 'origin_task_id': 'mixed-origin'})
    store.create_node(board, 'Constraint', 'mixed-constraint',
        native_store._attrs('Associated target', 'spec:integrated-spec:tr:item', 'seed'))
    store.create_node(board, 'Bug', 'mixed-one-node', {
        **native_store._attrs('One projected Bug', 'card:mixed-one', 'seed'),
        'source_created_at': (NOW - timedelta(days=2)).isoformat(), 'source_status': 'done', 'severity': 'major',
    })
    store.create_edge(board, 'violates', 'mixed-one-node', 'mixed-constraint', {
        'confidence': 0.8, 'rule_id': 'violates/bug_origin_proxy_tr/mixed-origin@v2.1',
        'layer': 'deterministic', 'created_by': 'worker_layer1', 'fallback_reason': 'inferred_origin_proxy:card:mixed-origin',
    }, from_type='Bug', to_type='Constraint')
    execution = CommunityGraphQueryExecution()
    monkeypatch.setattr(store, '_query_timeout', execution.remaining)
    before = database.transactions
    request = replace(query('proxy'), board_id=board)
    observed = await CommunityBugClustersReader(session, graph_reader=store, query_execution=execution).read(
        request, timeout_ms=15000)
    result = project_bug_clusters(request, observed)
    assert result['distinct_bug_count'] == 2
    assert result['projection_freshness']['state'] == 'incomplete'
    assert result['completeness']['complete_for_scope'] is False
    assert result['items'][0]['bug_refs'] == ['card:mixed-one']
    assert result['items'][0]['distinct_bug_count'] is None
    assert result['items'][0]['observed_bug_count'] == 1
    assert database.transactions == before


@pytest.mark.asyncio
async def test_kg59_distinct_causes_share_only_an_origin_association(source, real_store, monkeypatch):
    """KG-59: different recorded diagnoses must not become a common-cause claim."""
    import json
    from sqlalchemy import select
    from okto_pulse.core.models.bug_clusters import BugClustersResponse

    session, _ = source
    store, database, _, _ = real_store
    board = native_store.BOARD_ID
    await session.execute(Spec.__table__.insert(), dict(id='kg59-spec', board_id=board,
        title='Same affected contract', created_by='actor'))
    await seed(session, {'id': 'kg59-origin', 'board_id': board, 'card_type': 'normal', 'spec_id': 'kg59-spec'})
    diagnoses = {'kg59-one': 'Root cause: expired upstream certificate.',
                 'kg59-two': 'Root cause: integer overflow in a local counter.'}
    await seed(session, *(dict(id=key, board_id=board, origin_task_id='kg59-origin',
        conclusions=[dict(text=text, author_id='reviewer', created_at=NOW.isoformat())])
        for key, text in diagnoses.items()))
    stored = (await session.execute(select(Card.id, Card.conclusions).where(Card.id.in_(diagnoses)))).all()
    assert {key: entries[0]['text'] for key, entries in stored} == diagnoses
    store.create_node(board, 'Constraint', 'kg59-constraint',
        native_store._attrs('Origin-associated contract', 'spec:kg59-spec:tr:item', 'seed'))
    for key in diagnoses:
        store.create_node(board, 'Bug', key + '-node', {
            **native_store._attrs(key, 'card:' + key, 'seed'),
            'source_created_at': (NOW - timedelta(days=2)).isoformat(), 'source_status': 'done', 'severity': 'major',
        })
        store.create_edge(board, 'violates', key + '-node', 'kg59-constraint', {
            'confidence': 0.8, 'rule_id': 'violates/bug_origin_proxy_tr/kg59-origin@v2.1',
            'layer': 'deterministic', 'created_by': 'worker_layer1', 'fallback_reason': 'inferred_origin_proxy:card:kg59-origin',
        }, from_type='Bug', to_type='Constraint')
    execution = CommunityGraphQueryExecution()
    monkeypatch.setattr(store, '_query_timeout', execution.remaining)
    before = database.transactions
    request = replace(query('proxy'), board_id=board)
    snapshot = await CommunityBugClustersReader(session, graph_reader=store, query_execution=execution).read(request, timeout_ms=15000)
    result = BugClustersResponse.model_validate(project_bug_clusters(request, snapshot)).model_dump(mode='json')
    assert len(result['items']) == 1
    cluster = result['items'][0]
    assert result['distinct_bug_count'] == cluster['observed_bug_count'] == 2
    assert cluster['bug_refs'] == ['card:kg59-one', 'card:kg59-two']
    assert cluster['assertion_basis'] == 'origin_proxy'
    assert cluster['causal_conclusion'] == 'not_established'
    assert cluster['provenance_refs'] == ['violates/bug_origin_proxy_tr/kg59-origin@v2.1']
    assert result['completeness']['complete_for_scope'] is False
    assert all(text not in json.dumps(result) for text in diagnoses.values())
    assert database.transactions == before
