"""G6: a normative link between Decisions is distinct from revision generations."""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from okto_pulse.community.adapters.board_source_reader import CommunityBoardSourceReader
from okto_pulse.community.adapters.sqlalchemy_kg_cognitive_source import CommunitySqlAlchemyCognitiveSourceStore
from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source
from okto_pulse.community.adapters.board_rebuild_ingestion import CommunityBoardRebuildIngestionAdapter
from okto_pulse.community.adapters.rebuild_effects import CommunityRebuildEffects
from okto_pulse.community.adapters.rebuild_audit_storage import CommunityFileSystemRebuildAuditArtifactStore
from test_f06_community_rebuild_effects import _command
from okto_pulse.core.kg.rebuild_sources import RebuildSourceEnumerator, cognitive_durable_digest_from_rows
from okto_pulse.core.ports.consolidation import ConsolidationClaimScope
from okto_pulse.core.ports.offline_kg_recovery import issue_offline_recovery_capability, reserve_offline_consolidation
from okto_pulse.community.adapters.coordination import build_root_bound_community_write_lock_port
from okto_pulse.community.adapters.recovery_runtime_fence import offline_recovery_window

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import ConsolidationQueue, Spec
from okto_pulse.community.adapters.grafx_relationship_layout import resolve_relationship_table
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.kg.kg_service import KGService
from test_projection_materialized_parity import materialize, relationship_set


