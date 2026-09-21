"""Characterize exact consolidation on a preserved private native generation.

The fixture composes the actual queue/worker/graph path without a normal server,
scheduler or route purge. Its synthetic reservation is NOT an installer receipt.
"""

from dataclasses import asdict, replace
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest
from sqlalchemy import insert, select, func

from okto_pulse.community.adapters import sqlalchemy_database as db
from okto_pulse.community.adapters.board_rebuild_ingestion import CommunityBoardRebuildIngestionAdapter
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.composition import (
    configure_community_kg_registry, require_community_routed_graph_composition,
)
from okto_pulse.community.adapters.coordination import (
    register_community_coordination_providers, build_root_bound_community_write_lock_port,
)
from okto_pulse.community.adapters.graph_backend_binding import CommunityGraphBackendBindingStore
from okto_pulse.community.adapters.migration_runtime_fence import offline_migration_window
from okto_pulse.community.adapters.relational_effects import register_community_relational_effects
from okto_pulse.community.adapters.sqlalchemy_consolidation import CommunitySqlAlchemyConsolidationPersistence
from okto_pulse.community.adapters.sqlalchemy_models import Base, Board, Spec, Ideation, Refinement, ConsolidationQueue
from okto_pulse.community.config import CommunitySettings
from okto_pulse.core import configure_settings
from okto_pulse.core.ports.coordination import register_coordination_providers
from okto_pulse.core.ports.consolidation import ConsolidationClaimScope, get_consolidation_persistence_port
from okto_pulse.core.ports.deterministic_projection import make_deterministic_projection_planner
from okto_pulse.core.ports.offline_kg_recovery import issue_offline_recovery_capability, reserve_offline_consolidation
from okto_pulse.core.services.application_kg import drain_kg_health_probes
from logical_transfer_matrix_support import one_node_corpus, seed_generation, complete_node, complete_relation
from test_joint_recovery_native_history import history


