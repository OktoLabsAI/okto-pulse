"""Authored Learning through real SQL, signed evidence and disposable Grafx.

Unrelated completion gates and the health probe are explicit fixtures; this
suite qualifies the governed materializer, not full lifecycle admission.
"""

from dataclasses import replace

import pytest

from okto_pulse.core.application.use_cases.card_crud import MoveCardCommand, MoveCardUseCase
from okto_pulse.core.domain.learning_closeout import LearningCaptureSelection
from okto_pulse.core.kg.cognitive_closeout_production import ConsolidationPipelinePersister
from okto_pulse.core.ports.kg_cognitive_source import register_cognitive_source_store
from test_learning_submission_writer import (
    BOARD, author, independent_gates as _independent_gates, prepare, runtime as _runtime, unit,
)

pytestmark = pytest.mark.asyncio
runtime = _runtime
independent_gates = _independent_gates


@pytest.fixture
async def graph_runtime(runtime, independent_gates, monkeypatch, tmp_path):
    from conftest import CORE_REPO
    monkeypatch.syspath_prepend(str(CORE_REPO / 'tests'))
    monkeypatch.setenv('OKTO_PULSE_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('KG_BASE_DIR', str(tmp_path / 'kg'))
    monkeypatch.setenv('KG_CLEANUP_ENABLED', 'false')
    monkeypatch.setenv('KG_EMBEDDING_MODE', 'stub')
    from kg_registry_testing import configure_real_graph_and_data_test_kg_registry
    from kg_schema_testing import bootstrap_board_graph, close_all_connections, open_board_connection
    from okto_pulse.core.services import kg_health_service
    from okto_pulse.core.kg.primitives import reset_commit_health_cache_for_tests

    factory, _, store, _ = runtime
    configure_real_graph_and_data_test_kg_registry(factory)
    from okto_pulse.community.adapters.coordination import CommunityLocalWriteLockPort
    from okto_pulse.core.ports.coordination import register_coordination_providers
    register_coordination_providers(write_lock_port=CommunityLocalWriteLockPort())
    from okto_pulse.community.adapters.sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence
    from okto_pulse.core.ports.consolidation import register_consolidation_persistence_port
    register_consolidation_persistence_port(CommunitySqlAlchemyConsolidationPersistence())
    from okto_pulse.community.adapters.sqlalchemy_canonical_debt import CommunitySqlAlchemyCanonicalDebtStore
    from okto_pulse.core.ports.canonical_debt import register_canonical_debt_store
    register_canonical_debt_store(CommunitySqlAlchemyCanonicalDebtStore())
    register_cognitive_source_store(store)
    from okto_pulse.community.adapters.bug_cognitive_context import CommunityCanonicalBugNodeReader
    from okto_pulse.core.ports.bug_cognitive_context import register_canonical_bug_node_read_port
    register_canonical_bug_node_read_port(CommunityCanonicalBugNodeReader())
    async def healthy(*args, **kwargs):
        return {'overall_state': 'healthy', 'graph_state': 'healthy',
                'discovery_state': 'healthy', 'total_nodes': 1}
    monkeypatch.setattr(kg_health_service, 'get_kg_health', healthy)
    reset_commit_health_cache_for_tests(BOARD)
    bootstrap_board_graph(BOARD)
    with open_board_connection(BOARD) as (_, connection):
        connection.execute("CREATE (b:Bug {id: 'canonical-bug', title: 'Bug', "
            "source_artifact_ref: 'bug:bug-context', graph_layer: 'canonical', "
            "maturity_status: 'canonical_eligible'})").close()
    bug_id, data = await prepare(runtime, 'done')
    async with factory() as session:
        await MoveCardUseCase().execute(MoveCardCommand(bug_id, data), actor=author(), uow=unit(session))
    capture, = await store.enumerate(BOARD)
    selection = LearningCaptureSelection(learning_id=capture.node_id,
        generation=capture.generation, fingerprint=capture.record_fingerprint)
    try:
        yield runtime, capture, selection, ConsolidationPipelinePersister(factory)
    finally:
        close_all_connections()


def graph_rows(query, params=None):
    from kg_schema_testing import open_board_connection
    with open_board_connection(BOARD) as (_, connection):
        result = connection.execute(query, params or {})
        try:
            return [list(row) for row in result.rows]
        finally:
            result.close()


async def test_authored_materialization_keeps_birth_and_replays_literal_revision(graph_runtime, caplog, monkeypatch):
    caplog.set_level('INFO', logger='okto_pulse.core.kg.cognitive_closeout_production')
    from okto_pulse.core.kg import cognitive_closeout_production
    original_info = cognitive_closeout_production.logger.info
    def detailed_failure(message, *args, **kwargs):
        original_info(message, *args, **{**kwargs, 'exc_info': True})
    monkeypatch.setattr(cognitive_closeout_production.logger, 'info', detailed_failure)
    runtime, capture, selection, persister = graph_runtime
    factory, _, store, _ = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection), caplog.text
    history = await store.enumerate(BOARD)
    assert len(history) == 2
    assert history[0] == capture
    literal = max(history, key=lambda record: record.source_revision)
    assert literal.node_id == capture.node_id and literal.source_revision == 1
    assert literal.payload['content'] == capture.payload['content']
    assert literal.payload['created_by_agent'] == 'owner'
    assert literal.evidence_refs == (*capture.evidence_refs, 'bug:bug-context')
    from okto_pulse.core.domain.learning_materialization import CapturedLearningProjection
    projection = CapturedLearningProjection(capture, literal, 'bug-context')
    expected = {**projection.fields, 'created_by_agent': 'owner',
        'created_at': capture.payload['captured_at'], 'generation': 0}
    row, = graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN ' +
        ', '.join('n.' + key for key in expected), {'id': capture.node_id})
    observed = dict(zip(expected, row, strict=True))
    from datetime import datetime
    assert datetime.fromisoformat(observed.pop('created_at')) == datetime.fromisoformat(expected.pop('created_at'))
    assert observed == expected
    from okto_pulse.core.ports.kg_cognitive_source import COGNITIVE_SOURCE_VOLATILE_USAGE_FIELDS
    fields = [key for key in literal.payload if key not in COGNITIVE_SOURCE_VOLATILE_USAGE_FIELDS and key != 'created_at']
    actual, = graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN ' +
        ', '.join('n.' + key for key in fields), {'id': capture.node_id})
    assert [key for key, value in zip(fields, actual, strict=True) if value != literal.payload[key]] == []
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == [
        [capture.node_id, 'canonical-bug']]
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    assert await store.enumerate(BOARD) == history
    async with factory() as session:
        assert await store.read_revision_in_context(session, board_id=BOARD,
            node_id=capture.node_id, generation=0, source_revision=0) == capture
        from okto_pulse.core.application.learning_capture import list_learning_captures
        page = await list_learning_captures(session, board_id=BOARD, bug_id='bug-context')
        assert len(page['items']) == 1
        assert page['items'][0]['fingerprint'] == capture.record_fingerprint
        assert page['items'][0]['capture'] == capture.payload
    graph_rows('MATCH (n:Learning) WHERE n.id = $id DETACH DELETE n', {'id': capture.node_id})
    from okto_pulse.core.kg.interfaces import get_kg_registry
    def no_reembedding(*args, **kwargs):
        raise AssertionError('Recovery must reuse the sealed vector')
    monkeypatch.setattr(get_kg_registry().require_embedding_provider(), 'encode', no_reembedding)
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection), caplog.text
    assert await store.enumerate(BOARD) == history
    assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == [
        [capture.node_id, 'canonical-bug']]


