"""Origin proxy provenance must survive the public graph read surface."""
from okto_pulse.community.api import kg_routes
from test_kg_routes_grafx_relationship_layout import _RelationshipAwareExecutor
import test_grafx_graph_store as native_store
from test_grafx_graph_store import BOARD_ID, _attrs

real_store = native_store.real_store


def test_graph_read_distinguishes_inferred_proxy_from_parallel_unclassified_edge(monkeypatch):
    rule = 'violates/bug_origin_proxy_tr/origin@v2.1'
    executor = _RelationshipAwareExecutor({'violates__Bug__Constraint': [
        ['bug', 'constraint', 0.8, rule, 'deterministic', 'worker_layer1', 'inferred_origin_proxy:card:origin'],
        ['bug', 'constraint', 0.8, 'human-rule', 'cognitive', 'reviewer', ''],
        ['other-bug', 'constraint', 0.8, rule, 'deterministic', 'worker_layer1', 'inferred_origin_proxy:card:origin'],
    ]})
    monkeypatch.setattr(kg_routes, 'resolve_cypher_executor', lambda: executor)
    monkeypatch.setattr(kg_routes, '_relation_pairs', lambda *_: [('violates', 'Bug', 'Constraint')])
    edges, diagnostic = kg_routes._fetch_edges_for_nodes('board', {'bug', 'other-bug', 'constraint'})
    assert diagnostic['edge_read_status'] == 'ok'
    assert len(edges) == 3
    inferred = [edge for edge in edges if edge.get('assertion_basis') == 'origin_proxy']
    assert {edge['source'] for edge in inferred} == {'bug', 'other-bug'}
    assert all(edge['causal_conclusion'] == 'not_established' for edge in inferred)
    assert len({edge['id'] for edge in edges}) == 3
    assert 'r.rule_id' in executor.queries[0] and 'r.fallback_reason' in executor.queries[0]


def test_native_constraint_read_preserves_proxy_provenance(real_store, monkeypatch):
    from okto_pulse.core.kg import kg_service
    store, database, _, _ = real_store
    store.create_node(BOARD_ID, 'Constraint', 'proxy-constraint',
        _attrs('Shared association', 'spec:one:tr:one', 'seed'))
    rule = 'violates/bug_origin_proxy_tr/origin@v2.1'
    for identity, title in [('proxy-bug-a', 'A: disk fault'), ('proxy-bug-b', 'B: timeout')]:
        store.create_node(BOARD_ID, 'Bug', identity, _attrs(title, 'card:' + identity, 'seed'))
        store.create_edge(BOARD_ID, 'violates', identity, 'proxy-constraint', {
            'confidence': 0.8, 'rule_id': rule, 'layer': 'deterministic',
            'created_by': 'worker_layer1', 'fallback_reason': 'inferred_origin_proxy:card:origin',
        }, from_type='Bug', to_type='Constraint')
    before = database.transactions
    monkeypatch.setattr(kg_service, '_get_graph_store', lambda: store)
    result = kg_service.KGService().explain_constraint(BOARD_ID, 'proxy-constraint')
    assert {row['id'] for row in result['violations']} == {'proxy-bug-a', 'proxy-bug-b'}
    assert len(result['violations']) == 2
    assert all(row['confidence'] == 0.8 and row['assertion_basis'] == 'origin_proxy'
        and row['causal_conclusion'] == 'not_established' for row in result['violations'])
    assert database.transactions == before
