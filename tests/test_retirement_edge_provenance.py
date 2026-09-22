"""New graph edges belong to the exact acknowledged sessions, per audit."""

from contextlib import nullcontext
from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

from okto_pulse.core.kg.logical_transfer import LOGICAL_NULL
from okto_pulse.community.adapters import retirement_candidate_graph_reconciliation as reconcile
from okto_pulse.community.adapters.relational_recovery_snapshot import _deadline
from logical_transfer_matrix_support import MaterializedSource
from test_retirement_projection_partitions import corpus_with_partition


def graph_report(monkeypatch, session, expected):
    corpus = corpus_with_partition('working', 'working_immature')
    relation = corpus.relations[0]
    relation = replace(relation, properties={**relation.properties, 'created_by_session_id': session})
    corpus = replace(corpus, relations=(relation,))
    monkeypatch.setattr(reconcile, 'connect', lambda *args, **kwargs: nullcontext(object()))
    monkeypatch.setattr(reconcile, 'make_grafx_logical_source', lambda *args, **kwargs: MaterializedSource(corpus))
    return reconcile._board_graph(SimpleNamespace(physical_path='pinned', page_size=8192),
        {(node.type_name, node.key): 'session' for node in corpus.nodes}, 1, {}, _deadline(30),
        expected_edge_sessions=expected)


@pytest.mark.parametrize('session', ['foreign', '', LOGICAL_NULL], ids=['foreign', 'empty', 'null'])
def test_matching_global_count_does_not_authorize_unacknowledged_edge(monkeypatch, session):
    with pytest.raises(ValueError, match='graph_edge_session_changed'):
        graph_report(monkeypatch, session, {'session': 1})


def test_same_total_cannot_move_an_edge_to_another_acknowledged_session(monkeypatch):
    with pytest.raises(ValueError, match='graph_edge_session_count_changed'):
        graph_report(monkeypatch, 'other', {'session': 1, 'other': 0})


def test_matching_session_census_preserves_zero_edge_acks(monkeypatch):
    result = graph_report(monkeypatch, 'session', {'session': 1, 'other': 0})
    assert result['edge_session_validation'] == 'passed'
    assert result['edge_session_count'] == 2
    assert len(result['edge_session_sha256']) == 64


@pytest.mark.parametrize('counts,reason', [([], 'missing'), ([1, 0], 'duplicate')])
def test_each_ack_requires_one_audit_even_if_global_count_could_match(tmp_path, counts, reason):
    path = tmp_path / 'audit.sqlite3'
    with sqlite3.connect(path) as sql:
        sql.execute('CREATE TABLE kuzu_node_refs (board_id TEXT, session_id TEXT, '
            'kuzu_node_type TEXT, kuzu_node_id TEXT, operation TEXT)')
        sql.execute('CREATE TABLE consolidation_audit (session_id TEXT, edges_added INTEGER)')
        sql.executemany('INSERT INTO consolidation_audit VALUES (?, ?)', [('session', count) for count in counts])
    ack = SimpleNamespace(board_id='board', consolidation_session_id='session', node_ref_count=0)
    with pytest.raises(ValueError, match='graph_audit_' + reason):
        reconcile._relational_evidence(path, (ack,))