def sql_tables(path):
    with sqlite3.connect(path) as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")]
        return {name: sorted(repr(row) for row in connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"'))
            for name in names}


@pytest.mark.asyncio
@pytest.mark.timeout(240)
@pytest.mark.parametrize('terminal_cleanup,lose_after_ack', [(False, False), (True, False), (False, True)])
async def test_exact_projection_commits_without_purging_native_history(tmp_path, monkeypatch, terminal_cleanup, lose_after_ack):
    from okto_grafx import connect
    settings = CommunitySettings(database_url=f'sqlite+aiosqlite:///{tmp_path / "candidate.sqlite3"}',
        data_dir=str(tmp_path), kg_base_dir=str(tmp_path / 'kg'), kg_embedding_mode='stub', kg_embedding_dim=384)
    configure_settings(settings)
    runtime = db.configure_community_database(settings.database_url)
    factory = runtime.session_factory
    register_community_coordination_providers()
    write_port = build_root_bound_community_write_lock_port(tmp_path / 'kg')
    register_coordination_providers(write_lock_port=write_port)
    register_community_relational_effects(settings=settings)
    bindings = CommunityGraphBackendBindingStore(tmp_path / 'kg')
    physical = bindings.board_grafx_path('board', 'private-candidate')
    physical.parent.mkdir(parents=True)
    corpus = one_node_corpus('board', key='retained-history')
    if terminal_cleanup:
        stale = one_node_corpus('board', key='stale-rdl').nodes[0]
        stale = replace(stale, properties={**stale.properties,
            'source_artifact_ref': 'refinement:refinement:rdl:ledger:decision',
            'created_by_agent': 'system:historical_consolidation', 'human_curated': False,
            'graph_layer': 'semantic', 'maturity_status': 'canonical',
            'revocation_reason': '', 'superseded_by': ''})
        unowned = replace(stale, key='unowned-rdl', properties={**stale.properties, 'id': 'unowned-rdl',
            'source_artifact_ref': 'refinement:refinement:rdl:unowned:decision'})
        owner = complete_node(corpus.schema, 'Entity', 'refinement-root', 11)
        owner = replace(owner, properties={**owner.properties, 'source_artifact_ref': 'refinement:refinement'})
        edge = complete_relation(corpus.schema, corpus.schema.relation_layout('belongs_to', 'Decision', 'Entity'),
            'stale-rdl', 'refinement-root', 17)
        edge = replace(edge, properties={**edge.properties, 'rule_id': 'belongs_to/relational_rdl_decision@v2.0'})
        corpus = replace(corpus, nodes=corpus.nodes + (stale, unowned, owner), relations=(edge,))
    seed_generation('grafx', physical, corpus)
    with connect(physical, page_size=8192) as graph:
        binding = bindings.initialize_board_binding(board_id='board', backend='grafx', generation='private-candidate',
            physical_path=physical, page_size=8192, database=graph)
        reader = history(graph)
        reader.activate('board', ('Decision',), (), reason='disposable exact projection fixture')
        with graph.begin('write') as writer:
            writer.execute("MATCH (n:Decision {id: 'retained-history'}) SET n.title='Earlier title'")
        cursor = reader.commits('board')['entries'][-1]['commit']
        past = reader.as_of('board', cursor, ('Decision',), ())
        identity = graph.identity.database_uuid
    configure_community_kg_registry(factory, settings=settings)
    bundle = require_community_routed_graph_composition()
    try:
        async with runtime.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(insert(Board).values(id='board', name='Private Board', owner_id='owner', realm_id='local'))
            await connection.execute(insert(Spec).values(id='spec', board_id='board', title='Source Spec',
                status='done', created_by='owner', functional_requirements=[{'id': 'fr-one', 'title': 'Required behavior'}]))
            if terminal_cleanup:
                await connection.execute(insert(Ideation).values(id='idea', board_id='board', title='Archived context',
                    status='cancelled', created_by='owner'))
                await connection.execute(insert(Refinement).values(id='refinement', board_id='board', ideation_id='idea',
                    title='Terminal refinement', status='cancelled', created_by='owner'))
        snapshot = CommunityBoardSourceReader(tmp_path / 'candidate.sqlite3').fetch('board')
        assert snapshot.complete
        planner = make_deterministic_projection_planner(CommunitySqlAlchemyConsolidationPersistence())
        async with factory() as session:
            expected = await planner.prepare_board(session, board_id='board', source_rows=tuple(snapshot.rows),
                cognitive_rows=(), captured_at=datetime.now(timezone.utc))
        run_id = 'retirement-exact-fixture'
        ingestion = CommunityBoardRebuildIngestionAdapter(db_path=tmp_path / 'candidate.sqlite3')
        before = sql_tables(tmp_path / 'candidate.sqlite3')
        sources = tuple(row for row in snapshot.rows if row['artifact_type'] in {'spec', 'refinement'})
        scope = ConsolidationClaimScope(board_id='board', source=f'rebuild:{run_id}',
            reservation_lineage_id=hashlib.sha256(expected).hexdigest())
        outcomes = []
        offline_live = [False]
        if lose_after_ack:
            store = get_consolidation_persistence_port()
            commit, ack = store.commit, store.ack_exact_rebuild_commit
            pending_ack = [False]
            async def observed_ack(*args, **kwargs):
                receipt = await ack(*args, **kwargs)
                pending_ack[0] = receipt is not None
                return receipt
            async def lose_after_commit(*args, **kwargs):
                await commit(*args, **kwargs)
                if pending_ack[0]:
                    offline_live[0] = False
            monkeypatch.setattr(store, 'ack_exact_rebuild_commit', observed_ack)
            monkeypatch.setattr(store, 'commit', lose_after_commit)
        expected_exit = pytest.raises(RuntimeError, match='authority_lost') if lose_after_ack else nullcontext()
        with expected_exit, offline_migration_window((tmp_path, tmp_path / 'kg')):
            offline_live[0] = True
            try:
                with issue_offline_recovery_capability(board_id='board', lifetime_probe=lambda: offline_live[0]) as capability:
                    with reserve_offline_consolidation(claim_scope=scope, recovery_capability=capability,
                            write_lock_port=write_port, relational_scope_factory=factory, owner_id='fixture-installer') as reserved:
                        assert reserved.is_authorized()
                        ingestion.enqueue_sources(board_id='board', run_id=run_id, sources=sources)
                        for _ in sources:
                            result = await reserved.process_next()
                            outcomes.append(asdict(result))
                            assert result.acked_count == 1, json.dumps(asdict(result), default=str)
            finally:
                offline_live[0] = False
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(ConsolidationQueue)) == 0
        graph = bundle.grafx_pool.get(physical, page_size=8192)
        assert graph.identity.database_uuid == identity
        assert bindings.inspect_board_binding('board') == binding
        assert history(graph).as_of('board', cursor, ('Decision',), ()) == past
        assert tuple(map(tuple, graph.execute("MATCH (n:Entity {source_artifact_ref: 'spec:spec'}) RETURN n.title").rows)) == (('Source Spec',),)
        after = sql_tables(tmp_path / 'candidate.sqlite3')
        changed = sorted(name for name in before if before[name] != after[name])
        assert changed == ['app_settings', 'consolidation_audit', 'domain_events',
            'exact_rebuild_consolidation_ack_journal', 'global_update_outbox', 'kuzu_node_refs']
        if terminal_cleanup:
            rows = graph.execute("MATCH (n:Decision {id: 'stale-rdl'}) RETURN n.revocation_reason").rows
            assert tuple(map(tuple, rows)) == (('source_projection_removed',),)
            unowned_rows = graph.execute("MATCH (n:Decision {id: 'unowned-rdl'}) RETURN n.revocation_reason").rows
            assert tuple(map(tuple, unowned_rows)) == (('',),)
        with pytest.raises(RuntimeError, match='authority_lost'):
            await reserved.process_next()
        assert sql_tables(tmp_path / 'candidate.sqlite3') == after
        (tmp_path / 'exact-observations.json').write_text(json.dumps({'changed_tables': changed,
            'outcomes': outcomes}, default=str, indent=2), encoding='utf-8')
    finally:
        drain_kg_health_probes()
        bundle.grafx_pool.close_all()
        for pool in bundle.board.grafx_read_pools:
            pool.close_all()
        await runtime.close()
