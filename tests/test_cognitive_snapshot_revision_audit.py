"""A current-head snapshot cannot conceal corrupt older durable revisions."""

import json
import sqlite3

import pytest

from okto_pulse.community.adapters.board_source_reader import read_realm_cognitive_source_snapshot
from okto_pulse.core.ports.kg_cognitive_source import canonical_cognitive_source_fingerprint


def seed(connection, *, corrupt=None):
    connection.executescript('''
        CREATE TABLE boards (id TEXT, realm_id TEXT);
        CREATE TABLE kg_cognitive_sources (id TEXT, board_id TEXT, node_id TEXT, node_type TEXT,
            generation INTEGER, payload TEXT, evidence_refs TEXT, source_session_id TEXT, committed_at TEXT);
        CREATE TABLE kg_cognitive_source_revisions (id TEXT, cognitive_source_id TEXT, source_revision INTEGER,
            payload TEXT, evidence_refs TEXT, source_session_id TEXT, committed_at TEXT, record_fingerprint TEXT);
        INSERT INTO boards VALUES ('board', 'local');
    ''')
    refs = ['final_report:r']
    payload = {'title': 'birth'}
    connection.execute('INSERT INTO kg_cognitive_sources VALUES (?,?,?,?,?,?,?,?,?)',
        ('source', 'board', 'node', 'Decision', 0, json.dumps(payload), json.dumps(refs), 'kgses_old', '2026-01-01T00:00:00'))
    for revision in (1, 2):
        payload = {'title': f'revision-{revision}'}
        fingerprint = canonical_cognitive_source_fingerprint(board_id='board', node_id='node', node_type='Decision',
            generation=0, payload=payload, evidence_refs=refs)
        raw_payload = json.dumps(payload)
        if revision == 1 and corrupt == 'fingerprint': fingerprint = 'a' * 64
        if revision == 1 and corrupt == 'payload': raw_payload = '[]'
        stored_revision = -1 if revision == 1 and corrupt == 'negative_revision' else revision
        if revision == 1 and corrupt == 'conflicting_birth': stored_revision = 0
        raw_refs = json.dumps([False]) if revision == 1 and corrupt == 'evidence_refs' else json.dumps(refs)
        connection.execute('INSERT INTO kg_cognitive_source_revisions VALUES (?,?,?,?,?,?,?,?)',
            (f'rev-{revision}', 'source', stored_revision, raw_payload, raw_refs, 'kgses_old', f'2026-01-0{revision + 1}T00:00:00', fingerprint))


@pytest.mark.parametrize('corrupt', ['fingerprint', 'payload', 'evidence_refs', 'negative_revision', 'conflicting_birth'])
def test_unselected_older_corruption_rejects_current_snapshot(corrupt):
    with sqlite3.connect(':memory:') as connection:
        connection.row_factory = sqlite3.Row
        seed(connection, corrupt=corrupt)
        with pytest.raises(ValueError):
            read_realm_cognitive_source_snapshot(connection, realm_id='local')


def test_full_revision_audit_returns_only_the_same_latest_source_without_writes():
    with sqlite3.connect(':memory:') as connection:
        connection.row_factory = sqlite3.Row
        seed(connection)
        before = list(connection.iterdump())
        rows = read_realm_cognitive_source_snapshot(connection, realm_id='local')
        assert len(rows['board']) == 1
        assert rows['board'][0]['source_revision'] == 2
        assert json.loads(rows['board'][0]['payload']) == {'title': 'revision-2'}
        assert list(connection.iterdump()) == before
