"""A preserved orphan cannot excuse an orphan in the current projection."""

from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from okto_pulse.core.kg.logical_transfer import LOGICAL_NULL, LogicalFingerprintAccumulator
from okto_pulse.core.ports.projection_history import ProjectionSourceRoot
from okto_pulse.community.adapters import retirement_candidate_graph_reconciliation as reconcile
from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline
from logical_transfer_matrix_support import MaterializedSource, one_node_corpus


@pytest.mark.parametrize('preserved', [False, True])
def test_current_root_must_not_be_an_orphan_even_when_its_record_is_preserved(monkeypatch, preserved):
    corpus = one_node_corpus('board', key='root')
    node = corpus.nodes[0]
    properties = {**node.properties, 'source_artifact_ref': 'spec:s',
        'source_session_id': 'session-a', 'created_by_agent': 'system:historical_consolidation',
        'generation': 0, 'superseded_by': LOGICAL_NULL,
        **{field: LOGICAL_NULL for field in reconcile._SOURCE_FIELDS}}
    node = replace(node, properties=properties)
    corpus = replace(corpus, nodes=(node,))
    fingerprint = LogicalFingerprintAccumulator.for_schema(corpus.schema)
    fingerprint.add_node(node)
    prior = {'delta': {'unchanged_nodes': [{'node_type': node.type_name, 'node_id': node.key,
        'fingerprint': fingerprint.digest()}], 'retained_edges': []}} if preserved else None
    monkeypatch.setattr(reconcile, 'connect', lambda *args, **kwargs: nullcontext(object()))
    monkeypatch.setattr(reconcile, 'make_grafx_logical_source', lambda *args, **kwargs: MaterializedSource(corpus))
    binding = SimpleNamespace(physical_path='unused-pinned-reader', page_size=8192)
    roots = {ProjectionSourceRoot(node.type_name, 'spec:s'): {field: None for field in reconcile._SOURCE_FIELDS}}
    refs = {} if preserved else {(node.type_name, node.key): 'session-a'}
    with pytest.raises(ValueError, match='graph_orphan_detected'):
        reconcile._board_graph(binding, refs, 0, roots, _deadline(30), prior)
