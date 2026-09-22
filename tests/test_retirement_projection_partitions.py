"""The private candidate must retain the partition chosen by the Core plan."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from okto_pulse.core.kg.logical_transfer import LOGICAL_NULL, LogicalNode, LogicalRelation
from okto_pulse.core.ports.projection_history import ProjectionSourceRoot
from okto_pulse.community.adapters import retirement_candidate_graph_reconciliation as reconcile
from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline
from logical_transfer_matrix_support import MaterializedSource, one_node_corpus


def corpus_with_partition(layer, maturity):
    corpus = one_node_corpus('board')
    nodes = []
    for kind, key, source in (('Entity', 'root', 'board:board'),
            ('Requirement', 'child', 'spec:s:fr:r')):
        properties = {prop.name: LOGICAL_NULL for prop in corpus.schema.node_type(kind).properties}
        properties.update(id=key, source_artifact_ref=source, source_session_id='session',
            created_by_agent='system:historical_consolidation', generation=0,
            graph_layer=layer, maturity_status=maturity)
        nodes.append(LogicalNode(kind, key, properties))
    layout = corpus.schema.relation_layout('belongs_to', 'Requirement', 'Entity')
    properties = {prop.name: LOGICAL_NULL for prop in layout.properties}
    properties.update(rule_id='belongs_to/requirement@v1', layer='deterministic', created_by='system:worker')
    relation = LogicalRelation('belongs_to', 'Requirement', 'Entity', 'child', 'root', properties)
    return replace(corpus, nodes=tuple(nodes), relations=(relation,))


def report(monkeypatch, corpus, expected):
    monkeypatch.setattr(reconcile, 'connect', lambda *args, **kwargs: nullcontext(object()))
    monkeypatch.setattr(reconcile, 'make_grafx_logical_source', lambda *args, **kwargs: MaterializedSource(corpus))
    return reconcile._board_graph(SimpleNamespace(physical_path='pinned', page_size=8192),
        {(node.type_name, node.key): 'session' for node in corpus.nodes}, 1, {}, _deadline(30),
        expected_partitions=expected)


@pytest.mark.parametrize('kind,source', [('Entity', 'board:board'), ('Requirement', 'spec:s:fr:r')])
@pytest.mark.parametrize('layer,maturity', [('canonical', 'canonical_eligible'),
    ('working', 'working_stale'), (LOGICAL_NULL, LOGICAL_NULL)], ids=['canonical', 'stale', 'unknown'])
def test_counts_and_connectivity_cannot_hide_wrong_source_partition(monkeypatch, kind, source, layer, maturity):
    corpus = corpus_with_partition(layer, maturity)
    # This passed the preceding census/metadata/edge-count checks. A child is
    # independently checked even though source timestamps belong to its root.
    assert report(monkeypatch, corpus, None)['source_partition_validation'] == 'not_checked'
    with pytest.raises(ValueError, match='source_partition_changed'):
        report(monkeypatch, corpus, {ProjectionSourceRoot(kind, source): ('working', 'working_immature')})


def test_exact_working_projection_is_accepted_without_promoting_it(monkeypatch):
    corpus = corpus_with_partition('working', 'working_immature')
    expected = {ProjectionSourceRoot(node.type_name, node.properties['source_artifact_ref']):
        ('working', 'working_immature') for node in corpus.nodes}
    result = report(monkeypatch, corpus, expected)
    assert result['source_partition_validation'] == 'passed'
    assert result['source_partition_count'] == 2
    assert len(result['source_partition_sha256']) == 64


def test_plan_partitions_preserve_child_identity_and_refuse_conflicting_sources():
    root = {'node_type': 'Entity', 'source_artifact_ref': 'spec:s',
        'graph_layer': 'working', 'maturity_status': 'working_immature'}
    child = {**root, 'node_type': 'Requirement', 'source_artifact_ref': 'spec:s:fr:r'}
    plan = {'plans': [{'projection': None}, {'projection': {'nodes': [root, child, root]}}]}
    assert len(reconcile._partition_expectations(plan)) == 2
    plan['plans'].append({'projection': {'nodes': [{**child, 'graph_layer': 'canonical'}]}})
    with pytest.raises(ValueError, match='source_partition_conflict'):
        reconcile._partition_expectations(plan)


def test_unclassified_plan_is_not_an_implicit_canonical_default():
    with pytest.raises(ValueError, match='source_partition_invalid'):
        reconcile._partition_expectations({'plans': [{'projection': {'nodes': [
            {'node_type': 'Entity', 'source_artifact_ref': 'spec:s'}]}}]})