async def test_curated_projection_is_preserved_on_replay(graph_runtime):
    runtime, capture, selection, persister = graph_runtime
    _, _, store, _ = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    before = await store.enumerate(BOARD)
    graph_rows('MATCH (n:Learning) WHERE n.id = $id SET n.human_curated = true, n.content = $content',
        {'id': capture.node_id, 'content': 'Human curated correction'})
    assert not await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    assert graph_rows('MATCH (n:Learning) WHERE n.id = $id RETURN n.content, n.human_curated',
        {'id': capture.node_id}) == [['Human curated correction', True]]
    assert await store.enumerate(BOARD) == before


@pytest.mark.parametrize('projection', ['working', 'superseded', 'ambiguous', 'absent'])
async def test_materializer_resolves_only_one_active_canonical_bug(graph_runtime, projection):
    runtime, capture, selection, persister = graph_runtime
    _, _, store, _ = runtime
    before = await store.enumerate(BOARD)
    if projection == 'absent':
        graph_rows("MATCH (b:Bug) WHERE b.id = 'canonical-bug' DETACH DELETE b")
    else:
        graph_rows('CREATE (b:Bug {id: $id, title: $title, source_artifact_ref: $ref, '
            'graph_layer: $layer, superseded_by: $successor})',
            {'id': '000-old-bug', 'title': 'Historical projection', 'ref': 'bug:bug-context',
                'layer': 'working' if projection == 'working' else 'canonical',
                'successor': 'canonical-bug' if projection == 'superseded' else None})
    if projection == 'ambiguous':
        with pytest.raises(ValueError, match='canonical_bug_identity_ambiguous'):
            await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    else:
        assert await persister.persist_authored_learning(BOARD, 'bug-context', selection) == (
            projection != 'absent')
    if projection in ('ambiguous', 'absent'):
        assert await store.enumerate(BOARD) == before
        assert graph_rows('MATCH (n:Learning) RETURN n.id') == []
    else:
        assert graph_rows('MATCH (n:Learning)-[:validates]->(b:Bug) RETURN n.id, b.id') == [
            [capture.node_id, 'canonical-bug']]


