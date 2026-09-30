"""Qualify the governed writer's delta before composing it into an upgrade.

The shared fixture substitutes unrelated lifecycle gates and health only. This
does not certify a complete upgrade or grant historical applicability.
"""
from collections import Counter
from dataclasses import asdict
import json

import pytest

from okto_pulse.community.adapters.grafx_recovery_contracts import make_grafx_recovery_logical_source
from okto_pulse.community.adapters.logical_transfer_schema import board_logical_schema
from okto_pulse.core.domain.learning_closeout import LearningCaptureSelection
from okto_pulse.core.domain.learning_materialization_work import parse_learning_capture_work_ref
from okto_pulse.core.ports.learning_reconciliation import select_learning_reconciliation
from test_learning_materialization_writer import (
    BOARD, graph_runtime as _graph_runtime, runtime as _runtime,
    independent_gates as _independent_gates,
)

pytestmark = pytest.mark.asyncio
graph_runtime = _graph_runtime
runtime = _runtime
independent_gates = _independent_gates


async def sql_cells(factory):
    async with factory() as session:
        connection = await session.connection()
        names = (await connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")).scalars().all()
        result = {}
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            rows = await connection.exec_driver_sql('SELECT * FROM ' + quoted)
            result[name] = (tuple(rows.keys()), Counter(tuple(row) for row in rows))
        return result


def graph_cells():
    from kg_schema_testing import open_board_connection

    with open_board_connection(BOARD) as (database, _):
        snapshot = make_grafx_recovery_logical_source(database, scope='board').open_snapshot()
        try:
            nodes = tuple(node for batch in snapshot.iter_nodes(batch_size=500) for node in batch)
            relations = tuple(edge for batch in snapshot.iter_relations(batch_size=500) for edge in batch)
        finally:
            snapshot.close()
    return nodes, relations


@pytest.mark.parametrize('existing_generation', [None, 'mg_predecessor'])
async def test_selected_capture_has_only_owned_append_deltas(graph_runtime, record_property, monkeypatch,
        existing_generation):
    runtime, capture, _, persister = graph_runtime
    factory, _, store, _ = runtime
    from okto_pulse.community.adapters import relational_schema_steps
    monkeypatch.setattr(relational_schema_steps, 'get_engine', lambda: factory.kw['bind'])
    await relational_schema_steps._migrate_global_discovery_recovery_control_plane()
    from okto_pulse.community.adapters.materialization_health import materialization_generation_key
    if existing_generation is not None:
        async with factory() as session:
            connection = await session.connection()
            await connection.exec_driver_sql('INSERT INTO app_settings (key,value) VALUES (?,?)',
                (materialization_generation_key(BOARD), existing_generation))
            await session.commit()
    records = tuple(asdict(row) for row in await store.enumerate(BOARD))
    before_nodes, before_edges = graph_cells()
    selected, = select_learning_reconciliation(schema=board_logical_schema(), board_id=BOARD,
        records=records, nodes=before_nodes)
    assert selected.state == 'awaiting_revalidation'
    work, = [parse_learning_capture_work_ref(ref) for ref in selected.work_refs]
    assert work.fingerprint == capture.record_fingerprint
    before = await sql_cells(factory)
    assert await persister.persist_authored_learning(BOARD, work.bug_id,
        LearningCaptureSelection(learning_id=work.learning_id, generation=work.generation,
            fingerprint=work.fingerprint), raise_failures=True)
    after = await sql_cells(factory)
    assert set(before) == set(after)
    changed = {name: {'before': sum(before[name][1].values()), 'after': sum(after[name][1].values())}
        for name in before if before[name] != after[name]}
    record_property('relational_changed_tables', json.dumps(changed, sort_keys=True))
    assert set(changed) == {'consolidation_audit', 'kuzu_node_refs', 'global_update_outbox',
        'kg_cognitive_source_revisions', 'app_settings', 'domain_events',
        'global_discovery_source_revision'}, changed
    for name in before:
        assert before[name][0] == after[name][0], name
        if name not in {'global_discovery_source_revision', 'app_settings'}:
            assert not (before[name][1] - after[name][1]), name

    def added(table):
        return [dict(zip(after[table][0], row, strict=True))
            for row in (after[table][1] - before[table][1]).elements()]

    audit, = added('consolidation_audit')
    ref, = added('kuzu_node_refs')
    outbox, = added('global_update_outbox')
    event, = added('domain_events')
    setting, = added('app_settings')
    revision, = added('kg_cognitive_source_revisions')
    assert audit['board_id'] == ref['board_id'] == outbox['board_id'] == event['board_id'] == BOARD
    assert audit['artifact_type'] == 'bug' and audit['artifact_id'] == work.bug_id
    assert audit['agent_id'] == 'cognitive_closeout_worker'
    assert audit['nodes_added'] == audit['edges_added'] == 1
    assert audit['nodes_updated'] == audit['nodes_superseded'] == 0
    assert ref['session_id'] == outbox['session_id'] == audit['session_id']
    assert ref['kuzu_node_id'] == work.learning_id and ref['operation'] == 'add'
    parent, = [dict(zip(before['kg_cognitive_sources'][0], row, strict=True))
        for row in before['kg_cognitive_sources'][1]]
    assert parent['node_id'] == work.learning_id and parent['generation'] == work.generation
    assert revision['cognitive_source_id'] == parent['id'] and revision['source_revision'] == 1
    assert revision['source_session_id'] == audit['session_id']
    assert outbox['event_type'] == 'consolidation_committed'
    assert json.loads(outbox['payload']) == {'artifact_id': work.bug_id, 'session_id': audit['session_id'],
        'nodes_added': 1, 'nodes_updated': 0, 'nodes_superseded': 0, 'edges_added': 1}
    assert setting['key'] == materialization_generation_key(BOARD)
    removed_settings = [dict(zip(before['app_settings'][0], row, strict=True))
        for row in (before['app_settings'][1] - after['app_settings'][1]).elements()]
    assert removed_settings == ([] if existing_generation is None else [
        {'key': setting['key'], 'value': existing_generation}])
    payload = json.loads(event['payload_json'])
    assert event['event_type'] == 'kg.materialization_generation_advanced'
    assert payload == {'correlation_id': audit['session_id'],
        'previous_materialization_generation': existing_generation or 'unmaterialized-v1',
        'materialization_generation': setting['value']}
    old_fence, = [dict(zip(before['global_discovery_source_revision'][0], row, strict=True))
        for row in before['global_discovery_source_revision'][1]]
    new_fence, = added('global_discovery_source_revision')
    assert new_fence['revision'] - old_fence['revision'] == 5
    assert new_fence['incarnation_id'] == old_fence['incarnation_id']
    assert (await store.enumerate(BOARD))[0] == capture
    after_nodes, after_edges = graph_cells()
    existing = {(node.type_name, node.key): node for node in before_nodes}
    current = {(node.type_name, node.key): node for node in after_nodes}
    graph_changes = {str(key): {name: [str(node.properties.get(name)), str(current[key].properties.get(name))]
        for name in set(node.properties) | set(current[key].properties)
        if node.properties.get(name) != current[key].properties.get(name)}
        for key, node in existing.items() if current.get(key) != node}
    record_property('graph_existing_property_changes', json.dumps(graph_changes, sort_keys=True))
    # The existing governed commit's degree-delta hook recomputes only these
    # usage fields on the referenced Bug. This is an observed, bounded effect,
    # not a blanket allowance to rewrite historical semantic properties.
    assert set(graph_changes) == {str(('Bug', 'canonical-bug'))}, graph_changes
    assert set(graph_changes[str(('Bug', 'canonical-bug'))]) == {
        'last_recomputed_at', 'relevance_score'}
    bug = current['Bug', 'canonical-bug']
    assert 0 <= bug.properties['relevance_score'] <= 1
    assert set(current) - set(existing) == {('Learning', capture.node_id)}
    assert all(edge in after_edges for edge in before_edges)
    added = tuple(edge for edge in after_edges if edge not in before_edges)
    record_property('graph_added_relations', str(len(added)))
    assert len(added) == 1
    assert (added[0].source_type, added[0].source_key, added[0].target_type, added[0].target_key) == (
        'Learning', capture.node_id, 'Bug', 'canonical-bug')
