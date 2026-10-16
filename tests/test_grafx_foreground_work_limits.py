"""KG6.5: native work bounds constrain operators, independently of output rows."""
import pytest
from okto_grafx import connect
from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryResourceLimit
from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor


@pytest.mark.parametrize('option,resource,query', [
    ('max_intermediate_rows', 'intermediate_rows', 'MATCH (n:Item) RETURN count(n)'),
    ('max_traversal_expansions', 'traversal_expansions',
     "MATCH (a:Item {id: 'root'})-[:Link*1..2]->(b:Item) RETURN count(b)"),
    ('max_traversal_paths', 'traversal_paths',
     "MATCH (a:Item {id: 'root'})-[:Link*1..2]->(b:Item) RETURN count(b)"),
])
def test_native_operator_limit_is_not_bypassed_by_one_aggregate_row(tmp_path, option, resource, query):
    path = tmp_path / 'work-limit'
    # Populate through an unrestricted writer, then join a separately bounded
    # reader. These limits must never be installed on background rebuild reads.
    with connect(path) as writer_db:
        with writer_db.begin('write') as writer:
            writer.execute('CREATE NODE TABLE Item(id STRING, PRIMARY KEY(id))')
            writer.execute('CREATE REL TABLE Link(FROM Item TO Item)')
            for identity in ['root', 'a', 'b', 'c', 'd']:
                writer.execute('CREATE (:Item {id: $id})', {'id': identity})
            for identity in ['a', 'b', 'c', 'd']:
                writer.execute("MATCH (a:Item {id: 'root'}), (b:Item {id: $id}) CREATE (a)-[:Link]->(b)", {'id': identity})
        writer_db.checkpoint()
        with connect(path, read_only=True, **{option: 2}) as reader:
            executor = CommunityGrafxCypherExecutor(lambda board: reader)
            with pytest.raises(GraphQueryResourceLimit) as failure:
                executor.execute_read_only('b', query, max_rows=1)
            assert failure.value.details['resource'] == resource
            assert failure.value.details['limit'] == 2
        # The bounded refusal is read-only and does not taint the writer.
        assert writer_db.execute('MATCH (n:Item) RETURN count(n)').rows == ((5,),)


def test_native_query_working_memory_has_typed_refusal(tmp_path):
    with connect(tmp_path / 'memory-limit', query_memory_budget_bytes=64) as database:
        executor = CommunityGrafxCypherExecutor(lambda board: database)
        with pytest.raises(GraphQueryResourceLimit) as failure:
            executor.execute_read_only('b', 'UNWIND $values AS x RETURN collect(x)',
                                       {'values': [str(index) * 100 for index in range(10)]}, max_rows=1)
        assert failure.value.details['resource'] == 'query_memory_bytes'
        assert failure.value.details['limit'] == 64


def test_foreground_options_preserve_lower_configured_limits():
    from okto_pulse.community.adapters.grafx_foreground_query_limits import (
        FOREGROUND_QUERY_LIMITS, foreground_query_options,
    )

    options = foreground_query_options({'max_intermediate_rows': 7,
                                        'max_traversal_paths': 1000000,
                                        'max_result_rows': None, 'max_open_files': 100})
    assert options['max_intermediate_rows'] == 7
    assert options['max_traversal_paths'] == FOREGROUND_QUERY_LIMITS['max_traversal_paths']
    assert options['max_result_rows'] == FOREGROUND_QUERY_LIMITS['max_result_rows']
    assert options['max_open_files'] == 100
    assert all(type(options[key]) is int and 0 < options[key] <= value
               for key, value in FOREGROUND_QUERY_LIMITS.items())


@pytest.mark.asyncio
async def test_composed_scope_selects_bounded_readers_and_restores_background_scan(tmp_path, monkeypatch):
    from okto_pulse.community.config import CommunitySettings
    from okto_pulse.community.adapters import grafx_foreground_query_limits as limits
    from okto_pulse.community.adapters.routed_board_graph_composition import build_community_routed_board_graph_composition

    # A small test ceiling makes the native refusal deterministic without a
    # large fixture; the same production option composition is exercised.
    monkeypatch.setattr(limits, 'FOREGROUND_QUERY_LIMITS', dict(
        limits.FOREGROUND_QUERY_LIMITS, max_intermediate_rows=2))
    settings = CommunitySettings(_env_file=None, data_dir=str(tmp_path), kg_embedding_mode='stub')
    bundle = build_community_routed_board_graph_composition(settings=settings)
    board = 'foreground-budget-board'
    try:
        bundle.initialize_board_route(board)
        await bundle.graph_schema_manager.ensure_bootstrapped(board)
        query = 'UNWIND $values AS x RETURN count(x)'
        params = {'values': [1, 2, 3, 4, 5]}
        assert bundle.cypher_executor.execute_read_only(board, query, params, max_rows=1)['rows'] == [[5]]
        with pytest.raises(GraphQueryResourceLimit) as failure, bundle.graph_query_execution.scope(board, timeout_ms=15000):
            bundle.cypher_executor.execute_read_only(board, query, params, max_rows=1)
        assert failure.value.details['resource'] == 'intermediate_rows'
        assert failure.value.details['limit'] == 2
        assert bundle.cypher_executor.execute_read_only(board, query, params, max_rows=1)['rows'] == [[5]]
        assert any(pool.pooled_paths() for pool in bundle.grafx_query_pools)
    finally:
        await bundle.graph_lifecycle.close(None)
    assert all(not pool.pooled_paths() for pool in (*bundle.grafx_read_pools, *bundle.grafx_query_pools))