async def test_target_eligibility_is_rechecked_under_writer_fence(graph_runtime, monkeypatch):
    from okto_pulse.core.ports.bug_cognitive_context import resolve_canonical_bug_node_read_port
    runtime, _, selection, persister = graph_runtime
    _, _, store, _ = runtime
    before = await store.enumerate(BOARD)
    resolver = resolve_canonical_bug_node_read_port()
    original = resolver.resolve_current
    def changed_after_read(**kwargs):
        node_id = original(**kwargs)
        graph_rows('MATCH (b:Bug) WHERE b.id = $id SET b.superseded_by = $successor',
            {'id': node_id, 'successor': 'another-projection'})
        return node_id
    monkeypatch.setattr(resolver, 'resolve_current', changed_after_read)
    assert not await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    assert await store.enumerate(BOARD) == before
    assert graph_rows('MATCH (n:Learning) RETURN n.id') == []


async def test_distinct_authored_create_never_supersedes_same_bug_learning(graph_runtime):
    runtime, first, selection, persister = graph_runtime
    factory, assembler, store, request = runtime
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    from test_learning_capture_writer import actor, uow
    from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
    async with factory() as session:
        current = await assembler.assemble_semantic(session, board_id=BOARD, bug_id='bug-context')
        draft = replace(request,
            capture_id='distinct-lesson', content='A different lesson with independent scope.',
            expected_source_digest=current.source_digest, expected_source_version=current.source_policy_version)
        second = await StageLearningCaptureUseCase().execute(draft,
            actor=actor(), uow=uow(session))
        await session.commit()
    selection = LearningCaptureSelection(learning_id=second.node_id,
        generation=second.generation, fingerprint=second.record_fingerprint)
    assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    rows = graph_rows('MATCH (n:Learning) RETURN n.id, n.superseded_by')
    assert {row[0] for row in rows} == {first.node_id, second.node_id}
    assert all(not row[1] for row in rows)
    assert len(await store.enumerate(BOARD)) == 4
    async with factory() as session:
        replay = await StageLearningCaptureUseCase().execute(draft, actor=actor(), uow=uow(session))
        assert replay.payload == second.payload and replay.record_fingerprint == second.record_fingerprint


async def test_reopened_source_cannot_materialize_old_capture(graph_runtime):
    from okto_pulse.community.adapters.sqlalchemy_models import Card
    runtime, _, selection, persister = graph_runtime
    factory, _, store, _ = runtime
    before = await store.enumerate(BOARD)
    async with factory() as session:
        bug = await session.get(Card, 'bug-context')
        bug.status = 'in_progress'
        await session.commit()
    with pytest.raises(ValueError):
        await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    assert graph_rows('MATCH (n:Learning) RETURN n.id') == []
    assert await store.enumerate(BOARD) == before


@pytest.mark.parametrize('phase', ['first', 'recovery'])
async def test_conditional_append_failure_compensates_new_graph_node(graph_runtime, monkeypatch, phase):
    runtime, capture, selection, persister = graph_runtime
    _, _, store, _ = runtime
    if phase == 'recovery':
        assert await persister.persist_authored_learning(BOARD, 'bug-context', selection)
        graph_rows('MATCH (n:Learning) WHERE n.id = $id DETACH DELETE n', {'id': capture.node_id})
    before = await store.enumerate(BOARD)
    calls = []
    async def unavailable(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('injected conditional source outage')
    monkeypatch.setattr(store, 'append_many_if_current_in_context', unavailable)
    assert not await persister.persist_authored_learning(BOARD, 'bug-context', selection)
    assert calls == [True]
    assert graph_rows('MATCH (n:Learning) RETURN n.id') == []
    assert await store.enumerate(BOARD) == before


async def test_current_history_reader_refuses_invalid_revision(runtime):
    factory, _, store, request = runtime
    from test_learning_capture_writer import actor, uow
    from okto_pulse.core.application.use_cases.learning_capture import StageLearningCaptureUseCase
    async with factory() as session:
        capture = await StageLearningCaptureUseCase().execute(replace(request, capture_id='history'),
            actor=actor(), uow=uow(session))
        for revision in (-1, True, '0'):
            with pytest.raises((ValueError, TypeError)):
                await store.read_revision_in_context(session, board_id=BOARD,
                    node_id=capture.node_id, generation=0, source_revision=revision)
