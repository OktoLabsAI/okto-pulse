from okto_grafx import connect
from okto_pulse.core.application.processors.global_outbox import GlobalOutboxProcessor
from okto_pulse.community.adapters.grafx_global_discovery import ensure_current_grafx_global_schema
from okto_pulse.community.adapters.grafx_global_discovery_runtime import CommunityGrafxGlobalDiscoveryRuntime


def test_global_visibility_larger_than_native_parameter_limit(tmp_path):
    with connect(tmp_path / 'global') as db:
        ensure_current_grafx_global_schema(db)
        runtime = CommunityGrafxGlobalDiscoveryRuntime(
            lambda: db, lambda: tmp_path / 'global', lambda: None, lambda phase: None,
        )
        with db.begin('write') as tx:
            tx.execute("CREATE (:DecisionDigest {id:'digest', board_id:'board', original_node_id:'node-0', source_revoked:true})")
            tx.execute("CREATE (:DecisionDigest {id:'other', board_id:'other-board', original_node_id:'node-0', source_revoked:true})")
            tx.execute("CREATE (:DecisionDigest {id:'active-null', board_id:'board', original_node_id:'node-1'})")
            tx.execute("CREATE (:DecisionDigest {id:'active-correct', board_id:'board', original_node_id:'node-2', source_revoked:false})")
            tx.execute("CREATE (:DecisionDigest {id:'revoked-wrong', board_id:'board', original_node_id:'node-3', source_revoked:false})")
            tx.execute("CREATE (:DecisionDigest {id:'revoked-null', board_id:'board', original_node_id:'node-4'})")
            tx.execute("CREATE (:DecisionDigest {id:'revoked-correct', board_id:'board', original_node_id:'node-5', source_revoked:true})")
        revoked = {'node-3', 'node-4', 'node-5'}
        active = {f'node-{i}' for i in range(2414)} - revoked
        count = GlobalOutboxProcessor._set_board_digest_source_visibility(
            runtime, 'board', active_source_ids=active,
            revoked_source_ids=revoked,
        )
        assert count == 4
        # Retry converges without rewriting already-correct graph records.
        assert GlobalOutboxProcessor._set_board_digest_source_visibility(
            runtime, 'board', active_source_ids=active,
            revoked_source_ids=revoked,
        ) == 0
        assert db.execute('MATCH (d:DecisionDigest) RETURN d.id,d.source_revoked ORDER BY d.id').rows == (
            ('active-correct', False), ('active-null', False),
            ('digest', False), ('other', True),
            ('revoked-correct', True), ('revoked-null', True), ('revoked-wrong', True),
        )
        assert db.verify('all').findings == ()
