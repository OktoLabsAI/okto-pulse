"""Strict Community response-model contract for KG Health schema 1.1."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import pytest
from okto_pulse.core.kg.interfaces.graph_errors import GraphUnavailable

from okto_pulse.community.api.kg_health import (
    KGHealthResponse,
    GraphStorageRoute,
    _graph_storage_route,
    _graph_storage_snapshot_from_bundle,
    OrphanIntegrityProjection,
)


def test_response_model_owns_the_atomic_schema_1_1_fields() -> None:
    fields = KGHealthResponse.model_fields

    assert KGHealthResponse.model_config.get("extra") == "forbid"
    assert fields["health_schema_version"].default == "1.2"
    assert "materialization_state" in fields
    assert "materialization_generation" in fields
    assert "probe_reason_codes" in fields
    assert "native_runtime_budget" in fields
    assert "graph_storage" in fields
    assert list(fields).count("global_outbox_dead_letter_count") == 1
    assert "samples" not in OrphanIntegrityProjection.model_fields
    assert "OrphanIntegritySample" not in str(KGHealthResponse.model_json_schema())


def test_materialization_contract_defaults_fail_closed() -> None:
    response = KGHealthResponse.model_construct(
        board_id="board-schema-1-1",
        correlation_id="corr-schema-1-1",
        checked_at="2026-07-16T00:00:00+00:00",
    )

    assert response.health_schema_version == "1.2"
    assert response.materialization_state == "unknown"
    assert response.materialization_generation is None
    assert response.probe_reason_codes == {
        "board_graph": "materialization_evidence_unavailable",
        "board_census": "materialization_evidence_unavailable",
        "global_discovery": "materialization_evidence_unavailable",
    }
    assert response.graph_storage.board.backend is None
    assert response.graph_storage.board.binding_status == "unavailable"
    assert response.graph_storage.global_graph.backend is None


def test_graph_storage_snapshot_reports_the_authenticated_active_routes(
    tmp_path: Path,
) -> None:
    board_route = SimpleNamespace(
        backend="grafx",
        active_path=tmp_path / "boards" / "board-1" / "grafx" / "generation-7",
        generation="generation-7",
        page_size=8192,
    )
    global_route = SimpleNamespace(
        backend="ladybug",
        active_path=tmp_path / "global" / "discovery.lbug",
        generation="legacy",
        page_size=None,
    )
    resolver = SimpleNamespace(
        inspect_board_route=lambda board_id: board_route,
        inspect_global_route=lambda: global_route,
    )
    bundle = SimpleNamespace(
        resolver=resolver,
        binding_store=SimpleNamespace(root=tmp_path),
    )

    snapshot = _graph_storage_snapshot_from_bundle(bundle, "board-1")

    assert snapshot.board.model_dump() == {
        "scope": "board",
        "backend": "grafx",
        "binding_status": "bound",
        "generation": "generation-7",
        "page_size": 8192,
    }
    assert snapshot.global_graph.model_dump() == {
        "scope": "global",
        "backend": "ladybug",
        "binding_status": "bound",
        "generation": "legacy",
        "page_size": None,
    }
    assert "physical_path" not in GraphStorageRoute.model_fields
    assert str(tmp_path) not in snapshot.model_dump_json()
    assert "boards/board-1" not in snapshot.model_dump_json()
    assert "discovery.lbug" not in snapshot.model_dump_json()


@pytest.mark.parametrize("scope", ["board", "global"])
@pytest.mark.parametrize("case,status", [
    ("bound", "bound"), ("missing", "missing"),
    ("unavailable", "unavailable"), ("outside_root", "unavailable"),
])
def test_public_route_uses_only_inspection_and_preserves_unavailability(tmp_path, scope, case, status):
    calls = []

    class PassiveResolver:
        def inspect_board_route(self, board_id):
            calls.append(("board", board_id))
            return self.snapshot()

        def inspect_global_route(self):
            calls.append(("global", None))
            return self.snapshot()

        def snapshot(self):
            if case in {"missing", "unavailable"}:
                raise GraphUnavailable("private path and credential", details={
                    "reason": "binding_missing" if case == "missing" else "opaque_failure",
                    "physical_path": str(tmp_path / "private"),
                    "token": "private-token",
                })
            return SimpleNamespace(
                active_path=(tmp_path / "private" if case == "bound" else tmp_path.parent / "foreign"),
                backend="grafx", generation="g1", page_size=8192,
            )

        def __getattr__(self, name):
            pytest.fail(f"Health attempted non-observation access: {name}")

    result = _graph_storage_route(
        resolver=PassiveResolver(), storage_root=tmp_path, scope=scope, board_id="authorized-board",
    )
    assert calls == [(scope, "authorized-board" if scope == "board" else None)]
    assert result.binding_status == status
    assert result.backend == ("grafx" if status == "bound" else None)
    encoded = result.model_dump_json()
    assert all(value not in encoded for value in ("physical_path", "private", "foreign", "token", str(tmp_path)))
