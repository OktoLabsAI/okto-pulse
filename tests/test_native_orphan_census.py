"""KG-25: census observes the complete native projection without repairing it."""
import asyncio

import pytest

from okto_pulse.core.application.processors.deterministic_kg import DeterministicWorker
from okto_pulse.core.kg.logical_transfer import canonical_bytes, encode_value
from okto_pulse.core.kg.orphan_integrity import OrphanNodeScanner, build_orphan_integrity_projection
from okto_pulse.core.ports.projection_connectivity import observe_projection_connectivity
from okto_pulse.community.adapters.logical_transfer_factories import make_grafx_logical_source
from test_projection_materialized_parity import materialize, source


def snapshot(graph):
    reader = make_grafx_logical_source(graph, scope="board").open_snapshot()
    try:
        nodes = tuple(node for batch in reader.iter_nodes(batch_size=500) for node in batch)
        edges = tuple(edge for batch in reader.iter_relations(batch_size=500) for edge in batch)
        content = sorted((node.type_name, node.key, canonical_bytes({
            key: encode_value(value) for key, value in node.properties.items()})) for node in nodes)
        relations = sorted((edge.layout_name, edge.source_type, edge.source_key,
            edge.target_type, edge.target_key, canonical_bytes({
                key: encode_value(value) for key, value in edge.properties.items()})) for edge in edges)
        return nodes, (content, relations)
    finally:
        reader.close()


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_native_census_preserves_all_source_content_and_relationships(tmp_path):
    async def exercise(factory, graph):
        nodes, before = snapshot(graph)
        expected = DeterministicWorker().process_spec({
            "id": "spec", "board_id": "board", "status": "done",
            "title": "Spec", **source(["ac_two"])})
        indexed = {(node.type_name, node.properties.get("source_artifact_ref")): node
            for node in nodes}
        assert expected.nodes
        for emitted in expected.nodes:
            observed = indexed[(emitted.node_type, emitted.source_artifact_ref)]
            assert observed.properties.get("title") == emitted.title
            assert observed.properties.get("content") == emitted.content
        report = await asyncio.to_thread(
            lambda: OrphanNodeScanner().scan(board_id="board", connection=graph))
        assert report.orphan_count == 0
        assert build_orphan_integrity_projection(report).zero_orphan_validation == "passed"
        assert snapshot(graph)[1] == before

        # Deliberately corrupt a synthetic fixture. A census must report this
        # evidence, never delete it or invent an anchor to make counts green.
        with graph.begin("write") as transaction:
            transaction.execute(
                "CREATE (n:Learning {id: $id, title: $title, content: $content, "
                "source_artifact_ref: $ref, created_by_agent: $actor, "
                "graph_layer: $layer, maturity_status: $maturity})",
                {"id": "isolated-learning", "title": "Retained observation",
                 "content": "This content must survive failed validation.",
                 "ref": "spec:spec", "actor": "agent:cognitive",
                 "layer": "canonical", "maturity": "canonical_eligible"})
        _, damaged = snapshot(graph)
        report = await asyncio.to_thread(
            lambda: OrphanNodeScanner().scan(board_id="board", connection=graph))
        assert report.orphan_count == 1
        assert report.orphan_count_by_type == {"Learning": 1}
        projection = build_orphan_integrity_projection(report)
        assert projection.zero_orphan_validation == "failed_orphan_validation"
        assert projection.integrity_warning and projection.classification_delta == "at_risk"
        reader = make_grafx_logical_source(graph, scope="board").open_snapshot()
        try:
            observation, = observe_projection_connectivity(
                schema=reader.schema(), board_id="board",
                nodes=tuple(node for batch in reader.iter_nodes(batch_size=500) for node in batch),
                relations=tuple(edge for batch in reader.iter_relations(batch_size=500) for edge in batch),
                selected=(("Learning", "isolated-learning"),))
            assert observation.outcome == "rejected"
        finally:
            reader.close()
        assert snapshot(graph)[1] == damaged

    await materialize(tmp_path / "native-census", incremental=False,
        exercise=exercise, native_schema=True)
