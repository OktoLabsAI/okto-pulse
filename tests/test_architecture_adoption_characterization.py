"""Characterize the legacy selection gap; these are not AC-ARQ-01 acceptance.

Copy selection currently does not exclude other inherited roots. Keep evidence
of the persisted old contract so rollout cannot reinterpret historical Specs.
"""

import pytest
from sqlalchemy import func, select

from okto_pulse.community.adapters.sqlalchemy_models import ArchitectureDesign, Card, Spec
from okto_pulse.community.adapters.sqlalchemy_resource_gate_service import CommunitySqlAlchemyResourceGateAdapter
from okto_pulse.core.application.use_cases.mcp_spec_crud import McpDeriveSpecCommand, McpDeriveSpecUseCase
from okto_pulse.core.ports.relational_services import register_resource_gate_adapter_factory
from okto_pulse.core.services.architecture_candidates import load_spec_architecture_candidates

from test_spec_b_knowledge_propagation_e2e import (
    ACTOR_ID, BOARD_ID, REST_ACTOR, _seed_refinement_sources,
    spec_b_runtime as spec_b_runtime,
)


def _source(design_id):
    return ArchitectureDesign(
        id=design_id, board_id=BOARD_ID, parent_type="refinement",
        refinement_id="selected-refinement", title=design_id,
        global_description="A client sends an order to the service.",
        created_by=ACTOR_ID,
        entities=[
            {"id": "client", "name": "Client", "entity_type": "web_app", "responsibility": "Send orders."},
            {"id": "service", "name": "Service", "entity_type": "api", "responsibility": "Handle orders."},
        ],
        interfaces=[{
            "id": "orders", "name": "Create order", "endpoint": "POST /orders",
            "description": "Client sends an order to the service.",
            "direction": "source_to_target", "protocol": "REST", "contract_type": "OpenAPI",
            "source_entity_id": "client", "target_entity_id": "service",
        }],
        diagrams=[{
            "id": "context", "title": "Runtime", "diagram_type": "context", "format": "excalidraw_json",
            "adapter_payload": {"type": "excalidraw", "version": 2, "elements": [
                {"id": "client-node", "type": "rectangle", "label": "Client", "linkedEntityId": "client"},
                {"id": "service-node", "type": "rectangle", "label": "Service", "linkedEntityId": "service"},
                {"id": "orders-edge", "type": "arrow", "sourceElementId": "client-node",
                 "targetElementId": "service-node", "linkedInterfaceIds": ["orders"]},
            ], "appState": {}, "files": {}},
        }],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selection,mode,expected_copies", [
    (["chosen"], "copy", 1), ([], "copy", 0), (["chosen"], "reference_only", 0),
])
async def test_legacy_derive_selection_limits_copies_but_not_inherited_population(
    spec_b_runtime, selection, mode, expected_copies,
):
    runtime = spec_b_runtime
    register_resource_gate_adapter_factory(CommunitySqlAlchemyResourceGateAdapter)
    await _seed_refinement_sources(
        runtime, refinement_id="selected-refinement", roots=(),
        architecture_designs=(_source("chosen"), _source("not-chosen")),
    )
    async with runtime.uow_factory(actor=REST_ACTOR) as uow:
        result = await McpDeriveSpecUseCase().execute(
            McpDeriveSpecCommand(
                "refinement", "selected-refinement", architecture_design_ids=selection,
                architecture_propagation_mode=mode,
            ), actor=REST_ACTOR, uow=uow,
        )
        spec_id = result.spec.id
        summary = result.resource_propagation["by_type"]["architecture"]
        assert summary["requested_ids"] == selection
        assert summary["copied"] == expected_copies

    # Reopen after commit: transient write-result attachments cannot establish
    # durable adoption authority for subsequent reads or the future start gate.
    async with runtime.sessions() as db:
        spec = await db.get(Spec, spec_id)
        assert not spec.integration_requirements
        assert await db.scalar(select(func.count()).select_from(Card)) == 0
        copies = (await db.scalars(select(ArchitectureDesign).where(
            ArchitectureDesign.spec_id == spec_id,
        ))).all()
        assert len(copies) == expected_copies
        if copies:
            assert copies[0].source_design_id == "chosen"
        population = await load_spec_architecture_candidates(db, board_id=BOARD_ID, spec_id=spec_id)
        assert population.resolved
        assert {candidate.root_design_id for candidate in population.candidates} == {"chosen", "not-chosen"}
        assert len(population.candidates) == 2
