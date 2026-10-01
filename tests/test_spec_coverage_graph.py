from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from okto_pulse.community.adapters.sqlalchemy_models import Card
from okto_pulse.community.adapters.sqlalchemy_spec_coverage import CommunitySpecCoverageReader
from okto_pulse.community.adapters.routed_board_graph_facades import CommunityRoutedSemanticGraphStore
from okto_pulse.community.adapters.grafx_query_execution import CommunityGraphQueryExecution
from okto_pulse.community.adapters.grafx_graph_store import CommunityGrafxGraphStore
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable, GraphError
from okto_pulse.core.ports.spec_coverage_query import SpecCoverageGraphFacts, SpecCoverageGraphScope
from okto_pulse.core.services.spec_coverage_graph import build_spec_coverage_graph_scope
from okto_pulse.core.services.spec_coverage_query import project_spec_coverage
import test_grafx_graph_store as native
import test_spec_coverage_read as relational

real_store = native.real_store
ledger = relational.ledger


def test_native_read_is_scoped_bounded_and_read_only(real_store):
    store, database, _, _ = real_store
    board = native.BOARD_ID
    root = ('Entity', 'spec:coverage')
    child = ('Requirement', 'spec:coverage:fr:one')
    missing = ('Criterion', 'spec:coverage:ac:missing')
    edge = (*child, 'belongs_to', *root, 'belongs_to/requirement@v2.1', 'deterministic', 'worker_layer1')
    scope = SpecCoverageGraphScope((root, child, missing), (edge,))
    store.create_node(board, root[0], 'coverage-root', native._attrs('Root', root[1], 'coverage'))
    store.create_node(board, child[0], 'coverage-child', native._attrs('Child', child[1], 'coverage'))
    store.create_node(board, 'Requirement', 'coverage-foreign', native._attrs('Secret', 'spec:foreign:fr:x', 'coverage'))
    for identity in ('coverage-child', 'coverage-foreign'):
        store.create_edge(board, 'belongs_to', identity, 'coverage-root', {
            'confidence': 1.0, 'rule_id': edge[5], 'layer': edge[6], 'created_by': edge[7],
        }, from_type='Requirement', to_type='Entity')
    before = database.transactions
    result = store.read_spec_coverage_graph(board, scope)
    assert set(result.nodes) == {root, child}
    assert result.relations == (edge,)
    # Removing the last source link must not hide a surviving graph relation.
    without_link = store.read_spec_coverage_graph(board, replace(scope, relations=()))
    assert without_link.relations == (edge,)
    assert database.transactions == before


def test_duplicate_current_identity_fails_instead_of_selecting_an_arbitrary_node(real_store):
    store, _, _, _ = real_store
    key = ('Requirement', 'spec:ambiguous:fr:one')
    for identity in ('ambiguous-one', 'ambiguous-two'):
        store.create_node(native.BOARD_ID, key[0], identity, native._attrs(identity, key[1], 'coverage'))
    with pytest.raises(GraphError) as rejected:
        store.read_spec_coverage_graph(native.BOARD_ID, SpecCoverageGraphScope((key,), ()))
    assert str(rejected.value.__cause__) == 'spec_coverage_ambiguous_projection'


def test_routed_read_attaches_immutable_route_generation():
    generation = ['first']
    class Resolver:
        def acquire_board_route(self, board):
            return SimpleNamespace(scope='board', scope_id=board, backend='grafx', generation=generation[0])
    class Reader:
        def read_spec_coverage_graph(self, *args):
            return SpecCoverageGraphFacts()
    store = CommunityRoutedSemanticGraphStore(Resolver(), grafx=Reader(), operation_window=lambda _: nullcontext())
    assert store.read_spec_coverage_graph('board', SpecCoverageGraphScope((), ())).generation == 'first'
    generation[0] = 'second'
    assert store.read_spec_coverage_graph('board', SpecCoverageGraphScope((), ())).generation == 'second'


