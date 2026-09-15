"""Logical relationship reads must not silently become absent native type reads."""

import pytest
from okto_grafx import connect

from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
from okto_pulse.community.adapters.grafx_graph_transaction import CommunityGrafxGraphTransaction
from okto_pulse.community.adapters.grafx_relationship_layout import resolve_relationship_table


@pytest.mark.parametrize("codec", ["pure", "numpy"])
async def test_logical_relationship_without_endpoint_labels_reads_physical_member(tmp_path, codec):
    with connect(tmp_path / "db", codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Decision(id STRING,graph_layer STRING,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE depends_on__Decision__Decision(FROM Decision TO Decision)")
            tx.execute("CREATE(a:Decision {id:'a',graph_layer:'canonical'}),"
                       "(b:Decision {id:'b',graph_layer:'canonical'}),"
                       "(a)-[:depends_on__Decision__Decision]->(b)")
        executor = CommunityGrafxCypherExecutor(lambda board: db)
        typed = executor.execute_read_only("fixture", "MATCH(a:Decision)-[r:depends_on]->(b:Decision) RETURN count(*) AS total")
        assert typed["rows"] == [[1]]
        logical = executor.execute_read_only("fixture", "MATCH(a)-[r:depends_on]->(b) RETURN count(*) AS total")
        assert logical["rows"] == [[1]]
        provider = CommunityGrafxGraphTransaction(database_resolver=lambda board: db,
            revalidate_fence=lambda board, operation: None, node_types=("Decision",),
            relationship_table_resolver=resolve_relationship_table,
            relationship_pairs=(("depends_on", "Decision", "Decision"),))
        scope = await provider.begin("fixture")
        try:
            assert scope.execute("MATCH(a)-[r:depends_on]->(b) RETURN count(*) AS total").rows == ((1,),)
        finally:
            await scope.rollback()
