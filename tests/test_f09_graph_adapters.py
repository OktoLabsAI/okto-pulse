"""F09 Community adapter conformance tests."""

from __future__ import annotations

import inspect
from pathlib import Path

from okto_pulse.community.adapters.kuzu_graph_transaction import _materialize
from okto_pulse.community.adapters.local_storage_ref import (
    local_storage_ref,
    resolve_local_storage_ref,
)
from okto_pulse.core.kg.interfaces.global_discovery_runtime import (
    GlobalDiscoveryRuntime,
)
from okto_pulse.core.kg.interfaces.graph_transaction import GraphStatementResult


COMMUNITY_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = COMMUNITY_ROOT.parent / "okto_labs_pulse_core"


class _NativeCursor:
    def __init__(self) -> None:
        self._rows = iter((("n1", 0.9), ("n2", 0.8)))
        self._next = None
        self.closed = False

    def has_next(self) -> bool:
        try:
            self._next = next(self._rows)
            return True
        except StopIteration:
            return False

    def get_next(self):
        row, self._next = self._next, None
        return list(row)

    def get_column_names(self):
        return ["id", "score"]

    def close(self) -> None:
        self.closed = True


def test_f09_native_cursor_is_materialized_and_closed_in_community() -> None:
    native = _NativeCursor()
    result = _materialize(native)
    assert isinstance(result, GraphStatementResult)
    assert result.rows == (("n1", 0.9), ("n2", 0.8))
    assert result.columns == ("id", "score")
    assert native.closed is True


def test_f09_local_storage_locator_round_trips_only_inside_adapter(tmp_path: Path) -> None:
    artifact = tmp_path / "boards" / "b1" / "graph.lbug"
    ref = local_storage_ref(artifact)
    assert str(artifact) not in ref.token
    assert resolve_local_storage_ref(ref) == artifact.resolve()


def test_f09_global_runtime_exposes_typed_contract_without_native_openers() -> None:
    from okto_pulse.community.adapters.global_discovery_runtime import (
        CommunityGlobalDiscoveryRuntime,
    )

    runtime = CommunityGlobalDiscoveryRuntime()
    assert isinstance(runtime, GlobalDiscoveryRuntime)
    public_methods = {
        name for name, _ in inspect.getmembers(runtime, predicate=callable)
        if not name.startswith("_")
    }
    assert "open_connection" not in public_methods
    assert "open_kuzu_db" not in public_methods
    assert inspect.signature(runtime.execute).return_annotation == "GraphStatementResult"


def test_f09_community_owns_graph_ddl_pool_and_concrete_search_adapter() -> None:
    community_sources = {
        "graph_ddl": COMMUNITY_ROOT / "src" / "okto_pulse" / "community" / "adapters" / "graph_ddl.py",
        "pool": COMMUNITY_ROOT / "src" / "okto_pulse" / "community" / "adapters" / "graph_connection_pool.py",
        "hybrid": COMMUNITY_ROOT / "src" / "okto_pulse" / "community" / "adapters" / "hybrid_search.py",
        "chaos": COMMUNITY_ROOT / "src" / "okto_pulse" / "community" / "adapters" / "kg_chaos_executor.py",
    }
    assert all(path.exists() for path in community_sources.values())
    ddl = community_sources["graph_ddl"].read_text(encoding="utf-8")
    assert "CREATE NODE TABLE" in ddl
    assert "CREATE REL TABLE" in ddl
    assert not (CORE_ROOT / "src" / "okto_pulse" / "core" / "kg" / "connection_pool.py").exists()
    assert not (
        CORE_ROOT / "src" / "okto_pulse" / "core" / "kg" / "hybrid_search" / "kuzu_adapter.py"
    ).exists()
