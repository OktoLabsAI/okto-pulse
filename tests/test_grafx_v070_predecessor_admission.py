"""A new physical pair does not authorize an in-place historical upgrade."""

import pytest
from okto_grafx import Timestamp, connect
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.core.kg.logical_transfer import LogicalNode, LogicalTimestamp, schema_digest, transfer_logical_graph
from okto_pulse.community.adapters.grafx_recovery_contracts import (
    v060_recovery_contract, make_grafx_recovery_logical_sink, make_grafx_recovery_logical_source,
)
from okto_pulse.community.adapters.grafx_schema_bootstrap import ensure_current_grafx_board_schema
from okto_pulse.community.adapters.grafx_relationship_layout import resolve_relationship_table
from logical_transfer_matrix_support import Corpus, MaterializedSource, complete_node


def test_bootstrap_refuses_v060_without_changing_its_metadata_nodes_or_catalog(tmp_path):
    schema = v060_recovery_contract().schema
    node = complete_node(schema, 'Decision', 'retained', 1)
    metadata = LogicalNode('BoardMeta', 'board', {'board_id': 'board', 'schema_version': '0.6.0',
        'bootstrapped_at': LogicalTimestamp(0), 'embedding_model': 'retained', 'embedding_dimension': 384})
    corpus = Corpus(schema, (node, metadata), ())
    path = tmp_path / 'old'
    transfer_logical_graph(MaterializedSource(corpus), make_grafx_recovery_logical_sink(
        path, scope='board', expected_schema_digest=schema_digest(schema)))
    with connect(path, page_size=8192) as database:
        before = tuple((table.kind, table.name) for table in database.catalog.catalog.tables())
        fences = []
        with pytest.raises(GraphError) as raised:
            ensure_current_grafx_board_schema(database, board_id='board',
                bootstrapped_at=Timestamp(micros=1), revalidate_fence=fences.append)
        assert raised.value.details['reason'] == 'versioned_partial_schema'
        assert fences == []
        assert tuple((table.kind, table.name) for table in database.catalog.catalog.tables()) == before
        source = make_grafx_recovery_logical_source(database, scope='board').open_snapshot()
        try:
            actual = {row.type_name: row for batch in source.iter_nodes(batch_size=10) for row in batch}
            assert actual == {'Decision': node, 'BoardMeta': metadata}
        finally:
            source.close()


def test_v070_physical_requirement_pairs_accept_exact_typed_edges(tmp_path):
    # Physical capability only; worker emission and policy admission have their
    # own tests and are not inferred from these direct disposable writes.
    with connect(tmp_path / 'new', page_size=8192) as database:
        ensure_current_grafx_board_schema(database, board_id='board', bootstrapped_at=Timestamp(micros=1))
        with database.begin('write') as transaction:
            transaction.execute("CREATE (:Requirement {id: 'ir'})")
            transaction.execute("CREATE (:Constraint {id: 'or'})")
            transaction.execute("CREATE (:Constraint {id: 'tr'})")
            for kind, identity in [('Requirement', 'ir'), ('Constraint', 'or')]:
                table = resolve_relationship_table('derives_from', kind, 'Constraint')
                transaction.execute(f"MATCH (a:{kind}), (b:Constraint) WHERE a.id=$source AND b.id='tr' "
                    f"CREATE (a)-[:{table}]->(b)", {'source': identity})
        for kind, identity in [('Requirement', 'ir'), ('Constraint', 'or')]:
            table = resolve_relationship_table('derives_from', kind, 'Constraint')
            assert database.execute(f'MATCH (a:{kind})-[r:{table}]->(b:Constraint) RETURN a.id,b.id').rows == ((identity, 'tr'),)