@pytest.mark.asyncio
async def test_source_change_before_graph_read_refuses_stale_composition(ledger):
    session, _, _ = ledger
    query = replace(relational.QUERY, read_delivery=False, read_graph=True)
    calls = []
    class Reader:
        def read_spec_coverage_graph(self, *args):
            calls.append(args)
            return SpecCoverageGraphFacts()
    reader = CommunitySpecCoverageReader(session, graph_reader=Reader(), query_execution=CommunityGraphQueryExecution())
    snapshot = await reader.read(query, timeout_ms=15000)
    scope = build_spec_coverage_graph_scope(snapshot)
    await session.execute(update(Card).where(Card.id == 'test').values(status='in_progress'))
    await session.commit()
    with pytest.raises(ValueError, match='source_changed'):
        await reader.read_graph(query, scope, snapshot.source_revision, timeout_ms=15000)
    assert calls == []


@pytest.mark.asyncio
async def test_graph_unavailable_is_not_an_empty_success(ledger):
    session, _, _ = ledger
    query = replace(relational.QUERY, read_delivery=False, read_graph=True)
    class Reader:
        def read_spec_coverage_graph(self, *args):
            raise GraphCapabilityUnavailable('not configured')
    reader = CommunitySpecCoverageReader(session, graph_reader=Reader(), query_execution=CommunityGraphQueryExecution())
    snapshot = await reader.read(query, timeout_ms=15000)
    scope = build_spec_coverage_graph_scope(snapshot)
    observed = await reader.read_graph(query, scope, snapshot.source_revision, timeout_ms=15000)
    result = project_spec_coverage(query, replace(snapshot, graph=observed, graph_scope=scope))
    assert result['projection_freshness']['state'] == 'unavailable'
    assert result['structure']['summary'] is not None
    assert result['structure']['graph']['observed_nodes'] is None


@pytest.mark.asyncio
async def test_real_sql_and_grafx_keep_unprojected_source_in_scope_without_proof_credit(ledger, real_store):
    session, _, _ = ledger
    _, database, fence, _ = real_store
    execution = CommunityGraphQueryExecution()
    board = relational.BOARD
    store = CommunityGrafxGraphStore(lambda key: {board: database}[key], fence,
        query_timeout=execution.remaining)
    query = replace(relational.QUERY, read_graph=True)
    reader = CommunitySpecCoverageReader(session, graph_reader=store, query_execution=execution)
    snapshot = await reader.read(query, timeout_ms=15000)
    scope = build_spec_coverage_graph_scope(snapshot)
    # Deliberately omit one authorized source; an anti-join must retain it.
    projected = scope.nodes[:-1]
    identities = {key: f'composed-coverage-{index}' for index, key in enumerate(projected)}
    for (kind, ref), identity in identities.items():
        store.create_node(board, kind, identity, native._attrs(ref, ref, 'coverage-composed'))
    for edge in scope.relations:
        start, end = tuple(edge[:2]), tuple(edge[3:5])
        if start in identities and end in identities:
            store.create_edge(board, edge[2], identities[start], identities[end],
                {'confidence': 1.0, 'rule_id': edge[5], 'layer': edge[6], 'created_by': edge[7]},
                from_type=edge[0], to_type=edge[3])
    before = database.transactions
    graph = await reader.read_graph(query, scope, snapshot.source_revision, timeout_ms=15000)
    result = project_spec_coverage(query, replace(snapshot, graph=graph, graph_scope=scope))
    assert result['structure']['graph']['expected_nodes'] == len(scope.nodes)
    assert result['structure']['graph']['missing_nodes'] == 1
    assert result['projection_freshness']['state'] == 'incomplete'
    assert result['delivery']['counts']['verification_proven'] == 0
    assert any(row.get('observation') == 'not_found_in_projection' for row in result['items'])
    assert database.transactions == before
