"""Storage ordering refuses incomplete/cyclic Card source closures."""
import sqlite3

import pytest

from okto_pulse.community.adapters.board_rebuild_ingestion import _ordered_rebuild_sources


@pytest.fixture
def connection():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('CREATE TABLE cards(id TEXT PRIMARY KEY, board_id TEXT)')
        conn.execute('CREATE TABLE card_dependencies(card_id TEXT, depends_on_id TEXT)')
        conn.executemany('INSERT INTO cards VALUES (?, ?)',
            [('a', 'board'), ('b', 'board'), ('z', 'board'), ('foreign', 'other')])
        yield conn


def ordered(conn, identities, source_types=None):
    return [row['id'] for row in _ordered_rebuild_sources(conn, board_id='board',
        sources=[{'artifact_type': source_types[index] if source_types else 'card', 'id': identity}
                 for index, identity in enumerate(identities)])]


@pytest.mark.parametrize('source_types', [('card', 'card', 'card'),
                                         ('task', 'test', 'bug'), ('bug', 'task', 'test')])
def test_prerequisites_precede_dependents_independent_of_input_order(connection, source_types):
    connection.executemany('INSERT INTO card_dependencies VALUES (?, ?)', [('a', 'b'), ('b', 'z')])
    assert ordered(connection, ['a', 'b', 'z'], source_types) == ['z', 'b', 'a']
    assert ordered(connection, ['b', 'z', 'a'], source_types) == ['z', 'b', 'a']


@pytest.mark.parametrize('identities,edges,reason', [
    (['a', 'missing'], [], 'rebuild_card_source_missing'),
    (['a', 'foreign'], [], 'rebuild_card_source_missing'),
    (['a'], [('a', 'b')], 'rebuild_card_dependency_prerequisite_missing'),
    (['a'], [('a', 'foreign')], 'rebuild_card_dependency_prerequisite_missing'),
    (['a', 'b'], [('a', 'b'), ('b', 'a')], 'rebuild_card_dependency_cycle'),
    (['a'], [('a', 'a')], 'rebuild_card_dependency_cycle'),
])
def test_invalid_closure_refuses_without_modifying_sources(connection, identities, edges, reason):
    connection.executemany('INSERT INTO card_dependencies VALUES (?, ?)', edges)
    before = list(connection.iterdump())
    with pytest.raises(RuntimeError, match=reason):
        ordered(connection, identities)
    assert list(connection.iterdump()) == before


def test_unrelated_sources_do_not_join_selected_board_closure(connection):
    connection.executemany('INSERT INTO card_dependencies VALUES (?, ?)', [('foreign', 'a'), ('b', 'z')])
    assert ordered(connection, ['a']) == ['a']
