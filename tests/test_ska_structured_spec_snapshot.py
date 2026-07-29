"""SK-A snapshot fidelity at the Community structured-Spec adapter."""

from __future__ import annotations

from types import SimpleNamespace

from okto_pulse.community.adapters.sqlalchemy_structured_spec import _record


def test_structured_spec_projection_preserves_null_empty_and_authored_fields() -> None:
    row = SimpleNamespace(
        id="spec-ska-snapshot",
        board_id="board-ska-snapshot",
        status="draft",
        version=3,
        archived=False,
        title="Snapshot fidelity",
        description="NULL and [] are distinct canonical values.",
        context="SK-A A1a",
        functional_requirements=None,
        business_rules=[],
        technical_requirements=[
            {
                "id": "tr_snapshot",
                "text": "Preserve the persisted semantic shape.",
                "status": "active",
            }
        ],
        decisions=None,
        acceptance_criteria=[],
        api_contracts=None,
        integration_requirements=[],
        observability_requirements=None,
        test_scenarios=[],
    )

    projected = _record(row)

    assert projected.title == row.title
    assert projected.description == row.description
    assert projected.context == row.context
    assert projected.functional_requirements is None
    assert projected.business_rules == []
    assert projected.decisions is None
    assert projected.acceptance_criteria == []
    assert projected.api_contracts is None
    assert projected.integration_requirements == []
    assert projected.observability_requirements is None
    assert projected.test_scenarios == []
    assert projected.technical_requirements == row.technical_requirements
    assert projected.technical_requirements is not row.technical_requirements
    assert (
        projected.technical_requirements[0]
        is not row.technical_requirements[0]
    )
