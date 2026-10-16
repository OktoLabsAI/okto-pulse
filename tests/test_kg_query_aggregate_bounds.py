"""KG6.5: one result row may still contain an unbounded collection."""
from types import SimpleNamespace
import json

import pytest

from okto_grafx import connect
from okto_pulse.core.kg.interfaces import registry
from okto_pulse.core.kg.interfaces.graph_errors import GraphError
from okto_pulse.core.kg.tier_power import execute_cypher_read_only
from okto_pulse.community.adapters.grafx_cypher_executor import CommunityGrafxCypherExecutor
from okto_pulse.community.adapters.grafx_error_mapping import map_grafx_error


def test_native_collect_is_bounded_independently_of_result_rows(tmp_path, monkeypatch):
    with connect(tmp_path / 'collect') as database:
        with database.begin('write') as writer:
            writer.execute('CREATE NODE TABLE Item(id STRING, PRIMARY KEY(id))')
            for start in range(0, 10001, 500):
                writer.execute('UNWIND $ids AS id CREATE (:Item {id: id})',
                               {'ids': [f'item-{index}' for index in range(start, min(start + 500, 10001))]})
        executor = CommunityGrafxCypherExecutor(lambda board: database)
        monkeypatch.setattr(registry, 'get_kg_registry', lambda: SimpleNamespace(cypher_executor=executor))
        try:
            execute_cypher_read_only(
                'b', 'MATCH (n:Item) RETURN collect(n.id) AS items',
                max_rows=1, include_working=True,
            )
        except GraphError as failure:
            chain = []
            cause = failure
            while cause is not None:
                chain.append((type(cause).__name__, getattr(cause, 'details', {})))
                cause = cause.__cause__
            assert failure.code == 'graph_query_resource_limit', chain
            assert failure.details['limit'] == 1024
            assert failure.details['resource'] == 'result_value'
        else:
            raise AssertionError('An oversized collection must report its limit')


@pytest.mark.parametrize('field,resource', [
    ('max_result_rows', 'result_rows'), ('max_intermediate_rows', 'intermediate_rows'),
    ('max_traversal_expansions', 'traversal_expansions'), ('max_traversal_paths', 'traversal_paths'),
    ('query_memory_budget_bytes', 'query_memory_bytes'),
])
def test_typed_native_budget_is_explicit_and_not_retryable(field, resource):
    from okto_grafx.errors import GrafxQueryBudgetExceeded
    from okto_pulse.community.api.kg_routes import _graph_problem

    failure = map_grafx_error(GrafxQueryBudgetExceeded('bounded', field=field, limit=20, observed=21),
                             operation='read_only_query')
    assert failure.code == 'graph_query_resource_limit' and not failure.retryable
    assert failure.details == dict(resource=resource, limit=20, observed=21)
    response = _graph_problem(failure)
    assert response.status_code == 413
    assert 'Retry-After' not in response.headers
    assert json.loads(response.body)['resource_limit'] == failure.details


@pytest.mark.parametrize('details', [dict(field='query.result.rows[0][0]', value='malformed'),
    dict(field='query.result.rows[0][0]', limit=1024, value=1),
    dict(field='parameters.secret', limit=1024, value=1025)])
def test_non_budget_plan_errors_keep_original_classification(details):
    from okto_grafx.errors import GrafxConfigurationError, GrafxPlanError

    try:
        try:
            raise GrafxConfigurationError('original', **details)
        except GrafxConfigurationError as cause:
            raise GrafxPlanError('invalid result') from cause
    except GrafxPlanError as failure:
        mapped = map_grafx_error(failure, operation='read_only_query')
    assert mapped.code == 'graph_invalid_query'
    assert 'limit' not in mapped.details


@pytest.mark.parametrize("count", [2, 100], ids=["within_payload", "single_row_exceeds_payload"])
def test_native_single_row_collection_also_obeys_serialized_payload_budget(tmp_path, monkeypatch, count):
    from okto_pulse.core.kg.interfaces.graph_errors import GraphQueryResourceLimit

    # Each scalar and the 100-element array fit native value limits. The large
    # case still exceeds Pulse's response budget in just one result row.
    cell = "x" * 50000
    query = "UNWIND range(1, $count) AS i RETURN collect($cell) AS items"
    with connect(tmp_path / "payload") as database:
        native = database.execute(query, {"count": count, "cell": cell})
        assert len(native.rows) == 1 and len(native.rows[0][0]) == count
        executor = CommunityGrafxCypherExecutor(lambda board: database)
        monkeypatch.setattr(registry, "get_kg_registry", lambda: SimpleNamespace(cypher_executor=executor))
        if count == 2:
            result = execute_cypher_read_only("b", query, {"count": count, "cell": cell},
                max_rows=1, include_working=True)
            assert result["rows"] == [[(cell, cell)]]
            assert result["row_count"] == 1
        else:
            with pytest.raises(GraphQueryResourceLimit) as refused:
                execute_cypher_read_only("b", query, {"count": count, "cell": cell},
                    max_rows=1, include_working=True)
            assert refused.value.details["resource"] == "serialized_payload_bytes"
            assert refused.value.details["limit"] == 4 * 1024 * 1024
            assert refused.value.details["observed"] > refused.value.details["limit"]
            from okto_pulse.community.api.kg_routes import _graph_problem
            response = _graph_problem(refused.value)
            assert response.status_code == 413
            assert json.loads(response.body)["resource_limit"] == refused.value.details
            assert cell not in response.body.decode()