@pytest.mark.asyncio
@pytest.mark.timeout(480)
@pytest.mark.parametrize("final_link", [True, False], ids=["linked", "removed"])
async def test_explicit_decision_successor_projects_normative_history(tmp_path, final_link):
    async def exercise(factory, graph):
        predecessor_ref = "spec:spec:decision:dec_one"
        successor_ref = "spec:spec:decision:dec_two"
        async with factory() as session:
            spec = await session.get(Spec, "spec")
            previous = {**spec.decisions[0], "status": "superseded"}
            successor = {
                "id": "dec_two", "title": "Replacement decision",
                "rationale": "Explicit replacement of the earlier choice",
                "status": "active", "supersedes_decision_id": "dec_one",
                "linked_requirements": ["fr_two"],
            }
            # Already-authorized source mutation; admission is covered separately.
            spec.decisions = [previous, successor]
            session.add(ConsolidationQueue(
                id="explicit-decision-replacement", board_id="board",
                artifact_type="spec", artifact_id="spec", source="state_transition",
            ))
            await session.commit()
        assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
        rows = graph.execute(
            "MATCH (n:Decision) WHERE n.source_artifact_ref IN [$old, $new] "
            "RETURN n.id, n.source_artifact_ref, n.content, n.superseded_by, n.source_status",
            {"old": predecessor_ref, "new": successor_ref},
        ).rows
        by_ref = {row[1]: row for row in rows}
        assert set(by_ref) == {predecessor_ref, successor_ref}, rows
        old, new = by_ref[predecessor_ref], by_ref[successor_ref]
        assert old[2] == "Choice", rows
        edges = graph.execute(
            f"MATCH (a:Decision)-[r:{resolve_relationship_table('supersedes', 'Decision', 'Decision')}]->(b:Decision) "
            "WHERE a.id = $new AND b.id = $old RETURN r.rule_id, r.layer",
            {"old": old[0], "new": new[0]},
        ).rows
        assert len(edges) == 1, {"decisions": rows, "supersedes": edges}
        assert edges[0][1] == "deterministic", edges
        assert (old[4], new[4]) == ('superseded', 'active'), rows
        # These are distinct normative IDs, not generations of one identity.
        assert old[3] is None and new[3] is None, rows
        service = KGService()
        assert service.get_decision_history('board', 'Choice', use_semantic=False) == []
        chain = service.get_supersedence_chain('board', new[0])
        assert old[0] in {item['id'] for item in chain['chain']}, chain
        # Replacing a normative link retracts only this owner's fact. It never
        # deletes either Decision or rewrites its authored content/generation.
        for index, linked in enumerate([True, False, False, True, True, final_link]):
            async with factory() as session:
                spec = await session.get(Spec, 'spec')
                current = dict(spec.decisions[1])
                current['supersedes_decision_id'] = 'dec_one' if linked else None
                spec.decisions = [dict(spec.decisions[0]), current]
                session.add(ConsolidationQueue(
                    id=f'explicit-decision-replay-{index}', board_id='board',
                    artifact_type='spec', artifact_id='spec', source='state_transition'))
                await session.commit()
            assert await ConsolidationProcessor(relational_scope_factory=factory).process_batch() == 1
            actual = graph.execute(
                f"MATCH (a:Decision)-[r:{resolve_relationship_table('supersedes', 'Decision', 'Decision')}]->(b:Decision) "
                "WHERE r.rule_id = $rule RETURN a.id, b.id",
                {'rule': 'supersedes/explicit_decision@v2.1'},
            ).rows
            assert set(map(tuple, actual)) == ({(new[0], old[0])} if linked else set())
            assert len(actual) == int(linked)
            preserved = graph.execute(
                'MATCH (n:Decision) WHERE n.id = $id RETURN n.content, n.source_status',
                {'id': old[0]},
            ).rows
            assert tuple(preserved[0]) == ('Choice', 'superseded')

        def decision_rows():
            return {row[0]: tuple(row[1:]) for row in graph.execute(
                "MATCH (n:Decision) RETURN n.id, n.source_artifact_ref, n.content, "
                "n.generation, n.superseded_by, n.source_status, n.graph_layer, n.maturity_status"
            ).rows}

        # Rebuild from the same complete relational and durable cognitive sources.
        # Native history is part of the contract even when its owner has a newer edition.
        async with factory() as session:
            path = session.bind.url.database
        sources = CommunityBoardSourceReader(path).fetch("board")
        assert sources.complete
        store = CommunitySqlAlchemyCognitiveSourceStore(factory)
        history = await store.enumerate("board")
        before_decisions = decision_rows()
        before_edges = relationship_set(graph)
        artifacts = CommunityFileSystemRebuildAuditArtifactStore(tmp_path / "effects")
        owner = CommunityBoardRebuildIngestionAdapter(db_path=path, artifact_store=artifacts)
        effects = CommunityRebuildEffects(owner, artifact_store=artifacts)
        command = replace(_command(), board_id="board")
        snapshot = effects.snapshot(command, effect_key=f"{command.run_id}:snapshot")
        assert snapshot.ok and snapshot.details["readable"]
        reader = make_grafx_logical_source(graph, scope="board").open_snapshot()
        try:
            node_types = {node.type_name for batch in reader.iter_nodes(batch_size=500) for node in batch}
        finally:
            reader.close()
        with graph.begin("write") as writer:
            for node_type in sorted(node_types):
                writer.execute(f"MATCH (n:{node_type}) DETACH DELETE n")
        assert decision_rows() == {}
        captured_at = datetime.now(timezone.utc)
        cognitive = await store.enumerate_latest_verified("board")
        source_set = RebuildSourceEnumerator(
            source_store=lambda _board: list(sources.rows), now=captured_at,
            cognitive_digest_provider=lambda _board: cognitive_durable_digest_from_rows(cognitive),
        ).enumerate(board_id="board")
        rebuild_rows = [{**row.to_dict(), "_rebuild_manifest_created_at": captured_at.isoformat()}
                        for row in source_set.materializable_sources]
        scope = ConsolidationClaimScope(board_id="board", source="rebuild:decision-history",
            reservation_lineage_id=hashlib.sha256(json.dumps(rebuild_rows, sort_keys=True).encode()).hexdigest())
        root = tmp_path / "explicit-decisions"
        with offline_recovery_window((root, root / "kg")):
            with issue_offline_recovery_capability(board_id="board", lifetime_probe=lambda: True) as capability:
                with reserve_offline_consolidation(claim_scope=scope, recovery_capability=capability,
                        write_lock_port=build_root_bound_community_write_lock_port(root / "kg"),
                        relational_scope_factory=factory, owner_id="decision-history") as reserved:
                    counts = owner.enqueue_sources(board_id="board", run_id="decision-history",
                        sources=rebuild_rows)
                    assert counts["inserted"] == 1
                    outcome = await reserved.process_next()
                    assert outcome.acked_count == 1, outcome
                    # Exact queue replay must reuse the recovered identity,
                    # including when it now exists in the graph.
                    replay_history = await store.enumerate("board")
                    owner.enqueue_sources(board_id="board", run_id="decision-history", sources=rebuild_rows)
                    replay = await reserved.process_next()
                    assert replay.acked_count == 1, replay
                    assert await store.enumerate("board") == replay_history
        restored = effects.restore(command, effect_key=f"{command.run_id}:restore")
        assert restored.ok, restored
        assert CommunityBoardSourceReader(path).fetch("board").rows == sources.rows
        assert await store.enumerate("board") == history
        assert decision_rows() == before_decisions
        assert relationship_set(graph) == before_edges
        assert effects.restore(command, effect_key=f"{command.run_id}:restore") == restored

    await materialize(tmp_path / "explicit-decisions", incremental=False,
                      exercise=exercise, native_schema=True)
