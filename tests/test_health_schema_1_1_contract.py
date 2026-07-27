"""Strict Community response-model contract for KG Health schema 1.1."""

from __future__ import annotations

from okto_pulse.community.api.kg_health import KGHealthResponse


def test_response_model_owns_the_atomic_schema_1_1_fields() -> None:
    fields = KGHealthResponse.model_fields

    assert KGHealthResponse.model_config.get("extra") == "forbid"
    assert fields["health_schema_version"].default == "1.1"
    assert "materialization_state" in fields
    assert "materialization_generation" in fields
    assert "probe_reason_codes" in fields
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
