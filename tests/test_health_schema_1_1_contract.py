"""Strict Community response-model contract for KG Health schema 1.1."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from okto_pulse.community.api.kg_health import (
    KGHealthResponse,
    _graph_storage_snapshot_from_bundle,
)


def test_response_model_owns_the_atomic_schema_1_1_fields() -> None:
    fields = KGHealthResponse.model_fields

    assert KGHealthResponse.model_config.get("extra") == "forbid"
    assert fields["health_schema_version"].default == "1.1"
    assert "materialization_state" in fields
    assert "materialization_generation" in fields
    assert "probe_reason_codes" in fields
    assert "native_runtime_budget" in fields
    assert "graph_storage" in fields
    assert list(fields).count("global_outbox_dead_letter_count") == 1


def test_materialization_contract_defaults_fail_closed() -> None:
    response = KGHealthResponse.model_construct(
        board_id="board-schema-1-1",
        correlation_id="corr-schema-1-1",
        checked_at="2026-07-16T00:00:00+00:00",
    )

    assert response.health_schema_version == "1.1"
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
        "physical_path": "boards/board-1/grafx/generation-7",
        "generation": "generation-7",
        "page_size": 8192,
    }
    assert snapshot.global_graph.model_dump() == {
        "scope": "global",
        "backend": "ladybug",
        "binding_status": "bound",
        "physical_path": "global/discovery.lbug",
        "generation": "legacy",
        "page_size": None,
    }
