"""Historical records are captured literally, including unknown/retired sources."""

import pytest

from okto_pulse.core.kg.logical_transfer import encode_value
from okto_pulse.community.adapters import retirement_historical_graph_census as census
import test_joint_recovery_snapshot as recovery

sources = recovery.sources


def test_census_preserves_timestamps_multiplicity_and_retired_source_without_approval(sources, monkeypatch):
    global_graph = sources[1][1].database
    with global_graph.begin('write') as writer:
        for _ in range(2):
            writer.execute("MATCH (n:Topic {id:'baseline'}) "
                "CREATE (n)-[:TOPIC_RELATES_TO {weight:0.5}]->(n)")
    snapshot = recovery.capture(sources)
    document, digest = census.read_retirement_historical_graph_census(snapshot)
    assert (document, digest) == census.read_retirement_historical_graph_census(snapshot)
    assert document['state'] == 'captured_not_classified'
    assert document['snapshot_sha256'] == snapshot.manifest_sha256
    board, global_scope = document['graphs']
    assert (board['scope'], board['board_id']) == ('board', 'board-one')
    assert global_scope['scope'] == 'global_discovery' and global_scope['board_id'] is None
    node = board['nodes'][0]
    original = sources[4][0].nodes[0]
    assert node['fields']['source_artifact_ref'] == encode_value('sprint:opaque:v1')
    assert node['fields']['source_created_at'] == encode_value(original.properties['source_created_at'])
    assert node['fields']['created_at'] == encode_value(original.properties['created_at'])
    assert len(node['sha256']) == 64
    assert len(global_scope['relations']) == 1 and global_scope['relations'][0]['count'] == 2
    assert global_scope['counts']['relations'] == 2
    with monkeypatch.context() as scoped:
        scoped.setattr(census, '_MAX_NODES', 0)
        with pytest.raises(ValueError, match='historical_census_limit'):
            census.read_retirement_historical_graph_census(snapshot)
    assert census.read_retirement_historical_graph_census(snapshot) == (document, digest)
    artifact = snapshot.directory / 'graph-0000.jsonl'
    artifact.write_bytes(artifact.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='joint_snapshot_'):
        census.read_retirement_historical_graph_census(snapshot)
