"""KG-11: native Spec revision preserves Decision generations through replay.

Source lifecycle states are fixtures; this does not change lifecycle admission.
"""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json

import pytest

from okto_pulse.community.adapters.sqlalchemy_models import Spec, ConsolidationQueue
from okto_pulse.core.application.processors.consolidation import ConsolidationProcessor
from okto_pulse.core.kg.kg_service import KGService
from test_projection_materialized_parity import materialize, relationship_set
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


@pytest.mark.asyncio
@pytest.mark.timeout(480)
@pytest.mark.parametrize("pending_change", [False, True], ids=["unchanged", "pending-source"])
async def test_native_decision_revision_keeps_predecessor_and_idempotent_chain(tmp_path, pending_change):
    async def exercise(factory, graph):
        def rows():
            return {r[0]: tuple(r[1:]) for r in graph.execute(
                "MATCH (n:Decision) WHERE n.source_artifact_ref = $ref "
                "RETURN n.id,n.content,n.superseded_by,n.generation,n.graph_layer",
                {"ref": "spec:spec:decision:dec_one"},
            ).rows}

        original = rows()
        assert len(original) == 1
        predecessor_id, predecessor = next(iter(original.items()))
        successor_id = None
        for index, status in enumerate(["draft", "done"]):
            async with factory() as session:
                spec = await session.get(Spec, "spec")
                spec.status = status
                spec.edition = 2
                spec.decisions = [{**spec.decisions[0], "rationale": "Revised native decision"}]
                session.add(ConsolidationQueue(
                    id=f"decision-revision-{index}", board_id="board", artifact_type="spec",
                    artifact_id="spec", source="state_transition",
                ))
                await session.commit()
            processor = ConsolidationProcessor(relational_scope_factory=factory)
            assert await processor.process_batch() == 1
            current = rows()
            assert len(current) == 2
            active = [(key, value) for key, value in current.items() if value[1] is None]
            assert len(active) == 1
            identity, value = active[0]
            assert identity != predecessor_id
            if successor_id is not None:
                assert identity == successor_id
            successor_id = identity
            assert value[0] == "Revised native decision"
            assert value[2] == predecessor[2] + 1
            assert value[3] == ("working" if status == "draft" else "canonical")
            assert current[predecessor_id][0] == predecessor[0]
            assert current[predecessor_id][1] == successor_id
            chain = KGService().get_supersedence_chain("board", successor_id)
            assert chain["current_active"] == successor_id
            assert predecessor_id in {item["id"] for item in chain["chain"]}
            async with factory() as session:
                session.add(ConsolidationQueue(
                    id=f"decision-replay-{index}", board_id="board", artifact_type="spec",
                    artifact_id="spec", source="state_transition",
                ))
                await session.commit()
            assert await processor.process_batch() == 1
            assert rows() == current
            durable = await CommunitySqlAlchemyCognitiveSourceStore(factory).enumerate_latest_verified("board")
            prior = next(record for record in durable if record.node_id == predecessor_id)
            assert prior.payload["content"] == predecessor[0]
            assert prior.payload["superseded_by"] == successor_id

        if pending_change:
            async with factory() as session:
                spec = await session.get(Spec, "spec")
                spec.edition = 3
                spec.status = "draft"
                spec.decisions = [{**spec.decisions[0], "rationale": "Pending native decision"}]
                await session.commit()

        # Rebuild from the same complete relational and durable cognitive sources.
        # Native history is part of the contract even when its owner has a newer edition.
        async with factory() as session:
            path = session.bind.url.database
        sources = CommunityBoardSourceReader(path).fetch("board")
        assert sources.complete
        store = CommunitySqlAlchemyCognitiveSourceStore(factory)
        history = await store.enumerate("board")
        before_decisions = rows()
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
        assert rows() == {}
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
        root = tmp_path / "decisions"
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
        after_history = await store.enumerate("board")
        if not pending_change:
            assert after_history == history
            assert rows() == before_decisions
            assert relationship_set(graph) == before_edges
        else:
            assert all(record in after_history for record in history)
            rebuilt = rows()
            assert len(rebuilt) == 3
            active = [(key, value) for key, value in rebuilt.items() if value[1] is None]
            assert len(active) == 1
            latest_id, latest = active[0]
            assert latest == ("Pending native decision", None, 2, "working")
            assert rebuilt[predecessor_id] == before_decisions[predecessor_id]
            assert rebuilt[successor_id][0] == before_decisions[successor_id][0]
            assert rebuilt[successor_id][1] == latest_id
            latest_sources = await store.enumerate_latest_verified("board")
            previous = next(item for item in latest_sources if item.node_id == successor_id)
            assert previous.payload["superseded_by"] == latest_id
            assert previous.payload["content"] == "Revised native decision"
        assert effects.restore(command, effect_key=f"{command.run_id}:restore") == restored

    await materialize(tmp_path / "decisions", incremental=False, exercise=exercise, native_schema=True)
