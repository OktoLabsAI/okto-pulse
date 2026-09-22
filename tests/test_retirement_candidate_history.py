"""Cold candidate history includes old records and every routed scope."""

import shutil

import pytest

from okto_grafx import connect
from okto_pulse.core.ports.projection_effects import ProjectionPropertyEffect, ProjectionPropertyEffects

from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.retirement_candidate_history import observe_candidate_history
from logical_transfer_matrix_support import seed_generation
import test_joint_recovery_snapshot as recovery

sources = recovery.sources


def test_candidate_observes_preserved_retired_source_content_change_and_global_edge(sources, tmp_path):
    snapshot = recovery.capture(sources)
    target = tmp_path / 'candidate'
    target.mkdir()
    shutil.copy2(snapshot.directory / 'relational/database.sqlite3', target / 'database.sqlite3')
    bindings = CommunityGraphBackendBindingStore(target / 'kg-artifacts')
    for corpus in sources[4]:
        board = corpus.schema.scope == 'board'
        path = (bindings.board_grafx_path('board-one', 'candidate') if board
            else bindings.global_grafx_path('candidate'))
        path.parent.mkdir(parents=True)
        seed_generation('grafx', path, corpus)
        with connect(path, page_size=8192) as graph:
            options = dict(backend='grafx', generation='candidate', physical_path=path,
                page_size=8192, database=graph)
            if board:
                bindings.initialize_board_binding(board_id='board-one', **options)
            else:
                bindings.initialize_global_binding(**options)
    before = observe_candidate_history(target, snapshot)
    assert before['state'] == 'observed_not_classified'
    assert len(before['graphs']) == 2
    for scope in before['graphs']:
        delta = scope['delta']
        assert len(delta['unchanged_nodes']) == 1
        assert not delta['changed_nodes'] and not delta['removed_nodes']
    binding = bindings.inspect_board_binding('board-one')
    with connect(binding.physical_path, page_size=8192) as graph:
        with graph.begin('write') as writer:
            writer.execute("MATCH (n:Decision {id:'baseline'}) SET n.title='changed history'")
        graph.checkpoint()
    binding = bindings.inspect_global_binding()
    with connect(binding.physical_path, page_size=8192) as graph:
        with graph.begin('write') as writer:
            for _ in range(2):
                writer.execute("MATCH (n:Topic {id:'baseline'}) CREATE (n)-[:TOPIC_RELATES_TO {weight:0.5}]->(n)")
        graph.checkpoint()
    after = observe_candidate_history(target, snapshot)
    assert after == observe_candidate_history(target, snapshot)
    assert after['candidate_census_sha256'] != before['candidate_census_sha256']
    assert after['before_census_sha256'] == before['before_census_sha256']
    board, global_scope = (scope['delta'] for scope in after['graphs'])
    assert len(board['changed_nodes']) == 1 and not board['introduced_nodes']
    assert len(global_scope['unchanged_nodes']) == 1
    assert global_scope['introduced_edges'][0]['count'] == 2
    assert not global_scope['removed_edges']
    original = next(corpus for corpus in sources[4] if corpus.schema.scope == 'board').nodes[0]
    effects = (ProjectionPropertyEffects('board-one', 'verified-session',
        (ProjectionPropertyEffect.from_values('Decision', 'baseline',
            {'title': original.properties['title']}, {'title': 'changed history'}),)),)
    composed = observe_candidate_history(target, snapshot, property_effects=effects)
    assert composed['property_composition'] == [{'board_id': 'board-one', **board['changed_nodes'][0]}]
    assert composed['state'] == 'observed_not_classified'
    binding = bindings.inspect_board_binding('board-one')
    with connect(binding.physical_path, page_size=8192) as graph:
        with graph.begin('write') as writer:
            writer.execute("MATCH (n:Decision {id:'baseline'}) SET n.content='undeclared change'")
        graph.checkpoint()
    with pytest.raises(ValueError, match='final_node_mismatch'):
        observe_candidate_history(target, snapshot, property_effects=effects)
