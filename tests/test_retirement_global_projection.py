"""Cold native Global comparison derives its expected records from Board sources."""

from dataclasses import replace

import pytest
from okto_grafx import connect

from okto_pulse.core.kg.logical_transfer import LogicalRelation, LogicalVector
from okto_pulse.core.kg.interfaces.graph_errors import GraphCapabilityUnavailable, GraphError
from okto_pulse.community.adapters.composition import build_community_embedding
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.grafx_global_discovery_runtime import CommunityGrafxGlobalDiscoveryRuntime
from okto_pulse.community.adapters.logical_transfer_schema import board_logical_schema, global_logical_schema
from okto_pulse.community.adapters.retirement_candidate_global_reconciliation import compare_candidate_global_projection
from okto_pulse.community.adapters.retirement_candidate_global_materialization import (
    materialize_missing_candidate_global, verify_candidate_global_materialization,
)
from okto_pulse.community.config import CommunitySettings
from logical_transfer_matrix_support import Corpus, complete_node, seed_generation


@pytest.mark.parametrize('case', ['matched', 'wrong_layer', 'duplicate_link', 'auxiliary', 'production_writer'])
def test_native_global_is_compared_with_full_source_identity_and_policy(tmp_path, case):
    settings = CommunitySettings(kg_embedding_mode='stub', kg_embedding_dim=384)
    provider = build_community_embedding(settings=settings)
    bindings = CommunityGraphBackendBindingStore(tmp_path / 'kg-artifacts')
    board_schema, global_schema = board_logical_schema(), global_logical_schema()

    def vector(schema, kind, field, text):
        space = schema.vector_space(schema.node_type(kind).property_def(field).vector_space)
        return LogicalVector(space.name, space.storage_dtype, tuple(provider.encode(text)))

    source = complete_node(board_schema, 'Entity', 'source', 0, null_nullable=True)
    source = replace(source, properties={**source.properties, 'title': 'Source title', 'graph_layer': 'canonical',
        'embedding': vector(board_schema, 'Entity', 'embedding', 'Source title')})
    root = complete_node(global_schema, 'Board', 'board', 0, null_nullable=True)
    root = replace(root, properties={**root.properties, 'name': 'Board', 'summary': '',
        'topic_count': 0, 'entity_count': 0, 'decision_count': 1,
        'summary_embedding': vector(global_schema, 'Board', 'summary_embedding', 'Board Board')})
    digest = complete_node(global_schema, 'DecisionDigest', 'dd_board_source', 0, null_nullable=True)
    digest = replace(digest, properties={**digest.properties, 'board_id': 'board', 'original_node_id': 'source',
        'title': 'Source title', 'one_line_summary': 'Source title', 'node_type': 'Entity',
        'graph_layer': 'working' if case == 'wrong_layer' else 'canonical', 'source_revoked': False,
        'embedding': vector(global_schema, 'DecisionDigest', 'embedding', 'Source title')})
    link = LogicalRelation('CONTAINS_DECISION', 'Board', 'DecisionDigest', 'board', 'dd_board_source', {})
    global_nodes = (root, digest)
    if case == 'auxiliary':
        global_nodes += (complete_node(global_schema, 'Topic', 'unclassified', 0, null_nullable=True),)
    for scope, path, corpus in (
        ('board', bindings.board_grafx_path('board', 'private'), Corpus(board_schema, (source,), ())),
        ('global_discovery', bindings.global_grafx_path('private'),
            Corpus(global_schema, global_nodes, (link, link) if case == 'duplicate_link' else (link,))),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        if scope == 'global_discovery' and case == 'production_writer':
            with connect(path, page_size=8192) as graph:
                runtime = CommunityGrafxGlobalDiscoveryRuntime(lambda: graph, lambda: path,
                    graph.close, lambda phase: None)
                runtime.ensure_layer_schema()
                runtime.upsert_board_summary(board_id='board', name='Board', summary='',
                    summary_embedding=list(provider.encode('Board Board')), decision_count=1,
                    synced_at='2026-01-01T00:00:00Z')
                runtime.create_recovery_digest_batch(board_id='board', rows=({'digest_id': 'dd_board_source',
                    'board_id': 'board', 'original_node_id': 'source', 'title': 'Source title',
                    'summary': 'Source title', 'node_type': 'Entity', 'graph_layer': 'canonical',
                    'embedding': list(provider.encode('Source title')), 'created_at': '2026-01-01T00:00:00Z'},))
                graph.checkpoint()  # Same cold-reader prerequisite as the recovery coordinator.
        else:
            seed_generation('grafx', path, corpus)
        with connect(path, page_size=8192, read_only=True) as graph:
            options = dict(backend='grafx', generation='private', physical_path=path, page_size=8192, database=graph)
            if scope == 'board':
                bindings.initialize_board_binding(board_id='board', **options)
            else:
                bindings.initialize_global_binding(**options)
    inputs = {'state': 'captured_not_reconciled', 'boards': [{'board_id': 'board', 'board_name': 'Board',
        'board_summary': '', 'overlay_exclusions': []}]}
    report = compare_candidate_global_projection(tmp_path, inputs, settings=settings, max_seconds=60)
    assert report['state'] == ('matched' if case in {'matched', 'production_writer'} else 'mismatch')
    assert report['materialized'] and report['expected_nodes'] == 2
    if case == 'wrong_layer':
        assert report['changed_nodes'] == 1
    elif case == 'duplicate_link':
        assert report['unexpected_relations'] == 1
    elif case == 'auxiliary':
        assert report['unexpected_nodes'] == 1
    assert compare_candidate_global_projection(tmp_path, inputs, settings=settings, max_seconds=60) == report


@pytest.mark.parametrize('case', ['created', 'fence_lost', 'fence_after_summary', 'unknown_layer'])
def test_private_global_creation_is_source_owned_and_fenced(tmp_path, monkeypatch, case):
    settings = CommunitySettings(kg_embedding_mode='stub', kg_embedding_dim=384)
    provider = build_community_embedding(settings=settings)
    schema = board_logical_schema()
    node = complete_node(schema, 'Entity', 'source', 0, null_nullable=True)
    space = schema.vector_space(schema.node_type('Entity').property_def('embedding').vector_space)
    node = replace(node, properties={**node.properties, 'title': 'Source title',
        'graph_layer': 'legacy_unknown' if case == 'unknown_layer' else 'canonical',
        'embedding': LogicalVector(space.name, space.storage_dtype, tuple(provider.encode('Source title')))})
    bindings = CommunityGraphBackendBindingStore(tmp_path / 'kg-artifacts')
    path = bindings.board_grafx_path('board', 'private')
    path.parent.mkdir(parents=True)
    seed_generation('grafx', path, Corpus(schema, (node,), ()))
    with connect(path, page_size=8192, read_only=True) as graph:
        bindings.initialize_board_binding(board_id='board', backend='grafx', generation='private',
            physical_path=path, page_size=8192, database=graph)
    inputs = {'state': 'captured_not_reconciled', 'boards': [{'board_id': 'board', 'board_name': 'Board',
        'board_summary': '', 'overlay_exclusions': []}]}
    options = dict(settings=settings, generation='private', max_seconds=60)
    if case == 'fence_after_summary':
        alive = [True]
        original = CommunityGrafxGlobalDiscoveryRuntime.upsert_board_summary

        def lose_after_summary(runtime, **kwargs):
            original(runtime, **kwargs)
            alive[0] = False

        monkeypatch.setattr(CommunityGrafxGlobalDiscoveryRuntime, 'upsert_board_summary', lose_after_summary)
        with pytest.raises(GraphError) as lost:
            materialize_missing_candidate_global(tmp_path, inputs, require_live=lambda: alive[0], **options)
        assert isinstance(lost.value.__cause__, ValueError)
        assert str(lost.value.__cause__) == 'retirement_global_execution_fence_lost'
        with pytest.raises(GraphCapabilityUnavailable) as failure:
            bindings.inspect_global_binding()
        assert failure.value.details['reason'] == 'binding_missing'
        return  # The enclosing unpublished stage owns cleanup of partial files.
    if case == 'fence_lost':
        with pytest.raises(ValueError, match='execution_fence_lost'):
            materialize_missing_candidate_global(tmp_path, inputs, require_live=lambda: False, **options)
        assert not bindings.global_grafx_path('private').exists()
        return
    receipt = materialize_missing_candidate_global(tmp_path, inputs, require_live=lambda: True, **options)
    comparison = compare_candidate_global_projection(tmp_path, inputs, settings=settings, max_seconds=60)
    verify_candidate_global_materialization(tmp_path, inputs, receipt, had_global=False, comparison=comparison, **options)
    if case == 'unknown_layer':
        assert receipt == {'state': 'unsupported_source_layer'}
        assert not comparison['materialized'] and comparison['state'] == 'mismatch'
        return
    assert receipt['state'] == 'created' and comparison['state'] == 'matched'
    with pytest.raises(ValueError, match='receipt_changed'):
        verify_candidate_global_materialization(tmp_path, inputs, {**receipt, 'expected_sha256': '0' * 64},
            had_global=False, comparison=comparison, **options)
    assert materialize_missing_candidate_global(tmp_path, inputs, require_live=lambda: True, **options) == {'state': 'retained'}
    assert compare_candidate_global_projection(tmp_path, inputs, settings=settings, max_seconds=60) == comparison
