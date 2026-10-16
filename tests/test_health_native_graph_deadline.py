"""Native query control must receive the Health observation deadline."""

from types import SimpleNamespace

import pytest

from okto_pulse.community.adapters.routed_board_graph_composition import (
    build_community_routed_board_graph_composition,
)
from okto_pulse.core.kg.interfaces.graph_errors import GraphError


@pytest.mark.parametrize("door", ["scalar", "pair", "batch"])
def test_composed_health_query_has_native_deadline(tmp_path, door):
    bundle = build_community_routed_board_graph_composition(settings=SimpleNamespace(
        kg_base_dir=str(tmp_path / "graph"), kg_graph_backend="grafx",
        kg_global_graph_backend="grafx", kg_grafx_page_size=4096,
    ))
    try:
        route = bundle.initialize_board_route("deadline-board")
        writer = bundle.grafx_pool.get(route.active_path, page_size=4096)
        writer.checkpoint()  # Fixture setup, before Health observation.

        class AdvancingClock:
            now = 100.0

            def monotonic(self):
                self.now += 1.0
                return self.now

        for pool in bundle.grafx_read_pools:
            reader = pool.get(route.active_path, page_size=4096)
            reader._clock = AdvancingClock()
        with bundle.graph_health_observation.scope("deadline-board"):
            with pytest.raises(GraphError) as failure:
                query = "MATCH (n:Decision) RETURN count(n)"
                if door == "scalar":
                    bundle.cypher_executor.execute_read_only("deadline-board", query)
                elif door == "pair":
                    bundle.cypher_executor.execute_read_only_pair("deadline-board", query, query)
                else:
                    bundle.cypher_executor.execute_read_only_batch("deadline-board", [(query, {}, 100)])
        assert failure.value.details["backend_error_code"] == "query_deadline_exceeded"
        # Outside Health the same normal reader retains its prior query policy.
        result = bundle.cypher_executor.execute_read_only(
            "deadline-board", "MATCH (n:Decision) RETURN count(n)",
        )
        assert result["rows"] == [[0]]
    finally:
        for pool in (*bundle.grafx_read_pools, bundle.grafx_pool):
            for path in pool.pooled_paths():
                pool.close(path)
